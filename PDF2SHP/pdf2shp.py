import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, re, tempfile
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
from collections import OrderedDict

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
    """Menyimpan GeoDataFrame ke ZIP Shapefile di memory buffer."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_shp_path = os.path.join(temp_dir, "PKKPR_Output.shp")
        # Pastikan output selalu WGS84 EPSG:4326
        gdf.to_crs(epsg=4326).to_file(temp_shp_path, driver='ESRI Shapefile')

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in os.listdir(temp_dir):
                zf.write(os.path.join(temp_dir, file), arcname=file)
        zip_buffer.seek(0)
        return zip_buffer.read()


def parse_luas_from_text(text):
    """Ambil teks luas tanah dari dokumen."""
    # Normalisasi spasi dan cari pola
    text_clean = re.sub(r"\s+", " ", (text or ""), flags=re.IGNORECASE)
    luas_matches = re.findall(
        r"luas\s*tanah\s*yang\s*(dimohon|disetujui)\s*[:\-]?\s*([\d\.,]+\s*(M2|M²|M\s*2))",
        text_clean,
        re.IGNORECASE
    )
    luas_data = {}
    for label, value, _ in luas_matches:
        # Ambil nilai angka dan bersihkan, lalu simpan dengan labelnya
        luas_data[label.lower()] = re.sub(r'[^0-9,\.]', '', value.strip()).strip()

    # Prioritaskan "disetujui"
    if "disetujui" in luas_data and luas_data["disetujui"]:
        return luas_data["disetujui"], "disetujui"
    elif "dimohon" in luas_data and luas_data["dimohon"]:
        return luas_data["dimohon"], "dimohon"
    
    # Fallback jika formatnya sedikit berbeda
    m = re.search(r"luas\s*tanah\s*yang\s*disetujui\s*[:\-]?\s*([\d\.,]+)", text_clean, re.IGNORECASE)
    if m:
        return m.group(1).strip(), "disetujui"
        
    return None, "tidak ditemukan"


def format_angka_id(value):
    """Format angka besar dengan pemisah ribuan titik dan 2 desimal koma."""
    try:
        val = float(str(value).replace(',', '.')) # Pastikan input string diubah ke float dengan benar
        # Jika angka sangat dekat dengan integer, format sebagai integer
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", "X").replace(".", ",").replace("X", ".")
        # Jika tidak, format sebagai float dengan gaya Indonesia
        else:
            return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return str(value)


# ======================
# === Upload PKKPR ===
# ======================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

# Inisialisasi variabel di luar blok if
gdf_points, gdf_polygon = None, None
luas_pkkpr_doc, luas_pkkpr_doc_label = None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.lower().endswith(".pdf"):
        full_text = ""
        coords_by_type = {"disetujui": [], "dimohon": [], "lainnya": []}
        
        # Variabel ini akan menyimpan status tabel aktif di seluruh halaman
        blok_aktif = "lainnya" 

        try:
            with st.spinner("🔍 Membaca dan mengekstrak koordinat dari PDF..."):
                with pdfplumber.open(uploaded_pkkpr) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text() or ""
                        full_text += "\n" + text
                        low = text.lower()

                        # Perbarui status blok_aktif HANYA jika menemukan header baru
                        if "koordinat" in low and "disetujui" in low:
                            blok_aktif = "disetujui"
                        elif "koordinat" in low and "dimohon" in low:
                            blok_aktif = "dimohon"
                        
                        coords_found_in_table = False
                        tables = page.extract_tables()
                        
                        if tables:
                            for tb in tables:
                                if len(tb) <= 1: continue
                                header = [str(c or '').lower().strip() for c in tb[0]]
                                idx_lon, idx_lat = -1, -1
                                try:
                                    idx_lon = next(i for i, h in enumerate(header) if "bujur" in h)
                                    idx_lat = next(i for i, h in enumerate(header) if "lintang" in h)
                                except StopIteration:
                                    if len(header) >= 3 and any("no" in h for h in header): idx_lon, idx_lat = 1, 2
                                    elif len(header) == 2: idx_lon, idx_lat = 0, 1
                                
                                if idx_lon == -1 or idx_lat == -1: continue

                                for row in tb[1:]:
                                    if len(row) <= max(idx_lon, idx_lat): continue
                                    lon_str = str(row[idx_lon] or '').replace(",", ".").strip()
                                    lat_str = str(row[idx_lat] or '').replace(",", ".").strip()
                                    if not lon_str or not lat_str: continue

                                    try:
                                        if lon_str.startswith('8.'): lon_str = '9' + lon_str
                                        lon_val = float(re.sub(r"[^\d\.\-]", "", lon_str))
                                        lat_val = float(re.sub(r"[^\d\.\-]", "", lat_str))
                                        
                                        if 90 <= lon_val <= 145 and -11 <= lat_val <= 6:
                                            coords_by_type[blok_aktif].append((lon_val, lat_val))
                                            coords_found_in_table = True # Tandai bahwa kita berhasil
                                    except (ValueError, TypeError):
                                        continue
                        
                        # --- PERBAIKAN LOGIKA ---
                        # Jalankan fallback HANYA jika tidak ada koordinat yang ditemukan dari tabel di halaman ini
                        if not coords_found_in_table:
                            for line in text.split("\n"):
                                nums = re.findall(r"(\d+\.\d+)", line.replace(",", "."))
                                if len(nums) >= 2:
                                    try:
                                        lon_val, lat_val = float(nums[0]), float(nums[1])
                                        if 90 <= lon_val <= 145 and -11 <= lat_val <= 6:
                                            coords_by_type[blok_aktif].append((lon_val, lat_val))
                                    except (ValueError, TypeError):
                                        continue

                # === Prioritas & Pembersihan Hasil ===
                coords_final, coords_label = [], "tidak ditemukan"
                if coords_by_type["disetujui"]:
                    coords_final = coords_by_type["disetujui"]
                    coords_label = "disetujui"
                elif coords_by_type["dimohon"]:
                    coords_final = coords_by_type["dimohon"]
                    coords_label = "dimohon"
                elif coords_by_type["lainnya"]:
                    coords_final = coords_by_type["lainnya"]
                    coords_label = "lainnya"

                luas_pkkpr_doc, luas_pkkpr_doc_label = parse_luas_from_text(full_text)

                if coords_final:
                    # --- PERBAIKAN LOGIKA: Hapus duplikat sambil menjaga urutan ---
                    coords_unique = list(OrderedDict.fromkeys(coords_final))

                    # Pastikan poligon tertutup
                    if len(coords_unique) > 0 and coords_unique[0] != coords_unique[-1]:
                        coords_unique.append(coords_unique[0])

                    gdf_points = gpd.GeoDataFrame(
                        geometry=[Point(xy) for xy in coords_unique],
                        crs="EPSG:4326"
                    )
                    gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(coords_unique)], crs="EPSG:4326")
                    
                    with col2:
                        st.markdown(f"""
                        <div style="padding-top: 2.5rem;">
                            <p style='color:green; font-weight:bold;'>✅ PDF Berhasil Diproses</p>
                            <ul style='margin-left: -20px; font-size: 0.9em;'>
                                <li><b>{len(coords_unique)-1 if len(coords_unique) > 0 else 0}</b> titik unik ditemukan.</li>
                                <li>Kategori: <b>{coords_label.capitalize()}</b></li>
                            </ul>
                        </div>
                        """, unsafe_allow_html=True)
                else:
                    with col2:
                        st.error("Tidak ada koordinat yang dapat diekstrak dari PDF.")

        except Exception as e:
            st.error(f"Gagal memproses PDF: {e}")
            gdf_polygon = None

    elif uploaded_pkkpr.name.lower().endswith(".zip"):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                with zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()), 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                shp_file = next((os.path.join(root, name) for root, _, files in os.walk(temp_dir) for name in files if name.lower().endswith('.shp')), None)
                if shp_file:
                    gdf_polygon = gpd.read_file(shp_file)
                    if gdf_polygon.crs is None: gdf_polygon.set_crs(epsg=4326, inplace=True)
                    with col2:
                        st.markdown("<p style='color:green;font-weight:bold;padding-top:3.5rem;'>✅ Shapefile (PKKPR) terbaca.</p>", unsafe_allow_html=True)
                else:
                    st.error("File .shp tidak ditemukan di dalam ZIP.")
        except Exception as e:
            st.error(f"Gagal membaca shapefile PKKPR: {e}")
            gdf_polygon = None

# ======================
# === Analisis PKKPR ===
# ======================
if gdf_polygon is not None and not gdf_polygon.empty:
    zip_pkkpr_bytes = save_shapefile(gdf_polygon)
    st.download_button(
        "⬇️ Download SHP PKKPR (ZIP)", 
        zip_pkkpr_bytes, "PKKPR_Hasil_Konversi.zip", 
        mime="application/zip",
        use_container_width=True
    )

    gdf_wgs84 = gdf_polygon.to_crs(epsg=4326)
    centroid = gdf_wgs84.geometry.unary_union.centroid
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    
    # Calculate Areas
    luas_pkkpr_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
    luas_pkkpr_3857 = gdf_polygon.to_crs(epsg=3857).area.sum() # WGS 84 / Pseudo-Mercator

    luas_doc_str = format_angka_id(luas_pkkpr_doc) if luas_pkkpr_doc else "Tidak ditemukan"

    st.info(
        f"**Analisis Luas Batas PKKPR**:\n"
        f"- Luas PKKPR dari dokumen ({luas_pkkpr_doc_label.capitalize()}): **{luas_doc_str} m²**\n"
        f"- Luas PKKPR (UTM {utm_zone}): **{format_angka_id(luas_pkkpr_utm)} m²**\n"
        f"- Luas PKKPR (WGS 84 Mercator / EPSG:3857): **{format_angka_id(luas_pkkpr_3857)} m²**"
    )
    st.markdown("---")

# ======================
# === Upload Tapak Proyek ===
# ======================
col1_tapak, col2_tapak = st.columns([0.7, 0.3])
with col1_tapak:
    uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

gdf_tapak = None
if uploaded_tapak:
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            with zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()), 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            shp_file_tapak = next((os.path.join(root, name) for root, _, files in os.walk(temp_dir) for name in files if name.lower().endswith('.shp')), None)
            if shp_file_tapak:
                gdf_tapak = gpd.read_file(shp_file_tapak)
                if gdf_tapak.crs is None: gdf_tapak.set_crs(epsg=4326, inplace=True)
                with col2_tapak:
                    st.markdown("<p style='color:green;font-weight:bold;padding-top:3.5rem;'>✅ Shapefile tapak terbaca.</p>", unsafe_allow_html=True)
            else:
                st.error("File .shp tidak ditemukan di dalam ZIP tapak proyek.")
    except Exception as e:
        st.error(f"Gagal membaca shapefile Tapak Proyek: {e}")

# ======================
# === Overlay & Peta ===
# ======================
if gdf_polygon is not None and not gdf_polygon.empty:
    if gdf_tapak is not None and not gdf_tapak.empty:
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.unary_union.centroid
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
        
        # --- UTM Calculation ---
        gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg)
        gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)
        luas_tapak_utm = gdf_tapak_utm.area.sum()

        # --- Mercator Calculation (EPSG:3857) ---
        gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
        gdf_polygon_3857 = gdf_polygon.to_crs(epsg=3857)
        luas_tapak_3857 = gdf_tapak_3857.area.sum()
        
        luas_overlap_utm, luas_outside_utm = 0, luas_tapak_utm
        luas_overlap_3857, luas_outside_3857 = 0, luas_tapak_3857
        
        try:
            # Clean geometries for robust overlay (UTM)
            if not gdf_tapak_utm.is_valid.all(): gdf_tapak_utm.geometry = gdf_tapak_utm.geometry.buffer(0)
            if not gdf_polygon_utm.is_valid.all(): gdf_polygon_utm.geometry = gdf_polygon_utm.geometry.buffer(0)
            gdf_overlap_utm = gpd.overlay(gdf_tapak_utm, gdf_polygon_utm, how="intersection", keep_geom_type=True)
            luas_overlap_utm = gdf_overlap_utm.area.sum()
            luas_outside_utm = luas_tapak_utm - luas_overlap_utm
        except Exception as e:
            st.warning(f"Gagal menghitung overlay UTM: {e}")

        try:
            # Clean geometries for robust overlay (3857)
            if not gdf_tapak_3857.is_valid.all(): gdf_tapak_3857.geometry = gdf_tapak_3857.geometry.buffer(0)
            if not gdf_polygon_3857.is_valid.all(): gdf_polygon_3857.geometry = gdf_polygon_3857.geometry.buffer(0)
            gdf_overlap_3857 = gpd.overlay(gdf_tapak_3857, gdf_polygon_3857, how="intersection", keep_geom_type=True)
            luas_overlap_3857 = gdf_overlap_3857.area.sum()
            luas_outside_3857 = luas_tapak_3857 - luas_overlap_3857
        except Exception as e:
            st.warning(f"Gagal menghitung overlay Mercator (EPSG:3857): {e}")


        st.success(
            f"**HASIL ANALISIS OVERLAY**:\n"
            f"**Proyeksi UTM ({utm_zone}):**\n"
            f"- Total Luas Tapak Proyek: **{format_angka_id(luas_tapak_utm)} m²**\n"
            f"- Luas Tapak di dalam PKKPR (Overlap): **{format_angka_id(luas_overlap_utm)} m²**\n"
            f"- Luas Tapak di luar PKKPR (Outside): **{format_angka_id(luas_outside_utm)} m²**\n"
            f"**Proyeksi WGS 84 Mercator (EPSG:3857):**\n"
            f"- Total Luas Tapak Proyek: **{format_angka_id(luas_tapak_3857)} m²**\n"
            f"- Luas Tapak di dalam PKKPR (Overlap): **{format_angka_id(luas_overlap_3857)} m²**\n"
            f"- Luas Tapak di luar PKKPR (Outside): **{format_angka_id(luas_outside_3857)} m²**"
        )
        st.markdown("---")

    tab1, tab2 = st.tabs(["🌍 Peta Interaktif", "🖼️ Layout Peta (PNG)"])
    with tab1:
        st.subheader("Peta Interaktif")
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.unary_union.centroid
        m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None)
        folium.TileLayer("openstreetmap", name="OpenStreetMap").add_to(m)
        # Basemap ESRI untuk peta interaktif (Folium)
        folium.TileLayer(xyz.Esri.WorldImagery, name="Citra Satelit Esri").add_to(m)
        folium.TileLayer("CartoDB Positron", name="Peta Dasar Terang").add_to(m)
        folium.GeoJson(gdf_polygon.to_crs(epsg=4326), name="Batas PKKPR", style_function=lambda x: {"color": "#FFFF00", "weight": 3, "fillOpacity": 0.1, "dashArray": "5, 5"}).add_to(m)
        if gdf_tapak is not None:
            folium.GeoJson(gdf_tapak.to_crs(epsg=4326), name="Tapak Proyek", style_function=lambda x: {"color": "#FF0000", "weight": 2.5, "fillColor": "#FF0000", "fillOpacity": 0.4}).add_to(m)
        if gdf_points is not None:
            points_layer = folium.FeatureGroup(name="Titik Koordinat PKKPR")
            for i, row in gdf_points.to_crs(epsg=4326).iterrows():
                folium.CircleMarker([row.geometry.y, row.geometry.x], radius=4, color="#000000", weight=1, fill=True, fill_color="#FFA500", fill_opacity=1, popup=f"Titik {i+1}").add_to(points_layer)
            points_layer.add_to(m)
        Fullscreen(position="bottomleft").add_to(m)
        folium.LayerControl(collapsed=True).add_to(m)
        st_folium(m, width="100%", height=600, returned_objects=[])

    with tab2:
        st.subheader("Layout Peta Statis")
        with st.spinner("Membuat layout peta..."):
            gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
            xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
            width, height = xmax - xmin, ymax - ymin
            
            # Prevent division by zero if geometry is a point or invalid
            aspect_ratio = (height / width) if width > 0 else 1.0
            fig, ax = plt.subplots(figsize=(12, 12 * aspect_ratio), dpi=150)
            
            gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="#FFFF00", linewidth=2.5)
            
            if gdf_tapak is not None: 
                # Re-calculate overlay geometry in 3857 for the map visual
                gdf_tapak_3857_plot = gdf_tapak.to_crs(epsg=3857)
                if not gdf_tapak_3857_plot.is_valid.all(): gdf_tapak_3857_plot.geometry = gdf_tapak_3857_plot.geometry.buffer(0)
                if not gdf_poly_3857.is_valid.all(): gdf_poly_3857.geometry = gdf_poly_3857.geometry.buffer(0)
                
                # Area Overlap
                try:
                    gdf_overlap_plot = gpd.overlay(gdf_tapak_3857_plot, gdf_poly_3857, how="intersection", keep_geom_type=True)
                    gdf_overlap_plot.plot(ax=ax, facecolor="#00FF00", alpha=0.6, edgecolor="#008000", label="Overlap", zorder=2)
                except Exception:
                    pass

                # Area Outside (Difference)
                try:
                    # Difference of Tapak Proyek and PKKPR to get area outside
                    gdf_outside_plot = gpd.overlay(gdf_tapak_3857_plot, gdf_poly_3857, how="difference", keep_geom_type=True)
                    gdf_outside_plot.plot(ax=ax, facecolor="#FF0000", alpha=0.6, edgecolor="#800000", label="Outside", zorder=2)
                except Exception:
                     pass

                # Plot Tapak Boundary (Optional, to ensure all boundaries are visible)
                gdf_tapak_3857_plot.plot(ax=ax, facecolor="none", edgecolor="#FF0000", linewidth=1.5, linestyle='--', zorder=3)


            if gdf_points is not None: gdf_points.to_crs(epsg=3857).plot(ax=ax, color="#FFA500", edgecolor="#000000", markersize=30, zorder=4)
            
            # Basemap ESRI untuk peta statis (Matplotlib/Contextily)
            ctx.add_basemap(ax, crs="EPSG:3857", source=ctx.providers.Esri.WorldImagery)
            
            # Set limits with a 10% buffer
            ax.set_xlim(xmin - width * 0.1, xmax + width * 0.1)
            ax.set_ylim(ymin - height * 0.1, ymax + height * 0.1)
            
            # Legend Elements
            legend_elements = [
                mpatches.Patch(facecolor="none", edgecolor="#FFFF00", linewidth=2, label="Batas PKKPR"),
            ]
            if gdf_tapak is not None:
                legend_elements.extend([
                    mpatches.Patch(facecolor="#00FF00", edgecolor="#008000", alpha=0.6, label="Tapak Proyek (Overlap PKKPR)"),
                    mpatches.Patch(facecolor="#FF0000", edgecolor="#800000", alpha=0.6, label="Tapak Proyek (Di Luar PKKPR)"),
                    mlines.Line2D([], [], color="#FF0000", linestyle='--', linewidth=1.5, label="Batas Tapak Proyek"),
                ])
            if gdf_points is not None: legend_elements.append(mlines.Line2D([], [], color="#FFA500", marker='o', markeredgecolor="black", linestyle='None', markersize=7, label="Titik PKKPR"))
            
            ax.legend(handles=legend_elements, title="Legenda", loc="upper right", fontsize=8, title_fontsize=9, framealpha=0.9)
            ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14, weight="bold")
            ax.set_axis_off()
            
            png_buffer = io.BytesIO()
            plt.savefig(png_buffer, format="png", dpi=300, bbox_inches="tight", pad_inches=0.1)
            plt.close(fig)
            png_buffer.seek(0)
            
            st.image(png_buffer, caption="Preview Layout Peta")
            st.download_button("⬇️ Download Layout Peta (PNG)", png_buffer, "layout_peta_pkkpr.png", mime="image/png", use_container_width=True)
