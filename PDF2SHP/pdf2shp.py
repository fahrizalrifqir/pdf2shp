# full_streamlit_pkkpr.py
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
from math import atan2
import matplotlib.patches as mpatches
import matplotlib.lines as mlines

# ======================
# CONFIG
# ======================
st.set_page_config(page_title="PKKPR → SHP + Overlay (Final)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek (Final)")
st.markdown("---")
DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)
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
    s = str(line).replace('\xa0', ' ').replace('\u00B2', '²').strip()
    s_norm = re.sub(r"\s+", " ", s).upper()
    m = re.search(r"([0-9]+(?:[.,][0-9]+)*)\s*(M2|M²|HA|HEKTAR)\b", s_norm)
    if m:
        num_raw, unit_raw = m.group(1), m.group(2).upper()
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
            return polys[0] if len(polys) == 1 else MultiPolygon(polys) if polys else None
        return geom
    gdf["geometry"] = gdf["geometry"].apply(extract_valid)
    return gdf

def try_parse_float(s):
    try:
        return float(str(s).strip().replace(",", "."))
    except:
        return None

def dms_to_decimal(dms_str):
    if not dms_str or not isinstance(dms_str, str):
        return None
    s = dms_str.upper()
    s = s.replace("BT", "E").replace("BB", "W").replace("LS", "S").replace("LU", "N")
    s = s.replace(",", ".")
    s = s.replace("°", " ").replace("'", " ").replace("’", " ").replace('"', ' ')
    dir_match = re.search(r"\b([NSEW])\b", s)
    direction = dir_match.group(1) if dir_match else None
    s_clean = re.sub(r"[NSEW]", "", s).strip()
    parts = [p for p in re.split(r"\s+", s_clean) if p]
    if not parts:
        return None
    try:
        deg, minutes, seconds = float(parts[0]), float(parts[1]) if len(parts) > 1 else 0, float(parts[2]) if len(parts) > 2 else 0
    except:
        return None
    val = deg + minutes / 60 + seconds / 3600
    if direction in ("S", "W"):
        val *= -1
    return val

# ======================
# UNIVERSAL PDF PARSER
# ======================
def extract_tables_and_coords_from_pdf(uploaded_file):
    coords_plain = []
    text_all = ""

    # Gabungkan semua teks PDF
    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            text_all += (page.extract_text() or "") + "\n"

    # --- deteksi tabel "Bujur/Lintang" atau "Longitude/Latitude" atau "X/Y" + kolom No ---
    coords_with_no = []
    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            try:
                df = pd.DataFrame(table[1:], columns=table[0])
            except:
                df = pd.DataFrame(table)

            # Normalisasi nama kolom
            df.columns = [re.sub(r"\s+", " ", str(c)).strip().lower() for c in df.columns]

            # Deteksi kolom
            no_col, bujur_col, lintang_col = None, None, None
            for col in df.columns:
                if re.match(r"no\b", col):  # kolom No
                    no_col = col
                if any(k in col for k in ["bujur", "longitude", "long", "x"]):
                    bujur_col = col
                if any(k in col for k in ["lintang", "latitude", "lat", "y"]):
                    lintang_col = col

            if bujur_col and lintang_col:
                for _, row in df.iterrows():
                    raw_no = row.get(no_col, None)
                    raw_bujur = str(row.get(bujur_col, "")).strip()
                    raw_lintang = str(row.get(lintang_col, "")).strip()

                    # Deteksi format koordinat (DMS atau desimal)
                    def looks_like_dms(s):
                        return any(sym in s.upper() for sym in ["°", "º", "'", "’", '"', "BT", "LS", "LU", "E", "W"])

                    lon = dms_to_decimal(raw_bujur) if looks_like_dms(raw_bujur) else try_parse_float(raw_bujur)
                    lat = dms_to_decimal(raw_lintang) if looks_like_dms(raw_lintang) else try_parse_float(raw_lintang)

                    if lon and lat:
                        # Swap jika tertukar
                        if not (95 <= lon <= 141 and -11 <= lat <= 6) and (95 <= lat <= 141 and -11 <= lon <= 6):
                            lon, lat = lat, lon
                        if 95 <= lon <= 141 and -11 <= lat <= 6:
                            try:
                                n = int(str(raw_no).strip()) if raw_no not in [None, ""] else None
                            except:
                                n = None
                            coords_with_no.append((n, lon, lat))

    # Jika ada nomor, urutkan berdasarkan nomor
    if coords_with_no:
        coords_with_no.sort(key=lambda x: (x[0] if x[0] is not None else 99999))
        coords_plain = [(lon, lat) for _, lon, lat in coords_with_no]

    # --- fallback: cari pola umum jika tabel tidak ada ---
    if not coords_plain:
        num_pattern = re.compile(r"-?\d{1,3}(?:[.,]\d+)+")
        for line in text_all.splitlines():
            nums = num_pattern.findall(line)
            if len(nums) >= 2:
                a, b = try_parse_float(nums[0]), try_parse_float(nums[1])
                if a and b:
                    if 95 <= a <= 141 and -11 <= b <= 6:
                        coords_plain.append((a, b))
                    elif 95 <= b <= 141 and -11 <= a <= 6:
                        coords_plain.append((b, a))

    # Hapus duplikat
    seen, unique_coords = set(), []
    for xy in coords_plain:
        key = (round(xy[0], 6), round(xy[1], 6))
        if key not in seen:
            unique_coords.append(xy)
            seen.add(key)

    return {"coords": unique_coords, "luas": None}


