# =========================================================
# FULL STREAMLIT PKKPR
# FINAL FIX VERSION
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

from shapely.geometry import (
    Point,
    Polygon,
    MultiPolygon,
    GeometryCollection,
    MultiPoint,
    LineString,
)

from shapely.validation import make_valid
from shapely.ops import polygonize_full

import folium
from streamlit_folium import st_folium
from folium.plugins import Fullscreen

import pdfplumber

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines

import contextily as ctx
import xyzservices.providers as xyz

# =========================================================
# CONFIG
# =========================================================
st.set_page_config(
    page_title="PKKPR → SHP + Overlay",
    layout="wide"
)

st.title("PKKPR → Shapefile Converter & Overlay Tapak Proyek")
st.markdown("---")

DEBUG = st.sidebar.checkbox("Debug Mode", value=False)

INDO_BOUNDS = (
    95.0,
    141.0,
    -11.0,
    6.0
)

# =========================================================
# FORMAT ANGKA
# =========================================================
def format_angka_id(value):

    try:

        val = float(value)

        if abs(val - round(val)) < 0.001:

            return f"{int(round(val)):,}".replace(",", ".")

        else:

            s = f"{val:,.2f}"

            s = (
                s.replace(",", "X")
                 .replace(".", ",")
                 .replace("X", ".")
            )

            return s

    except:
        return str(value)

# =========================================================
# UTM INFO
# =========================================================
def get_utm_info(lon, lat):

    zone = int((lon + 180) / 6) + 1

    if lat >= 0:
        epsg = 32600 + zone
    else:
        epsg = 32700 + zone

    zone_label = f"{zone}{'N' if lat >= 0 else 'S'}"

    return epsg, zone_label

# =========================================================
# TRY PARSE FLOAT
# =========================================================
def try_parse_float(s):

    try:
        return float(str(s).strip().replace(",", "."))

    except:
        return None

# =========================================================
# DMS TO DECIMAL
# =========================================================
def dms_to_decimal(dms_str):

    if not dms_str:
        return None

    s = str(dms_str).upper().strip()

    s = (
        s.replace("BT", "E")
         .replace("BB", "W")
         .replace("LS", "S")
         .replace("LU", "N")
         .replace("º", "°")
         .replace("’", "'")
         .replace("″", '"')
    )

    direction = None

    m_dir = re.search(r"[NSEW]", s)

    if m_dir:
        direction = m_dir.group(0)

    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", s)

    if not nums:
        return None

    try:

        deg = float(nums[0])

        minutes = float(nums[1]) if len(nums) > 1 else 0

        seconds = float(nums[2]) if len(nums) > 2 else 0

    except:
        return None

    val = deg + (minutes / 60) + (seconds / 3600)

    if direction in ["S", "W"]:
        val *= -1

    return val

# =========================================================
# FIX GEOMETRY
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

    gdf["geometry"] = (
        gdf.geometry
        .buffer(0)
        .simplify(0.0000001, preserve_topology=True)
    )

    return gdf

# =========================================================
# SORT CLOCKWISE
# =========================================================
def sort_coords_clockwise(coords):

    cx = sum(x for x, y in coords) / len(coords)

    cy = sum(y for x, y in coords) / len(coords)

    coords_sorted = sorted(
        coords,
        key=lambda p: math.atan2(
            p[1] - cy,
            p[0] - cx
        )
    )

    return coords_sorted

