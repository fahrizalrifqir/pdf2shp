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

st.set_page_config(page_title="PDF/Shapefile PKKPR → SHP + Overlay", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")

# ======================
# === Fungsi Helper ===
# ======================
def get_utm_epsg(lon, lat):
    """Deteksi zona UTM dari koordinat lon/lat"""
    zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        return 32600 + zone  # UTM utara
    else:
        return 32700 + zone  # UTM selatan

def save_shapefile(gdf, folder_name, zip_name):
    """Simpan GeoDataFrame ke shapefile .zip"""
    if os.path.exists(folder_name):
        shutil.rmtree(folder_name)
    os.makedirs(folder_name, exist_ok=True)
    shp_path = os.path.join(folder_name, "data.shp")
    gdf.to_file(shp_path)
    zip_path = f"{zip_name}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for file in os.listdir(folder_name):
            zf.write(os.path.join(folder_name, file), arcname=file)
    return zip_path

def parse_luas(line):
    """Ambil angka luas dari teks PDF"""
    match = re.search(r"([\d\.\,]+)", line)
    if not match:
        return None
    num_str = match.group(1)
    num_str = num_str.replace(".", "").replace(",", ".")
    try:
        return float(num_str)
    except:
        return None

# ======================
# === Upload Files ===
# ======================
uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])
uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

coords = []
gdf_points, gdf_polygon, gdf_tapak = None, None, None
luas_pkkpr_doc, luas_pkkpr_doc_label = None, None

# ======================
# === Ekstrak PKKPR ===
# ======================
if uploaded_pkkpr:
    if uploaded_pkkpr.name.endswith(".pdf"):
        coords = []
        luas_disetujui, luas_dimohon = None, None
        with pdfplumber.open(uploaded_pkkpr) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    for line in text.split("\n"):
                        low = line.lower()
                        if "luas tanah yang disetujui" in low and luas_disetujui is None:
                            luas_disetujui = parse_luas(line)
                        elif "luas tanah yang dimohon" in low and luas_dimohon is None:
                            luas_dimohon = parse_luas(line)

                # cari tabel koordinat
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row and len(row) >= 3:
                            try:
                                lon = float(row[1])
                                lat = float(row[2])
                                if 95 <= lon <= 141 and -11 <= lat <= 6:
                                    coords.append((lon, lat))
                            except:
                                continue

        # pilih luas
        if luas_disetujui is not None:
            luas_pkkpr_doc = luas_disetujui
            luas_pkkpr_doc_label = "disetujui"
        elif luas_dimohon is not None:
            luas_pkkpr_doc = luas_dimohon
            luas_pkkpr_doc_label = "dimohon"

        if coords:
            gdf_points = gpd.GeoDataFrame(
                pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
                geometry=[Point(xy) for xy in coords],
                crs="EPSG:4326"
            )
            if len(coords) > 2:
                if coords[0] != coords[-1]:
                    coords.append(coords[0])  # Tutup polygon
                poly = Polygon(coords)
                gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")

        luas_info = f"{luas_pkkpr_doc:,.2f} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "tidak ditemukan"
        st.success(f"✅ PKKPR dari PDF berhasil diekstrak ({len(coords)} titik, luas dokumen: {luas_info}).")

    elif uploaded_pkkpr.name.endswith(".zip"):
        if os.path.exists("pkkpr_shp"):
            shutil.rmtree("pkkpr_shp")
        with zipfile.ZipFile(uploaded_pkkpr, "r") as z:
            z.extractall("pkkpr_shp")
        gdf_polygon = gpd.read_file("pkkpr_shp")
        if gdf_polygon.crs is None:
            gdf_polygon.set_crs(epsg=4326, inplace=True)
        st.success("✅ PKKPR dari Shapefile berhasil dibaca.")
    
    # === Ekspor SHP PKKPR (Dipindahkan ke sini) ===
    if gdf_polygon is not None:
        st.subheader("⬇️ Download Hasil Konversi PKKPR")
        zip_pkkpr_only = save_shapefile(gdf_polygon, "out_pkkpr_only", "PKKPR_Hasil_Konversi")
        with open(zip_pkkpr_only, "rb") as f:
            st.download_button("⬇️ Download SHP PKKPR (ZIP)", f, file_name="PKKPR_Hasil_Konversi.zip", mime="application/zip")
        st.markdown("---") # Tambahkan garis pemisah
        

