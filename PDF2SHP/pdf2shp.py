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

st.set_page_config(page_title="PDF Koordinat → SHP + Overlay", layout="wide")
st.title("PDF Koordinat → Shapefile Converter & Overlay Tapak Proyek")

# ======================
# === Upload PDF ===
# ======================
uploaded_file = st.file_uploader("Upload file PDF PKKPR", type=["pdf"])
uploaded_shp = st.file_uploader("Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

coords = []
gdf_polygon = None
gdf_tapak = None

# ======================
# === Ekstrak Koordinat dari PDF ===
# ======================
if uploaded_file:
    with pdfplumber.open(uploaded_file) as pdf:
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
        st.success(f"Berhasil menemukan {len(coords)} titik koordinat.")

        gdf_points = gpd.GeoDataFrame(
            pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
            geometry=[Point(xy) for xy in coords],
            crs="EPSG:4326"
        )

        if len(coords) > 2:
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            poly = Polygon(coords)
            gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")

        # Simpan Shapefile hasil ekstraksi
        shp_folder = "output_shp_pdf"
        os.makedirs(shp_folder, exist_ok=True)
        shp_path = os.path.join(shp_folder, "PKKPR_polygon.shp")
        if gdf_polygon is not None:
            gdf_polygon.to_file(shp_path)

            zip_filename = "PKKPR_polygon.zip"
            with zipfile.ZipFile(zip_filename, 'w') as z:
                for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                    fpath = shp_path.replace(".shp", ext)
                    if os.path.exists(fpath):
                        z.write(fpath, os.path.basename(fpath))

            with open(zip_filename, "rb") as f:
                st.download_button("⬇️ Download Shapefile (PKKPR Polygon)", f, "PKKPR_polygon.zip", mime="application/zip")

# ======================
# === Upload & Overlay Tapak Proyek ===
# ======================
if uploaded_shp:
    with zipfile.ZipFile(uploaded_shp, "r") as z:
        z.extractall("tapak_shp")
    gdf_tapak = gpd.read_file("tapak_shp")

    st.success("Shapefile Tapak Proyek berhasil dibaca.")

    if gdf_polygon is not None:
        # pastikan CRS sama
        gdf_tapak = gdf_tapak.to_crs(gdf_polygon.crs)

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

        gdf_polygon.to_crs(epsg=3857).plot(ax=ax, color="red", alpha=0.4, edgecolor="none", label="PKKPR")
        gdf_tapak.to_crs(epsg=3857).plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2, label="Tapak Proyek")

        ctx.add_basemap(ax, crs=gdf_polygon.crs.to_string(), source=ctx.providers.Esri.WorldImagery)

        ax.legend()
        ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14)

        out_png = "layout_peta.png"
        plt.savefig(out_png, dpi=300, bbox_inches="tight")

        with open(out_png, "rb") as f:
            st.download_button("⬇️ Download Layout Peta (PNG)", f, "layout_peta.png", mime="image/png")

