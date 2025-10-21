import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, re, tempfile
from shapely.geometry import Point, Polygon
from shapely import affinity
import folium
from streamlit_folium import st_folium
import pdfplumber
import matplotlib.pyplot as plt
import contextily as ctx
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
from folium.plugins import Fullscreen
import xyzservices.providers as xyz
import pyproj

# ======================
# === Konfigurasi App ===
# ======================
st.set_page_config(page_title="PKKPR → SHP & Overlay (Robust)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")
st.markdown("---")

# Toggle debug untuk output tambahan
DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

# ======================
# === Fungsi Helper ===
# ======================

def get_utm_info(lon, lat):
    """Menentukan zona UTM dan kode EPSG berdasarkan koordinat.
    Menjaga zona agar tetap valid (1–60) dan memastikan EPSG valid."""
    try:
        lon_f = float(lon)
        lat_f = float(lat)
    except Exception:
        return 3857, "3857-Mercator"

    # normalisasi longitude supaya tidak keluar dari -180 sampai 180
    if lon_f > 180 or lon_f < -180:
        lon_f = ((lon_f + 180) % 360) - 180

    zone = int((lon_f + 180) / 6) + 1
    zone = max(1, min(zone, 60))  # batasi antara 1 dan 60

    if lat_f >= 0:
        epsg = 32600 + zone
        ns = "N"
    else:
        epsg = 32700 + zone
        ns = "S"

    epsg = int(epsg)
    zone_label = f"{zone}{ns}"
    return epsg, zone_label


def save_shapefile(gdf):
    """Simpan GeoDataFrame ke zip shapefile di memory buffer."""
    with tempfile.TemporaryDirectory() as temp_dir:
        out_path = os.path.join(temp_dir, "PKKPR_Output.shp")
        # ekspor dalam WGS84 (EPSG:4326)
        gdf.to_crs(epsg=4326).to_file(out_path)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in os.listdir(temp_dir):
                file_path = os.path.join(temp_dir, file)
                zf.write(file_path, arcname=file)
        zip_buffer.seek(0)
        return zip_buffer.read()


def normalize_text(s):
    if not s:
        return s
    s = str(s)
    s = s.replace('\u2019', "'").replace('\u201d', '"').replace('\u201c', '"')
    s = s.replace('’', "'").replace('“', '"').replace('”', '"')
    s = s.replace('\xa0', ' ')
    return s


def parse_coordinate(coord_str):
    """
    Konversi string koordinat (DMS atau desimal) ke float.
    Menangani berbagai separator dan karakter aneh.
    """
    if coord_str is None:
        return None
    coord_str = normalize_text(str(coord_str)).strip()
    if coord_str == "":
        return None
    coord_str = coord_str.replace(',', '.')
    m_dir = re.search(r'([NnSsEeWw])$', coord_str.strip())
    direction = m_dir.group(1).upper() if m_dir else None
    if direction:
        coord_body = coord_str[:m_dir.start()].strip()
    else:
        coord_body = coord_str
    coord_body = coord_body.replace('°', 'd').replace('\u00b0', 'd')
    coord_body = coord_body.replace('’', "'").replace('`', "'").replace('‘', "'")
    coord_body = coord_body.replace('"', 's').replace('”', 's').replace('″', 's')
    coord_body = re.sub(r'\s+', '', coord_body)
    m_dms = re.match(r"^(\d{1,3})d(\d{1,3})'(\d{1,3}(?:\.\d+)?)s?$", coord_body)
    if m_dms:
        d, m, s = m_dms.groups()
        try:
            decimal = float(d) + float(m) / 60 + float(s) / 3600
            if direction in ('S', 'W'):
                decimal *= -1
            return decimal
        except:
            pass
    m_dms2 = re.match(r"^(\d{1,3})[\.,\s:;\-](\d{1,3})[\.,\s:;\-]?(\d{1,3}(?:\.\d+)?)$", coord_body)
    if m_dms2:
        d, m, s = m_dms2.groups()
        try:
            decimal = float(d) + float(m) / 60 + float(s) / 3600
            if direction in ('S', 'W'):
                decimal *= -1
            return decimal
        except:
            pass
    decimal_str = re.sub(r"[^0-9\.\-]", '', coord_body)
    try:
        if decimal_str not in ['', '.', '-', '-.']:
            return float(decimal_str)
    except:
        pass
    m_anynum = re.search(r"(-?\d{1,3}\.\d+)", coord_str)
    if m_anynum:
        try:
            return float(m_anynum.group(1))
        except:
            pass
    return None


