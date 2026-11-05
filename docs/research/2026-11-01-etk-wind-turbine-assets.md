# Installed ETK wind turbine audit

Inspected read-only on 2026-11-01. No game, server, reload, or active cache operation was performed. No proprietary assets were copied into the project. Only derived dimensions and resource paths are documented here; original game resources remain installed in the game.

## Source and selected parts

Installed archive: `content/levels/driver_training.zip` in the installed game.

The map's current `main/MissionGroup/StaticObjects/items.level.json` contains 11 turbine `Prefab` entries. Their prefab files use separate tower and rotor `TSStatic` objects. Phase 1 selects the slower, animated rotor variant 1.

| Item | Installed VFS path |
|---|---|
| Reference prefab | `/levels/driver_training/art/prefabs/turbine_blades1.prefab` |
| Tower/nacelle | `/levels/driver_training/art/shapes/objects/turbine_tower.dae` |
| Rotor | `/levels/driver_training/art/shapes/objects/turbine_blades1.dae` |
| Other rotor variants | Same directory, `turbine_blades2.dae` through `turbine_blades4.dae` |

Each shape has a packaged `.cdae` companion. The `.dae` names are the stock prefab's references and should remain the runtime references. The older monolithic `turbine.dae` also exists but is not used by these current prefab entries; it is smaller, has different materials, and has no embedded animation. Do not use it as a substitute for the current two-part model.

The current prefabs set tower position `[0,0,0]`, rotor position `[0,-3.52027345,101.410286]`, identity rotations, and unit scales. Both have `playAmbient=1`, `collisionType="Collision Mesh"`, and `meshCulling=0`. The tower has a legacy `isRenderEnabled=false` field; the new renderer deliberately leaves the tower visible, matching the existing project's solution for a similar stock pump-jack legacy field.

## Dimensions, origin, and orientation

Calculated from COLLADA position sources and the complete visual-scene node transforms, including `<unit meter="0.01">`, and Z-up. The root scale 100 and per-mesh scale 0.01 cancel. Source coordinates therefore convert from centimetres to metres. These are model measurements, not claimed Happy Jack real-world turbine measurements.

| Measurement | Metres |
|---|---:|
| Stock rotor hub above installation origin/grade | 101.410286 |
| Maximum rotor radius about local Y axis (highest LOD) | 34.579241 |
| Swept rotor diameter | 69.158482 |
| Maximum swept blade tip above installation origin/grade | 135.989527 |
| Tower/nacelle top | 102.872800 |
| Tower lowest visible vertex | -27.087250 |
| Tower/nacelle width along X | 3.504240 |
| Tower/nacelle span along Y | 7.615469 |
| Rotor thickness along Y | 3.435010 |

Tower top-LOD bounds: `[-1.75212,-1.797882,-27.08725]` to `[1.75212,5.817587,102.8728]` metres, relative to the stock installation origin. The lower cylindrical skirt extends underground in the stock installation convention. Place installation origin at sampled terrain grade; do not add 27.08725 metres. That would lift the model relative to its stock installation and change the documented hub height.

The rotor's pivot is at its local origin. Its plane is X/Z, shaft axis is local Y, and the rotor sits on the negative-Y side of the tower. The nacelle extends towards positive Y. Identity object rotation keeps the tower vertical; apply yaw only. Scale the entire model and rotor offset uniformly together.

Both selected shapes contain authored LOD meshes with suffixes `L1000`, `L500`, `L200` and a dedicated `Colmesh-1`. Tower visible triangle counts are 584, 294, and 204, plus 108 collision triangles. Rotor visible triangle counts are 2299, 1459, and 696, plus 696 collision triangles. Preserve the stock engine's LOD selection.

## Materials and texture dependencies

The tower's actual triangle material is `lod_vertcol`. The rotor uses `lod_vertcol` on all LODs and `m_plastic_trim` on 684 top-LOD triangles. The unused legacy `turbine` material from the map's `objects/main.materials.json` is unnecessary for the selected current shapes.

Load these installed material files before registering either TSStatic:

- `/art/shapes/common/common.materials.json`: `lod_vertcol`, pure vertex colors, no textures.
- `/art/shapes/garage_and_dealership/main.materials.json`: `m_plastic_trim`, PBR plastic with a breakup layer.

These definitions are in the game's `content/art_shapes.zip`. The plastic material references the following paths under `/assets/materials/trim/plastic/m_plastic_trim/`: `t_plastic_trim_01_ao.data.png`, `t_plastic_trim_01_b.color.png`, `t_plastic_trim_01_nm.normal.png`, `t_plastic_trim_01_o.data.png`, and `t_plastic_trim_01_r.data.png`. All corresponding `.dds` files exist in installed `content/assets/materials/trim.zip`; `.png` in the material is the engine's standard cooked-resource naming convention.

The breakup layer references `/assets/materials/breakup/t_breakup_general/t_breakup_general_b.color.png`, `t_breakup_general_o.data.png`, and `t_breakup_general_r.data.png`; their cooked `.dds` files exist in `content/assets/materials/breakup.zip`.

## Animation

`turbine_blades1.dae` embeds 301 sampled keyframes spanning 0–10 seconds, rotating 360 degrees around local Y. Channels animate its three authored LODs and collision node. The stock TSStatic `playAmbient=1` activates this engine sequence. Thus variant 1 is approximately 6 RPM, with no custom per-frame Lua animation. No shape-constructor script or separate animation asset exists beside these shapes.

Variant 2 completes one rotation in 8.333333 seconds (about 7.2 RPM); variant 3 in 6.666667 seconds (about 9 RPM). Variant 4 contains no animation. The default selected renderer uses variant 1. Actual rendering and looping remain an in-game verification item, because the active-session restriction rules out launching or modifying the game to test it now.

## Compatibility and runtime handling

The stock parts are normal TSStatic shapes at installed VFS paths, like other cross-level props already referenced by BeamNG Earth. They can be instantiated directly from another level without copying or redistributing any mesh/material/texture files. The new module checks both shape paths and both material files before creation, loads installed definitions, and creates one tower plus one rotor. If files are unavailable or creation fails, it logs a warning once and leaves no placeholder or partial assembly; a later tile delivery can retry.

The new module uses stable OSM IDs and tile ownership refcounts, moves the existing two objects for replacement records and origin rebases, keeps the tower upright, removes stale records on empty tile deliveries, and deletes the assembly after its last owner is removed. Its collision callback lets the existing batched collision rebuild include the stock dedicated collision meshes. Native ambient animation requires no update tick.

Offline LuaJIT test `mod/tests/test_windturbines.lua`: **59 assertions passed, 0 failed** on 2026-11-01. This checks assembly geometry, scaling/yaw, scene ID preservation, duplicate suppression, multiple owners, empty replacement/removal, terrain elevation propagation, reload stability, rebases, missing optional metadata, malformed records, asset/material absence, material errors, partial-creation cleanup, and recovery. Stubbed engine behavior does not prove material appearance, collision, native LOD distance, or ambient playback in the actual game.
