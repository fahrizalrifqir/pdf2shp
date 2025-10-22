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

# ======================
# KONFIGURASI
# ======================
st.set_page_config(page_title="PKKPR → SHP & Overlay (Final)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")
st.markdown("---")
DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

PURWAKARTA_CENTER = (107.44, -6.56)
INDO_BOUNDS = (95.0, 141.0, -11.0, 6.0)

# ======================
# FUNGSI UMUM
# ======================
def normalize_text(s):
    if not s: return s
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

# ======================
# PERBAIKAN GEOMETRI
# ======================
def fix_polygon_geometry(gdf):
    if gdf is None or len(gdf) == 0:
        return gdf
    try:
        gdf["geometry"] = gdf["geometry"].apply(lambda g: make_valid(g))
    except Exception:
        pass
    b = gdf.total_bounds
    if (-180 <= b[0] <= 180) and (-90 <= b[1] <= 90):
        return gdf.set_crs(epsg=4326, allow_override=True)
    for fac in [10, 100, 1000, 10000, 100000]:
        g2 = gdf.copy()
        g2["geometry"] = g2["geometry"].apply(lambda g: affinity.scale(g, xfact=1/fac, yfact=1/fac, origin=(0,0)))
        b2 = g2.total_bounds
        if (95 <= b2[0] <= 145) and (-11 <= b2[1] <= 6):
            return g2.set_crs(epsg=4326, allow_override=True)
    return gdf

