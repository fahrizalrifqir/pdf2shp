# app.py — PKKPR → SHP & Overlay (Final, Prioritize 32748, robust UTM detect)
import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, re, tempfile, math
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

# -------------------------
# CONFIG
# -------------------------
st.set_page_config(page_title="PKKPR → SHP & Overlay (Final PB)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")
st.markdown("---")
DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

# quick constants
PURWAKARTA_CENTER = (107.44, -6.56)  # lon, lat approximate
INDO_BOUNDS = (95.0, 141.0, -11.0, 6.0)  # lon_min, lon_max, lat_min, lat_max

# refresh button
if st.sidebar.button("🔄 Refresh Aplikasi"):
    try:
        st.cache_data.clear()
    except Exception:
        pass
    st.experimental_rerun()

# -------------------------
# Utility functions
# -------------------------
def normalize_text(s):
    if not s:
        return s
    s = str(s)
    s = s.replace('\u2019', "'").replace('\u201d', '"').replace('\u201c', '"')
    s = s.replace('’', "'").replace('“', '"').replace('”', '"')
    s = s.replace('\xa0', ' ')
    return s

def format_angka_id(value):
    try:
        val = float(value)
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")
        else:
            return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(value)

def get_utm_info(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return epsg, f"{zone}{'N' if lat >= 0 else 'S'}"

def save_shapefile(gdf):
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "PKKPR_Output.shp")
        # ensure polygons are valid and in WGS84 for saving
        gdf.to_crs(epsg=4326).to_file(out_path)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in os.listdir(tmp):
                zf.write(os.path.join(tmp, f), arcname=f)
        buf.seek(0)
        return buf.read()

# -------------------------
# Coordinate parsing
# -------------------------
def dms_bt_ls_to_decimal(dms_str):
    if not isinstance(dms_str, str):
        return None
    dms_str = dms_str.replace(",", ".").strip()
    pattern = r"(\d+)[°:\s]+(\d+)[\'′:\s]+([\d.]+)\"?\s*([A-Za-z]*)"
    m = re.search(pattern, dms_str)
    if not m:
        return None
    d, mm, ss, dirc = m.groups()
    dec = float(d) + float(mm)/60.0 + float(ss)/3600.0
    if dirc and dirc.upper() in ["S", "LS", "W", "BB"]:
        dec = -abs(dec)
    return dec

def extract_coords_bt_ls_from_text(text):
    out = []
    text = normalize_text(text)
    pattern = r"(\d{1,3}°\s*\d{1,2}'\s*[\d,\.]+\"\s*B[BT])[^0-9]+(\d{1,2}°\s*\d{1,2}'\s*[\d,\.]+\"\s*[LS])"
    for m in re.finditer(pattern, text, flags=re.IGNORECASE):
        lon_raw, lat_raw = m.groups()
        lon = dms_bt_ls_to_decimal(lon_raw)
        lat = dms_bt_ls_to_decimal(lat_raw)
        if (lon is not None) and (lat is not None):
            out.append((lon, lat))
    return out

def extract_coords_from_text(text):
    out = []
    text = normalize_text(text)
    pattern = r"(-?\d{1,3}\.\d+)[^\d\-\.,]+(-?\d{1,3}\.\d+)"
    for m in re.finditer(pattern, text):
        a,b = float(m.group(1)), float(m.group(2))
        # interpret as lon,lat or swapped
        if 90 <= abs(a) <= 145 and -11 <= b <= 6:
            out.append((a,b))
        elif 90 <= abs(b) <= 145 and -11 <= a <= 6:
            out.append((b,a))
    return out

def extract_coords_comma_decimal(text):
    out=[]
    text = normalize_text(text)
    pattern = r"(\d{1,3},\d+)\s+(-?\d{1,2},\d+)"
    for m in re.finditer(pattern, text):
        lon_s, lat_s = m.groups()
        try:
            lon = float(lon_s.replace(",", "."))
            lat = float(lat_s.replace(",", "."))
            if 90 <= abs(lon) <= 145 and -11 <= lat <= 6:
                out.append((lon, lat))
        except:
            pass
    return out

def extract_coords_projected(text):
    out=[]
    text = normalize_text(text)
    # match two large numbers separated by whitespace/tab/space
    pattern = r"(-?\d{5,13}(?:\.\d+)?)[^\d\-\.]{1,6}(-?\d{5,13}(?:\.\d+)?)"
    for m in re.finditer(pattern, text):
        a_s, b_s = m.groups()
        try:
            a = float(a_s)
            b = float(b_s)
            out.append((a,b))
        except:
            pass
    return out

