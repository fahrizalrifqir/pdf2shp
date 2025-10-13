import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, shutil, re
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
import tempfile # PENTING: Import tempfile untuk manajemen file aman

# ======================
# === Konfigurasi App ===
# ======================
st.set_page_config(page_title="PKKPR → SHP + Overlay", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")

# ======================
# === Fungsi Helper ===
# ======================
def get_utm_info(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    zone_label = f"{zone}{'N' if lat >= 0 else 'S'}"
    return epsg, zone_label


# FUNGSI BARU: Menggunakan io.BytesIO dan tempfile untuk lingkungan cloud
def save_shapefile(gdf):
    """Menyimpan GeoDataFrame ke ZIP Shapefile di memory buffer."""
    
    # 1. Membuat direktori sementara
    with tempfile.TemporaryDirectory() as temp_dir:
        # 2. Menyimpan GeoDataFrame ke Shapefile di direktori sementara
        # Geopandas akan membuat semua file SHP (shp, shx, dbf, prj, etc.) di sini.
        temp_shp_path = os.path.join(temp_dir, "data.shp")
        gdf.to_file(temp_shp_path)
        
        # 3. Membuat ZIP file di buffer memori
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            # Iterasi file-file yang baru dibuat oleh GeoPandas
            for file in os.listdir(temp_dir):
                file_path = os.path.join(temp_dir, file)
                # Menulis file ke dalam ZIP dari lokasi sementara, dengan nama file aslinya
                zf.write(file_path, arcname=file)
        
        zip_buffer.seek(0)
        return zip_buffer.read() # Mengembalikan konten zip sebagai bytes


def dms_to_decimal(dms_str):
    """Konversi koordinat DMS (° ' " + BT/BB/LS/LU) ke desimal, dukung koma/titik."""
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
    """Ambil teks luas tanah dari dokumen apa adanya, prioritas: disetujui → dimohon → tanpa judul."""
    text_clean = re.sub(r"\s+", " ", (text or ""), flags=re.IGNORECASE)
    luas_matches = re.findall(
        r"luas\s*tanah\s*yang\s*(dimohon|disetujui)\s*[:\-]?\s*([\d\.,]+\s*(m2|m²))",
        text_clean,
        re.IGNORECASE
    )
    if not luas_matches:
        return None, "tanpa judul"
    luas_data = {}
    for label, value, satuan in luas_matches:
        luas_data[label.lower()] = (value.strip().upper() if value else "").replace(" ", "")
    if "disetujui" in luas_data:
        return luas_data["disetujui"], "disetujui"
    elif "dimohon" in luas_data:
        return luas_data["dimohon"], "dimohon"
    else:
        return None, "tanpa judul"


def format_angka_id(value):
    try:
        val = float(value)
        if val >= 1000:
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
    # Menggunakan uploaded_pkkpr.read() hanya sekali jika perlu
    if uploaded_pkkpr.name.endswith(".pdf"):
        # Logika parsing PDF Anda (Tidak perlu io.BytesIO)
        coords_disetujui, coords_dimohon, coords_plain = [], [], []
        full_text, blok_aktif = "", None
        
        # pdfplumber dapat membaca objek file yang diunggah secara langsung
        try:
            with pdfplumber.open(uploaded_pkkpr) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    full_text += "\n" + text

                    # ... (Semua logika parsing koordinat PDF Anda di sini) ...
                    for line in text.split("\n"):
                        low = line.lower()
                        if "koordinat" in low and "disetujui" in low:
                            blok_aktif = "disetujui"
                        elif "koordinat" in low and "dimohon" in low:
                            blok_aktif = "dimohon"

                        # Format DMS
                        dms_parts = re.findall(r"\d+°\s*\d+'\s*[\d\.,]+\"\s*[A-Za-z]+", line)
                        if len(dms_parts) >= 2:
                            lon, lat = dms_to_decimal(dms_parts[0]), dms_to_decimal(dms_parts[1])
                            if lon and lat and 90 <= lon <= 145 and -11 <= lat <= 6:
                                target = {"disetujui": coords_disetujui, "dimohon": coords_dimohon}.get(blok_aktif, coords_plain)
                                target.append((lon, lat))
                            continue

                        # Format desimal
                        mline = re.findall(r"[-+]?\d+[.,]\d+", line)
                        if len(mline) >= 2:
                            try:
                                lon, lat = float(mline[0].replace(",", ".")), float(mline[1].replace(",", "."))
                                if 90 <= lon <= 145 and -11 <= lat <= 6:
                                    target = {"disetujui": coords_disetujui, "dimohon": coords_dimohon}.get(blok_aktif, coords_plain)
                                    target.append((lon, lat))
                            except:
                                pass

                    # Tabel koordinat
                    for tb in (page.extract_tables() or []):
                        for row in tb:
                            if not row:
                                continue
                            row_join = " ".join([str(x) for x in row if x])
                            parts = re.findall(r"\d+°\s*\d+'\s*[\d\.,]+\"\s*[A-Za-z]+", row_join)
                            if len(parts) >= 2:
                                lon, lat = dms_to_decimal(parts[0]), dms_to_decimal(parts[1])
                                if lon and lat and 90 <= lon <= 145 and -11 <= lat <= 6:
                                    coords_plain.append((lon, lat))
                                continue
                            nums = re.findall(r"[-+]?\d+[.,]\d+", row_join)
                            if len(nums) >= 2:
                                try:
                                    lon, lat = float(nums[0].replace(",", ".")), float(nums[1].replace(",", "."))
                                    if 90 <= lon <= 145 and -11 <= lat <= 6:
                                        coords_plain.append((lon, lat))
                                except:
                                    pass

            # Prioritas koordinat
            if coords_disetujui:
                coords, coords_label = coords_disetujui, "disetujui"
            elif coords_dimohon:
                coords, coords_label = coords_dimohon, "dimohon"
            elif coords_plain:
                coords, coords_label = coords_plain, "tanpa judul"
            else:
                coords_label = "tidak ditemukan"

            luas_pkkpr_doc, luas_pkkpr_doc_label = parse_luas_from_text(full_text)
            coords = list(dict.fromkeys(coords))

            # Koreksi urutan lon-lat
            if coords:
                fx, fy = coords[0]
                flipped_coords = [(y, x) for x, y in coords] if -11 <= fx <= 6 and 90 <= fy <= 145 else coords
                flipped_coords = list(dict.fromkeys(flipped_coords))
                if flipped_coords[0] != flipped_coords[-1]:
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
    
    # BARU: Penanganan Shapefile PKKPR
    elif uploaded_pkkpr.name.endswith(".zip"):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # Membaca ZIP dari buffer
                zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()), 'r')
                zip_ref.extractall(temp_dir)
                zip_ref.close()
                
                # GeoPandas bisa membaca direktori secara langsung
                gdf_polygon = gpd.read_file(temp_dir)
                if gdf_polygon.crs is None:
                    gdf_polygon.set_crs(epsg=4326, inplace=True)
                
                with col2:
                    st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ Shapefile (PKKPR)</p>", unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Gagal membaca shapefile PKKPR: {e}")
            gdf_polygon = None


