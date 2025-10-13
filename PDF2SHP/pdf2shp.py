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


def dms_to_decimal(dms_str):
    """Konversi koordinat DMS (° ' " + BT/BB/LS/LU) ke desimal."""
    if not dms_str:
        return None
    dms_str = dms_str.strip().replace(" ", "").replace(",", ".")
    m = re.match(r"(\d+)[°](\d+)'([\d\.]+)\"?([A-Za-z]+)", dms_str)
    if not m:
        return None
    deg, minute, second, direction = m.groups()
    decimal = float(deg) + float(minute) / 60 + float(second) / 3600
    if direction.upper() in ["S", "LS", "W", "BB"]:
        decimal *= -1
    return decimal


def parse_luas_from_text(text):
    """Ambil teks luas tanah dari dokumen."""
    text_clean = re.sub(r"\s+", " ", (text or ""), flags=re.IGNORECASE)
    luas_matches = re.findall(
        r"luas\s*tanah\s*yang\s*(dimohon|disetujui)\s*[:\-]?\s*([\d\.,]+\s*(M2|M²))",
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
        m = re.search(r"luas\s*tanah\s*[:\-]?\s*([\d\.,]+\s*(M2|M²))", text_clean, re.IGNORECASE)
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
        full_text, blok_aktif = "", None
        
        try:
            with pdfplumber.open(uploaded_pkkpr) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    full_text += "\n" + text

                    # Logika penentuan blok "disetujui" atau "dimohon"
                    for line in text.split("\n"):
                        low = line.lower()
                        if "koordinat" in low and "disetujui" in low:
                            blok_aktif = "disetujui"
                        elif "koordinat" in low and "dimohon" in low:
                            blok_aktif = "dimohon"

                        # 1. Parsing Baris Per Baris (DMS)
                        dms_parts = re.findall(r"\d+°\s*\d+'\s*[\d\.,]+\"\s*[A-Za-z]+", line)
                        if len(dms_parts) >= 2:
                            lon, lat = dms_to_decimal(dms_parts[0]), dms_to_decimal(dms_parts[1])
                            if lon and lat and 90 <= lon <= 145 and -11 <= lat <= 6:
                                target = {"disetujui": coords_disetujui, "dimohon": coords_dimohon}.get(blok_aktif, coords_plain)
                                target.append((lon, lat))
                            continue
                            
                    # 2. Parsing Tabel Koordinat (Fokus pada Desimal Murni) (Perbaikan Kritis)
                    for tb in (page.extract_tables() or []):
                        if len(tb) <= 1: 
                            continue 

                        header = [str(c).lower().strip() for c in tb[0] if c]
                        
                        idx_lon, idx_lat = -1, -1
                        
                        # Logika untuk mencari kolom Longitude dan Latitude berdasarkan header
                        try:
                            # Prioritas 1: Cari "bujur" dan "lintang"
                            idx_lon = next(i for i, h in enumerate(header) if "bujur" in h or "longitude" in h)
                            idx_lat = next(i for i, h in enumerate(header) if "lintang" in h or "latitude" in h)
                        except StopIteration:
                            # Prioritas 2: Fallback (Asumsi No, Long/Lat, Lat/Long). Ambil kolom 1 dan 2 setelah 'No.'
                            if len(header) >= 3 and any(h in header for h in ["no.", "nomor"]): 
                                if len(header) > 2:
                                    # Default untuk kasus seperti file Anda (indeks 1 dan 2)
                                    idx_lon, idx_lat = 1, 2
                            elif len(header) == 2:
                                # Cuma 2 kolom
                                idx_lon, idx_lat = 0, 1

                        # Jika indeks kolom ditemukan, coba ekstrak data
                        if idx_lon != -1 and idx_lat != -1 and len(header) > max(idx_lon, idx_lat):
                            for row in tb[1:]: # Iterasi baris data
                                if len(row) > max(idx_lon, idx_lat) and row[idx_lon] and row[idx_lat]:
                                    try:
                                        # Hapus koma desimal dan ganti dengan titik, lalu bersihkan karakter lain
                                        lon_str = str(row[idx_lon]).strip().replace(",", ".")
                                        lat_str = str(row[idx_lat]).strip().replace(",", ".")
                                        
                                        # Hapus semua karakter non-angka/titik/minus (Pembersihan Kritis)
                                        lon_val = float(re.sub(r'[^\d\.\-]', '', lon_str))
                                        lat_val = float(re.sub(r'[^\d\.\-]', '', lat_str))
                                        
                                        # VALIDASI PENTING (Bujur/Longitude, Lintang/Latitude)
                                        is_lon_valid = 90 <= lon_val <= 145
                                        is_lat_valid = -11 <= lat_val <= 6
                                        
                                        if is_lon_valid and is_lat_valid:
                                            coords_plain.append((lon_val, lat_val))
                                            
                                        # Cek urutan terbalik (Lat, Long)
                                        is_lon_valid_rev = 90 <= lat_val <= 145 
                                        is_lat_valid_rev = -11 <= lon_val <= 6  
                                        
                                        if is_lon_valid_rev and is_lat_valid_rev:
                                            coords_plain.append((lat_val, lon_val)) # Simpan sebagai (Long, Lat)
                                            
                                    except ValueError:
                                        pass
                        
                        # Logika untuk parsing DMS di baris tabel (jaga-jaga)
                        for row in tb[1:]:
                            if not row: continue
                            row_join = " ".join([str(x) for x in row if x])
                            dms_parts = re.findall(r"\d+°\s*\d+'\s*[\d\.,]+\"\s*[A-Za-z]+", row_join)
                            if len(dms_parts) >= 2:
                                lon, lat = dms_to_decimal(dms_parts[0]), dms_to_decimal(dms_parts[1])
                                if lon and lat and 90 <= lon <= 145 and -11 <= lat <= 6:
                                    coords_plain.append((lon, lat))

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

            if coords:
                fx, fy = coords[0]
                # Koreksi: Jika koordinat pertama menunjukkan urutan (Lat, Long), balikkan
                flipped_coords = [(y, x) for x, y in coords] if -11 <= fx <= 6 and 90 <= fy <= 145 else coords
                flipped_coords = list(dict.fromkeys(flipped_coords))
                
                # Logic untuk menutup poligon: 
                # Jika titik akhir tidak sama dengan titik awal, tambahkan titik awal sebagai titik penutup.
                if len(flipped_coords) > 1 and flipped_coords[0] != flipped_coords[-1]:
                    flipped_coords.append(flipped_coords[0])

                gdf_points = gpd.GeoDataFrame(
                    pd.DataFrame(flipped_coords, columns=["Longitude", "Latitude"]),
                    geometry=[Point(xy) for xy in flipped_coords],
                    crs="EPSG:4326"
                )
                gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(flipped_coords)], crs="EPSG:4326")

            with col2:
                # Perhatikan label yang ditampilkan di sini adalah jumlah titik unik
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
    
    luas_pkkpr_hitung = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
    luas_doc_str = f"{luas_pkkpr_doc} ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(
        f"**Analisis Luas Batas PKKPR** (WGS 84 / Zona UTM {utm_zone}):\n"
        f"- Luas PKKPR (dokumen): **{luas_doc_str}**\n"
        f"- Luas PKKPR (Hitungan Geospasial): **{format_angka_id(luas_pkkpr_hitung)} m²**"
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

