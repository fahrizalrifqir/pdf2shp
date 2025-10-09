import streamlit as st
import geopandas as gpd
import pandas as pd
import io
import os
import zipfile
import shutil
import re
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
from pyproj import Transformer

# ======================
# === Konfigurasi App ===
# ======================
st.set_page_config(page_title="PKKPR → SHP + Overlay", layout="wide")
st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")

# ======================
# === Helper Formatting & Fungsi ===
# ======================

def fmt_number_id(num, decimals=2):
    """
    Format number memakai gaya Indonesia:
    - ribuan: '.'  (dot)
    - desimal: ',' (comma)
    """
    try:
        s = f"{num:,.{decimals}f}"  # default: 1,234,567.89
        # swap to Indonesian: 1.234.567,89
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return s
    except Exception:
        return str(num)

def format_area_dynamic(value_m2):
    """
    Ambil nilai area dalam m² (value_m2) lalu kembalikan (display_value_str, unit_str)
    - Jika >= 10000 m² -> tampilkan dalam Ha (value_m2 / 10000) dengan 2 desimal
    - Jika < 10000 m² -> tampilkan dalam m² dengan 2 desimal
    """
    if value_m2 is None:
        return "-", ""
    try:
        if value_m2 >= 10000:
            val_ha = value_m2 / 10000.0
            return f"{fmt_number_id(val_ha, 2)}", "Ha"
        else:
            return f"{fmt_number_id(value_m2, 2)}", "m²"
    except Exception:
        return str(value_m2), "m²"

def format_doc_area(value_doc, unit_doc):
    """
    Format untuk luas yang tercantum di dokumen:
    - value_doc: numeric (nilai sesuai dokumen)
    - unit_doc: "Ha" atau "m²" (jika None atau unknown, fallback ke m²)
    Output: formatted string seperti '20,76 Ha' (menggunakan pemisah Indonesia)
    """
    if value_doc is None:
        return "-"
    try:
        if unit_doc is None:
            unit_doc = "m²"
        # Jika unit dokumen Ha, tampilkan dengan 2 desimal
        if unit_doc.lower() in ["ha", "hektar"]:
            return f"{fmt_number_id(float(value_doc), 2)} Ha"
        else:
            # m²
            return f"{fmt_number_id(float(value_doc), 2)} m²"
    except Exception:
        # fallback plain
        return f"{value_doc} {unit_doc or ''}"

# ======================
# === Fungsi GIS & Util ===
# ======================

def get_utm_info_from_lonlat(lon, lat):
    """Return (epsg_code, zone_label) from lon/lat (WGS84)."""
    zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        epsg = 32600 + zone
        zone_label = f"{zone}N"
    else:
        epsg = 32700 + zone
        zone_label = f"{zone}S"
    return epsg, zone_label

def save_shapefile(gdf, folder_name, zip_name):
    """
    Save GeoDataFrame as ESRI Shapefile into folder_name and zip it to zip_name.zip.
    Returns path to zip file.
    """
    if os.path.exists(folder_name):
        shutil.rmtree(folder_name)
    os.makedirs(folder_name, exist_ok=True)
    shp_path = os.path.join(folder_name, "data.shp")
    gdf.to_file(shp_path, driver="ESRI Shapefile")
    zip_path = f"{zip_name}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for file in os.listdir(folder_name):
            zf.write(os.path.join(folder_name, file), arcname=file)
    return zip_path

def parse_luas(line):
    """Ekstrak nilai luas dan satuan sesuai dokumen (Ha atau m²). Mengembalikan (nilai_numeric, satuan_string)"""
    match = re.search(r"([\d\.\,]+)", line)
    if not match:
        return None, None
    num_str = match.group(1)
    # normalisasi pemisah ribuan/desimal
    if "." in num_str and "," in num_str:
        num_str = num_str.replace(".", "").replace(",", ".")
    elif "," in num_str:
        num_str = num_str.replace(",", ".")
    try:
        val = float(num_str)
        if re.search(r"\b(ha|hektar)\b", line.lower()):
            satuan = "Ha"
        else:
            satuan = "m²"
        return val, satuan
    except:
        return None, None

