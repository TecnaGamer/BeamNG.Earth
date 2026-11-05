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


## Irregular rim variation

The same South Rim aerial and supplied canyon photographs show patchy soil/rock
contact rather than a uniform fade. Open PR titles, bodies and file lists were
checked again; no matching cliff implementation was found (no outside code used).

Two octaves of the existing native 3D value noise modulate the transition weight
before smoothstep. Coordinates are the global source grid and source height, so
neighbouring tiles share a deterministic pattern. Coarse/fine lattice spacing is
6/2 source units, weighted 70/30; amplitude is bounded by 0.8*t*(1-t). Both endpoints
remain exact, preserving the buried contour and full interior rock. Geometry,
material blending and later relief still share the same weight. Only transitional
vertices evaluate noise. No additional shader work, textures, layers or decals.

Windows native rebuilt (unchanged v146 API). 194 tests passed, two optional-data
cases skipped, including repeatability, bounded variation, unchanged endpoints,
halo independence and tile seam continuity. Linux rebuild is still pending.

Same-camera temporary probes were inspected in shot_20261104_022617_7.jpg (before)
and shot_20261104_022619_8.jpg (after). Variation is deliberately subtle at this
viewing distance; broad collars and terrain-lighting differences remain. Original
objects were restored. Exports in build/cliff-rim-noise-preview/ grew from
220130/273055 to 220837/273062 triangles (0.32% / 0.003%), through the existing
adaptive relief refinement; both versions used 31 pieces total. No FPS claim is
made from this comparison. Restart the server and regenerate the location to load
these new baked weights; already loaded meshes are unchanged.


## Upstream dev integration (2026-11-04)

Merged upstream dev d46e07b8. Native API 147 combines the cliff streaming and
transition kernels with upstream CDAE and float-grid compression. Cliff detail
threshold changes and the distant-cliff toggle now unpack shapes before editing
and retain compression when writing them. Split-cliff object names remain intact.

Windows native rebuilt and cliff, compression, upstream server and Lua regression
checks passed. Linux native rebuild remains pending: the local WSL installation
has no Rust/C toolchain and Docker is not running. The minimum-version check
rejects the older Linux binary until it is rebuilt.


## Generation switches (2026-11-05)

The graphics message carries nearby faces, scattered rocks, transition decals and
existing distant-face preferences. The shared server snapshots the union of
connected players' requests for each worker job; explicit offline area builds
without players retain all scenery. An off switch therefore skips its generation
unless another player on the same server needs it. Already running jobs finish;
no cancellation of native code or mutation of loaded shapes is attempted.

All nearby components off returns before terrain/classification sampling. Props
without faces retain coarse cliff detection but skip render-grid resampling,
relief, terrain transitions, PBR/tint work, canyon refinement, streaming partitions
and LOD generation. Rocks-only sit on the terrain, since there is no rock skin to
fit them to. With faces enabled, disabled rock/decal stages are skipped separately.
Road and geotile revisit keys include all three flags. Revisit bundles and pooled
slots use only the current content manifest, excluding obsolete cliff files.

Distant faces off bypasses farcliffs.geometry. Terrain geometry, satellite texture
and ordinary terrain shading remain. If an off-built cached tile is requested
after enabling, it is rebuilt with cliffs using a new source path/material slot;
its old rendered shape is left intact. Nearby objects already present toggle
immediately. Reload the level after enabling to populate tiles built while off;
distant appearance continues to apply as tiles load, as before.

Open upstream PR titles, descriptions and file lists were checked; no matching
cliff generation gate was found. No outside implementation was imported. This
change only routes settings and gates existing kernels; native API stays 147.

Validation: 29 focused generation/cache/material-slot tests passed, plus the
existing cliff geometry, distant cliff, worker, graphics, revisit, road-geotile
and protocol suites (optional real-data fixtures disabled). Lua settings and core
suites passed 361 and 331 assertions. The distant upgrade test retains a held
material slot and checks its source and pooled shape bytes remain unchanged.
No in-game frame-time measurement was made for this batch. Restart the server
and reload the game to load the updated generation controls.


