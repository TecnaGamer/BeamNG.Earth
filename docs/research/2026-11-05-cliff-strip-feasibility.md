# Cliff intersection strips: engine feasibility (2026-11-05)

Optimization baseline committed as `5e619d68` before this experiment. This study does not replace the production decal system. No engine shader was edited.

## Result

**Material feasibility confirmed in the running BeamNG PBR renderer.** A mesh can retain the classic or canyon cliff material and vertex RGB tint while its coverage becomes sparse toward its sides. The working path uses alpha testing, not translucent blending. Literal vertex alpha is not supported by the current PBR opacity path; storing the per-vertex edge threshold in UV1 is a working alternative.

This is a material/mesh proof, not a completed terrain-intersection implementation or a performance benchmark. Accurate contact, terrain LOD behavior, mip stability, and cold-generation/frame-time cost still need production validation.

## Live experiments and evidence

Four temporary six-meter panels were tested in the same scene:

1. Vertex-alpha gradient, height opacity map: panel remained opaque at its sides. Vertex alpha did not control PBR opacity.
2. UV1 edge threshold, synthetic height: dense center with progressively perforated sides.
3. The same threshold method with the canyon material, its PBR/detail maps, height, and RGB tint: working clipped edges.
4. The same method with the classic material: working clipped edges.

The corrected DDS-based panel test was repeated successfully. Evidence: `build/cliff-strip-probe/panels.png` (original MCP screenshot `shot_20261105_195322_102.png`). The synthetic control isolates the coverage operation from lighting and rock textures. Fine slivers at extreme UV boundaries and small center pinholes remain in this diagnostic asset; mask quantization, endpoint sampling, and mip handling need tightening before shipping.

Two curved strips were also generated from terrain height/normal samples, each 6 by 18 meters in XY, with 576 triangles. One half retained full coverage as a color control, and the other used the edge threshold. Actual terrain placement was obscured by existing cliff geometry; the sampled shapes were raised 200 meters for isolated inspection. Both retained their PBR appearance across opaque and eroded portions. Evidence: `build/cliff-strip-probe/curved-strips.png` (original `shot_20261105_195114_101.png`). **This does not validate contact with the rendered terrain or intersection alignment.**

Temporary objects were deleted and the original free-camera position, rotation, and FOV restored. Probe materials and assets remain only under the game's temporary directory; no scene objects were saved. Local reproduction scripts and exact settings are in `build/cliff-strip-probe/probe.py`, `ground.py`, and `materials.json`.

## Why it works

The installed `shaders/common/material/shadergen/defaultMat.hlsl` uses vertex RGB for base color, but does not multiply PBR opacity by mesh vertex alpha. Its opacity-detail operation is:

```
opacity = opacityFactor * opacityMap * saturate(1 + (2 * opacityDetailMap - 1) * detailOpacityMapStrength)
```

With strength 1, encode a normalized rock height `h` as `H = (1 + h) / 2`. Encode a one-dimensional lookup for edge threshold `t` as `D(t) = 0.5 / (1 + 1.03*t)`. Then:

```
opacity = (1 + h) / (2 * (1 + 1.03*t))
alpha test near 0.5 keeps approximately h >= 1.03*t
```

`t=0` at the center keeps almost all rock; `t=1` at the sides rejects the rock. This changes binary coverage rather than fading the surviving texels. The extra 3% makes the outer limit reject even the tallest texels. Eight-bit quantization and alphaRef=128 mean this is approximate; a production encoder should reserve a margin at the fully dense endpoint.

Working material fields:

```
version: 1.5
alphaTest: true
alphaRef: 128
translucent: false
Stages[0].opacityFactor: 1
Stages[0].opacityMapUseUV: 0
Stages[0].opacityDetailMapUseUV: 1
Stages[0].detailOpacityMapStrength: 1
Stages[0].vertColor: true
```

Explicit linear RGBA DDS masks worked. Initial grayscale PNG masks did not yield the intended coverage. The exact import/format cause was not isolated, so use the proven linear format rather than assuming arbitrary PNG masks are equivalent. The diagnostic DDS files have one mip level; they are not shipping assets.

The opacity-detail lookup shares `detailScale` with the rock's detail maps. Divide UV1 coordinates by that scale so the threshold is sampled once across the strip without changing rock detail density. Keep lookup coordinates inside texel centers to avoid repeat wrapping. The CDAE writer flips V on both UV sets; compensate when supplying the lookup's constant V coordinate.