# =========================================================
# PDF PARSER
# =========================================================
def extract_tables_and_coords_from_pdf(uploaded_file):

    coords_plain = []

    text_all = ""

    ordered_from_table = False

    with pdfplumber.open(uploaded_file) as pdf:

        for page in pdf.pages:

            text_all += (page.extract_text() or "") + "\n"

    coords_with_no = []

    with pdfplumber.open(uploaded_file) as pdf:

        for page in pdf.pages:

            table = page.extract_table()

            if not table:
                continue

            try:
                df = pd.DataFrame(
                    table[1:],
                    columns=table[0]
                )

            except:
                df = pd.DataFrame(table)

            df.columns = [
                re.sub(r"\s+", " ", str(c)).strip().lower()
                for c in df.columns
            ]

            no_col = None
            bujur_col = None
            lintang_col = None

            for col in df.columns:

                if re.match(r"no\b", col):
                    no_col = col

                if any(k in col for k in [
                    "bujur",
                    "longitude",
                    "long",
                    "x"
                ]):
                    bujur_col = col

                if any(k in col for k in [
                    "lintang",
                    "latitude",
                    "lat",
                    "y"
                ]):
                    lintang_col = col

            if bujur_col and lintang_col:

                for _, row in df.iterrows():

                    raw_no = row.get(no_col, None)

                    raw_lon = str(
                        row.get(bujur_col, "")
                    ).strip()

                    raw_lat = str(
                        row.get(lintang_col, "")
                    ).strip()

                    def looks_like_dms(s):

                        return any(sym in s.upper() for sym in [
                            "°",
                            "'",
                            '"',
                            "BT",
                            "LS",
                            "LU",
                            "E",
                            "W"
                        ])

                    lon = (
                        dms_to_decimal(raw_lon)
                        if looks_like_dms(raw_lon)
                        else try_parse_float(raw_lon)
                    )

                    lat = (
                        dms_to_decimal(raw_lat)
                        if looks_like_dms(raw_lat)
                        else try_parse_float(raw_lat)
                    )

                    if lon and lat:

                        if (
                            not (95 <= lon <= 141 and -11 <= lat <= 6)
                            and
                            (95 <= lat <= 141 and -11 <= lon <= 6)
                        ):
                            lon, lat = lat, lon

                        if 95 <= lon <= 141 and -11 <= lat <= 6:

                            try:
                                n = int(str(raw_no).strip())

                            except:
                                n = None

                            coords_with_no.append(
                                (n, lon, lat)
                            )

    if coords_with_no:

        coords_with_no.sort(
            key=lambda x: (
                x[0] if x[0] is not None else 99999
            )
        )

        coords_plain = [
            (lon, lat)
            for _, lon, lat in coords_with_no
        ]

        ordered_from_table = True

    # remove duplicate
    seen = set()

    unique_coords = []

    for xy in coords_plain:

        key = (
            round(xy[0], 6),
            round(xy[1], 6)
        )

        if key not in seen:

            unique_coords.append(xy)

            seen.add(key)

    return {
        "coords": unique_coords,
        "ordered": ordered_from_table
    }

# =========================================================
# SAVE SHAPEFILE
# =========================================================
def save_shapefile_layers(gdf_poly, gdf_points):

    with tempfile.TemporaryDirectory() as tmpdir:

        if gdf_poly is not None:

            gdf_poly.to_crs(
                epsg=4326
            ).to_file(
                os.path.join(
                    tmpdir,
                    "PKKPR_Polygon.shp"
                )
            )

        if gdf_points is not None:

            gdf_points.to_crs(
                epsg=4326
            ).to_file(
                os.path.join(
                    tmpdir,
                    "PKKPR_Points.shp"
                )
            )

        buf = io.BytesIO()

        with zipfile.ZipFile(
            buf,
            "w",
            zipfile.ZIP_DEFLATED
        ) as zf:

            for f in os.listdir(tmpdir):

                zf.write(
                    os.path.join(tmpdir, f),
                    arcname=f
                )

        buf.seek(0)

        return buf.read()

# =========================================================
# UI UPLOAD
# =========================================================
st.subheader("📄 Upload Dokumen PKKPR")

uploaded = st.file_uploader(
    "Upload PDF / SHP ZIP",
    type=["pdf", "zip"]
)

gdf_polygon = None
gdf_points = None

