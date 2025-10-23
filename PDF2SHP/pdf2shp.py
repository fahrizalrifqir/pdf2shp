# app.py
import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, shutil, re, tempfile, math
from shapely.geometry import Point, Polygon, MultiPolygon, GeometryCollection
import folium
from streamlit_folium import st_folium
import pdfplumber
import matplotlib.pyplot as plt
import contextily as ctx
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
from folium.plugins import Fullscreen
import xyzservices.providers as xyz
from shapely.validation import make_valid
from shapely import affinity
from pyproj import Transformer

# ======================
# CONFIG
# ======================
st.set_page_config(page_title="PKKPR → SHP + Overlay (Final)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek (Final)")
st.markdown("---")
DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

# Constants
PURWAKARTA_CENTER = (107.44, -6.56)
INDO_BOUNDS = (95.0, 141.0, -11.0, 6.0)

# ======================
# HELPERS
# ======================
def normalize_text(s):
    if not s:
        return ""
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
    m = re.search(r"(luas[^\n\r]{0,60}?(:|–|-)?\s*)([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?)[\s\-–]*(" + unit_pattern + r")?", s, flags=re.IGNORECASE)
    if m:
        num = m.group(3)
        unit = (m.group(4) or "").strip()
        unit_up = unit.upper()
        if "HA" in unit_up:
            unit_disp = "Ha"
        elif "M2" in unit_up or "M²" in unit_up or unit_up == "M":
            unit_disp = "m²"
        elif unit:
            unit_disp = unit
        else:
            unit_disp = ""
        return f"{num} {unit_disp}".strip()
    m2 = re.search(r"([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?)[\s]*(" + unit_pattern + r")", s, flags=re.IGNORECASE)
    if m2:
        num = m2.group(1)
        unit = (m2.group(2) or "").strip()
        unit_up = unit.upper()
        if "HA" in unit_up:
            unit_disp = "Ha"
        else:
            unit_disp = "m²" if ("M2" in unit_up or "M²" in unit_up or unit_up == "M") else unit
        return f"{num} {unit_disp}".strip()
    m3 = re.search(r"([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?)", s)
    if m3:
        num = m3.group(1)
        return num
    return None

