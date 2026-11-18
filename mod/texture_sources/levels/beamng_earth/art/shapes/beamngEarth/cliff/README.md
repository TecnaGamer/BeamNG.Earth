# Dynamic cliffs texture sources

These lossless WebP files are the authored inputs to the standard texture
installer. The manifest maps them to BC7 sRGB colour, BC5 normal, and BC4 scalar
DDS files with mipmaps. Normals, roughness and occlusion use their authored maps;
the installer does not infer replacement maps from the colour image.

Primary cliff maps retain 4096 × 4096 detail. Shared microdetail and scalar maps
keep their smaller authored sizes. The physical repeat is unchanged: 91.44 m
for natural and road-cut rock, with a 182.88 m vertical canyon chart. Separate
fine detail and satellite-driven colour provide variation at other scales.

## Provenance and redistribution

Natural rock maps incorporate the following Poly Haven scans, all released
under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/):

- [Coastal Cliff 01](https://polyhaven.com/a/coastal_cliff_01), Rob Tuytel and Rico Cilliers.
- [Coastal Cliff 02](https://polyhaven.com/a/coastal_cliff_02), Rob Tuytel.
- [Coastal Cliff 04](https://polyhaven.com/a/coastal_cliff_04), Rob Tuytel and Rico Cilliers.
- [Rock Face 01](https://polyhaven.com/a/rock_face_01), Dario Barresi.
- [Rock Face 02](https://polyhaven.com/a/rock_face_02), Dario Barresi and Rico Cilliers.

The scans were projected, assembled, colour-corrected, deformed and weathered.
`rock_grain.normal.webp` derives from [Rock 06](https://polyhaven.com/a/rock_06)
by Rob Tuytel, also CC0, with broad slopes removed before normalizing fine grain.
CC0 permits modification and redistribution of these sources and derivatives.

Limestone, arid and canyon imagery was authored for this contribution using
generative image tools and offline assembly and PBR processing. These are
illustrative rock materials, not measured scans or geological classifications.
Their authored contribution is distributed under the repository's MIT license;
the CC0 scan components retain their own permission above. Reference photographs
are not bundled as texture sources.

No BeamNG-owned rocky-dirt texture, stock rock mesh, or derivative of those
assets belongs in this folder or in the distribution. Dependent decorations
must obtain stock assets from the player's own installation and keep generated
maps in a private, versioned local cache.

## Reproduction

`modtextures.py` reproduces the installed DDS files directly from these sources
and the texture manifest. `tools/import_cliff_textures.py` imports an authored
material directory, caps primary maps at 4K, renormalizes resized normals,
deduplicates maps, and rejects known stock-derived inputs. It accepts the input
directory as an argument; no workstation-specific source path is required.

All cliff maps use the single `BNE_CLIFFS` opt-in. Set it to `1` before
installation or BeamMP packaging to prepare every cliff map. Without it, no
cliff maps are converted. Shared maps are encoded only once. Both categories'
assets are prepared up front so changing category settings never starts texture
conversion while driving.

With both categories enabled, the 27 authored maps have the following footprint
(decimal MB, measured from local player and BeamMP packages):

| Component | Size |
| --- | ---: |
| Lossless source files | 292.6 MB |
| Source payload in the player ZIP | 291.6 MB |
| DDS payload in the BeamMP client ZIP | 320.8 MB |
| Installed DDS files, including mipmaps | 378.2 MB |
| Estimated texture block memory if every mip of every map is resident | 378.2 MB |

ZIP payload totals exclude archive headers and other mod files. The memory
estimate counts compressed texture blocks only; it is not measured game RAM or
VRAM usage and excludes engine overhead, meshes and locally prepared stock maps.
Enabling just one category installs only its required maps. Shared maps count
once. The player download includes the lossless sources for both categories even
when both are disabled; disabled categories do not convert or load those maps.

The launcher prepares private contact-strip textures before starting the server,
using its selected BeamNG installation. For a standalone install, pass
`--game-dir` to `install.py`. The private files live under the game user folder's
`temp/beamngEarth/shapes/cliff_contact_v1`, outside the mod and package inputs.
Neutral additive ground maps are prepared alongside them in `cliff_ground_v1`
from the stock lossless color, AO and roughness sources; the normal DDS is copied
unchanged. Rust removes broad albedo shading with a periodic Gaussian, centers
fine detail on neutral gray, and encodes complete mip chains. Color detail uses
RGB BC7 to avoid a single-channel color cast; scalar maps use BC4.
Changed stock maps or encoder versions invalidate the cache; missing or damaged
outputs are rebuilt. If stock assets cannot be read, the client skips contact
strips and logs a diagnostic while keeping the cliff faces. Neither disabled
category causes private preparation. The installer respects `--dry-run`.
If private ground maps are unavailable, material loading disables those optional
detail layers while retaining the authored color, normal, roughness and AO maps.

Texture names use a family, purpose and channel, for example
`arid_near_base.color.webp`. Outcrops are embedded large rocks, companions are
their smaller neighbours, and rubble consists of settled stones at cliff toes.

### Limestone normal microrelief

The limestone normal includes an offline fine-relief pass from
`tools/enhance_limestone_normal.py`: small authored chips are strengthened and
seeded, spatially varied grain is baked into the same 4096-square normal. The
separate material detail-normal layer, color, roughness and occlusion are unchanged.
The result is renormalized and converted to BC5 with the standard installer.

To reproduce this pass, extract `limestone_cut.normal.webp` from commit
`f63626c8` to a separate input file, then run the tool with that input and the
source asset as the output. Do not apply the pass repeatedly to its own result.