# -------------------------
# Geometry helpers
# -------------------------
def auto_fix_to_polygon(coords):
    if not coords or len(coords) < 3:
        return None
    unique=[]
    for c in coords:
        if not unique or c != unique[-1]:
            unique.append(c)
    if unique[0] != unique[-1]:
        unique.append(unique[0])
    try:
        poly = Polygon(unique)
        if not poly.is_valid or poly.area == 0:
            pts = gpd.GeoSeries([Point(x,y) for x,y in unique], crs="EPSG:4326")
            poly = pts.unary_union.convex_hull
        return poly
    except Exception:
        return None

# -------------------------
# UTM detection (zones 46-50S) with XY/YX test and Purwakarta prioritization
# -------------------------
def in_indonesia(lon, lat):
    lon_min, lon_max, lat_min, lat_max = INDO_BOUNDS
    return (lon_min <= lon <= lon_max) and (lat_min <= lat <= lat_max)

def dist_to_purwakarta(lon, lat):
    # simple Euclidean on degrees (good enough for ranking)
    return math.hypot(lon - PURWAKARTA_CENTER[0], lat - PURWAKARTA_CENTER[1])

def try_zones_orders(easting, northing, zones=[46,47,48,49,50], prioritize_epsg=32748):
    """
    Try combinations: for zone in zones (south only -> 327xx),
    test order XY (easting, northing) and YX (northing, easting).
    Return candidate list of dicts with epsg, order, lon, lat, dist.
    """
    candidates=[]
    for zone in zones:
        epsg = 32700 + zone
        try:
            transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        except Exception:
            continue
        # try XY
        try:
            lon_xy, lat_xy = transformer.transform(easting, northing)
            if in_indonesia(lon_xy, lat_xy):
                candidates.append({"epsg":epsg,"order":"xy","lon":lon_xy,"lat":lat_xy,"dist":dist_to_purwakarta(lon_xy,lat_xy)})
        except Exception:
            pass
        # try YX
        try:
            lon_yx, lat_yx = transformer.transform(northing, easting)
            if in_indonesia(lon_yx, lat_yx):
                candidates.append({"epsg":epsg,"order":"yx","lon":lon_yx,"lat":lat_yx,"dist":dist_to_purwakarta(lon_yx,lat_yx)})
        except Exception:
            pass
    # sort: prioritize epsg==prioritize_epsg then smaller dist
    candidates_sorted = sorted(candidates, key=lambda c: (0 if c["epsg"]==prioritize_epsg else 1, c["dist"]))
    return candidates_sorted

