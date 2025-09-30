import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile
from shapely.geometry import Point, Polygon
import folium
from streamlit_folium import st_folium
import pdfplumber
import matplotlib.pyplot as plt
import contextily as ctx
import matplotlib.patches as mpatches
import matplotlib.lines as mlines

st.set_page_config(page_title="PDF/ZIP PKKPR → SHP + Overlay", layout="wide")
st.title("PDF/ZIP PKKPR → Shapefile Converter & Overlay Tapak Proyek")

# ======================
# === Upload File PKKPR (PDF atau ZIP) ===
# ======================
uploaded_pkkpr = st.file_uploader("📂 Upload file PKKPR (PDF atau ZIP)", type=["pdf", "zip"])
uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

coords = []
gdf_polygon = None
gdf_points = None
gdf_tapak = None

# ======================
# === Ekstrak PKKPR dari PDF ===
# ======================
if uploaded_pkkpr is not None and uploaded_pkkpr.name.lower().endswith(".pdf"):
    with pdfplumber.open(uploaded_pkkpr) as pdf:
        for page in pdf.pages:
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

    if coords:
        st.success(f"✅ Berhasil menemukan {len(coords)} titik koordinat dari PDF.")

        # Buat GeoDataFrame Titik
        gdf_points = gpd.GeoDataFrame(
            pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
            geometry=[Point(xy) for xy in coords],
            crs="EPSG:4326"
        )

        # Buat GeoDataFrame Polygon
        if len(coords) > 2:
            if coords[0] != coords[-1]:
                coords.append(coords[0])  # Tutup polygon
            poly = Polygon(coords)
            gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")

        # Simpan Shapefile hasil ekstraksi (polygon + titik)
        shp_folder = "output_shp_pdf"
        os.makedirs(shp_folder, exist_ok=True)

        if gdf_polygon is not None:
            gdf_polygon.to_file(os.path.join(shp_folder, "PKKPR_polygon.shp"))
        if gdf_points is not None:
            gdf_points.to_file(os.path.join(shp_folder, "PKKPR_points.shp"))

        # Zip semua file shapefile
        zip_filename = "PKKPR_output.zip"
        with zipfile.ZipFile(zip_filename, 'w') as z:
            for root, dirs, files in os.walk(shp_folder):
                for file in files:
                    z.write(os.path.join(root, file), file)

        with open(zip_filename, "rb") as f:
            st.download_button("⬇️ Download Shapefile (PKKPR Polygon + Titik)", f, "PKKPR_output.zip", mime="application/zip")

# ======================
# === Ekstrak PKKPR dari ZIP (Shapefile) ===
# ======================
elif uploaded_pkkpr is not None and uploaded_pkkpr.name.lower().endswith(".zip"):
    with zipfile.ZipFile(uploaded_pkkpr, "r") as z:
        z.extractall("pkkpr_shp")
    gdf_polygon = gpd.read_file("pkkpr_shp")
    st.success("✅ Shapefile PKKPR berhasil dibaca.")

# ======================
# === Upload & Overlay Tapak Proyek ===
# ======================
if uploaded_tapak:
    with zipfile.ZipFile(uploaded_tapak, "r") as z:
        z.extractall("tapak_shp")
    gdf_tapak = gpd.read_file("tapak_shp")

    st.success("✅ Shapefile Tapak Proyek berhasil dibaca.")

# ======================
# === Analisis & Peta ===
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    # Pastikan CRS sama (gunakan meter / EPSG:3857 untuk luas)
    gdf_tapak = gdf_tapak.to_crs(epsg=3857)
    gdf_polygon = gdf_polygon.to_crs(epsg=3857)
    if gdf_points is not None:
        gdf_points = gdf_points.to_crs(epsg=3857)

    luas_tapak = gdf_tapak.area.sum()
    luas_overlap = gdf_tapak.overlay(gdf_polygon, how="intersection").area.sum()
    luas_outside = luas_tapak - luas_overlap

    st.info(f"""
    **Analisis Luas Tapak Proyek:**
    - Total Luas Tapak Proyek: {luas_tapak:.2f} m²
    - Luas di dalam PKKPR: {luas_overlap:.2f} m²
    - Luas di luar PKKPR: {luas_outside:.2f} m²
    """)

    # ======================
    # === Layout Peta PNG dengan Legend ===
    # ======================
    fig, ax = plt.subplots(figsize=(10, 10))

    # PKKPR Polygon (outline kuning, no fill)
    gdf_polygon.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)

    # Tapak Proyek (merah transparan)
    gdf_tapak.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")

    # Titik koordinat PKKPR (oranye bulat dengan outline hitam)
    if gdf_points is not None:
        gdf_points.plot(ax=ax, color="orange", edgecolor="black", markersize=50)

    # Tambahkan basemap
    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)

    # Legend kustom
    legend_elements = [
        mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=2, label="PKKPR (Polygon)"),
        mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
        mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black",
                      linestyle="None", markersize=8, label="PKKPR (Titik)")
    ]
    ax.legend(handles=legend_elements, loc="upper right", fontsize=10, frameon=True)

    ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14)

    out_png = "layout_peta.png"
    plt.savefig(out_png, dpi=300, bbox_inches="tight")

    with open(out_png, "rb") as f:
        st.download_button("⬇️ Download Layout Peta (PNG)", f, "layout_peta.png", mime="image/png")

    # ======================
    # === Preview Folium ===
    # ======================
    st.subheader("🌍 Preview Peta Interaktif")
    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17)

    # PKKPR Polygon
    folium.GeoJson(
        gdf_polygon.to_crs(epsg=4326),
        style_function=lambda x: {"color": "yellow", "fillOpacity": 0, "weight": 2}
    ).add_to(m)

    # Tapak Proyek
    folium.GeoJson(
        gdf_tapak.to_crs(epsg=4326),
        style_function=lambda x: {"color": "red", "fillColor": "red", "fillOpacity": 0.4}
    ).add_to(m)

    # Titik koordinat PKKPR
    if gdf_points is not None:
        for _, row in gdf_points.to_crs(epsg=4326).iterrows():
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=5,
                color="black",
                fill=True,
                fill_color="orange",
                fill_opacity=1
            ).add_to(m)

    st_folium(m, width=900, height=600)
