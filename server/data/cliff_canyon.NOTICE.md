# Canyon cliff texture set

Contributor: Austint30. Files: `canyon_*.webp` under
`mod/texture_sources/levels/beamng_earth/art/shapes/beamngEarth/cliff/`,
and the same named files in the optional 4K pack.

Tool: OpenAI image generation through ChatGPT/Codex, confirmed by the contributor
on 2026-11-20 and by the retained authoring records for the arid/canyon assemblies.
Model/version: **not recorded**. No particular image model or model version is
asserted. Generation dates are approximate (late October / November 2026);
the following dates identify records, not an exact generation timestamp.

Authoring notes record revisions on 2026-11-03 and 2026-11-06, including the `tiled-v5` assembly.

Sixteen overlapping generated patches were assembled and processed into aligned near/far PBR maps. The retained manifest describes 1254-pixel patches and a 64-pixel overlap halo. The appearance is illustrative, not measured geology.

## Licence and contributor confirmation

On 2026-11-23, Austint30 confirmed that this generated texture set used only
his own prompts and generated images, with no third-party reference images,
and authorized dedicating his rights in the set to CC0.

To the extent possible under law, Austint30 has waived all copyright and
related or neighboring rights in this generated texture set under
[CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/).
This dedication covers the authored source images and their derived PBR maps,
including both bundled and optional high-resolution versions. It does not
relicense unrelated code, stock game assets, or separately credited scans.

Tool: OpenAI image generation through ChatGPT/Codex. Exact model/version and
exact generation timestamps were not retained; the record dates above are
not represented as exact generation dates. The no-third-party-input statement
is the contributor's confirmation, not a reconstruction from retained prompts.

The bundled version is derived from the retained lossless sources by
`tools/prepare_cliff_pack.py`: color at most 2048 square; normal/scalar at most
1024 square; filtered normals renormalized. Conversion to BC7 color, BC5 normal
and BC4 scalar DDS with mipmaps uses the existing native installer. This does not
replace authored normals with color-derived runtime normals.
