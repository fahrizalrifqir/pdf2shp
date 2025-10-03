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
import xyzservices.providers as xyz
import matplotlib.transforms as mtransforms

# ======================
# === Konfigurasi App ===
# ======================
st.set_page_config(page_title="PDF/Shapefile PKKPR → SHP + Overlay", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")

# ======================
# === Fungsi Helper ===
# ======================
def get_utm_info(lon, lat):
    """Deteksi zona UTM dari koordinat lon/lat"""
    zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        epsg = 32600 + zone
        zone_label = f"{zone}N"
    else:
        epsg = 32700 + zone
        zone_label = f"{zone}S"
    return epsg, zone_label

def save_shapefile(gdf, folder_name, zip_name):
    """Simpan GeoDataFrame ke shapefile dan zip"""
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
    """Ambil angka luas dari teks PDF (format Indonesia)."""
    match = re.search(r"([\d\.\,]+)", line)
    if not match:
        return None
    num_str = match.group(1)
    if "." in num_str and "," in num_str:  # contoh: 149.525,32
        num_str = num_str.replace(".", "").replace(",", ".")
    elif "," in num_str:  # contoh: 162,5
        num_str = num_str.replace(",", ".")
    try:
        return float(num_str)
    except:
        return None

def add_north_arrow(ax, size=0.1, loc_x=0.95, loc_y=0.95):
    """Tambahkan north arrow ke plot"""
    ax.annotate('N',
                xy=(loc_x, loc_y), xytext=(loc_x, loc_y-size),
                xycoords='axes fraction',
                fontsize=14, ha='center',
                arrowprops=dict(facecolor='black', width=5, headwidth=15))

def add_scale_bar(ax, length, location=(0.1, 0.05), linewidth=3):
    """Tambahkan skala grafis sederhana (dalam meter)"""
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    scalebar_x = x0 + (x1 - x0) * location[0]
    scalebar_y = y0 + (y1 - y0) * location[1]

    ax.hlines(scalebar_y, scalebar_x, scalebar_x + length, colors='black', linewidth=linewidth)
    ax.vlines([scalebar_x, scalebar_x + length], scalebar_y - (y1-y0)*0.005, scalebar_y + (y1-y0)*0.005, colors='black', linewidth=linewidth)

    ax.text(scalebar_x + length/2, scalebar_y + (y1-y0)*0.01,
            f"{int(length):,} m", ha='center', va='bottom', fontsize=10, color='black')

def choose_scale_length(width_m):
    """Pilih panjang skala grafis otomatis"""
    candidates = [100, 200, 500, 1000, 2000, 5000, 10000]
    for c in candidates:
        if width_m / c < 10:
            return c
    return candidates[-1]

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
        coords_disetujui, coords_dimohon = [], []
        luas_disetujui, luas_dimohon = None, None
        table_mode = None

        with pdfplumber.open(uploaded_pkkpr) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue

                for line in text.split("\n"):
                    low = line.lower().strip()

                    # deteksi luas tanah
                    if "luas tanah yang disetujui" in low and luas_disetujui is None:
                        luas_disetujui = parse_luas(line)
                    elif "luas tanah yang dimohon" in low and luas_dimohon is None:
                        luas_dimohon = parse_luas(line)

                    # deteksi judul tabel koordinat
                    if "tabel koordinat yang disetujui" in low:
                        table_mode = "disetujui"
                        continue
                    elif "tabel koordinat yang dimohon" in low:
                        table_mode = "dimohon"
                        continue

                    # regex cari koordinat: "No, Bujur, Lintang"
                    m = re.match(r"^\d+\s+([0-9\.\-]+)\s+([0-9\.\-]+)", line)
                    if m:
                        try:
                            lon, lat = float(m.group(1)), float(m.group(2))
                            if 95 <= lon <= 141 and -11 <= lat <= 6:
                                if table_mode == "disetujui":
                                    coords_disetujui.append((lon, lat))
                                elif table_mode == "dimohon":
                                    coords_dimohon.append((lon, lat))
                        except:
                            continue

        # pilih koordinat: disetujui > dimohon
        if coords_disetujui:
            coords = coords_disetujui
            luas_pkkpr_doc = luas_disetujui
            luas_pkkpr_doc_label = "disetujui"
        elif coords_dimohon:
            coords = coords_dimohon
            luas_pkkpr_doc = luas_dimohon
            luas_pkkpr_doc_label = "dimohon"

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
            st.markdown(f"<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ {len(coords)} titik</p>", unsafe_allow_html=True)

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
    st.subheader("📊 Hasil Analisis Overlay")

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
    - Luas di dalam PKKPR: **{luas_overlap:,.2f} m²**
    - Luas di luar PKKPR: **{luas_outside:,.2f} m²**
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
# === Layout Peta PNG (A3) ===
# ======================
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG) - Format A3")

    out_png = "layout_peta.png"
    fig, ax = plt.subplots(figsize=(16.5, 11.7), dpi=150)  # A3 landscape

    # plot geometri
    gdf_polygon.to_crs(epsg=3857).plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)
    if gdf_tapak is not None:
        gdf_tapak.to_crs(epsg=3857).plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")
    if gdf_points is not None:
        gdf_points.to_crs(epsg=3857).plot(ax=ax, color="orange", edgecolor="black", markersize=60)

    # basemap
    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery, attribution=False)

    # cek rasio bounding box geometri → tentukan posisi legenda
    bounds = gdf_polygon.to_crs(epsg=3857).total_bounds
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]

    if width >= height:
        leg_loc = "upper left"
        leg_anchor = (1.02, 1)
    else:
        leg_loc = "upper center"
        leg_anchor = (0.5, -0.05)

    # legenda
    legend_elements = [
        mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black",
                      linestyle="None", markersize=8, label="PKKPR (Titik)"),
        mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=2, label="PKKPR (Polygon)"),
        mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
    ]
    leg = ax.legend(
        handles=legend_elements,
        title="Legenda",
        loc=leg_loc,
        bbox_to_anchor=leg_anchor,
        fontsize=12,
        title_fontsize=14,
        frameon=True,
        facecolor="white"
    )
    leg.get_frame().set_alpha(0.7)

    # judul peta
    ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=18, weight="bold")

    # north arrow
    add_north_arrow(ax, size=0.08, loc_x=0.95, loc_y=0.95)

    # skala grafis otomatis
    scale_length = choose_scale_length(width)
    add_scale_bar(ax, length=scale_length, location=(0.1, 0.05))

    # hilangkan axis
    ax.set_axis_off()

    plt.savefig(out_png, dpi=300, bbox_inches="tight")
    with open(out_png, "rb") as f:
        st.download_button("⬇️ Download Layout Peta (PNG, A3)", f, "layout_peta.png", mime="image/png")

    st.pyplot(fig)
