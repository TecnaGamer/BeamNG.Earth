# Road-cut limestone restoration

The approved `source.png` is 1254 × 1254. Its original 2048-pixel material was
resampled, so enlarging that material could not reveal additional detail.
`restored_4k.png` is a 4096 × 4096 composite of sixteen generative restorations
made with the built-in OpenAI image-generation tool on 2026-11-01. It retains
the original bedding layout and broad mineral color. Fine fractures and pores
are synthesized, not recovered measurements or photogrammetry.

The source is divided into four rows and columns with 28 source pixels of
overlap on each side, wrapping across the outside edges. Each crop was restored
to 1254 × 1254, giving slightly more native output pixels than needed for its
1024-pixel atlas core. `restoration_tiles.json` records the exact bounds and
output hashes. Originals and model outputs remain in the local ignored
`build/roadcut-restoration/` directory. The assembled source is retained here
so rebaking does not require further AI calls.

`tools/restore_roadcut_material.py --assemble` registers the restored crops to
the source using bounded smooth optical flow, matches broad color, and blends
their overlaps. `--bake --install` rebuilds/selects the packaged 4K albedo,
normal, roughness and AO maps. The CPU height image is not modified. Shading
uses the original broad relief and approximate fine joint relief aligned to
the restored image; normals are projected against a flat receiver. Texture
scale remains 91.44 metres (300 feet), with the existing regional vertex tint.

This is an offline asset build: no extra procedural mesh work is done in-game.
Four 4K maps use more texture memory than four 2K maps. Geometry density,
collision and UVs are unchanged by restoration.

The `--detail` bake adds a separate 2048-pixel fine bump normal from the CC0
Rock 06 scan (Rob Tuytel / Poly Haven). It repeats over 1.2 metres and filters
out broad source-rock slopes at 3.5 cm before normalizing; small relief covers
the entire face, including the flat beds. The engine blends it at strength
0.7 over the approved macro normal. This replaces the existing detail-normal
sample, preserves detail color/UV scale, and adds no geometry. Source attribution
is in the packaged cliff `SOURCES.md`.

## Generative edit prompt

Use case: precise-object-edit. Edit target: the attached low-resolution limestone texture crop. Perform faithful generative super-resolution and artifact restoration, not redesign. Return a single square high-resolution texture crop, ideally 1536x1536 or larger. Preserve EXACT composition, every major horizontal bed boundary, crack, broken block, dark mineral streak and tan/gray color region in the same normalized positions; no zoom, crop, perspective, rotation or new layers. Replace fuzzy mottled cellular AI-generated surface artifacts with crisp physically plausible fractured limestone: sharp chipped edges, tiny angular flakes, fine irregular stone pores, subtle mineral grain, clean narrow fissures. Do not simply sharpen existing blur or amplify noise. Restore photographic high-frequency detail while keeping large and medium features locked to the input. Flat diffuse illumination/albedo-like color, no added cast shadows or specular highlights. Full bleed stone only; no borders, labels, text, vignette, vegetation. Keep the outer edges and their features aligned with the input for later overlap stitching.
