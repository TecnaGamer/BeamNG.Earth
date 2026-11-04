# Cliff/terrain transitions: research and implementation (2026-11-04)

The test place is the Grand Canyon South Rim, West Rim Trail. Cached/aerial NAIP
and the user's canyon photographs show irregular exposed ledges meeting soil,
not the rectangular outlines produced by repeating a coarse detection mask.
Open upstream PR titles, descriptions and file lists were checked first; none
contained a matching implementation. No contributor code was imported.

## Options considered

- Decals: the user's previous experiment was too expensive. This change adds none.
- Transparent or alpha-clipped mesh rims: BeamNG supports both, but alpha clipping
  makes hard cutouts; translucent rims introduce sorting/depth and shadow handling.
  Those tradeoffs are unnecessary for this opaque rock/soil boundary.
- Runtime virtual textures: Epic documents a mesh/terrain blending workflow using
  virtual texture assets, volumes and mesh renderers. It is engine infrastructure,
  not a material-only facility we can assume is available in BeamNG.
- Opaque material-layer blending plus contour relaxation: selected. BeamNG's
  shipped defaultMat.hlsl blends each later layer's base color, normal, AO and
  roughness by that layer's opacity. It uses vertex RGB for base color, not vertex
  alpha for the layer opacity. UV1 can carry an inexpensive mask lookup while all
  near rock PBR maps keep their aligned UV0 chart.

