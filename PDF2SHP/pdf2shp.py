import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, re, tempfile
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
from shapely import affinity
from shapely.validation import make_valid
from pyproj import Transformer

# =====================================
# Konfigurasi Aplikasi Streamlit
# =====================================
st.set_page_config(page_title="PKKPR → SHP & Overlay (Final Auto-Detect ID)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")
st.markdown("---")

DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

if st.sidebar.button("🔄 Refresh Aplikasi"):
    try:
        st.cache_data.clear()
    except Exception:
        pass
    st.experimental_rerun()

# =====================================
# Fungsi Umum
# =====================================
def normalize_text(s):
    if not s:
        return s
    s = str(s)
    s = s.replace('\u2019', "'").replace('\u201d', '"').replace('\u201c', '"')
    s = s.replace('’', "'").replace('“', '"').replace('”', '"')
    s = s.replace('\xa0', ' ')
    return s

def get_utm_info(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return epsg, f"{zone}{'N' if lat >= 0 else 'S'}"

def save_shapefile(gdf):
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "PKKPR_Output.shp")
        gdf.to_crs(epsg=4326).to_file(out_path)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in os.listdir(tmp):
                zf.write(os.path.join(tmp, f), arcname=f)
        buf.seek(0)
        return buf.read()

def format_angka_id(value):
    try:
        val = float(value)
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")
        else:
            return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(value)

# =====================================
# Parsing Koordinat
# =====================================
def dms_bt_ls_to_decimal(dms_str):
    if not isinstance(dms_str, str):
        return None
    dms_str = dms_str.replace(",", ".").strip()
    pattern = r"(\d+)[°:\s]+(\d+)[\'′:\s]+([\d.]+)\"?\s*([A-Z]*)"
    match = re.search(pattern, dms_str)
    if not match:
        return None
    deg = float(match.group(1))
    minute = float(match.group(2))
    second = float(match.group(3))
    direction = match.group(4).upper()
    decimal = deg + (minute / 60) + (second / 3600)
    if direction in ["LS", "S", "BB", "W"]:
        decimal *= -1
    return decimal

def extract_coords_bt_ls_from_text(text):
    coords = []
    text = normalize_text(text)
    pattern = r"(\d{1,3}°\s*\d{1,2}'\s*[\d,\.]+\"\s*B[BT])[^0-9]+(\d{1,2}°\s*\d{1,2}'\s*[\d,\.]+\"\s*[LS])"
    for m in re.finditer(pattern, text, flags=re.IGNORECASE):
        lon_raw, lat_raw = m.groups()
        lon = dms_bt_ls_to_decimal(lon_raw)
        lat = dms_bt_ls_to_decimal(lat_raw)
        if lon is not None and lat is not None:
            coords.append((lon, lat))
    return coords

def extract_coords_from_text(text):
    out = []
    text = normalize_text(text)
    pattern = r"(-?\d{1,3}\.\d+)[^\d\-\.,]+(-?\d{1,3}\.\d+)"
    for m in re.finditer(pattern, text):
        a, b = float(m.group(1)), float(m.group(2))
        if 90 <= abs(a) <= 145 and -11 <= b <= 6:
            out.append((a, b))
        elif 90 <= abs(b) <= 145 and -11 <= a <= 6:
            out.append((b, a))
    return out

def extract_coords_comma_decimal(text):
    coords = []
    text = normalize_text(text)
    pattern = r"(\d{1,3},\d+)\s+(-?\d{1,2},\d+)"
    for m in re.finditer(pattern, text):
        lon_str, lat_str = m.groups()
        try:
            lon = float(lon_str.replace(",", "."))
            lat = float(lat_str.replace(",", "."))
            if 90 <= lon <= 145 and -11 <= lat <= 6:
                coords.append((lon, lat))
        except:
            continue
    return coords

def extract_coords_projected(text):
    out = []
    text = normalize_text(text)
    pattern = r"(-?\d{5,13}(?:\.\d+)?)[^\d\-\.]{1,6}(-?\d{5,13}(?:\.\d+)?)"
    for m in re.finditer(pattern, text):
        a_str, b_str = m.groups()
        try:
            a = float(a_str)
            b = float(b_str)
            out.append((a, b))
        except:
            continue
    return out

# =====================================
# Fix Geometry
# =====================================
def fix_polygon_geometry(gdf):
    if gdf is None or len(gdf) == 0:
        return gdf
    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].apply(lambda g: make_valid(g))
    return gdf

