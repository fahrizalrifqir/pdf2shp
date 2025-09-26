import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, re
from shapely.geometry import Point, Polygon
import folium
from streamlit_folium import st_folium
import pdfplumber

st.set_page_config(page_title="PDF Koordinat → SHP/KML", layout="wide")
st.title("PDF Koordinat → Shapefile & KML Converter")

uploaded_file = st.file_uploader("Upload file PDF", type=["pdf"])

def extract_coords_from_text(text):
    numbers = re.findall(r"-?\d+\.\d+", text)
    coords = []
    for i in range(0, len(numbers) - 1, 2):
        try:
            lon, lat = float(numbers[i]), float(numbers[i + 1])
            if 95 <= lon <= 141 and -11 <= lat <= 6:  # filter Indonesia
                coords.append((lon, lat))
        except:
            continue
    return coords

if uploaded_file:
    coords = []

    # --- ekstrak teks dengan pdfplumber ---
    all_text = ""
    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                all_text += text + "\n"

    coords = extract_coords_from_text(all_text)

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

        # === PREVIEW PETA ===
        m = folium.Map(location=[coords[0][1], coords[0][0]], zoom_start=17)
        folium.PolyLine([(lat, lon) for lon, lat in coords],
                        color="blue", weight=2.5).add_to(m)
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
            st.download_button("Download Shapefile (ZIP)", f, "koordinat_shp.zip", mime="application/zip")

        # === SIMPAN KML (Point) ===
        kml_filename = "koordinat.kml"
        gdf_points.to_file(kml_filename, driver="KML")
        with open(kml_filename, "rb") as f:
            st.download_button("Download KML (Titik)", f, "koordinat.kml", mime="application/vnd.google-earth.kml+xml")

        # === SIMPAN KML (Polygon) ===
        if gdf_polygon is not None:
            kml_poly_filename = "koordinat_polygon.kml"
            gdf_polygon.to_file(kml_poly_filename, driver="KML")
            with open(kml_poly_filename, "rb") as f:
                st.download_button("Download KML (Polygon)", f, "koordinat_polygon.kml", mime="application/vnd.google-earth.kml+xml")

    else:
        st.error("Tidak ada koordinat yang terbaca dari PDF (mungkin tabel berupa gambar).")
