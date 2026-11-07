# Canyon rock material

Current near exposure is .53 (reduced again after user review); distant
contrast .84 and normal strengths .65 near / .70 far remain approved.

## Grazing-light material tuning - 2026-11-03

User requested darker near cliffs, slightly quieter far layers, and weaker
normals under steep sun angles. Material-only tuning: NEAR_CANYON_EXPOSURE=.82
(linear), FAR_CANYON_ALBEDO_DETAIL_STRENGTH=.84 (was .90), normalMapStrength=.65
near and .70 far (were engine default1). Engine defaultMat.hlsl verified this
scales tangent-space XY then renormalizes; macro features and normal UV scales
remain intact. Selected albedo pixels, normal files and CPU geometry unchanged.

Earlier revision history follows.

## Selected layered revision, 2026-11-03

The user selected `source-layered-v4.png` after iterative built-in image_gen
edits against their cliff photograph: irregular layer contacts, broken pale
stone, distinct darker mineral beds and softer fine-scale lighting. Exact
prompts and the source hash are in `layered-revision-v4.json`.

The selected source is **1254 x 1254**. The requested runtime albedo is
**4096 x 4096**, resampled by the baker; this is not native generated 4K detail.
The user chose to remain with the built-in generator instead of the API fallback.
The resampling preserves the source; it cannot invent higher-resolution detail.

Active albedo: `canyon_v4_base.color.png`, used by near canyon and distant canyon
materials. Near material factors preserve the previous albedo's linear mean
RGB, so existing satellite tints remain calibrated. Distant material mean
compensation and boundary correction read the new asset. Its distinct mineral
beds intentionally allow greater local hue variation (25% linear chroma bound;
mean satellite RGB agreement remains unchanged). The 64-square boundary lookup
is reduced in strips to bound each worker's memory with the 4K input.

Approved v3 near/far normal maps, their strengths and LOD scales, CPU displacement,
and geometry are unchanged. Their relief remains based on the earlier source;
this revision changes albedo rather than claiming a new measured PBR scan.
Older albedos remain packaged for existing material references and calibration.
The sections below describe the earlier revisions.

Generated with the built-in image_gen tool on 2026-11-02. The user's canyon
reference informed the visual brief: weathered red beds, buff/cream bands and
vertical joints. `generation.json` records the exact prompt and source hash.
`source.png` is the original 1254-square generated image (not a measured scan).

Run `python tools/bake_canyon_material.py` from the repository root to bake the
periodic base colour, restrained normal, roughness and CPU height maps. The
baker area-downsamples the original to 1024 x 1024 for BeamNG TextureCooker.
All runtime PBR maps must use power-of-two dimensions >=16; the generated
1254 x 1254 source is retained only as an authoring asset. Runtime names use
`canyon_v3` to bypass earlier texture-cache entries.
Normal/height relief is an approximation inferred from local contrast, with
broad albedo colour removed. CPU displacement is capped at 0.65 m before biome
scaling. The shader chart spans 91.44 m horizontally and 182.88 m vertically.
The physical scale is an artistic choice for broad beds seen across a canyon.

Near material: `bne_cliff_canyon_sat`. Far material: `bne_far_cliff_canyon_blend`,
satellite base colour, restrained rock detail, a shared normal map and constant roughness. Far cliffs reuse the terrain
triangles and add no displaced geometry, scatter, decals or colliders.
The original limestone, El Paso arid road-cut and natural scan assets remain.

The color revision uses `source-muted.png`, edited with built-in image_gen
against an actual game screenshot. `color-revision.json` records its exact prompt
and hash. It reduces the orange cast and bright cream-band contrast, retaining
a dusty rose/beige palette. The shared linear-light palette factor is
[0.84, 0.90, 0.93]. The baker multiplies by 4 for tint headroom and sRGB-encodes
the material factor, paired with sRGB-encoded vertex gains that cancel that
headroom. Both inputs are decoded by BeamNG before multiplication. Legacy
material names remain defined for already loaded older shapes.
Height and normal still derive from the original source, so this color revision
does not change cliff displacement or normal strength.

For distant cliffs, satellite color supplies the base, with the existing rock albedo used as a
detail map (strength 0.90 for canyon layers, 0.45 for other rock). The baker compensates the detail map's exact
linear mean under BeamNG's blendDetail equation, avoiding a fixed rose cast or
exposure shift. This preserves restrained mineral variation without white-
balancing pale beds toward green. Legacy `_sat` materials remain for old shapes.

Nearby canyon cliffs retain their full authored base-color texture and the
previous 45% exposure-normalized regional blend, with chromaticity correction
limited to 30%. Applying the far detail-only treatment nearby muted the rock
colors too much and was reverted following user review. Near photo shadows
remain excluded so real lighting stays authoritative. Far cliffs use the tile's
photo color and luminance (48 m Gaussian blur) in existing mesh vertices. At
shared cliff/terrain boundary vertices they sample the unblurred photo, keeping
the boundary color closer to its neighboring terrain. Far cliffs preserve photo
shadows. No extra GPU texture samples, triangles or imagery downloads are added.
Distant terrain also transitions from de-shadowed to original photo shading over
L0-L3; L3 and beyond retain the original shading. Both paths still use the
configured color matching, tone and season processing.

The macro color is sampled when geometry is built. Later asynchronous photo
color corrections update terrain textures immediately; cliff vertex colors
catch up when that tile is rebuilt. Existing triangle-mask boundaries remain
angular; this material pass does not add edge blending geometry.

