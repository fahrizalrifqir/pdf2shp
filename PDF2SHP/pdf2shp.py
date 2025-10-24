# full_streamlit_pkkpr.py
import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, tempfile, re, math
from shapely.geometry import Point, Polygon, MultiPolygon, GeometryCollection, MultiPoint, LineString
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

# ======================
# CONFIG
# ======================
st.set_page_config(page_title="PKKPR → SHP + Overlay (Final)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek (Final)")
st.markdown("---")
DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

# Constants (Indonesia bounding box)
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
# DMS parser (mendukung BT/BB/LS/LU serta N/S/E/W)
# ======================
def dms_to_decimal(dms_str):
    """
    Terima string DMS seperti:
      112° 48' 03,590" BT
      7° 14' 32,198" LS
    atau variasi lain dengan spasi / koma desimal.
    Kembalikan float desimal (lon/lat) atau None.
    """
    if not dms_str or not isinstance(dms_str, str):
        return None
    s = dms_str.upper()
    # ganti label lokal ke N/S/E/W
    s = s.replace("BT", "E").replace("BB", "W").replace("LS", "S").replace("LU", "N")
    # normalisasi koma desimal
    s = s.replace(",", ".")
    # kosongkan simbol yang mengganggu
    s = s.replace("°", " ").replace("º"," ").replace("'", " ").replace("’", " ").replace('”', ' ').replace('"', ' ')
    # temukan arah (N/S/E/W) jika ada
    dir_match = re.search(r"\b([NSEW])\b", s)
    direction = dir_match.group(1) if dir_match else None
    # buang huruf arah dari string angka
    s_clean = re.sub(r"[NSEW]", "", s).strip()
    parts = [p for p in re.split(r"\s+", s_clean) if p != ""]
    if len(parts) == 0:
        return None
    try:
        deg = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
    except:
        return None
    val = deg + minutes / 60.0 + seconds / 3600.0
    if direction in ("S", "W"):
        val *= -1.0
    return val

# helper numeric parse
def try_parse_float(s):
    try:
        return float(str(s).strip().replace(",", "."))
    except:
        return None

