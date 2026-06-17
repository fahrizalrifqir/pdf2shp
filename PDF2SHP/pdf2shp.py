import streamlit as st
import geopandas as gpd
import pandas as pd
import io
import os
import zipfile
import tempfile
import re
import math
import pdfplumber
import folium
import contextily as ctx
import xyzservices.providers as xyz
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines

from shapely.geometry import (
    Point,
    Polygon,
    MultiPolygon,
    GeometryCollection,
    LineString,
)
from shapely.validation import make_valid
from shapely.ops import polygonize_full, unary_union
from streamlit_folium import st_folium
from folium.plugins import Fullscreen

# =========================================================
# CONFIG
# =========================================================
st.set_page_config(
    page_title="PKKPR Overlay Analyzer",
    layout="wide"
)

st.title("🗺️ PKKPR → SHP + Overlay Tapak Proyek")
st.markdown("---")

DEBUG = st.sidebar.checkbox("Debug Mode", False)

# =========================================================
# FORMAT
# =========================================================
def format_angka_id(value):
    try:
        val = float(value)
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")
        s = f"{val:,.2f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(value)

# =========================================================
# CRS
# =========================================================
def get_utm_info(lon, lat):
    zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        epsg = 32600 + zone
    else:
        epsg = 32700 + zone
    return epsg, f"{zone}{'N' if lat >= 0 else 'S'}"

try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CSV_PATH = os.path.join(BASE_DIR, "Kecamatan.csv")
    
    df_wilayah = pd.read_csv(
        CSV_PATH,
        sep=";",
        encoding="utf-8"
    )
    df_wilayah.columns = df_wilayah.columns.astype(str).str.strip()
except:
    df_wilayah = pd.DataFrame(columns=["PROVINSI", "KABUPATEN/KOTA", "KECAMATAN", "X", "Y"])

# =========================================================
# SIDEBAR ZONA UTM
# =========================================================
st.sidebar.markdown("---")
st.sidebar.subheader("🗺️ Zona UTM")

provinsi = st.sidebar.selectbox(
    "Provinsi",
    [""] + sorted(df_wilayah["PROVINSI"].dropna().astype(str).unique().tolist())
)

df_filter = df_wilayah.copy()

if provinsi:
    df_filter = df_filter[df_filter["PROVINSI"] == provinsi]

kabupaten = st.sidebar.selectbox(
    "Kabupaten/Kota",
    [""] + sorted(df_filter["KABUPATEN/KOTA"].dropna().astype(str).unique().tolist())
)

if kabupaten:
    df_filter = df_filter[df_filter["KABUPATEN/KOTA"] == kabupaten]

kecamatan = st.sidebar.selectbox(
    "Kecamatan",
    [""] + sorted(df_filter["KECAMATAN"].dropna().astype(str).unique().tolist())
)

st.sidebar.markdown("---")

if kecamatan:
    df_zona = df_filter[df_filter["KECAMATAN"] == kecamatan].copy()
elif kabupaten:
    df_zona = df_filter[df_filter["KABUPATEN/KOTA"] == kabupaten].copy()
elif provinsi:
    df_zona = df_filter[df_filter["PROVINSI"] == provinsi].copy()
else:
    df_zona = pd.DataFrame()

if not df_zona.empty:
    zona_list = []
    for _, row in df_zona.iterrows():
        try:
            lon = float(row["X"])
            lat = float(row["Y"])
            epsg, zona = get_utm_info(lon, lat)
            zona_list.append((zona, epsg))
        except:
            pass
    zona_unik = sorted(set(zona_list))
    st.sidebar.markdown("### Zona UTM")
    for zona, epsg in zona_unik:
        st.sidebar.success(f"Zona UTM : {zona} | EPSG : {epsg}")

# =========================================================
# PARSE
# =========================================================
def try_parse_float(s):
    try:
        return float(str(s).strip().replace(",", "."))
    except:
        return None

def dms_to_decimal(coord):
    if coord is None:
        return None
    s = str(coord).upper().strip()
    s = (
        s.replace("BT", "E").replace("BB", "W")
        .replace("LS", "S").replace("LU", "N")
        .replace("º", "°").replace("'", "'")
        .replace("′", "'").replace("″", '"')
    )
    direction = None
    m = re.search(r"[NSEW]", s)
    if m:
        direction = m.group(0)
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", s)
    if not nums:
        return None
    try:
        deg = float(nums[0])
        minutes = float(nums[1]) if len(nums) > 1 else 0
        seconds = float(nums[2]) if len(nums) > 2 else 0
    except:
        return None
    val = abs(deg) + (minutes / 60) + (seconds / 3600)
    if direction in ["S", "W"] or str(coord).strip().startswith("-"):
        val *= -1
    return val