# === Ekspor SHP PKKPR ===
if gdf_polygon is not None:
    # Menggunakan fungsi save_shapefile yang mengembalikan bytes
    zip_pkkpr_bytes = save_shapefile(gdf_polygon)
    st.download_button(
        "⬇️ Download SHP PKKPR (ZIP)", 
        zip_pkkpr_bytes,
        "PKKPR_Hasil_Konversi.zip", 
        mime="application/zip"
    )

# === Analisis Luas PKKPR ===
if gdf_polygon is not None:
    # Tambahkan re-project sebelum centroid jika gdf_polygon berpotensi memiliki CRS geografis
    gdf_polygon_proj = gdf_polygon.to_crs(epsg=4326)
    centroid = gdf_polygon_proj.geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    
    luas_pkkpr_hitung = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
    luas_pkkpr_mercator = gdf_polygon.to_crs(epsg=3857).area.sum()
    luas_doc_str = f"{luas_pkkpr_doc} ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(
        f"- Luas PKKPR (dokumen): {luas_doc_str}\n"
        f"- Luas PKKPR (UTM Zona {utm_zone}): {format_angka_id(luas_pkkpr_hitung)} m²\n"
        f"- Luas PKKPR (proyeksi WGS 84 / Mercator): {format_angka_id(luas_pkkpr_mercator)} m²"
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
        # BARU: Menggunakan tempfile untuk Shapefile Tapak
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
        st.error(f"Gagal membaca shapefile: {e}")