def deduplicate_coords(coords, tolerance=1e-6):
    """
    Hilangkan duplikasi koordinat akibat teks ganda / OCR,
    tetapi jangan menghapus titik pertama dan terakhir (biarkan jika memang identik karena polygon tertutup).
    """
    if not coords:
        return coords
    unique = []
    n = len(coords)
    for i, (lon, lat) in enumerate(coords):
        # selalu simpan index 0 dan index akhir
        if i == 0 or i == n - 1:
            unique.append((lon, lat))
            continue
        found = False
        for ulon, ulat in unique:
            if abs(lon - ulon) < tolerance and abs(lat - ulat) < tolerance:
                found = True
                break
        if not found:
            unique.append((lon, lat))
    return unique

def detect_and_transform_coords(coords):
    """
    Deteksi & transform coords projected -> lon/lat (EPSG:4326).
    Mengembalikan (transformed_coords_list, detected_zone_int_or_None, hemi 'N'/'S'/None)
    Catatan: fungsi tidak menampilkan pesan log deteksi.
    """
    if not coords:
        return coords, None, None

    # cek apakah sudah lon/lat (rentang Indonesia)
    try:
        lon_ok = all(95 <= float(x) <= 141 for x, y in coords)
        lat_ok = all(-11 <= float(y) <= 6 for x, y in coords)
        if lon_ok and lat_ok:
            return [(float(x), float(y)) for x, y in coords], None, None
    except Exception:
        pass

    # jika tidak cukup bukti projected (nilai kecil), kembalikan langsung sebagai float (best-effort)
    if not all(abs(float(x)) > 1000 and abs(float(y)) > 1000 for x, y in coords):
        return [(float(x), float(y)) for x, y in coords], None, None

    zone_candidates = range(46, 56)  # cakupan umum untuk Indonesia
    orders = [("easting_northing", lambda x, y: (x, y)), ("northing_easting", lambda x, y: (y, x))]
    best = {"score": -1, "transformed": None, "zone": None, "hemi": None, "order": None}

    for order_name, reorder in orders:
        reordered_inputs = [reorder(float(x), float(y)) for x, y in coords]
        for zone in zone_candidates:
            for hemi in ("N", "S"):
                epsg = 32600 + zone if hemi == "N" else 32700 + zone
                transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
                try:
                    transformed = [transformer.transform(xx, yy) for xx, yy in reordered_inputs]
                except Exception:
                    continue
                cnt_in = sum(1 for lon, lat in transformed if 95 <= lon <= 141 and -11 <= lat <= 6)
                if cnt_in > best["score"]:
                    best.update({"score": cnt_in, "transformed": transformed, "zone": zone, "hemi": hemi, "order": order_name})

    # fallback scanning (jika belum ada satu pun yang masuk)
    if best["transformed"] is None or best["score"] <= 0:
        try:
            for zone in zone_candidates:
                for hemi in ("N", "S"):
                    epsg_try = 32600 + zone if hemi == "N" else 32700 + zone
                    transformer_try = Transformer.from_crs(f"EPSG:{epsg_try}", "EPSG:4326", always_xy=True)
                    try:
                        transformed_try = [transformer_try.transform(float(x), float(y)) for x, y in coords]
                    except Exception:
                        continue
                    cnt_in_try = sum(1 for lon, lat in transformed_try if 95 <= lon <= 141 and -11 <= lat <= 6)
                    if cnt_in_try > best["score"]:
                        best.update({"score": cnt_in_try, "transformed": transformed_try, "zone": zone, "hemi": hemi, "order": "fallback_scan"})
        except Exception:
            pass

    if best["transformed"] is not None and best["score"] >= 1:
        return [(float(lon), float(lat)) for lon, lat in best["transformed"]], best["zone"], best["hemi"]

    # gagal transform -> kembalikan as floats
    return [(float(x), float(y)) for x, y in coords], None, None

def hitung_luas_wgs84_mercator(gdf):
    """Hitung luas pada proyeksi WGS 84 / Pseudo Mercator (EPSG:3857)"""
    if gdf is None or gdf.empty:
        return None
    gdf_3857 = gdf.to_crs(epsg=3857)
    return gdf_3857.area.sum()

# ======================
# === Upload PKKPR ===
# ======================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_pkkpr = st.file_uploader("📂 Upload PKKPR (PDF koordinat atau Shapefile ZIP)", type=["pdf", "zip"])

coords, gdf_points, gdf_polygon = [], None, None
luas_pkkpr_doc, luas_pkkpr_doc_label, satuan_luas = None, None, None
detected_pkkpr_zone, detected_pkkpr_hemi = None, None

