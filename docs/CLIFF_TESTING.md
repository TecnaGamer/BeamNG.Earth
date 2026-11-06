# Cliff rocks testing

This experimental feature adds terrain-fitted cliff faces, forest-instanced broken rocks,
and terrain-projected transition decals. Natural cliffs and road cuts use different materials,
with regional rock colors that avoid copying vegetation and satellite shadows directly.

## Install and controls

Use the installation steps in [QUICKSTART.md](QUICKSTART.md). Install both the mod and server
from the same build. Restart the server and game after replacing an earlier testing build so
loaded meshes and materials cannot mask a change. Normal driving streams cliffs automatically.

In the existing **BeamNG.Earth settings** UI, switch these independently:

- **Cliff faces**: fitted meshes and their near-vehicle collision.
- **Scattered cliff rocks**: broken rock collections around steep cliff transitions.
- **Cliff transition decals**: rock textures projected over the terrain at cliff edges.

All three default to enabled and can be changed during play. The ordinary **Rocks** option
controls the existing rock scatter separately. No launcher flag or environment variable is needed.
These controls disable client rendering; the server still prepares cliff geometry for streamed tiles.

## Suggested checks

- California coast, Cowell-Purisima Trail: `37.41159, -122.43018` and
  `37.41692, -122.43404`. Check continuous cliff coverage, nearly vertical faces,
  terrain transitions, natural color, and broken-rock grounding.
- Austin, South Capital of Texas Highway: `30.29941, -97.82788` and
  `30.30035, -97.82782`. Check terraced road cuts, low ends tapering toward road
  height, fitted bases, and cliff sections with a road above the crest.
- Austin, North Capital of Texas Highway: `30.30705, -97.82551`. Check the
  sustained roadside bank near the buildings and junction. A major road's urban
  classification must not suppress a cut backed by the original terrain;
  pavement and built-up crests must remain excluded. From `30.30565, -97.82529`,
  check for exposed wedges along the rounded foot and crest. Keep the simulation
  unpaused while waiting for newly streamed cliff meshes to appear.
- Drive away until tiles unload and return. Repeat after restarting. Coverage and
  materials should be consistent; there should be no duplicate surfaces or flicker.
- Approach a cliff slowly to check collision, then disable cliff faces and confirm
  the invisible mesh no longer blocks the vehicle. Roads and shoulders should remain clear.
- Check flat cities, small isolated elevation spikes, bridges, and stacked interchanges.
  A road elevation difference alone must not generate a rock wall. Road cuts require
  supporting terrain profiles; natural cliffs require sustained steep terrain and area.

Detection is heuristic. Please report missing cliffs as well as false positives, including
coordinates, screenshots, build/commit, and whether they persist after restarting.

## Performance comparison

Keep the camera, resolution, graphics settings, and traffic identical. Wait for terrain and
textures to finish streaming before each measurement. Compare all three cliff controls off,
then enable each one alone, then all three together. Record CPU/GPU frame times and FPS;
milliseconds are easier to compare across different baseline frame rates.

Decals have distance and count limits and are created over multiple frames. Broken rocks use
the forest system and asset LODs. Cliff collision is activated near the vehicle. These limits
reduce costs but are not a guarantee of a particular frame rate. Include hardware and graphics
settings with performance reports.

## Assets and development

Shipped scan-derived assets are CC0; the road-cut material uses an AI-restored source.
See [CREDITS.md](../CREDITS.md) for provenance. Small-rock collections and decal textures
are copied and tinted from the user's installed BeamNG assets at runtime, not redistributed.

The source checkout includes offline preparation instructions in
`tools/assets/cliff/SCANS.md` and `tools/assets/roadcut_generated/RESTORATION.md`.
These tools and source images are not required to run the packaged mod.

## Detection regression tests

Run the focused suite from the repository root (PowerShell):

```powershell
$env:BNGE_ROADGEO_REAL = '0'
python -m unittest server.tests.test_cliffcuts server.tests.test_cliffrocks server.tests.test_clifflod server.tests.test_cliffdecals server.tests.test_clifftalus server.tests.test_cdae server.tests.test_roadgeo
```