## Near detail and contact corrections (2026-11-05)

The approved canyon source is 1254 x 1254, enlarged to a 4096 x 4096 runtime
albedo. Its diffuse-derived PBR maps are also 4096 x 4096; this does not make
the source native 4K. A final built-in image generation attempt explicitly
requested native 4096 x 4096 but again returned 1254 x 1254. The candidate is
not installed and no native-4K claim is made. The paid API fallback was not used.

Near canyon material now uses a restrained independently tiled grain/detail
normal at a square 1.2 m physical repeat, while preserving the large geological
beds. Broad normal strength is reduced from .65 to .42. Distant materials and
assets are unchanged. This adds material texture samples, not meshes or decals.
BeamNG supports these independent detail maps and scale controls:
https://docs.beamng.com/modding/materials/materials_1.5/
https://documentation.beamng.com/modding/materials/vehicle/common-detail/
Detail maps are preferable here to increasing geometry or adding parallax
marching. Triplanar projection needs extra texture samples; virtual-texture
terrain blending would require engine support rather than just material JSON.
https://dev.epicgames.com/documentation/unreal-engine/adding-detail-textures-to-unreal-engine-materials
https://developer.nvidia.com/gpugems/gpugems3/part-i-geometry/chapter-1-generating-complex-procedural-terrains-using-gpu

Native API 148 fixes contour intersections to use full terrain-weight attributes
rather than partially faded endpoint attributes, blends geometric normals toward
the ground normal, and samples color at ground coordinates rather than displaced
rock coordinates. Shared by all near cliff material families. A coarse-grid
flood fill repairs only enclosed sloping detection dips up to 64 square metres
and 12 metres across, preserving road/water/city/unknown/flat/boundary exclusions.

Validation: the combined 108-test geometry/detail/transition/generation/LOD/
far-material suite passed against the installed native API 148.
Windows native 148 built and installed; server restarted without closing the
game or moving the camera. Live rebuilt geometry was inspected in screenshot
shot_20261105_010804_3.png. Lighting changed during the comparison. Visible pale
rims and larger holes remain; this is not a complete seam fix. The rim still
samples cached source imagery, whereas terrain uses finished tone/deshadow
processing. Matching that finished pipeline is outstanding. Linux native rebuild
remains pending because the local toolchain is unavailable. No push/release.

## Reconstructed 4K canyon material (2026-11-05)

The earlier single-image 4K limitation above is now resolved with the user's
requested crop/regenerate/stitch workflow. Sixteen independently generated
1254 x 1254 patches cover a 4 x 4 grid. Each patch is downsampled to 1152 square,
with a 1024-pixel core and 128-pixel overlap. No generated patch is enlarged.
The assembled albedo contains newly generated fine rock detail at 4096 x 4096;
it is not a single native-4K generation or an enlargement of the old full image.

tools/bake_canyon_tiled.py reproduces the assembly, using broad palette
correction against the approved v4 image, cosine overlap weights, and an
8-pixel periodic border. Patches, exact generation prompt, dimensions and hashes
are in tools/assets/canyon_generated/tiled-v5/. The original layered composition
and mean reflected colour are retained. The 1-pixel high-frequency RMS increased
from 0.00761 to 0.03238; opposing albedo borders match exactly. This metric confirms
added fine content, not measured geological accuracy.

Normal, roughness and cavity AO are computed at 4096 square from the complete
assembled albedo using wrap-aware filters. They are approximate inferred PBR,
not measured channels. The material remains nonmetallic and retains its existing
normal strength and independent 1.2 m detail repeat. No extra meshes, decals,
texture samples or per-frame generation work were added by this replacement.
The runtime dimensions match the previous upsampled maps, so pixel storage is
unchanged; compressed asset sizes can differ.

Only bne_cliff_canyon_sat and its terrain-transition variant use the new v5
maps. Distant material definitions and their approved v4 assets are unchanged.
The full legacy canyon baker finishes by applying this reconstructed near bake,
so it cannot silently restore the upsampled near material.

