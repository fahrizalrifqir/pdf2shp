import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, re
from shapely.geometry import Point, Polygon
import fitz  # PyMuPDF
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="PDF Koordinat → SHP/KML", layout="wide")
st.title("PDF Koordinat → Shapefile & KML Converter")

uploaded_file = st.file_uploader("Upload file PDF", type=["pdf"])

if uploaded_file:
    # Baca PDF dengan PyMuPDF
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")

    coords = []
    for page in doc:
        text = page.get_text("text")
        for line in text.split("\n"):
            # Cari dua angka float dalam setiap baris (lon, lat)
            numbers = re.findall(r"-?\d+\.\d+", line)
            if len(numbers) >= 2:
                try:
                    lon, lat = float(numbers[0]), float(numbers[1])
                    coords.append((lon, lat))
                except:
                    continue

    if coords:
        st.success(f"Berhasil menemukan {len(coords)} titik koordinat.")

        # Buat GeoDataFrame (point)
        gdf_points = gpd.GeoDataFrame(
            pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
            geometry=[Point(xy) for xy in coords],
            crs="EPSG:4326"
        )

        # Buat polygon jika titik lebih dari 2 dan membentuk area
        gdf_polygon = None
        if len(coords) > 2:
            poly = Polygon(coords)
            gdf_polygon = gpd.GeoDataFrame(
                geometry=[poly], crs="EPSG:4326"
            )

        # === PREVIEW PETA ===
        m = folium.Map(location=[coords[0][1], coords[0][0]], zoom_start=17)
        # garis polyline
        folium.PolyLine([(lat, lon) for lon, lat in coords],
                        color="blue", weight=2.5).add_to(m)
        # titik koordinat
        for i, (lon, lat) in enumerate(coords, start=1):
            folium.CircleMarker(location=[lat, lon],
                                radius=3,
                                popup=f"Point {i}",
                                color="red").add_to(m)
        st_folium(m, width=900, height=600)

        # === SIMPAN SHP (ZIP) ===
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
            st.download_button(
                "Download Shapefile (ZIP)",
                f,
                file_name="koordinat_shp.zip",
                mime="application/zip"
            )

        # === SIMPAN KML (Point) ===
        kml_filename = "koordinat.kml"
        gdf_points.to_file(kml_filename, driver="KML")
        with open(kml_filename, "rb") as f:
            st.download_button(
                "Download KML (Titik)",
                f,
                file_name="koordinat.kml",
                mime="application/vnd.google-earth.kml+xml"
            )

        # === SIMPAN KML (Polygon) jika ada ===
        if gdf_polygon is not None:
            kml_poly_filename = "koordinat_polygon.kml"
            gdf_polygon.to_file(kml_poly_filename, driver="KML")
            with open(kml_poly_filename, "rb") as f:
                st.download_button(
                    "Download KML (Polygon)",
                    f,
                    file_name="koordinat_polygon.kml",
                    mime="application/vnd.google-earth.kml+xml"
                )

    else:
        st.warning("Tidak ada koordinat yang ditemukan di PDF.")
