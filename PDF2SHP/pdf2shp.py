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
    return epsg, f"{zone}{'N' if lat >= 0 else 'S'}"

def save_shapefile(gdf, folder_name, zip_name):
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

def parse_luas_from_text(text):
    """
    Ekstrak luas tanah dari seluruh isi PDF.
    Menangani format Indonesia (1.548.038,08).
    """
    text_clean = re.sub(r"\s+", " ", text.lower())
    m = re.search(r"luas tanah yang (disetujui|dimohon)\s*[:\-]?\s*([\d\.\,]+)", text_clean)
    if not m:
        return None, None
    label = m.group(1)
    num_str = m.group(2)
    num_str = re.sub(r"[^\d\.,]", "", num_str)
    if "." in num_str and "," in num_str:
        num_str = num_str.replace(".", "").replace(",", ".")
    elif "," in num_str and "." not in num_str:
        num_str = num_str.replace(",", ".")
    elif num_str.count(".") > 1:
        parts = num_str.split(".")
        num_str = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(num_str), label
    except:
        return None, label

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
        blok_aktif = None
        full_text = ""

        with pdfplumber.open(uploaded_pkkpr) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                full_text += "\n" + text

                # Deteksi blok koordinat aktif
                for line in text.split("\n"):
                    low = line.lower()
                    if "koordinat" in low and "disetujui" in low:
                        blok_aktif = "disetujui"
                    elif "koordinat" in low and "dimohon" in low:
                        blok_aktif = "dimohon"

                    # Tangkap angka dalam baris
                    m = re.findall(r"[-+]?\d+\.\d+", line)
                    if len(m) >= 2:
                        lon, lat = float(m[0]), float(m[1])
                        if 95 <= lon <= 141 and -11 <= lat <= 6:
                            if blok_aktif == "disetujui":
                                coords_disetujui.append((lon, lat))
                            elif blok_aktif == "dimohon":
                                coords_dimohon.append((lon, lat))
                            else:
                                coords_plain.append((lon, lat))

                # Tangkap tabel koordinat jika ada
                tables = page.extract_tables()
                if tables:
                    for tb in tables:
                        for row in tb:
                            if not row:
                                continue
                            nums = re.findall(r"[-+]?\d+\.\d+", " ".join([str(x) for x in row if x]))
                            if len(nums) >= 2:
                                lon, lat = float(nums[0]), float(nums[1])
                                if 95 <= lon <= 141 and -11 <= lat <= 6:
                                    if blok_aktif == "disetujui":
                                        coords_disetujui.append((lon, lat))
                                    elif blok_aktif == "dimohon":
                                        coords_dimohon.append((lon, lat))
                                    else:
                                        coords_plain.append((lon, lat))

        # Ambil luas dari teks
        luas_pkkpr_doc, luas_pkkpr_doc_label = parse_luas_from_text(full_text)

        # Pilih koordinat sesuai prioritas
        if coords_disetujui:
            coords = coords_disetujui
            luas_pkkpr_doc_label = "disetujui"
        elif coords_dimohon:
            coords = coords_dimohon
            luas_pkkpr_doc_label = "dimohon"
        elif coords_plain:
            coords = coords_plain
            luas_pkkpr_doc_label = "tanpa judul"

        # Hapus duplikat titik
        coords = list(dict.fromkeys(coords))

        # Bangun GeoDataFrame
        if coords:
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            gdf_points = gpd.GeoDataFrame(
                pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
                geometry=[Point(xy) for xy in coords],
                crs="EPSG:4326"
            )
            gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(coords)], crs="EPSG:4326")

        with col2:
            label = luas_pkkpr_doc_label or "tidak ditemukan"
            st.markdown(
                f"<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ {len(coords)} titik ({label})</p>",
                unsafe_allow_html=True,
            )

    elif uploaded_pkkpr.name.endswith(".zip"):
        if os.path.exists("pkkpr_shp"):
            shutil.rmtree("pkkpr_shp")
        with zipfile.ZipFile(uploaded_pkkpr, "r") as z:
            z.extractall("pkkpr_shp")
        gdf_polygon = gpd.read_file("pkkpr_shp")
        if gdf_polygon.crs is None:
            gdf_polygon.set_crs(epsg=4326, inplace=True)
        with col2:
            st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅</p>", unsafe_allow_html=True)

# === Ekspor SHP PKKPR ===
if gdf_polygon is not None:
    zip_pkkpr_only = save_shapefile(gdf_polygon, "out_pkkpr_only", "PKKPR_Hasil_Konversi")
    with open(zip_pkkpr_only, "rb") as f:
        st.download_button(
            "⬇️ Download SHP PKKPR (ZIP)", f, file_name="PKKPR_Hasil_Konversi.zip", mime="application/zip"
        )

