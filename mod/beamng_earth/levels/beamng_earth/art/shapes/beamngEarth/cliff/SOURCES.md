# Cliff material sources

Coastal `natural_*` PBR maps are assembled from five registered CC0 Poly Haven
scans. See `SCAN_ATTRIBUTION.txt` for authors and links. Depth is projected from
source geometry, then assembled and weathered. `tools/assets/cliff/SCANS.md`
documents reproduction; `server/cliff_height.png` drives runtime displacement.

Road-cut `limestone_cut_*` maps use the approved AI-authored limestone image,
restored in sixteen overlapping generative crops and assembled at 4096 square.
See `tools/assets/roadcut_generated/RESTORATION.md` for provenance and baking.
Fine details are synthesized and normal relief is approximate. Existing CPU
displacement in `server/cliff_cut_height.png` is preserved by the material bake.

Both macro patterns span 91.44 by 91.44 m and use flat-reference normal maps.
Fine normal/color detail repeats independently at 1.2 m. Rock is nonmetallic;
metallicFactor is zero and needs no metallic texture.

`rock_grain.normal.png` filters tangent slopes from Rock 06 by
Rob Tuytel / Poly Haven, CC0 1.0, retaining fine grain with a 3.5 cm high-pass
filter. Material strength is 0.7 for limestone road cuts and 0.3 for natural
cliffs and arid road cuts. The unmodified source is retained offline at
`tools/assets/cliff/rock_06_nor_gl_2k.png`.
https://polyhaven.com/a/rock_06
https://creativecommons.org/publicdomain/zero/1.0/
Source MD5: b68a3a7753345d0f7ca670c15785941d
Rebuild with `tools/restore_roadcut_material.py --detail --install`.

Additional fine grain, rubble and transition decals are copied privately from
the player's BeamNG installation at runtime. Proprietary game assets are not
included in this directory or the distribution.

Fractured arid `arid_near_*` and `arid_far_*` maps use sixteen independently
generated detail crops assembled at 4096 square. They replace the earlier
El Paso road-cut experiment. See `tools/assets/arid_generated/README.md` for
provenance and reproduction. The separate `server/cliff_arid_cut_height.png`
shares the atlas chart. Both natural arid faces and arid road cuts use this
family, with local mineral tint supplied by the runtime selector.

## Naming and terminology

Packaged textures use lowercase snake_case names describing their family,
role, and channel: `arid_near_base.color.png`, `canyon_far.normal.dds`, and
`natural_near_roughness.data.png`. Revision numbers belong in Git. Preserve
BeamNG's `.color`, `.normal`, and `.data` suffixes: they select import behavior.
A normal's authoring PNG and cooked DDS share a stem; both are retained when
an offline bake needs the PNG. `*_source*` denotes a retained bake input;
`canyon_reference*` supports the existing reference/compatibility materials.
`ground_detail*` is shared granular detail, while `rock_grain` is the CC0
micro normal. Source provenance and attribution files remain beside the maps.

Use **outcrop** for a large embedded rock projection, **companion** for its
smaller embedded neighbors, and **rubble** for loose stones at a cliff toe.
**Transition** means the opaque terrain blend; **contact strip** means the
separate rocky-dirt edge overlay. Serialized `talus_*` groups and `bne_*`
material IDs remain stable because cached tile payloads and meshes refer to
them. Lua `clifftalus.kind` translates those wire IDs to the population names.

After rebaking normals/materials, run `tools/bake_cliff_roughness.py` as the
final roughness pass. It preserves approved noise seeds in `roughness_bake.json`,
so renaming a source does not change its polish islands. Earlier bakers may
produce provisional roughness maps; only maps referenced by the final materials
or required by a subsequent bake belong in the shipped asset directory.
Validate bindings with `tools/run_tests.py test_cliffassets test_cliffroughness`.