if uploaded_pkkpr:
    if uploaded_pkkpr.name.lower().endswith(".pdf"):
        coords_plain = []
        luas_disetujui, luas_dimohon = None, None
        try:
            with pdfplumber.open(uploaded_pkkpr) as pdf:
                for page in pdf.pages:
                    # coba ekstrak tabel
                    table = page.extract_table()
                    if table:
                        for row in table:
                            # asumsi: kolom 1=idx, kolom2=x, kolom3=y (sesuaikan jika beda)
                            if len(row) >= 3:
                                try:
                                    x, y = float(row[1]), float(row[2])
                                    coords_plain.append((x, y))
                                except:
                                    continue
                    # ekstrak teks baris demi baris untuk luas & pola koordinat
                    text = page.extract_text()
                    if not text:
                        continue
                    for line in text.split("\n"):
                        low = line.lower().strip()
                        if "luas tanah yang disetujui" in low and luas_disetujui is None:
                            luas_disetujui, satuan_luas = parse_luas(line)
                        elif "luas tanah yang dimohon" in low and luas_dimohon is None:
                            luas_dimohon, satuan_luas = parse_luas(line)
                        # pola: index easting northing  -> contoh: "1 414695.19 90214.00"
                        m = re.match(r"^\s*\d+\s+([0-9\.\-]+)\s+([0-9\.\-]+)", line)
                        if m:
                            try:
                                coords_plain.append((float(m.group(1)), float(m.group(2))))
                            except:
                                continue
        except Exception as e:
            st.error(f"Gagal membaca PDF: {e}")
            coords_plain = []

        # deduplicate tetapi pertahankan titik pertama & terakhir
        coords_unique = deduplicate_coords(coords_plain)
        # deteksi & transform jika perlu
        coords_transformed, detected_zone, detected_hemi = detect_and_transform_coords(coords_unique)
        detected_pkkpr_zone, detected_pkkpr_hemi = detected_zone, detected_hemi
        coords = coords_transformed
        luas_pkkpr_doc = luas_disetujui or luas_dimohon
        luas_pkkpr_doc_label = "disetujui" if luas_disetujui else "dimohon" if luas_dimohon else "tidak tercantum"

        if coords:
            gdf_points = gpd.GeoDataFrame(
                pd.DataFrame(coords, columns=["Longitude", "Latitude"]),
                geometry=[Point(xy) for xy in coords],
                crs="EPSG:4326",
            )
            if len(coords) > 2:
                poly_coords = coords.copy()
                # pastikan polygon tertutup
                if poly_coords[0] != poly_coords[-1]:
                    poly_coords.append(poly_coords[0])
                gdf_polygon = gpd.GeoDataFrame(geometry=[Polygon(poly_coords)], crs="EPSG:4326")

        with col2:
            st.markdown(f"<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ {len(coords)} titik</p>", unsafe_allow_html=True)

    elif uploaded_pkkpr.name.lower().endswith(".zip"):
        # unzip dan baca shapefile
        try:
            if os.path.exists("pkkpr_shp"):
                shutil.rmtree("pkkpr_shp")
            with zipfile.ZipFile(uploaded_pkkpr, "r") as z:
                z.extractall("pkkpr_shp")
            gdf_polygon = gpd.read_file("pkkpr_shp")
            if gdf_polygon.crs is None:
                gdf_polygon.set_crs(epsg=4326, inplace=True)
            with col2:
                st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ (SHP)</p>", unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Error membaca shapefile PKKPR: {e}")

# === Ekspor SHP PKKPR ===
if gdf_polygon is not None:
    try:
        zip_pkkpr_only = save_shapefile(gdf_polygon, "out_pkkpr_only", "PKKPR_Hasil_Konversi")
        with open(zip_pkkpr_only, "rb") as f:
            st.download_button("⬇️ Download SHP PKKPR (ZIP)", f, file_name="PKKPR_Hasil_Konversi.zip", mime="application/zip")
    except Exception as e:
        st.error(f"Gagal ekspor SHP PKKPR: {e}")

