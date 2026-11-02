# Cliff material sources

Coastal `scan_v2_*` PBR maps are assembled from five registered CC0 Poly Haven
scans. See `SCAN_ATTRIBUTION.txt` for authors and links. Depth is projected from
source geometry, then assembled and weathered. `tools/assets/cliff/SCANS.md`
documents reproduction; `server/cliff_height.png` drives runtime displacement.

Road-cut `cut_restored_v1_*` maps use the approved AI-authored limestone image,
restored in sixteen overlapping generative crops and assembled at 4096 square.
See `tools/assets/roadcut_generated/RESTORATION.md` for provenance and baking.
Fine details are synthesized and normal relief is approximate. Existing CPU
displacement in `server/cliff_cut_height.png` is preserved by the material bake.

Both macro patterns span 91.44 by 91.44 m and use flat-reference normal maps.
Fine normal/color detail repeats independently at 1.2 m. Rock is nonmetallic;
metallicFactor is zero and needs no metallic texture.

`cut_restored_micro_v1.normal.png` filters tangent slopes from Rock 06 by
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

Arid road-cut `cut_arid_v1_*` maps use an original AI-generated interpretation
of two user-supplied TX-375 El Paso photographs, reconstructed in sixteen
overlapping detail crops at 4096 square. See
`tools/assets/arid_roadcut_generated/RESTORATION.md` and `generation.json` for
provenance, exact prompts and reproduction. Height and PBR relief are inferred,
not measured. The separate `server/cliff_arid_cut_height.png` shares the atlas
chart. World scale is 91.44 m; regional vertex color remains active. The shared
Rock 06 fine normal above retains its existing attribution and license.
