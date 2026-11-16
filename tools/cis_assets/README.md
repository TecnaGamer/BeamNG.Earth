# CIS S-1 and S-2 assets

All imagery is original deterministic procedural drawing (NumPy + Pillow), with no fonts, photographs, image-model outputs or network inputs. Seeds are fixed in code. Python 3 with numpy and Pillow; Blender 4.5 for previews.

From the repository root, rebuild each atlas in order:

    python tools/cis_assets/build_atlas.py A0
    python tools/cis_assets/validate.py A0
    D:/blender-4.5/blender.exe --background --python tools/cis_assets/preview.py -- A0

Repeat these commands with A1, A2, A3, A4, A5. For M1 through M6 use `python tools/cis_assets/build_modules.py M1` instead of build_atlas.py; run the validator and Blender preview with the same task ID after each. A0 must precede all other tasks. Later validation includes all earlier tasks. Each generator updates only its entries in shared materials/module metadata. Rebuilding all tasks in this order reproduces every production file. Run `python tools/cis_assets/report.py` after the last task to rebuild the report. Run preview.py again for A1 through A5 after modules are built to include their final stamped geometry.

Output: `mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cis/`; previews: `assets/cis_renders/`. No DDS conversion: material map references deliberately use future DDS filenames. The roof tiling material is a 1024-square resampling of the common 2m roof cell; atlas remains 96 pixels/metre. Atlas origin is top-left; mesh UV V is inverted. Two pixels of extrusion on either side form each four-pixel gutter. Physical dimensions are rounded once to nearest integer pixel. Roughness is 8-bit single-channel grayscale (Pillow mode L) with metallicFactor=0, as localpoi (no invented ORM packing). Normals are green-up from semantic relief, not luminance. UUID5 uses URL namespace and `bne-cis/<material name>`.

The COLLADA writer uses one geometry, separately indexed welded positions, face normals and UVs, meter units, Z_UP, material bindings, one visual-scene node and no collision/LOD nodes. Common atlas UV cells are referenced directly by modules. Physical size variants and top-only overrides are explicit in module metadata. `bounds_min/max`, `mount`, `task`, `budget`, and `top_only` are additive metadata. Module galleries show each deliverable; building previews show seeded cell choices and modules available at render time. Far building previews use painted balcony cells and no geometry modules. No in-game acceptance is claimed.
# Final rebuild and verification

`python tools/cis_assets/verify_rebuild.py` rebuilds each task in order in a fresh system-temp directory, runs the validator after every task, and compares all production file bytes. The recorded SHA256 inventory is `assets/cis_src/rebuild_verification.json`.

`python tools/cis_assets/render_batch.py A0 A1 A2 A3 A4 A5 M1 M2 M3 M4 M5 M6` regenerates all previews sequentially. Blender logs stay locally in assets/cis_renders and are not committed. All previews are 1600 x 1000, Cycles with a uniform neutral world and ground plane. Near cameras use 1.7 m eye height and approximately 30 m horizontal distance to the closest building corner; lens widens to keep the complete building in frame. Far building cameras are 300 m off the facade with no stamped geometry. Module galleries retain geometry in all views, since individual modules have no far skin. A0 is a common-material swatch gallery. A4 additionally renders all three views of the 24 x 24 m tower. Material aliases reference A0 DDS paths with colour factors and do not require additional texture files.

## S-2: F1 correction

Regenerate the S-1 PNGs using the existing commands above; the committed DDS files are retained untouched. All `.data.png` and `.lit.png` files now use mode **L**, including common and standalone roof maps. Color/normal stay RGB. `build_atlas.py` adds seeded patchwork weathering to A1?A4 only, with damp common plinth/parapet strips; A5 color and normal bytes stay identical. Existing cell IDs, weights, rectangles, dimensions, materials and COLLADA bytes are preserved.

    python tools/cis_assets/validate.py F1
    D:/blender-4.5/blender.exe --background --threads 8 --python tools/cis_assets/preview.py -- F1
    python tools/cis_assets/verify_rebuild.py
    python tools/cis_assets/report.py F1

The F1 preview command refreshes A0?A4 and saves explicit `khrush_panel_after_<view>.png` and `panel9_after_<view>.png`. Their `before` images are archived from the committed S-1 previews and remain immutable reference artifacts. `assets/cis_src/f1_baseline.json` records all original cell layouts, DDS and non-pixel hashes, and original PNG byte counts from the S-1 commit. Verification excludes retained DDS, which are reviewer conversion outputs and are never regenerated here. PNG/JSON/DAE files remain byte-identical to a clean generator rebuild.

The generator also carries the tasks of the next atlases (A6-A9: Stalinist blocks, brick towers, new low-rise and panel blocks); they are not shipped yet.
