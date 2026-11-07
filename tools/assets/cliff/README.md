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

## Default cliff erosion normal

`tools/bake_default_cliff_erosion.py` combines the existing 8192 x 8192
`scan_v2_flat.normal.png` with the approved `canyon_far_v8.normal.png`.
It adds their surface gradients, preserving the scan detail and the separate
micro-normal slot without an additional runtime texture sample. The erosion
uses the near canyon scale (274.32 m horizontally, 182.88 m vertically), with
periodic crop feathering and a 0.45 slope gain. The default cliff UV repeat
remains 91.44 m; the larger erosion pattern is cropped into that repeat.

Run the baker with Python, NumPy, OpenCV and Pillow. Have BeamNG cook the
generated `scan_eroded_v1.normal.png`, then run the baker with
`--publish <cooked DDS path>`. Publishing validates BC5_UNORM, full 8K
resolution and all 14 mip levels before updating both default cliff materials
to `scan_eroded_v1_ready.normal.dds`. The adjacent bake JSON records inputs
and parameters. Keep the original scan normal for reproducible rebakes.
