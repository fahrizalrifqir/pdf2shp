# ======================
# === Analisis + Ekspor SHP ===
# ======================

if gdf_polygon is not None:
    # Ekspor SHP PKKPR selalu tersedia begitu PKKPR ada
    zip_pkkpr = save_shapefile(gdf_polygon, "out_pkkpr", "PKKPR_Hasil")
    with open(zip_pkkpr, "rb") as f:
        st.download_button("⬇️ Download SHP PKKPR (ZIP)", f,
                           file_name="PKKPR_Hasil.zip", mime="application/zip")

if gdf_polygon is not None and gdf_tapak is not None:
    # Hanya kalau ada tapak proyek, lakukan analisis
    centroid = gdf_tapak.to_crs(epsg=4326).geometry.centroid.iloc[0]
    utm_epsg = get_utm_epsg(centroid.x, centroid.y)
    gdf_tapak_utm = gdf_tapak.to_crs(epsg=utm_epsg)
    gdf_polygon_utm = gdf_polygon.to_crs(epsg=utm_epsg)

    luas_tapak = gdf_tapak_utm.area.sum()
    luas_pkkpr_hitung = gdf_polygon_utm.area.sum()
    luas_overlap = gdf_tapak_utm.overlay(gdf_polygon_utm, how="intersection").area.sum()
    luas_outside = luas_tapak - luas_overlap

    luas_doc_str = f"{luas_pkkpr_doc:,.2f} m² ({luas_pkkpr_doc_label})" if luas_pkkpr_doc else "-"
    st.info(f"""
    **Analisis Luas Tapak Proyek (Proyeksi UTM {utm_epsg}):**
    - Total Luas Tapak Proyek: {luas_tapak:,.2f} m²
    - Luas PKKPR (dokumen): {luas_doc_str}
    - Luas PKKPR (hitung dari geometri): {luas_pkkpr_hitung:,.2f} m²
    - Luas di dalam PKKPR: {luas_overlap:,.2f} m²
    - Luas di luar PKKPR: {luas_outside:,.2f} m²
    """)

    # Download SHP Tapak
    zip_tapak = save_shapefile(gdf_tapak_utm, "out_tapak", "Tapak_Hasil_UTM")
    with open(zip_tapak, "rb") as f:
        st.download_button("⬇️ Download SHP Tapak Proyek (UTM)", f,
                           file_name="Tapak_Hasil_UTM.zip", mime="application/zip")