The deterministic fixtures cover coastal cliffs, terraces, upper roads, tapering
ends, fitted toes, near-vertical faces, and pavement clearance. The North Capital
scenario also tests the urban-road selection path, original terrain evidence,
nearby buildings, and rejection of artificial fill, missing source data, and
minor urban roads. Rounded-edge coverage is checked against exported triangles
along an oblique urban bank; bounded crest fitting must stop at terrace benches,
gentle hills, excluded land, and missing height samples. These tests run without
downloading terrain or launching the game; use the locations above for visual
checks. Add both a valid cliff case and
nearby false-positive cases when refining detection.

## El Paso divided mountain cut regression

At `31.87810, -106.49561`, Woodrow Bean Transmountain Drive / TX-375, check both
opposing faces and the divided highway below. Dense cliff exports previously
exceeded the runtime 16-bit vertex index range, producing giant sheets across
the road despite valid uint32 indices in the CDAE file. The loaded offending
shapes had 102,738 and 251,511 vertices. Re-exporting those exact triangles as
simultaneous objects of at most 65,535 vertices removed the sheets in-game on
2026-11-01. No cliff detection/road exclusion thresholds were changed.

The writer preserves all triangles, material assignments, normals, UVs and
vertex colors across chunk boundaries. Small shapes retain the previous byte
layout; splitting is enabled only by cliff exports, and chunks are not LODs.
Dense shapes add a few draw objects and duplicate boundary vertices.

Arid road cuts now select their own angular fractured 4K PBR family through the
existing climate biome. Austin retains its restored limestone. Both use the
same 300 ft world scale and regional tint. The arid relief is an approximation
from generated texture features, not measured depth. It adds four 4K maps and
a small CPU height resource. Server code and height changes invalidate revisit
geometry; restart the development server to use them, and reload materials or
restart the game for an existing session.

Tests cover tall opposing curved cuts, tile clipping, rotated geometry, both
projection charts and material groups; they require bounded local indices,
finite geometry, short edges and no triangle loss. Export tests also verify
object links and per-vertex attributes. Climate selection, arid pixel-center
sampling, periodicity, resource packaging and cache invalidation are covered.


## Cliff detail and camera LODs

**Graphics > Cliff detail** controls natural cliff faces and both road-cut
families together. Low and Medium simplify earlier; High and Extreme retain
fine geometry farther away. Presets Low / Medium / High / Ultra / Extreme map
to cliff Low / Medium / Normal / High / Extreme respectively. Existing saved
presets inherit the new setting; manual changes mark the preset Custom.
Scattered rocks retain their authored asset LODs and separate visibility switch.

Cliff shapes contain the original mesh and up to three progressively clustered
levels (1.5, 3 and 6 m cells). Open edges, holes, tile outlines, UV chart edges,
and material boundaries are pinned. Connected clustering rejects folded faces
and nonmanifold edges; small or difficult shapes can retain fewer levels.
The original finest mesh and collision surface remain intact. Every detail is
split within the 65,535-vertex limit; absent coarse objects use null meshes.

Normal thresholds are 1200 / 600 / 240 pixels with a final 2-pixel cull size.
The engine uses camera projected size, including FOV and its global Object detail
setting. Cliff detail scales only cliff thresholds (0.55 / 0.75 / 1 / 1.5 / 2
reach factors). The server writes thresholds into a private copy at a fresh,
unheld shape slot before the game loads it. Cached sources and already-loaded
shapes remain unchanged. Changes apply as cliffs load; reload the level to update
cliffs already in view. Updating from a build without cliff LODs requires a
server/game restart; the server code fingerprint invalidates old meshes.

Do not use TSShapeConstructor.setDetailLevelSize on streamed shapes. The first
implementation passed isolated native tests but crashed during streaming inside
that C method (2026-11-01, beamng.1.log). Lua pcall cannot catch a native crash.
The replacement sends cliffDetail with graphics settings and never edits a
loaded native shape.

Validation: a retained real cliff reduced from 40,090 triangles to 21,744,
16,142 and 14,730 (about 63% fewer at the coarsest level). The dense native-loader
fixture combines three copies, exercises split objects and null padding, and
loads separate quality-adjusted files in BeamNG. All 65 sampled native
collision rays matched the full-detail-only shape exactly. These are geometry
and collision checks, not an FPS benchmark; coarse outlines should still be
reviewed while driving and from a free camera in varied terrain.