def parse_luas_from_text(text):
    text_clean = re.sub(r"\s+", " ", (text or ""), flags=re.IGNORECASE)
    luas_matches = re.findall(
        r"luas\s*tanah\s*yang\s*(dimohon|disetujui)\s*[:\-]?\s*([\d\.,]+\s*(M2|M²|HA))",
        text_clean,
        re.IGNORECASE
    )
    luas_data = {}
    for label, value, satuan in luas_matches:
        luas_data[label.lower()] = (value.strip().upper() if value else "").replace(" ", "")
    if "disetujui" in luas_data:
        return luas_data["disetujui"], "disetujui"
    elif "dimohon" in luas_data:
        return luas_data["dimohon"], "dimohon"
    else:
        m = re.search(r"luas\s*tanah\s*[:\-]?\s*([\d\.,]+\s*(M2|M²|HA))", text_clean, re.IGNORECASE)
        if m:
            return m.group(1).strip(), "tanpa judul"
        return None, "tidak ditemukan"


def format_angka_id(value):
    try:
        val = float(value)
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")
        else:
            return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(value)


def extract_coords_dms_from_text(text):
    coords = []
    if not text:
        return coords
    text = normalize_text(text)
    pattern = r"(\d{1,3}°\s*\d{1,2}'\s*[\d,\.]+\"\s*[BbTt]{1,2})\s+(\d{1,2}°\s*\d{1,2}'\s*[\d,\.]+\"\s*[LlUu]{1,2})"
    for m in re.finditer(pattern, text):
        lon_raw, lat_raw = m.groups()
        lon = parse_coordinate(lon_raw)
        lat = parse_coordinate(lat_raw)
        if lon is not None and lat is not None:
            coords.append((lon, lat))
    return coords


def extract_coords_from_text(text):
    out = []
    if not text:
        return out
    text = normalize_text(text)
    pattern = r"(-?\d{1,3}\.\d+)[^\d\-\.,]+(-?\d{1,3}\.\d+)"
    for m in re.finditer(pattern, text):
        a, b = m.group(1), m.group(2)
        try:
            a_f, b_f = float(a), float(b)
            # heuristik untuk Indonesia: lon ~95..141 (abs 95..145), lat ~-11..6
            if 90 <= abs(a_f) <= 145 and -11 <= b_f <= 6:
                out.append((a_f, b_f))
            elif 90 <= abs(b_f) <= 145 and -11 <= a_f <= 6:
                out.append((b_f, a_f))
            else:
                out.append((a_f, b_f))
        except:
            continue
    return out


