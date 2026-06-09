# FULL STREAMLIT PKKPR
# COMPLETE VERSION
# PDF PKKPR + SHP PKKPR + SHP TAPAK + ATTRIBUTE TABLE + OVERLAY + PNG EXPORT
# ROBUST COORDINATE PARSER
# =========================================================

import streamlit as st
import geopandas as gpd
import pandas as pd
import io
import os
import zipfile
import tempfile
import re
import math
import pdfplumber
import folium
import contextily as ctx
import xyzservices.providers as xyz
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines

from shapely.geometry import (
    Point,
    Polygon,
    MultiPolygon,
    GeometryCollection,
    LineString,
)

from shapely.validation import make_valid
from shapely.ops import polygonize_full

from streamlit_folium import st_folium
from folium.plugins import Fullscreen

# =========================================================
# CONFIG
# =========================================================
st.set_page_config(
    page_title="PKKPR Overlay Analyzer",
    layout="wide"
)

st.title("PKKPR → SHP + Overlay Tapak Proyek")
st.markdown("---")

DEBUG = st.sidebar.checkbox("Debug Mode", False)

# =========================================================
# FORMAT
# =========================================================
def format_angka_id(value):
    try:
        val = float(value)

        if abs(val - round(val)) < 0.001:
            return f"{int(round(val)):,}".replace(",", ".")

        s = f"{val:,.2f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")

    except:
        return str(value)

# =========================================================
# CRS
# =========================================================
def get_utm_info(lon, lat):
    zone = int((lon + 180) / 6) + 1

    if lat >= 0:
        epsg = 32600 + zone
    else:
        epsg = 32700 + zone

    return epsg, f"{zone}{'N' if lat >= 0 else 'S'}"

# =========================================================
# PARSE
# =========================================================
def try_parse_float(s):
    try:
        return float(str(s).strip().replace(",", "."))
    except:
        return None


def dms_to_decimal(coord):
    if coord is None:
        return None

    s = str(coord).upper().strip()

    s = (
        s.replace("BT", "E")
        .replace("BB", "W")
        .replace("LS", "S")
        .replace("LU", "N")
        .replace("º", "°")
        .replace("’", "'")
        .replace("′", "'")
        .replace("″", '"')
    )

    direction = None

    m = re.search(r"[NSEW]", s)
    if m:
        direction = m.group(0)

    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", s)

    if not nums:
        return None

    try:
        deg = float(nums[0])
        minutes = float(nums[1]) if len(nums) > 1 else 0
        seconds = float(nums[2]) if len(nums) > 2 else 0
    except:
        return None

    val = abs(deg) + (minutes / 60) + (seconds / 3600)

    if direction in ["S", "W"] or str(coord).strip().startswith("-"):
        val *= -1

    return val


def parse_any_coordinate(val):
    if val is None:
        return None

    s = str(val).strip()

    f = try_parse_float(s)

    if f is not None:
        return f

    return dms_to_decimal(s)


def normalize_lon_lat(a, b):
    if a is None or b is None:
        return None

    if 95 <= a <= 141 and -11 <= b <= 6:
        return (a, b)

    if 95 <= b <= 141 and -11 <= a <= 6:
        return (b, a)

    return None

# =========================================================
# GEOMETRY
# =========================================================
def fix_geometry(gdf):
    if gdf is None or gdf.empty:
        return gdf

    gdf = gdf.copy()

    gdf["geometry"] = gdf.geometry.apply(make_valid)

    def clean_geom(geom):
        if geom is None:
            return None

        if geom.geom_type == "GeometryCollection":
            polys = [
                g for g in geom.geoms
                if g.geom_type in ["Polygon", "MultiPolygon"]
            ]

            if len(polys) == 0:
                return None

            if len(polys) == 1:
                return polys[0]

            return MultiPolygon(polys)

        return geom

    gdf["geometry"] = gdf.geometry.apply(clean_geom)
    gdf = gdf[gdf.geometry.notnull()]
    gdf["geometry"] = gdf.geometry.buffer(0)

    return gdf


