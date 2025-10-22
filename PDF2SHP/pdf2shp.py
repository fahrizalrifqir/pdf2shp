import streamlit as st
import geopandas as gpd
import pandas as pd
import io, os, zipfile, shutil, re, tempfile
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
from shapely.validation import make_valid
from shapely import affinity
from pyproj import Transformer

# ======================
# KONFIGURASI
# ======================
st.set_page_config(page_title="PKKPR → SHP + Overlay (Hirarki Final)", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek (Hirarki Final)")
st.markdown("---")

DEBUG = st.sidebar.checkbox("Tampilkan debug logs", value=False)

# ======================
# FUNGSI BANTUAN
# ======================
def normalize_text(s):
    if not s: return s
    s = str(s).replace('\u2019', "'").replace('\u201d', '"').replace('\u201c', '"')
    s = s.replace('’', "'").replace('“', '"').replace('”', '"').replace('\xa0', ' ')
    return s

def get_utm_info(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return epsg, f"{zone}{'N' if lat >= 0 else 'S'}"

def format_angka_id(value):
    try:
        val = float(value)
        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")
        else:
            return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(value)

def parse_luas_line(line):
    """Ambil teks luas dan satuannya langsung dari dokumen"""
    if not line:
        return None
    m = re.search(r"luas\s*tanah.*?([\d\.,]+)\s*([a-z²0-9 ]*)", line, flags=re.IGNORECASE)
    if not m:
        return None
    angka = m.group(1).strip()
    satuan = m.group(2).replace(" ", "").upper()
    # Normalisasi satuan agar tetap seperti di dokumen
    if "HA" in satuan:
        satuan = "Ha"
    elif "M2" in satuan or "M²" in satuan or "M" in satuan:
        satuan = "m²"
    else:
        satuan = satuan or ""
    return f"{angka} {satuan}".strip()

def save_shapefile_layers(gdf_poly, gdf_point):
    with tempfile.TemporaryDirectory() as tmpdir:
        gdf_poly.to_file(os.path.join(tmpdir, "PKKPR_Polygon.shp"))
        gdf_point.to_file(os.path.join(tmpdir, "PKKPR_Point.shp"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in os.listdir(tmpdir):
                zf.write(os.path.join(tmpdir, f), arcname=f)
        buf.seek(0)
        return buf.read()

def fix_geometry(gdf):
    gdf["geometry"] = gdf["geometry"].apply(lambda g: make_valid(g))
    b = gdf.total_bounds
    if not (-180 <= b[0] <= 180 and -90 <= b[1] <= 90):
        for fac in [10, 100, 1000, 10000]:
            g2 = gdf.copy()
            g2["geometry"] = g2["geometry"].apply(lambda g: affinity.scale(g, 1/fac, 1/fac, origin=(0,0)))
            b2 = g2.total_bounds
            if 95 <= b2[0] <= 141 and -11 <= b2[1] <= 6:
                return g2.set_crs(epsg=4326, allow_override=True)
    return gdf

# ======================
# UPLOAD FILE PKKPR
# ======================
col1, col2 = st.columns([0.7, 0.3])
uploaded_pkkpr = col1.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

coords, luas_pkkpr_doc, luas_label = [], None, None
gdf_polygon, gdf_points = None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.endswith(".pdf"):
        coords_disetujui, coords_dimohon, coords_plain = [], [], []
        luas_disetujui, luas_dimohon = None, None
        mode = None

        try:
            with pdfplumber.open(uploaded_pkkpr) as pdf:
                for page in pdf.pages:
                    table = page.extract_table()
                    text = page.extract_text() or ""
                    for line in text.split("\n"):
                        low = line.lower().strip()

                        # Deteksi luas langsung dari dokumen
                        if "luas tanah yang disetujui" in low and not luas_disetujui:
                            luas_disetujui = parse_luas_line(line)
                        elif "luas tanah yang dimohon" in low and not luas_dimohon:
                            luas_dimohon = parse_luas_line(line)

                        # Ubah mode sesuai label
                        if "koordinat" in low and "disetujui" in low:
                            mode = "disetujui"
                        elif "koordinat" in low and "dimohon" in low:
                            mode = "dimohon"

                        # Ambil dari baris teks
                        m = re.match(r"^\s*\d+\s+([0-9\.\-]+)\s+([0-9\.\-]+)", line)
                        if m:
                            try:
                                x, y = float(m.group(1)), float(m.group(2))
                                if 95 <= x <= 141 and -11 <= y <= 6:
                                    if mode == "disetujui": coords_disetujui.append((x, y))
                                    elif mode == "dimohon": coords_dimohon.append((x, y))
                                    else: coords_plain.append((x, y))
                            except: pass

                    # Ambil dari tabel (kalau ada)
                    if table:
                        for row in table:
                            if len(row) >= 3:
                                try:
                                    x, y = float(row[1]), float(row[2])
                                    if 95 <= x <= 141 and -11 <= y <= 6:
                                        if mode == "disetujui": coords_disetujui.append((x, y))
                                        elif mode == "dimohon": coords_dimohon.append((x, y))
                                        else: coords_plain.append((x, y))
                                except: continue

            # Pilih sesuai hirarki
            if coords_disetujui:
                coords = coords_disetujui
                luas_pkkpr_doc, luas_label = luas_disetujui, "disetujui"
            elif coords_dimohon:
                coords = coords_dimohon
                luas_pkkpr_doc, luas_label = luas_dimohon, "dimohon"
            elif coords_plain:
                coords = coords_plain
                luas_pkkpr_doc, luas_label = None, "tanpa label"

            if coords:
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                gdf_points = gpd.GeoDataFrame(pd.DataFrame(coords, columns=["Lon", "Lat"]),
                                              geometry=[Point(x, y) for x, y in coords], crs="EPSG:4326")
                gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(coords)], crs="EPSG:4326")
                gdf_polygon = fix_geometry(gdf_polygon)
                st.success(f"✅ {len(coords)} titik terdeteksi ({luas_label})")
            else:
                st.warning("Tidak ada koordinat yang valid ditemukan di PDF.")

        except Exception as e:
            st.error(f"Gagal memproses PDF: {e}")

    elif uploaded_pkkpr.name.endswith(".zip"):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                zip_ref = zipfile.ZipFile(io.BytesIO(uploaded_pkkpr.read()), 'r')
                zip_ref.extractall(tmp)
                gdf_polygon = gpd.read_file(tmp)
                if gdf_polygon.crs is None:
                    gdf_polygon.set_crs(epsg=4326, inplace=True)
                gdf_polygon = fix_geometry(gdf_polygon)
                st.success("✅ Shapefile PKKPR terbaca")
        except Exception as e:
            st.error(f"Gagal membaca shapefile PKKPR: {e}")

# ======================
# ANALISIS LUAS
# ======================
if gdf_polygon is not None:
    try:
        centroid = gdf_polygon.geometry.centroid.iloc[0]
        utm_epsg, utm_zone = get_utm_info(centroid.x, centroid.y)
        luas_utm = gdf_polygon.to_crs(epsg=utm_epsg).area.sum()
        luas_merc = gdf_polygon.to_crs(epsg=3857).area.sum()
        if luas_pkkpr_doc:
            st.info(f"📏 **Luas PKKPR (dari dokumen, {luas_label})**: {luas_pkkpr_doc}")
        st.success(f"**Analisis Luas Geometri**\n- Luas (UTM {utm_zone}): {format_angka_id(luas_utm)} m²\n- Luas (Mercator): {format_angka_id(luas_merc)} m²")
    except Exception as e:
        st.error(f"Gagal menghitung luas: {e}")
    st.markdown("---")

    try:
        zip_bytes = save_shapefile_layers(gdf_polygon, gdf_points)
        st.download_button("⬇️ Download SHP PKKPR (Polygon + Point)", zip_bytes, "PKKPR_Hasil_Konversi.zip", mime="application/zip")
    except Exception as e:
        st.error(f"Gagal menyiapkan shapefile: {e}")

# ======================
# PETA INTERAKTIF
# ======================
if gdf_polygon is not None:
    st.subheader("🌍 Peta Interaktif")
    centroid = gdf_polygon.geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=None)
    Fullscreen(position="bottomleft").add_to(m)
    folium.TileLayer("openstreetmap", name="OpenStreetMap").add_to(m)
    folium.TileLayer("CartoDB Positron", name="CartoDB Positron").add_to(m)
    folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery").add_to(m)
    folium.GeoJson(gdf_polygon, name="PKKPR Polygon",
                   style_function=lambda x: {"color": "yellow", "weight": 3, "fillOpacity": 0.1}).add_to(m)
    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker([row.geometry.y, row.geometry.x], radius=4,
                                color="black", fill=True, fill_color="orange",
                                popup=f"Titik {i+1}").add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, width=900, height=600)
    st.markdown("---")

# ======================
# LAYOUT PNG
# ======================
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG)")
    try:
        gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
        xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
        width, height = xmax - xmin, ymax - ymin
        fig, ax = plt.subplots(figsize=(14, 10) if width > height else (10, 14), dpi=150)
        gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2.5, label="Batas PKKPR")
        if gdf_points is not None:
            gdf_points.to_crs(epsg=3857).plot(ax=ax, color="orange", edgecolor="black", markersize=30, label="Titik PKKPR")
        try:
            ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)
        except: pass
        legend = [
            mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=1.5, label="Batas PKKPR"),
            mlines.Line2D([], [], color="orange", marker="o", linestyle="None", label="Titik PKKPR")
        ]
        ax.legend(handles=legend, title="Legenda", loc="upper right", fontsize=8, title_fontsize=9)
        ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14, weight="bold")
        ax.set_axis_off()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=300, bbox_inches="tight")
        buf.seek(0)
        st.download_button("⬇️ Download Layout Peta (PNG)", buf, "layout_peta.png", mime="image/png")
    except Exception as e:
        st.error(f"Gagal membuat layout peta: {e}")
