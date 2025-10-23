# app.py
import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, tempfile, re, math
from shapely.geometry import Point, Polygon, MultiPolygon, GeometryCollection
from shapely.validation import make_valid
from shapely import affinity
import folium
from streamlit_folium import st_folium
import pdfplumber
import matplotlib.pyplot as plt
import contextily as ctx
from folium.plugins import Fullscreen
import xyzservices.providers as xyz
from pyproj import Transformer

# ======================
# CONFIG
# ======================
st.set_page_config(page_title="PKKPR → SHP + Overlay (Final)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek (Final)")
st.markdown("---")
DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

# Constants
INDO_BOUNDS = (95.0, 141.0, -11.0, 6.0)

# ======================
# HELPERS
# ======================
def format_angka_id(value):
    try:
        val = float(value)
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")
        else:
            s = f"{val:,.2f}"
            s = s.replace(",", "X").replace(".", ",").replace("X", ".")
            return s
    except:
        return str(value)

def get_utm_info(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    zone_label = f"{zone}{'N' if lat >= 0 else 'S'}"
    return epsg, zone_label

def parse_luas_line(line):
    if not line:
        return None
    s = str(line)
    s = s.replace('\xa0', ' ').replace('\u00B2', '²').replace('m2', 'm²')
    unit_pattern = r"(m2|m²|m\s*2|ha|hektar)"
    m = re.search(r"([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?)[\s]*(" + unit_pattern + r")", s, flags=re.IGNORECASE)
    if m:
        num = m.group(1)
        unit = (m.group(2) or "").strip().upper()
        return f"{num} {'Ha' if 'HA' in unit else 'm²'}"
    return None

def save_shapefile_layers(gdf_poly, gdf_points):
    with tempfile.TemporaryDirectory() as tmpdir:
        if gdf_poly is not None and not gdf_poly.empty:
            gdf_poly.to_crs(epsg=4326).to_file(os.path.join(tmpdir, "PKKPR_Polygon.shp"))
        if gdf_points is not None and not gdf_points.empty:
            gdf_points.to_crs(epsg=4326).to_file(os.path.join(tmpdir, "PKKPR_Points.shp"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in os.listdir(tmpdir):
                zf.write(os.path.join(tmpdir, f), arcname=f)
        buf.seek(0)
        return buf.read()

def fix_geometry(gdf):
    if gdf is None or gdf.empty:
        return gdf
    gdf["geometry"] = gdf["geometry"].apply(make_valid)
    def extract_valid(geom):
        if geom is None:
            return None
        if geom.geom_type == "GeometryCollection":
            polys = [g for g in geom.geoms if g.geom_type in ["Polygon", "MultiPolygon"]]
            if not polys:
                return None
            return polys[0] if len(polys) == 1 else MultiPolygon(polys)
        return geom
    gdf["geometry"] = gdf["geometry"].apply(extract_valid)
    return gdf

def extract_coords_from_line_pair(line):
    s = line.strip()
    s = re.sub(r"([0-9])(-\d)", r"\1 \2", s)
    m = re.search(r"(-?\d+\.\d+)\s+(-?\d+\.\d+)", s)
    if not m:
        return None
    try:
        a, b = float(m.group(1)), float(m.group(2))
    except:
        return None
    if 95 <= a <= 141 and -11 <= b <= 6:
        return (a, b)
    if 95 <= b <= 141 and -11 <= a <= 6:
        return (b, a)
    return None

def extract_tables_and_coords_from_pdf(uploaded_file):
    coords, luas = [], None
    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                parsed = extract_coords_from_line_pair(line)
                if parsed:
                    coords.append(parsed)
                if "luas" in line.lower() and luas is None:
                    luas = parse_luas_line(line)
            table = page.extract_table()
            if table:
                for row in table:
                    vals = [re.findall(r"(-?\d+\.\d+)", str(c)) for c in row]
                    flat = [float(v) for sub in vals for v in sub]
                    if len(flat) >= 2:
                        if 95 <= flat[0] <= 141 and -11 <= flat[1] <= 6:
                            coords.append((flat[0], flat[1]))
                        elif 95 <= flat[1] <= 141 and -11 <= flat[0] <= 6:
                            coords.append((flat[1], flat[0]))
    return {"coords": coords, "luas": luas}

# =====================================================
# UI: Upload PKKPR (PDF atau SHP)
# =====================================================
st.subheader("📄 Upload Dokumen PKKPR (PDF atau SHP ZIP)")
col1, col2 = st.columns([3, 2])

with col1:
    uploaded = st.file_uploader("Unggah file PKKPR", type=["pdf", "zip"], label_visibility="collapsed")

gdf_polygon = None
gdf_points = None
luas_pkkpr_doc = None

with col2:
    if uploaded:
        if uploaded.name.lower().endswith(".pdf"):
            parsed = extract_tables_and_coords_from_pdf(uploaded)
            coords = parsed["coords"]
            luas_pkkpr_doc = parsed["luas"]
            if coords:
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                gdf_points = gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in coords], crs="EPSG:4326")
                gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(coords)], crs="EPSG:4326")
                gdf_polygon = fix_geometry(gdf_polygon)
                st.success(f"Berhasil mengekstrak **{len(coords)} titik** dari PDF ✅")
            else:
                st.warning("Tidak ada koordinat ditemukan dalam PDF.")
        elif uploaded.name.lower().endswith(".zip"):
            with tempfile.TemporaryDirectory() as tmp:
                zf = zipfile.ZipFile(io.BytesIO(uploaded.read()))
                zf.extractall(tmp)
                for root, _, files in os.walk(tmp):
                    for f in files:
                        if f.lower().endswith(".shp"):
                            gdf_polygon = gpd.read_file(os.path.join(root, f))
                            break
            gdf_polygon = fix_geometry(gdf_polygon)
            st.success("Shapefile PKKPR berhasil dimuat ✅")

