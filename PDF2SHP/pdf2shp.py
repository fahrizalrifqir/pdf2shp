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
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return epsg, f"{zone}{'N' if lat >= 0 else 'S'}"


def save_shapefile(gdf):
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_shp_path = os.path.join(temp_dir, "PKKPR_Output.shp")
        gdf.to_crs(epsg=4326).to_file(temp_shp_path)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in os.listdir(temp_dir):
                zf.write(os.path.join(temp_dir, file), arcname=file)
        zip_buffer.seek(0)
        return zip_buffer.read()


def dms_to_decimal(dms_str):
    if not dms_str:
        return None
    dms_str = dms_str.strip().replace(" ", "").replace(",", ".")
    m = re.match(r"(\d+)[°](\d+)'([\d\.]+)\"?([A-Za-z]+)", dms_str)
    if not m:
        return None
    deg, minute, second, direction = m.groups()
    decimal = float(deg) + float(minute) / 60 + float(second) / 3600
    if direction.upper() in ["S", "LS", "W", "BB"]:
        decimal *= -1
    return decimal


def parse_luas_from_text(text):
    text_clean = re.sub(r"\s+", " ", (text or ""), flags=re.IGNORECASE)
    luas_matches = re.findall(
        r"luas\s*tanah\s*yang\s*(dimohon|disetujui)\s*[:\-]?\s*([\d\.,]+\s*(M2|M²))",
        text_clean,
        re.IGNORECASE,
    )
    luas_data = {}
    for label, value, satuan in luas_matches:
        luas_data[label.lower()] = (value.strip().upper() if value else "").replace(" ", "")

    if "disetujui" in luas_data:
        return luas_data["disetujui"], "disetujui"
    elif "dimohon" in luas_data:
        return luas_data["dimohon"], "dimohon"
    else:
        m = re.search(r"luas\s*tanah\s*[:\-]?\s*([\d\.,]+\s*(M2|M²))", text_clean, re.IGNORECASE)
        if m:
            return m.group(1).strip(), "tanpa judul"
        return None, "tidak ditemukan"


def format_angka_id(value):
    try:
        val = float(value)
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")
        else:
            return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(value)


# ======================
# === Upload PKKPR ===
# ======================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

