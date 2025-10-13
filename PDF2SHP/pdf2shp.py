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


def dms_to_decimal(dms_str):
    """Konversi koordinat DMS (derajat°, menit', detik\") ke desimal."""
    if not dms_str:
        return None
    dms_str = dms_str.strip().replace(" ", "")
    m = re.match(r"(\d+)[°](\d+)'([\d\.]+)\"?([NSEW])", dms_str)
    if not m:
        return None
    deg, minute, second, direction = m.groups()
    decimal = float(deg) + float(minute) / 60 + float(second) / 3600
    if direction in ["S", "W"]:
        decimal *= -1
    return decimal


def parse_luas_from_text(text):
    """
    Cari nilai luas tanah dari teks PKKPR dengan prioritas:
    1. disetujui
    2. dimohon
    3. tanpa judul
    """
    text_clean = re.sub(r"\s+", " ", (text or "").lower())
    luas_matches = re.findall(r"luas\s*tanah\s*yang\s*(dimohon|disetujui)\s*[:\-]?\s*([\d\.,]+)", text_clean)
    if not luas_matches:
        return None, "tanpa judul"

    luas_data = {}
    for label, num_str in luas_matches:
        num_str = re.sub(r"[^\d\.,]", "", num_str)
        if "." in num_str and "," in num_str:
            num_str = num_str.replace(".", "").replace(",", ".")
        elif "," in num_str and "." not in num_str:
            num_str = num_str.replace(",", ".")
        elif num_str.count(".") > 1:
            parts = num_str.split(".")
            num_str = "".join(parts[:-1]) + "." + parts[-1]
        try:
            luas_data[label] = float(num_str)
        except:
            continue

    if "disetujui" in luas_data:
        return luas_data["disetujui"], "disetujui"
    elif "dimohon" in luas_data:
        return luas_data["dimohon"], "dimohon"
    else:
        return None, "tanpa judul"


