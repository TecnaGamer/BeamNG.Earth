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


### Relief-aligned roughness

After changing cliff normals or rebaking cliff materials, run
`python tools/bake_cliff_roughness.py` with NumPy, Pillow and SciPy installed.
This final authoring pass binds a roughness map to every cliff face material,
including distant variants and contact strips. It reads the actual normal source
(or its uncooked authoring PNG) and measures convexity at several scales.
Exposed ridges are modestly smoother; recessed joints are rougher. It does not
change albedo, normals, AO, geometry or placement. Terrain rim soil retains its
existing finish; scattered stock boulders retain their distinct authored maps.

The broad near face range is bounded to 0.68–0.96 and the distant/contact range to
0.75–0.97 before detail. Granular detail is restrained so it cannot overwhelm
the broad variation. Roughness follows the normal UV set, including UV1 on
far materials. The baked audit `relief_roughness_v1.json` records source hashes,
output hashes, ranges and convex/concave means. These are artistic estimates
from shading relief, not measured roughness. Run `test_cliffroughness` to check
curvature sign, wrapping, bounds, coverage and asset freshness.

Small, softly blended Perlin-shaped patches use a maximum 34% roughness reduction.
Patch cell widths are 32 and 12 texels. An independent 48-texel Perlin field varies
polish strength from 55% to 100% of that maximum. Two finer Perlin bands at 6 and
3 texels interrupt the polish within each island, restoring roughness toward the
dry base. A 20-texel field varies both their frequency mix and breakup amount
(35–95%), avoiding a uniform grain intensity across patches. Fine grain never
makes a texel glossier than the previous polish ceiling. Concave joints stay matte.
All noise fields wrap periodically. Near/contact maps include the patches;
standard mip filtering softens their fine detail with distance. Distant maps
retain broad matte variation. The audit records patch coverage and parameters.