coords_final, gdf_points, gdf_polygon = [], None, None
luas_pkkpr_doc, luas_pkkpr_doc_label = None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.endswith(".pdf"):
    full_text = ""
    coords_by_type = {"disetujui": [], "dimohon": [], "lainnya": []}
    found_priority = None
    found_disetujui, found_dimohon = False, False

    try:
        with st.spinner("🔍 Membaca dan mengekstrak koordinat dari PDF..."):
            with pdfplumber.open(uploaded_pkkpr) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    full_text += "\n" + text
                    low = text.lower()

                    blok_aktif = None
                    if "koordinat" in low and "disetujui" in low:
                        blok_aktif = "disetujui"
                        found_disetujui = True
                    elif "koordinat" in low and "dimohon" in low:
                        blok_aktif = "dimohon"
                        found_dimohon = True
                    else:
                        blok_aktif = "lainnya"

                    for tb in (page.extract_tables() or []):
                        if len(tb) <= 1:
                            continue
                        header = [str(c).lower().strip() for c in tb[0] if c]
                        idx_lon, idx_lat = -1, -1
                        try:
                            idx_lon = next(i for i, h in enumerate(header) if "bujur" in h)
                            idx_lat = next(i for i, h in enumerate(header) if "lintang" in h)
                        except StopIteration:
                            if len(header) >= 3 and any("no" in h for h in header):
                                idx_lon, idx_lat = 1, 2
                            elif len(header) == 2:
                                idx_lon, idx_lat = 0, 1

                        if idx_lon == -1 or idx_lat == -1:
                            continue

                        for row in tb[1:]:
                            if len(row) <= max(idx_lon, idx_lat):
                                continue
                            row_join = " ".join([str(x) for x in row if x]).strip()
                            if not re.search(r"\d+\.\d+", row_join):
                                continue

                            lon_str = str(row[idx_lon]).replace(",", ".").strip()
                            lat_str = str(row[idx_lat]).replace(",", ".").strip()
                            try:
                                lon_val = float(re.sub(r"[^\d\.\-]", "", lon_str))
                                lat_val = float(re.sub(r"[^\d\.\-]", "", lat_str))
                            except:
                                continue
                            if not (90 <= lon_val <= 145 and -11 <= lat_val <= 6):
                                continue

                            coords_by_type[blok_aktif].append((lon_val, lat_val))

        # Tentukan prioritas hasil
        if found_disetujui and coords_by_type["disetujui"]:
            coords_final = coords_by_type["disetujui"]
            coords_label = "disetujui"
        elif found_dimohon and coords_by_type["dimohon"]:
            coords_final = coords_by_type["dimohon"]
            coords_label = "dimohon"
        elif coords_by_type["lainnya"]:
            coords_final = coords_by_type["lainnya"]
            coords_label = "lainnya"
        else:
            coords_final, coords_label = [], "tidak ditemukan"

        luas_pkkpr_doc, luas_pkkpr_doc_label = parse_luas_from_text(full_text)

        if coords_final:
            if coords_final[0] != coords_final[-1]:
                coords_final.append(coords_final[0])

            gdf_points = gpd.GeoDataFrame(
                pd.DataFrame(coords_final, columns=["Longitude", "Latitude"]),
                geometry=[Point(xy) for xy in coords_final],
                crs="EPSG:4326"
            )
            gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(coords_final)], crs="EPSG:4326")

        with col2:
            st.markdown(f"<p style='color:green;font-weight:bold;padding-top:3.5rem;'>✅ {len(coords_final)} titik ({coords_label})</p>", unsafe_allow_html=True)

    except Exception as e:
        st.error(f"Gagal memproses PDF: {e}")
        gdf_polygon = None

            # Ambil sesuai prioritas
            if found_priority:
                coords_final = coords_by_type[found_priority]
                coords_label = found_priority
            else:
                coords_label = "tidak ditemukan"

            luas_pkkpr_doc, luas_pkkpr_doc_label = parse_luas_from_text(full_text)

            if coords_final:
                # Tutup poligon
                if coords_final[0] != coords_final[-1]:
                    coords_final.append(coords_final[0])

                gdf_points = gpd.GeoDataFrame(
                    pd.DataFrame(coords_final, columns=["Longitude", "Latitude"]),
                    geometry=[Point(xy) for xy in coords_final],
                    crs="EPSG:4326"
                )
                gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(coords_final)], crs="EPSG:4326")

            with col2:
                st.markdown(f"<p style='color:green;font-weight:bold;padding-top:3.5rem;'>✅ {len(coords_final)} titik ({coords_label})</p>", unsafe_allow_html=True)

        except Exception as e:
            st.error(f"Gagal memproses PDF: {e}")
            gdf_polygon = None

    elif uploaded_pkkpr.name.endswith(".zip"):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()), "r")
                zip_ref.extractall(temp_dir)
                zip_ref.close()
                gdf_polygon = gpd.read_file(temp_dir)
                if gdf_polygon.crs is None:
                    gdf_polygon.set_crs(epsg=4326, inplace=True)
                with col2:
                    st.markdown("<p style='color:green;font-weight:bold;padding-top:3.5rem;'>✅ Shapefile (PKKPR)</p>", unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Gagal membaca shapefile PKKPR: {e}")
            gdf_polygon = None


# =========================
# === Analisis & Overlay ===
# =========================
if gdf_polygon is not None:
    zip_pkkpr_bytes = save_shapefile(gdf_polygon)
    st.download_button("⬇️ Download SHP PKKPR (ZIP)", zip_pkkpr_bytes, "PKKPR_Hasil_Konversi.zip", mime="application/zip")

    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    luas_pkkpr_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
    luas_pkkpr_mercator = gdf_polygon.to_crs(epsg=3857).area.sum()

    luas_doc_str = f"{luas_pkkpr_doc} ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(
        f"**Analisis Luas Batas PKKPR**:\n"
        f"- Luas PKKPR (dokumen): **{luas_doc_str}**\n"
        f"- Luas PKKPR (UTM {utm_zone}): **{format_angka_id(luas_pkkpr_utm)} m²**\n"
        f"- Luas PKKPR (WGS84 Mercator): **{format_angka_id(luas_pkkpr_mercator)} m²**"
    )
    st.markdown("---")