Validation: 31 transition/far-material tests passed, including four 4096-square
runtime maps and all 16 source patches being at least their output footprint.
Installed the new maps into the unpacked mod and reloaded both near materials.
Live fields verified all four v5 maps; screenshot shot_20261105_020335_4.png
was inspected without moving the camera. The pale contact rims and larger
threshold holes documented above are still visible and remain separate work.


## Canyon close-up material refinement (2026-11-05)

Compared paired Utah, Italy, East Coast and desert detail maps from the installed
BeamNG archives in the same nearby view. Italy's irregular fine grain was the
best fit; Utah/desert were softer and East Coast flakes competed with the beds.
The selected color/normal DDS files are copied unchanged into the mod as
canyon_detail_italy_v1.*, including their mip chains. Source archive paths and
SHA-256 hashes are recorded in canyon_detail_italy_v1.sources.json, with asset
attribution in STOCK_TEXTURES.md. Runtime references use /levels/beamng_earth/,
not the stock archives or generated temporary files.

The pair repeats every 0.9 m in both physical directions despite the main chart's
91.44 x 182.88 m footprint. Color strength is .10 and detail normal strength .24.
Use the actual BeamNG normalDetailMapUseUV field (verified against live fields),
not detailNormalMapUseUV. Anisotropic filtering is enabled. Official controls:
https://documentation.beamng.com/modding/materials/vehicle/common-detail/
https://docs.beamng.com/modding/materials/materials_1.5/

Rebuilt the aligned 4096-square normal, roughness and AO from the accepted v5
albedo. Locally normalized contrast at several scales emphasizes coherent stone
faces, cracks and ledges; fine grain contributes little to the main relief.
Normal strength .30 was selected after checking grazing sunlight. Roughness
varies with granular/recessed areas and exposed faces; bounded multi-scale
cavity AO darkens inferred recesses. These are approximate albedo-derived
channels, not measured PBR. The accepted albedo pixels are unchanged.

Both near canyon variants use these maps. Distant materials remain unchanged.
The copied detail pair totals 699380 bytes and replaces the previous two detail
samples; no additional draw calls, geometry or texture samples were introduced
by this refinement. No frame-time improvement is claimed.

Validation: 36 canyon PBR, transition and far-cliff tests passed. Installed and
reloaded both near materials in game; compared all four stock pairs and old/new
PBR at identical camera positions. Camera restored afterward. Evidence lives in
build/canyon-detail-study/, including final-wide-comparison.png and
final-close-comparison.png. Existing contact-rim and large threshold-hole issues
remain separate from this material refinement.


## 2026-11-05: larger detail and sharper normal edges
Following feedback, enlarged near canyon detail repeat from 0.9 to 2.4 m in both
physical axes (2.67x larger features). Main normal strength remains .30 and detail
strength remains .24. Replaced Gaussian face averaging with wrap-aware bilateral
filtering of locally normalized relief. Denoises within faces without rounding
across high-contrast joints; the initial 1.2-pixel grain prefilter still limits
pixel noise. Roughness/AO rebaked from the revised relief. Albedo and far materials
unchanged. All filtering occurs offline, with no added runtime shader samples.
37 focused PBR/transition/far tests pass, including sharp step preservation without
height overshoot. Installed/reloaded live and compared identical camera view;
camera never moved. Backup and screenshot pair: build/canyon-hard-edge-study/.
Broad albedo softness at extremely close range remains; enlarged detail does not
replace missing broad diffuse information. No commit or push.


## 2026-11-05: separate broad canyon relief from detail grain
Shifted the near canyon main normal toward larger blocks and ledges: area-reduce
and median-filter the face signal before edge-preserving reconstruction, remove
pores from main height, and weight broad bedding/block variation more strongly.
A first pass flattened broad relief too far, so broad components were increased
before final installation. Main material strength stays .30. Small grain belongs
to the independently tiled detail normal, now .42 strength (was .24), with a
3.0 m physical repeat (was 2.4 m, features 25% larger). Color strength stays .10.
Rebaked aligned 4K normal/roughness/AO; diffuse and distant definitions unchanged.
No new shader samples or geometry. 38 focused tests passed, including broad
normal frequency response relative to fine grain. Installed and reloaded both
near variants; inspected live screenshots in build/canyon-scale-study/.
Camera was not moved by this work. No commit/push.