def sort_coords_clockwise(coords):
    cx = sum(x for x, y in coords) / len(coords)
    cy = sum(y for x, y in coords) / len(coords)

    return sorted(
        coords,
        key=lambda p: math.atan2(p[1] - cy, p[0] - cx)
    )

# =========================================================
# PDF COORD PARSER
# =========================================================
def parse_coords_from_text_block(block):
    coords = []

    lines = block.splitlines()

    for line in lines:
        nums = re.findall(r'[-+]?\d+(?:\.\d+)?', line)

        if len(nums) >= 2:
            a = parse_any_coordinate(nums[-2])
            b = parse_any_coordinate(nums[-1])

            xy = normalize_lon_lat(a, b)

            if xy:
                coords.append(xy)

    return coords


def extract_tables_and_coords_from_pdf(uploaded_file):

    uploaded_file.seek(0)

    coords_with_no = []

    # =====================================================
    # PRIORITAS 1 : BACA SEMUA TABEL PDF
    # =====================================================
    with pdfplumber.open(uploaded_file) as pdf:

        for page in pdf.pages:

            try:
                tables = page.extract_tables()
            except:
                tables = []

            for table in tables:

                if not table or len(table) < 2:
                    continue

                try:
                    df = pd.DataFrame(
                        table[1:],
                        columns=table[0]
                    )
                except:
                    continue

                df.columns = [
                    re.sub(r"\s+", " ", str(c)).strip().lower()
                    for c in df.columns
                ]

                no_col = None
                bujur_col = None
                lintang_col = None

                for c in df.columns:

                    if "no" in c:
                        no_col = c

                    if any(x in c for x in [
                        "bujur",
                        "longitude",
                        "long",
                        "x"
                    ]):
                        bujur_col = c

                    if any(x in c for x in [
                        "lintang",
                        "latitude",
                        "lat",
                        "y"
                    ]):
                        lintang_col = c

                if not (bujur_col and lintang_col):
                    continue

                for _, row in df.iterrows():

                    lon = parse_any_coordinate(
                        row.get(bujur_col)
                    )

                    lat = parse_any_coordinate(
                        row.get(lintang_col)
                    )

                    xy = normalize_lon_lat(
                        lon,
                        lat
                    )

                    if not xy:
                        continue

                    try:
                        n = int(
                            str(
                                row.get(no_col)
                            ).strip()
                        )
                    except:
                        n = 999999

                    coords_with_no.append(
                        (n, xy)
                    )

    # =====================================================
    # JIKA TABEL BERHASIL DIBACA
    # =====================================================
    if coords_with_no:

        coords_with_no.sort(
            key=lambda x: x[0]
        )

        coords = [
            xy
            for _, xy in coords_with_no
        ]

        return coords, True

    # =====================================================
    # FALLBACK TEXT PARSER
    # =====================================================
    uploaded_file.seek(0)

    full_text = ""

    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            full_text += (
                page.extract_text() or ""
            ) + "\n"

    coords = parse_coords_from_text_block(
        full_text
    )

    if len(coords) >= 3:
        return coords, True

    return [], False

# =========================================================
# SHP
# =========================================================
def read_shp_zip(uploaded):
    with tempfile.TemporaryDirectory() as tmp:
        zf = zipfile.ZipFile(io.BytesIO(uploaded.read()))
        zf.extractall(tmp)

        shp_path = None

        for root, _, files in os.walk(tmp):
            for f in files:
                if f.lower().endswith(".shp"):
                    shp_path = os.path.join(root, f)
                    break

        if shp_path:
            return gpd.read_file(shp_path)

    return None


def show_attributes(gdf, title):
    cols = [c for c in gdf.columns if c.lower() != "geometry"]

    if cols:
        st.subheader(title)
        st.dataframe(gdf[cols], use_container_width=True)


def save_shapefile_layers(gdf_poly, gdf_points):
    with tempfile.TemporaryDirectory() as tmpdir:
        if gdf_poly is not None:
            gdf_poly.to_crs(4326).to_file(
                os.path.join(tmpdir, "PKKPR_Polygon.shp")
            )

        if gdf_points is not None:
            gdf_points.to_crs(4326).to_file(
                os.path.join(tmpdir, "PKKPR_Points.shp")
            )

        buf = io.BytesIO()

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in os.listdir(tmpdir):
                zf.write(os.path.join(tmpdir, f), arcname=f)

        buf.seek(0)
        return buf.read()

