import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, re
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
import tempfile

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
    """Menentukan zona UTM dan kode EPSG berdasarkan koordinat."""
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    zone_label = f"{zone}{'N' if lat >= 0 else 'S'}"
    return epsg, zone_label


def save_shapefile(gdf):
    """Menyimpan GeoDataFrame ke ZIP Shapefile di memory buffer menggunakan tempfile."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Pastikan ada nama file shapefile
        out_path = os.path.join(temp_dir, "PKKPR_Output.shp")
        # Kalau gdf berisi multipel layers, tulis sekali
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
    # Normalisasi karakter kutip dan spasi unicode
    s = s.replace('\u2019', "'").replace('\u201d', '"').replace('\u201c', '"')
    s = s.replace('’', "'").replace('“', '"').replace('”', '"')
    s = s.replace('\xa0', ' ')
    return s


def parse_coordinate(coord_str):
    """
    Fungsi universal untuk mengkonversi string koordinat (DMS atau Desimal) ke float.
    Menangani berbagai separator dan karakter aneh.
    """
    if coord_str is None:
        return None
    coord_str = normalize_text(str(coord_str)).strip()
    if coord_str == "":
        return None

    # Ganti koma desimal jika ada
    coord_str = coord_str.replace(',', '.')

    # Hilangkan teks non-angka yang biasa muncul (kecuali - .)
    # Tapi simpan huruf arah di akhir (N,S,E,W) jika ada
    m_dir = re.search(r'([NnSsEeWw])$', coord_str.strip())
    direction = m_dir.group(1).upper() if m_dir else None
    if direction:
        coord_body = coord_str[:m_dir.start()].strip()
    else:
        coord_body = coord_str

    # Normalisasi simbol degree, minute, second
    coord_body = coord_body.replace('°', 'd').replace('\u00b0', 'd')
    coord_body = coord_body.replace('’', "'").replace('`', "'").replace('‘', "'")
    coord_body = coord_body.replace('"', 's').replace('”', 's').replace('″', 's')
    coord_body = re.sub(r'\s+', '', coord_body)

    # 1) Coba parse DMS dengan pola umum: 108d44'10.52s atau 108d44'10.52
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

    # 2) Coba pola lain D M S yang mungkin dipisah dengan spasi atau titik
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

    # 3) Coba parse sebagai desimal langsung (setelah membersihkan non-digit kecuali - .)
    decimal_str = re.sub(r"[^0-9\.\-]", '', coord_body)
    try:
        if decimal_str not in ['', '.', '-', '-.']:
            return float(decimal_str)
    except:
        pass

    # 4) Jika masih gagal, coba ambil angka pertama yang terlihat (fallback)
    m_anynum = re.search(r"(-?\d{1,3}\.\d+)", coord_str)
    if m_anynum:
        try:
            return float(m_anynum.group(1))
        except:
            pass

    return None


def parse_luas_from_text(text):
    """Ambil teks luas tanah dari dokumen."""
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
    """Format angka besar dengan pemisah ribuan titik."""
    try:
        val = float(value)
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")
        else:
            return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(value)


# ======================
# === Upload PKKPR ===
# ======================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

coords, gdf_points, gdf_polygon = [], None, None
luas_pkkpr_doc, luas_pkkpr_doc_label = None, None


# ----- Utility: Extract coordinate-like pairs from raw text (fallback) -----
def extract_coords_from_text(text):
    """Cari pasangan koordinat (lon lat) di text menggunakan regex heuristik.
    Mengembalikan list of (lon, lat) atau (lat, lon) tergantung deteksi.
    """
    out = []
    if not text:
        return out
    text = normalize_text(text)

    # Pola: angka desimal dengan optional minus, lebih dari 2 digit sebelum titik sering longitude
    pattern = r"(-?\d{1,3}\.\d+)[^\d\-\.,]+(-?\d{1,3}\.\d+)"
    for m in re.finditer(pattern, text):
        a, b = m.group(1), m.group(2)
        try:
            a_f, b_f = float(a), float(b)
            # Deteksi urutan berdasarkan rentang Indonesia
            if 90 <= abs(a_f) <= 145 and -11 <= b_f <= 6:
                out.append((a_f, b_f))
            elif 90 <= abs(b_f) <= 145 and -11 <= a_f <= 6:
                out.append((b_f, a_f))
            else:
                # Tambahkan tentatif untuk manual review
                out.append((a_f, b_f))
        except:
            continue
    return out


if uploaded_pkkpr:
    if uploaded_pkkpr.name.endswith('.pdf'):
        coords_disetujui, coords_dimohon, coords_plain = [], [], []
        full_text = ""
        blok_aktif = None

        try:
            with pdfplumber.open(uploaded_pkkpr) as pdf:
                for page_idx, page in enumerate(pdf.pages, start=1):
                    text = page.extract_text() or ""
                    full_text += "\n" + text

                    # Deteksi blok aktif dari line-based scan (lebih toleran)
                    for line in text.split('\n'):
                        low = line.lower()
                        if 'disetujui' in low and 'koordinat' in low:
                            blok_aktif = 'disetujui'
                        elif 'dimohon' in low or 'dimohonkan' in low:
                            blok_aktif = 'dimohon'

                    # 1) Coba ekstraksi tabel lewat extract_tables()
                    tables = page.extract_tables() or []
                    if DEBUG:
                        st.write(f"Halaman {page_idx} - tabel terdeteksi: {len(tables)}")

                    for tb in tables:
                        if not tb or len(tb) <= 1:
                            continue
                        header = [str(c).lower().strip() if c else '' for c in tb[0]]

                        # Cari kolom bujur/lintang dengan keyword lebih toleran
                        lon_keywords = ['bujur', 'longitude', 'x', 'bt', 'btu']
                        lat_keywords = ['lintang', 'latitude', 'y', 'ls', 'lt']

                        idx_lon = next((i for i, h in enumerate(header) if any(k in h for k in lon_keywords)), -1)
                        idx_lat = next((i for i, h in enumerate(header) if any(k in h for k in lat_keywords)), -1)

                        # Fallback jika header tidak jelas: coba kolom 1 & 2
                        if idx_lon == -1 or idx_lat == -1:
                            if len(tb[0]) >= 2:
                                idx_lon, idx_lat = 1, 2 if len(tb[0]) > 2 else (0, 1)

                        # Iterasi baris
                        for row in tb[1:]:
                            cleaned_row = [str(cell).strip() if cell is not None else '' for cell in row]
                            # Ambil sel yang relevan jika ada
                            try:
                                lon_str = cleaned_row[idx_lon]
                                lat_str = cleaned_row[idx_lat]
                            except Exception:
                                # Kalau indeks salah, skip
                                continue

                            lon_val = parse_coordinate(lon_str)
                            lat_val = parse_coordinate(lat_str)

                            # Validasi rentang (long Indonesia positive ~90-145, lat ~-11..6)
                            is_lon_valid = lon_val is not None and 90 <= abs(lon_val) <= 145
                            is_lat_valid = lat_val is not None and -11 <= lat_val <= 6

                            # Koreksi bila terbalik
                            if not (is_lon_valid and is_lat_valid) and lon_val is not None and lat_val is not None:
                                is_lon_valid_rev = abs(lat_val) >= 90 and -11 <= lon_val <= 6
                                is_lat_valid_rev = 90 <= abs(lon_val) <= 145 and -11 <= lat_val <= 6
                                if is_lon_valid_rev and is_lat_valid_rev:
                                    lon_val, lat_val = lat_val, lon_val
                                    is_lon_valid = True
                                    is_lat_valid = True

                            # Jika valid, masukkan ke target sesuai blok
                            if is_lon_valid and is_lat_valid:
                                target = coords_disetujui if blok_aktif == 'disetujui' else (coords_dimohon if blok_aktif == 'dimohon' else coords_plain)
                                target.append((lon_val, lat_val))
                            else:
                                if DEBUG:
                                    st.write('Baris tidak valid atau tidak ter-parse:', cleaned_row)

                    # 2) Fallback: cari pasangan angka pada teks halaman
                    found_pairs = extract_coords_from_text(text)
                    if found_pairs:
                        # Tambahkan ke coords_plain jika belum ada
                        for pair in found_pairs:
                            coords_plain.append(pair)

            # Setelah iterasi halaman, jika tidak ada coords dari tabel coba dari full_text
            if not any([coords_disetujui, coords_dimohon, coords_plain]):
                if DEBUG:
                    st.write('Tidak ditemukan via tabel, mencoba ekstraksi teks penuh...')
                text_pairs = extract_coords_from_text(full_text)
                coords_plain.extend(text_pairs)

            # Prioritas: disetujui > dimohon > plain
            if coords_disetujui:
                coords, coords_label = coords_disetujui, 'disetujui'
            elif coords_dimohon:
                coords, coords_label = coords_dimohon, 'dimohon'
            elif coords_plain:
                # Hapus duplikat sambil menjaga urutan
                seen = set()
                uniq = []
                for c in coords_plain:
                    if c not in seen:
                        seen.add(c)
                        uniq.append(c)
                coords, coords_label = uniq, 'titik unik ditemukan'
            else:
                coords_label = 'tidak ditemukan'

            luas_pkkpr_doc, luas_pkkpr_doc_label = parse_luas_from_text(full_text)

            # Siapkan GeoDataFrame jika coords ada
            coords = list(dict.fromkeys(coords))
            if coords:
                # Pastikan polygon tertutup
                flipped_coords = coords.copy()
                if len(flipped_coords) > 1 and flipped_coords[0] != flipped_coords[-1]:
                    flipped_coords.append(flipped_coords[0])

                gdf_points = gpd.GeoDataFrame(
                    pd.DataFrame(flipped_coords, columns=["Longitude", "Latitude"]),
                    geometry=[Point(xy) for xy in flipped_coords],
                    crs="EPSG:4326"
                )
                try:
                    gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(flipped_coords)], crs="EPSG:4326")
                except Exception as e:
                    st.error(f"Gagal membuat polygon dari koordinat: {e}")
                    gdf_polygon = None

            with col2:
                st.markdown(f"<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ {len(coords)} titik ({coords_label})</p>", unsafe_allow_html=True)

        except Exception as e:
            st.error(f"Gagal memproses PDF: {e}")
            if DEBUG:
                st.exception(e)
            gdf_polygon = None

    elif uploaded_pkkpr.name.endswith('.zip'):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()), 'r')
                zip_ref.extractall(temp_dir)
                zip_ref.close()

                # Bacaan shapefile bisa menghasilkan beberapa layer; cari yg geometry
                gdf_polygon = gpd.read_file(temp_dir)
                if gdf_polygon.crs is None:
                    gdf_polygon.set_crs(epsg=4326, inplace=True)

                with col2:
                    st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ Shapefile (PKKPR)</p>", unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Gagal membaca shapefile PKKPR: {e}")
            if DEBUG:
                st.exception(e)
            gdf_polygon = None

# Jika tidak ada file diupload, set None
else:
    gdf_polygon = None

# ---
# Hasil Konversi dan Analisis PKKPR
if 'gdf_polygon' in locals() and gdf_polygon is not None:
    # Download SHP PKKPR
    try:
        zip_pkkpr_bytes = save_shapefile(gdf_polygon)
        st.download_button(
            "⬇️ Download SHP PKKPR (ZIP)",
            zip_pkkpr_bytes,
            "PKKPR_Hasil_Konversi.zip",
            mime="application/zip"
        )
    except Exception as e:
        st.error(f"Gagal menyiapkan unduhan shapefile: {e}")

    # Analisis Luas PKKPR
    try:
        gdf_polygon_proj = gdf_polygon.to_crs(epsg=4326)
        centroid = gdf_polygon_proj.geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)

        luas_pkkpr_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
        luas_pkkpr_mercator = gdf_polygon.to_crs(epsg=3857).area.sum()

        luas_doc_str = f"{luas_pkkpr_doc} ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "tidak tersedia (dokumen)"
        st.info(
            f"**Analisis Luas Batas PKKPR**:\n"
            f"- Luas PKKPR (dokumen): **{luas_doc_str}**\n"
            f"- Luas PKKPR (UTM {utm_zone}): **{format_angka_id(luas_pkkpr_utm)} m²**\n"
            f"- Luas PKKPR (WGS 84 Mercator/EPSG:3857): **{format_angka_id(luas_pkkpr_mercator)} m²**"
        )
    except Exception as e:
        st.error(f"Gagal menghitung luas: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# ================================
# === Upload Tapak Proyek (SHP) ===
# ================================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"] , key='tapak')

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

# ---
# Analisis Overlay Tapak dan PKKPR
if 'gdf_polygon' in locals() and gdf_polygon is not None and gdf_tapak is not None:
    try:
        gdf_tapak_proj = gdf_tapak.to_crs(epsg=4326)
        centroid = gdf_tapak_proj.geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)

        gdf_tapak_utm, gdf_polygon_utm = gdf_tapak.to_crs(epsg=utm_epsg), gdf_polygon.to_crs(epsg=utm_epsg)
        luas_tapak_utm, luas_pkkpr = gdf_tapak_utm.area.sum(), gdf_polygon_utm.area.sum()
        luas_tapak_mercator = gdf_tapak.to_crs(epsg=3857).area.sum()

        # Intersection with robust handling
        try:
            inter = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how='intersection')
            luas_overlap = inter.area.sum() if not inter.empty else 0
        except Exception:
            # Fallback intersection manual
            inter = gdf_tapak_utm.geometry.intersection(gdf_polygon_utm.unary_union)
            luas_overlap = sum([g.area for g in inter if not g.is_empty])

        luas_outside = luas_tapak_utm - luas_overlap

        st.success(
            "**HASIL ANALISIS OVERLAY TAPAK PROYEK:**\n"
            f"- Total Luas Tapak Proyek (UTM {utm_zone}): **{format_angka_id(luas_tapak_utm)} m²**\n"
            f"- Total Luas Tapak Proyek (WGS 84 Mercator/EPSG:3857): **{format_angka_id(luas_tapak_mercator)} m²**\n"
            f"- Luas Tapak di dalam PKKPR (Overlap): **{format_angka_id(luas_overlap)} m²**\n"
            f"- Luas Tapak di luar PKKPR (Outside): **{format_angka_id(luas_outside)} m²**"
        )
    except Exception as e:
        st.error(f"Gagal analisis overlay: {e}")
        if DEBUG:
            st.exception(e)
    st.markdown("---")

# ---
# Preview Peta Interaktif (Folium)
if 'gdf_polygon' in locals() and gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")

    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None, attr='')
    Fullscreen(position="bottomleft").add_to(m)

    folium.TileLayer("openstreetmap", name="OpenStreetMap", attr='').add_to(m)
    folium.TileLayer("CartoDB Positron", name="CartoDB Positron", attr='').add_to(m)
    folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery", attr='').add_to(m)

    folium.GeoJson(gdf_polygon.to_crs(epsg=4326), name="Batas PKKPR", style_function=lambda x: {"color": "yellow", "weight": 3, "fillOpacity": 0.1}).add_to(m)

    if gdf_tapak is not None:
        folium.GeoJson(gdf_tapak.to_crs(epsg=4326), name="Tapak Proyek", style_function=lambda x: {"color": "red", "weight": 2, "fillColor": "red", "fillOpacity": 0.4}).add_to(m)

    if 'gdf_points' in locals() and gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker([row.geometry.y, row.geometry.x], radius=4, color="black", fill=True, fill_color="orange", fill_opacity=1, popup=f"Titik {i+1}").add_to(m)

    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, width=900, height=600)
    st.markdown("---")

# ---
# Layout Peta (PNG)
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

        ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)

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