# ======================
# === Analisis PKKPR ===
# ======================
if gdf_polygon is not None:
    centroid = gdf_polygon.to_crs(4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    luas_pkkpr_hitung = gdf_polygon.to_crs(utm_epsg).area.sum()
    luas_pkkpr_mercator = gdf_polygon.to_crs(3857).area.sum()
    luas_doc_str = f"{luas_pkkpr_doc:,.2f} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(f"""
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (UTM Zona {utm_zone}): {luas_pkkpr_hitung:,.2f} m²
    - Luas PKKPR (proyeksi WGS 84 / Mercator): {luas_pkkpr_mercator:,.2f} m²
    """)
    st.markdown("---")

# ================================
# === Upload Tapak Proyek (SHP) ===
# ================================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

if uploaded_tapak:
    try:
        if os.path.exists("tapak_shp"):
            shutil.rmtree("tapak_shp")
        with zipfile.ZipFile(uploaded_tapak, "r") as z:
            z.extractall("tapak_shp")
        gdf_tapak = gpd.read_file("tapak_shp")
        if gdf_tapak.crs is None:
            gdf_tapak.set_crs(4326, inplace=True)
        with col2:
            st.markdown(
                "<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅</p>",
                unsafe_allow_html=True,
            )
    except Exception as e:
        gdf_tapak = None
        st.error(str(e))
else:
    gdf_tapak = None

# ======================
# === Analisis Overlay ===
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    st.subheader("📊 Analisis Overlay PKKPR & Tapak Proyek")
    centroid = gdf_tapak.to_crs(4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    gdf_tapak_utm = gdf_tapak.to_crs(utm_epsg)
    gdf_polygon_utm = gdf_polygon.to_crs(utm_epsg)
    luas_tapak = gdf_tapak_utm.area.sum()
    luas_pkkpr_hitung = gdf_polygon_utm.area.sum()
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak - luas_overlap
    luas_doc_str = f"{luas_pkkpr_doc:,.2f} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(f"""
    **Analisis Luas Tapak Proyek :**
    - Total Luas Tapak Proyek: {luas_tapak:,.2f} m²
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (UTM Zona {utm_zone}): {luas_pkkpr_hitung:,.2f} m²
    - Luas Tapak Proyek di dalam PKKPR: **{luas_overlap:,.2f} m²**
    - Luas Tapak Proyek di luar PKKPR: **{luas_outside:,.2f} m²**
    """)
    st.markdown("---")

# ======================
# === Peta Interaktif ===
# ======================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    centroid = gdf_polygon.to_crs(4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None)
    Fullscreen(position="bottomleft").add_to(m)

    # Tambahkan 4 basemap utama
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer("Esri.WorldImagery", name="Esri World Imagery").add_to(m)
    folium.TileLayer("CartoDB.Positron", name="CartoDB Positron").add_to(m)
    folium.TileLayer(
        tiles="https://{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        name="Google Satellite",
        attr="Google",
        subdomains=["mt0", "mt1", "mt2", "mt3"]
    ).add_to(m)

    folium.GeoJson(
        gdf_polygon.to_crs(4326),
        name="PKKPR",
        style_function=lambda x: {"color": "yellow", "weight": 2, "fillOpacity": 0},
    ).add_to(m)

    if gdf_tapak is not None:
        folium.GeoJson(
            gdf_tapak.to_crs(4326),
            name="Tapak Proyek",
            style_function=lambda x: {"color": "red", "weight": 1, "fillColor": "red", "fillOpacity": 0.4},
        ).add_to(m)

    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker(
                [row.geometry.y, row.geometry.x],
                radius=5,
                color="black",
                fill_color="orange",
                fill=True,
                popup=f"Titik {i+1}",
            ).add_to(m)

    folium.LayerControl(collapsed=True, position="topright").add_to(m)
    st_folium(m, width=900, height=600)
    st.markdown("---")

# ======================
# === Layout Peta PNG ===
# ======================
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG) - Auto Size")
    out_png = "layout_peta.png"
    gdf_poly_3857 = gdf_polygon.to_crs(3857)
    xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
    width, height = xmax - xmin, ymax - ymin
    figsize = (14, 10) if width > height else (10, 14)

    fig, ax = plt.subplots(figsize=figsize, dpi=150)
    gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)
    if gdf_tapak is not None:
        gdf_tapak_3857 = gdf_tapak.to_crs(3857)
        gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")
    if gdf_points is not None:
        gdf_points_3857 = gdf_points.to_crs(3857)
        gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=25)
    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery, attribution=False)
    ax.set_xlim(xmin - width * 0.05, xmax + width * 0.05)
    ax.set_ylim(ymin - height * 0.05, ymax + height * 0.05)
    ax.set_axis_off()
    plt.savefig(out_png, dpi=300, bbox_inches="tight")
    with open(out_png, "rb") as f:
        st.download_button("⬇️ Download Layout Peta (PNG, Auto)", f, "layout_peta.png", mime="image/png")
    st.pyplot(fig)
