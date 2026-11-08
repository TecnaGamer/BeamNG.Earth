# World labels — Natural Earth (public domain)

`world_labels.json.gz` holds the 3D map's far-out labels: continents, oceans and seas, countries, states and
provinces, populated places, mountain ranges and deserts. It is made by `tools/build_world_labels.py` from
Natural Earth 1:10m v5.1.2 (the GeoJSON of github.com/nvkelso/natural-earth-vector, tag v5.1.2):
`ne_10m_geography_regions_polys`, `ne_10m_geography_marine_polys`, `ne_10m_admin_0_countries`,
`ne_10m_admin_1_states_provinces` and `ne_10m_populated_places_simple`.

Natural Earth is in the public domain (https://www.naturalearthdata.com/about/terms-of-use/). No permission
is needed to use, modify or distribute it. We credit it anyway: "Made with Natural Earth."

Each row is `[kind, name, lat, lon, min zoom, max zoom, rank, extra]`. The zooms are Natural Earth's own
label zooms (min_zoom / MIN_LABEL / max_label). A polygon's point is the grid point farthest from the edges of
its largest part.