Additional Lua regressions:

```powershell
luajit mod/tests/test_clifflod.lua
luajit mod/tests/test_graphics.lua
luajit mod/tests/test_settings.lua
luajit mod/tests/test_roads.lua
```


## Distant natural cliffs and canyon variant (2026-11-02)

Distant CDAE terrain (levels above zero, sampled at up to 128 m spacing) now
uses wall-projected rock on steep exposed triangles. `farcliffs.py` preserves
all positions, triangle winding, normals, holes and skirts. It duplicates
vertices only at material/projection boundaries. Triangle count is unchanged;
there are additional material draws and some vertex memory, so this is not
zero rendering cost. The coarsest horizon, DAE fallback and near terrain keep
the existing imagery path. Terrain detail and its presets control distant
geometry; Cliff detail continues to control generated nearby rock skins.
Nearby cliff faces toggles the additional skins; distant rock remains terrain.

Detection: face slope >= 48 degrees, vertical drop >= 8 m, positive XY area
(excludes skirts). Cached shrub, grass and bare-rock land cover qualify;
forest, crops, built-up land, snow, wetlands and water do not. Land cover is
read with `fetch=False`: no wider download workload. Unknown cover is allowed
only in the explicit Colorado Plateau appearance region. This keeps broad
Grand Canyon coverage independent of near-road prefetch without a worldwide
unknown-land fallback. Classification is an appearance heuristic, not geology.

`cliffmaterial.canyon_region` covers the Grand Canyon and similar Colorado
Plateau escarpments (including Zion/Canyonlands), excluding El Paso and Austin.
Near natural faces and distant terrain share the new canyon family. Road cuts
retain their existing limestone/arid choice. Canyon height and every material
map share a geographic chart (91.44 x 182.88 m). Source and generation prompt:
`tools/assets/canyon_generated`; bake via `tools/bake_canyon_material.py`.

Offline measurement: `python tools/preview_far_cliffs.py` uses the usual DEM
provider/cache and writes `build/grand-canyon-cliffs/coverage.json` and
`terrain.npz`. A 32.768 km square at 36.14 N, 112.12 W, sampled at 64 m:
56/64 tiles have cliffs; 67,075 of 425,288 triangles cover about 281.6 square km
of wall surface; zero triangles added. Material classification/assembly median
was about 4.1 ms per tile on the development PC before satellite-color sampling
was added (not an in-game FPS benchmark).

`tools/render_far_cliffs.py` renders an optional Blender preview from those
outputs. It uses illustrative ground colours, not aerial imagery or BeamNG's
lighting; it exposes the coarse face-selection boundaries. It is not visual
acceptance for in-game material matching. Initial implementation was done with
the game closed; subsequent texture/color fixes were checked in the running
game from the user-controlled free camera. Coarse face-mask boundaries remain
visible and need a separate transition/blending pass. No native shape edits
were used.

Regressions: `test_farcliffs.py`, `test_farfield.py`, `test_surface.py`, existing
cliff suites, and Lua `test_farfield.lua` / `test_farfield_spares.lua`.

Texture import regression (2026-11-02): BeamNG rejected the original 1254-square
runtime canyon maps. Bake v3 outputs 1024-square base/normal/roughness maps;
source remains 1254. Tests now enforce TextureCooker power-of-two dimensions.

### Satellite color and distant baked shadows

Canyon near meshes use the same cached regional imagery sampler as the original
natural cliffs. Their new tint blends 45% mineral hue with normalized exposure,
rejecting blue/green/invalid samples; local satellite shadows remain excluded.
Distant cliff vertices sample the tile's existing satellite texture after a
48 m Gaussian blur and retain 90% of its broad color/luminance. This includes
photo shadows and needs no additional texture downloads, GPU textures or
triangles. Both limestone-style and canyon distant materials use this path.

BeamNG decodes vertex colors and material factors from sRGB. Linear tint gains
must be encoded before writing RGBA8; the 4x linear material headroom must also
be encoded, including values above 1. Regression tests decode both and verify
that their product preserves the intended albedo. Separate `_sat` names keep
old in-flight shapes compatible with legacy white vertex colors.