def parse_any_coordinate(val):
    if val is None:
        return None
    s = str(val).strip()
    f = try_parse_float(s)
    if f is not None:
        return f
    return dms_to_decimal(s)

def normalize_lon_lat(a, b):
    if a is None or b is None:
        return None
    if 95 <= a <= 141 and -15 <= b <= 15:
        return (a, b)
    if 95 <= b <= 141 and -15 <= a <= 15:
        return (b, a)
    if abs(a) > 1000 and abs(b) > 1000:
        return (a, b)
    return None

# =========================================================
# GEOMETRY
# =========================================================
def fix_geometry(gdf):
    if gdf is None or gdf.empty:
        return gdf
    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.apply(make_valid)

    def clean_geom(geom):
        if geom is None:
            return None
        if geom.geom_type == "GeometryCollection":
            polys = [g for g in geom.geoms if g.geom_type in ["Polygon", "MultiPolygon"]]
            if len(polys) == 0:
                return None
            if len(polys) == 1:
                return polys[0]
            return MultiPolygon(polys)
        return geom

    gdf["geometry"] = gdf.geometry.apply(clean_geom)
    gdf = gdf[gdf.geometry.notnull()]
    gdf["geometry"] = gdf.geometry.buffer(0)
    return gdf

def sort_coords_clockwise(coords):
    cx = sum(x for x, y in coords) / len(coords)
    cy = sum(y for x, y in coords) / len(coords)
    return sorted(coords, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))

# =========================================================
# PDF COORD PARSER
# =========================================================
def parse_coords_from_text_block(block):
    coords = []
    lines = block.splitlines()
    for line in lines:
        nums = re.findall(r'[-+]?\d+(?:\.\d+)?', line)
        if len(nums) >= 2:
            a = parse_any_coordinate(nums[-2])
            b = parse_any_coordinate(nums[-1])
            xy = normalize_lon_lat(a, b)
            if xy:
                coords.append(xy)
    return coords

def get_table_priority(text):
    text = str(text).lower()
    if "tabel koordinat yang disetujui" in text:
        return 1
    if "tabel koordinat yang dimohonkan" in text:
        return 2
    if "tabel koordinat yang dimohonkan dan disetujui" in text:
        return 3
    return 999

def detect_coordinate_type(coords):
    if not coords:
        return "UNKNOWN"
    xs = [x for x, y in coords]
    ys = [y for x, y in coords]
    try:
        maxx = max(xs); minx = min(xs)
        maxy = max(ys); miny = min(ys)
        if (90 <= minx <= 150 and 90 <= maxx <= 150 and -15 <= miny <= 15 and -15 <= maxy <= 15):
            return "WGS84"
        if (100000 <= maxx <= 900000 and 1000000 <= maxy <= 10000000):
            return "UTM"
        if (maxx > 1000 and maxy > 1000):
            return "TM3"
    except:
        pass
    return "UNKNOWN"