def auto_fix_to_polygon(coords):
    if not coords or len(coords) < 3:
        return None
    unique_coords = []
    for c in coords:
        if not unique_coords or c != unique_coords[-1]:
            unique_coords.append(c)
    if unique_coords[0] != unique_coords[-1]:
        unique_coords.append(unique_coords[0])
    try:
        poly = Polygon(unique_coords)
        if not poly.is_valid or poly.area == 0:
            pts = gpd.GeoSeries([Point(x, y) for x, y in unique_coords], crs="EPSG:4326")
            poly = pts.unary_union.convex_hull
        return poly
    except Exception:
        return None

# =====================================
# UTM Auto-Detect (zona 46S–50S)
# =====================================
def try_zones_and_find_valid(easting, northing):
    """Coba zona 46–50S dan urutan XY/YX untuk hasil di Indonesia."""
    for zone in range(46, 51):
        epsg = 32700 + zone  # hanya selatan
        transformer_xy = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        transformer_yx = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        for order in ["xy", "yx"]:
            try:
                if order == "xy":
                    lon, lat = transformer_xy.transform(easting, northing)
                else:
                    lon, lat = transformer_yx.transform(northing, easting)
                if 95.0 <= lon <= 141.0 and -11.0 <= lat <= 6.0:
                    return epsg, order, lon, lat
            except Exception:
                continue
    return None, None, None, None

def detect_and_transform_projected_pairs(pairs):
    if not pairs:
        return [], None
    med = pairs[len(pairs)//2]
    epsg_found, order_found, lon_test, lat_test = try_zones_and_find_valid(med[0], med[1])
    if epsg_found is None:
        return [], None
    transformer = Transformer.from_crs(f"EPSG:{epsg_found}", "EPSG:4326", always_xy=True)
    transformed = []
    for a, b in pairs:
        if order_found == "xy":
            lon, lat = transformer.transform(a, b)
        else:
            lon, lat = transformer.transform(b, a)
        transformed.append((lon, lat))
    return transformed, epsg_found

# =====================================
# PDF Parsing (cached)
# =====================================
@st.cache_data
def parse_pdf_texts(file_bytes):
    texts = []
    try:
        with pdfplumber.open(file_bytes) as pdf:
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
    except Exception:
        pass
    return texts

# =====================================
# Upload PKKPR
# =====================================
col1, col2 = st.columns([0.7, 0.3])
uploaded_pkkpr = col1.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])
coords, gdf_polygon, gdf_points = [], None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.endswith(".pdf"):
        texts = parse_pdf_texts(uploaded_pkkpr)
        coords_all = []
        coords_proj = []
        for t in texts:
            coords_all += extract_coords_bt_ls_from_text(t)
            coords_all += extract_coords_from_text(t)
            coords_all += extract_coords_comma_decimal(t)
            coords_proj += extract_coords_projected(t)

        if len(coords_proj) >= max(3, len(coords_all)):
            transformed, epsg_found = detect_and_transform_projected_pairs(coords_proj)
            if transformed:
                coords = transformed
                gdf_points = gpd.GeoDataFrame(pd.DataFrame(coords, columns=["Lon", "Lat"]),
                                              geometry=[Point(x, y) for x, y in coords], crs="EPSG:4326")
                poly = auto_fix_to_polygon(coords)
                if poly:
                    gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                    col2.success(f"{len(coords)} titik terdeteksi (EPSG {epsg_found})")
                else:
                    st.error("Gagal membuat polygon dari titik UTM.")
        else:
            coords = coords_all
            if coords:
                gdf_points = gpd.GeoDataFrame(pd.DataFrame(coords, columns=["Lon", "Lat"]),
                                              geometry=[Point(x, y) for x, y in coords], crs="EPSG:4326")
                poly = auto_fix_to_polygon(coords)
                if poly:
                    gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                    col2.success(f"{len(coords)} titik terdeteksi (geografis)")
            else:
                st.error("Tidak ditemukan koordinat dalam PDF.")
    elif uploaded_pkkpr.name.endswith(".zip"):
        with tempfile.TemporaryDirectory() as tmp:
            zf = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()))
            zf.extractall(tmp)
            gdf_polygon = gpd.read_file(tmp)
            if gdf_polygon.crs is None:
                gdf_polygon.set_crs(epsg=4326, inplace=True)
            gdf_polygon = fix_polygon_geometry(gdf_polygon)
            col2.success("Shapefile PKKPR dibaca.")