# =========================================================
# READ PDF
# =========================================================
if uploaded:

    if uploaded.name.lower().endswith(".pdf"):

        parsed = extract_tables_and_coords_from_pdf(
            uploaded
        )

        coords = parsed["coords"]

        ordered_flag = parsed["ordered"]

        if coords:

            pts = [
                Point(x, y)
                for x, y in coords
            ]

            gdf_points = gpd.GeoDataFrame(
                geometry=pts,
                crs="EPSG:4326"
            )

            coords_proc = coords.copy()

            if not ordered_flag:

                coords_proc = sort_coords_clockwise(
                    coords_proc
                )

            if coords_proc[0] != coords_proc[-1]:

                coords_proc.append(coords_proc[0])

            poly_candidate = None

            try:

                poly_candidate = Polygon(coords_proc)

                if (
                    not poly_candidate.is_valid
                    or
                    poly_candidate.area == 0
                ):

                    poly_candidate = poly_candidate.buffer(0)

                if (
                    not poly_candidate.is_valid
                    or
                    poly_candidate.area == 0
                ):

                    ls = LineString(coords_proc)

                    polys, _, _, _ = polygonize_full(ls)

                    poly_list = list(polys)

                    if poly_list:

                        poly_candidate = max(
                            poly_list,
                            key=lambda p: p.area
                        )

            except Exception as e:

                if DEBUG:
                    st.write(e)

            if (
                poly_candidate is not None
                and
                poly_candidate.is_valid
                and
                poly_candidate.area > 0
            ):

                gdf_polygon = gpd.GeoDataFrame(
                    geometry=[poly_candidate],
                    crs="EPSG:4326"
                )

                gdf_polygon = fix_geometry(
                    gdf_polygon
                )

                st.success(
                    f"Berhasil membuat polygon dari {len(coords)} titik"
                )

            else:

                st.warning(
                    "Polygon gagal dibuat"
                )

# =========================================================
# READ SHP ZIP
# =========================================================
    elif uploaded.name.lower().endswith(".zip"):

        with tempfile.TemporaryDirectory() as tmp:

            zf = zipfile.ZipFile(
                io.BytesIO(uploaded.read())
            )

            zf.extractall(tmp)

            for root, _, files in os.walk(tmp):

                for f in files:

                    if f.lower().endswith(".shp"):

                        try:

                            gdf_polygon = gpd.read_file(
                                os.path.join(root, f)
                            )

                            break

                        except Exception as e:

                            if DEBUG:
                                st.write(e)

        if gdf_polygon is not None:

            gdf_polygon = fix_geometry(
                gdf_polygon
            )

            st.success(
                "Shapefile berhasil dibaca"
            )