def extract_tables_and_coords_from_pdf(uploaded_file):
    uploaded_file.seek(0)
    candidate_tables = []

    with pdfplumber.open(uploaded_file) as pdf:
        for page_no, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            priority = get_table_priority(page_text)
            try:
                tables = page.extract_tables()
            except:
                tables = []
            for table in tables:
                if not table or len(table) < 2:
                    continue
                candidate_tables.append({"priority": priority, "page": page_no, "table": table})

    candidate_tables.sort(key=lambda x: (x["priority"], x["page"]))
    all_results = []
    seen_coords = set()

    for item in candidate_tables:
        table = item["table"]
        try:
            df = pd.DataFrame(table[1:], columns=table[0])
        except:
            continue

        df.columns = [re.sub(r"\s+", " ", str(c)).strip().lower() for c in df.columns]

        no_col = x_col = y_col = ket_col = None
        for c in df.columns:
            if "no" in c:
                no_col = c
            if any(k in c for k in ["bujur", "longitude", "long", "x"]):
                x_col = c
            if any(k in c for k in ["lintang", "latitude", "lat", "y"]):
                y_col = c
            if "keterangan" in c:
                ket_col = c

        if not (x_col and y_col):
            continue

        coords_with_no = []
        groups = {}
        last_ket = None

        for _, row in df.iterrows():
            try:
                x = parse_any_coordinate(row.get(x_col))
                y = parse_any_coordinate(row.get(y_col))
                if x is None or y is None:
                    continue
                xy = normalize_lon_lat(x, y)
                if not xy:
                    continue
                ket = ""
                if ket_col:
                    val = row.get(ket_col)
                    if pd.notna(val):
                        ket = str(val).strip()
                        if ket:
                            last_ket = ket
                if last_ket:
                    groups.setdefault(last_ket, []).append(xy)
                no = len(coords_with_no) + 1
                if no_col:
                    try:
                        no = int(str(row.get(no_col)).strip())
                    except:
                        pass
                coords_with_no.append((no, xy))
            except:
                continue

        if groups:
            for nama_sumur, coords in groups.items():
                if len(coords) < 4:
                    continue
                coord_type = detect_coordinate_type(coords)
                coord_signature = tuple((round(x, 8), round(y, 8)) for x, y in coords)
                if coord_signature in seen_coords:
                    continue
                seen_coords.add(coord_signature)
                all_results.append({"nama": nama_sumur, "coords": coords, "coord_type": coord_type, "page": item["page"]})

        if len(coords_with_no) >= 3:
            coords_with_no.sort(key=lambda x: x[0])
            coords = [xy for _, xy in coords_with_no]
            coord_type = detect_coordinate_type(coords)
            if coord_type == "TM3":
                continue
            coord_signature = tuple((round(x, 8), round(y, 8)) for x, y in coords)
            if coord_signature in seen_coords:
                continue

            # Cek apakah tabel ini adalah lanjutan dari tabel sebelumnya
            # (tabel multi-halaman yang dipecah — nomor urut lanjut dari tabel sebelumnya)
            merged = False
            if all_results:
                prev = all_results[-1]
                prev_coords = prev["coords"]
                # Cek apakah titik pertama tabel ini dekat dengan titik terakhir tabel sebelumnya
                # atau nomor koordinat lanjut (tidak mulai dari 1)
                first_no = coords_with_no[0][0] if coords_with_no else 1
                if first_no > 1 and abs(item["page"] - prev.get("page", 0)) <= 2:
                    # Gabung ke tabel sebelumnya
                    merged_coords = prev_coords + coords
                    # Hapus duplikat berurutan
                    deduped = [merged_coords[0]]
                    for c in merged_coords[1:]:
                        if (round(c[0], 6), round(c[1], 6)) != (round(deduped[-1][0], 6), round(deduped[-1][1], 6)):
                            deduped.append(c)
                    prev["coords"] = deduped
                    prev["coord_type"] = detect_coordinate_type(deduped)
                    seen_coords.add(coord_signature)
                    merged = True

            if not merged:
                seen_coords.add(coord_signature)
                all_results.append({"coords": coords, "coord_type": coord_type, "page": item["page"], "nama": f"PKKPR {len(all_results)+1}"})

    if len(all_results) > 0:
        return all_results

    uploaded_file.seek(0)
    full_text = ""
    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

    coords = parse_coords_from_text_block(full_text)
    if len(coords) >= 3:
        coord_type = detect_coordinate_type(coords)
        return [{"coords": coords, "coord_type": coord_type, "page": 0, "nama": "PKKPR 1"}]

    return []

# =========================================================
# SHP
# =========================================================
def read_shp_zip(uploaded):
    with tempfile.TemporaryDirectory() as tmp:
        zf = zipfile.ZipFile(io.BytesIO(uploaded.read()))
        zf.extractall(tmp)
        shp_path = None
        for root, _, files in os.walk(tmp):
            for f in files:
                if f.lower().endswith(".shp"):
                    shp_path = os.path.join(root, f)
                    break
        if shp_path:
            return gpd.read_file(shp_path)
    return None

def show_attributes(gdf, title):
    cols = [c for c in gdf.columns if c.lower() != "geometry"]
    if cols:
        st.subheader(title)
        st.dataframe(gdf[cols], use_container_width=True)

