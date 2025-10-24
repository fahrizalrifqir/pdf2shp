import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, tempfile, re, math
from shapely.geometry import Point, Polygon, MultiPolygon, GeometryCollection
from shapely.validation import make_valid
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
    """
    Format numeric value ke gaya Indonesia: ribuan pake titik, desimal pake koma.
    Input: float/int/str (jika str yang berisi angka akan dicoba konversi).
    Output: string, tanpa satuan.
    """
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
    """
    Ekstrak nilai luas dari teks (window). Kembalikan string seperti '2.007 Ha' atau '1.234 m²'.
    """
    if not line:
        return None
    s = str(line)
    s = s.replace('\xa0', ' ').replace('\u00B2', '²').strip()
    s_norm = re.sub(r"\s+", " ", s).upper()

    m = re.search(r"([0-9]+(?:[.,][0-9]+)*)\s*(M2|M²|M\s*2|HA|HEKTAR)\b", s_norm, flags=re.IGNORECASE)
    if m:
        num_raw = m.group(1)
        unit_raw = m.group(2).upper()
        unit_out = "Ha" if "HA" in unit_raw else "m²"
        return f"{num_raw} {unit_out}"

    m2 = re.search(r"([0-9]+(?:[.,][0-9]+)*)\b", s)
    if m2:
        return m2.group(1)
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