# =========================================================
# ANALISIS LUAS
# =========================================================
if gdf_polygon is not None:

    centroid = (
        gdf_polygon.to_crs(4326)
        .geometry.centroid.iloc[0]
    )

    utm_epsg, utm_zone = get_utm_info(
        centroid.x,
        centroid.y
    )

    luas_utm = (
        gdf_polygon
        .to_crs(utm_epsg)
        .area.sum()
    )

    luas_mercator = (
        gdf_polygon
        .to_crs(epsg=3857)
        .area.sum()
    )

    st.write(
        f"Luas UTM {utm_zone}: "
        f"{format_angka_id(luas_utm)} m²"
    )

    st.write(
        f"Luas Proyeksi Mercator (EPSG:3857): "
        f"{format_angka_id(luas_mercator)} m²"
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
st.subheader("🏗️ Upload Tapak")

uploaded_tapak = st.file_uploader(
    "Upload SHP ZIP Tapak",
    type=["zip"]
)

gdf_tapak = None

if uploaded_tapak and gdf_polygon is not None:

    with tempfile.TemporaryDirectory() as tmp:

        zf = zipfile.ZipFile(
            io.BytesIO(uploaded_tapak.read())
        )

        zf.extractall(tmp)

        for root, _, files in os.walk(tmp):

            for f in files:

                if f.lower().endswith(".shp"):

                    try:

                        gdf_tapak = gpd.read_file(
                            os.path.join(root, f)
                        )

                        break

                    except Exception as e:

                        if DEBUG:
                            st.write(e)

    if gdf_tapak is not None:

        gdf_tapak = fix_geometry(gdf_tapak)

        st.success("Tapak berhasil dibaca")

# =========================================================
# OVERLAY
# =========================================================
if gdf_polygon is not None and gdf_tapak is not None:

    st.subheader("📊 Analisis Overlay")

    centroid = (
        gdf_polygon.to_crs(4326)
        .geometry.centroid.iloc[0]
    )

    utm_epsg, utm_zone = get_utm_info(
        centroid.x,
        centroid.y
    )

    gdf_poly_utm = gdf_polygon.to_crs(
        utm_epsg
    )

    gdf_tapak_utm = gdf_tapak.to_crs(
        utm_epsg
    )

    inter = gpd.overlay(
        gdf_tapak_utm,
        gdf_poly_utm,
        how="intersection"
    )

    luas_overlap = inter.area.sum()

    luas_tapak = gdf_tapak_utm.area.sum()
    luas_di_luar = max(0, luas_tapak - luas_overlap)

    st.write(
        f"Luas Tapak: "
        f"{format_angka_id(luas_tapak)} m²"
    )

    st.write(
        f"Luas Overlay: "
        f"{format_angka_id(luas_overlap)} m²"
    )

    st.write(
        f"Luas Tapak di luar PKKPR: "
        f"{format_angka_id(luas_di_luar)} m²"
    )

# =========================================================
# PREVIEW MAP
# =========================================================
if gdf_polygon is not None:

    st.subheader("🌍 Preview Peta")

    if gdf_tapak is not None:
        combined_preview = pd.concat([
            gdf_polygon.to_crs(4326),
            gdf_tapak.to_crs(4326)
        ], ignore_index=True)
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
        xyz.Esri.WorldImagery
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
                "fillOpacity": 0.35
            }
        ).add_to(m)

    if gdf_points is not None:

        for i, row in gdf_points.iterrows():

            folium.CircleMarker(
                [row.geometry.y, row.geometry.x],
                radius=4,
                color="black",
                fill=True,
                fill_color="orange",
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
        width=1000,
        height=600
    )

