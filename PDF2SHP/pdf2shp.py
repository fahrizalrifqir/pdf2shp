import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile
from shapely.geometry import Point, Polygon
import folium
from streamlit_folium import st_folium
import pdfplumber

st.set_page_config(page_title="PDF Koordinat → SHP/KML", layout="wide")
st.title("PDF Koordinat → Shapefile & KML Converter")

uploaded_file = st.file_uploader("Upload file PDF", type=["pdf"])

if uploaded_file:
    coords = []

    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    # contoh baris: ["1", "107.5792419085158", "-6.97468187015887"]
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

        gdf_polygon = None
        if len(coords) > 2:
            if coords[0] != coords[-1]:
                coords.append(coords[0])  # tutup polygon
            poly = Polygon(coords)
            gdf_polygon = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")

        # === SIMPAN FILE OUTPUT ===
        # Shapefile (ZIP)
        shp_folder = "output_shp"
        os.makedirs(shp_folder, exist_ok=True)
        shp_path = os.path.join(shp_folder, "koordinat.shp")
        gdf_points.to_file(shp_path)

        zip_filename = "shapefile_output.zip"
        with zipfile.ZipFile(zip_filename, 'w') as z:
            for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                fpath = shp_path.replace(".shp", ext)
                if os.path.exists(fpath):
                    z.write(fpath, os.path.basename(fpath))

        with open(zip_filename, "rb") as f:
            st.download_button("⬇️ Download Shapefile (ZIP)", f, "koordinat_shp.zip", mime="application/zip")

        # KML Titik
        kml_filename = "koordinat.kml"
        gdf_points.to_file(kml_filename, driver="KML")
        with open(kml_filename, "rb") as f:
            st.download_button("⬇️ Download KML (Titik)", f, "koordinat.kml", mime="application/vnd.google-earth.kml+xml")

        # KML Polygon
        if gdf_polygon is not None:
            kml_poly_filename = "koordinat_polygon.kml"
            gdf_polygon.to_file(kml_poly_filename, driver="KML")
            with open(kml_poly_filename, "rb") as f:
                st.download_button("⬇️ Download KML (Polygon)", f, "koordinat_polygon.kml", mime="application/vnd.google-earth.kml+xml")

        # === PREVIEW PETA ===
        st.subheader("Preview Peta")
        m = folium.Map(location=[coords[0][1], coords[0][0]], zoom_start=17)
        folium.PolyLine([(lat, lon) for lon, lat in coords],
                        color="blue", weight=2.5).add_to(m)
        for i, (lon, lat) in enumerate(coords, start=1):
            folium.CircleMarker(location=[lat, lon],
                                radius=3,
                                popup=f"Point {i}",
                                color="red").add_to(m)
        st_folium(m, width=900, height=600)

    else:
        st.error("Tidak ada koordinat yang terbaca dari tabel PDF.")