# =====================================
# Analisis Luas & Download SHP
# =====================================
if gdf_polygon is not None:
    zip_bytes = save_shapefile(gdf_polygon)
    st.download_button("⬇️ Download SHP PKKPR (ZIP)", zip_bytes, "PKKPR_Hasil_Konversi.zip", mime="application/zip")

    centroid = gdf_polygon.geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    luas_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
    luas_merc = gdf_polygon.to_crs(epsg=3857).area.sum()
    st.info(f"**Analisis Luas Batas PKKPR**:\n- Luas (UTM {utm_zone}): **{format_angka_id(luas_utm)} m²**\n- Luas (WGS84 Mercator): **{format_angka_id(luas_merc)} m²**")
    st.markdown("---")

# =====================================
# Upload Tapak Proyek
# =====================================
col1, col2 = st.columns([0.7, 0.3])
uploaded_tapak = col1.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"], key="tapak")
gdf_tapak = None
if uploaded_tapak:
    with tempfile.TemporaryDirectory() as tmp:
        zf = zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()))
        zf.extractall(tmp)
        gdf_tapak = gpd.read_file(tmp)
        if gdf_tapak.crs is None:
            gdf_tapak.set_crs(epsg=4326, inplace=True)
        col2.success("Shapefile tapak proyek dibaca.")

# =====================================
# Overlay
# =====================================
if gdf_polygon is not None and gdf_tapak is not None:
    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg)
    gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)
    inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how="intersection")
    luas_overlap = inter.area.sum() if not inter.empty else 0
    luas_tapak = gdf_tapak_utm.area.sum()
    luas_outside = luas_tapak - luas_overlap
    st.success(f"**HASIL OVERLAY TAPAK:**\n- Luas Tapak: **{format_angka_id(luas_tapak)} m²**\n- Di dalam PKKPR: **{format_angka_id(luas_overlap)} m²**\n- Di luar PKKPR: **{format_angka_id(luas_outside)} m²**")
    st.markdown("---")

# =====================================
# Peta Interaktif
# =====================================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    centroid = gdf_polygon.geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None)
    Fullscreen(position="bottomleft").add_to(m)
    folium.TileLayer("openstreetmap").add_to(m)
    folium.TileLayer("CartoDB Positron").add_to(m)
    folium.TileLayer(xyz.Esri.WorldImagery).add_to(m)
    folium.GeoJson(gdf_polygon, name="PKKPR", style_function=lambda x: {"color": "yellow", "weight": 3, "fillOpacity": 0.1}).add_to(m)
    if gdf_tapak is not None:
        folium.GeoJson(gdf_tapak, name="Tapak Proyek", style_function=lambda x: {"color": "red", "fillColor": "red", "fillOpacity": 0.4}).add_to(m)
    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker([row.geometry.y, row.geometry.x], radius=4, color="black", fill=True, fill_color="orange", fill_opacity=1, popup=f"Titik {i+1}").add_to(m)
    folium.LayerControl().add_to(m)
    st_folium(m, width=900, height=600)
    st.markdown("---")

# ======================
# === Layout Peta PNG ===
# ======================
if 'gdf_polygon' in locals() and gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG) untuk Dokumentasi")
    try:
        gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
        xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
        width, height = xmax - xmin, ymax - ymin

        fig, ax = plt.subplots(figsize=(14, 10) if width > height else (10, 14), dpi=150)

        gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2.5, label="Batas PKKPR")

        if gdf_tapak is not None:
            gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
            gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red", label="Tapak Proyek")

        if 'gdf_points' in locals() and gdf_points is not None:
            gdf_points_3857 = gdf_points.to_crs(epsg=3857)
            gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=30, label="Titik PKKPR")

        try:
            ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)
        except Exception:
            if DEBUG:
                st.write("Gagal memuat basemap Esri via contextily.")

        ax.set_xlim(xmin - width*0.05, xmax + width*0.05)
        ax.set_ylim(ymin - height*0.05, ymax + height*0.05)

        legend = [
            mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black", linestyle="None", markersize=5, label="PKKPR (Titik)"),
            mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=1.5, label="PKKPR (Polygon)"),
            mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
        ]
        ax.legend(handles=legend, title="Legenda", loc="upper right", fontsize=8, title_fontsize=9)
        ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14, weight="bold")
        ax.set_axis_off()

        png_buffer = io.BytesIO()
        plt.savefig(png_buffer, format="png", dpi=300, bbox_inches="tight")
        plt.close(fig)
        png_buffer.seek(0)

        st.download_button(
            "⬇️ Download Layout Peta (PNG)",
            png_buffer,
            "layout_peta.png",
            mime="image/png"
        )

    except Exception as e:
        st.error(f"Gagal membuat layout peta: {e}")
        if DEBUG:
            st.exception(e)