# =========================================================
# PNG EXPORT
# =========================================================
if gdf_polygon is not None:

    st.subheader("🖼 Export PNG")

    try:

        # ============================================
        # CONVERT CRS
        # ============================================
        gdf_poly_3857 = gdf_polygon.to_crs(epsg=3857).copy()

        gdf_poly_3857["geometry"] = (
            gdf_poly_3857.geometry
            .buffer(0)
        )

        if gdf_tapak is not None:
            gdf_tapak_extent = gdf_tapak.to_crs(epsg=3857).copy()
            gdf_tapak_extent["geometry"] = gdf_tapak_extent.geometry.buffer(0)

            extent_gdf = pd.concat([
                gdf_poly_3857,
                gdf_tapak_extent
            ], ignore_index=True)
        else:
            extent_gdf = gdf_poly_3857

        # ============================================
        # EXTENT
        # ============================================
        xmin, ymin, xmax, ymax = extent_gdf.total_bounds

        width = xmax - xmin
        height = ymax - ymin

        padx = max(width * 0.08, 50)
        pady = max(height * 0.08, 50)

        # ============================================
        # FIGURE
        # ============================================
        fig, ax = plt.subplots(
            figsize=(10, 10),
            dpi=300
        )

        # ============================================
        # PLOT DULU POLYGON
        # ============================================
        gdf_poly_3857.plot(
            ax=ax,
            facecolor="none",
            edgecolor="yellow",
            linewidth=2.5,
            joinstyle="miter",
            capstyle="projecting",
            zorder=5
        )

        # ============================================
        # TAPAK
        # ============================================
        if gdf_tapak is not None:

            try:

                gdf_tapak_3857 = gdf_tapak.to_crs(epsg=3857)

                gdf_tapak_3857.plot(
                    ax=ax,
                    facecolor="red",
                    edgecolor="red",
                    alpha=0.35,
                    linewidth=1.5,
                    zorder=4
                )

            except Exception as e:

                if DEBUG:
                    st.write("Plot tapak gagal:", e)

        # ============================================
        # TITIK
        # ============================================
        if gdf_points is not None and not gdf_points.empty:

            try:

                gdf_points_3857 = gdf_points.to_crs(epsg=3857)

                gdf_points_3857.plot(
                    ax=ax,
                    color="orange",
                    edgecolor="black",
                    markersize=30,
                    zorder=6
                )

            except Exception as e:

                if DEBUG:
                    st.write("Plot titik gagal:", e)

        # ============================================
        # SET EXTENT DULU
        # ============================================
        ax.set_xlim(
            xmin - padx,
            xmax + padx
        )

        ax.set_ylim(
            ymin - pady,
            ymax + pady
        )

        # ============================================
        # BARU TAMBAH BASEMAP
        # ============================================
        try:

            extent_width = xmax - xmin
            extent_height = ymax - ymin
            max_extent = max(extent_width, extent_height)

            if max_extent < 300:
                dynamic_zoom = 21
            elif max_extent < 800:
                dynamic_zoom = 20
            elif max_extent < 2000:
                dynamic_zoom = 19
            elif max_extent < 5000:
                dynamic_zoom = 18
            else:
                dynamic_zoom = 17

            ctx.add_basemap(
                ax,
                source=ctx.providers.Esri.WorldImagery,
                crs=gdf_poly_3857.crs.to_string(),
                zoom=dynamic_zoom
            )

        except Exception as e:

            if DEBUG:
                st.write("Esri gagal:", e)

            try:

                ctx.add_basemap(
                    ax,
                    source=ctx.providers.OpenStreetMap.Mapnik,
                    crs=gdf_poly_3857.crs.to_string(),
                    zoom=18
                )

            except Exception as e2:

                if DEBUG:
                    st.write("OSM gagal:", e2)

                ax.set_facecolor("#d9d9d9")

        # ============================================
        # TITLE
        # ============================================
        ax.set_title(
            "Peta Kesesuaian Tapak Proyek dengan PKKPR",
            fontsize=14,
            pad=15
        )

        ax.axis("off")

        # ============================================
        # LEGEND
        # ============================================
        legend_elements = [

            mpatches.Patch(
                facecolor="none",
                edgecolor="yellow",
                linewidth=2,
                label="PKKPR (Polygon)"
            ),

            mpatches.Patch(
                facecolor="red",
                edgecolor="red",
                alpha=0.4,
                label="Tapak Proyek"
            ),

            mlines.Line2D(
                [],
                [],
                color="orange",
                marker="o",
                markeredgecolor="black",
                linestyle="None",
                markersize=8,
                label="PKKPR (Titik)"
            )
        ]

        ax.legend(
            handles=legend_elements,
            loc="upper right",
            fontsize=9,
            frameon=True,
            facecolor="white",
            edgecolor="black",
            title="Keterangan"
        )

        # ============================================
        # SAVE PNG
        # ============================================
        buf = io.BytesIO()

        plt.savefig(
            buf,
            format="png",
            bbox_inches="tight",
            pad_inches=0.2,
            dpi=300
        )

        buf.seek(0)

        plt.close(fig)

        st.download_button(
            "⬇️ Download Peta PNG",
            data=buf,
            file_name="Peta_Overlay.png",
            mime="image/png"
        )

    except Exception as e:

        st.error(f"Gagal membuat PNG: {e}")

        if DEBUG:
            st.exception(e)