def detect_projected_pairs_with_priority(pairs, zones=[46,47,48,49,50], prioritize_epsg=32748):
    """
    Given list of (a,b) numeric pairs from PDF, determine ordering+epsg.
    Strategy: test median pair, collect candidates, choose top candidate.
    Return transformed list lon/lat and chosen epsg+order.
    """
    if not pairs:
        return None, None, None
    mid = pairs[len(pairs)//2]
    a_med, b_med = mid
    # Try interpreting (a_med,b_med) as (easting,northing) and (northing,easting)
    cand1 = try_zones_orders(a_med, b_med, zones=zones, prioritize_epsg=prioritize_epsg)
    cand2 = try_zones_orders(b_med, a_med, zones=zones, prioritize_epsg=prioritize_epsg)
    # merge candidates but avoid duplicates
    all_cand = cand1 + [c for c in cand2 if c not in cand1]
    if not all_cand:
        return None, None, None
    chosen = all_cand[0]
    epsg_chosen = chosen["epsg"]
    order = chosen["order"]
    transformer = Transformer.from_crs(f"EPSG:{epsg_chosen}", "EPSG:4326", always_xy=True)
    transformed=[]
    for a,b in pairs:
        if order=="xy":
            lon, lat = transformer.transform(a,b)
        else:
            lon, lat = transformer.transform(b,a)
        transformed.append((lon, lat))
    return transformed, epsg_chosen, order

# -------------------------
# PDF reading (cached)
# -------------------------
@st.cache_data
def parse_pdf_texts(file_like):
    pages=[]
    try:
        with pdfplumber.open(file_like) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
    except Exception:
        # fallback: attempt to read bytes as str
        try:
            raw = file_like.read().decode("utf-8", errors="ignore")
            pages = [raw]
        except Exception:
            pages=[]
    return pages

# -------------------------
# UI: file upload
# -------------------------
col1, col2 = st.columns([0.7,0.3])
uploaded = col1.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf","zip"])
gdf_polygon=None
gdf_points=None
detected_info = {}

# sidebar EPSG override (optional)
st.sidebar.markdown("## Proyeksi (opsional)")
epsg_override_text = st.sidebar.text_input("Override EPSG (contoh: 32748) — kosong = auto-detect", value="")

if uploaded:
    if uploaded.name.lower().endswith(".pdf"):
        texts = parse_pdf_texts(uploaded)
        all_geo=[]
        all_geo += sum([extract_coords_bt_ls_from_text(t) for t in texts], [])
        all_geo += sum([extract_coords_from_text(t) for t in texts], [])
        all_geo += sum([extract_coords_comma_decimal(t) for t in texts], [])
        proj_pairs = sum([extract_coords_projected(t) for t in texts], [])
        # Heuristic: if many projected pairs and few geographic -> projected mode
        if len(proj_pairs) >= max(3, len(all_geo)):
            # try override EPSG first
            epsg_override = int(epsg_override_text) if epsg_override_text.strip().isdigit() else None
            transformed=None
            chosen_epsg=None
            chosen_order=None
            if epsg_override:
                # try use override: attempt both orderings
                candidates = try_zones_orders( # small hack: use only override zone
                    proj_pairs[len(proj_pairs)//2][0],
                    proj_pairs[len(proj_pairs)//2][1],
                    zones=[int(str(epsg_override)[-2:])],
                    prioritize_epsg=epsg_override
                )
                if candidates:
                    # choose first and transform all
                    chosen = candidates[0]
                    chosen_epsg = chosen["epsg"]
                    chosen_order = chosen["order"]
                    transformer = Transformer.from_crs(f"EPSG:{chosen_epsg}","EPSG:4326", always_xy=True)
                    transformed=[]
                    for a,b in proj_pairs:
                        if chosen_order=="xy":
                            lon,lat = transformer.transform(a,b)
                        else:
                            lon,lat = transformer.transform(b,a)
                        transformed.append((lon,lat))
            if transformed is None or not transformed:
                # auto-detect with priority to 32748
                transformed, chosen_epsg, chosen_order = detect_projected_pairs_with_priority(proj_pairs, zones=[46,47,48,49,50], prioritize_epsg=32748)
            if transformed:
                # Assign
                gdf_points = gpd.GeoDataFrame(pd.DataFrame(transformed, columns=["Lon","Lat"]),
                                              geometry=[Point(x,y) for x,y in transformed], crs="EPSG:4326")
                poly = auto_fix_to_polygon(transformed)
                if poly:
                    gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                    gdf_polygon = fix_polygon_geometry(gdf_polygon)
                    detected_info = {"mode":"projected","epsg":chosen_epsg,"order":chosen_order,"n_points":len(transformed)}
                    col2.success(f"Terbaca {len(transformed)} titik (proyeksi). EPSG dipilih: {chosen_epsg} (order {chosen_order})")
                else:
                    st.error("Terbaca koordinat metrik tapi gagal membentuk polygon valid.")
            else:
                st.error("Gagal auto-detect proyeksi untuk koordinat metrik. Coba isi override EPSG di sidebar (mis. 32748).")
        else:
            # geographic mode
            coords = all_geo
            if coords:
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                gdf_points = gpd.GeoDataFrame(pd.DataFrame(coords, columns=["Lon","Lat"]),
                                              geometry=[Point(x,y) for x,y in coords], crs="EPSG:4326")
                poly = auto_fix_to_polygon(coords)
                if poly:
                    gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                    gdf_polygon = fix_polygon_geometry(gdf_polygon)
                    detected_info = {"mode":"geographic","n_points":len(coords)}
                    col2.success(f"Terbaca {len(coords)} titik (geografis).")
                else:
                    st.error("Terdeteksi koordinat geografis namun polygon gagal dibuat.")
            else:
                st.error("Tidak menemukan koordinat di PDF.")
    elif uploaded.name.lower().endswith(".zip"):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                zf = zipfile.ZipFile(io.BytesIO(uploaded.read()))
                zf.extractall(tmp)
                gdf_polygon = gpd.read_file(tmp)
                if gdf_polygon.crs is None:
                    gdf_polygon.set_crs(epsg=4326, inplace=True)
                gdf_polygon = fix_polygon_geometry(gdf_polygon)
                col2.success("Shapefile PKKPR terbaca.")
                detected_info = {"mode":"shapefile","n_features":len(gdf_polygon)}
            except Exception as e:
                st.error(f"Gagal membaca shapefile: {e}")
                if DEBUG:
                    st.exception(e)

# show detected info in sidebar
if detected_info:
    st.sidebar.markdown("### Hasil Auto-detect")
    for k,v in detected_info.items():
        st.sidebar.write(f"- **{k}** : {v}")

# -------------------------
# Hasil & Luas PKKPR
# -------------------------
if gdf_polygon is not None:
    # ensure polygon-only for export
    try:
        # try to explode and filter polygons if necessary
        geo_only = gdf_polygon.copy()
        geo_only["geom_type"] = geo_only.geometry.geom_type
        if any(geo_only["geom_type"] != "Polygon"):
            geo_only = geo_only.explode(index_parts=False).reset_index(drop=True)
        # keep polygon/multipolygon only
        geo_only = geo_only[geo_only.geometry.geom_type.isin(["Polygon","MultiPolygon"])]
        if geo_only.empty:
            raise ValueError("Tidak ada geometri Polygon valid.")
        zip_bytes = save_shapefile(geo_only)
        st.download_button("⬇️ Download SHP PKKPR (ZIP)", zip_bytes, "PKKPR_Hasil_Konversi.zip", mime="application/zip")
    except Exception as e:
        st.error(f"Gagal menyiapkan shapefile: {e}")
        if DEBUG:
            st.exception(e)

    # luas
    try:
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
        luas_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
        luas_merc = gdf_polygon.to_crs(epsg=3857).area.sum()
        st.info(f"**Analisis Luas Batas PKKPR**:\n- Luas (UTM {utm_zone}): **{format_angka_id(luas_utm)} m²**\n- Luas (WGS84 Mercator): **{format_angka_id(luas_merc)} m²**")
    except Exception as e:
        st.error(f"Gagal menghitung luas: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# -------------------------
# Upload Tapak Proyek
# -------------------------
col1, col2 = st.columns([0.7,0.3])
uploaded_tapak = col1.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"], key="tapak")
gdf_tapak = None
if uploaded_tapak:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            zf = zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()))
            zf.extractall(tmp)
            gdf_tapak = gpd.read_file(tmp)
            if gdf_tapak.crs is None:
                gdf_tapak.set_crs(epsg=4326, inplace=True)
            col2.success("Shapefile tapak proyek terbaca.")
        except Exception as e:
            st.error(f"Gagal baca shapefile tapak: {e}")
            if DEBUG:
                st.exception(e)

# -------------------------
# Overlay
# -------------------------
if gdf_polygon is not None and gdf_tapak is not None:
    try:
        centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
        gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg)
        gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)
        inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how="intersection")
        luas_overlap = inter.area.sum() if not inter.empty else 0
        luas_tapak = gdf_tapak_utm.area.sum()
        luas_outside = luas_tapak - luas_overlap
        st.success(f"**HASIL OVERLAY TAPAK:**\n- Luas Tapak UTM {utm_zone}: **{format_angka_id(luas_tapak)} m²**\n- Luas Tapak di dalam PKKPR: **{format_angka_id(luas_overlap)} m²**\n- Luas Tapak di luar PKKPR : **{format_angka_id(luas_outside)} m²**")
    except Exception as e:
        st.error(f"Gagal overlay: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# -------------------------
# Peta Interaktif
# -------------------------
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    try:
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        m = folium.Map(location=[centroid.y, centroid.x], zoom_start=15, tiles=None)
        Fullscreen(position="bottomleft").add_to(m)
        folium.TileLayer("openstreetmap", name="OpenStreetMap").add_to(m)
        folium.TileLayer("CartoDB Positron", name="CartoDB Positron").add_to(m)
        folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery").add_to(m)
        folium.GeoJson(gdf_polygon.to_crs(epsg=4326), name="PKKPR", style_function=lambda x: {"color":"yellow","weight":3,"fillOpacity":0.1}).add_to(m)
        if gdf_tapak is not None:
            folium.GeoJson(gdf_tapak.to_crs(epsg=4326), name="Tapak Proyek", style_function=lambda x: {"color":"red","fillColor":"red","fillOpacity":0.4}).add_to(m)
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

# -------------------------
# Layout PNG
# -------------------------
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG) untuk Dokumentasi")
    try:
        gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
        xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
        width, height = xmax - xmin, ymax - ymin
        fig, ax = plt.subplots(figsize=(14,10) if width>height else (10,14), dpi=150)
        gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2.5, label="Batas PKKPR")
        if gdf_tapak is not None:
            gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
            gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red", label="Tapak Proyek")
        if gdf_points is not None:
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
        st.download_button("⬇️ Download Layout Peta (PNG)", png_buffer, "layout_peta.png", mime="image/png")
    except Exception as e:
        st.error(f"Gagal membuat layout peta: {e}")
        if DEBUG:
            st.exception(e)