# =========================================================
# UI PKKPR
# =========================================================
st.subheader("Upload Dokumen PKKPR")

col_upload, col_info = st.columns([3, 1])

with col_upload:
    uploaded = st.file_uploader(
        "Upload PDF / SHP ZIP",
        type=["pdf", "zip"]
    )
with col_info:
    info_box = st.empty()

gdf_polygon = None
gdf_points = None
# =========================================================
# PROCESS PKKPR
# =========================================================
if uploaded:

    if uploaded.name.lower().endswith(".pdf"):

        coords, ordered = extract_tables_and_coords_from_pdf(uploaded)

        if coords:

            # ==========================
            # TITIK KOORDINAT
            # ==========================
            gdf_points = gpd.GeoDataFrame(
                {
                    "No": list(range(1, len(coords) + 1))
                },
                geometry=[Point(x, y) for x, y in coords],
                crs="EPSG:4326"
            )

            coords_proc = coords.copy()

            # tutup polygon jika belum tertutup
            if coords_proc[0] != coords_proc[-1]:
                coords_proc.append(coords_proc[0])

            try:

                # ==========================
                # POLYGON ASLI DARI PDF
                # ==========================
                poly_candidate = Polygon(coords_proc)

                info_box.success(
                    f"""
                Jumlah titik : {len(coords)}

                Polygon valid : {"Ya" if poly_candidate.is_valid else "Tidak"}
                """
                )
               
                # jika tidak valid tampilkan info
                if not poly_candidate.is_valid:

                    try:
                        from shapely.validation import explain_validity

                        st.warning(
                            f"Polygon invalid : "
                            f"{explain_validity(poly_candidate)}"
                        )
                    except:
                        pass

                # ==========================
                # BUAT GDF
                # ==========================
                gdf_polygon = gpd.GeoDataFrame(
                    geometry=[poly_candidate],
                    crs="EPSG:4326"
                )

            except Exception as e:

                st.error(
                    f"Gagal membuat polygon : {e}"
                )

                gdf_polygon = None

        else:
            st.error("Koordinat PDF tidak ditemukan")

    elif uploaded.name.lower().endswith(".zip"):

        gdf_polygon = read_shp_zip(uploaded)

        if gdf_polygon is not None:

            st.success("SHP PKKPR berhasil dibaca")

            st.write("CRS :", gdf_polygon.crs)

            show_attributes(
                gdf_polygon,
                "Atribut SHP PKKPR"
            )

# =========================================================
# LUAS + DOWNLOAD SHP
# =========================================================
if gdf_polygon is not None:
    centroid = gdf_polygon.to_crs(4326).geometry.centroid.iloc[0]

    utm_epsg, utm_zone = get_utm_info(
        centroid.x,
        centroid.y
    )

    luas_utm = gdf_polygon.to_crs(utm_epsg).area.sum()
    luas_utm_ha = luas_utm / 10000
    luas_merc = gdf_polygon.to_crs(3857).area.sum()
    luas_merc_ha = luas_merc / 10000

    st.write(
        f"Luas UTM {utm_zone}: "
        f"{format_angka_id(luas_utm)} m² "
        f"({format_angka_id(luas_utm_ha)} Ha)"
    )

    st.write(
        f"Luas Mercator: "
        f"{format_angka_id(luas_merc)} m² "
        f"({format_angka_id(luas_merc_ha)} Ha)"
    )

    zip_bytes = save_shapefile_layers(
        gdf_polygon,
        gdf_points
    )

    st.download_button(
        "⬇️ Download SHP PKKPR",
        zip_bytes,
        "PKKPR_Hasil.zip",
        mime="application/zip"
    )

# =========================================================
# TAPAK
# =========================================================
st.subheader("Upload SHP ZIP Tapak")

uploaded_tapak = st.file_uploader(
    "Upload SHP ZIP Tapak",
    type=["zip"]
)

