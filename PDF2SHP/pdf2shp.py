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

# ======================
# === Konfigurasi App ===
# ======================
st.set_page_config(page_title="PKKPR → SHP & Overlay (Final)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")
st.markdown("---")

DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

# Tombol refresh manual
if st.sidebar.button("🔄 Refresh Aplikasi"):
    try:
        st.cache_data.clear()
    except Exception:
        pass
    try:
        st.cache_resource.clear()
    except Exception:
        pass
    st.experimental_rerun()

# ======================
# === Fungsi Umum ===
# ======================
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
        # force to single geometry type earlier; gdf should already be polygon-only
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

# ======================
# === Parsing Koordinat ===
# ======================
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
        # Try interpret as (lon, lat) or swapped (lat, lon)
        if 90 <= abs(a) <= 145 and -11 <= b <= 6:
            out.append((a, b))
        elif 90 <= abs(b) <= 145 and -11 <= a <= 6:
            out.append((b, a))
    return out

def extract_coords_comma_decimal(text):
    coords = []
    text = normalize_text(text)
    # handle patterns like: 108,064739 -6,862542  or 108,064739\t-6,862542
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

# ======================
# === Fix Geometry ===
# ======================
def fix_polygon_geometry(gdf):
    if gdf is None or len(gdf) == 0:
        return gdf
    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].apply(lambda g: make_valid(g))
    b = gdf.total_bounds
    # If bounds obviously not lon/lat, attempt scaling heuristics
    if not (-180 <= b[0] <= 180 and -90 <= b[1] <= 90):
        for fac in [10, 100, 1000, 10000, 100000]:
            g2 = gdf.copy()
            g2["geometry"] = g2["geometry"].apply(lambda g: affinity.scale(g, xfact=1/fac, yfact=1/fac, origin=(0,0)))
            b2 = g2.total_bounds
            if (90 <= abs(b2[0]) <= 145 and -11 <= b2[1] <= 6):
                return g2.set_crs(epsg=4326, allow_override=True)
    return gdf

def ensure_polygon_only(gdf):
    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].apply(lambda g: g if g.geom_type in ["Polygon", "MultiPolygon"] else None)
    gdf = gdf[gdf["geometry"].notnull()]
    if gdf.empty:
        raise ValueError("Tidak ada geometri Polygon yang valid untuk disimpan.")
    return gdf

def auto_fix_to_polygon(coords):
    """
    Hapus duplikat berurutan, tutup polygon, coba buat Polygon.
    Jika gagal, gunakan convex hull dari titik.
    """
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
        if (not poly.is_valid) or (poly.area == 0):
            pts = gpd.GeoSeries([Point(x, y) for x, y in unique_coords], crs="EPSG:4326")
            poly = pts.unary_union.convex_hull
        return poly
    except Exception:
        return None

# ======================
# === Upload File ===
# ======================
col1, col2 = st.columns([0.7, 0.3])
uploaded_pkkpr = col1.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