# === Upload Tapak Proyek ===
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

gdf_tapak = None
if uploaded_tapak:
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_tapak.read()), "r")
            zip_ref.extractall(temp_dir)
            zip_ref.close()
            gdf_tapak = gpd.read_file(temp_dir)
            if gdf_tapak.crs is None:
                gdf_tapak.set_crs(epsg=4326, inplace=True)
            with col2:
                st.markdown("<p style='color:green;font-weight:bold;padding-top:3.5rem;'>✅</p>", unsafe_allow_html=True)
    except Exception as e:
        st.error(f"Gagal membaca shapefile Tapak Proyek: {e}")

# === Overlay ===
if gdf_polygon is not None and gdf_tapak is not None:
    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
    gdf_tapak_utm, gdf_polygon_utm = gdf_tapak.to_crs(epsg=utm_epsg), gdf_polygon.to_crs(epsg=utm_epsg)
    luas_tapak_utm = gdf_tapak_utm.area.sum()
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak_utm - luas_overlap

    st.success(
        f"**HASIL ANALISIS OVERLAY TAPAK PROYEK:**\n"
        f"- Total Luas Tapak (UTM {utm_zone}): **{format_angka_id(luas_tapak_utm)} m²**\n"
        f"- Luas di dalam PKKPR: **{format_angka_id(luas_overlap)} m²**\n"
        f"- Luas di luar PKKPR: **{format_angka_id(luas_outside)} m²**"
    )
    st.markdown("---")

# === Peta Interaktif ===
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None)
    Fullscreen(position="bottomleft").add_to(m)
    folium.TileLayer("openstreetmap", name="OpenStreetMap").add_to(m)
    folium.TileLayer("CartoDB Positron", name="CartoDB Positron").add_to(m)
    folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery").add_to(m)
    folium.GeoJson(gdf_polygon.to_crs(epsg=4326), name="PKKPR",
                   style_function=lambda x: {"color": "yellow", "weight": 3, "fillOpacity": 0.1}).add_to(m)
    if gdf_tapak is not None:
        folium.GeoJson(gdf_tapak.to_crs(epsg=4326), name="Tapak Proyek",
                       style_function=lambda x: {"color": "red", "weight": 2, "fillOpacity": 0.4}).add_to(m)
    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker([row.geometry.y, row.geometry.x], radius=4, color="black",
                                fill=True, fill_color="orange", fill_opacity=1,
                                popup=f"Titik {i+1}").add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, width=900, height=600)
    st.markdown("---")

# === Layout PNG ===
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG)")
    gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
    xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
    fig, ax = plt.subplots(figsize=(12, 10), dpi=150)
    gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2.5, label="Batas PKKPR")
    if gdf_tapak is not None:
        gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
        gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red", label="Tapak Proyek")
    if gdf_points is not None:
        gdf_points_3857 = gdf_points.to_crs(epsg=3857)
        gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=30, label="Titik PKKPR")
    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)
    ax.legend(title="Legenda", loc="upper right", fontsize=8)
    ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14, weight="bold")
    ax.set_axis_off()
    png_buffer = io.BytesIO()
    plt.savefig(png_buffer, format="png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    png_buffer.seek(0)
    st.download_button("⬇️ Download Layout Peta (PNG)", png_buffer, "layout_peta.png", mime="image/png")