Terrain photos interpolate from normal near de-shadowing at L0, through 1/3
and 2/3 original shading at L1/L2, to full original shading at L3+. A shared
Imagery render view reuses raw fetch/cache/pools, while isolating finished
near/far caches and splitting their memory budget. Color matching and seasonal
processing remain active; interpolation occurs in linear light. Cache keys
include the shadow blend. With de-shadowing disabled, the original photo path
is unchanged.

Limitation: asynchronous color-match updates can replace a terrain texture
while its cliff vertex colors still reflect the build-time photo. They catch
up on tile regeneration; no loaded native mesh is mutated. The 48 m blur and
coarse vertex interpolation intentionally discard fine aerial detail.

Validation: 67 Python far-cliff/terrain/shadow tests, the existing 127-test cliff
harness (2 optional-data skips), and 176 Lua farfield checks. Tests cover color
variation, shadow retention/exclusion, UV orientation, blur, PBR color-space
round trips, unchanged triangles/collision and isolated imagery cache results.
Live reference: `build/grand-canyon-cliffs/in-game-satellite-color.jpg`.

### Warm bands and macro normals (live refinement)

Full RGB white balancing made the less-red pale beds green. Canyon tint now
transfers luminance separately, with 25% chromaticity correction far / 30% near;
far rock uses a 0.85 exposure gain to reduce washout. Satellite shadow variation
is retained. The base-color texture itself is unchanged.

Distant rock now has a shared normal map. Canyon `canyon_far_v3.normal.png`
combines periodic vertical erosion and source-aligned broad ledges, with quiet
fine fractures. Macro strength was reduced 20% after the user's live review.
The 5-30 m structures survive low-resolution mips; nearby displacement/normals
are unchanged. Other distant rock uses the existing scan flat normal map.
Cost: one shared normal-map sample per distant rock pixel, no extra geometry.
18 focused color/shadow/geometry/material regressions pass, including warm pale
bands under neutral satellite tint, unit normals and macro relief at a 32px mip.

### Larger normals on the farthest cliffs

The existing far normal maps repeat at 2x physical scale on tiles >=3072 m wide
and 4x on tiles >=6144 m. These LOD proxies cover the nominal 4/8 km rings despite
latitude-dependent Mercator footprints. Actual view distance follows terrain
selection; this is not a fixed camera-distance cutoff. The coarsest horizon
that does not qualify as cliff still keeps its existing photo-only appearance.

Normals use UV1 independently of the unchanged UV0 albedo chart. Scaling the
absolute geographic chart before wrapping the tile origin preserves neighboring
phase; terrain vertices retain their existing detail UVs. No new maps, triangles,
materials or draw calls. Existing LOD crossovers handle changes between scales.
70 Python checks pass across far-cliff, shadow, farfield and global-tile suites;
new coverage checks scale thresholds, unchanged albedo/geometry, adjacent tile
normal phase and finite exported UVs/tangents.

## Distant cliff faces switch (2026-11-02)

Graphics now has `distantCliffFaces`, default on. Low preset disables it;
Medium, High, Ultra and Extreme enable it. A manual override selects Custom.
The choice is saved and sent on connect and change in the graphics message.
Nearby cliff faces, road cuts, scattered rocks and decals stay independent.

Applies to newly loaded tiles; reload the level for a consistent change across
cliffs already in view. No native shape edits or synchronous world rebuilds.
Far export retains a photo-only CDAE only where cliff triangles exist. The off
variant preserves the original terrain triangles, UVs, normals and colors.
Connections selecting Off receive a fresh pooled shape/material slot; the
shared authored shape remains untouched. Normal slotsGone tracking releases
it, photo retexture updates still target each instance's material, and cached
plain files are pruned with their terrain tile.

Validation: 50 focused Python tests (export, graphics validation, pooled-slot
isolation/lifetime, protocol agreement); Lua cliff settings30, settings320,
core307 checks. The broader protocol suite also completed with one optional
skip; its combined run's test_graphics import-path error was rerun successfully
with PYTHONPATH=server;server/tests.


## Canyon terrain fit and normal width (2026-11-03)

Nearby canyon skins now start from the terrain envelope, with 0.20 m clearance.
Their albedo/relief chart follows that same base to avoid stretching after fitting.
The original natural scan contributes 60% of its relief alongside canyon beds.
Conforming subdivision resolves intermediate height samples on tall faces, with
shared edges and terrain-checked residual displacement. LODs derive from the
original mesh and reject terrain penetration rather than compounding inward drift.