# ======================
# AUTO SORT KOORDINAT
# ======================
def sort_coords_clockwise(coords):
    if not coords:
        return coords
    cx = sum(x for x, y in coords) / len(coords)
    cy = sum(y for x, y in coords) / len(coords)
    coords_sorted = sorted(coords, key=lambda p: math.atan2(p[1]-cy, p[0]-cx))
    return coords_sorted

# ======================
# UI: Upload
# ======================
st.subheader("📄 Upload Dokumen PKKPR (PDF atau SHP ZIP)")
col1, col2 = st.columns([3, 2])

with col1:
    uploaded = st.file_uploader("Unggah file PKKPR", type=["pdf", "zip"], label_visibility="collapsed")

gdf_polygon = None
gdf_points = None
luas_pkkpr_doc = None

with col2:
    st.write("Parser membaca tabel koordinat (Bujur/Lintang, Longitude/Latitude, atau X/Y).")
    if uploaded:
        if uploaded.name.lower().endswith(".pdf"):
            parsed = extract_tables_and_coords_from_pdf(uploaded)
            coords = parsed["coords"]
            luas_pkkpr_doc = parsed["luas"]
            if coords:
                coords = sort_coords_clockwise(coords)
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                pts = [Point(x, y) for x, y in coords]
                gdf_points = gpd.GeoDataFrame(geometry=pts, crs="EPSG:4326")
                try:
                    poly = Polygon(coords)
                    if poly.is_valid and poly.area > 0:
                        gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                        gdf_polygon = fix_geometry(gdf_polygon)
                        st.success(f"Berhasil mengekstrak {len(coords)} titik dan membentuk polygon ✅")
                    else:
                        st.warning("Koordinat terbaca, tetapi polygon tidak valid — hanya titik disimpan.")
                except Exception as e:
                    st.error("Gagal membentuk polygon.")
                    if DEBUG: st.exception(e)
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

# ======================
# ANALISIS LUAS
# ======================
if gdf_polygon is not None:
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    luas_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
    luas_merc = gdf_polygon.to_crs(epsg=3857).area.sum()

    st.write(f"Luas UTM {utm_zone}: {format_angka_id(luas_utm)} m²")
    st.write(f"Luas Mercator: {format_angka_id(luas_merc)} m²")
    if luas_pkkpr_doc:
        st.write(f"Luas dokumen: {luas_pkkpr_doc}")

    zip_bytes = save_shapefile_layers(gdf_polygon, gdf_points)
    st.download_button("⬇️ Download SHP PKKPR", zip_bytes, "PKKPR_Hasil.zip", mime="application/zip")

