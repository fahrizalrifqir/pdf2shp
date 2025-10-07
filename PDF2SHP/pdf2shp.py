import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, shutil, re
from shapely.geometry import Point, Polygon
import folium
from streamlit_folium import st_folium
import pdfplumber
import matplotlib.pyplot as plt
import contextily as ctx
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
from folium.plugins import Fullscreen
import xyzservices.providers as xyz

# ======================
# === Konfigurasi App ===
# ======================
st.set_page_config(page_title="PKKPR → SHP + Overlay", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")

# ======================
# === Fungsi Helper ===
# ======================
def get_utm_info(lon, lat):
    zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        epsg = 32600 + zone
        zone_label = f"{zone}N"
    else:
        epsg = 32700 + zone
        zone_label = f"{zone}S"
    return epsg, zone_label

def save_shapefile(gdf, folder_name, zip_name):
    if os.path.exists(folder_name):
        shutil.rmtree(folder_name)
    os.makedirs(folder_name, exist_ok=True)
    shp_path = os.path.join(folder_name, "data.shp")
    gdf.to_file(shp_path)
    zip_path = f"{zip_name}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for file in os.listdir(folder_name):
            zf.write(os.path.join(folder_name, file), arcname=file)
    return zip_path

def parse_luas(line):
    match = re.search(r"([\d\.\,]+)", line)
    if not match:
        return None
    num_str = match.group(1)
    if "." in num_str and "," in num_str:
        num_str = num_str.replace(".", "").replace(",", ".")
    elif "," in num_str:
        num_str = num_str.replace(",", ".")
    try:
        return float(num_str)
    except:
        return None

# ======================
# === Upload PKKPR ===
# ======================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

coords, gdf_points, gdf_polygon = [], None, None
luas_pkkpr_doc, luas_pkkpr_doc_label = None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.endswith(".pdf"):
        coords_disetujui, coords_dimohon, coords_plain = [], [], []
        luas_disetujui, luas_dimohon = None, None
        table_mode = None

        with pdfplumber.open(uploaded_pkkpr) as pdf:
            for page in pdf.pages:
                table = page.extract_table()
                if table:
                    for row in table:
                        if len(row) >= 3 and row[1] and row[2]:
                            try:
                                lon, lat = float(row[1]), float(row[2])
                                if 95 <= lon <= 141 and -11 <= lat <= 6:
                                    if table_mode == "disetujui":
                                        coords_disetujui.append((lon, lat))
                                    elif table_mode == "dimohon":
                                        coords_dimohon.append((lon, lat))
                                    else:
                                        coords_plain.append((lon, lat))
                            except:
                                continue

                text = page.extract_text()
                if not text:
                    continue
                for line in text.split("\n"):
                    low = line.lower().strip()

                    if "luas tanah yang disetujui" in low and luas_disetujui is None:
                        luas_disetujui = parse_luas(line)
                    elif "luas tanah yang dimohon" in low and luas_dimohon is None:
                        luas_dimohon = parse_luas(line)

                    if "koordinat" in low and "disetujui" in low:
                        table_mode = "disetujui"
                        continue
                    elif "koordinat" in low and "dimohon" in low:
                        table_mode = "dimohon"
                        continue

                    m = re.match(r"^\s*\d+\s+([0-9\.\-]+)\s+([0-9\.\-]+)", line)
                    if m:
                        try:
                            lon, lat = float(m.group(1)), float(m.group(2))
                            if 95 <= lon <= 141 and -11 <= lat <= 6:
                                if table_mode == "disetujui":
                                    coords_disetujui.append((lon, lat))
                                elif table_mode == "dimohon":
                                    coords_dimohon.append((lon, lat))
                                else:
                                    coords_plain.append((lon, lat))
                        except:
                            continue

        # === Pilih koordinat berdasarkan hirarki ===
        if coords_disetujui:
            coords = coords_disetujui
            luas_pkkpr_doc = luas_disetujui if luas_disetujui else luas_dimohon
            luas_pkkpr_doc_label = "disetujui"
        elif coords_dimohon:
            coords = coords_dimohon
            luas_pkkpr_doc = luas_dimohon
            luas_pkkpr_doc_label = "dimohon"
        elif coords_plain:
            coords = coords_plain
            luas_pkkpr_doc = None
            luas_pkkpr_doc_label = "tanpa judul"

        # buat geodataframe
        if coords:
            gdf_points = gpd.GeoDataFrame(
                pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
                geometry=[Point(xy) for xy in coords],
                crs="EPSG:4326"
            )
            if len(coords) > 2:
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                poly = Polygon(coords)
                gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")

        with col2:
            st.markdown(f"<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ {len(coords)} titik ({luas_pkkpr_doc_label})</p>", unsafe_allow_html=True)

    elif uploaded_pkkpr.name.endswith(".zip"):
        if os.path.exists("pkkpr_shp"):
            shutil.rmtree("pkkpr_shp")
        with zipfile.ZipFile(uploaded_pkkpr, "r") as z:
            z.extractall("pkkpr_shp")
        gdf_polygon = gpd.read_file("pkkpr_shp")
        if gdf_polygon.crs is None:
            gdf_polygon.set_crs(epsg=4326, inplace=True)
        with col2:
            st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅</p>", unsafe_allow_html=True)

# === Ekspor SHP PKKPR ===
if gdf_polygon is not None:
    zip_pkkpr_only = save_shapefile(gdf_polygon, "out_pkkpr_only", "PKKPR_Hasil_Konversi")
    with open(zip_pkkpr_only, "rb") as f:
        st.download_button("⬇️ Download SHP PKKPR (ZIP)", f, file_name="PKKPR_Hasil_Konversi.zip", mime="application/zip")

# ======================
# === Analisis PKKPR Sendiri ===
# ======================
if gdf_polygon is not None:
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)

    gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)
    luas_pkkpr_hitung = gdf_polygon_utm.area.sum()

    luas_doc_str = f"{luas_pkkpr_doc:,.2f} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"

    st.info(f"""
    *(Proyeksi UTM Zona {utm_zone}):**
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (hitung dari geometri): {luas_pkkpr_hitung:,.2f} m²
    """)

    st.markdown("---")

