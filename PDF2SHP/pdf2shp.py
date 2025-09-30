import streamlit as st
import geopandas as gpd
import folium
from streamlit_folium import st_folium
import os
import zipfile
import tempfile
from shapely.geometry import Polygon
import fitz  # PyMuPDF
import re

st.set_page_config(page_title="📄 PDF2SHP PKKPR", layout="wide")
st.title("📄 PDF2SHP – Konversi PKKPR ke SHP + Analisis Tapak")

# =========================
# Helper functions
# =========================

def save_shapefile(gdf, foldername, layername):
    tmpdir = tempfile.mkdtemp()
    outdir = os.path.join(tmpdir, foldername)
    os.makedirs(outdir, exist_ok=True)
    outpath = os.path.join(outdir, f"{layername}.shp")
    gdf.to_file(outpath)

    zip_path = f"{outdir}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in os.listdir(outdir):
            zf.write(os.path.join(outdir, f), arcname=f)
    return zip_path

def get_utm_epsg(lon, lat):
    zone = int((lon + 180) / 6) + 1
    return 32600 + zone if lat >= 0 else 32700 + zone

def extract_luas_from_pdf(pdf_file):
    text = ""
    with fitz.open(stream=pdf_file.read(), filetype="pdf") as doc:
        for page in doc:
            text += page.get_text("text")

    luas_disetujui = None
    luas_dimohon = None

    match_disetujui = re.search(r"Luas[^0-9]*([0-9\.\,]+)[^\n]*(disetujui)", text, re.IGNORECASE)
    match_dimohon = re.search(r"Luas[^0-9]*([0-9\.\,]+)[^\n]*(dimohon)", text, re.IGNORECASE)

    def to_float(num_str):
        return float(num_str.replace(".", "").replace(",", "."))

    if match_disetujui:
        luas_disetujui = to_float(match_disetujui.group(1))
    if match_dimohon:
        luas_dimohon = to_float(match_dimohon.group(1))

    if luas_disetujui:
        return luas_disetujui, "disetujui"
    elif luas_dimohon:
        return luas_dimohon, "dimohon"
    else:
        return None, None

# =========================
# Upload Section
# =========================

uploaded_pkkpr = st.file_uploader("📥 Upload PKKPR (PDF/ZIP)", type=["pdf", "zip"])
uploaded_tapak = st.file_uploader("📥 Upload Tapak Proyek (SHP/ZIP)", type=["zip"])

gdf_polygon = None
gdf_tapak = None
luas_pkkpr_doc, luas_pkkpr_doc_label = None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.lower().endswith(".pdf"):
        luas_pkkpr_doc, luas_pkkpr_doc_label = extract_luas_from_pdf(uploaded_pkkpr)
    elif uploaded_pkkpr.name.lower().endswith(".zip"):
        tmpdir = tempfile.mkdtemp()
        with zipfile.ZipFile(uploaded_pkkpr, "r") as zf:
            zf.extractall(tmpdir)
        shp_files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.endswith(".shp")]
        if shp_files:
            gdf_polygon = gpd.read_file(shp_files[0])

if uploaded_tapak:
    tmpdir = tempfile.mkdtemp()
    with zipfile.ZipFile(uploaded_tapak, "r") as zf:
        zf.extractall(tmpdir)
    shp_files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.endswith(".shp")]
    if shp_files:
        gdf_tapak = gpd.read_file(shp_files[0])

# =========================
# Map Visualization
# =========================

if gdf_polygon is not None or gdf_tapak is not None:
    m = folium.Map(location=[-2, 118], zoom_start=5, control_scale=True)

    if gdf_polygon is not None:
        folium.GeoJson(
            gdf_polygon, name="PKKPR",
            style_function=lambda x: {"fillColor": "orange", "color": "red", "weight": 2, "fillOpacity": 0.2}
        ).add_to(m)

    if gdf_tapak is not None:
        folium.GeoJson(
            gdf_tapak, name="Tapak Proyek",
            style_function=lambda x: {"fillColor": "blue", "color": "blue", "weight": 2, "fillOpacity": 0.2}
        ).add_to(m)

    folium.LayerControl(collapsed=False, position="bottomleft").add_to(m)
    folium.LatLngPopup().add_to(m)

    st_folium(m, width=1000, height=600)

# =========================
# Ekspor & Analisis
# =========================

if gdf_polygon is not None:
    # SHP PKKPR selalu bisa diunduh
    zip_pkkpr = save_shapefile(gdf_polygon, "out_pkkpr", "PKKPR_Hasil")
    with open(zip_pkkpr, "rb") as f:
        st.download_button("⬇️ Download SHP PKKPR (ZIP)", f,
                           file_name="PKKPR_Hasil.zip", mime="application/zip")

if gdf_polygon is not None and gdf_tapak is not None:
    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg = get_utm_epsg(centroid.x, centroid.y)
    gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg)
    gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)

    luas_tapak = gdf_tapak_utm.area.sum()
    luas_pkkpr_hitung = gdf_polygon_utm.area.sum()
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak - luas_overlap

    luas_doc_str = f"{luas_pkkpr_doc:,.2f} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"

    st.info(f"""
    **📊 Analisis Luas Tapak Proyek (Proyeksi UTM {utm_epsg}):**
    - Total Luas Tapak Proyek: {luas_tapak:,.2f} m²
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (hitung dari geometri): {luas_pkkpr_hitung:,.2f} m²
    - Luas di dalam PKKPR: {luas_overlap:,.2f} m²
    - Luas di luar PKKPR: {luas_outside:,.2f} m²
    """)

    zip_tapak = save_shapefile(gdf_tapak_utm, "out_tapak", "Tapak_Hasil_UTM")
    with open(zip_tapak, "rb") as f:
        st.download_button("⬇️ Download SHP Tapak Proyek (UTM)", f,
                           file_name="Tapak_Hasil_UTM.zip", mime="application/zip")