# ======================
# UPLOAD TAPAK
# ======================
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
    if gdf_tapak is not None:
        gdf_tapak = fix_geometry(gdf_tapak)
        st.success("Tapak berhasil dimuat ✅")

# ======================
# ANALISIS OVERLAY
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    st.subheader("Analisis Overlay")
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    gdf_tapak_utm = gdf_tapak.to_crs(utm_epsg)
    luas_tapak = gdf_tapak_utm.area.sum()
    gdf_pkkpr_utm = gdf_polygon.to_crs(utm_epsg)
    inter = gpd.overlay(gdf_tapak_utm, gdf_pkkpr_utm, how="intersection")
    luas_overlap = inter.area.sum()
    st.write(f"Luas Tapak UTM {utm_zone}: {format_angka_id(luas_tapak)} m²")
    st.write(f"Luas di dalam PKKPR: {format_angka_id(luas_overlap)} m²")
    st.write(f"Luas di luar PKKPR: {format_angka_id(luas_tapak - luas_overlap)} m²")

# ======================
# PREVIEW PETA
# ======================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None)
    Fullscreen(position="bottomleft").add_to(m)
    folium.TileLayer("openstreetmap").add_to(m)
    folium.TileLayer("CartoDB Positron").add_to(m)
    folium.TileLayer(xyz.Esri.WorldImagery).add_to(m)
    folium.GeoJson(gdf_polygon.to_crs(4326),
                   name="PKKPR",
                   style_function=lambda x: {"color":"yellow","weight":3,"fillOpacity":0.1}).add_to(m)
    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker([row.geometry.y, row.geometry.x],
                                radius=4, color="black", fill=True,
                                fill_color="orange",
                                popup=f"Titik {i+1}").add_to(m)
    if gdf_tapak is not None:
        folium.GeoJson(gdf_tapak.to_crs(4326),
                       name="Tapak Proyek",
                       style_function=lambda x: {"color":"red","fillColor":"red","fillOpacity":0.4}).add_to(m)
    folium.LayerControl().add_to(m)
    st_folium(m, width=900, height=600)

# =====================================================
# Layout PNG — tombol download + legenda (pojok kanan atas)
# =====================================================
import matplotlib.patches as mpatches
import matplotlib.lines as mlines

if gdf_polygon is not None:
    try:
        gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
        xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds

        fig, ax = plt.subplots(figsize=(10, 10), dpi=150)

        gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2.5)

        if 'gdf_tapak' in locals() and gdf_tapak is not None:
            gdf_tapak.to_crs(epsg=3857).plot(ax=ax, facecolor="red", alpha=0.4)

        if gdf_points is not None and not gdf_points.empty:
            gdf_points.to_crs(epsg=3857).plot(ax=ax, color="orange", markersize=20)

        ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)

        ax.set_xlim(xmin - (xmax - xmin) * 0.05, xmax + (xmax - xmin) * 0.05)
        ax.set_ylim(ymin - (ymax - ymin) * 0.05, ymax + (ymax - ymin) * 0.05)
        ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14)
        ax.axis("off")

        legend_elements = [
            mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=2, label="PKKPR (Polygon)"),
            mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
            mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black", linestyle="None",
                          markersize=8, label="PKKPR (Titik)")
        ]
        ax.legend(
            handles=legend_elements,
            loc="upper right",
            fontsize=9,
            frameon=True,
            facecolor="white",
            edgecolor="black",
            title="Keterangan",
            title_fontsize=9
        )

        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=200)
        buf.seek(0)
        plt.close(fig)

        st.download_button("⬇️ Download Peta PNG", data=buf, file_name="Peta_Overlay.png", mime="image/png")

    except Exception as e:
        st.error(f"Gagal membuat peta: {e}")
        if DEBUG:
            st.exception(e)


