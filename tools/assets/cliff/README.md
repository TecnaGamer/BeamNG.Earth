# Cliff asset sources

The current coastal atlas is assembled from five CC0 Poly Haven scans.
See [SCANS.md](SCANS.md) for provenance, dependencies and reproduction commands.
`scans.json` records download URLs and checksums; raw scans stay in ignored `build/`.

`rock_06_nor_gl_2k.png` is the original CC0 Rock 06 OpenGL normal map by
Rob Tuytel / Poly Haven, used offline to bake road-cut micro relief.
Source: https://dl.polyhaven.org/file/ph-assets/Textures/png/2k/rock_06/rock_06_nor_gl_2k.png
MD5: b68a3a7753345d0f7ca670c15785941d

The separate road-cut source and 4K restored image are in
[`../roadcut_generated`](../roadcut_generated/RESTORATION.md).
Runtime maps are under the mod's `art/shapes/beamngEarth/cliff/` directory;
CPU displacement maps are `server/cliff_height.png` and `server/cliff_cut_height.png`.