def save_shapefile_layers(gdf_poly, gdf_points):
    with tempfile.TemporaryDirectory() as tmpdir:
        files = []
        if gdf_poly is not None and not gdf_poly.empty:
            gdf_poly.to_crs(epsg=4326).to_file(os.path.join(tmpdir, "PKKPR_Polygon.shp"))
        if gdf_points is not None and not gdf_points.empty:
            gdf_points.to_crs(epsg=4326).to_file(os.path.join(tmpdir, "PKKPR_Points.shp"))
        for f in os.listdir(tmpdir):
            files.append(os.path.join(tmpdir, f))
        if not files:
            raise ValueError("Tidak ada geometri untuk disimpan.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in files:
                zf.write(p, arcname=os.path.basename(p))
        buf.seek(0)
        return buf.read()

# =====================================================
# FIX GEOMETRY — perbaikan shapefile GeometryCollection
# =====================================================
def fix_geometry(gdf):
    if gdf is None or gdf.empty:
        return gdf
    try:
        gdf["geometry"] = gdf["geometry"].apply(lambda geom: make_valid(geom))
    except Exception:
        pass

    def extract_valid(geom):
        if geom is None:
            return None
        if geom.geom_type == "GeometryCollection":
            polys = [g for g in geom.geoms if g.geom_type in ["Polygon", "MultiPolygon"]]
            if not polys:
                return None
            if len(polys) == 1:
                return polys[0]
            return MultiPolygon(polys)
        return geom

    gdf["geometry"] = gdf["geometry"].apply(extract_valid)

    try:
        b = gdf.total_bounds
    except Exception:
        return gdf
    if (-180 <= b[0] <= 180) and (-90 <= b[1] <= 90):
        try:
            return gdf.set_crs(epsg=4326, allow_override=True)
        except Exception:
            return gdf
    try:
        centroid = gdf.geometry.unary_union.centroid
    except Exception:
        centroid = None
    for fac in [10, 100, 1000, 10000, 100000]:
        try:
            g2 = gdf.copy()
            origin = (centroid.x, centroid.y) if centroid else (0, 0)
            g2["geometry"] = g2["geometry"].apply(lambda geom: affinity.scale(geom, xfact=1/fac, yfact=1/fac, origin=origin))
            b2 = g2.total_bounds
            if DEBUG:
                st.sidebar.write(f"DEBUG: try scale 1/{fac} -> bounds {b2}")
            if (95 <= b2[0] <= 145) and (-11 <= b2[1] <= 6):
                if DEBUG:
                    st.sidebar.write(f"DEBUG: Rescale berhasil dengan factor {fac}. New bounds: {b2}")
                return g2.set_crs(epsg=4326, allow_override=True)
        except Exception as e:
            if DEBUG:
                st.sidebar.write(f"DEBUG: rescale gagal untuk fac {fac}: {e}")
            continue
    return gdf

# =====================================================
# IMPROVED COORD PARSER (PDF OSS tolerant)
# =====================================================
def extract_coords_from_line_pair(line):
    """
    Parse koordinat dari baris PDF OSS — toleran terhadap spasi hilang antar angka.
    Contoh format yang didukung:
    '1 107.304212631806 -6.29747131047679'
    '1 107.304212631806-6.29747131047679'
    '107.304212631806 -6.29747131047679'
    '107.304212631806-6.29747131047679'
    """
    s = line.strip()
    s = re.sub(r"([0-9])(-\d)", r"\1 \2", s)  # tambahkan spasi sebelum minus kedua
    m = re.search(r"(-?\d+\.\d+)\s+(-?\d+\.\d+)", s)
    if not m:
        return None
    try:
        a = float(m.group(1))
        b = float(m.group(2))
    except:
        return None
    if 95 <= a <= 141 and -11 <= b <= 6:
        return (a, b)
    if 95 <= b <= 141 and -11 <= a <= 6:
        return (b, a)
    return None

def in_indonesia(lon, lat):
    lon_min, lon_max, lat_min, lat_max = INDO_BOUNDS
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max

def try_zones_orders(easting, northing, zones=(46,47,48,49,50), prioritize_epsg=32748):
    candidates = []
    for z in zones:
        epsg = 32700 + z
        try:
            transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        except Exception:
            continue
        try:
            lon_xy, lat_xy = transformer.transform(easting, northing)
            if in_indonesia(lon_xy, lat_xy):
                candidates.append({"epsg":epsg,"order":"xy","lon":lon_xy,"lat":lat_xy})
        except Exception:
            pass
        try:
            lon_yx, lat_yx = transformer.transform(northing, easting)
            if in_indonesia(lon_yx, lat_yx):
                candidates.append({"epsg":epsg,"order":"yx","lon":lon_yx,"lat":lat_yx})
        except Exception:
            pass
    candidates_sorted = sorted(candidates, key=lambda c: (0 if c["epsg"]==prioritize_epsg else 1))
    return candidates_sorted

def detect_projected_pairs_with_priority(pairs, zones=(46,47,48,49,50), prioritize_epsg=32748):
    if not pairs:
        return None, None, None
    a_med, b_med = pairs[len(pairs)//2]
    cand = try_zones_orders(a_med, b_med, zones=zones, prioritize_epsg=prioritize_epsg)
    if not cand:
        cand = try_zones_orders(b_med, a_med, zones=zones, prioritize_epsg=prioritize_epsg)
        if not cand:
            return None, None, None
    chosen = cand[0]
    chosen_epsg = chosen["epsg"]; chosen_order = chosen["order"]
    transformer = Transformer.from_crs(f"EPSG:{chosen_epsg}", "EPSG:4326", always_xy=True)
    out = []
    for a,b in pairs:
        if chosen_order == "xy":
            lon, lat = transformer.transform(a,b)
        else:
            lon, lat = transformer.transform(b,a)
        out.append((lon, lat))
    return out, chosen_epsg, chosen_order

# --------------------------
# Extract tables & coords from PDF (hierarchy)
# --------------------------
def extract_tables_and_coords_from_pdf(uploaded_file):
    coords_disetujui = []
    coords_dimohon = []
    coords_plain = []
    luas_disetujui = None
    luas_dimohon = None
    pages_texts = []

    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages_texts.append(text)
            table = page.extract_table()
            # detect mode from lines first
            mode = None
            for line in text.splitlines():
                low = line.lower()
                if "koordinat" in low and "disetujui" in low:
                    mode = "disetujui"
                elif "koordinat" in low and "dimohon" in low:
                    mode = "dimohon"
                # extract luas inline
                if "luas tanah yang disetujui" in low and luas_disetujui is None:
                    luas_disetujui = parse_luas_line(line)
                if "luas tanah yang dimohon" in low and luas_dimohon is None:
                    luas_dimohon = parse_luas_line(line)
                # parse coordinate-like lines
                parsed = extract_coords_from_line_pair(line)
                if parsed:
                    x,y = parsed
                    if mode == "disetujui":
                        coords_disetujui.append((x,y))
                    elif mode == "dimohon":
                        coords_dimohon.append((x,y))
                    else:
                        coords_plain.append((x,y))

            # parse table rows (if present)
            if table:
                header = None
                if len(table) > 0 and any(cell and re.search(r"bujur|lintang", str(cell), flags=re.IGNORECASE) for cell in table[0]):
                    header = [str(c).strip().lower() if c else "" for c in table[0]]
                    rows = table[1:]
                else:
                    rows = table
                for row in rows:
                    if not row:
                        continue
                    nums = []
                    for cell in row:
                        if cell is None:
                            continue
                        cell_s = str(cell).strip()
                        m = re.search(r"(-?\d{1,13}[\,\.\d]*)", cell_s)
                        if m:
                            try:
                                nums.append(float(m.group(1).replace(",", ".")))
                            except:
                                pass
                    if len(nums) >= 2:
                        if header:
                            try:
                                idx_bujur = next(i for i,v in enumerate(header) if "bujur" in v)
                                idx_lintang = next(i for i,v in enumerate(header) if "lintang" in v)
                                
                                # ================================================
                                # === MULAI BLOK PERBAIKAN ===
                                # Ekstrak nilai berdasarkan header (yang mungkin salah)
                                lon_val = float(re.search(r"(-?\d{1,13}[\,\.\d]*)", str(row[idx_bujur])).group(1).replace(",", "."))
                                lat_val = float(re.search(r"(-?\d{1,13}[\,\.\d]*)", str(row[idx_lintang])).group(1).replace(",", "."))

                                # Cek jika nilai tertukar (karena header PDF salah)
                                # (mis. lat_val = 107.x dan lon_val = -6.x)
                                if (95 <= lat_val <= 141) and (-11 <= lon_val <= 6):
                                    # Nilai tertukar, tukar kembali
                                    lon, lat = lat_val, lon_val
                                else:
                                    # Nilai (dan header) sudah benar
                                    lon, lat = lon_val, lat_val
                                # === SELESAI BLOK PERBAIKAN ===
                                # ================================================

                            except Exception:
                                lon, lat = nums[0], nums[1]
                        else:
                            lon, lat = nums[0], nums[1]
                        page_text_low = text.lower()
                        if "koordinat" in page_text_low and "disetujui" in page_text_low:
                            coords_disetujui.append((lon, lat))
                        elif "koordinat" in page_text_low and "dimohon" in page_text_low:
                            coords_dimohon.append((lon, lat))
                        else:
                            coords_plain.append((lon, lat))

    # fallback detection for luas (scan whole pages for any numeric+unit near 'luas')
    joined = "\n".join(pages_texts)
    m_dis = re.search(r"luas\s+tanah\s+yang\s+disetujui[^\d\n\r]{0,40}[:\-–]?\s*([\d\.,]+)\s*(m2|m²|m\s*2|ha|hektar)?",
                        joined, flags=re.IGNORECASE)
    m_dim = re.search(r"luas\s+tanah\s+yang\s+dimohon[^\d\n\r]{0,40}[:\-–]?\s*([\d\.,]+)\s*(m2|m²|m\s*2|ha|hektar)?",
                        joined, flags=re.IGNORECASE)
    if m_dis and luas_disetujui is None:
        num = m_dis.group(1)
        unit = m_dis.group(2) or ""
        luas_disetujui = f"{num} {unit.strip()}".strip()
    if m_dim and luas_dimohon is None:
        num = m_dim.group(1)
        unit = m_dim.group(2) or ""
        luas_dimohon = f"{num} {unit.strip()}".strip()
    if luas_disetujui is None or luas_dimohon is None:
        for line in joined.splitlines():
            low = line.lower()
            if "luas tanah yang disetujui" in low and luas_disetujui is None:
                luas_disetujui = parse_luas_line(line)
            if "luas tanah yang dimohon" in low and luas_dimohon is None:
                luas_dimohon = parse_luas_line(line)

    return {
        "disetujui": coords_disetujui,
        "dimohon": coords_dimohon,
        "plain": coords_plain,
        "luas_disetujui": luas_disetujui,
        "luas_dimohon": luas_dimohon
    }

# ======================
# UI: Upload PKKPR
# ======================
col1, col2 = st.columns([0.7, 0.3])
uploaded_pkkpr = col1.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

epsg_override_input = st.sidebar.text_input("Override EPSG (mis. 32748) — kosong = auto-detect", value="")

gdf_polygon = None
gdf_points = None
luas_pkkpr_doc = None
luas_label = None
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

            # choose priority: disetujui > dimohon > plain
            if coords_disetujui:
                coords_sel = coords_disetujui
                luas_pkkpr_doc = luas_disetujui
                luas_label = "disetujui"
            elif coords_dimohon:
                coords_sel = coords_dimohon
                luas_pkkpr_doc = luas_dimohon
                luas_label = "dimohon"
            else:
                coords_sel = coords_plain
                luas_pkkpr_doc = None
                luas_label = "plain"

            # classify pairs
            projected_pairs = []
            geographic_pairs = []
            for a,b in coords_sel:
                if abs(a) > 1000 or abs(b) > 1000:
                    projected_pairs.append((a,b))
                else:
                    geographic_pairs.append((a,b))

            coords_final = []
            if len(projected_pairs) >= max(3, len(geographic_pairs)):
                epsg_override = int(epsg_override_input) if epsg_override_input.strip().isdigit() else None
                transformed = None; chosen_epsg = None; chosen_order = None
                if epsg_override:
                    try:
                        ttest = Transformer.from_crs(f"EPSG:{epsg_override}", "EPSG:4326", always_xy=True)
                        sample = projected_pairs[len(projected_pairs)//2]
                        try:
                            lon_xy, lat_xy = ttest.transform(sample[0], sample[1])
                            if in_indonesia(lon_xy, lat_xy):
                                chosen_epsg = epsg_override; chosen_order = "xy"
                        except:
                            pass
                        if chosen_epsg is None:
                            try:
                                lon_yx, lat_yx = ttest.transform(sample[1], sample[0])
                                if in_indonesia(lon_yx, lat_yx):
                                    chosen_epsg = epsg_override; chosen_order = "yx"
                            except:
                                pass
                        if chosen_epsg:
                            t = Transformer.from_crs(f"EPSG:{chosen_epsg}", "EPSG:4326", always_xy=True)
                            transformed = []
                            for a,b in projected_pairs:
                                if chosen_order=="xy":
                                    lon,lat = t.transform(a,b)
                                else:
                                    lon,lat = t.transform(b,a)
                                transformed.append((lon,lat))
                    except Exception:
                        transformed = None
                if transformed is None:
                    transformed, chosen_epsg, chosen_order = detect_projected_pairs_with_priority(projected_pairs, zones=(46,47,48,49,50), prioritize_epsg=32748)
                if transformed is None:
                    st.warning("Koordinat metrik terdeteksi tetapi zona/proyeksi tidak berhasil dideteksi. Coba override EPSG di sidebar.")
                    coords_final = projected_pairs
                    detected_info = {"mode":"projected (undetected)","n_points":len(coords_final)}
                else:
                    coords_final = transformed
                    detected_info = {"mode":"projected","epsg":chosen_epsg,"order":chosen_order,"n_points":len(coords_final)}
            else:
                coords_final = geographic_pairs
                detected_info = {"mode":"geographic","n_points":len(coords_final)}

            # Build GeoDataFrames
            if coords_final:
                if coords_final[0] != coords_final[-1]:
                    coords_final.append(coords_final[0])
                gdf_points = gpd.GeoDataFrame(pd.DataFrame(coords_final, columns=["Lon","Lat"]),
                                            geometry=[Point(x,y) for x,y in coords_final], crs="EPSG:4326")
                poly = Polygon(coords_final)
                gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
                gdf_polygon = fix_geometry(gdf_polygon)
            else:
                st.warning("Tidak ada koordinat terpilih dari dokumen.")
        except Exception as e:
            st.error(f"Gagal memproses PDF: {e}")
            if DEBUG:
                st.exception(e)

    elif uploaded_pkkpr.name.lower().endswith(".zip"):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                zf = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()))
                zf.extractall(tmp)
                # try to read first vector file found
                gdf_polygon = None
                for root, dirs, files in os.walk(tmp):
                    for fname in files:
                        if fname.lower().endswith((".shp", ".geojson", ".gpkg")):
                            try:
                                gdf_polygon = gpd.read_file(os.path.join(root, fname))
                                break
                            except Exception:
                                continue
                    if gdf_polygon is not None:
                        break
                if gdf_polygon is None:
                    # try reading folder as shapefile
                    gdf_polygon = gpd.read_file(tmp)

                # --- Heuristik CRS: jangan langsung set_crs(4326) jika .crs None ---
                if gdf_polygon.crs is None:
                    try:
                        b = gdf_polygon.total_bounds  # [minx, miny, maxx, maxy]
                        minx, miny, maxx, maxy = b
                        # Jika nilai dalam rentang lon/lat maka set sebagai 4326,
                        # jika tidak, asumsi projected (meter) dan jangan paksa 4326.
                        if (-180 <= minx <= 180) and (-90 <= miny <= 90) and (-180 <= maxx <= 180) and (-90 <= maxy <= 90):
                            gdf_polygon.set_crs(epsg=4326, inplace=True)
                            if DEBUG:
                                st.sidebar.write("DEBUG: CRS tidak ditemukan — bounds menyerupai lon/lat, set CRS=4326.")
                        else:
                            # kemungkinan data dalam satuan meter/projected; biarkan crs None untuk diproses lebih lanjut
                            if DEBUG:
                                st.sidebar.write("DEBUG: CRS tidak ditemukan — bounds menunjukkan koordinat projected (meter). Tidak memaksa EPSG:4326.")
                    except Exception as e:
                        if DEBUG:
                            st.sidebar.write("DEBUG: heuristik CRS gagal:", e)

                gdf_polygon = fix_geometry(gdf_polygon)
                st.success("Shapefile PKKPR terbaca dari ZIP.")
        except Exception as e:
            st.error(f"Gagal membaca shapefile PKKPR: {e}")
            if DEBUG:
                st.exception(e)

# show detection info
if detected_info:
    st.sidebar.markdown("### Hasil Deteksi Koordinat")
    for k,v in detected_info.items():
        st.sidebar.write(f"- **{k}**: {v}")

# Additional debug info for loaded GDF
if DEBUG and 'gdf_polygon' in globals() and gdf_polygon is not None:
    try:
        st.sidebar.markdown("### DEBUG: Info GDF Polygon")
        st.sidebar.write("CRS (gdf_polygon.crs):", getattr(gdf_polygon, "crs", None))
        try:
            st.sidebar.write("Bounds (total_bounds):", gdf_polygon.total_bounds)
        except Exception as e:
            st.sidebar.write("Bounds: error -", e)
        try:
            centroid_tmp = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
            st.sidebar.write("Centroid (lon,lat) setelah to_crs(4326):", (centroid_tmp.x, centroid_tmp.y))
        except Exception as e:
            st.sidebar.write("Centroid (to_crs): error -", e)
    except Exception:
        pass

# ======================
# ANALISIS LUAS (OUTPUT FORMAT sesuai permintaan)
# ======================
if gdf_polygon is not None:
    try:
        st.markdown("### Analisis Luas Geometri\n")
        # Luas Dokumen (tampilkan sesuai dokumen, kosong jika tidak ada)
        if luas_pkkpr_doc:
            st.write("Luas Dokumen PKKPR :")
            st.info(f"{luas_pkkpr_doc}")
        else:
            st.write("Luas Dokumen PKKPR :")
            st.info("")

        # Luas geometri (UTM & Mercator)
        # safe centroid: jika gdf_polygon crs known use it, else assume 4326
        try:
            centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        except Exception:
            centroid = gdf_polygon.geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
        try:
            luas_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
        except Exception:
            luas_utm = None
        try:
            luas_merc = gdf_polygon.to_crs(epsg=3857).area.sum()
        except Exception:
            luas_merc = None

        st.write("")  # spacer
        if luas_utm is not None:
            st.write(f"Luas PKKPR (UTM {utm_zone}): {format_angka_id(luas_utm)} m²")
        else:
            st.write("Luas PKKPR (UTM): Gagal menghitung (cek CRS).")
        if luas_merc is not None:
            st.write(f"Luas PKKPR (Mercator): {format_angka_id(luas_merc)} m²")
        else:
            st.write("Luas PKKPR (Mercator): Gagal menghitung (cek CRS).")
    except Exception as e:
        st.error(f"Gagal menghitung luas: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

    # Export shapefile (two layers in zip)
    try:
        zip_bytes = save_shapefile_layers(gdf_polygon, gdf_points)
        st.download_button("⬇️ Download SHP PKKPR (Polygon + Point)", zip_bytes, "PKKPR_Hasil_Konversi.zip", mime="application/zip")
    except Exception as e:
        st.error(f"Gagal menyiapkan shapefile: {e}")
        if DEBUG:
            st.exception(e)

# ======================
# Upload Tapak Proyek (overlay)
# ======================
col1, col2 = st.columns([0.7, 0.3])
uploaded_tapak = col1.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"], key="tapak")
gdf_tapak = None
if uploaded_tapak:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zf = zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()))
            zf.extractall(tmp)
            gdf_tapak = None
            for root, dirs, files in os.walk(tmp):
                for fname in files:
                    if fname.lower().endswith((".shp", ".geojson", ".gpkg")):
                        try:
                            gdf_tapak = gpd.read_file(os.path.join(root, fname))
                            break
                        except Exception:
                            continue
                if gdf_tapak is not None:
                    break
            if gdf_tapak is None:
                gdf_tapak = gpd.read_file(tmp)
            # Heuristik serupa untuk tapak: only set 4326 if bounds look like lon/lat
            if gdf_tapak.crs is None:
                try:
                    b2 = gdf_tapak.total_bounds
                    minx, miny, maxx, maxy = b2
                    if (-180 <= minx <= 180) and (-90 <= miny <= 90) and (-180 <= maxx <= 180) and (-90 <= maxy <= 90):
                        gdf_tapak.set_crs(epsg=4326, inplace=True)
                        if DEBUG:
                            st.sidebar.write("DEBUG: Tapak CRS undetected -> set 4326 (lon/lat bounds).")
                    else:
                        if DEBUG:
                            st.sidebar.write("DEBUG: Tapak CRS undetected -> assume projected (meter). Not forcing 4326.")
                except Exception as e:
                    if DEBUG:
                        st.sidebar.write("DEBUG: heuristik CRS tapak gagal:", e)
            st.success("Shapefile Tapak terbaca.")
    except Exception as e:
        st.error(f"Gagal membaca shapefile Tapak Proyek: {e}")
        if DEBUG:
            st.exception(e)

