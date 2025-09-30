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

# ==========================================
# === Konfigurasi Aplikasi ===
# ==========================================
st.set_page_config(page_title="PKKPR → SHP + Overlay", layout="wide")
st.title("📑 PKKPR → Shapefile Converter & Overlay Tapak Proyek")

# ==========================================
# === Fungsi Helper ===
# ==========================================
def get_utm_info(lon, lat):
    """Deteksi zona UTM dari koordinat lon/lat"""
    zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        return 32600 + zone, f"{zone}N"  # UTM utara
    else:
        return 32700 + zone, f"{zone}S"  # UTM selatan

def save_shapefile(gdf, folder_name, zip_name):
    """Simpan GeoDataFrame ke shapefile ZIP"""
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
    """Ambil angka luas dari teks PDF"""
    match = re.search(r"([\d\.\,]+)", line)
    if not match:
        return None
    num_str = match.group(1).replace(".", "").replace(",", ".")
    try:
        return float(num_str)
    except:
        return None

def extract_from_pdf(uploaded_file):
    """Ekstraksi koordinat & luas dari dokumen PDF PKKPR"""
    coords, luas_disetujui, luas_dimohon = [], None, None

    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            # Ambil teks
            text = page.extract_text()
            if text:
                for line in text.split("\n"):
                    low = line.lower()
                    if "luas tanah yang disetujui" in low and luas_disetujui is None:
                        luas_disetujui = parse_luas(line)
                    elif "luas tanah yang dimohon" in low and luas_dimohon is None:
                        luas_dimohon = parse_luas(line)

            # Ambil tabel koordinat
            for table in page.extract_tables():
                for row in table:
                    if row and len(row) >= 3:
                        try:
                            lon, lat = float(row[1]), float(row[2])
                            if 95 <= lon <= 141 and -11 <= lat <= 6:
                                coords.append((lon, lat))
                        except:
                            continue

    # Tentukan luas prioritas
    if luas_disetujui is not None:
        luas, label = luas_disetujui, "disetujui"
    elif luas_dimohon is not None:
        luas, label = luas_dimohon, "dimohon"
    else:
        luas, label = None, None

    return coords, luas, label, luas_disetujui, luas_dimohon

def build_geodata(coords):
    """Bangun GeoDataFrame dari koordinat"""
    if not coords:
        return None, None

    gdf_points = gpd.GeoDataFrame(
        pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
        geometry=[Point(xy) for xy in coords],
        crs="EPSG:4326"
    )

    gdf_polygon = None
    if len(coords) > 2:
        if coords[0] != coords[-1]:
            coords.append(coords[0])  # Tutup polygon
        gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(coords)], crs="EPSG:4326")

    return gdf_points, gdf_polygon

# ==========================================
# === Upload PKKPR (PDF / SHP) ===
# ==========================================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

coords, gdf_points, gdf_polygon = [], None, None
luas_doc, luas_doc_label, luas_disetujui, luas_dimohon = None, None, None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.endswith(".pdf"):
        coords, luas_doc, luas_doc_label, luas_disetujui, luas_dimohon = extract_from_pdf(uploaded_pkkpr)
        gdf_points, gdf_polygon = build_geodata(coords)
        with col2: st.success(f"{len(coords)} titik koordinat")

    elif uploaded_pkkpr.name.endswith(".zip"):
        if os.path.exists("pkkpr_shp"): shutil.rmtree("pkkpr_shp")
        with zipfile.ZipFile(uploaded_pkkpr, "r") as z: z.extractall("pkkpr_shp")
        gdf_polygon = gpd.read_file("pkkpr_shp")
        if gdf_polygon.crs is None: gdf_polygon.set_crs(epsg=4326, inplace=True)
        with col2: st.success("Shapefile PKKPR terbaca")

# Ekspor SHP PKKPR
if gdf_polygon is not None:
    zip_pkkpr = save_shapefile(gdf_polygon, "out_pkkpr", "PKKPR_Hasil")
    with open(zip_pkkpr, "rb") as f:
        st.download_button("⬇️ Download SHP PKKPR (ZIP)", f, "PKKPR_Hasil.zip")

# ==========================================
# === Upload Tapak Proyek (SHP) ===
# ==========================================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