# ======================
# PDF PARSER LENGKAP
# ======================
def extract_tables_and_coords_from_pdf(uploaded_file):
    def dms_to_decimal(dms_str):
        s = dms_str.replace(",", ".").replace("°", " ").replace("'", " ").replace("\"", " ")
        s = re.sub(r"[NnSsEeWw]", "", s)
        parts = [p for p in s.split() if p.strip()]
        if len(parts) == 0:
            return None
        deg = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0
        seconds = float(parts[2]) if len(parts) > 2 else 0
        val = deg + minutes / 60 + seconds / 3600
        if any(x in dms_str.upper() for x in ["S", "W"]):
            val *= -1
        return val

    def try_parse_float(s):
        try:
            return float(s.strip().replace(",", "."))
        except:
            return None

    def in_indonesia(lon, lat):
        return 95 <= lon <= 141 and -11 <= lat <= 6

    def try_convert_utm(easting, northing):
        for zone in range(46, 52):
            for south in [True, False]:
                epsg = 32700 + zone if south else 32600 + zone
                try:
                    transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
                    lon, lat = transformer.transform(easting, northing)
                    if in_indonesia(lon, lat):
                        return (lon, lat)
                except:
                    continue
        return None

    coords_disetujui, coords_dimohon, coords_plain = [], [], []
    luas_disetujui, luas_dimohon, luas_plain = None, None, None

    num_pattern = r"-?\d{1,3}(?:[.,]\d+)+"
    dms_pattern = r"\d{1,3}[°\s]\d{1,2}['\s]\d{1,2}(?:[.,]\d+)?\s*[NSEW]"

    current_mode = "plain"

    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = text.splitlines()

            for idx, raw_line in enumerate(lines):
                line = raw_line.strip()
                l = line.lower()
                if "koordinat" in l and "disetujui" in l:
                    current_mode = "disetujui"
                elif "koordinat" in l and "dimohon" in l:
                    current_mode = "dimohon"

            for idx, raw_line in enumerate(lines):
                line = raw_line.strip()
                l = line.lower()

                if "luas" in l:
                    window = " ".join([lines[i] for i in range(idx, min(idx+4, len(lines)))])
                    parsed = parse_luas_line(window)
                    if parsed:
                        win_low = window.lower()
                        if "disetujui" in win_low:
                            luas_disetujui = luas_disetujui or parsed
                        elif "dimohon" in win_low or "dimohonkan" in win_low:
                            luas_dimohon = luas_dimohon or parsed
                        else:
                            luas_plain = luas_plain or parsed
                        if DEBUG:
                            st.write("DEBUG: Found luas in window:", window, "->", parsed)
                        continue

                parsed_line = parse_luas_line(line)
                if parsed_line:
                    if "disetujui" in l:
                        luas_disetujui = luas_disetujui or parsed_line
                    elif "dimohon" in l or "dimohonkan" in l:
                        luas_dimohon = luas_dimohon or parsed_line
                    else:
                        luas_plain = luas_plain or parsed_line
                    if DEBUG:
                        st.write("DEBUG: Found luas in line:", line, "->", parsed_line)

            table = page.extract_table()
            if table:
                for row in table:
                    if not row:
                        continue
                    nums = []
                    for cell in row:
                        if not cell:
                            continue
                        for n in re.findall(num_pattern, str(cell)):
                            val = try_parse_float(n)
                            if val is not None:
                                nums.append(val)
                        for d in re.findall(dms_pattern, str(cell)):
                            dec = dms_to_decimal(d)
                            if dec is not None:
                                nums.append(dec)

                    if len(nums) >= 2:
                        a, b = nums[0], nums[1]
                        pair = None
                        if in_indonesia(a, b):
                            pair = (a, b)
                        elif in_indonesia(b, a):
                            pair = (b, a)
                        elif (100000 <= abs(a) <= 9999999) and (100000 <= abs(b) <= 9999999):
                            utm = try_convert_utm(a, b)
                            if utm:
                                pair = utm

                        if pair:
                            if current_mode == "disetujui":
                                coords_disetujui.append(pair)
                            elif current_mode == "dimohon":
                                coords_dimohon.append(pair)
                            else:
                                coords_plain.append(pair)

    if coords_disetujui:
        coords = coords_disetujui
    elif coords_dimohon:
        coords = coords_dimohon
    else:
        coords = coords_plain

    if luas_disetujui:
        luas = luas_disetujui
    elif luas_dimohon:
        luas = luas_dimohon
    else:
        luas = luas_plain

    seen = set()
    unique_coords = []
    for xy in coords:
        key = (round(xy[0], 6), round(xy[1], 6))
        if key not in seen:
            unique_coords.append(xy)
            seen.add(key)

    return {"coords": unique_coords, "luas": luas}

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
                try:
                    gdf_points = gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in coords], crs="EPSG:4326")
                    gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(coords)], crs="EPSG:4326")
                    gdf_polygon = fix_geometry(gdf_polygon)
                    st.success(f"Berhasil mengekstrak **{len(coords)} titik** dari PDF ✅")
                except Exception as e:
                    st.error(f"Gagal membuat geometry dari koordinat: {e}")
                    if DEBUG:
                        st.exception(e)
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
# Analisis Luas PKKPR (tampilan persis format yang diminta)
# =====================================================
if gdf_polygon is not None:
    # Luas PKKPR Dokumen (teks asli dari dokumen)
    if luas_pkkpr_doc:
        st.write(f"Luas PKKPR Dokumen :  {luas_pkkpr_doc}")
    else:
        st.write("Luas PKKPR Dokumen :  (tidak ditemukan di dokumen)")

    # centroid & area calculations
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)

    try:
        luas_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
    except Exception as e:
        luas_utm = None
        if DEBUG:
            st.write("DEBUG: Gagal menghitung luas UTM:", e)
    try:
        luas_merc = gdf_polygon.to_crs(epsg=3857).area.sum()
    except Exception as e:
        luas_merc = None
        if DEBUG:
            st.write("DEBUG: Gagal menghitung luas Mercator:", e)

    # tampilkan dalam satu baris, dua spasi setelah titik dua
    st.write(f"Luas PKKPR (UTM {utm_zone}):  {format_angka_id(luas_utm) + ' m²' if luas_utm is not None else '(gagal menghitung)'}")
    st.write(f"Luas PKKPR (Mercator):  {format_angka_id(luas_merc) + ' m²' if luas_merc is not None else '(gagal menghitung)'}")

    zip_bytes = save_shapefile_layers(gdf_polygon, gdf_points)
    st.download_button("⬇️ Download SHP PKKPR", zip_bytes, "PKKPR_Hasil.zip", mime="application/zip")

# =====================================================
# Upload Tapak (Overlay)
# =====================================================
st.subheader("🏗️ Upload Shapefile Tapak Proyek (ZIP)")
uploaded_tapak = st.file_uploader("Unggah Tapak Proyek", type=["zip"], key="tapak")
gdf_tapak = None
if uploaded_tapak and gdf_polygon is not None:
    with tempfile.TemporaryDirectory() as tmp:
        zf = zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()))
        zf.extractall(tmp)
        for root, _, files in os.walk(tmp):
            for f in files:
                if f.lower().endswith(".shp"):
                    gdf_tapak = gpd.read_file(os.path.join(root, f))
                    break
    if gdf_tapak is None:
        st.error("Tidak menemukan file .shp di dalam ZIP Tapak.")
    else:
        gdf_tapak = fix_geometry(gdf_tapak)