# ======================
# PDF parsing: fokus ke tabel Bujur / Lintang (DMS LS/BT)
# ======================
def extract_tables_and_coords_from_pdf(uploaded_file):
    """
    Tujuan utama: deteksi tabel dengan header 'Bujur' & 'Lintang' (atau 'Bujur (BT)' dll.)
    dan ekstrak nilai DMS di bawahnya. Jika tidak menemukan tabel semacam itu,
    fallback ke scanning tabel/page secara umum (seperti sebelumnya).
    """
    coords_disetujui, coords_dimohon, coords_plain = [], [], []
    luas_disetujui, luas_dimohon, luas_plain = None, None, None

    # pola untuk menemukan DMS (termasuk BT/LS)
    dms_pattern_generic = re.compile(r"\d{1,3}\s*[°º]?\s*\d{1,2}\s*['’]?\s*\d{1,2}(?:[.,]\d+)?\s*(?:BT|BB|LS|LU|N|S|E|W)\b", flags=re.IGNORECASE)
    num_pattern = re.compile(r"-?\d{1,3}(?:[.,]\d+)+")
    current_mode = "plain"

    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = text.splitlines()

            # jika debug, tampilkan sebagian teks halaman
            if DEBUG:
                st.write(f"DEBUG: Halaman {page.page_number} teks (snippet):")
                st.write("\n".join(lines[:25]))

            # deteksi mode kata 'disetujui' / 'dimohon' di sekitar kata 'koordinat'
            for idx, raw_line in enumerate(lines):
                l = raw_line.lower()
                if "koordinat" in l and "disetujui" in l:
                    current_mode = "disetujui"
                elif "koordinat" in l and "dimohon" in l:
                    current_mode = "dimohon"

            # 1) Coba ekstrak tabel terstruktur terlebih dahulu (page.extract_table)
            table = page.extract_table()
            if table and any(cell for row in table for cell in (row or []) ):
                # convert ke DataFrame sementara agar mudah memeriksa header
                try:
                    df = pd.DataFrame(table[1:], columns=table[0])
                except Exception:
                    # fallback: buat df tanpa header jika bukan table header
                    df = pd.DataFrame(table)
                # normalisasi nama kolom ke lowercase tanpa spasi ekstra
                df.columns = [re.sub(r"\s+", " ", str(c)).strip().lower() for c in df.columns]
                # cek apakah ada kolom 'bujur' dan 'lintang' (bisa variasi)
                candidate_cols = {col: col for col in df.columns}
                bujur_col = None
                lintang_col = None
                for col in df.columns:
                    if "bujur" in col or "longitude" in col or "long" == col:
                        bujur_col = col
                    if "lintang" in col or "latitude" in col or "lat" == col:
                        lintang_col = col

                if bujur_col and lintang_col:
                    if DEBUG:
                        st.write(f"DEBUG: Ditemukan tabel Bujur/Lintang di halaman {page.page_number}, kolom: {bujur_col}, {lintang_col}")
                        st.write(df[[bujur_col, lintang_col]].head(10).to_string(index=False))
                    # iterasi baris dan parse DMS di kolom tersebut
                    for _, row in df.iterrows():
                        raw_bujur = str(row[bujur_col]) if bujur_col in row else ""
                        raw_lintang = str(row[lintang_col]) if lintang_col in row else ""
                        lon = dms_to_decimal(raw_bujur)
                        lat = dms_to_decimal(raw_lintang)
                        if lon is not None and lat is not None:
                            if current_mode == "disetujui":
                                coords_disetujui.append((lon, lat))
                            elif current_mode == "dimohon":
                                coords_dimohon.append((lon, lat))
                            else:
                                coords_plain.append((lon, lat))
                    # selesai dengan tabel ini, lanjut halaman berikutnya
                    continue

                # jika tidak ada header bujur/lintang, coba scanning sel demi sel untuk menemukan pasangan DMS
                pairs_found = []
                flat_cells = [c for row in table for c in (row or []) if c]
                # cari semua DMS dalam cells
                dms_cells = [c for c in flat_cells if dms_pattern_generic.search(str(c))]
                # jika ada banyak, ambil pasangan berurutan
                if len(dms_cells) >= 2:
                    # convert each matched cell to decimal if possible
                    decs = [dms_to_decimal(str(c)) for c in dms_cells]
                    # ambil pasangan (lon,lat) dari decs yang valid dan yang masuk bounds indonesia
                    for i in range(0, len(decs)-1, 2):
                        a = decs[i]; b = decs[i+1]
                        if a is not None and b is not None:
                            if 95 <= a <= 141 and -11 <= b <= 6:
                                pairs_found.append((a, b))
                            elif 95 <= b <= 141 and -11 <= a <= 6:
                                pairs_found.append((b, a))
                # masukkan pairs_found ke list sesuai mode
                for p in pairs_found:
                    if current_mode == "disetujui":
                        coords_disetujui.append(p)
                    elif current_mode == "dimohon":
                        coords_dimohon.append(p)
                    else:
                        coords_plain.append(p)

            # 2) Jika tidak ada tabel atau tabel tidak jelas, scan teks baris demi baris
            # Cari baris yang mengandung dua DMS (bisa dalam satu baris atau dua baris berturut-turut)
            for i in range(len(lines)):
                line = lines[i].strip()
                if not line:
                    continue
                # cari semua DMS di baris
                dmss = dms_pattern_generic.findall(line)
                if dmss and len(dmss) >= 2:
                    # ada dua DMS di satu baris -> ambil pasangan pertama
                    lon = dms_to_decimal(dmss[0])
                    lat = dms_to_decimal(dmss[1])
                    if lon is not None and lat is not None and (95 <= lon <= 141 and -11 <= lat <= 6):
                        if current_mode == "disetujui":
                            coords_disetujui.append((lon, lat))
                        elif current_mode == "dimohon":
                            coords_dimohon.append((lon, lat))
                        else:
                            coords_plain.append((lon, lat))
                        continue
                # jika satu DMS di baris, lihat baris berikutnya
                if dmss and len(dmss) == 1 and i + 1 < len(lines):
                    next_line = lines[i+1].strip()
                    dmss2 = dms_pattern_generic.findall(next_line)
                    if dmss2:
                        lon = dms_to_decimal(dmss[0])
                        lat = dms_to_decimal(dmss2[0])
                        if lon is not None and lat is not None and (95 <= lon <= 141 and -11 <= lat <= 6):
                            if current_mode == "disetujui":
                                coords_disetujui.append((lon, lat))
                            elif current_mode == "dimohon":
                                coords_dimohon.append((lon, lat))
                            else:
                                coords_plain.append((lon, lat))
                        continue
                # fallback: cari dua angka desimal di satu baris (mis. 112,8000 7,2345)
                nums = num_pattern.findall(line)
                if len(nums) >= 2:
                    parsed = [try_parse_float(n) for n in nums]
                    parsed = [p for p in parsed if p is not None]
                    if len(parsed) >= 2:
                        a, b = parsed[0], parsed[1]
                        # cek apakah (a,b) atau (b,a) masuk bounds Indonesia
                        if 95 <= a <= 141 and -11 <= b <= 6:
                            pair = (a, b)
                        elif 95 <= b <= 141 and -11 <= a <= 6:
                            pair = (b, a)
                        else:
                            pair = None
                        if pair:
                            if current_mode == "disetujui":
                                coords_disetujui.append(pair)
                            elif current_mode == "dimohon":
                                coords_dimohon.append(pair)
                            else:
                                coords_plain.append(pair)

            # cari informasi luas juga (baris 'luas' sudah diperiksa per halaman)
            for idx, raw_line in enumerate(lines):
                line = raw_line.strip()
                if "luas" in line.lower():
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

    # priority: disetujui > dimohon > plain
    if coords_disetujui:
        coords = coords_disetujui
    elif coords_dimohon:
        coords = coords_dimohon
    else:
        coords = coords_plain

    # luas priority
    if luas_disetujui:
        luas = luas_disetujui
    elif luas_dimohon:
        luas = luas_dimohon
    else:
        luas = luas_plain

    # deduplicate coordinates (rounded)
    seen = set()
    unique_coords = []
    for xy in coords:
        key = (round(xy[0], 6), round(xy[1], 6))
        if key not in seen:
            unique_coords.append(xy)
            seen.add(key)

    return {"coords": unique_coords, "luas": luas}