# ================================
# === Upload Tapak Proyek (SHP) ===
# ================================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

if uploaded_tapak:
    try:
        if os.path.exists("tapak_shp"):
            shutil.rmtree("tapak_shp")
        with zipfile.ZipFile(uploaded_tapak, "r") as z:
            z.extractall("tapak_shp")
        gdf_tapak = gpd.read_file("tapak_shp")
        if gdf_tapak.crs is None:
            gdf_tapak.set_crs(epsg=4326, inplace=True)
        with col2:
            st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅</p>", unsafe_allow_html=True)
    except Exception as e:
        gdf_tapak = None
        with col2:
            st.markdown("<p style='color: red; font-weight: bold; padding-top: 3.5rem;'>❌ Gagal dibaca</p>", unsafe_allow_html=True)
        st.error(f"Error: {e}")
else:
    gdf_tapak = None

# ======================
# === Analisis Overlay ===
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    st.subheader("📊 Analisis Overlay PKKPR & Tapak Proyek")

    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)

    gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg)
    gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)

    luas_tapak = gdf_tapak_utm.area.sum()
    luas_pkkpr_hitung = gdf_polygon_utm.area.sum()
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak - luas_overlap

    luas_doc_str = f"{luas_pkkpr_doc:,.2f} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"

    st.info(f"""
    **Analisis Luas Tapak Proyek (Proyeksi UTM Zona {utm_zone}):**
    - Total Luas Tapak Proyek: {luas_tapak:,.2f} m²
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (hitung dari geometri): {luas_pkkpr_hitung:,.2f} m²
    - Luas Tapak Proyek di dalam PKKPR: **{luas_overlap:,.2f} m²**
    - Luas Tapak Proyek di luar PKKPR: **{luas_outside:,.2f} m²**
    """)

    st.markdown("---")

# ======================
# === Preview Interaktif ===
# ======================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")

    tile_choice = st.selectbox("Pilih Basemap:", ["OpenStreetMap", "Esri World Imagery"])
    tile_provider = xyz["Esri"]["WorldImagery"] if tile_choice == "Esri World Imagery" else xyz["OpenStreetMap"]["Mapnik"]

    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=tile_provider)

    Fullscreen(position="bottomleft").add_to(m)

    folium.GeoJson(
        gdf_polygon.to_crs(epsg=4326),
        name="PKKPR",
        style_function=lambda x: {"color": "yellow", "weight": 2, "fillOpacity": 0}
    ).add_to(m)

    if gdf_tapak is not None:
        folium.GeoJson(
            gdf_tapak.to_crs(epsg=4326),
            name="Tapak Proyek",
            style_function=lambda x: {"color": "red", "weight": 1, "fillColor": "red", "fillOpacity": 0.4}
        ).add_to(m)

    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=5,
                color="black",
                fill=True,
                fill_color="orange",
                fill_opacity=1,
                popup=f"Titik {i+1}"
            ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=900, height=600)

    st.markdown("---")

# ======================
# === Layout Peta PNG ===
# ======================
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG) - Auto Size")

    out_png = "layout_peta.png"

    gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
    xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
    width = xmax - xmin
    height = ymax - ymin

    if width > height:
        figsize = (14, 10)
    else:
        figsize = (10, 14)

    fig, ax = plt.subplots(figsize=figsize, dpi=150)

    gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)

    if gdf_tapak is not None:
        gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
        gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")

    if gdf_points is not None:
        gdf_points_3857 = gdf_points.to_crs(epsg=3857)
        gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=25)

    basemap_source = ctx.providers.OpenStreetMap.Mapnik if (gdf_tapak is not None and gdf_tapak_3857.area.sum() < 0.01 * width * height) else ctx.providers.Esri.WorldImagery
    ctx.add_basemap(ax, crs=3857, source=basemap_source, attribution=False)

    dx = width * 0.05
    dy = height * 0.05
    ax.set_xlim(xmin - dx, xmax + dx)
    ax.set_ylim(ymin - dy, ymax + dy)

    legend_elements = [
        mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black",
                      linestyle="None", markersize=5, label="PKKPR (Titik)"),
        mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=1.5, label="PKKPR (Polygon)"),
        mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
    ]

    leg = ax.legend(
        handles=legend_elements,
        title="Legenda",
        loc="upper right",
        bbox_to_anchor=(0.98, 0.98),
        fontsize=8,
        title_fontsize=9,
        markerscale=0.8,
        labelspacing=0.3,
        frameon=True,
        facecolor="white"
    )
    leg.get_frame().set_alpha(0.7)

    ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14, weight="bold")
    ax.set_axis_off()

    plt.savefig(out_png, dpi=300, bbox_inches="tight")
    with open(out_png, "rb") as f:
        st.download_button("⬇️ Download Layout Peta (PNG, Auto)", f, "layout_peta.png", mime="image/png")

    st.pyplot(fig)