The user identified the remaining dense vertical streaks as the normal map.
Canyon normal repeats are now 3x wider horizontally on an independent UV1 chart;
vertical scale, albedo, tint, normal strength and geometry remain unchanged by
this adjustment. Near refinement and all LOD exports preserve UV1/tangents. Far
normals retain their additional 1x/2x/4x ring scale and geographic phase.

Validation: 85 cliff/far-cliff/detail/LOD tests passed, plus the new near normal
chart regression covering unchanged geometry/color, refinement, LODs and export.
Earlier terrain-fit validation: El Paso harness 130 passed, 2 optional skips;
15 km rebase left no built tiles behind. Live visual tuning still requires user
review at their chosen lighting; no camera or lighting changes are made.


### Near tall-wall correction and diffuse-derived PBR (2026-11-03)

Supersedes the near normal-width adjustment above. Near normals now match the
approved diffuse UV0. The dedicated `bake_canyon_near_pbr.py` produces 4096-square
normal, roughness and subtle cavity AO maps from that exact runtime diffuse;
metallicity remains zero. These are inferred channels, not measured PBR. Broad
lighting/pigment changes are filtered before differentiating, and cavity AO is
bounded to avoid baking strong shadows. Albedo and exposure are unchanged.

A 200 m sigmoid wall reproduced 193 folded horizontal edges in the old initial
normal displacement. Canyon backing now retains XY ordering, refines long wall
edges first and only then samples the original scan/canyon relief. Displacements
are terrain-checked and shortened at local orientation flips. Pure canyon tiles
use 1 m rather than 0.5 m footprint sampling; vertical splits target 2 m. Road
cuts retain their existing sampling/fit. A regression covers a cut in the halo
without letting it switch all natural canyon vertices back to the old path.

Mid-distance canyon normal width remains 3x. The >=6144 m tile tier uses 1.5x
horizontal width instead of 3x, keeping its existing vertical repeat. This is a
streaming tile tier, not an exact camera-distance boundary.

Live inspection after the first reload found near-mesh 99th-percentile vertical
edges around 2 m. Temporary overlay hiding confirmed broad grooves also exist in
the underlying terrain; all overlays were restored, with camera/time untouched.
The mixed-halo correction was identified and tested after this inspection.

Validation: 92 focused geometry/PBR tests passed; El Paso harness ran 132 tests
with 2 optional-data skips and passed its 15 km rebase checks. BeamNG cooked
all three new near maps successfully. Local runtime updated; no release made.


### Bounded cliff streaming (2026-11-03)

All near cliff families (natural limestone, arid, canyon, and fitted road cuts)
use the same spatial batching kernel. It partitions existing triangles rather
than simplifying or regenerating the surface. Pieces copy positions, vertex
normals, both UV charts, tint and material indices exactly. LOD generation locks
the new shared boundaries; projected LOD thresholds scale with piece radius.
The target is 16,384 triangles per piece, capped at 64 pieces per tile (extreme
tiles can exceed the triangle target to retain this protocol bound). Small and
legacy single-shape tiles remain supported. Far terrain materials are unchanged.

The mod imports at most one cliff piece per frame under a separate measured
`cliffMesh` budget. Range-driven collision changes use `cliffContact`; overdue
collision rebuilds get a frame between cliff imports instead of piling onto
one import or starving behind a long queue. Near collision still follows the
visible mesh. LODs must remove at least 25% of the previous level's triangles;
near-duplicates no longer inflate shape files. All cliff shaders are prewarmed
in their UV2/colour vertex format even with road dirt strips disabled.

A captured 348,652-triangle canyon tile went from 48.82 MB to 32 pieces totalling
26.51 MB, largest 0.984 MB, preserving every finest-level face. Six fresh hidden
TSStatic imports in the running game took 8.49–9.72 ms each; an equally fresh
whole-shape control took 56.19 ms. These isolate synchronous shape registration,
not whole-frame FPS or a guarantee against other engine/physics stalls. Every
probe object was deleted immediately and the camera/lighting were untouched.
The worktree's `build/cliff-stream-benchmark/` contains the captured results.