Distant canyon normal: `canyon_far_v3.normal.png`. The deterministic baker combines
periodic vertical erosion grooves (2 m horizontal / 14 m vertical Gaussian
scales), broad source-aligned beds, and restrained fine fractures. Macro RMS
heights are 1.12 m for grooves and 0.64 m for beds, reduced 20% after live user
review. This affects shading only. Nearby normals and CPU displacement remain
unchanged. The map retains visible normal variation at a 32 x 32 mip, and uses
one shared texture sample. Other distant natural cliffs use scan_v2_flat normals.
No new image-generation call was used: the albedo is unchanged, color correction
is in the satellite tint, and normals are a deterministic height-field bake.

Distant normal scale follows terrain LOD tile width: 1x below 3072 m, 2x from
3072 m, 4x from 6144 m. These thresholds accommodate Mercator tile widths;
actual camera distance depends on terrain detail/FOV/streaming selection.
Both far rock families use `normalMapUseUV = 1` with a separate geographic
normal UV chart. UV0, satellite color and normal strength remain unchanged.
The chart is scaled before its integer origin is removed, preserving phase
between neighboring tiles at the same tier. Existing terrain LOD crossovers
handle tier changes; there is no native live shape edit, extra texture or draw.

The distant canyon detail strength is 0.90: stronger light/dark beds while the
mean compensation still matches satellite color. The boundary correction also
samples the authored detail's linear-light macro gain in the wall UV chart and
cancels it at shared cliff/terrain vertices. Interpolation tapers this correction
across boundary triangles; interior vertices retain full layer contrast. The CPU
lookup reads shipped material/albedo data, filtered to 64x64 in linear light.
No image pixels, normal maps, geometry, or nearby cliff materials are changed.
This softens broad color steps; it is not a pixel-perfect shader feather and
normal/geometry boundaries can remain visible.


### Distant erosion revision v7

The runtime canyon normal is now `canyon_far_v7_ready.normal.dds`: a cooked
1024-square BC5 normal with all 11 mip levels. `canyon_far_v7.normal.png` is
its authoring output. The earlier v3 description above records the original
profile. V7 keeps the same texture sample budget, UV scale and material strength.
It adds warped channels with variable depth, interrupted tributaries and
source-aligned rock chips. Rounded convex shoulders meet concave valleys with
a softened floor (profile epsilon 0.16; v6 used 0.025 and was too sharp).
Near normals, albedo, geometry and CPU displacement are unaffected.

Run `tools/bake_canyon_material.py --far-only` to produce the PNG. Preload it
in BeamNG with `ui_imgui.ImTextureHandler` and wait until the texture cooker
has produced the complete DDS in the game's temp tree. Then run
`tools/bake_canyon_material.py --publish-far <cooked DDS path>` to validate and
package it and update both distant canyon material bindings. Deploy the DDS
before the material JSON. Publishing a fresh PNG binding before cooking caused
intermittent "no normal" textures during live iteration; shipping the completed
DDS removes that import dependency.

Far mesh charts now retain their projection across same-facing bends when the
projection remains well-conditioned. This removes unnecessary chart switches
without adding triangles or draw calls. Opposing folds, tile chart boundaries
and cliff/terrain classification edges can still produce visible transitions.

Verified through BeamNG MCP at the user's camera, including a tighter FOV,
with the camera restored afterward. Screenshots are under
`build/far-normal-study/` (`final-dds.png`, `final-close.png`,
`current-check.png`). Automated checks cover continuous coarse-bend charts,
normal length/mip detail and complete packaged BC5 mip payloads.


### Distant erosion balance v8

V8 reduces broad groove relief by 30% (1.12 m to 0.784 m RMS) and increases
fine channels, tributaries, rock chips and fine source relief by 25%. Broad
beds, channel shapes, UV scales and material normal strength remain unchanged.
Runtime uses `canyon_far_v8_ready.normal.dds`, cooked and validated before
binding; the authoring output is `canyon_far_v8.normal.png`.


Near canyon erosion normal (2026-11-06)
-------------------------------------
`tools/bake_canyon_near_erosion.py` combines the approved
`canyon_far_v8.normal.png` with `canyon_v5_faceted_near.normal.png` into a 4096-square
normal. It adds surface gradients with periodic sampling, retains the separate
3 m dirt-rocky detail normal, and uses the existing UV0 normal slot in both near
canyon materials. No extra texture samples, mesh work, or distant-material changes.
Erosion features are enlarged 3x horizontally to match the far cliff width
multiplier. The inner 80% uses the exact enlarged sampling; a smooth closing
crossfade makes the cropped pattern tile across UV0 without a hard seam. Only
erosion is enlarged: diffuse-aligned joints and dirt-rocky microdetail retain
their original scale. Existing color, roughness and AO are unchanged.

After rebaking near PBR, run this tool again, cook its PNG through BeamNG, then
run `tools/bake_canyon_near_erosion.py --publish <cooked DDS path>`. Publish verifies
4096 x 4096 BC5_UNORM and the complete 13-level mip chain before binding
`canyon_v5_eroded_near_v2_ready.normal.dds`. Ship the cooked DDS; never bind a fresh
uncooked PNG in a running scene. Keep the faceted source normal for reproducible
composition rather than feeding the previously combined result back into itself.