# ======================
# SIMPAN SHAPEFILE (POLYGON + POINT)
# ======================
def save_shapefile(gdf_polygon, gdf_points=None):
    with tempfile.TemporaryDirectory() as tmp:
        files_written = []
        if gdf_polygon is not None and not gdf_polygon.empty:
            poly_path = os.path.join(tmp, "PKKPR_Polygon.shp")
            gdf_polygon.to_crs(epsg=4326).to_file(poly_path)
            files_written += [os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("PKKPR_Polygon")]
        if gdf_points is not None and not gdf_points.empty:
            point_path = os.path.join(tmp, "PKKPR_Points.shp")
            gdf_points.to_crs(epsg=4326).to_file(point_path)
            files_written += [os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("PKKPR_Points")]
        if not files_written:
            raise ValueError("Tidak ada data geometri valid untuk disimpan.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files_written:
                zf.write(f, arcname=os.path.basename(f))
        buf.seek(0)
        return buf.read()

# ======================
# PARSING KOORDINAT
# ======================
def dms_bt_ls_to_decimal(dms_str):
    if not isinstance(dms_str, str): return None
    dms_str = dms_str.replace(",", ".").strip()
    pattern = r"(\d+)[°:\s]+(\d+)[\'′:\s]+([\d.]+)\"?\s*([A-Za-z]*)"
    m = re.search(pattern, dms_str)
    if not m: return None
    d, mm, ss, dirc = m.groups()
    dec = float(d) + float(mm)/60.0 + float(ss)/3600.0
    if dirc and dirc.upper() in ["S", "LS", "W", "BB"]:
        dec = -abs(dec)
    return dec

def extract_coords_bt_ls_from_text(text):
    out=[]
    text = normalize_text(text)
    pattern = r"(\d{1,3}°\s*\d{1,2}'\s*[\d,\.]+\"\s*B[BT])[^0-9]+(\d{1,2}°\s*\d{1,2}'\s*[\d,\.]+\"\s*[LS])"
    for m in re.finditer(pattern, text, flags=re.IGNORECASE):
        lon_raw, lat_raw = m.groups()
        lon = dms_bt_ls_to_decimal(lon_raw)
        lat = dms_bt_ls_to_decimal(lat_raw)
        if lon and lat: out.append((lon, lat))
    return out

def extract_coords_from_text(text):
    out=[]
    text = normalize_text(text)
    pattern = r"(-?\d{1,3}\.\d+)[^\d\-\.,]+(-?\d{1,3}\.\d+)"
    for m in re.finditer(pattern, text):
        a,b = float(m.group(1)), float(m.group(2))
        if 90 <= abs(a) <= 145 and -11 <= b <= 6: out.append((a,b))
        elif 90 <= abs(b) <= 145 and -11 <= a <= 6: out.append((b,a))
    return out

def extract_coords_comma_decimal(text):
    out=[]
    text = normalize_text(text)
    pattern = r"(\d{1,3},\d+)\s+(-?\d{1,2},\d+)"
    for m in re.finditer(pattern, text):
        try:
            lon = float(m.group(1).replace(",", "."))
            lat = float(m.group(2).replace(",", "."))
            if 90 <= abs(lon) <= 145 and -11 <= lat <= 6:
                out.append((lon, lat))
        except: pass
    return out

def extract_coords_projected(text):
    out=[]
    text = normalize_text(text)
    pattern = r"(-?\d{5,13}(?:\.\d+)?)[^\d\-\.]{1,6}(-?\d{5,13}(?:\.\d+)?)"
    for m in re.finditer(pattern, text):
        try: out.append((float(m.group(1)), float(m.group(2))))
        except: pass
    return out

def auto_fix_to_polygon(coords):
    if not coords or len(coords) < 3: return None
    unique=[]
    for c in coords:
        if not unique or c != unique[-1]: unique.append(c)
    if unique[0] != unique[-1]: unique.append(unique[0])
    try:
        poly = Polygon(unique)
        if not poly.is_valid or poly.area == 0:
            pts = gpd.GeoSeries([Point(x,y) for x,y in unique], crs="EPSG:4326")
            poly = pts.unary_union.convex_hull
        return poly
    except: return None

# ======================
# DETEKSI UTM 46–50S
# ======================
def in_indonesia(lon, lat):
    lon_min, lon_max, lat_min, lat_max = INDO_BOUNDS
    return (lon_min <= lon <= lon_max) and (lat_min <= lat <= lat_max)

def dist_to_purwakarta(lon, lat):
    return math.hypot(lon - PURWAKARTA_CENTER[0], lat - PURWAKARTA_CENTER[1])

def try_zones_orders(e, n, zones=[46,47,48,49,50], prioritize=32748):
    cands=[]
    for z in zones:
        epsg = 32700 + z
        transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        for order in ["xy","yx"]:
            try:
                lon, lat = transformer.transform(e, n) if order=="xy" else transformer.transform(n, e)
                if in_indonesia(lon, lat):
                    cands.append({"epsg":epsg,"order":order,"lon":lon,"lat":lat,"dist":dist_to_purwakarta(lon,lat)})
            except: pass
    return sorted(cands, key=lambda c: (0 if c["epsg"]==prioritize else 1, c["dist"]))

def detect_projected_pairs_with_priority(pairs, prioritize=32748):
    if not pairs: return None, None, None
    e0,n0 = pairs[len(pairs)//2]
    cand = try_zones_orders(e0,n0,prioritize=prioritize)
    if not cand: return None,None,None
    epsg = cand[0]["epsg"]; order = cand[0]["order"]
    t = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    res=[]
    for e,n in pairs:
        lon,lat = t.transform(e,n) if order=="xy" else t.transform(n,e)
        res.append((lon,lat))
    return res,epsg,order

# ======================
# BACA PDF
# ======================
@st.cache_data
def parse_pdf_texts(f):
    pages=[]
    with pdfplumber.open(f) as pdf:
        for p in pdf.pages:
            pages.append(p.extract_text() or "")
    return pages

# ======================
# INPUT FILE
# ======================
col1,col2=st.columns([0.7,0.3])
uploaded=col1.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)",type=["pdf","zip"])
epsg_override=st.sidebar.text_input("Override EPSG (mis. 32748)","")

gdf_polygon=None; gdf_points=None; detected_info={}

if uploaded:
    if uploaded.name.lower().endswith(".pdf"):
        texts=parse_pdf_texts(uploaded)
        coords=[]; proj=[]
        for t in texts:
            coords+=extract_coords_bt_ls_from_text(t)
            coords+=extract_coords_from_text(t)
            coords+=extract_coords_comma_decimal(t)
            proj+=extract_coords_projected(t)
        if len(proj)>=max(3,len(coords)):
            epsg_override_i=int(epsg_override) if epsg_override.isdigit() else None
            if epsg_override_i:
                t=Transformer.from_crs(f"EPSG:{epsg_override_i}","EPSG:4326",always_xy=True)
                res=[t.transform(e,n) for e,n in proj]
                coords=res; epsg=epsg_override_i; order="xy"
            else:
                coords,epsg,order=detect_projected_pairs_with_priority(proj,prioritize=32748)
            if coords:
                gdf_points=gpd.GeoDataFrame(pd.DataFrame(coords,columns=["Lon","Lat"]),geometry=[Point(x,y) for x,y in coords],crs="EPSG:4326")
                gdf_polygon=gpd.GeoDataFrame(geometry=[auto_fix_to_polygon(coords)],crs="EPSG:4326")
                gdf_polygon=fix_polygon_geometry(gdf_polygon)
                detected_info={"mode":"projected","epsg":epsg,"order":order,"n_points":len(coords)}
                col2.success(f"Terbaca {len(coords)} titik (EPSG {epsg}, {order})")
        elif coords:
            gdf_points=gpd.GeoDataFrame(pd.DataFrame(coords,columns=["Lon","Lat"]),geometry=[Point(x,y) for x,y in coords],crs="EPSG:4326")
            gdf_polygon=gpd.GeoDataFrame(geometry=[auto_fix_to_polygon(coords)],crs="EPSG:4326")
            gdf_polygon=fix_polygon_geometry(gdf_polygon)
            detected_info={"mode":"geographic","n_points":len(coords)}
            col2.success(f"Terbaca {len(coords)} titik geografis.")
        else:
            st.error("Tidak ditemukan koordinat di PDF.")
    elif uploaded.name.lower().endswith(".zip"):
        with tempfile.TemporaryDirectory() as tmp:
            zf=zipfile.ZipFile(io.BytesIO(uploaded.read()))
            zf.extractall(tmp)
            gdf_polygon=gpd.read_file(tmp)
            if gdf_polygon.crs is None: gdf_polygon.set_crs(epsg=4326,inplace=True)
            gdf_polygon=fix_polygon_geometry(gdf_polygon)
            col2.success("Shapefile PKKPR terbaca.")
            detected_info={"mode":"shapefile","n_features":len(gdf_polygon)}

if detected_info:
    st.sidebar.write("### Deteksi:")
    for k,v in detected_info.items(): st.sidebar.write(f"- **{k}**: {v}")

# ======================
# OUTPUT & LUAS
# ======================
if gdf_polygon is not None:
    try:
        zip_bytes=save_shapefile(gdf_polygon,gdf_points)
        st.download_button("⬇️ Download SHP PKKPR (ZIP)",zip_bytes,"PKKPR_Hasil_Konversi.zip",mime="application/zip")
    except Exception as e:
        st.error(f"Gagal menyiapkan shapefile: {e}")
    try:
        c=gdf_polygon.to_crs(4326).geometry.centroid.iloc[0]
        epsg,zone=get_utm_info(c.x,c.y)
        luas_utm=gdf_polygon.to_crs(epsg=epsg).area.sum()
        luas_merc=gdf_polygon.to_crs(epsg=3857).area.sum()
        st.info(f"**Luas PKKPR:**\n- UTM {zone}: **{format_angka_id(luas_utm)} m²**\n- Mercator: **{format_angka_id(luas_merc)} m²**")
    except Exception as e:
        st.error(f"Gagal menghitung luas: {e}")
    st.markdown("---")

# ======================
# TAPAK PROYEK
# ======================
col1,col2=st.columns([0.7,0.3])
tapak=col1.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)",type=["zip"],key="tapak")
gdf_tapak=None
if tapak:
    with tempfile.TemporaryDirectory() as tmp:
        zf=zipfile.ZipFile(io.BytesIO(tapak.read()))
        zf.extractall(tmp)
        gdf_tapak=gpd.read_file(tmp)
        if gdf_tapak.crs is None:gdf_tapak.set_crs(epsg=4326,inplace=True)
        col2.success("Shapefile Tapak terbaca.")

# ======================
# OVERLAY
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    try:
        c=gdf_tapak.to_crs(4326).geometry.centroid.iloc[0]
        epsg,zone=get_utm_info(c.x,c.y)
        gdf_t=gdf_tapak.to_crs(epsg); gdf_p=gdf_polygon.to_crs(epsg)
        inter=gpd.overlay(gdf_t,gdf_p,how="intersection")
        luas_t=gdf_t.area.sum(); luas_i=inter.area.sum() if not inter.empty else 0
        st.success(f"**HASIL OVERLAY:**\n- Luas Tapak: **{format_angka_id(luas_t)} m²**\n- Dalam PKKPR: **{format_angka_id(luas_i)} m²**\n- Di luar: **{format_angka_id(luas_t-luas_i)} m²**")
    except Exception as e:
        st.error(f"Gagal overlay: {e}")
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


