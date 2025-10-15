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
st.set_page_config(page_title="PKKPR → SHP & Overlay", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")
st.markdown("---")

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
        temp_shp_path = os.path.join(temp_dir, "PKKPR_Output.shp")
        gdf.to_crs(epsg=4326).to_file(temp_shp_path)
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in os.listdir(temp_dir):
                file_path = os.path.join(temp_dir, file)
                zf.write(file_path, arcname=file)
        
        zip_buffer.seek(0)
        return zip_buffer.read()

def parse_coordinate(coord_str):
    """
    Fungsi universal Paling Robust untuk mengkonversi string koordinat (DMS atau Desimal) ke nilai float desimal.
    Fokus pada penanganan format DMS yang tidak baku/terpadu.
    """
    if not coord_str:
        return None

    coord_str = coord_str.strip()
    clean_str = coord_str.replace(" ", "").replace(",", ".")
    clean_str = re.sub(r'[\$\{\}\\\^"]', '', clean_str) 
    clean_str = clean_str.replace("'", "'").replace("°", "d").replace('"', "s")
    
    # 1. Coba parse sebagai DMS standar (contoh: 104d57'40.000sBT)
    m_dms_std = re.match(r"(\d+)d?(\d+)'([\d\.]+)(s)?([A-Za-z]+)?", clean_str, re.IGNORECASE)
    if m_dms_std:
        try:
            deg, minute, second, _, direction = m_dms_std.groups()
            decimal = float(deg) + float(minute) / 60 + float(second) / 3600
            # Handle direction (S, W, LS, BB)
            if direction and direction.upper() in ["S", "LS", "W", "BB"]:
                decimal *= -1
            return decimal
        except (ValueError, TypeError):
            pass 
            
    # 2. Coba parse sebagai Desimal Murni (contoh: 98.24312 atau -2.6378)
    try:
        # Hapus semua karakter non-angka/titik/minus kecuali minus (-)
        decimal_str_clean = re.sub(r'[^\d\.\-]', '', clean_str)
        decimal = float(decimal_str_clean)
        
        # Jika angka ini sangat besar (> 180), kemungkinan besar itu adalah DMS tanpa pemisah (contoh: 1045740.000)
        if abs(decimal) > 180: 
             # Coba paksa interpretasi DMS (DDDMMSS.sss atau DDMMSS.sss)
             num_part = decimal_str_clean.split('.')[0]
             
             if len(num_part) >= 7 and num_part.isdigit(): # Pola DDDMMSS (mis. 1045740)
                 deg = float(num_part[:3])
                 minute = float(num_part[3:5])
                 
                 # Detik: sisanya termasuk pecahan setelah titik desimal
                 second_str_with_decimal = num_part[5:]
                 if '.' in decimal_str_clean:
                     second_str_with_decimal += '.' + decimal_str_clean.split('.', 1)[1]
                 
                 try:
                    second = float(second_str_with_decimal)
                    
                    if 0 <= deg <= 180 and 0 <= minute <= 60:
                        # Tambahkan logic arah jika ada (contoh: 1045740.000BT)
                        direction_match = re.search(r"[NSEWBS]$", coord_str, re.IGNORECASE)
                        dms_decimal = deg + minute / 60 + second / 3600
                        if direction_match and direction_match.group(0).upper() in ["S", "LS", "W", "BB"]:
                             dms_decimal *= -1
                        return dms_decimal
                 except:
                     pass
        
        # Jika lolos cek besar/DMS, kembalikan sebagai desimal
        return decimal
        
    except ValueError:
        pass
        
    # 3. Coba parse sebagai DMS yang sangat padat tanpa simbol (jika desimal murni gagal)
    # Ini adalah upaya terakhir jika semua gagal.
    if len(clean_str.split('.')[0]) >= 7:
        try:
            num_str = clean_str.split('.')[0]
            # Pola: DDDMMSS (3-2-2)
            deg = float(num_str[:3])
            minute = float(num_str[3:5])
            second = float(num_str[5:7]) if len(num_str) >= 7 else 0
            
            if 0 <= deg <= 180 and 0 <= minute <= 60:
                 return deg + minute / 60 + second / 3600
        except:
             pass

    return None

def validate_and_fix_coords(lon_val, lat_val):
    """Memvalidasi dan memperbaiki pasangan koordinat untuk Indonesia."""
    
    is_lon_valid = lon_val is not None and 90 <= lon_val <= 145
    is_lat_valid = lat_val is not None and -11 <= lat_val <= 6
    
    if is_lon_valid and is_lat_valid:
        return lon_val, lat_val, False # Lon, Lat, Not Flipped

    # --- Typo Correction Logic (Kehilangan digit di Longitude) ---
    if lon_val is not None and lat_val is not None:
        
        # 1. Deteksi Longitude yang kehilangan digit '9' atau '10'
        if 8 < lon_val < 10 and -11 <= lat_val <= 6: 
            lon_fixed = lon_val + 90 
            if 90 <= lon_fixed <= 145:
                return lon_fixed, lat_val, False 
        elif 6 < lon_val < 10 and -11 <= lat_val <= 6:
            lon_fixed = lon_val + 100
            if 90 <= lon_fixed <= 145:
                return lon_fixed, lat_val, False
        
        # 2. Cek Pasangan Terbalik (Lat, Long)
        is_lon_valid_rev = lat_val is not None and 90 <= lat_val <= 145 
        is_lat_valid_rev = lon_val is not None and -11 <= lon_val <= 6 
        
        if is_lon_valid_rev and is_lat_valid_rev:
            return lat_val, lon_val, True # Longitude dan Latitude Dibalik, Flipped

    return None, None, False # Invalid


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

if uploaded_pkkpr:
    if uploaded_pkkpr.name.endswith(".pdf"):
        coords_disetujui, coords_dimohon, coords_plain = [], [], []
        full_text, blok_aktif = None, None
        
        try:
            with pdfplumber.open(uploaded_pkkpr) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    full_text = (full_text or "") + "\n" + text

                    # Logika penentuan blok "disetujui" atau "dimohon"
                    for line in text.split("\n"):
                        low = line.lower()
                        if "tabel koordinat yang disetujui" in low:
                            blok_aktif = "disetujui"
                        elif "tabel koordinat yang dimohonkan" in low:
                            blok_aktif = "dimohon"

                    # Parsing Tabel Koordinat
                    for tb in (page.extract_tables() or []):
                        if len(tb) <= 1: 
                            continue 

                        for row in tb[1:]: # Iterasi baris data
                            
                            cleaned_cells = [str(cell).strip() for cell in row if cell]
                            
                            if len(cleaned_cells) < 2:
                                continue
                            
                            found_valid_pair = False
                            target = {"disetujui": coords_disetujui, "dimohon": coords_dimohon}.get(blok_aktif, coords_plain)
                            
                            # Logika 1: Coba semua pasangan kolom berdekatan
                            for i in range(len(cleaned_cells) - 1):
                                val1 = parse_coordinate(cleaned_cells[i])
                                val2 = parse_coordinate(cleaned_cells[i+1])
                                
                                lon_fixed, lat_fixed, is_flipped = validate_and_fix_coords(val1, val2)
                                
                                if lon_fixed is not None:
                                    target.append((lon_fixed, lat_fixed))
                                    found_valid_pair = True
                                    break 

                            # Logika 2: Coba pasangkan 2 kolom terakhir (No., Long, Lat)
                            if not found_valid_pair and len(cleaned_cells) >= 3:
                                lon_val = parse_coordinate(cleaned_cells[-2])
                                lat_val = parse_coordinate(cleaned_cells[-1])
                                
                                lon_fixed, lat_fixed, is_flipped = validate_and_fix_coords(lon_val, lat_val)
                                
                                if lon_fixed is not None:
                                    target.append((lon_fixed, lat_fixed))
                                    found_valid_pair = True
                                
                # --- Sisa Logika Setelah Parsing (PRIORITAS & GEODATAFRAME) ---
                if coords_disetujui:
                    coords, coords_label = coords_disetujui, "disetujui"
                elif coords_dimohon:
                    coords, coords_label = coords_dimohon, "dimohon"
                elif coords_plain:
                    coords, coords_label = coords_plain, "titik unik ditemukan"
                else:
                    coords_label = "tidak ditemukan"

                luas_pkkpr_doc, luas_pkkpr_doc_label = parse_luas_from_text(full_text)
                
                coords = list(dict.fromkeys(coords)) 
                
                # --- AUTO-FLIP GLOBAL (MENGATASI KESALAHAN LOKASI) ---
                if coords:
                    temp_lon = [c[0] for c in coords]
                    temp_lat = [c[1] for c in coords]
                    
                    # Cek apakah rata-rata Longitude di luar batas Indonesia (Lon 90-145)
                    avg_lon = sum(temp_lon) / len(temp_lon)
                    
                    if not (90 <= avg_lon <= 145):
                        # Jika rata-rata koordinat berada di luar Indonesia, coba balikkan urutan
                        coords_flipped = [(c[1], c[0]) for c in coords]
                        avg_lon_flipped = sum([c[0] for c in coords_flipped]) / len(coords_flipped)

                        if 90 <= avg_lon_flipped <= 145:
                            coords = coords_flipped
                            coords_label += " (di-flip)"
                            
                    flipped_coords = coords
                    
                    if len(flipped_coords) > 1 and flipped_coords[0] != flipped_coords[-1]:
                        flipped_coords.append(flipped_coords[0])

                    gdf_points = gpd.GeoDataFrame(
                        pd.DataFrame(flipped_coords, columns=["Longitude", "Latitude"]),
                        geometry=[Point(xy) for xy in flipped_coords],
                        crs="EPSG:4326"
                    )
                    gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(flipped_coords)], crs="EPSG:4326")

                with col2:
                    st.markdown(f"<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ {len(coords)} titik ({coords_label})</p>", unsafe_allow_html=True)

        except Exception as e:
            st.error(f"Gagal memproses PDF: {e}")
            gdf_polygon = None
    
    # Penanganan Shapefile PKKPR (ZIP)
    elif uploaded_pkkpr.name.endswith(".zip"):
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
            gdf_polygon = None