gdf_tapak = None
if uploaded_tapak:
    try:
        if os.path.exists("tapak_shp"): shutil.rmtree("tapak_shp")
        with zipfile.ZipFile(uploaded_tapak, "r") as z: z.extractall("tapak_shp")
        gdf_tapak = gpd.read_file("tapak_shp")
        if gdf_tapak.crs is None: gdf_tapak.set_crs(epsg=4326, inplace=True)
        with col2: st.success("Shapefile Tapak terbaca")
    except Exception as e:
        st.error(f"Gagal membaca Tapak: {e}")

# ==========================================
# === Analisis Overlay ===
# ==========================================
if gdf_polygon is not None and gdf_tapak is not None:
    st.subheader("📊 Hasil Analisis Overlay")

    # Reproyeksi ke UTM
    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)

    gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg)
    gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)

    luas_tapak = gdf_tapak_utm.area.sum()
    luas_pkkpr_calc = gdf_polygon_utm.area.sum()
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak - luas_overlap

    # Tampilkan luas
    luas_doc_str = f"{luas_doc:,.2f} m² ({luas_doc_label})" if luas_doc else "-"
    st.info(f"""
    **Analisis Luas Tapak Proyek (Zona UTM {utm_zone}):**
    - Total Tapak Proyek: {luas_tapak:,.2f} m²
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (hitung): {luas_pkkpr_calc:,.2f} m²
    - Luas di dalam PKKPR: **{luas_overlap:,.2f} m²**
    - Luas di luar PKKPR: **{luas_outside:,.2f} m²**
    """)

    st.markdown("---")

    # ==========================================
    # === Preview Peta Interaktif ===
    # ==========================================
    st.subheader("🌍 Preview Peta Interaktif")

    basemap = st.selectbox("Pilih Basemap:", ["OpenStreetMap", "Esri World Imagery"])
    tile = xyz["Esri"]["WorldImagery"] if basemap == "Esri World Imagery" else xyz["OpenStreetMap"]["Mapnik"]

    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=tile)

    folium.TileLayer(xyz["OpenStreetMap"]["Mapnik"], name="OpenStreetMap").add_to(m)
    folium.TileLayer(xyz["Esri"]["WorldImagery"], name="Esri Imagery").add_to(m)

    # PKKPR
    folium.GeoJson(gdf_polygon.to_crs(epsg=4326),
                   name="PKKPR",
                   style_function=lambda x: {"color": "yellow", "weight": 2, "fillOpacity": 0}).add_to(m)

    # Tapak
    folium.GeoJson(gdf_tapak.to_crs(epsg=4326),
                   name="Tapak Proyek",
                   style_function=lambda x: {"color": "red", "weight": 1, "fillColor": "red", "fillOpacity": 0.4}).add_to(m)

    # Titik koordinat
    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker([row.geometry.y, row.geometry.x],
                                radius=5, color="black",
                                fill=True, fill_color="orange",
                                popup=f"Titik {i+1}").add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=900, height=600)

    st.markdown("---")

    # ==========================================
    # === Layout Peta PNG ===
    # ==========================================
    st.subheader("🖼️ Layout Peta (PNG)")

    out_png = "layout_peta.png"
    fig, ax = plt.subplots(figsize=(10, 10))

    gdf_polygon.to_crs(epsg=3857).plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)
    gdf_tapak.to_crs(epsg=3857).plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")
    if gdf_points is not None:
        gdf_points.to_crs(epsg=3857).plot(ax=ax, color="orange", edgecolor="black", markersize=50)

    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery, attribution=False)

    legend = [
        mpatches.Patch(facecolor="none", edgecolor="yellow", label="PKKPR"),
        mpatches.Patch(facecolor="red", alpha=0.4, label="Tapak Proyek"),
        mlines.Line2D([], [], color="orange", marker="o", linestyle="None", markersize=8, label="PKKPR (Titik)")
    ]
    ax.legend(handles=legend, loc="upper right")
    ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14)
    ax.set_axis_off()

    plt.savefig(out_png, dpi=300, bbox_inches="tight")
    with open(out_png, "rb") as f:
        st.download_button("⬇️ Download Layout Peta (PNG)", f, "layout_peta.png", mime="image/png")
    st.pyplot(fig)