def save_shapefile_layers(gdf_poly, gdf_points):
    with tempfile.TemporaryDirectory() as tmpdir:
        if gdf_poly is not None:
            gdf_poly.to_crs(4326).to_file(os.path.join(tmpdir, "PKKPR_Polygon.shp"))
        if gdf_points is not None:
            gdf_points.to_crs(4326).to_file(os.path.join(tmpdir, "PKKPR_Points.shp"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in os.listdir(tmpdir):
                zf.write(os.path.join(tmpdir, f), arcname=f)
        buf.seek(0)
        return buf.read()

# =========================================================
# STATE
# =========================================================
gdf_polygon = None
gdf_points = None
gdf_tapak = None
coord_type = "WGS84"

# =========================================================
# SINGLE PAGE LAYOUT
# =========================================================

# --- ROW 1: Upload ---
col_upload, col_tapak_upload = st.columns(2)

with col_upload:
    st.write("**Dokumen PKKPR**")
    uploaded = st.file_uploader("Upload PDF / SHP ZIP", type=["pdf", "zip"])
    info_box = st.empty()
    pkkpr_luas_box = st.empty()
    info_box_detail = st.container()   # ← baris luas UTM & Mercator PKKPR


with col_tapak_upload:
    st.write("**Tapak Proyek**")
    uploaded_tapak = st.file_uploader("Upload SHP ZIP Tapak", type=["zip"])
    tapak_info = st.empty()
    tapak_info_detail = st.container()  # ← baris luas UTM & Mercator Tapak


st.markdown("---")

# ------------------
# PROCESS PKKPR
# ------------------
if uploaded:
    if uploaded.name.lower().endswith(".pdf"):
        results = extract_tables_and_coords_from_pdf(uploaded)

        total_luas_ha = 0
        for r in results:
            coords = r["coords"].copy()
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            try:
                poly = Polygon(coords)
                gdf_tmp = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                centroid = poly.centroid
                utm_epsg, _ = get_utm_info(centroid.x, centroid.y)
                luas_ha = (gdf_tmp.to_crs(utm_epsg).area.iloc[0] / 10000)
                r["luas_ha"] = luas_ha
                total_luas_ha += luas_ha
            except:
                r["luas_ha"] = 0

        # Hitung luas total dengan dua proyeksi
        try:
            _all_polys = [make_valid(Polygon(r["coords"] if r["coords"][0] == r["coords"][-1] else r["coords"] + [r["coords"][0]])) for r in results if len(r.get("coords", [])) >= 3]
            _gdf_all = gpd.GeoDataFrame(geometry=_all_polys, crs="EPSG:4326")
            _c_all = _gdf_all.geometry.unary_union.centroid
            _epsg_all, _zone_all = get_utm_info(_c_all.x, _c_all.y)
            _luas_utm_all = _gdf_all.to_crs(_epsg_all).area.sum()
            _luas_merc_all = _gdf_all.to_crs(3857).area.sum()
            pkkpr_luas_box.success(f"Jumlah PKKPR unik : {len(results)}")
            info_box_detail.caption(f"UTM {_zone_all} : {format_angka_id(_luas_utm_all)} m² / **{format_angka_id(_luas_utm_all/10000)} Ha**")
            info_box_detail.caption(f"Mercator : {format_angka_id(_luas_merc_all)} m² / **{format_angka_id(_luas_merc_all/10000)} Ha**")
        except:
            pkkpr_luas_box.success(
                f"Jumlah PKKPR unik : {len(results)} | "
                f"Total luas PKKPR : {format_angka_id(total_luas_ha)} Ha"
            )

        total_polygons = []
        for r in results:
            c = r["coords"].copy()
            if c[0] != c[-1]:
                c.append(c[0])
            try:
                poly = make_valid(Polygon(c))
                if not poly.is_empty and poly.geom_type in ["Polygon", "MultiPolygon"]:
                    total_polygons.append(poly)
            except:
                pass

        unique_points = set()
        for r in results:
            for x, y in r["coords"]:
                unique_points.add((round(x, 8), round(y, 8)))

        gdf_points_total = gpd.GeoDataFrame(
            geometry=[Point(x, y) for x, y in unique_points],
            crs="EPSG:4326"
        )

        if len(results) > 0:
            opsi = ["PKKPR TOTAL"] + list(range(len(results)))
            pilihan = st.selectbox(
                "Pilih PKKPR",
                opsi,
                format_func=lambda x: "PKKPR TOTAL" if x == "PKKPR TOTAL" else f"{results[x]['nama']} | {results[x]['luas_ha']:.2f} Ha"
            )

            if pilihan == "PKKPR TOTAL":
                gdf_polygon = gpd.GeoDataFrame(geometry=total_polygons, crs="EPSG:4326")
                coord_type = "WGS84"
                gdf_points = gdf_points_total
            else:
                coords = results[pilihan]["coords"]
                coord_type = results[pilihan]["coord_type"]
        else:
            st.error("Koordinat PDF tidak ditemukan")

        if pilihan != "PKKPR TOTAL":
            source_crs = "EPSG:4326"
            gdf_points = gpd.GeoDataFrame(
                {"No": list(range(1, len(coords) + 1))},
                geometry=[Point(x, y) for x, y in coords],
                crs=source_crs
            )
            coords_proc = coords.copy()
            if coords_proc[0] != coords_proc[-1]:
                coords_proc.append(coords_proc[0])

            try:
                from shapely.validation import make_valid
                poly_candidate = make_valid(Polygon(coords_proc))

                if DEBUG:
                    st.write("Geom Type :", poly_candidate.geom_type)
                    st.write("Valid :", poly_candidate.is_valid)
                    st.write("Empty :", poly_candidate.is_empty)

                try:
                    _c_sel = poly_candidate.centroid
                    _epsg_sel, _zone_sel = get_utm_info(_c_sel.x, _c_sel.y)
                    _gdf_sel = gpd.GeoDataFrame(geometry=[poly_candidate], crs="EPSG:4326")
                    _luas_utm_sel = _gdf_sel.to_crs(_epsg_sel).area.sum()
                    _luas_merc_sel = _gdf_sel.to_crs(3857).area.sum()
                    info_box.success(f"Jenis koordinat : {coord_type} | Valid : {'Ya' if poly_candidate.is_valid else 'Tidak'}")
                    info_box_detail.caption(f"UTM {_zone_sel} : {format_angka_id(_luas_utm_sel)} m² / **{format_angka_id(_luas_utm_sel/10000)} Ha**")
                    info_box_detail.caption(f"Mercator : {format_angka_id(_luas_merc_sel)} m² / **{format_angka_id(_luas_merc_sel/10000)} Ha**")
                except:
                    info_box.success(
                        f"Jenis koordinat : {coord_type} | "
                        f"Polygon valid : {'Ya' if poly_candidate.is_valid else 'Tidak'}"
                    )

                if not poly_candidate.is_valid:
                    try:
                        from shapely.validation import explain_validity
                        st.warning(f"Polygon invalid : {explain_validity(poly_candidate)}")
                    except:
                        pass

                gdf_polygon = gpd.GeoDataFrame(geometry=[poly_candidate], crs=source_crs)

            except Exception as e:
                st.error(f"Gagal membuat polygon : {e}")
                gdf_polygon = None

    elif uploaded.name.lower().endswith(".zip"):
        gdf_polygon = read_shp_zip(uploaded)
        if gdf_polygon is not None:
            if DEBUG:
                st.write("CRS :", gdf_polygon.crs)
            try:
                _c = gdf_polygon.to_crs(4326).geometry.centroid.iloc[0]
                _epsg, _zone = get_utm_info(_c.x, _c.y)
                _luas_utm = gdf_polygon.to_crs(_epsg).area.sum()
                _luas_merc = gdf_polygon.to_crs(3857).area.sum()
                info_box.success("SHP PKKPR berhasil dibaca")
                info_box_detail.caption(f"UTM {_zone} : {format_angka_id(_luas_utm)} m² / **{format_angka_id(_luas_utm/10000)} Ha**")
                info_box_detail.caption(f"Mercator : {format_angka_id(_luas_merc)} m² / **{format_angka_id(_luas_merc/10000)} Ha**")
            except:
                info_box.success("SHP PKKPR berhasil dibaca")
            show_attributes(gdf_polygon, "Atribut SHP PKKPR")

# ------------------
# TAPAK
# ------------------
if uploaded_tapak and gdf_polygon is not None:
    gdf_tapak = read_shp_zip(uploaded_tapak)
    if gdf_tapak is not None:
        gdf_tapak = fix_geometry(gdf_tapak)
        try:
            _c = gdf_tapak.to_crs(4326).geometry.centroid.iloc[0]
            _epsg, _zone = get_utm_info(_c.x, _c.y)
            _luas_utm_t = gdf_tapak.to_crs(_epsg).area.sum()
            _luas_merc_t = gdf_tapak.to_crs(3857).area.sum()
            tapak_info.success("SHP Tapak berhasil dibaca")
            tapak_info_detail.caption(f"UTM {_zone} : {format_angka_id(_luas_utm_t)} m² / **{format_angka_id(_luas_utm_t/10000)} Ha**")
            tapak_info_detail.caption(f"Mercator : {format_angka_id(_luas_merc_t)} m² / **{format_angka_id(_luas_merc_t/10000)} Ha**")
        except:
            tapak_info.success("SHP Tapak berhasil dibaca")
        show_attributes(gdf_tapak, "Atribut SHP Tapak")

# =========================================================
# ANALISIS OVERLAY
# =========================================================
if gdf_polygon is not None and coord_type == "WGS84" and gdf_tapak is not None:
    st.subheader("Analisis Overlay")
    centroid = gdf_polygon.to_crs(4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)

    gdf_poly_utm = gdf_polygon.to_crs(utm_epsg)
    gdf_tapak_utm = gdf_tapak.to_crs(utm_epsg)

    inter = gpd.overlay(gdf_tapak_utm, gdf_poly_utm, how="intersection")

    luas_overlap = inter.area.sum()
    luas_tapak  = gdf_tapak_utm.area.sum()
    luas_luar = max(0, luas_tapak - luas_overlap)

    col_a, col_b, col_c = st.columns(3)
    col_a.metric(f"Luas Tapak (UTM {utm_zone})", f"{format_angka_id(luas_tapak/10000)} Ha", f"{format_angka_id(luas_tapak)} m²")
    col_b.metric("Luas Overlay", f"{format_angka_id(luas_overlap/10000)} Ha", f"{format_angka_id(luas_overlap)} m²")
    col_c.metric("Luas di luar PKKPR", f"{format_angka_id(luas_luar/10000)} Ha", f"{format_angka_id(luas_luar)} m²")

    st.markdown("---")

# =========================================================
# PETA (zoom to layer via fit_bounds)
# =========================================================
if gdf_polygon is not None and coord_type == "WGS84":
    st.subheader("Peta")

    if gdf_tapak is not None:
        combined_preview = pd.concat(
            [gdf_polygon.to_crs(4326), gdf_tapak.to_crs(4326)],
            ignore_index=True
        )
    else:
        combined_preview = gdf_polygon.to_crs(4326)

    bounds = combined_preview.total_bounds  # [minx, miny, maxx, maxy]
    centroid = combined_preview.geometry.unary_union.centroid

    # Key unik berdasarkan bounds — paksa st_folium re-render saat data berubah
    map_key = f"map_{bounds[0]:.6f}_{bounds[1]:.6f}_{bounds[2]:.6f}_{bounds[3]:.6f}"

    m = folium.Map(
        location=[centroid.y, centroid.x],
        zoom_start=14,
        tiles=None,
        zoom_control=True,
    )
    Fullscreen().add_to(m)
    folium.TileLayer(xyz.Esri.WorldImagery, name="Esri Satellite").add_to(m)

    folium.GeoJson(
        gdf_polygon.to_crs(4326),
        name="PKKPR",
        style_function=lambda x: {
            "color": "yellow",
            "weight": 3,
            "fillOpacity": 0.1
        }
    ).add_to(m)

    if gdf_tapak is not None:
        folium.GeoJson(
            gdf_tapak.to_crs(4326),
            name="Tapak",
            style_function=lambda x: {
                "color": "red",
                "fillColor": "red",
                "weight": 2,
                "fillOpacity": 0.35
            }
        ).add_to(m)

    if gdf_points is not None and not gdf_points.empty:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=4,
                color="black",
                fill=True,
                fill_color="orange",
                fill_opacity=1,
                popup=f"Titik {i+1}"
            ).add_to(m)

    # Zoom to layer — fit_bounds ke extent semua layer
    m.fit_bounds([
        [bounds[1], bounds[0]],
        [bounds[3], bounds[2]]
    ])

    folium.LayerControl().add_to(m)
    st_folium(m, width="100%", height=650, key=map_key, returned_objects=[])

    st.markdown("---")

    # =========================================================
    # EXPORT
    # =========================================================
    st.subheader("Export")
    col_export1, col_export2 = st.columns(2)

    with col_export1:
        st.write("**SHP PKKPR**")
        geom = gdf_polygon.to_crs(4326).geometry.iloc[0]
        if geom is not None and not geom.is_empty:
            zip_bytes = save_shapefile_layers(gdf_polygon, gdf_points)
            st.download_button(
                "⬇️ Download SHP PKKPR",
                zip_bytes,
                "PKKPR_Hasil.zip",
                mime="application/zip"
            )

    with col_export2:
        st.write("**Peta PNG**")
        try:
            gdf_poly_3857 = gdf_polygon.to_crs(3857).copy()
            gdf_poly_3857["geometry"] = gdf_poly_3857.geometry.buffer(0)

            if gdf_tapak is not None:
                gdf_tapak_3857 = gdf_tapak.to_crs(3857).copy()
                gdf_tapak_3857["geometry"] = gdf_tapak_3857.geometry.buffer(0)
                extent_gdf = pd.concat([gdf_poly_3857, gdf_tapak_3857], ignore_index=True)
            else:
                gdf_tapak_3857 = None
                extent_gdf = gdf_poly_3857

            xmin, ymin, xmax, ymax = extent_gdf.total_bounds
            width = xmax - xmin
            height = ymax - ymin
            padx = max(width * 0.20, 100)
            pady = max(height * 0.20, 100)

            x0 = xmin - padx
            x1 = xmax + padx
            y0 = ymin - pady
            y1 = ymax + pady

            fig, ax = plt.subplots(figsize=(10, 10), dpi=150)

            # 1. Set extent sebelum basemap
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)

            # 2. Basemap dengan reset_extent=False agar extent tidak berubah
            basemap_ok = False
            for source in [ctx.providers.Esri.WorldImagery, ctx.providers.OpenStreetMap.Mapnik]:
                try:
                    ctx.add_basemap(ax, source=source, crs="EPSG:3857", reset_extent=False)
                    basemap_ok = True
                    break
                except Exception:
                    continue
            if not basemap_ok:
                ax.set_facecolor("#c9e8f5")

            # 3. Plot vektor di atas basemap
            if gdf_tapak_3857 is not None:
                gdf_tapak_3857.plot(ax=ax, facecolor="red", edgecolor="red", alpha=0.35, linewidth=1.5, zorder=5)

            gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2, zorder=4)

            if gdf_points is not None and not gdf_points.empty:
                gdf_points_3857 = gdf_points.to_crs(3857)
                gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=30, zorder=6)

            # 4. Paksa extent kembali ke nilai awal (plot() bisa menggeser)
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
            ax.set_aspect("equal")
            ax.axis("off")
            ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=12, pad=10)

            legend_elements = [
                mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black", linestyle="None", markersize=8, label="Titik PKKPR"),
                mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=2, label="PKKPR"),
                mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak")
            ]

            poly_centroid = gdf_poly_3857.unary_union.centroid
            corners = {
                "upper left":  (xmin, ymax),
                "upper right": (xmax, ymax),
                "lower left":  (xmin, ymin),
                "lower right": (xmax, ymin)
            }
            max_dist = -1
            best_corner = "upper right"
            for loc, (x, y) in corners.items():
                dist = ((poly_centroid.x - x) ** 2 + (poly_centroid.y - y) ** 2)
                if dist > max_dist:
                    max_dist = dist
                    best_corner = loc

            ax.legend(handles=legend_elements, loc=best_corner, frameon=True,
                      facecolor="white", framealpha=0.9, edgecolor="black", fontsize=9)

            buf = io.BytesIO()
            plt.savefig(buf, format="png", bbox_inches="tight", dpi=150)
            buf.seek(0)
            png_bytes = buf.getvalue()
            plt.close(fig)

            st.download_button(
                "⬇️ Download Peta PNG",
                data=png_bytes,
                file_name="Peta_Overlay.png",
                mime="image/png"
            )

        except Exception as e:
            st.error(f"Gagal membuat PNG: {e}")

else:
    if not uploaded:
        st.info("💡 Silakan upload dokumen PKKPR untuk memulai.")

# =========================================================
# END
# =========================================================
st.markdown("---")
st.caption("PKKPR Overlay Analyzer Ready")