gdf_tapak = None

if uploaded_tapak and gdf_polygon is not None:
    gdf_tapak = read_shp_zip(uploaded_tapak)

    if gdf_tapak is not None:
        gdf_tapak = fix_geometry(gdf_tapak)

        st.success("SHP Tapak berhasil dibaca")

        show_attributes(
            gdf_tapak,
            "Atribut SHP Tapak"
        )

# =========================================================
# OVERLAY
# =========================================================
if gdf_polygon is not None and gdf_tapak is not None:
    st.subheader("Analisis Overlay")

    centroid = gdf_polygon.to_crs(4326).geometry.centroid.iloc[0]

    utm_epsg, utm_zone = get_utm_info(
        centroid.x,
        centroid.y
    )

    gdf_poly_utm = gdf_polygon.to_crs(utm_epsg)
    gdf_tapak_utm = gdf_tapak.to_crs(utm_epsg)

    inter = gpd.overlay(
        gdf_tapak_utm,
        gdf_poly_utm,
        how="intersection"
    )

    luas_overlap = inter.area.sum()
    luas_tapak  = gdf_tapak_utm.area.sum()
    luas_luar = max(0, luas_tapak - luas_overlap)

    st.write(
        f"Luas Tapak UTM {utm_zone}: "
        f"{format_angka_id(luas_tapak)} m² "
        f"({format_angka_id(luas_tapak/10000)} Ha)"
    )

    st.write(
        f"Luas Overlay: "
        f"{format_angka_id(luas_overlap)} m² "
        f"({format_angka_id(luas_overlap/10000)} Ha)"
    )

    st.write(
        f"Luas Tapak di luar PKKPR: "
        f"{format_angka_id(luas_luar)} m² "
        f"({format_angka_id(luas_luar/10000)} Ha)"
    )
    # =========================================================
# PREVIEW MAP
# =========================================================
if gdf_polygon is not None:
    st.subheader("Preview Peta")

    if gdf_tapak is not None:
        combined_preview = pd.concat(
            [
                gdf_polygon.to_crs(4326),
                gdf_tapak.to_crs(4326)
            ],
            ignore_index=True
        )
    else:
        combined_preview = gdf_polygon.to_crs(4326)

    centroid = combined_preview.geometry.unary_union.centroid

    m = folium.Map(
        location=[centroid.y, centroid.x],
        zoom_start=18,
        tiles=None
    )

    Fullscreen().add_to(m)

    folium.TileLayer(
        xyz.Esri.WorldImagery,
        name="Esri Satellite"
    ).add_to(m)

    folium.GeoJson(
        gdf_polygon.to_crs(4326),
        name="PKKPR",
        style_function=lambda x: {
            "color": "yellow",
            "weight": 3,
            "fillOpacity": 0.1
        }
    ).add_to(m)

    if gdf_tapak is not None:
        folium.GeoJson(
            gdf_tapak.to_crs(4326),
            name="Tapak",
            style_function=lambda x: {
                "color": "red",
                "fillColor": "red",
                "weight": 2,
                "fillOpacity": 0.35
            }
        ).add_to(m)

    if gdf_points is not None and not gdf_points.empty:
        for i, row in gdf_points.iterrows():
            folium.CircleMarker(
                location=[
                    row.geometry.y,
                    row.geometry.x
                ],
                radius=4,
                color="black",
                fill=True,
                fill_color="orange",
                fill_opacity=1,
                popup=f"Titik {i+1}"
            ).add_to(m)

    bounds = combined_preview.total_bounds

    m.fit_bounds([
        [bounds[1], bounds[0]],
        [bounds[3], bounds[2]]
    ])

    folium.LayerControl().add_to(m)

    st_folium(
        m,
        width=1200,
        height=650
    )