# ======================
# UI: Upload PKKPR (PDF atau SHP ZIP)
# ======================
st.subheader("📄 Upload Dokumen PKKPR (PDF atau SHP ZIP)")
col1, col2 = st.columns([3, 2])

with col1:
    uploaded = st.file_uploader("Unggah file PKKPR", type=["pdf", "zip"], label_visibility="collapsed")

gdf_polygon = None
gdf_points = None
luas_pkkpr_doc = None

with col2:
    st.write("Petunjuk: parser akan mengecek tabel bertanda 'Bujur' & 'Lintang' (format DMS LS/BT).")
    if DEBUG:
        st.write("DEBUG mode aktif — info detail akan tampil.")
    if uploaded:
        if uploaded.name.lower().endswith(".pdf"):
            parsed = extract_tables_and_coords_from_pdf(uploaded)
            coords = parsed["coords"]
            luas_pkkpr_doc = parsed["luas"]
            if DEBUG:
                st.write("DEBUG: coords count after parsing:", len(coords))
                if len(coords) > 0:
                    st.write("DEBUG: contoh 20 koordinat:", coords[:20])
            if coords:
                # jika pertama != terakhir, jangan otomatis menutup ring — tapi kita tutup jika tampak seperti polygon
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                try:
                    pts = [Point(x, y) for x, y in coords]
                    gdf_points = gpd.GeoDataFrame(geometry=pts, crs="EPSG:4326")

                    # coba buat polygon sederhana (jika memungkinkan)
                    poly = None
                    try:
                        poly = Polygon(coords)
                        if poly.is_valid and poly.area and poly.geom_type.lower() == "polygon":
                            gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                            gdf_polygon = fix_geometry(gdf_polygon)
                            st.success(f"Berhasil mengekstrak **{len(coords)} titik** dan membentuk polygon ✅")
                        else:
                            # jika polygon tidak valid (contour tipis), tetap simpan titik dan beri peringatan
                            gdf_polygon = None
                            st.warning("Koordinat berhasil diekstrak tetapi polygon tidak valid (mungkin urutan titik). Titik disimpan.")
                            if DEBUG:
                                st.write("DEBUG: Polygon valid?", poly.is_valid if poly is not None else None, "area:", poly.area if poly is not None else None)
                    except Exception as e:
                        gdf_polygon = None
                        st.warning("Gagal membentuk polygon langsung dari koordinat. Titik disimpan.")
                        if DEBUG:
                            st.exception(e)
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
# Analisis Luas PKKPR (tampilan singkat)
# =====================================================
if gdf_polygon is not None:
    if luas_pkkpr_doc:
        st.write(f"Luas PKKPR Dokumen :  {luas_pkkpr_doc}")
    else:
        st.write("Luas PKKPR Dokumen :  (tidak ditemukan di dokumen)")

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
# Analisis Luas Overlay (ringkas)
# =====================================================
if gdf_polygon is not None and gdf_tapak is not None:
    st.subheader("Analisis Luas Overlay")
    try:
        gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
        luas_tapak_merc = gdf_tapak_3857.area.sum()
    except Exception as e:
        luas_tapak_merc = None
        if DEBUG:
            st.write("DEBUG: Gagal hitung luas tapak Mercator:", e)
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    try:
        gdf_tapak_utm = gdf_tapak.to_crs(utm_epsg)
        luas_tapak_utm = gdf_tapak_utm.area.sum()
    except Exception as e:
        luas_tapak_utm = None
        if DEBUG:
            st.write("DEBUG: Gagal hitung luas tapak UTM:", e)
    try:
        gdf_polygon_utm = gdf_polygon.to_crs(utm_epsg)
        inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how="intersection")
        luas_overlap = inter.area.sum()
    except Exception as e:
        luas_overlap = None
        if DEBUG:
            st.write("DEBUG: Gagal hitung overlap UTM:", e)
    st.write(f"Luas Tapak Mercator :  {format_angka_id(luas_tapak_merc) + ' m²' if luas_tapak_merc is not None else '(gagal menghitung)'}")
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