Regression coverage includes all material families, exact face/attribute
preservation, deterministic bounded partitions, piece publishing, LOD boundaries,
visibility, collision deferral, overdue rebuild fairness, drop cleanup, geotile
rebases, and exact/base variant swaps. Native version is 144. Restart the local
server and reload the mod together to apply the new multi-piece stream; an old
client understands only the legacy first piece. The source hash automatically
invalidates prior generated content, so no manual cache deletion is needed.

Validation: 188 focused Python tests (2 optional-data skips), all 85 Lua test
modules, and the Rust partition regression passed. Windows native v144 was
built and the two changed Lua modules were staged in the local unpacked mod,
with backups under `build/cliff-stream-install-backup`. The running server/VM
were left intact. Linux native rebuild remains pending: the referenced
`.claude/devtools/build_native_all.sh` is absent here and WSL has no Rust/C
toolchain. Rebuild the Linux binary before packaging a Linux release.

## Opaque terrain transitions (2026-11-04)

All four near cliff families now use inward-only corner rounding and a narrow
satellite-colored opaque rim. No added decals or per-cliff texture objects.
UV0 retains the original PBR detail; UV1 carries the edge mask. Distant materials
are unchanged. See [research, implementation and measurements](CLIFF_TRANSITIONS.md).
Native version 145 is required. Restart the server and reload the mod together;
existing content invalidates by source/native hash. Linux binary rebuild pending.

### Material activation regression

The initial transition materials omitted `activeLayers: 2`, so BeamNG rendered
only their rock layer. The follow-up fix enables the second layer in all four
variants and asserts that property in `test_clifftransition`. Same-camera live
captures on 2026-11-04 at 01:27 show the blend activating. Earlier import/CPU
timings measured the inactive layer and are not evidence for its rendering cost.
Some stepped silhouettes and smooth collars remain; do not call all seams fixed.

### Continuous contours and tapered thickness

All four near cliff families now cut a filtered coverage contour through their
boundary triangles instead of retaining the replicated detection-cell outline.
The geometry tapers to sunken sampled ground along the same band as the material.
Later canyon heightmap relief uses the same taper, preventing a thick rebuilt rim.
Streaming splits are proportional to the triangle budget, avoiding a doubling of
pieces when boundary intersections add a few triangles. Rebuild/reload the server
and regenerate the location to test native v146; material reload alone is insufficient.


## Cliff-base Forest rubble (2026-11-06)

The natural cliff mask now seeds rubble only at downhill exits. The candidate
surface must be below the toe and below 44 degrees (full density through 28).
A deterministic, jittered 2 m lattice creates clusters with at most 5 m lateral
scatter and a thinning 22 m downslope runout. Density and size decline with
travel distance. Uphill crests and cliff walls do not qualify. Road, water,
unknown-cover and city exclusions still apply; adjacent tiles own instances once.
The same algorithm serves canyon and classic natural cliffs and is skipped when
cliff rock generation is disabled.

Three Johnson Valley collections and Utah boulders 1, 3 and 5 are copied into the
user's temp asset directory with private materials and their authored LODs. Utah
boulders were removed from the generic desert scatter so they only appear in
these base aprons. Normal, roughness, AO and detail maps are copied from the
installed game; no game-owned files are modified or redistributed.

Rubble RGB now represents the source cliff's final mean albedo: its regional
vertex tint multiplied by the measured texture/material mean in linear light.
Classic and canyon use their respective tint pipelines, sampled at the source
cliff rather than beneath the fallen rock. A 16-level RGB step limits material
variants. Private materials compensate the UV/surface-area-weighted linear mean
of each substrate (desert .18946/.18067/.17330; Utah .24181/.11078/.08191).
The coarse collection LOD uses the same target mean instead of stock vertex color.

Validation: 92 Python cliff tests, 15 Lua talus checks and 141 Forest checks pass.
The 1 km synthetic wall benchmark, three warm runs, changed from 20.6-21.7 ms /
2,232 instances to 16.7-16.9 ms / 1,029 instances. This measures placement only,
not total streaming or render time. Screenshots in local `build/cliff-talus/`
show the same live canyon view before/after and a close inspection: the old bright
crest clusters are removed and tinted boulders/collections now occupy lower
benches and spread downhill. This is a visual sample, not an exhaustive seating
or color match across every biome.
