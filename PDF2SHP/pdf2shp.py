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

st.set_page_config(page_title="PDF/SHP PKKPR → SHP + Overlay", layout="wide")
st.title("PDF/SHP PKKPR → Shapefile Converter & Overlay Tapak Proyek")

# ======================
# === Upload Files ===
# ======================
uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF atau Shapefile ZIP)", type=["pdf", "zip"])
uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

coords = []
gdf_points = None
gdf_polygon = None
gdf_tapak = None

# ======================
# === Proses PKKPR ===
# ======================
if uploaded_pkkpr:
    filename = uploaded_pkkpr.name.lower()

    # Jika input PKKPR berupa PDF
    if filename.endswith(".pdf"):
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

            # GeoDataFrame Titik
            gdf_points = gpd.GeoDataFrame(
                pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
                geometry=[Point(xy) for xy in coords],
                crs="EPSG:4326"
            )

            # GeoDataFrame Polygon
            if len(coords) > 2:
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                poly = Polygon(coords)
                gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")

            # === Simpan Shapefile Titik ===
            shp_point_folder = "output_shp_points"
            os.makedirs(shp_point_folder, exist_ok=True)
            shp_point_path = os.path.join(shp_point_folder, "PKKPR_points.shp")
            gdf_points.to_file(shp_point_path)

            zip_point_filename = "PKKPR_points.zip"
            with zipfile.ZipFile(zip_point_filename, 'w') as z:
                for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                    fpath = shp_point_path.replace(".shp", ext)
                    if os.path.exists(fpath):
                        z.write(fpath, os.path.basename(fpath))
            with open(zip_point_filename, "rb") as f:
                st.download_button("⬇️ Download Shapefile (PKKPR Titik)", f, "PKKPR_points.zip", mime="application/zip")

            # === Simpan Shapefile Polygon ===
            if gdf_polygon is not None:
                shp_poly_folder = "output_shp_polygon"
                os.makedirs(shp_poly_folder, exist_ok=True)
                shp_poly_path = os.path.join(shp_poly_folder, "PKKPR_polygon.shp")
                gdf_polygon.to_file(shp_poly_path)

                zip_poly_filename = "PKKPR_polygon.zip"
                with zipfile.ZipFile(zip_poly_filename, 'w') as z:
                    for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                        fpath = shp_poly_path.replace(".shp", ext)
                        if os.path.exists(fpath):
                            z.write(fpath, os.path.basename(fpath))
                with open(zip_poly_filename, "rb") as f:
                    st.download_button("⬇️ Download Shapefile (PKKPR Polygon)", f, "PKKPR_polygon.zip", mime="application/zip")

    # Jika input PKKPR berupa Shapefile ZIP
    elif filename.endswith(".zip"):
        with zipfile.ZipFile(uploaded_pkkpr, "r") as z:
            z.extractall("pkkpr_shp")
        gdf_polygon = gpd.read_file("pkkpr_shp")
        st.success("✅ Shapefile PKKPR berhasil dibaca.")

# ======================
# === Upload Tapak Proyek ===
# ======================
if uploaded_tapak:
    with zipfile.ZipFile(uploaded_tapak, "r") as z:
        z.extractall("tapak_shp")
    gdf_tapak = gpd.read_file("tapak_shp")
    st.success("✅ Shapefile Tapak Proyek berhasil dibaca.")

# ======================
# === Overlay & Analisis ===
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    gdf_tapak = gdf_tapak.to_crs(epsg=3857)
    gdf_polygon = gdf_polygon.to_crs(epsg=3857)

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
    # === Layout Peta PNG ===
    # ======================
    fig, ax = plt.subplots(figsize=(10, 10))

    gdf_polygon.plot(ax=ax, color="red", alpha=0.4, edgecolor="none", label="PKKPR")
    gdf_tapak.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2, label="Tapak Proyek")

    # Tambahkan basemap
    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)

    ax.legend()
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
    folium.GeoJson(gdf_polygon.to_crs(epsg=4326),
                   style_function=lambda x: {"color": "red", "fillColor": "red", "fillOpacity": 0.4},
                   name="PKKPR").add_to(m)

    # Tapak Proyek
    folium.GeoJson(gdf_tapak.to_crs(epsg=4326),
                   style_function=lambda x: {"color": "yellow", "fillOpacity": 0},
                   name="Tapak Proyek").add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=900, height=600)