## 2026-11-05: visually verified normal balance
Feedback: coarse normal became flat/rounded, while .42 detail strength appeared
noisy. Captured unchanged-camera before, detail-only isolation, initial candidate,
and refined final screenshots in build/canyon-balance-study/. First candidate
restored too much small pebbling and failed fine-frequency rejection; refined it
before final installation. Face reduction now uses 2-pixel stride, 5-sample median
and 2.5-pixel prefilter at 4K, retaining medium fractures lost by the previous
4-pixel stride. Relief weights: facets 1.4, ledges 1.2, macro 1.0, recess -.25.
Main strength remains .30. Detail strength .16 replaces .42; repeat remains 3 m.
Final screenshot shows clearer medium fractures and less speckled highlight noise.
38 focused tests pass, including frequency rejection and sharp boundary checks.
Rebaked normal/roughness/AO and installed/reloaded both near canyon variants.
Distant definitions unchanged; camera not moved. Final visual comparison:
build/canyon-balance-study/final-comparison.png. No commit/push.


## 2026-11-05: sharper 2K close-up detail layer
The 512-square Italy pair remained soft at the enlarged 3 m repeat. Compared
stronger Italy color contributions (.10/.35/.60), inspected a soft 1K stock detail
candidate, and inspected six stock rock albedos. West Coast's tileable 2048-square
surface/normal gave much sharper close-up cracks than the Italy detail pair.
Selected a moderate .28 color-data blend and .12 detail normal strength at 3 m;
main canyon normal, roughness, AO, albedo and distant definitions are unchanged.

Pinned t_wca_rock_b.color.dds and t_wca_rock_nm.normal.dds from rocks.zip locally.
Runtime canyon_detail_wca_v1.data.png is neutral RGB linear shader data derived
from source luminance, with broad color/illumination removed and mean .5. The
2048-square normal DDS is copied unchanged, with existing mips. Stock provenance
and hashes in canyon_detail_wca_v1.sources.json; attribution in STOCK_TEXTURES.md.
Reproduce detail data with tools/bake_canyon_detail.py; near PBR baker invokes it.
A single-channel draft was rejected during screenshot checks because the shader
needs RGB detail. Final validation explicitly checks RGB neutrality and size.

Captured before/after from exactly the same 3 m view and inspected screenshots:
build/canyon-detail-color-study/final-comparison.png. Clear small fractures now
break up the soft base instead of relying on noisy normal strength. Installed
both near material variants; original camera restored. 38 focused tests pass.
Texture resolution/memory increases from 512 to 2048 per detail map; shader sample
count, geometry and draw count are unchanged. Broad underlying albedo resolution
is unchanged. No commit/push.


## 2026-11-05: balance detail relief against painted cracks
Confirmed the pinned West Coast detail normal is loaded in both near canyon
materials. The cooked main normal and bundled detail normal both use BC5; the
shader decodes and applies the detail correctly. Live normal-off/.45/.70 captures
show its contribution. The previous .12 normal was too weak beside .28 color.
Selected .12 detail color and .45 detail normal, retaining the 3 m repeat and all
main PBR maps. This reduces the tan crack overlay while exposing angular surface
relief. Stronger .70 was rejected in favor of the restrained .45 comparison.
Installed/reloaded both near variants and verified their live fields. Screenshot
comparison: build/canyon-detail-relief-study/comparison.png (before, normal off,
selected moderate, stronger). Final original camera view saved alongside it.
Original camera restored; all other material definitions are unchanged. No new
texture samples, geometry, or draw calls. Existing contact seam/holes remain a
separate unresolved issue. No commit/push.