# ======================
# === Perbaikan Otomatis Geometri (Debug helper) ===
# ======================
def debug_and_fix_gdf_polygon(gdf, debug=DEBUG):
    info = {}
    try:
        info['len'] = len(gdf)
        info['geom_type'] = gdf.geom_type.unique().tolist()
        info['crs'] = getattr(gdf, "crs", None)
        info['bounds'] = gdf.total_bounds.tolist() if len(gdf) > 0 else None
        info['is_empty'] = gdf.is_empty.all() if len(gdf) > 0 else True
        info['is_valid'] = gdf.is_valid.all() if len(gdf) > 0 else False
    except Exception as e:
        if debug:
            st.write("DEBUG: gagal baca properti gdf_polygon:", e)
        info['error'] = str(e)

    if debug:
        st.write("DEBUG PKKPR: info awal:", info)

    try:
        # Jika kosong atau tidak valid: coba unary_union + make_valid
        if info.get('is_empty') or not info.get('is_valid'):
            try:
                geom_union = gdf.unary_union
                if geom_union is not None and not geom_union.is_empty:
                    # make_valid ada di shapely.validation (Shapely >=1.8/2.0)
                    try:
                        from shapely.validation import make_valid
                        geom_valid = make_valid(geom_union)
                    except Exception:
                        # fallback: gunakan unary_union langsung
                        geom_valid = geom_union
                    if not geom_valid.is_empty and geom_valid.geom_type in ('Polygon', 'MultiPolygon'):
                        newg = gpd.GeoDataFrame(geometry=[geom_valid], crs=gdf.crs)
                        if debug: st.write("DEBUG: make_valid/unary_union berhasil membentuk polygon.")
                        return newg, "fixed_by_make_valid"
            except Exception as e:
                if debug: st.write("DEBUG: unary_union/make_valid gagal:", e)

        # Jika geometri Point/MultiPoint -> coba buat polygon dari titik
        geom_types = [t.lower() for t in gdf.geom_type.unique().tolist()]
        if any('point' in t for t in geom_types) or any('multipoint' in t for t in geom_types):
            coords = []
            for geom in gdf.geometry:
                try:
                    if geom is None: continue
                    if geom.geom_type == 'Point':
                        coords.append((geom.x, geom.y))
                    else:
                        for p in geom.geoms:
                            coords.append((p.x, p.y))
                except Exception:
                    continue
            seen = set(); uniq = []
            for c in coords:
                if c not in seen:
                    seen.add(c); uniq.append(c)
            if len(uniq) >= 3:
                if uniq[0] != uniq[-1]:
                    uniq.append(uniq[0])
                new_poly = Polygon(uniq)
                newg = gpd.GeoDataFrame(geometry=[new_poly], crs=gdf.crs)
                if debug: st.write(f"DEBUG: Dibangun polygon dari titik (count={len(uniq)-1}).")
                return newg, "fixed_from_points"

        # Cek indikasi terbalik (lat/lon tertukar) berdasarkan bounds aneh
        try:
            b = gdf.total_bounds
            xmin, ymin, xmax, ymax = b
            if (abs(xmin) <= 90 and abs(xmax) <= 90) and (abs(ymin) >= 90 or abs(ymax) >= 90):
                def swapxy(geom):
                    try:
                        if geom.geom_type == 'Polygon':
                            rings = []
                            for ring in [geom.exterior] + list(geom.interiors):
                                rings.append([(y, x) for (x, y) in ring.coords])
                            return Polygon(rings[0], rings[1:])
                        elif geom.geom_type == 'Point':
                            return Point(geom.y, geom.x)
                        else:
                            return geom
                    except Exception:
                        return geom
                g_swapped = gdf.copy()
                g_swapped['geometry'] = g_swapped.geometry.apply(swapxy)
                if debug: st.write("DEBUG: Mencoba swap XY pada geometri karena indikasi terbalik.")
                return g_swapped, "fixed_swapped_xy"
        except Exception:
            pass

        return gdf, "no_change"
    except Exception as e:
        if debug:
            st.write("DEBUG: error saat debug_and_fix_gdf_polygon:", e)
        return gdf, "error"


# ======================
# === Normalisasi Skala Jika Perlu ===
# ======================
def try_rescale_to_lonlat(gdf, debug=DEBUG):
    """Coba bagi koordinat dengan factor 1,10,100,... sampai cocok sebagai lon/lat."""
    if gdf is None or len(gdf) == 0:
        return gdf, None
    b = gdf.total_bounds
    xmin, ymin, xmax, ymax = b
    # Jika sudah wajar, langsung return
    if (-180 <= xmin <= 180 and -90 <= ymin <= 90 and -180 <= xmax <= 180 and -90 <= ymax <= 90):
        return gdf, None

    for fac in [1, 10, 100, 1000, 10000, 100000]:
        try:
            g2 = gdf.copy()
            # scale around origin (0,0) — pembagian koordinat
            g2['geometry'] = g2['geometry'].apply(
                lambda geom: affinity.scale(geom, xfact=1.0/fac, yfact=1.0/fac, origin=(0,0))
            )
            b2 = g2.total_bounds
            xmin2, ymin2, xmax2, ymax2 = b2
            # heuristik rentang untuk Indonesia
            if (90 <= abs(xmin2) <= 145 and -11 <= ymin2 <= 6 and 90 <= abs(xmax2) <= 145 and -11 <= ymax2 <= 6) \
               or (-180 <= xmin2 <= 180 and -90 <= ymin2 <= 90 and -180 <= xmax2 <= 180 and -90 <= ymax2 <= 90):
                # terdeteksi cocok -> set CRS ke 4326 dan return
                g2 = g2.set_crs(epsg=4326, allow_override=True)
                if debug:
                    st.write(f"DEBUG: Rescale berhasil dengan factor {fac}. New bounds: {b2}")
                return g2, fac
        except Exception as e:
            if debug:
                st.write(f"DEBUG: Rescale gagal factor {fac}: {e}")
            continue
    return gdf, None


