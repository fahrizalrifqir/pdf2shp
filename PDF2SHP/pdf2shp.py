# app.py
import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, shutil, re, tempfile, math
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
# App config
# ======================
st.set_page_config(page_title="PKKPR → SHP + Overlay", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")
DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

# Constants
PURWAKARTA_CENTER = (107.44, -6.56)
INDO_BOUNDS = (95.0, 141.0, -11.0, 6.0)  # lon_min, lon_max, lat_min, lat_max

# ----------------------
# Helper functions
# ----------------------
def get_utm_info(lon, lat):
    zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        epsg = 32600 + zone
        zone_label = f"{zone}N"
    else:
        epsg = 32700 + zone
        zone_label = f"{zone}S"
    return epsg, zone_label

def format_angka_id(value):
    try:
        val = float(value)
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")
        else:
            return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(value)

def save_shapefile(gdf_polygon, gdf_points=None):
    """Simpan dua shapefile (polygon & points) ke ZIP dan kembalikan bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        files_written = []
        # polygon
        if gdf_polygon is not None and not gdf_polygon.empty:
            poly_path = os.path.join(tmp, "PKKPR_Polygon.shp")
            gdf_polygon.to_crs(epsg=4326).to_file(poly_path)
            files_written += [os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("PKKPR_Polygon")]
        # points
        if gdf_points is not None and not gdf_points.empty:
            point_path = os.path.join(tmp, "PKKPR_Points.shp")
            gdf_points.to_crs(epsg=4326).to_file(point_path)
            files_written += [os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("PKKPR_Points")]
        if not files_written:
            raise ValueError("Tidak ada geometri valid untuk disimpan.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files_written:
                zf.write(f, arcname=os.path.basename(f))
        buf.seek(0)
        return buf.read()

# Geometry fixers
def fix_polygon_geometry(gdf):
    if gdf is None or len(gdf) == 0:
        return gdf
    g = gdf.copy()
    try:
        g["geometry"] = g["geometry"].apply(lambda geom: make_valid(geom))
    except Exception:
        pass
    try:
        b = g.total_bounds
    except Exception:
        return g
    if (-180 <= b[0] <= 180) and (-90 <= b[1] <= 90):
        try:
            return g.set_crs(epsg=4326, allow_override=True)
        except:
            return g
    for fac in [10, 100, 1000, 10000, 100000]:
        try:
            g2 = g.copy()
            g2["geometry"] = g2["geometry"].apply(lambda geom: affinity.scale(geom, xfact=1/fac, yfact=1/fac, origin=(0, 0)))
            b2 = g2.total_bounds
            if (95 <= b2[0] <= 145) and (-11 <= b2[1] <= 6):
                return g2.set_crs(epsg=4326, allow_override=True)
        except Exception:
            continue
    return g

def auto_fix_to_polygon(coords):
    if not coords or len(coords) < 3:
        return None
    unique = []
    for c in coords:
        if not unique or c != unique[-1]:
            unique.append(c)
    if unique[0] != unique[-1]:
        unique.append(unique[0])
    try:
        poly = Polygon(unique)
        if not poly.is_valid or poly.area == 0:
            pts = gpd.GeoSeries([Point(x, y) for x, y in unique], crs="EPSG:4326")
            poly = pts.unary_union.convex_hull
        return poly
    except Exception:
        return None

# Coordinate parsing helpers
def normalize_text(s):
    if not s:
        return ""
    s = str(s)
    s = s.replace('\u2019', "'").replace('\u201d', '"').replace('\u201c', '"')
    s = s.replace('’', "'").replace('“', '"').replace('”', '"')
    s = s.replace('\xa0', ' ')
    return s

def extract_coords_from_line_pair(line):
    """
    Try to parse a line containing two numbers (lon lat) separated by spaces/tabs/comma.
    Returns tuple (lon, lat) if in Indonesia ranges, else None.
    """
    # First try patterns with optional leading index
    patterns = [
        r"^\s*\d+\s+([0-9\.\-]+)\s+([0-9\.\-]+)",  # "1 107.57 -7.04"
        r"^\s*([0-9\.\-]+)[\s,;]+([0-9\.\-]+)\s*$",  # "107.57, -7.04" or "107.57 -7.04"
    ]
    for p in patterns:
        m = re.match(p, line)
        if m:
            try:
                a = float(m.group(1))
                b = float(m.group(2))
                # decide if (lon,lat) or swapped
                if 95 <= a <= 141 and -11 <= b <= 6:
                    return (a, b)
                if 95 <= b <= 141 and -11 <= a <= 6:
                    return (b, a)
            except:
                return None
    return None

# Projected detection (UTM) - test zones 46-50S and XY/YX
def in_indonesia(lon, lat):
    lon_min, lon_max, lat_min, lat_max = INDO_BOUNDS
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max

def dist_to_purwakarta(lon, lat):
    return math.hypot(lon - PURWAKARTA_CENTER[0], lat - PURWAKARTA_CENTER[1])

def try_zones_orders(easting, northing, zones=(46,47,48,49,50), prioritize_epsg=32748):
    candidates = []
    for zone in zones:
        epsg = 32700 + zone
        try:
            transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        except Exception:
            continue
        # XY
        try:
            lon_xy, lat_xy = transformer.transform(easting, northing)
            if in_indonesia(lon_xy, lat_xy):
                candidates.append({"epsg": epsg, "order": "xy", "lon": lon_xy, "lat": lat_xy, "dist": dist_to_purwakarta(lon_xy, lat_xy)})
        except Exception:
            pass
        # YX
        try:
            lon_yx, lat_yx = transformer.transform(northing, easting)
            if in_indonesia(lon_yx, lat_yx):
                candidates.append({"epsg": epsg, "order": "yx", "lon": lon_yx, "lat": lat_yx, "dist": dist_to_purwakarta(lon_yx, lat_yx)})
        except Exception:
            pass
    candidates_sorted = sorted(candidates, key=lambda c: (0 if c["epsg"] == prioritize_epsg else 1, c["dist"]))
    return candidates_sorted

def detect_projected_pairs_with_priority(pairs, zones=(46,47,48,49,50), prioritize_epsg=32748):
    if not pairs:
        return None, None, None
    a_med, b_med = pairs[len(pairs)//2]
    cand = try_zones_orders(a_med, b_med, zones=zones, prioritize_epsg=prioritize_epsg)
    if not cand:
        # try swapped
        cand = try_zones_orders(b_med, a_med, zones=zones, prioritize_epsg=prioritize_epsg)
        if not cand:
            return None, None, None
    chosen = cand[0]
    chosen_epsg = chosen["epsg"]
    chosen_order = chosen["order"]
    transformer = Transformer.from_crs(f"EPSG:{chosen_epsg}", "EPSG:4326", always_xy=True)
    transformed = []
    for a, b in pairs:
        if chosen_order == "xy":
            lon, lat = transformer.transform(a, b)
        else:
            lon, lat = transformer.transform(b, a)
        transformed.append((lon, lat))
    return transformed, chosen_epsg, chosen_order

# --------------------------
# Robust luas extraction
# --------------------------
def extract_luas_from_text_pages(pages):
    """
    Cari pola luas pada teks PDF. Mengembalikan tuple (value_in_m2, unit_str, raw_match).
    - mendeteksi m2, m², 'm 2' (with space), ha, hektar
    - jika unit = ha -> dikalikan 10000
    - jika unit tidak ada, coba lihat kata kunci 'ha'/'hektar' dekat match
    """
    patterns = [
        # capture number then optional space then unit (m2, m², m 2, ha)
        r"([\d\.\,]+)\s*(m2|m²|m\s*2|hektar|ha)\b",
        # sometimes phrased: "Luas: 2 m 2" or "Luas: 0,25 ha"
        r"(luas[^\d]{0,10})([\d\.\,]+)\s*(m2|m²|m\s*2|hektar|ha)\b",
        # fallback number-only lines that mention 'luas' near them
        r"luas[^\d]{0,10}([\d\.\,]+)\b"
    ]
    joined = "\n".join([p for p in pages if p])
    joined = normalize_text(joined)
    # search with context lines for fallback decision
    lines = joined.splitlines()
    best = None  # (value_m2, unit, raw)
    for i, line in enumerate(lines):
        low = line.lower()
        for pat in patterns:
            for m in re.finditer(pat, low, flags=re.IGNORECASE):
                # m may capture different groups depending on pattern; get last two groups that look like number+unit
                groups = m.groups()
                # find number and unit in groups
                number = None
                unit = None
                for g in groups[::-1]:
                    if g is None:
                        continue
                    g = str(g).strip()
                    if re.match(r"^[\d\.\,]+$", g):
                        number = g
                        continue
                    if re.match(r"^(m2|m²|m\s*2|hektar|ha)$", g):
                        unit = g
                        continue
                # fallback: if groups length >=2 and first is number then last unit
                if number is None:
                    # try to extract number from match text
                    mm = re.search(r"([\d\.\,]+)", m.group(0))
                    if mm:
                        number = mm.group(1)
                # clean number
                if not number:
                    continue
                num_s = number.replace(".", "").replace(",", ".") if ("," in number and "." in number) else number.replace(",", ".")
                try:
                    val = float(num_s)
                except:
                    continue
                unit_norm = None
                if unit:
                    unit_norm = unit.replace(" ", "")
                # if unit absent, look around nearby lines for 'ha' or 'hektar'
                if not unit_norm:
                    context = " ".join(lines[max(0, i-2):min(len(lines), i+3)]).lower()
                    if "hektar" in context or "ha" in context:
                        unit_norm = "ha"
                # convert to m2
                if unit_norm and unit_norm.startswith("ha"):
                    val_m2 = val * 10000.0
                else:
                    # m2 or unspecified -> assume m2
                    val_m2 = val
                # choose largest plausible area (most documents state full area as a large number)
                if best is None or val_m2 > best[0]:
                    best = (val_m2, unit_norm if unit_norm else "m2", m.group(0).strip())
    return best  # (value_m2, unit_str, raw) or None

# --------------------------
# Parsing PDF with hierarchy
# --------------------------
def extract_tables_and_coords_from_pdf(uploaded_file):
    """
    Parse PDF pages:
    - extract tables (table rows) when available,
    - extract plain-text lines and try to parse coordinate pairs,
    - maintain three buckets by table_mode: disetujui, dimohon, plain (no label)
    - also extract luas_disetujui and luas_dimohon if found
    """
    coords_disetujui = []
    coords_dimohon = []
    coords_plain = []
    luas_disetujui = None
    luas_dimohon = None
    table_mode = None  # current context: "disetujui", "dimohon", or None

    pages_texts = []
    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            pages_texts.append(page.extract_text() or "")
            # extract table if present
            table = page.extract_table()
            if table:
                # detect header row to map columns if possible (e.g., "No", "Bujur", "Lintang")
                header = None
                if len(table) and any(cell and re.search(r"bujur|bujur", str(cell), flags=re.IGNORECASE) for cell in table[0]):
                    header = [str(c).strip().lower() if c else "" for c in table[0]]
                    data_rows = table[1:]
                else:
                    # Heuristic: if first row contains non-numeric strings, treat as header
                    first = table[0]
                    if any(re.search(r"[A-Za-z]", str(c) or "") for c in first):
                        header = [str(c).strip().lower() if c else "" for c in first]
                        data_rows = table[1:]
                    else:
                        data_rows = table
                # parse rows
                for row in data_rows:
                    if not row:
                        continue
                    # try to find numeric cells
                    nums = []
                    for cell in row:
                        if cell is None:
                            continue
                        cell_s = str(cell).strip()
                        # remove extraneous characters
                        m = re.search(r"(-?\d{1,13}[\,\.\d]*)", cell_s)
                        if m:
                            s = m.group(1).replace(",", ".")
                            try:
                                f = float(s)
                                nums.append(f)
                            except:
                                continue
                    if len(nums) >= 2:
                        # If header indicates 'bujur' first then 'lintang', use that mapping.
                        if header:
                            # find columns indices for bujur/lintang
                            try:
                                idx_bujur = next(i for i,v in enumerate(header) if "bujur" in v)
                                idx_lintang = next(i for i,v in enumerate(header) if "lintang" in v)
                                # ensure indices in range
                                if idx_bujur < len(row) and idx_lintang < len(row):
                                    try:
                                        lon_raw = re.search(r"(-?\d{1,13}[\,\.\d]*)", str(row[idx_bujur])).group(1).replace(",", ".")
                                        lat_raw = re.search(r"(-?\d{1,13}[\,\.\d]*)", str(row[idx_lintang])).group(1).replace(",", ".")
                                        lon = float(lon_raw); lat = float(lat_raw)
                                    except:
                                        lon, lat = nums[0], nums[1]
                                else:
                                    lon, lat = nums[0], nums[1]
                            except StopIteration:
                                lon, lat = nums[0], nums[1]
                        else:
                            lon, lat = nums[0], nums[1]
                        # append to bucket
                        if table_mode == "disetujui":
                            coords_disetujui.append((lon, lat))
                        elif table_mode == "dimohon":
                            coords_dimohon.append((lon, lat))
                        else:
                            coords_plain.append((lon, lat))
            # extract text lines
            text = (page.extract_text() or "")
            for line in text.splitlines():
                low = line.lower().strip()
                # detect mode switches based on headings
                if "koordinat" in low and "disetujui" in low:
                    table_mode = "disetujui"
                    continue
                elif "koordinat" in low and "dimohon" in low:
                    table_mode = "dimohon"
                    continue
                elif "koordinat" in low and ("tabel" in low or "daftar" in low or "tanpa" in low):
                    table_mode = None
                    continue
                # extract luas lines
                if "luas tanah yang disetujui" in low and luas_disetujui is None:
                    lu = parse_luas(line)
                    if lu is not None:
                        luas_disetujui = lu
                elif "luas tanah yang dimohon" in low and luas_dimohon is None:
                    lu = parse_luas(line)
                    if lu is not None:
                        luas_dimohon = lu
                # parse coordinate pairs from free text lines
                parsed = extract_coords_from_line_pair(line)
                if parsed:
                    lon, lat = parsed
                    if table_mode == "disetujui":
                        coords_disetujui.append((lon, lat))
                    elif table_mode == "dimohon":
                        coords_dimohon.append((lon, lat))
                    else:
                        coords_plain.append((lon, lat))
    # extract luas with better pattern (catch units)
    luas_best = extract_luas_from_text_pages(pages_texts)
    # If luas_best exists and indicates 'disetujui' or 'dimohon' already parsed, keep parsed ones as priority
    if luas_best:
        # luas_best is tuple (value_m2, unit_str, raw)
        # We won't override luas_disetujui/dimohon if already parsed explicitly as "luas tanah yang disetujui/dimohon"
        if luas_disetujui is None and luas_dimohon is None:
            # assign to general fallback (we will show it later)
            # store as luas_disetujui for convenience (label will be 'detected')
            luas_disetujui = luas_best[0]
    return {
        "disetujui": coords_disetujui,
        "dimohon": coords_dimohon,
        "plain": coords_plain,
        "luas_disetujui": luas_disetujui,
        "luas_dimohon": luas_dimohon,
        "luas_detected_raw": luas_best[2] if luas_best else None,
        "luas_detected_unit": luas_best[1] if luas_best else None
    }

# -------------------------
# UI: Upload PKKPR
# -------------------------
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])
# EPSG override
epsg_override_input = st.sidebar.text_input("Override EPSG (mis. 32748) — kosong = auto-detect", value="")

coords = []
gdf_points = None
gdf_polygon = None
luas_pkkpr_doc = None
luas_pkkpr_doc_label = None
detected_info = {}

if uploaded_pkkpr:
    if uploaded_pkkpr.name.lower().endswith(".pdf"):
        try:
            parsed = extract_tables_and_coords_from_pdf(uploaded_pkkpr)
            coords_disetujui = parsed["disetujui"]
            coords_dimohon = parsed["dimohon"]
            coords_plain = parsed["plain"]
            luas_disetujui = parsed["luas_disetujui"]
            luas_dimohon = parsed["luas_dimohon"]
            luas_detected_raw = parsed.get("luas_detected_raw")
            luas_detected_unit = parsed.get("luas_detected_unit")
            # choose based on hierarchy
            if coords_disetujui:
                coords_selected = coords_disetujui
                luas_pkkpr_doc = luas_disetujui or luas_dimohon
                luas_pkkpr_doc_label = "disetujui"
            elif coords_dimohon:
                coords_selected = coords_dimohon
                luas_pkkpr_doc = luas_dimohon
                luas_pkkpr_doc_label = "dimohon"
            else:
                coords_selected = coords_plain
                luas_pkkpr_doc = None
                luas_pkkpr_doc_label = "plain"
            # If no explicit luas from lines but luas_detected_raw exists, show it as detected
            if luas_pkkpr_doc is None and luas_detected_raw is not None:
                luas_pkkpr_doc = parsed["luas_disetujui"]  # earlier we set this
                luas_pkkpr_doc_label = "detected"
            # show luas dokumen if available
            if luas_pkkpr_doc:
                st.info(f"📏 Luas PKKPR (dari dokumen, {luas_pkkpr_doc_label}): **{format_angka_id(luas_pkkpr_doc)} m²**")
            elif luas_detected_raw:
                # as fallback show what we detected
                st.info(f"📏 Luas PKKPR (terdeteksi mentah): {luas_detected_raw} (unit: {luas_detected_unit})")
            # Determine whether coords are projected (UTM-like) or geographic:
            projected_pairs = []
            geographic_pairs = []
            for a, b in coords_selected:
                # if numbers are clearly formatted with comma as decimal -> convert earlier
                # here assume a,b already floats
                if abs(a) > 1000 or abs(b) > 1000:
                    projected_pairs.append((a, b))
                else:
                    geographic_pairs.append((a, b))
            # If projected_pairs sufficient -> try detect UTM (46-50S) with override support
            if len(projected_pairs) >= max(3, len(geographic_pairs)):
                epsg_override = int(epsg_override_input) if epsg_override_input.strip().isdigit() else None
                transformed = None
                chosen_epsg = None
                chosen_order = None
                if epsg_override:
                    try:
                        transformer = Transformer.from_crs(f"EPSG:{epsg_override}", "EPSG:4326", always_xy=True)
                        sample = projected_pairs[len(projected_pairs)//2]
                        # try XY
                        try:
                            lon_xy, lat_xy = transformer.transform(sample[0], sample[1])
                            if in_indonesia(lon_xy, lat_xy):
                                chosen_epsg = epsg_override
                                chosen_order = "xy"
                        except:
                            pass
                        if chosen_epsg is None:
                            # try YX
                            try:
                                lon_yx, lat_yx = transformer.transform(sample[1], sample[0])
                                if in_indonesia(lon_yx, lat_yx):
                                    chosen_epsg = epsg_override
                                    chosen_order = "yx"
                            except:
                                pass
                        if chosen_epsg:
                            t = Transformer.from_crs(f"EPSG:{chosen_epsg}", "EPSG:4326", always_xy=True)
                            transformed = []
                            for a,b in projected_pairs:
                                if chosen_order == "xy":
                                    lon, lat = t.transform(a,b)
                                else:
                                    lon, lat = t.transform(b,a)
                                transformed.append((lon, lat))
                    except Exception:
                        transformed = None
                if transformed is None:
                    transformed, chosen_epsg, chosen_order = detect_projected_pairs_with_priority(projected_pairs, zones=(46,47,48,49,50), prioritize_epsg=32748)
                if transformed is None:
                    st.error("Gagal mendeteksi zona/proyeksi untuk koordinat metrik. Coba masukan Override EPSG di sidebar (mis. 32748).")
                else:
                    coords = transformed
                    detected_info = {"mode": "projected", "epsg": chosen_epsg, "order": chosen_order, "n_points": len(coords)}
            else:
                # use geographic pairs
                coords = geographic_pairs
                detected_info = {"mode": "geographic", "n_points": len(coords)}
            # Build geodataframes
            if coords:
                # ensure closed polygon
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                gdf_points = gpd.GeoDataFrame(pd.DataFrame(coords, columns=["Lon", "Lat"]),
                                              geometry=[Point(x, y) for x, y in coords], crs="EPSG:4326")
                poly = auto_fix_to_polygon(coords)
                if poly is not None:
                    gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                    gdf_polygon = fix_polygon_geometry(gdf_polygon)
                else:
                    st.warning("Koordinat terbaca namun gagal membentuk polygon valid — coba periksa file atau gunakan override EPSG.")
            else:
                st.error("Tidak ada koordinat terpilih dari dokumen (periksa tabel).")
        except Exception as e:
            st.error(f"Gagal memproses PDF: {e}")
            if DEBUG:
                st.exception(e)

    elif uploaded_pkkpr.name.lower().endswith(".zip"):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                zf = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()))
                zf.extractall(tmp)
                gdf_polygon = gpd.read_file(tmp)
                if gdf_polygon.crs is None:
                    gdf_polygon.set_crs(epsg=4326, inplace=True)
                gdf_polygon = fix_polygon_geometry(gdf_polygon)
                st.success("Shapefile PKKPR terbaca dari ZIP.")
        except Exception as e:
            st.error(f"Gagal membaca shapefile PKKPR: {e}")
            if DEBUG:
                st.exception(e)

# Show detected info in sidebar
if detected_info:
    st.sidebar.markdown("### Hasil Deteksi Koordinat")
    for k, v in detected_info.items():
        st.sidebar.write(f"- **{k}**: {v}")

# -------------------------
# Export SHP (Polygon + Points)
# -------------------------
if gdf_polygon is not None:
    try:
        # explode multipolygons and keep polygons only for export
        g_export = gdf_polygon.copy()
        try:
            g_export = g_export.explode(index_parts=False).reset_index(drop=True)
        except Exception:
            pass
        g_export = g_export[g_export.geometry.type.isin(["Polygon", "MultiPolygon"])]
        if g_export.empty:
            raise ValueError("Tidak ada geometri polygon valid untuk disimpan.")
        zip_bytes = save_shapefile(g_export, gdf_points)
        st.download_button("⬇️ Download SHP PKKPR (ZIP)", zip_bytes, "PKKPR_Hasil_Konversi.zip", mime="application/zip")
    except Exception as e:
        st.error(f"Gagal menyiapkan shapefile: {e}")
        if DEBUG:
            st.exception(e)

    # Luas dari geometri
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
col1, col2 = st.columns([0.7, 0.3])
uploaded_tapak = col1.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"], key='tapak')
gdf_tapak = None
if uploaded_tapak:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zf = zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()))
            zf.extractall(tmp)
            gdf_tapak = gpd.read_file(tmp)
            if gdf_tapak.crs is None:
                gdf_tapak.set_crs(epsg=4326, inplace=True)
            st.success("Shapefile Tapak terbaca.")
    except Exception as e:
        st.error(f"Gagal membaca shapefile Tapak Proyek: {e}")
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
        inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how='intersection')
        luas_overlap = inter.area.sum() if not inter.empty else 0
        luas_tapak = gdf_tapak_utm.area.sum()
        luas_outside = luas_tapak - luas_overlap
        st.success(f"**HASIL OVERLAY TAPAK:**\n- Luas Tapak UTM {utm_zone}: **{format_angka_id(luas_tapak)} m²**\n- Luas Tapak di dalam PKKPR: **{format_angka_id(luas_overlap)} m²**\n- Luas Tapak Di luar PKKPR : **{format_angka_id(luas_outside)} m²**")
    except Exception as e:
        st.error(f"Gagal overlay: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# -------------------------
# Interactive map
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
        fig, ax = plt.subplots(figsize=(14, 10) if width > height else (10, 14), dpi=150)
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