# ---
## Hasil Konversi dan Analisis PKKPR
if gdf_polygon is not None:
    # --- Download SHP PKKPR ---
    zip_pkkpr_bytes = save_shapefile(gdf_polygon)
    st.download_button(
        "⬇️ Download SHP PKKPR (ZIP)", 
        zip_pkkpr_bytes,
        "PKKPR_Hasil_Konversi.zip", 
        mime="application/zip"
    )

    # --- Analisis Luas PKKPR ---
    gdf_polygon_proj = gdf_polygon.to_crs(epsg=4326)
    centroid = gdf_polygon_proj.geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    
    # Hitung Luas UTM
    luas_pkkpr_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
    
    # Hitung Luas WGS 84 Mercator (EPSG:3857)
    luas_pkkpr_mercator = gdf_polygon.to_crs(epsg=3857).area.sum() 
    
    luas_doc_str = f"{luas_pkkpr_doc} ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "484.071,60 M² (dokumen)"
    st.info(
        f"**Analisis Luas Batas PKKPR**:\n"
        f"- Luas PKKPR (dokumen): **{luas_doc_str}**\n"
        f"- Luas PKKPR (UTM {utm_zone}): **{format_angka_id(luas_pkkpr_utm)} m²**\n"
        f"- Luas PKKPR (WGS 84 Mercator/EPSG:3857): **{format_angka_id(luas_pkkpr_mercator)} m²**"
    )
    st.markdown("---")