# =====================================================
# Analisis Luas Overlay (format persis diminta)
# =====================================================
if gdf_polygon is not None and gdf_tapak is not None:
    st.subheader("Analisis Luas Overlay")

    # Luas Tapak Mercator
    try:
        gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
        luas_tapak_merc = gdf_tapak_3857.area.sum()
    except Exception as e:
        luas_tapak_merc = None
        if DEBUG:
            st.write("DEBUG: Gagal hitung luas tapak Mercator:", e)

    # Luas Tapak UTM (menggunakan zona UTM dari centroid PKKPR)
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    try:
        gdf_tapak_utm = gdf_tapak.to_crs(utm_epsg)
        luas_tapak_utm = gdf_tapak_utm.area.sum()
    except Exception as e:
        luas_tapak_utm = None
        if DEBUG:
            st.write("DEBUG: Gagal hitung luas tapak UTM:", e)

    # Overlap (hitung di UTM untuk presisi)
    try:
        gdf_polygon_utm = gdf_polygon.to_crs(utm_epsg)
        inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how="intersection")
        luas_overlap = inter.area.sum()
    except Exception as e:
        luas_overlap = None
        if DEBUG:
            st.write("DEBUG: Gagal hitung overlap UTM:", e)

    # tampilkan Tapak Mercator (sebelum header overlay, sesuai permintaan Anda sebelumnya)
    st.write(f"Luas Tapak Mercator :  {format_angka_id(luas_tapak_merc) + ' m²' if luas_tapak_merc is not None else '(gagal menghitung)'}")

    # Header Analisis Luas Overlay dan tiga baris hasilnya
    st.write("")  # pemisah kosong
    st.write(f"Luas Tapak UTM {utm_zone} :  {format_angka_id(luas_tapak_utm) + ' m²' if luas_tapak_utm is not None else '(gagal menghitung)'}")
    st.write(f"Luas Tapak di dalam PKKPR UTM {utm_zone} :  {format_angka_id(luas_overlap) + ' m²' if luas_overlap is not None else '(gagal menghitung)'}")
    if luas_tapak_utm is not None and luas_overlap is not None:
        luar = luas_tapak_utm - luas_overlap
        st.write(f"Luas Tapak di luar PKKPR UTM {utm_zone} :  {format_angka_id(luar) + ' m²'}")
    else:
        st.write(f"Luas Tapak di luar PKKPR UTM {utm_zone} :  (gagal menghitung)")

# =====================================================
# PREVIEW PETA
# =====================================================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    try:
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None)
        Fullscreen(position="bottomleft").add_to(m)
        folium.TileLayer("openstreetmap", name="OpenStreetMap").add_to(m)
        folium.TileLayer("CartoDB Positron", name="CartoDB Positron").add_to(m)
        folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery").add_to(m)
        folium.GeoJson(gdf_polygon.to_crs(epsg=4326),
                       name="PKKPR",
                       style_function=lambda x: {"color":"yellow","weight":3,"fillOpacity":0.1}).add_to(m)
        if gdf_points is not None:
            for i, row in gdf_points.iterrows():
                folium.CircleMarker([row.geometry.y, row.geometry.x],
                                    radius=4, color="black", fill=True,
                                    fill_color="orange",
                                    popup=f"Titik {i+1}").add_to(m)
        if gdf_tapak is not None:
            folium.GeoJson(gdf_tapak.to_crs(epsg=4326),
                           name="Tapak Proyek",
                           style_function=lambda x: {"color":"red","fillColor":"red","fillOpacity":0.4}).add_to(m)
        folium.LayerControl(collapsed=True).add_to(m)
        st_folium(m, width=900, height=600)
    except Exception as e:
        st.error(f"Gagal menampilkan peta: {e}")
        if DEBUG:
            st.exception(e)

# =====================================================
# Layout PNG — tombol download
# =====================================================
if gdf_polygon is not None:
    try:
        gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
        xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
        fig, ax = plt.subplots(figsize=(10, 10), dpi=150)
        gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2.5)
        if gdf_tapak is not None:
            gdf_tapak.to_crs(epsg=3857).plot(ax=ax, facecolor="red", alpha=0.4)
        if gdf_points is not None:
            gdf_points.to_crs(epsg=3857).plot(ax=ax, color="orange", markersize=20)
        ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)
        ax.set_xlim(xmin - (xmax - xmin) * 0.05, xmax + (xmax - xmin) * 0.05)
        ax.set_ylim(ymin - (ymax - ymin) * 0.05, ymax + (ymax - ymin) * 0.05)
        ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14)
        ax.axis("off")
        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=200)
        buf.seek(0)
        plt.close(fig)
        st.download_button("⬇️ Download Peta PNG", data=buf, file_name="Peta_Overlay.png", mime="image/png")
    except Exception as e:
        st.error(f"Gagal membuat peta: {e}")
        if DEBUG:
            st.exception(e)