Primary sources:
[BeamNG Materials v1.5](https://docs.beamng.com/modding/materials/materials_1.5/),
[Epic Runtime Virtual Textures and Mesh Terrain](https://dev.epicgames.com/documentation/unreal-engine/runtime-virtual-textures-and-mesh-terrain-in-unreal-engine).
Engine implementation also inspected locally at
`shaders/common/material/shadergen/defaultMat.hlsl` (material-layer loop).

## Implementation

`native/earthfast/src/clifftransition.rs` filters coverage at the detection-cell
scale (2 m Gaussian sigma, 4 m support in physical XY). A 0.60 contour trims the
original triangles; it never adds coverage in excluded cells. Boundary samples
are interpolated and shared by adjacent triangles. Unlike clamping each corner
to its incident fine cell, this cuts diagonally through the original staircase.
Narrow unsupported tips may disappear. Sampling-halo edges are not treated as
cliff boundaries; filtering precedes material/chart splitting and tile clipping.

Geometry uses the same smoothstep as the material rim: the full displaced
surface tapers toward the sampled terrain, ending 0.35 m below it. New contour
vertices are placed on the interpolated sunken ground. The later canyon heightmap
relief pass receives the same taper through a native weight kernel, so it cannot
restore full rock thickness at the edge. Interior relief remains unchanged.

Only crossing triangles gain intersections; no extra strip mesh is created.
Streaming partitions allocate the number of pieces needed by the triangle budget
rather than doubling all leaves at a power-of-two threshold. Each piece still
respects the existing 16,384-triangle target and the existing 64-piece cap.

Natural strata, limestone cuts, arid cuts and canyon faces share one 16x16 lookup
mask and four opaque material variants. No per-tile textures/material objects,
extra scenery objects, geometry skirts, decals or alpha sorting. UV1 encodes the
rim weight. UV0 still drives diffuse, normal, AO, roughness and exported tangents.
Existing streaming pieces, collision geometry and LOD mechanisms remain in use.
Old cached shapes retain their original material names and UV1 interpretation.
Distant terrain materials and normals are unchanged.

Only the rim samples cached satellite RGB, including the local photo's broad
shading. The rock interior keeps its existing shadow-rejecting mineral tint.
Linear-light mean compensation prevents the second layer from whitening the
transition. Missing imagery falls back to the existing rock's mean rather than
black or white. No network fetch is introduced. Rims fade bump/AO as well as color
into a rough, flat-normal soil layer. This is an approximate color match: raw
cached imagery can differ from season-graded terrain or a different imagery LOD.

Run `python tools/bake_cliff_transition.py` after rebaking a source cliff material.
It updates the four variants and `server/cliff_transition_means.json`; both the
packager and generated-content fingerprint include the calibration. The opaque
second layer adds shader work (including a mask lookup); it is not free.

## Validation and limitations

- 192 focused Python tests ran: 190 passed, two optional-data skips. All 85 Lua modules
  passed. Native contour regression passed. Coverage includes all four families,
  excluded holes, no base topology growth, halo/tile continuity, interior colors,
  aligned PBR tangents, material prewarm, streaming, LOD and road clearance.
- Actual cached tile g17_24706_51443: 110,781 sampled vertices; 54,848 base faces;
  11,533 rim vertices and 1,167 moved outline vertices. Zero reversed base faces.
  Native preparation median 7.41 ms, maximum 7.72 ms over ten warm runs; excludes
  imagery loading and downstream refinement/export. Runs server-side, GIL released.
- Same-input before/after finest exports: 256,013 vs 256,089 faces, 16 streaming
  pieces each, 11,349,720 vs 11,354,540 bytes (0.043% increase). The tiny face-count
  difference comes from the existing vertical refinement reacting to rounded
  corners, not a new tessellation pass.
- Initial temporary-shape visual validation missed a material configuration bug:
  Stages[1] was populated but activeLayers still defaulted to 1. Those screenshots
  did not demonstrate the intended blend. All original objects were restored.
- Follow-up at 36.05723, -112.14671 confirmed 220 loaded near cliff shapes used
  the new material, but the live material reported activeLayers=1. Explicitly
  setting activeLayers=2 made the rim blend visibly render in same-camera shots
  shot_20261104_012722_1.jpg and shot_20261104_012723_2.jpg. All four variants
  now declare two layers, guarded by a regression assertion. The corrected
  material was reloaded live and copied to the installed unpacked mod.
  Rounded/stepped silhouettes and some smooth-looking collars remain visible;
  this fixes activation, not all terrain intersection geometry.
- Before the activation fix, fresh shape imports had similar medians (9.19 vs 8.74 ms). The new material's
  first import took 21.75 ms before prewarming, so all variants are included in
  the startup UV2/color prewarm. These are registration timings, not total FPS.
- Before the activation fix, alternating same-view CPU medians were 4.56/4.88/4.71/4.59 ms (before/after/
  before/after). GPU timing stayed fixed at exactly 4.282816 ms even after enabling
  the timing overlay; it is stale and cannot substantiate a GPU performance claim.
  These timings do not validate the enabled second layer. GPU validation,
  especially Steam Deck, remains outstanding.
- Windows native v146 built. Linux binary rebuild remains pending (the referenced
  build helper is absent and this WSL installation lacks the compiler toolchain).
  Rebuild it before a Linux release. No release or push was made for this change.

Artifacts are in `build/cliff-transition-preview/`, including grid, exports,
timings, and paths to the initial before/after screenshots. The layer-activation
fix is already applied to the running game and installed mod; it requires no
mesh regeneration, server restart, or cache deletion.


## Contour and thickness follow-up

- Two same-camera cached South Rim tile comparisons visibly rounded the square
  protrusions along the foreground ridge. Temporary non-colliding probes were
  removed and the original streamed scene restored. Images:
  `shot_20261104_020918_5.jpg` (old) and `shot_20261104_020920_6.jpg` (contour plus relief taper).
- Initial contour exports added 2.4% and 4.5% triangles in the two tested tiles.
  The first partition implementation doubled one tile from 16 to 32 pieces;
  proportional partitioning now avoids that discontinuity while keeping the
  per-piece import budget. Final export/import results are recorded separately
  in `build/cliff-contour-preview/`. Final exports use 30 old / 31 new pieces
  across both tiles (the currently streamed old scene used 32). Median import
  time was 8.57 / 8.59 ms; maximum 10.04 / 9.40 ms. These are single-run shape
  registration measurements, not steady-state frame timing.
- A 110,781-vertex cached grid benchmark measured median 6.08 ms for preparation
  and 12.73 ms for one 54,848-face contour clip (eight warm runs). This is server
  work, not a per-frame operation, and excludes imagery/refinement/export.
- Final validation: 193 tests passed, 2 optional-data tests skipped; diff whitespace
  checks passed. No temporary contour probe objects remain in the game.
- Tests cover fractional contour edges, winding, exclusions, tile continuity,
  exact contour endpoints, geometry/relief taper agreement and batch thresholds.
- The material remains opaque and uses the already-enabled two-layer shader;
  no new decal or blending pass was introduced. GPU/FPS impact is not established
  by these import timings. A residual mismatch with the actual simplified terrain
  or its lighting can remain; this is not a claim that every intersection vanishes.
- This is generated geometry, so the running server must reload native v146 and
  regenerate the location. Old loaded shapes cannot acquire it through a material
  reload. Cache identity includes the source/native binary; no manual cache purge.