# ================================
# === Upload Tapak Proyek (SHP) ===
# ================================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

gdf_tapak = None
if uploaded_tapak:
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()), 'r')
            zip_ref.extractall(temp_dir)
            zip_ref.close()
            
            gdf_tapak = gpd.read_file(temp_dir) 
            if gdf_tapak.crs is None:
                gdf_tapak.set_crs(epsg=4326, inplace=True)
                
            with col2:
                st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅</p>", unsafe_allow_html=True)
    except Exception as e:
        st.error(f"Gagal membaca shapefile Tapak Proyek: {e}")

# ---
## Analisis Overlay Tapak dan PKKPR
if gdf_polygon is not None and gdf_tapak is not None:
    # Tentukan CRS UTM berdasarkan centroid Tapak Proyek
    gdf_tapak_proj = gdf_tapak.to_crs(epsg=4326)
    centroid = gdf_tapak_proj.geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    
    # Proyeksikan kedua GeodataFrame ke UTM untuk hitungan area akurat
    gdf_tapak_utm, gdf_polygon_utm = gdf_tapak.to_crs(epsg=utm_epsg), gdf_polygon.to_crs(epsg=utm_epsg)
    luas_tapak_utm, luas_pkkpr = gdf_tapak_utm.area.sum(), gdf_polygon_utm.area.sum()
    
    # Hitung Luas WGS 84 Mercator (EPSG:3857) untuk Tapak Proyek
    luas_tapak_mercator = gdf_tapak.to_crs(epsg=3857).area.sum()
    
    # Hitung tumpang tindih (intersection)
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak_utm - luas_overlap
    
    st.success(
        "**HASIL ANALISIS OVERLAY TAPAK PROYEK:**\n"
        f"- Total Luas Tapak Proyek (UTM {utm_zone}): **{format_angka_id(luas_tapak_utm)} m²**\n"
        f"- Total Luas Tapak Proyek (WGS 84 Mercator/EPSG:3857): **{format_angka_id(luas_tapak_mercator)} m²**\n"
        f"- Luas Tapak di dalam PKKPR (Overlap): **{format_angka_id(luas_overlap)} m²**\n"
        f"- Luas Tapak di luar PKKPR (Outside): **{format_angka_id(luas_outside)} m²**\n"
    )
    st.markdown("---")