The official [BeamNG PBR material reference](https://documentation.beamng.com/modding/materials/materials_1.5/) documents the two UV sets, opacity/detail maps, vertex color and alpha clipping. The specific vertex-alpha limitation and lookup equation above were verified against the installed shader and live tests.

## Matching classic and canyon colors

Use a material variant of the adjoining cliff family, preserving its base-color factor, diffuse texture, normal, roughness, AO, and detail settings. Reuse/interpolate the **actual cliff vertex RGB at the intersection**, rather than independently estimating a nearby satellite tint. The coverage lookup occupies UV1 and leaves vertex RGB available for this purpose.

Also preserve the cliff UV phase and texture scale at contact. Identical material inputs avoid a new albedo discontinuity; differing surface normals, tangent orientation, lighting, and baked satellite shadows can still cause visible differences. The probe used representative constant tints, not colors extracted from an actual contact edge, so exact scene color matching remains an integration test.

The existing two-layer terrain-transition material already uses UV1 for satellite mapping. It cannot be copied unchanged while also assigning UV1 to the threshold. Start with the single-layer classic/canyon cliff material variant; do not silently break the existing second-layer UVs.

## Recommended implementation constraints

- Derive the centerline from the actual rock/terrain intersection or the same continuous boundary used by the rock mesh, not the old coarse qualifying-cell outline.
- Sample the exact terrain tile/surface used for rendering, with matching triangle interpolation. A generic first-TerrainBlock height query is insufficient validation in a streamed scene. Keep a small, measured depth offset and handle terrain LOD changes.
- Batch strips by tile and cliff family. Disable collision/decal reception. Build them within the existing generation budgets, not as hundreds of individually spawned decals or meshes.
- Use bounded adaptive longitudinal spacing and a few cross-strip rows. The proof's dense grid is diagnostic, not a production tessellation requirement.
- Use a height mask aligned to the rock texture or a separately authored rubble texture that shares the cliff palette. The proof uses the existing reduced CPU displacement heights; it does not establish final edge aesthetics.
- Generate/test alpha-coverage-aware mipmaps and inspect motion, grazing views, shadows, and far LODs. Alpha testing avoids sorted transparency, but still costs texture reads, rasterization, and potentially depth/shadow work.
- The lookup adds two mask samples to the base cliff material. This test establishes compatibility, **not a measured performance win**. Validate with the established repeatable camera path and uncached generation before replacing production decals.

The next implementation can proceed using stock PBR materials and UV1 threshold control; a custom shader is not required for the demonstrated effect.


## First surface-placement prototype

Implemented in `server/cliffstrips.py` and `tools/preview_cliff_strips.py`.
This is an explicitly invoked live preview, not the production streaming path.
The existing decal implementation is retained and disabled for the preview;
`--cleanup` removes the temporary TSStatics and restores its previous setting.

- The centerline follows interpolated zero-clearance contours between the loaded
  cliff triangles and the rendered ground. Near ground is the actual TerrainBlock,
  sampled on its native lattice; center samples recover each cell's diagonal.
  Visible TSStatic terrain is also accepted as a receiver.
- Strip vertices project orthographically onto the frontmost local terrain or
  rock triangle. Missing samples and edges longer than 1.5 m are rejected to
  avoid bridging unrelated ledges. A 2.5 cm normal offset reduces z-fighting.
- A fixed planar UV frame keeps the texture straight around bends. UV1 carries
  the side/end erosion threshold. Both endpoints reach the fully clipped value;
  the dense interior survives the actual 8-bit alpha reference of 128.
- The placeholder is stock `t_dirt_rocky`, extracted into the game's private temp
  directory with its height, normal, roughness and AO maps. No stock texture is
  added to the repository. Height controls coverage, not vertex displacement.
- Strip vertex colors interpolate nearby cliff colors after accounting for the
  existing satellite rim layer. The rubble material is normalized by its linear
  mean reflectance. This avoids applying the canyon's red base-color bias twice.
  The same path recognizes classic, roadcut, arid and canyon materials.

Live Grand Canyon test: eight contact runs, one TSStatic/material batch, 7,538
vertices and 13,390 triangles. Each run hit both terrain and rock receivers.
The last full offline build/installation invocation took about 1.49 seconds;
that is NOT a frame-time measurement or a suitable synchronous streaming budget.
There is no recurring update/generation callback in this preview.

Validation: five unit tests pass, covering four-edge coverage including encoded
8-bit alpha, frontmost receiver selection/misses, planar UVs on a curved strip,
two receiver surfaces, triangle winding, and degenerate centerlines.
Inspected live screenshots from two outward-facing viewpoints:
- `screenshots/mcp/shot_20261105_205321_105.png`
- `screenshots/mcp/shot_20261105_205734_106.png`
Both are relative to the BeamNG user directory. The second view is left active.

Remaining prototype limits: sharp folds can still occlude portions of the strip;
projection should eventually clip against receiver triangle boundaries rather
than only sampling vertices. The contact is piecewise linear, not an exact
triangle/triangle intersection. Strongly curving runs can overlap themselves.
Height-mask mip/coverage behavior, LOD changes, streaming lifetime, and cold-load
frame timings still need validation before production deployment. The current
rubble reads as distinct dark patches in places; this is a texture/color tuning
starting point, not a claim that the terrain seam is solved.

Run from the feature worktree with the project Python environment:

```text
python tools/preview_cliff_strips.py
python tools/preview_cliff_strips.py --focus 11672.95312 -6446.201172 2007.028809
python tools/preview_cliff_strips.py --cleanup
python -m unittest discover -s server/tests -p test_cliffstrips.py
```

The first command uses the camera's surface hit as its focus. `--width` controls
full width in metres (default 4); `--max-strips` limits the preview (default 8).


## Receiver-clipped prototype follow-up

The current implementation supersedes the vertex-only projection described above.
Each coverage-mask cell is clipped to the receiving triangles. Overlapping
receiver regions are split along their depth-plane intersection and the rear
fragments are removed. This keeps the strip on each side of a sharp terrain/rock
fold instead of interpolating a triangle through the fold. Genuine receiver gaps
remain empty. A common 2.5 cm projection-axis offset avoids separating adjacent
faces by independently offsetting them along their normals.

The mask grid is now 0.8 m; actual surface tessellation comes from receivers.
Spatial-bin candidates are filtered by projected bounds, disjoint occluders are
skipped, and identical fragment vertices are welded. A 12,000-triangle per-strip
limit bounds pathological output. This is still an offline Python reference
implementation, not a streaming-budget implementation.

Rock-side tint is interpolated from the exact receiving cliff face, including
its existing satellite-rim weight. Rock-side shading normals come from the
source mesh; TerrainBlock shading normals are approximated from native-grid
height gradients. Rubble AO is reduced to 0.35 to avoid excessive darkening.
The ground side still uses nearby cliff tint, not a direct sample of the terrain
material. Texture variation, lighting, and baked satellite shadows therefore
remain possible sources of visible color mismatch.

Eight contact runs now total **22,198 vertices / 18,959 triangles**, in one
non-colliding TSStatic batch. The full offline snapshot/build/install invocation
took **13.36 s** (initial unfiltered clipping took 17.87 s). This increase over
the older sampled grid buys exact fold conformity; it is explicitly unsuitable
for synchronous streaming. Production adoption needs an asynchronous/native
build path and bounded installation work, followed by cold-generation testing.
No per-frame strip generation was added.

Eight tests pass. New regression cases check crossing receiver planes for
complete projected area and no buried faces, genuine gaps for no bridging, and
interpolated receiver shading normals. Earlier checks retain all-four-edge
height clipping, curved-strip planar UVs, winding, and degenerate input handling.

Live screenshots inspected in the BeamNG user directory:
- `screenshots/mcp/shot_20261105_212251_107.png`: initial exact-fold projection.
- `screenshots/mcp/shot_20261105_213003_108.png`: final tint/normals, visible strips.
- `screenshots/mcp/shot_20261105_213327_109.png`: matched camera, strips hidden.

The comparison isolates the added rubble patches from existing terrain skirts
and cracks. The preview wraps onto terrain and rock and erodes at its borders,
but the stock rubble still reads as a separate darker patch in places. The
terrain/cliff seam is not claimed solved. All eight strips remain visible for
review; profiler hooks and camera movement were restored after measurement.

### Bounded rendering measurement

Four 12-second passes used the same camera path, 120-frame settling period and
60 FPS cap. Order was visible / hidden / hidden / visible. Camera displacement
was `(3, 2, 1) * sin(pi*t)` metres with a 0.15-radian yaw oscillation. This is a
loaded-scene rendering comparison, **not uncached terrain or strip generation**.
The capture contained no road generation units. Evidence and the exact harness
are under `build/projected-cliff-strips/timing-*.json` and `benchmark.lua`.

| Pass | Frames | Median ms | P95 ms | P99 ms | Max ms | Frames >25 ms |
|---|---:|---:|---:|---:|---:|---:|
| Visible 1 | 720 | 16.667 | 16.912 | 17.489 | 26.215 | 1 |
| Hidden 1 | 720 | 16.660 | 16.877 | 17.528 | 24.507 | 0 |
| Hidden 2 | 721 | 16.663 | 16.850 | 17.444 | 17.735 | 0 |
| Visible 2 | 720 | 16.663 | 16.880 | 17.450 | 26.798 | 1 |

Steady capped frame times were similar. Both visible passes had an isolated
26-27 ms frame, at different points on the path; the short sample cannot isolate
the cause or establish negligible strip cost. It also cannot establish GPU
headroom under the frame cap. Larger coverage, installation spikes, mip/alpha
stability, receiver LOD changes and cold streaming remain production gates.


## Production integration and live verification (2026-11-05, final)

This section supersedes the prototype-only status above. Contact strips now
arrive through normal tile streaming; no preview command is required. The old
cliff DecalRoad implementation was removed. Ordinary road decals are unaffected.

### Generation and lifecycle

The shared classic/canyon cliff builder extracts zero-clearance contact runs and
caches bounded receiving-mesh patches alongside geotile content. The active
anchor's skin job maps those patches into world coordinates and projects them
onto the cliff and the actual quantized one-meter TerrainBlock lattice. Both
height queries and receiver triangles use the engine's alternating diagonals.
Anchor changes rebuild these small strips without regenerating the cliff mesh.

The earlier two-meter tile-local ground approximation buried 305 of 545 sampled
vertices. The corrected live sample had 3 of 673 vertices below ground, at most
1.32 cm, with median clearance about 3.27 cm. This resolved the widespread
missing ground-side fragments; it is not a guarantee of zero intersection at
every cliff/terrain fold.

Native earthfast version 149 performs receiver clipping and depth occlusion,
releasing the GIL. Each tile allows at most eight strips and 16,000 output
triangles, with at most 32 candidate runs and 16,000 cached receiver faces.
Runs are limited to 24 m. Planar UVs remain straight across curved paths;
height-driven alpha clipping erodes both sides and both ends. Bundled stock
`t_dirt_rocky` maps provide the initial rubble appearance. Vertex color comes
from receiving cliff colors (including satellite-rim tint), with nearby cliff
colors used on the terrain side. The heightmap controls fragment coverage;
individual gravel stones are not geometrically displaced.

The UI calls this **Cliff transition strips**, retaining the saved/protocol key
`cliffDecals` for compatibility. Disabling it hides live strips immediately and
skips new server strip preparation/projection. Existing tiles that were generated
with strips disabled require reload after reenabling. Strips are non-colliding,
share the cliff import budget (at most one cliff or strip import per frame), and
participate in tile unload, replacement, anchor rebasing, and visibility changes.
An optional strip-generation failure preserves the parent cliff and road skin.

### Live evidence

Normal streaming produced 194 visible strip objects in the inspected scene.
The camera was placed at a real streamed strip near 35.99973, -112.02376, not an
experimental preview. Matched screenshots were visually inspected:

- `build/projected-cliff-strips/final-on.png`: rubble crosses the cliff foot and
  ground with sparse side/end fragments.
- `build/projected-cliff-strips/final-off.png`: same view, actual setting disabled.
- `build/projected-cliff-strips/terrain-grid-clearance.json`: sampled live terrain
  clearance after the lattice fix.

The setting was left enabled and temporary profiling hooks removed. The camera
was returned to the comparison view, overlooking a visible strip.

### Performance and validation

Final loaded-scene passes used the same 12-second movement, 120-frame settling
period and 60 FPS cap as the earlier harness:

| Strips | Frames | Median ms | P95 ms | P99 ms | Max ms | Frames >25 ms |
|---|---:|---:|---:|---:|---:|---:|
| Off | 721 | 16.675 | 17.043 | 17.537 | 18.337 | 0 |
| On | 720 | 16.669 | 16.952 | 17.497 | 17.809 | 0 |

Raw capture: `build/projected-cliff-strips/final-frametimes.json`. No material
steady frame-time regression was observable under the cap; this does not measure
GPU headroom. Final server logs show roughly 0.9-1.3 seconds of strip work per
sampled populated tile, executed on server workers. A preceding full reconnect
capture still had major terrain/collision stalls (P99 44.15 ms, worst 587.25 ms);
six strip imports in that capture individually took 2.48-3.13 ms. The feature is
functional, not a claim that overall world streaming is stutter-free.

Validation: 56 focused Python tests passed (one skipped) before the final terrain
regressions; the final strip/roadgeo/blockworker/prewarm selection passed 31 tests
(two external-data cases skipped). Relevant Lua suites passed 898 checks,
including strip visibility, collision exclusion, skin replacement and settings.
External-data roadgeo cases require dependencies unavailable in the standalone
Python test interpreter; they were skipped with `BNGE_ROADGEO_REAL=0`, not counted
as passing. `git diff --check` is clean.

Remaining visual limits: coverage is budgeted rather than continuous along every
cliff, short run endpoints can leave gaps, and the stock rubble can still read
as a separate darker patch against satellite imagery with baked shadows. Those
are texture/coverage tuning opportunities; normal production installation and
terrain projection now work.


## Wider coverage and rock-only tint revision (2026-11-05)

Supersedes the initial 3 m wide / 16k-triangle strip limits above. Contact runs
now span up to 48 m, are selected farthest-first, and use an 8 m projected width.
Multiple runs are batched into at most eight imports per tile, with 8k triangles
per batch and 64k total. Receiver margins increase to 8 m; projection uses the
quantized terrain lattice and clamps final lifted vertices above the terrain.
The clamp prevents buried vertices; it is not proof that every triangle remains
above all neighboring cells or every cliff LOD. Coverage remains budget limited.

The real captured canyon fixture projects 30 runs / 979 m of contact into eight
batches (63,490 triangles), versus the old eight 24 m runs / 192 m ceiling.
Prepare measured 0.729 s and projection 0.963 s. Voxel-averaged ground-side tint
lookup avoids the earlier 14 s all-vertices nearest-color scan. Expanded coverage
has a higher triangle budget, not a zero-cost promise. Runtime visibility fades
at 450/150 m; the strip material is included in NO_SHADOW and reloaded with
castShadows=0 (verified live). Normal shading and received shadows remain.

Latest user correction: use only the cliff rock color, not a satellite color
transition across the strip. Preserve original pre-rim vertex tint as an optional
build-only attribute through contour clipping, tile clipping and canyon refinement.
It is collected only when strips are enabled, never exported as an extra shader
stream. Both halves inherit that rock albedo; coverage erosion alone reveals the
underlying terrain at the sides and ends. Decode sRGB vertex tint before linear
material multiplication, then encode the final strip vertex colors for BeamNG.
No new texture fetches, shading stages, or draw calls for this color correction.

Validation: 96 focused Python tests pass, including classic/canyon strips with
artificially dark or green satellite rims and tint preservation through refinement.
Live capture: build/projected-cliff-strips/rock-color-on.png. Same loaded scene,
12-second camera motion at 60 FPS cap: disabled/enabled P99 17.500/17.511 ms,
max 17.752/17.646 ms; 720 frames each, zero frames above 25 ms. This is a loaded
scene comparison, not an uncached world-generation test. Reconnection still has
unrelated import/collision/terrain generation stalls. Camera/profiler restored and
strips enabled. Server PID is in build/cliff-cold-study/active-server.json; current
log is build/projected-cliff-strips/rock-color-server.log. Changes uncommitted.


## Strip coverage budget and occlusion fix (2026-11-06)

The 64k tile budget was dropping most of the retained contact network on dense
cliff tiles. Raise the shared classic/canyon/roadcut limit to 24 parts / 192k
triangles per tile, retaining the 8k per-import cap and one cliff/strip import per
frame. Use a 4 m mask grid to contain the total tessellation cost. Always include
the cross-strip centerline and at least one interior along-strip row; otherwise
short strips or odd cross-section counts could erode the entire visible center.

Fix duplicate consecutive polygon vertices in both native and reference receiver
occlusion: zero-length clip edges must be skipped, or subtraction can retain a
whole hidden polygon. The crossed-receiver regression now rejects buried pieces.
Native API/minimum version is 150; Windows release earthfast.pyd rebuilt. The
Linux native binary was not rebuilt in this Windows session.

Captured canyon fixture: all 77 retained contact runs / 2,605 m project into 20
parts / 152,236 triangles (previously 30 runs / 979 m / 63,490 triangles).
Prepare: 0.710 s; projection: 2.033 s. This increases total generation and geometry
cost to restore coverage; it does not make generation free. Short runs below
4 m, receiver-face limits, 128 attempts and the tile triangle budget still apply.

Live game reconnected to updated server and deployed 24-part Lua consumer.
Screenshots under build/projected-cliff-strips: coverage-before.png,
coverage-after.png and coverage-angle.png. Rubble now extends across several
previously bare lower contacts. Some contacts remain visibly bare; the captures
are evidence of expanded coverage, not universal coverage or perfect blending.
Rock-only tint, side/end erosion, 8 m width, shadow casting disabled and distance
fading are preserved.

Loaded scene, matching 12 s camera travel at 60 FPS cap:
- Enabled: 719 frames, median 16.667 ms, P95 16.892, P99 17.507, max 28.602;
  two frames above 25 ms.
- Disabled: 720 frames, median 16.666 ms, P95 16.911, P99 17.495, max 28.474;
  one frame above 25 ms.
Raw data: build/projected-cliff-strips/coverage-frametimes.json. This is not an
uncached streaming benchmark or a GPU-headroom measurement. Sampled server tiles
spent roughly 1-3.1 s on strips during the reconnect.

Validation: focused Python suites 36 passed / 2 skipped; broader cliff suites
84 passed; Lua roads 148 passed and roadgeo 49 passed. Camera/profiler restored,
strips enabled. Active server is recorded in build/cliff-cold-study/active-server.json;
log: build/projected-cliff-strips/full-coverage-server.log. Changes uncommitted.


## Screen-space shadow exclusion (2026-11-06)

The user correctly identified the remaining dark strip outlines as screen-space
shadows. `castShadows=false` already disabled shadow-map casting, but the installed
engine's screenSpaceShadows.cs.hlsl traces scene depth without a material mask.
Live isolation: disabling SSAO, material AO/normal, or sun shadow maps did not
remove the outlines; disabling ScreenSpaceShadowsPostFx did.

The shared classic/canyon/roadcut contact material now uses the forward path:
translucent=true, translucentBlendOp=LerpAlpha, translucentZWrite=false,
translucentRecvShadows=true. Alpha testing remains enabled and castShadows stays
false. BlendOp=None was tested and still took the opaque/depth-writing path.
To keep sparse erosion rather than semitransparent surviving texels, alphaRef is
255 and opacityFactor is 255/128. This preserves the prior cutoff (128/255),
while surviving alpha is >=1 and saturates to an opaque hardware blend weight.
Distance visibility may still fade as before. No texture/geometry changes or
additional draw calls; no engine shader or global shadow settings changed.
Baker and packaged JSON both updated; installed material deployed and live-reloaded.

SSS, SSAO, sun shadows, original camera and profiler settings restored. Screenshots
in build/projected-cliff-strips: sss-before.png, sss-disabled-diagnostic.png,
sss-after.png, sss-after-angle.png. The false offset shadows are gone with global
SSS enabled; surface normal shading and receiving shadows remain. Forward overlays
can have sorting limitations at overlapping translucent surfaces; no obvious new
ordering artifact appeared in these two views, not an exhaustive validation.

Matching loaded-scene 12-second camera travel, 720 frames/run, 60 FPS cap:
- Previous opaque: median 16.662 ms, P95 16.877, P99 17.559, max 28.939.
- Forward cutout: median 16.660 ms, P95 16.885, P99 17.537, max 28.334.
Both had one frame >25 ms. No material regression visible in this capped sample;
this is not an uncached-generation test or proof of equal GPU cost.
Raw data: screen-space-shadow-frametimes.json. Validation: baker parses, all
packaged texture references resolve, clip-boundary equivalence and fully opaque
survivors checked numerically, git diff --check clean. Changes uncommitted.
