# Cliff material pipeline

The coastal material covers 91.44 × 91.44 metres (300 × 300 feet) with an 8192
square atlas assembled from registered scans. Runtime assets are already baked;
players do not need Blender, source downloads or these authoring tools.

## Sources

- [Coastal Cliff 01](https://polyhaven.com/a/coastal_cliff_01): Rob Tuytel, Rico Cilliers.
- [Coastal Cliff 02](https://polyhaven.com/a/coastal_cliff_02): Rob Tuytel.
- [Coastal Cliff 04](https://polyhaven.com/a/coastal_cliff_04): Rob Tuytel, Rico Cilliers.
- [Rock Face 01](https://polyhaven.com/a/rock_face_01): Dario Barresi.
- [Rock Face 02](https://polyhaven.com/a/rock_face_02): Dario Barresi, Rico Cilliers.

All five use [CC0](https://polyhaven.com/license). `scans.json` records the
download URLs, authors and checksums. The downloader uses Poly Haven's public API.
Rock 06 by Rob Tuytel / Poly Haven supplies the separate CC0 micro normal;
its original source and checksum are in [README.md](README.md).

## Coastal bake

Run from the repository root with NumPy, OpenCV and Pillow. The projection and
review renderer were authored for Blender 3.6. Raw downloads and intermediates
stay under ignored `build/`; allow substantial RAM and disk space for 8K float maps.

```powershell
python tools/fetch_cliff_scans.py --resolution 8k
$blender = 'C:/Program Files/Blender Foundation/Blender 3.6/blender.exe'
foreach ($asset in @('coastal_cliff_01', 'coastal_cliff_02', 'coastal_cliff_04', 'rock_face_01', 'rock_face_02')) {
    & $blender --factory-startup -b -t 8 --python tools/project_cliff_scans.py -- --asset $asset --pixels-per-metre 90
}
python tools/assemble_cliff_scans.py --size 8192
& $blender --factory-startup -b -t 8 --python tools/render_cliff_assembly.py
python tools/bake_scan_cliff_material.py
python -m unittest discover -s tools -p test_cliff_assembly.py -v
```

Depth comes from mesh positions. Albedo, normals, roughness and AO share the same
projection and patch ownership. Bounded color normalization, varied patch placement,
minimum-error seams and a common coordinate warp reduce visible joins. Large relief
and deterministic runoff processing vary the surface beyond individual scans.
Runoff is an artistic approximation, not a geological simulation. Heightfields
cannot reproduce caves or undercuts; projection stretching and captured lighting
can remain. Review wide, close and clay renders before installing a new bake.

The runtime bake applies periodic edges and writes the `scan_v2_*` maps plus
`server/cliff_height.png` (256 × 64, 16-bit). Runtime normals use a flat reference
and OpenGL +Y convention. Full relief favors visible detail on the low-density
mesh, but can reinforce slopes already represented by geometry. The offline
review renderer instead subtracts its dense mesh's relief. Undisplaced UVs keep
the height and texture phase aligned. Fine detail repeats independently at 1.2 m.

## Natural material calibration (2026-11-01)

The natural scan material uses RGB base factors (1.3, 1.4, 1.5), replacing
(2, 2, 2) to reduce bright orange outcrops beside the arid road cuts and terrain.
It shares the packaged Rock 06 micro normal with arid cuts at strength 0.3 and
1.2 m detail scale. Regional vertex tint remains active; the scanned macro
normal, albedo atlas, roughness, AO, and CPU displacement are unchanged.
This is the shared natural material, so the calibration applies in all climates.
The bake script preserves these defaults. Live review: El Paso at
31.87931, -106.49860, with natural outcrops, cuts, and terrain in the same view.

## Road-cut bake

The approved limestone image was restored in sixteen overlapping generative
crops, registered and assembled at 4096 square. The committed `restored_4k.png`
allows deterministic rebaking without an image-generation service:

```powershell
python tools/restore_roadcut_material.py --bake --install
```

`--detail --install` rebakes only the scanned micro normal. `--assemble` requires
the local generated crop outputs and `tiles.json` in `build/roadcut-restoration/`;
those intermediates are not part of the repository. See
[RESTORATION.md](../roadcut_generated/RESTORATION.md) for provenance and prompts.
Joint-guided shading relief is approximate, not measured depth. The existing
CPU road-cut heightmap and 91.44 m UV span are preserved when rebaking materials.

## Runtime-only game assets

Cliff rubble privately copies installed Johnson Valley rock collections and
rebinds their materials for regional tint. Fine grain and D0206 fallen-rock decals
also use the player's installation. These proprietary source meshes and maps are
not redistributed. Their copies live under BeamNG's `/temp/beamngEarth/` directory.