# ---
## 🌍 Preview Peta Interaktif (Folium)
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    
    # Inisialisasi peta Folium. Gunakan tiles=None dan attr='' untuk menghilangkan atribusi default.
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None, attr='')
    
    Fullscreen(position="bottomleft").add_to(m)
    
    # Menambahkan TileLayer tanpa atribusi (attr='')
    folium.TileLayer("openstreetmap", name="OpenStreetMap", attr='').add_to(m) 
    folium.TileLayer("CartoDB Positron", name="CartoDB Positron", attr='').add_to(m) 
    folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery", attr='').add_to(m) 
    
    # Plot PKKPR
    folium.GeoJson(gdf_polygon.to_crs(epsg=4326),
                    name="Batas PKKPR", style_function=lambda x: {"color": "yellow", "weight": 3, "fillOpacity": 0.1}).add_to(m)
    
    # Plot Tapak Proyek
    if gdf_tapak is not None:
        folium.GeoJson(gdf_tapak.to_crs(epsg=4326),
                         name="Tapak Proyek", style_function=lambda x: {"color": "red", "weight": 2, "fillColor": "red", "fillOpacity": 0.4}).add_to(m)
                        
    # Plot Titik PKKPR
    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker([row.geometry.y, row.geometry.x], radius=4, color="black",
                                 fill=True, fill_color="orange", fill_opacity=1, popup=f"Titik {i+1}").add_to(m)
                                
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, width=900, height=600)
    st.markdown("---")

# ---
## 🖼️ Layout Peta (PNG)
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG) untuk Dokumentasi")
    
    gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
    xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
    width, height = xmax - xmin, ymax - ymin
    
    fig, ax = plt.subplots(figsize=(14, 10) if width > height else (10, 14), dpi=150)
    
    # Plot PKKPR
    gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2.5, label="Batas PKKPR")
    
    # Plot Tapak Proyek
    if gdf_tapak is not None:
        gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
        gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red", label="Tapak Proyek")
    
    # Plot Titik
    if gdf_points is not None:
        gdf_points_3857 = gdf_points.to_crs(epsg=3857)
        gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=30, label="Titik PKKPR")
        
    # Tambahkan Basemap
    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)
    
    ax.set_xlim(xmin - width*0.05, xmax + width*0.05)
    ax.set_ylim(ymin - height*0.05, ymax + height*0.05)
    
    # Legenda
    legend = [
        mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black", linestyle="None", markersize=5, label="PKKPR (Titik)"),
        mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=1.5, label="PKKPR (Polygon)"),
        mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
    ]
    ax.legend(handles=legend, title="Legenda", loc="upper right", fontsize=8, title_fontsize=9)
    ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14, weight="bold")
    ax.set_axis_off()
    
    # Simpan ke buffer memori
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
