# Arid road-cut material

`source.png` is a 1254-square original generated with the built-in image tool,
informed by the two user-supplied El Paso TX-375 photos. Exposure is balanced
between their sunlit and shadowed appearances. Reference screenshots are not
packaged. This is a visual interpretation, not a measured geological scan.

`restored_4k.png` is a 4096-square composite of sixteen overlapping generative
detail restorations. It is not a simple enlargement. Large angular fractures
remain registered to the source; fine chips and grain are synthesized.
`restoration_tiles.json` records crop bounds, output sizes and hashes.
`generation.json` preserves both exact prompts and the tool used.

This experiment is superseded by `tools/assets/arid_generated` and
`tools/bake_arid_cliff_material.py`. Its unused images and baker were removed
during cleanup. The prompts and restoration metadata remain as provenance;
the original images and `tools/restore_arid_roadcut_material.py` are available
in Git at commit `16300ecf` if the historical experiment needs reproduction.

Historical bake notes follow (these do not describe the active material).

Albedo, normal, roughness and AO are 4096 square at the established 91.44 m
(300 ft) world scale. Narrow isotropic joints estimate relief without the
horizontal morphological bias of the limestone bake. Depth is approximate,
not photogrammetry. A separate 256 by 64 CPU height map is derived from this
same arid source; its pixel-center chart and orientation match the PBR maps.
The existing Austin CPU height and texture assets are unchanged.

The material uses existing regional vertex tint, the same neutral albedo
calibration and the shared CC0 Rock 06 micro normal (see packaged SOURCES.md).
Arid Koppen B climates select this cut family through the existing visual
biome; climate is an appearance prior rather than a lithology map. Austin's
bundled Cfa climate retains limestone. Natural cliffs retain their material.


## Terrain tone adjustment (2026-11-01)

The arid normal bake uses 35% of the inferred shading relief; fine normal
strength is 0.3 instead of 0.7. CPU geometry is unchanged. Source mineral chroma
is reduced to 50% and linear albedo contrast to a 0.65 power around the neutral
calibration. This retains detailed fractures while reducing rust/gray mottling.
The arid base factor is (1.4, 1.45, 1.55), tuned toward the warmer natural outcrops
in the live El Paso view, checking both sunlit and shadowed faces. Geographic vertex tint remains active. This tuning
applies only to the arid family; the limestone material is unchanged.
