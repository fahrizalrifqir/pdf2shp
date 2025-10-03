# ======================
# === Layout Peta PNG (A3) ===
# ======================
if gdf_polygon is not None:
    st.subheader("🖼️ Layout Peta (PNG) - Format A3")

    out_png = "layout_peta.png"
    fig, ax = plt.subplots(figsize=(16.5, 11.7), dpi=150)  # A3 landscape

    # plot geometri
    gdf_polygon.to_crs(epsg=3857).plot(ax=ax, facecolor="none", edgecolor="yellow", linewidth=2)
    if gdf_tapak is not None:
        gdf_tapak.to_crs(epsg=3857).plot(ax=ax, facecolor="red", alpha=0.4, edgecolor="red")
    if gdf_points is not None:
        gdf_points.to_crs(epsg=3857).plot(ax=ax, color="orange", edgecolor="black", markersize=60)

    # basemap
    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery, attribution=False)

    # cek rasio bounding box geometri → tentukan posisi legenda
    bounds = gdf_polygon.to_crs(epsg=3857).total_bounds
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]

    if width >= height:
        # landscape → legenda di kanan atas
        leg_loc = "upper left"
        leg_anchor = (1.02, 1)
    else:
        # portrait → legenda di bawah tengah
        leg_loc = "upper center"
        leg_anchor = (0.5, -0.05)

    # legenda
    legend_elements = [
        mlines.Line2D([], [], color="orange", marker="o", markeredgecolor="black",
                      linestyle="None", markersize=8, label="PKKPR (Titik)"),
        mpatches.Patch(facecolor="none", edgecolor="yellow", linewidth=2, label="PKKPR (Polygon)"),
        mpatches.Patch(facecolor="red", edgecolor="red", alpha=0.4, label="Tapak Proyek"),
    ]
    leg = ax.legend(
        handles=legend_elements,
        title="Legenda",
        loc=leg_loc,
        bbox_to_anchor=leg_anchor,
        fontsize=12,
        title_fontsize=14,
        frameon=True,
        facecolor="white"
    )
    leg.get_frame().set_alpha(0.7)  # transparan

    # judul peta
    ax.set_title("Peta Kesesuaian Tapak Proyek dengan PKKPR", fontsize=18, weight="bold")

    # hilangkan axis
    ax.set_axis_off()

    plt.savefig(out_png, dpi=300, bbox_inches="tight")
    with open(out_png, "rb") as f:
        st.download_button("⬇️ Download Layout Peta (PNG, A3)", f, "layout_peta.png", mime="image/png")

    st.pyplot(fig)