# ======================
# === Upload PKKPR ===
# ======================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

coords, gdf_points, gdf_polygon = [], None, None
luas_pkkpr_doc, luas_pkkpr_doc_label = None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.endswith('.pdf'):
        coords_plain = []
        full_text = ""
        try:
            with pdfplumber.open(uploaded_pkkpr) as pdf:
                for page_idx, page in enumerate(pdf.pages, start=1):
                    text = page.extract_text() or ""
                    full_text += "\n" + text

                    tables = page.extract_tables() or []
                    for tb in tables:
                        if not tb or len(tb) <= 1:
                            continue
                        header = [str(c).lower().strip() if c else '' for c in tb[0]]
                        lon_keywords = ['bujur', 'longitude', 'x', 'bt', 'btu']
                        lat_keywords = ['lintang', 'latitude', 'y', 'ls', 'lt']
                        idx_lon = next((i for i, h in enumerate(header) if any(k in h for k in lon_keywords)), -1)
                        idx_lat = next((i for i, h in enumerate(header) if any(k in h for k in lat_keywords)), -1)
                        if idx_lon == -1 or idx_lat == -1:
                            if len(tb[0]) >= 2:
                                idx_lon, idx_lat = 1, 2 if len(tb[0]) > 2 else (0, 1)
                        for row in tb[1:]:
                            cleaned_row = [str(cell).strip() if cell else '' for cell in row]
                            try:
                                lon_str = cleaned_row[idx_lon]
                                lat_str = cleaned_row[idx_lat]
                            except Exception:
                                continue
                            lon_val = parse_coordinate(lon_str)
                            lat_val = parse_coordinate(lat_str)
                            is_lon_valid = lon_val is not None and 90 <= abs(lon_val) <= 145
                            is_lat_valid = lat_val is not None and -11 <= lat_val <= 6
                            if not (is_lon_valid and is_lat_valid) and lon_val is not None and lat_val is not None:
                                if abs(lat_val) >= 90 and -11 <= lon_val <= 6:
                                    lon_val, lat_val = lat_val, lon_val
                                    is_lon_valid = True
                                    is_lat_valid = True
                            if is_lon_valid and is_lat_valid:
                                coords_plain.append((lon_val, lat_val))

                    dms_found = extract_coords_dms_from_text(text)
                    if dms_found:
                        coords_plain.extend(dms_found)

                    found_pairs = extract_coords_from_text(text)
                    if found_pairs:
                        coords_plain.extend(found_pairs)

            if not coords_plain:
                coords_plain = extract_coords_dms_from_text(full_text) or extract_coords_from_text(full_text)

            coords = list(dict.fromkeys(coords_plain))
            luas_pkkpr_doc, luas_pkkpr_doc_label = parse_luas_from_text(full_text)

            if coords:
                flipped_coords = coords.copy()
                if len(flipped_coords) > 1 and flipped_coords[0] != flipped_coords[-1]:
                    flipped_coords.append(flipped_coords[0])

                gdf_points = gpd.GeoDataFrame(
                    pd.DataFrame(flipped_coords, columns=["Longitude", "Latitude"]),
                    geometry=[Point(xy) for xy in flipped_coords],
                    crs="EPSG:4326"
                )
                gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(flipped_coords)], crs="EPSG:4326")

            with col2:
                st.markdown(f"<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ {len(coords)} titik (PDF terdeteksi)</p>", unsafe_allow_html=True)

        except Exception as e:
            st.error(f"Gagal memproses PDF: {e}")
            if DEBUG:
                st.exception(e)

    elif uploaded_pkkpr.name.endswith('.zip'):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()), 'r')
                zip_ref.extractall(temp_dir)
                zip_ref.close()
                gdf_polygon = gpd.read_file(temp_dir)
                if gdf_polygon.crs is None:
                    gdf_polygon.set_crs(epsg=4326, inplace=True)
                with col2:
                    st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ Shapefile (PKKPR)</p>", unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Gagal membaca shapefile PKKPR: {e}")
            if DEBUG:
                st.exception(e)