# ======================
# === Upload Tapak Proyek ===
# ======================
if uploaded_tapak:
    if os.path.exists("tapak_shp"):
        shutil.rmtree("tapak_shp")
    with zipfile.ZipFile(uploaded_tapak, "r") as z:
        z.extractall("tapak_shp")
    gdf_tapak = gpd.read_file("tapak_shp")
    if gdf_tapak.crs is None:
        gdf_tapak.set_crs(epsg=4326, inplace=True)
    st.success("✅ Shapefile Tapak Proyek berhasil dibaca.")

# ======================
# === Analisis Luas + Ekspor SHP (Hanya jika KEDUA file ada) ===
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    st.subheader("📊 Hasil Analisis Overlay")

    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg = get_utm_epsg(centroid.x, centroid.y)
    gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg)
    gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)

    luas_tapak = gdf_tapak_utm.area.sum()
    luas_pkkpr_hitung = gdf_polygon_utm.area.sum()
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak - luas_overlap

    luas_doc_str = f"{luas_pkkpr_doc:,.2f} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(f"""
    **Analisis Luas Tapak Proyek (Proyeksi UTM {utm_epsg}):**
    - Total Luas Tapak Proyek: {luas_tapak:,.2f} m²
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (hitung dari geometri): {luas_pkkpr_hitung:,.2f} m²
    - Luas di dalam PKKPR: **{luas_overlap:,.2f} m²**
    - Luas di luar PKKPR: **{luas_outside:,.2f} m²**
    """)
    
    st.markdown("---")
    
    # === Ekspor SHP Tapak Proyek (UTM) ===
    st.subheader("⬇️ Download Shapefile Tapak Proyek (UTM)")
    # NOTE: Tombol download PKKPR di sini DIHILANGKAN agar tidak duplikasi
    
    zip_tapak = save_shapefile(gdf_tapak_utm, "out_tapak", "Tapak_Hasil_UTM")
    with open(zip_tapak, "rb") as f:
        st.download_button("⬇️ Download SHP Tapak Proyek (UTM)", f, file_name="Tapak_Hasil_UTM.zip", mime="application/zip")
        
    st.markdown("---")

    # ======================
    # === Layout Peta PNG ===
    # ======================
    st.subheader("🖼️ Layout Peta (PNG)")
    fig, ax = plt.subplots(figsize=(10, 10))
    gdf_polygon.to_crs(epsg=3857).plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)
    gdf_tapak.to_crs(epsg=3857).plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")
    if gdf_points is not None:
        gdf_points.to_crs(epsg=3857).plot(ax=ax, color="orange", edgecolor="black", markersize=50)

    # hilangkan tulisan attribution
    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery, attribution=False)

    legend_elements = [
        mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=2, label="PKKPR (Polygon)"),
        mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
        mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black", linestyle="None", markersize=8, label="PKKPR (Titik)")
    ]
    ax.legend(handles=legend_elements, loc="upper right", fontsize=10, frameon=True)
    ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14)
    ax.set_axis_off()

    st.pyplot(fig)
    out_png = "layout_peta.png"
    plt.savefig(out_png, dpi=300, bbox_inches="tight")
    with open(out_png, "rb") as f:
        st.download_button("⬇️ Download Layout Peta (PNG)", f, "layout_peta.png", mime="image/png")
        
    st.markdown("---")

    # ======================
    # === Preview Interaktif Folium ===
    # ======================
    st.subheader("🌍 Preview Peta Interaktif")
    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles="cartodbdarkmatter")
    folium.GeoJson(gdf_polygon.to_crs(epsg=4326), name="PKKPR", style_function=lambda x: {"color": "yellow", "weight": 2, "fillOpacity": 0}).add_to(m)
    folium.GeoJson(gdf_tapak.to_crs(epsg=4326), name="Tapak Proyek", style_function=lambda x: {"color": "red", "weight": 1, "fillColor": "red", "fillOpacity": 0.4}).add_to(m)

    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=5, color="black", fill=True, fill_color="orange", fill_opacity=1,
                popup=f"Titik {i+1}"
            ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=900, height=600)
