import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile
from shapely.geometry import Point
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
            parts = line.strip().split()
            if len(parts) >= 3:
                try:
                    lon, lat = float(parts[1]), float(parts[2])
                    coords.append((lon, lat))
                except:
                    continue

    if coords:
        st.success(f"Berhasil menemukan {len(coords)} titik koordinat.")

        gdf = gpd.GeoDataFrame(
            pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
            geometry=[Point(xy) for xy in coords],
            crs="EPSG:4326"
        )

        # Preview peta
        m = folium.Map(location=[coords[0][1], coords[0][0]], zoom_start=13)
        folium.PolyLine([(lat, lon) for lon, lat in coords],
                        color="blue", weight=2.5).add_to(m)
        for i, (lon, lat) in enumerate(coords, start=1):
            folium.CircleMarker(location=[lat, lon],
                                radius=3,
                                popup=f"Point {i}",
                                color="red").add_to(m)
        st_folium(m, width=900, height=600)

        # Simpan SHP (ZIP)
        shp_folder = "output_shp"
        os.makedirs(shp_folder, exist_ok=True)
        shp_path = os.path.join(shp_folder, "koordinat.shp")
        gdf.to_file(shp_path)

        zip_filename = "shapefile_output.zip"
        with zipfile.ZipFile(zip_filename, 'w') as z:
            for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                fpath = shp_path.replace(".shp", ext)
                if os.path.exists(fpath):
                    z.write(fpath, os.path.basename(fpath))

        with open(zip_filename, "rb") as f:
            st.download_button("Download Shapefile (ZIP)", f, "koordinat_shp.zip", mime="application/zip")

        # Simpan KML
        kml_filename = "koordinat.kml"
        gdf.to_file(kml_filename, driver="KML")

        with open(kml_filename, "rb") as f:
            st.download_button("Download KML", f, "koordinat.kml", mime="application/vnd.google-earth.kml+xml")
    else:
        st.warning("Tidak ada koordinat yang ditemukan di PDF.")