# =========================================================
# PNG EXPORT
# =========================================================
if gdf_polygon is not None:
    st.subheader("Export PNG")

    try:
        gdf_poly_3857 = gdf_polygon.to_crs(3857).copy()
        gdf_poly_3857["geometry"] = gdf_poly_3857.geometry.buffer(0)

        if gdf_tapak is not None:
            gdf_tapak_3857 = gdf_tapak.to_crs(3857).copy()
            gdf_tapak_3857["geometry"] = gdf_tapak_3857.geometry.buffer(0)

            extent_gdf = pd.concat(
                [gdf_poly_3857, gdf_tapak_3857],
                ignore_index=True
            )
        else:
            gdf_tapak_3857 = None
            extent_gdf = gdf_poly_3857

        xmin, ymin, xmax, ymax = extent_gdf.total_bounds

        width = xmax - xmin
        height = ymax - ymin

        padx = max(width * 0.20, 100)
        pady = max(height * 0.20, 100)

        fig, ax = plt.subplots(
            figsize=(10, 10),
            dpi=300
        )

        if gdf_tapak_3857 is not None:
            gdf_tapak_3857.plot(
                ax=ax,
                facecolor="red",
                edgecolor="red",
                alpha=0.35,
                linewidth=1.5,
                zorder=4
            )

        gdf_poly_3857.plot(
            ax=ax,
            facecolor="none",
            edgecolor="yellow",
            linewidth=2.5,
            zorder=5
        )

        if gdf_points is not None and not gdf_points.empty:
            gdf_points_3857 = gdf_points.to_crs(3857)

            gdf_points_3857.plot(
                ax=ax,
                color="orange",
                edgecolor="black",
                markersize=30,
                zorder=6
            )

        ax.set_xlim(
            xmin - padx,
            xmax + padx
        )

        ax.set_ylim(
            ymin - pady,
            ymax + pady
        )

        try:
            ctx.add_basemap(
                ax,
                source=ctx.providers.Esri.WorldImagery,
                crs=gdf_poly_3857.crs
            )
        except:
            ctx.add_basemap(
                ax,
                source=ctx.providers.OpenStreetMap.Mapnik,
                crs=gdf_poly_3857.crs
            )

        ax.set_title(
            "Peta Kesesuaian Tapak Proyek dengan PKKPR",
            fontsize=14
        )

        ax.axis("off")

        legend_elements = [
            mpatches.Patch(
                facecolor="none",
                edgecolor="yellow",
                linewidth=2,
                label="PKKPR"
            ),
            mpatches.Patch(
                facecolor="red",
                edgecolor="red",
                alpha=0.4,
                label="Tapak"
            ),
            mlines.Line2D(
                [],
                [],
                color="orange",
                marker="o",
                markeredgecolor="black",
                linestyle="None",
                markersize=8,
                label="Titik PKKPR"
            )
        ]

        # ==========================================
        # CARI SUDUT TERJAUH DARI POLYGON
        # ==========================================
        poly_centroid = gdf_poly_3857.unary_union.centroid
        
        corners = {
            "upper left":  (xmin, ymax),
            "upper right": (xmax, ymax),
            "lower left":  (xmin, ymin),
            "lower right": (xmax, ymin)
        }
        
        max_dist = -1
        best_corner = "upper right"
        
        for loc, (x, y) in corners.items():
        
            dist = (
                (poly_centroid.x - x) ** 2 +
                (poly_centroid.y - y) ** 2
            )
        
            if dist > max_dist:
                max_dist = dist
                best_corner = loc
        
        ax.legend(
            handles=legend_elements,
            loc=best_corner,
            frameon=True,
            facecolor="white",
            framealpha=0.9,
            edgecolor="black"
        )
        with st.spinner("Membuat peta PNG..."):

                fig.canvas.draw()

                buf = io.BytesIO()
            
                plt.savefig(
                    buf,
                    format="png",
                    bbox_inches="tight",
                    dpi=300
                )
            
                buf.seek(0)
            
                png_bytes = buf.getvalue()
                plt.close(fig)
    
        st.success("Peta PNG siap diunduh")
        
        st.download_button(
            "⬇️ Download Peta PNG",
            data=png_bytes,
            file_name="Peta_Overlay.png",
            mime="image/png"
        )

    except Exception as e:
        st.error(f"Gagal membuat PNG: {e}")
        # =========================================================
# END
# =========================================================
st.markdown("---")
st.caption("PKKPR Overlay Analyzer Ready")
