# Fences around industrial, military, and airport areas

Facility perimeters use full mapped boundaries rather than tile-clipped polygons. Industrial sites, military facilities, airports and quarries share the same selection and placement pipeline. Mapped physical fences take priority; independent secured compounds retain their own fences.

The server selects a geographic fence network first, then fits stock chain-link sections to the rendered terrain. Stable path identities, panel spacing and tile ownership keep connected boundaries consistent across tile edges and reloads. Duplicate source geometry is assigned one owner. Road, railway, bridge and building checks prevent unsafe placements; gate evidence preserves openings. Diagnostics distinguish missing source geometry from panels omitted during placement.

The client streams nearby stock fence sections with bounded work per frame, transforms them with the world origin, and removes them when their owning tiles unload. Collision activation is batched and can wait until the player clears a panel while its visible mesh remains present. Proprietary game assets are referenced at runtime and are not redistributed.

F. E. Warren uses the explicitly approved outer ways of OSM relation 252043 (`type=boundary`, `boundary=border_zone`). Its main alignment is preserved without inferred land-use edges, road-side relocation or invented connectors. The pinned source has two open chains; source gaps remain open and are reported. Other facilities retain the general routing behavior. See [source selection and refresh instructions](WARREN_AUTHORITATIVE_BOUNDARY.md).

Perimeters are enabled by default; `BNE_PERIMETERS=0` disables generation. `BNE_PERIMETER_DIAGNOSTICS=1` enables serialization diagnostics. The application packages the evidence and approved-source snapshots. Source changes require a server restart and client reload; ordinary source refreshes use scoped content keys rather than a manual cache purge.

Validation covers source selection, independent compounds, duplicate control, tile continuity, terrain fitting, transport/building clearance, serialization, streaming, rebase and collision behavior. The selected regression run completed 748 tests: 746 passed and two external-data cases were skipped. Offline photo-location replays use captured map inputs and synthetic terrain. The user reported satisfactory behavior; no additional in-game verification or performance benchmark is claimed.