# =====================================================
# Analisis Luas
# =====================================================
if gdf_polygon is not None:
    st.subheader("📏 Analisis Luas Geometri")
    if luas_pkkpr_doc:
        st.write(f"Luas Dokumen PKKPR: **{luas_pkkpr_doc}**")

    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    luas_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
    luas_merc = gdf_polygon.to_crs(epsg=3857).area.sum()

    st.write(f"Luas PKKPR (UTM {utm_zone}): {format_angka_id(luas_utm)} m²")
    st.write(f"Luas PKKPR (Mercator): {format_angka_id(luas_merc)} m²")

    zip_bytes = save_shapefile_layers(gdf_polygon, gdf_points)
    st.download_button("⬇️ Download SHP PKKPR", zip_bytes, "PKKPR_Hasil.zip", mime="application/zip")

# =====================================================
# Upload Tapak (Overlay)
# =====================================================
st.subheader("🏗️ Upload Shapefile Tapak Proyek (ZIP)")
uploaded_tapak = st.file_uploader("Unggah Tapak Proyek", type=["zip"], key="tapak")
if uploaded_tapak and gdf_polygon is not None:
    with tempfile.TemporaryDirectory() as tmp:
        zf = zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()))
        zf.extractall(tmp)
        gdf_tapak = None
        for root, _, files in os.walk(tmp):
            for f in files:
                if f.lower().endswith(".shp"):
                    gdf_tapak = gpd.read_file(os.path.join(root, f))
                    break
                    
    st.subheader("Analisis Luas Overlay (UTM {utm_zone})")
    gdf_tapak = fix_geometry(gdf_tapak)
    utm_epsg, utm_zone = get_utm_info(*gdf_polygon.to_crs(4326).geometry.centroid.iloc[0].coords[0])
    gdf_tapak_utm = gdf_tapak.to_crs(utm_epsg)
    gdf_polygon_utm = gdf_polygon.to_crs(utm_epsg)
    inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how="intersection")
    luas_tapak = gdf_tapak_utm.area.sum()
    luas_overlap = inter.area.sum()
    st.success(f"Luas Tapak: {format_angka_id(luas_tapak)} m²\n\n"
               f"Luas Tapak di dalam PKKPR: {format_angka_id(luas_overlap)} m²\n\n"
               f"Luas Tapak di luar PKKPR: {format_angka_id(luas_tapak - luas_overlap)} m²")

# =====================================================
# Layout PNG — hanya tombol download
# =====================================================
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG)")
    try:
        gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
        xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
        fig, ax = plt.subplots(figsize=(10, 10), dpi=150)
        gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2.5)
        if 'gdf_tapak' in locals():
            gdf_tapak.to_crs(epsg=3857).plot(ax=ax, facecolor="red", alpha=0.4)
        if gdf_points is not None:
            gdf_points.to_crs(epsg=3857).plot(ax=ax, color="orange", markersize=20)
        ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)
        ax.set_xlim(xmin - (xmax - xmin) * 0.05, xmax + (xmax - xmin) * 0.05)
        ax.set_ylim(ymin - (ymax - ymin) * 0.05, ymax + (ymax - ymin) * 0.05)
        ax.set_title("Peta Tapak Proyek & Batas PKKPR", fontsize=14)
        ax.axis("off")
        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=200)
        buf.seek(0)
        plt.close(fig)
        st.download_button("⬇️ Download Layout PNG", data=buf, file_name="Layout_PKKPR.png", mime="image/png")
    except Exception as e:
        st.error(f"Gagal membuat layout PNG: {e}")
        if DEBUG:
            st.exception(e)