def format_angka_id(value):
    try:
        return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(value)


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
        full_text = ""
        blok_aktif = None

        with pdfplumber.open(uploaded_pkkpr) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                full_text += "\n" + text

                for line in text.split("\n"):
                    low = line.lower()
                    if "koordinat" in low and "disetujui" in low:
                        blok_aktif = "disetujui"
                    elif "koordinat" in low and "dimohon" in low:
                        blok_aktif = "dimohon"

                    # Format DMS
                    dms_parts = re.findall(r"\d+°\d+'\d+\.\d+\"[NSEW]", line)
                    if len(dms_parts) >= 2:
                        lon = dms_to_decimal(dms_parts[0])
                        lat = dms_to_decimal(dms_parts[1])
                        if lon and lat and 95 <= lon <= 141 and -11 <= lat <= 6:
                            if blok_aktif == "disetujui":
                                coords_disetujui.append((lon, lat))
                            elif blok_aktif == "dimohon":
                                coords_dimohon.append((lon, lat))
                            else:
                                coords_plain.append((lon, lat))
                        continue

                    # Format desimal
                    mline = re.findall(r"[-+]?\d+[.,]\d+", line)
                    if len(mline) >= 2:
                        try:
                            lon = float(mline[0].replace(",", "."))
                            lat = float(mline[1].replace(",", "."))
                            if 95 <= lon <= 141 and -11 <= lat <= 6:
                                if blok_aktif == "disetujui":
                                    coords_disetujui.append((lon, lat))
                                elif blok_aktif == "dimohon":
                                    coords_dimohon.append((lon, lat))
                                else:
                                    coords_plain.append((lon, lat))
                        except:
                            pass

                # Cek tabel koordinat
                tables = page.extract_tables()
                if tables:
                    for tb in tables:
                        for row in tb:
                            if not row:
                                continue
                            row_join = " ".join([str(x) for x in row if x])

                            # Format DMS
                            if "°" in row_join:
                                parts = re.findall(r"\d+°\d+'\d+\.\d+\"[NSEW]", row_join)
                                if len(parts) >= 2:
                                    lon = dms_to_decimal(parts[0])
                                    lat = dms_to_decimal(parts[1])
                                    if lon and lat and 95 <= lon <= 141 and -11 <= lat <= 6:
                                        coords_plain.append((lon, lat))
                                continue

                            # Format desimal
                            nums = re.findall(r"[-+]?\d+[.,]\d+", row_join)
                            if len(nums) >= 2:
                                try:
                                    lon = float(nums[0].replace(",", "."))
                                    lat = float(nums[1].replace(",", "."))
                                    if 95 <= lon <= 141 and -11 <= lat <= 6:
                                        coords_plain.append((lon, lat))
                                except:
                                    pass

        # === Prioritas pembacaan koordinat ===
        if coords_disetujui:
            coords = coords_disetujui
            coords_label = "disetujui"
        elif coords_dimohon:
            coords = coords_dimohon
            coords_label = "dimohon"
        elif coords_plain:
            coords = coords_plain
            coords_label = "tanpa judul"
        else:
            coords_label = "tidak ditemukan"

        luas_pkkpr_doc, luas_pkkpr_doc_label = parse_luas_from_text(full_text)

        coords = list(dict.fromkeys(coords))

        # Koreksi urutan lon-lat
        flipped_coords = []
        if coords:
            first_x, first_y = coords[0]
            if -11 <= first_x <= 6 and 95 <= first_y <= 141:
                flipped_coords = [(y, x) for x, y in coords]
            else:
                flipped_coords = [(x, y) for x, y in coords]

        # Buat GeoDataFrame
        if flipped_coords:
            flipped_coords = list(dict.fromkeys(flipped_coords))
            if flipped_coords[0] != flipped_coords[-1]:
                flipped_coords.append(flipped_coords[0])

            gdf_points = gpd.GeoDataFrame(
                pd.DataFrame(flipped_coords, columns=["Longitude", "Latitude"]),
                geometry=[Point(xy) for xy in flipped_coords],
                crs="EPSG:4326"
            )
            gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(flipped_coords)], crs="EPSG:4326")

        with col2:
            label_display = coords_label or "tidak ditemukan"
            count_display = len(flipped_coords) if flipped_coords else 0
            st.markdown(
                f"<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ {count_display} titik ({label_display})</p>",
                unsafe_allow_html=True,
            )

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
    gdf_polygon_3857 = gdf_polygon.to_crs(epsg=3857)
    luas_pkkpr_mercator = gdf_polygon_3857.area.sum()

    luas_doc_str = f"{format_angka_id(luas_pkkpr_doc)} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(f"""
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (UTM Zona {utm_zone}): {format_angka_id(luas_pkkpr_hitung)} m²
    - Luas PKKPR (proyeksi WGS 84 / Mercator): {format_angka_id(luas_pkkpr_mercator)} m²
    """)
    st.markdown("---")

# (Bagian overlay, preview interaktif, dan layout peta PNG tetap seperti sebelumnya)

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
    luas_doc_str = f"{format_angka_id(luas_pkkpr_doc)} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(f"""
    **Analisis Luas Tapak Proyek :**
    - Total Luas Tapak Proyek: {format_angka_id(luas_tapak)} m²
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (UTM Zona {utm_zone}): {format_angka_id(luas_pkkpr_hitung)} m²
    - Luas Tapak Proyek UTM di dalam PKKPR: **{format_angka_id(luas_overlap)} m²**
    - Luas Tapak Proyek UTM di luar PKKPR: **{format_angka_id(luas_outside)} m²**
    """)
    st.markdown("---")

# ======================
# === Preview Interaktif ===
# ======================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")

    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17)
    Fullscreen(position="bottomleft").add_to(m)

    folium.TileLayer(
        tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="© OpenStreetMap contributors",
        name="OpenStreetMap"
    ).add_to(m)

    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles © Esri",
        name="Esri World Imagery"
    ).add_to(m)

    folium.TileLayer(
        tiles="https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png",
        attr="© CartoDB",
        name="CartoDB Positron"
    ).add_to(m)

    folium.TileLayer(
        tiles="https://stamen-tiles.a.ssl.fastly.net/terrain/{z}/{x}/{y}.png",
        attr="Map tiles © Stamen Design, CC BY 3.0 — Map data © OpenStreetMap contributors",
        name="Stamen Terrain"
    ).add_to(m)

    folium.GeoJson(
        gdf_polygon.to_crs(epsg=4326),
        name="PKKPR",
        style_function=lambda x: {"color": "yellow", "weight": 2, "fillOpacity": 0}
    ).add_to(m)

    if 'gdf_tapak' in locals() and gdf_tapak is not None:
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

    folium.LayerControl(collapsed=True, position="topright").add_to(m)
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
    figsize = (14, 10) if width > height else (10, 14)

    fig, ax = plt.subplots(figsize=figsize, dpi=150)
    gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)

    if 'gdf_tapak' in locals() and gdf_tapak is not None:
        gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
        gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")

    if gdf_points is not None:
        gdf_points_3857 = gdf_points.to_crs(epsg=3857)
        gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=25)

    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery, attribution=False)
    dx, dy = width * 0.05, height * 0.05
    ax.set_xlim(xmin - dx, xmax + dx)
    ax.set_ylim(ymin - dy, ymax + dy)

    legend_elements = [
        mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black", linestyle="None", markersize=5, label="PKKPR (Titik)"),
        mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=1.5, label="PKKPR (Polygon)"),
        mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
    ]
    leg = ax.legend(handles=legend_elements, title="Legenda", loc="upper right",
                    bbox_to_anchor=(0.98, 0.98), fontsize=8, title_fontsize=9,
                    markerscale=0.8, labelspacing=0.3, frameon=True, facecolor="white")
    leg.get_frame().set_alpha(0.7)
    ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14, weight="bold")
    ax.set_axis_off()
    plt.savefig(out_png, dpi=300, bbox_inches="tight")

    with open(out_png, "rb") as f:
        st.download_button("⬇️ Download Layout Peta (PNG, Auto)", f, "layout_peta.png", mime="image/png")

    st.pyplot(fig)