if gdf_polygon is not None and gdf_tapak is not None:
    try:
        centroid_t = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
        utm_epsg_t, utm_zone_t = get_utm_info(centroid_t.x, centroid_t.y)
        gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg_t)
        gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg_t)
        inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how="intersection")
        luas_overlap = inter.area.sum() if not inter.empty else 0
        luas_tapak = gdf_tapak_utm.area.sum()
        luas_outside = luas_tapak - luas_overlap
        st.success(f"**HASIL OVERLAY TAPAK:**\n- Luas Tapak UTM {utm_zone_t}: **{format_angka_id(luas_tapak)} m²**\n- Luas Tapak di dalam PKKPR: **{format_angka_id(luas_overlap)} m²**\n- Luas Tapak Di luar PKKPR : **{format_angka_id(luas_outside)} m²**")
    except Exception as e:
        st.error(f"Gagal overlay: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# ======================
# Interactive map
# ======================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    try:
        # centroid safe conversion
        try:
            centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        except Exception:
            centroid = gdf_polygon.geometry.centroid.iloc[0]
        m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None)
        Fullscreen(position="bottomleft").add_to(m)
        folium.TileLayer("openstreetmap", name="OpenStreetMap").add_to(m)
        folium.TileLayer("CartoDB Positron", name="CartoDB Positron").add_to(m)
        folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery").add_to(m)
        folium.GeoJson(gdf_polygon.to_crs(epsg=4326), name="PKKPR", style_function=lambda x: {"color":"yellow","weight":3,"fillOpacity":0.1}).add_to(m)
        if gdf_points is not None:
            for i, row in gdf_points.iterrows():
                folium.CircleMarker([row.geometry.y, row.geometry.x], radius=4, color="black", fill=True, fill_color="orange", popup=f"Titik {i+1}").add_to(m)
        if gdf_tapak is not None:
            folium.GeoJson(gdf_tapak.to_crs(epsg=4326), name="Tapak Proyek", style_function=lambda x: {"color":"red","fillColor":"red","fillOpacity":0.4}).add_to(m)
        folium.LayerControl(collapsed=True).add_to(m)
        st_folium(m, width=900, height=600)
    except Exception as e:
        st.error(f"Gagal membuat peta interaktif: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# ======================
# Layout PNG
# ======================
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
            gdf_points.to_crs(epsg=3857).plot(ax=ax, color="orange", edgecolor="black", markersize=30, label="Titik PKKPR")
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
        ax.legend(handles=legend, 