else:
    gdf_polygon = None

# Jika gdf_polygon ada, jalankan perbaikan otomatis (debug helper)
if 'gdf_polygon' in locals() and gdf_polygon is not None:
    gdf_polygon, fix_status = debug_and_fix_gdf_polygon(gdf_polygon, debug=DEBUG)
    if DEBUG:
        st.write("DEBUG: fix_status =", fix_status)

    # Jika bounds tidak sesuai lon/lat normal, coba rescale otomatis
    try:
        b0 = gdf_polygon.total_bounds
        if not (-180 <= b0[0] <= 180 and -90 <= b0[1] <= 90 and -180 <= b0[2] <= 180 and -90 <= b0[3] <= 90):
            gdf_rescaled, fac = try_rescale_to_lonlat(gdf_polygon, debug=DEBUG)
            if fac:
                gdf_polygon = gdf_rescaled
                if DEBUG:
                    st.success(f"Auto-normalisasi skala: dibagi {fac} — CRS diset ke EPSG:4326.")
            else:
                if DEBUG:
                    st.warning("Auto-normalisasi skala: tidak ditemukan faktor pembagi yang cocok.")
    except Exception as e:
        if DEBUG:
            st.write("DEBUG: error saat percobaan rescale:", e)

# --- Hasil Konversi dan Analisis Luas (aman)
if 'gdf_polygon' in locals() and gdf_polygon is not None:
    try:
        try:
            zip_pkkpr_bytes = save_shapefile(gdf_polygon)
            st.download_button("⬇️ Download SHP PKKPR (ZIP)", zip_pkkpr_bytes, "PKKPR_Hasil_Konversi.zip", mime="application/zip")
        except Exception as e:
            st.error(f"Gagal menyiapkan unduhan shapefile: {e}")
            if DEBUG:
                st.exception(e)

        # centroid pada EPSG:4326
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)

        try:
            pyproj.CRS.from_epsg(int(utm_epsg))
            use_epsg = int(utm_epsg)
            used_zone_label = utm_zone
        except Exception:
            if DEBUG:
                st.warning(f"UTM EPSG {utm_epsg} tidak valid — fallback ke EPSG:3857.")
            use_epsg = 3857
            used_zone_label = "fallback-3857"

        try:
            gdf_polygon_utm = gdf_polygon.to_crs(epsg=use_epsg)
            luas_pkkpr_utm = gdf_polygon_utm.area.sum()
        except Exception as e:
            if DEBUG:
                st.warning(f"Gagal konversi ke EPSG {use_epsg}: {e}")
            gdf_polygon_utm = gdf_polygon.to_crs(epsg=3857)
            luas_pkkpr_utm = gdf_polygon_utm.area.sum()
            used_zone_label = "fallback-3857"

        try:
            luas_pkkpr_mercator = gdf_polygon.to_crs(epsg=3857).area.sum()
        except Exception:
            luas_pkkpr_mercator = None

        luas_doc_str = f"{luas_pkkpr_doc} ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "tidak tersedia (dokumen)"
        info_lines = [
            f"**Analisis Luas Batas PKKPR**:",
            f"- Luas PKKPR (dokumen): **{luas_doc_str}**",
            f"- Luas PKKPR (UTM {used_zone_label}): **{format_angka_id(luas_pkkpr_utm)} m²**"
        ]
        if luas_pkkpr_mercator is not None:
            info_lines.append(f"- Luas PKKPR (WGS84 Mercator): **{format_angka_id(luas_pkkpr_mercator)} m²**")

        st.info("\n".join(info_lines))
    except Exception as e:
        st.error(f"Gagal menghitung luas: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# ======================
# === Upload Tapak Proyek ===
# ======================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"], key='tapak')

gdf_tapak = None
if uploaded_tapak:
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()), 'r')
            zip_ref.extractall(temp_dir)
            zip_ref.close()
            gdf_tapak = gpd.read_file(temp_dir)
            if gdf_tapak.crs is None:
                gdf_tapak.set_crs(epsg=4326, inplace=True)
            with col2:
                st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅</p>", unsafe_allow_html=True)
    except Exception as e:
        st.error(f"Gagal membaca shapefile Tapak Proyek: {e}")
        if DEBUG:
            st.exception(e)