# ======================
# === Analisis Overlay ===
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    # Tambahkan re-project sebelum centroid
    gdf_tapak_proj = gdf_tapak.to_crs(epsg=4326)
    centroid = gdf_tapak_proj.geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    
    gdf_tapak_utm, gdf_polygon_utm = gdf_tapak.to_crs(epsg=utm_epsg), gdf_polygon.to_crs(epsg=utm_epsg)
    luas_tapak, luas_pkkpr = gdf_tapak_utm.area.sum(), gdf_polygon_utm.area.sum()
    
    # Pastikan kedua GeoDataFrame di-proyeksikan ulang sebelum overlay
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak - luas_overlap
    luas_doc_str = f"{luas_pkkpr_doc} ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(
        "**Analisis Luas Tapak Proyek:**\n"
        f"- Total Luas Tapak Proyek: {format_angka_id(luas_tapak)} m²\n"
        f"- Luas PKKPR (dokumen): {luas_doc_str}\n"
        f"- Luas PKKPR (UTM Zona {utm_zone}): {format_angka_id(luas_pkkpr)} m²\n"
        f"- Luas Tapak di dalam PKKPR: **{format_angka_id(luas_overlap)} m²**\n"
        f"- Luas Tapak di luar PKKPR: **{format_angka_id(luas_outside)} m²**"
    )
    st.markdown("---")

# ======================
# === Preview Interaktif ===
# ======================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17)
    
    Fullscreen(position="bottomleft").add_to(m)
    
    # Tile 1: OpenStreetMap (Aman)
    folium.TileLayer("openstreetmap").add_to(m) 
    
    # Tile 2: Ganti "CartoDB positron" (lowercase) menjadi nama resmi Folium atau xyzservices (Aman)
    folium.TileLayer("CartoDB Positron").add_to(m) 
    
    # Tile 3: HAPUS "Stamen Terrain" yang menyebabkan ValueError.
    # Jika Anda ingin tile Terrain, gunakan xyzservices:
    # folium.TileLayer(xyz.Stamen.Terrain, name="Stamen Terrain (via xyzservices)").add_to(m)
    
    # Tile 4: Esri World Imagery (Aman karena menggunakan xyzservices)
    folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery").add_to(m)
    
    # --- (Sisa kode Folium tidak berubah) ---
    folium.GeoJson(gdf_polygon.to_crs(epsg=4326),
                   name="PKKPR", style_function=lambda x: {"color": "yellow", "weight": 2, "fillOpacity": 0}).add_to(m)
    if gdf_tapak is not None:
        folium.GeoJson(gdf_tapak.to_crs(epsg=4326),
                       name="Tapak Proyek", style_function=lambda x: {"color": "red", "weight": 1, "fillColor": "red", "fillOpacity": 0.4}).add_to(m)
    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker([row.geometry.y, row.geometry.x], radius=5, color="black",
                                fill=True, fill_color="orange", fill_opacity=1, popup=f"Titik {i+1}").add_to(m)
    
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, width=900, height=600)
    st.markdown("---")

# ======================
# === Layout Peta PNG ===
# ======================
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG)")
    
    gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
    xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
    width, height = xmax - xmin, ymax - ymin
    
    # Gunakan plt.figure() dan plt.close(fig) untuk manajemen memori yang lebih baik
    fig, ax = plt.subplots(figsize=(14, 10) if width > height else (10, 14), dpi=150)
    
    gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)
    
    if gdf_tapak is not None:
        gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
        gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")
    
    if gdf_points is not None:
        gdf_points_3857 = gdf_points.to_crs(epsg=3857)
        gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=25)
        
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
    
    # Simpan ke buffer memori, bukan file lokal
    png_buffer = io.BytesIO()
    plt.savefig(png_buffer, format="png", dpi=300, bbox_inches="tight")
    plt.close(fig) # Tutup figure untuk membebaskan memori
    png_buffer.seek(0)
    
    st.download_button(
        "⬇️ Download Layout Peta (PNG)", 
        png_buffer, 
        "layout_peta.png", 
        mime="image/png"
    )