coords, gdf_points, gdf_polygon = [], None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.lower().endswith(".pdf"):
        try:
            text_full = ""
            with pdfplumber.open(uploaded_pkkpr) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    text_full += "\n" + text
                    coords += extract_coords_bt_ls_from_text(text)
                    coords += extract_coords_from_text(text)
                    coords += extract_coords_comma_decimal(text)

            if coords:
                poly = auto_fix_to_polygon(coords)
                if poly is not None:
                    gdf_points = gpd.GeoDataFrame(pd.DataFrame(coords, columns=["Lon", "Lat"]),
                                                  geometry=[Point(x, y) for x, y in coords], crs="EPSG:4326")
                    gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                    gdf_polygon = fix_polygon_geometry(gdf_polygon)
                    col2.markdown(f"<p style='color:green;font-weight:bold;padding-top:3.5rem;'>✅ {len(coords)} titik terdeteksi & polygon valid</p>", unsafe_allow_html=True)
                else:
                    st.error("Koordinat tidak membentuk polygon yang valid.")
            else:
                st.error("Tidak ditemukan koordinat pada PDF.")
        except Exception as e:
            st.error(f"Gagal memproses PDF: {e}")
            if DEBUG:
                st.exception(e)

    elif uploaded_pkkpr.name.lower().endswith(".zip"):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()), 'r')
                zip_ref.extractall(tmp)
                gdf_polygon = gpd.read_file(tmp)
                if gdf_polygon.crs is None:
                    gdf_polygon.set_crs(epsg=4326, inplace=True)
                gdf_polygon = fix_polygon_geometry(gdf_polygon)
                col2.markdown("<p style='color:green;font-weight:bold;padding-top:3.5rem;'>✅ Shapefile (PKKPR)</p>", unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Gagal membaca shapefile PKKPR: {e}")
            if DEBUG:
                st.exception(e)

# ======================
# === Hasil & Luas ===
# ======================
if gdf_polygon is not None:
    try:
        # Pastikan polygon-only sebelum simpan
        gdf_polygon = ensure_polygon_only(gdf_polygon)
        zip_bytes = save_shapefile(gdf_polygon)
        st.download_button("⬇️ Download SHP PKKPR (ZIP)", zip_bytes, "PKKPR_Hasil_Konversi.zip", mime="application/zip")
    except Exception as e:
        st.error(f"Gagal menyiapkan shapefile: {e}")
        if DEBUG:
            st.exception(e)

    try:
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
        luas_pkkpr_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
        luas_pkkpr_mercator = gdf_polygon.to_crs(epsg=3857).area.sum()
        st.info(
            f"**Analisis Luas Batas PKKPR**:\n"
            f"- Luas (UTM {utm_zone}): **{format_angka_id(luas_pkkpr_utm)} m²**\n"
            f"- Luas (WGS84 Mercator): **{format_angka_id(luas_pkkpr_mercator)} m²**"
        )
    except Exception as e:
        st.error(f"Gagal menghitung luas: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# ======================
# === Upload Tapak ===
# ======================
col1, col2 = st.columns([0.7, 0.3])
uploaded_tapak = col1.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"], key='tapak')
gdf_tapak = None

if uploaded_tapak:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()), 'r')
            zip_ref.extractall(tmp)
            gdf_tapak = gpd.read_file(tmp)
            if gdf_tapak.crs is None:
                gdf_tapak.set_crs(epsg=4326, inplace=True)
            col2.markdown("<p style='color:green;font-weight:bold;padding-top:3.5rem;'>✅</p>", unsafe_allow_html=True)
    except Exception as e:
        st.error(f"Gagal membaca shapefile Tapak Proyek: {e}")
        if DEBUG:
            st.exception(e)

# ======================
# === Overlay ===
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    try:
        centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
        gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg)
        gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)
        inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how='intersection')
        luas_overlap = inter.area.sum() if not inter.empty else 0
        luas_tapak = gdf_tapak_utm.area.sum()
        luas_outside = luas_tapak - luas_overlap
        st.success(
            f"**HASIL OVERLAY TAPAK:**\n"
            f"- Luas Tapak UTM {utm_zone}: **{format_angka_id(luas_tapak)} m²**\n"
            f"- Luas Tapak di dalam PKKPR: **{format_angka_id(luas_overlap)} m²**\n"
            f"- Luas Tapak Di luar PKKPR : **{format_angka_id(luas_outside)} m²**"
        )
    except Exception as e:
        st.error(f"Gagal overlay: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# ======================
# === Peta Interaktif ===
# ======================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    try:
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None)
        Fullscreen(position="bottomleft").add_to(m)
        folium.TileLayer("openstreetmap", name="OpenStreetMap").add_to(m)
        folium.TileLayer("CartoDB Positron", name="CartoDB Positron").add_to(m)
        folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery").add_to(m)
        folium.GeoJson(gdf_polygon.to_crs(epsg=4326), name="PKKPR", style_function=lambda x: {"color": "yellow", "weight": 3, "fillOpacity": 0.1}).add_to(m)
        if gdf_tapak is not None:
            folium.GeoJson(gdf_tapak.to_crs(epsg=4326), name="Tapak Proyek", style_function=lambda x: {"color": "red", "fillColor": "red", "fillOpacity": 0.4}).add_to(m)
        if gdf_points is not None:
            for i, row in gdf_points.iterrows():
                folium.CircleMarker([row.geometry.y, row.geometry.x], radius=4, color="black", fill=True, fill_color="orange", fill_opacity=1, popup=f"Titik {i+1}").add_to(m)
        folium.LayerControl(collapsed=True).add_to(m)
        st_folium(m, width=900, height=600)
    except Exception as e:
        st.error(f"Gagal membuat peta interaktif: {e}")
        if DEBUG:
            st.exception(e)
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
