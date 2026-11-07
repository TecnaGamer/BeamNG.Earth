# Arid fractured cliff material

This is the non-layered dry-rock appearance used at Hoover Dam and other arid
cliff exposures outside the higher-priority layered canyon regions. The supplied
Hoover Dam photograph informed the angular joints, irregular slabs and sandy
mineral palette; it is not used as a photographic texture or geological map.

## Source and resolution

`v1/manifest.json` records the built-in image generator prompts and source paths.
The initial 1254-square image is a layout guide. Sixteen separately generated
1254-square patches add local detail to overlapping footprints of that layout.
The existing canyon tiled assembler aligns their low-frequency colour and
feathers overlap into a seamless 4096 x 4096 result. This is a tiled generated
4K asset, not a single low-resolution image enlarged to 4K, and not a measured
photogrammetry scan. `v1/assembly.json` records source hashes and detail checks.
The upsampled `guide.png` is only a patch-layout reference, not the runtime map.

One texture repeat covers 91.44 x 91.44 metres (300 x 300 feet). Near material
also reuses the pinned rocky-dirt microdetail at a three-metre repeat.

## Rebuilding

Run `C:/Python311/python.exe tools/bake_arid_cliff_material.py` from the feature
checkout with NumPy, Pillow, SciPy and OpenCV installed. It reuses the canyon
edge-preserving diffuse-to-relief/PBR tools and tiled assembler. Near/far albedo,
normal, roughness and AO outputs are all 4096 square. The CPU displacement lookup
is deliberately much smaller (256 x 64); it is not the shader normal texture.
Normals/AO/roughness are inferred from diffuse appearance, not measured surface
properties. Moderate normal strengths avoid exaggerating residual albedo shading.
The far normal uses broader relief with finer relief retained at lower weight.

BeamNG cooks each `.normal.png` into BC5 DDS; copy those cooked results to the
corresponding `_ready.normal.dds` names referenced by the materials. Both checked
runtime DDS files have a 4096-square base and complete mip chains. Do not point a
material to an uncooked source PNG: that previously produced missing normals.
`arid_fractured_v1_bake.json` beside the runtime textures records material settings.

## Placement and performance

The shared cliff registry selects local dry climate classes 4-7, then applies
existing slope/land-cover/settings gates. Layered canyon regions take priority.
This is an appearance heuristic rather than a lithology classification. Road
cuts remain a separate provenance context but share the arid material family.
Satellite-derived mineral colour matching remains active for faces and rubble.

A sparse subset of existing arid cliff-toe stones becomes 4-8 metre Utah outcrops,
limited to one candidate per 24-metre scatter cell, within six metres of the toe
and on slopes up to 30 degrees. These replace candidates rather than adding a
second population, preserve authored LODs and collision, and use the existing
full-footprint road exclusion. Utah assets are copied from the installed game to
private runtime paths. Large outcrops reuse the new arid cliff albedo with
satellite-derived colour compensation, retaining the Utah mesh normals, roughness
and AO maps. Game-owned assets are not bundled in this source directory. Ordinary rubble and canyon placement stay unchanged.