# ======================
# === Analisis PKKPR Sendiri ===
# ======================
if gdf_polygon is not None:
    # tentukan zona UTM PKKPR: pakai detected jika ada, kalau tidak gunakan centroid
    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    if detected_pkkpr_zone is not None:
        utm_epsg = 32600 + detected_pkkpr_zone if detected_pkkpr_hemi == "N" else 32700 + detected_pkkpr_zone
        utm_zone_label = f"{detected_pkkpr_zone}{detected_pkkpr_hemi}"
    else:
        utm_epsg, utm_zone_label = get_utm_info_from_lonlat(centroid.x, centroid.y)

    # hitung luas di UTM & Mercator (hasil dalam m²)
    gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)
    luas_pkkpr_utm_m2 = gdf_polygon_utm.area.sum()
    luas_pkkpr_mercator_m2 = hitung_luas_wgs84_mercator(gdf_polygon)

    # format tampilan: dokumen sesuai satuan dokumen, hasil analisis pake logika otomatis
    doc_str = format_doc_area(luas_pkkpr_doc, satuan_luas)
    utm_disp_val, utm_disp_unit = format_area_dynamic(luas_pkkpr_utm_m2)
    merc_disp_val, merc_disp_unit = format_area_dynamic(luas_pkkpr_mercator_m2)

    # gabungkan string yang rapi
    utm_line = f"{utm_disp_val} {utm_disp_unit}" if utm_disp_unit else "-"
    merc_line = f"{merc_disp_val} {merc_disp_unit}" if merc_disp_unit else "-"

    st.info(f"""
**Perbandingan Luas PKKPR (berdasarkan proyeksi):**
- Luas PKKPR (dokumen): {doc_str} ({luas_pkkpr_doc_label})
- Luas PKKPR (UTM Zona {utm_zone_label}): {utm_line}
- Luas PKKPR (WGS 84 / Pseudo Mercator): {merc_line}
""")
    st.markdown("---")

# ================================
# === Upload Tapak Proyek (SHP) ===
# ================================
col1, col2 = st.columns([0.7, 0.3])
with col1:
    uploaded_tapak = st.file_uploader("📂 Upload Shapefile Tapak Proyek (ZIP)", type=["zip"])

gdf_tapak = None
if uploaded_tapak:
    try:
        if os.path.exists("tapak_shp"):
            shutil.rmtree("tapak_shp")
        with zipfile.ZipFile(uploaded_tapak, "r") as z:
            z.extractall("tapak_shp")
        gdf_tapak = gpd.read_file("tapak_shp")
        if gdf_tapak.crs is None:
            gdf_tapak.set_crs(epsg=4326, inplace=True)
        with col2:
            st.markdown("<p style='color: green; font-weight: bold; padding-top: 3.5rem;'>✅ Tapak dibaca</p>", unsafe_allow_html=True)
    except Exception as e:
        with col2:
            st.markdown("<p style='color: red; font-weight: bold; padding-top: 3.5rem;'>❌ Gagal dibaca</p>", unsafe_allow_html=True)
        st.error(f"Error membaca shapefile tapak: {e}")

# ======================
# === Analisis Overlay ===
# ======================
if gdf_polygon is not None and gdf_tapak is not None:
    st.subheader("📊 Analisis Overlay PKKPR & Tapak Proyek")

    # zona UTM untuk tapak otomatis berdasarkan centroid tapak
    centroid_tapak = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg_tapak, utm_zone_label_tapak = get_utm_info_from_lonlat(centroid_tapak.x, centroid_tapak.y)

    # gunakan untuk analisis overlay
    try:
        gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg_tapak)
    except Exception:
        gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg_tapak)

    # gunakan PKKPR UTM yang dihitung sebelumnya (utm_epsg variable)
    if 'utm_epsg' not in locals():
        centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
        utm_epsg, utm_zone_label = get_utm_info_from_lonlat(centroid.x, centroid.y)
        gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)

    luas_tapak_m2 = gdf_tapak_utm.area.sum()
    luas_pkkpr_hitung_m2 = gdf_polygon_utm.area.sum()

    try:
        inter = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection")
        luas_overlap_m2 = inter.area.sum() if not inter.empty else 0.0
    except Exception:
        luas_overlap_m2 = 0.0
        for a in gdf_tapak_utm.geometry:
            for b in gdf_polygon_utm.geometry:
                try:
                    intersec = a.intersection(b)
                    luas_overlap_m2 += intersec.area if not intersec.is_empty else 0.0
                except Exception:
                    continue

    luas_outside_m2 = luas_tapak_m2 - luas_overlap_m2

    # format hasil overlay mengikuti aturan otomatis (Ha jika >=10000 m2)
    tapak_disp_val, tapak_disp_unit = format_area_dynamic(luas_tapak_m2)
    pkkprh_disp_val, pkkprh_disp_unit = format_area_dynamic(luas_pkkpr_hitung_m2)
    overlap_disp_val, overlap_disp_unit = format_area_dynamic(luas_overlap_m2)
    outside_disp_val, outside_disp_unit = format_area_dynamic(luas_outside_m2)

    st.info(f"""
**Analisis Luas Tapak Proyek (Proyeksi UTM Zona {utm_zone_label_tapak}):**
- Total Luas Tapak Proyek: {tapak_disp_val} {tapak_disp_unit}
- Luas PKKPR (dokumen): {format_doc_area(luas_pkkpr_doc, satuan_luas)} ({luas_pkkpr_doc_label})
- Luas PKKPR (hitung dari geometri): {pkkprh_disp_val} {pkkprh_disp_unit}
- Luas Tapak Proyek di dalam PKKPR: **{overlap_disp_val} {overlap_disp_unit}**
- Luas Tapak Proyek di luar PKKPR: **{outside_disp_val} {outside_disp_unit}**
""")
    st.markdown("---")

