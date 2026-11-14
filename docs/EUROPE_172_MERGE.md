# European infrastructure: integration with 1.7.2-beta

Merge target: `8375d5171454dcc8ca9005f9a34bf37229ff9523` (v1.7.2-beta).
Original contribution: `d5359f2a8b63c4d023e8437956f743541ff8e407` (PR #505).

1.7.2 builds road content in permanent geographic tiles and reuses it across
origin changes; it also prebuilds this content for saved areas/routes. Merely
resolving the five textual conflicts would omit regional data from those new
paths. This integration passes the geographic profile through live generation,
prebuilding, revisit keys and the map's world-cache signature. Changing the
profile cannot reuse a tile generated with a different profile.

Town/direction sign faces are included in retained geotile content, with their
placement pivot and height relief. The Lua consumer retains and repositions
them at a rebase, without adding face collisions (their supports remain
separate). Regional signs, chevrons and delineators respect geotile ownership,
including its precise edges, instead of just the rectangular bounding box.

The European sidewalk palette is applied to 1.7.2's native sidewalk meshes.
Vertex positions, triangle order, ramps and lean collision geometry remain
native. Contiguous palette batches preserve piece order at country borders;
tactile and brick UVs retain their existing mappings. Palette selection is
saved with the skin inputs and reused when draping into a new anchor.

The upstream junction-control functions and the complete Python/Lua navgraph
files are unchanged from 1.7.2. The default regional mode remains `off`.
The models/data and documented limitations of Europe v4 are unchanged.
No native source or binary change is introduced relative to 1.7.2.

## Validation

16 targeted Python modules: **208 passed, 2 skipped**, no failures.
The skipped roadgeo cases require an external real-map cache not present in
the test environment. The new offline `test_regional_geotiles` cases exercise:

- Live generation of a regional sign and European sidewalks, skin rebuilding
  after an approximately 15 km anchor move, and unchanged retained sign bytes.
- Prebuilding followed by live reuse, with distinct cache entries for `off`.
- The server's live/prebuild dispatch and its map-cache signature agreeing on
  the geographic profile.
- Mixed native/European palettes preserving geometry, collision and brick/
  tactile UVs.

Four Lua suites: **262 assertions passed**. `test_roadgeo` now also verifies
regional faces are non-colliding, retain object identity across rebases and
server restarts, and receive the correct new position.

Commands (from the repository root, with dependencies and matching earthfast):

```sh
python tools/run_tests.py test_regional_poles test_countrybounds test_regional_furniture test_regional_revision test_europe_infrastructure test_delineator_density test_regional_geotiles test_furniture test_navgraph test_sidewalk_collision test_blockworker test_revisit test_roadgeo test_geotiles test_areas test_signready --par 4 --timeout 240
luajit mod/tests/test_trees.lua
luajit mod/tests/test_roads.lua
luajit mod/tests/test_nightlights.lua
luajit mod/tests/test_roadgeo.lua
```

This does not replace an in-game Windows check of the merged version. Suggested
checks are the German B427 near Winden, a France/Germany border crossing, a
French village, Quebec/Ontario and Turkey; revisit a prebuilt area and trigger
a rebase/nearby teleport. Compare `--regional-poles europe` with `off`.

This merge targets the exact release above. Subsequent upstream commits may
require another merge; it does not promise compatibility with future versions.