## 2026-11-05: use the selected t_dirt_rocky terrain detail pair
Confirmed the previous t_wca_rock pair is the main mesh surface in West Coast's
wca_rock Material (mapTo wca_rock1), which has its own separate detail pair.
It was unsuitable as small-scale canyon detail: its broad planar normals were
more apparent after increasing strength. The earlier description of it as a
stock detail material was incorrect.
Inspected actual TerrainMaterial sources from Italy, West Coast, and terrain.zip.
User selected t_dirt_rocky. Bundled its original 1024-square color and BC5 normal
from terrain.zip / assets/materials/terrain/rock/t_dirt_rocky/. The normal retains
stock mipmaps; color is converted into neutral RGB detail data. No upsampling.
Updated the detail baker, pinned provenance/hash manifest, material bindings,
README and validation to use this pair. Main 4K canyon maps are unchanged.

Final settings: 3 m repeat on both axes, color .18, detail normal .22. Compared
2 m/3 m and normal-off views; kept 3 m so individual stones remain readable.
Installed both near variants and verified live paths and strengths. During hot
import the engine temporarily retained its missing-texture fallback; reloading
both materials after import completed cleared it. Inspected the new final-close
screenshot to verify the fallback was gone and granular relief was visible.
Evidence: build/canyon-terrain-detail-study/final-close.png and
final-original-view.png. Camera restored. All other materials and all main PBR
bindings unchanged; no extra shader samples, geometry or draw calls. The detail
maps now use 1K rather than 2K. 38 focused tests pass; no commit/push.


## 2026-11-05: requested t_rock_b terrain trial
Switched both near canyon variants from t_dirt_rocky to West Coast terrain
Rock_cliff's t_rock_b.png / t_rock_nm.png pair, copied locally at native 1024.
The source is levels/west_coast_usa/art/terrains/ in west_coast_usa.zip, not the
previous t_wca_rock mesh material. Neutral detail conversion and provenance are
reproducible through tools/bake_canyon_detail.py. The near PBR baker now binds it.
Kept color .18, normal .22 and 3 m repeat for a direct comparison. No other
material fields changed. Original t_dirt_rocky assets retained for rollback.
Installed and reloaded after texture import; verified the normal's cooked mip
chain and inspected the close screenshot. Broader connected cracks replace the
previous granular pebble pattern. Same-view before/after and restored-camera view
are in build/canyon-t-rock-study/. Camera restored; 38 focused tests pass.
No commit/push. This is the active user-requested trial, pending visual feedback.


## 2026-11-05: restore rocky dirt PBR and refine canyon fractures
Restored t_dirt_rocky at 3 m, color .18 and normal .22 in both near canyon
variants. Copied its native 1K stock AO and roughness into the mod, with provenance
and hashes. Derived shader-detail data: AO is half the source (only subtracts
occlusion; white does not erase the main cavity map), roughness is mean-centered
around .5. Strengths .22 AO and .35 roughness. Local engine shader/editor confirms
ambientOcclusionDetailMap/detailAoMapStrength and roughnessDetailMap/
detailRoughnessMapStrength; both use the same detail UVs and scale as color.
Two extra near-material texture samples, no extra geometry or draw calls.

Main 4K maps now use canyon_v5_faceted_near: tighter bilateral face filtering
(.035 range sigma vs .055) retains fractured edges; local relief below -.20
retains only 15% of its former slope, creating flatter deep recess floors without
quantized terraces. Main strength stays .30. AO and roughness rebaked from that
relief. These remain inferred PBR, not measured surface height. An initial
1.6-pixel preblur experiment reintroduced fine noise and was rejected; retained
2.5-pixel preblur and median grain rejection. Main albedo and all far materials
unchanged. Bake default uses the new map names for reproducibility.

Installed and checked in BeamNG after texture cooking/reload. Same-camera
before/final full screenshots and native-pixel normal-comparison.png are in
build/canyon-angular-pbr-study/. The change is subtle: crisper joint transitions
and flatter concave floors, not stronger broad bumps. Camera was not moved.
Focused material, far-cliff and transition validation: 40 tests pass.
Existing geometry gaps/satellite shadow seams are outside this material edit.
No commit/push.