# ======================
# === Preview Interaktif ===
# ======================
if gdf_polygon is not None:
    st.subheader("🌍 Preview Peta Interaktif")
    tile_choice = st.selectbox("Pilih Basemap:", ["OpenStreetMap", "Esri World Imagery"])
    tile_provider = xyz["Esri"]["WorldImagery"] if tile_choice == "Esri World Imagery" else xyz["OpenStreetMap"]["Mapnik"]

    centroid = gdf_polygon.to_crs(epsg=4326).geometry.centroid.iloc[0]
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=17, tiles=tile_provider)
    Fullscreen(position="bottomleft").add_to(m)

    folium.GeoJson(
        gdf_polygon.to_crs(epsg=4326),
        name="PKKPR",
        style_function=lambda x: {"color": "yellow", "weight": 2, "fillOpacity": 0}
    ).add_to(m)

    if gdf_tapak is not None:
        folium.GeoJson(
            gdf_tapak.to_crs(epsg=4326),
            name="Tapak Proyek",
            style_function=lambda x: {"color": "red", "weight": 1, "fillColor": "red", "fillOpacity": 0.4}
        ).add_to(m)

    if gdf_points is not None:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=5,
                color="black",
                fill=True,
                fill_color="orange",
                fill_opacity=1,
                popup=f"Titik {i+1}"
            ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=900, height=600)
    st.markdown("---")

# ======================
# === Layout Peta PNG ===
# ======================
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG) - Auto Size")
    out_png = "layout_peta.png"

    try:
        gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857)
        xmin, ymin, xmax, ymax = gdf_poly_3857.total_bounds
        width, height = xmax - xmin, ymax - ymin
        if width <= 0 or height <= 0:
            figsize = (10, 8)
        else:
            figsize = (14, 10) if width > height else (10, 14)

        fig, ax = plt.subplots(figsize=figsize, dpi=150)
        gdf_poly_3857.plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)

        if gdf_tapak is not None:
            gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)
            gdf_tapak_3857.plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")

        if gdf_points is not None:
            gdf_points_3857 = gdf_points.to_crs(epsg=3857)
            gdf_points_3857.plot(ax=ax, color="orange", edgecolor="black", markersize=25)

        legend_elements = [
            mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black", linestyle="None", markersize=5, label="PKKPR (Titik)"),
            mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=1.5, label="PKKPR (Polygon)"),
            mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
        ]
        leg = ax.legend(handles=legend_elements, title="Legenda", loc="upper right",
                        bbox_to_anchor=(0.98, 0.98), fontsize=8, title_fontsize=9,
                        markerscale=0.8, labelspacing=0.3, frameon=True)
        leg.get_frame().set_alpha(0.7)

        try:
            ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery, attribution=False)
        except Exception:
            pass

        if width > 0 and height > 0:
            ax.set_xlim(xmin - width * 0.05, xmax + width * 0.05)
            ax.set_ylim(ymin - height * 0.05, ymax + height * 0.05)

        ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=14, weight="bold")
        ax.set_axis_off()
        plt.savefig(out_png, dpi=300, bbox_inches="tight")
        with open(out_png, "rb") as f:
            st.download_button("⬇️ Download Layout Peta (PNG, Auto)", f, "layout_peta.png", mime="image/png")
        st.pyplot(fig)
    except Exception as e:
        st.error(f"Gagal membuat layout peta PNG: {e}")

# ======================
# === Selesai ===
# ======================
st.markdown("<br><small>Catatan: Aplikasi ini otomatis mendeteksi proyeksi/UTM. Pesan log internal 'Duplicate removed' dan 'Deteksi koordinat PKKPR' disembunyikan sesuai permintaan.</small>", unsafe_allow_html=True)