# ---
## Analisis Overlay Tapak dan PKKPR
if gdf_polygon is not None and gdf_tapak is not None:
    # Tentukan CRS UTM berdasarkan centroid Tapak Proyek
    gdf_tapak_proj = gdf_tapak.to_crs(epsg=4326)
    centroid = gdf_tapak_proj.geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    
    # Proyeksikan kedua GeodataFrame ke UTM untuk hitungan area akurat
    gdf_tapak_utm, gdf_polygon_utm = gdf_tapak.to_crs(epsg=utm_epsg), gdf_polygon.to_crs(epsg=utm_epsg)
    luas_tapak, luas_pkkpr = gdf_tapak_utm.area.sum(), gdf_polygon_utm.area.sum()
    
    # Hitung tumpang tindih (intersection)
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak - luas_overlap
    
    st.success(
        "**HASIL ANALISIS OVERLAY TAPAK PROYEK:**\n"
        f"- Total Luas Tapak Proyek: **{format_angka_id(luas_tapak)} m²**\n"
        f"- Luas Tapak di dalam PKKPR (Overlap): **{format_angka_id(luas_overlap)} m²**\n"
        f"- Luas Tapak di luar PKKPR (Outside): **{format_angka_id(luas_outside)} m²**\n"
    )
    st.markdown("---")

# ---
## 🌍 Preview Peta Interaktif (Folium)
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17)
    
    Fullscreen(position="bottomleft").add_to(m)
    folium.TileLayer("openstreetmap").add_to(m)
    
    # Tile yang aman
    folium.TileLayer("CartoDB Positron").add_to(m) 
    folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery").add_to(m)
    
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