# ======================
# === Analisis Overlay (aman) ===
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    try:
        centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
        try:
            pyproj.CRS.from_epsg(int(utm_epsg))
            use_epsg = int(utm_epsg)
            used_zone_label = utm_zone
        except Exception:
            if DEBUG:
                st.warning(f"UTM EPSG {utm_epsg} tidak valid — fallback ke EPSG:3857.")
            use_epsg = 3857
            used_zone_label = "fallback-3857"

        try:
            gdf_tapak_utm = gdf_tapak.to_crs(epsg=use_epsg)
            gdf_polygon_utm = gdf_polygon.to_crs(epsg=use_epsg)
        except Exception as e:
            if DEBUG:
                st.warning(f"Gagal konversi ke EPSG {use_epsg}: {e}")
            gdf_tapak_utm = gdf_tapak.to_crs(epsg=3857)
            gdf_polygon_utm = gdf_polygon.to_crs(epsg=3857)
            used_zone_label = "fallback-3857"

        inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how='intersection')
        luas_overlap = inter.area.sum() if not inter.empty else 0
        luas_tapak = gdf_tapak_utm.area.sum()
        luas_outside = luas_tapak - luas_overlap

        st.success(
            f"**HASIL OVERLAY TAPAK:**\n"
            f"- Luas Tapak: **{format_angka_id(luas_tapak)} m²**\n"
            f"- Overlap PKKPR: **{format_angka_id(luas_overlap)} m²**\n"
            f"- Di luar PKKPR: **{format_angka_id(luas_outside)} m²**"
        )
    except Exception as e:
        st.error(f"Gagal overlay: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# ======================
# === Preview Peta Interaktif (Folium) ===
# ======================
if 'gdf_polygon' in locals() and gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    try:
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None, attr='')
        Fullscreen(position="bottomleft").add_to(m)

        folium.TileLayer("openstreetmap", name="OpenStreetMap", attr='').add_to(m)
        folium.TileLayer("CartoDB Positron", name="CartoDB Positron", attr='').add_to(m)
        folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery", attr='').add_to(m)

        folium.GeoJson(
            gdf_polygon.to_crs(epsg=4326),
            name="Batas PKKPR",
            style_function=lambda x: {"color": "yellow", "weight": 3, "fillOpacity": 0.1}
        ).add_to(m)

        if gdf_tapak is not None:
            folium.GeoJson(
                gdf_tapak.to_crs(epsg=4326),
                name="Tapak Proyek",
                style_function=lambda x: {"color": "red", "weight": 2, "fillColor": "red", "fillOpacity": 0.4}
            ).add_to(m)

        if 'gdf_points' in locals() and gdf_points is not None:
            for i, row in gdf_points.iterrows():
                folium.CircleMarker(
                    [row.geometry.y, row.geometry.x],
                    radius=4,
                    color="black",
                    fill=True,
                    fill_color="orange",
                    fill_opacity=1,
                    popup=f"Titik {i+1}"
                ).add_to(m)

        folium.LayerControl(collapsed=True).add_to(m)
        st_folium(m, width=900, height=600)
    except Exception as e:
        st.error(f"Gagal menampilkan peta interaktif: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# ======================
# === Layout Peta (PNG) untuk dokumentasi ===
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
            try:
                gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
                gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red", label="Tapak Proyek")
            except Exception as e:
                if DEBUG:
                    st.warning(f"Gagal konversi Tapak ke 3857 untuk layout PNG: {e}")

        if 'gdf_points' in locals() and gdf_points is not None:
            try:
                gdf_points_3857 = gdf_points.to_crs(epsg=3857)
                gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=30, label="Titik PKKPR")
            except Exception:
                pass

        try:
            ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)
        except Exception:
            if DEBUG:
                st.warning("Gagal menambahkan basemap. Lanjut tanpa basemap.")

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
