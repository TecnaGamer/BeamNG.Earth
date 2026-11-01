# Installed grass asset audit

Verified against BeamNG.drive Steam build **26298565** (app 284160). All five maps below were available under `content/levels/<map>.zip`. Source definitions/materials were inspected directly and foliage textures were decoded for visual inspection. No game meshes or textures are included in this repository.

## GroundCover sources

The paths below are game VFS paths prefixed with `/levels/`.

| Map | GroundCover definitions | Selected objects | Material JSON |
| --- | --- | --- | --- |
| Hirochi Raceway | `hirochi_raceway/main/MissionGroup/vegetation/terrain_grass/items.level.json` | `small_grass_close`, `long_grass_distant` | `hirochi_raceway/art/shapes/groundcover/main.materials.json` |
| West Coast USA | `west_coast_usa/main/MissionGroup/vegetation/items.level.json` | `grass_dry_1`, `shrubs_near`; lawn fallback `mown_grass_close` | `west_coast_usa/art/groundcover/main.materials.json` |
| Small Island USA | `small_island/main/MissionGroup/Level_objects/vegetation/items.level.json` | `small_grass_close`; fallback `medium_grass_close` | `small_island/art/shapes/groundcover/main.materials.json` |
| Johnson Valley | `johnson_valley/main/MissionGroup/level_object/vegetation/items.level.json` | `Grass` | `johnson_valley/art/shapes/groundcover/main.materials.json` |
| Italy | `italy/main/MissionGroup/Level_objects/vegetation/terrain_Grass/items.level.json` | `small_grass_green_close`, `long_grass_green_distant` | `italy/art/shapes/groundcover/main.materials.json` |

The selected definitions use billboard GroundCover compatible with the existing CPU placement, surface masks, density and streaming system. Johnson Valley's mesh-based `dirt_grass_dry` definitions are unused; its `Grass` billboard supplies dry tufts without new mesh dependencies.

## Colors and dependencies

Texture paths below start with `/assets/materials/foliage/`. The material JSON declares `.png`; the inspected installation stores corresponding compiled `.dds` files in `content/assets/materials/foliage.zip`. The loader checks both forms.

| Material/source | Base color path | Inspected appearance |
| --- | --- | --- |
| `m_grass_dry_short`, Small Island | `grass/t_grass_dry_short_01/t_grass_dry_short_01_b.color.png` | Golden/tan blades |
| `m_grass_dry_01`, West Coast/Small Island | `grass/t_grass_dry_long_01/t_grass_dry_long_01_b.color.png` | Pale golden/tan blades |
| `m_jv_dry_grass`, Johnson Valley | `grass/t_jv_dry_grass/t_jv_dry_grass_b.color.png` | Beige/gray dry tufts and scrub |
| `m_grass_green_short_01`, Italy | `grass/t_grass_green_short_02/t_grass_green_short_02_b.color.png` | Muted yellow-green short grass |
| `m_grass_green_long_01`, Italy | `grass/t_grass_green_long_03/t_grass_green_long_03_b.color.png` | Lighter yellow-green blades |
| `m_grass_green_short_01`, Hirochi | Same short texture as Italy; stock factor `0.881529987 0.894159973 0.825331986 1` | Darker short grass |
| `m_grass_green_long_01`, Hirochi | `grass/t_grass_green_long_01/t_grass_green_long_01_b.color.png` | Dark, rich green blades |

The selected materials' normal, opacity, roughness and ambient-occlusion maps were also verified:

- Short grasses reuse `grass/t_grass_green_short_01/t_grass_green_short_01_{nm.normal,o.data,r.data,ao.data}.png`.
- Long grasses reuse `grass/t_grass_green_long_01/t_grass_green_long_01_{nm.normal,o.data,r.data,ao.data}.png`.
- Johnson Valley uses `grass/t_jv_dry_grass/t_jv_dry_grass_{nm.normal,o.data,r.data,ao.data}.png`.
- West Coast shrubs use material `wca_groundcover` and `groundcover/t_wca_groundcover/t_wca_groundcover_{b.color,nm.normal,o.data,r.data,ao.data}.png`.

## Registration, fallbacks and quantity

`groundcover.lua` reads the installed GroundCover and material JSON through `readFile`/`jsonDecode`. It creates private materials named `bngEarthGC_<source>_<stock-material>`, preserving scalar/vector fields and indexed stages, and registers them before creating GroundCover. Relative texture paths retain the source material directory. Original game files remain unchanged, and no conversion is needed.

Isolation is necessary because Italy and Hirochi both name their long material `m_grass_green_long_01` while referencing different textures. Their short materials also share names with different tint/alpha settings. Loading both entire stock material files globally could replace the appearance of existing grass.

Missing definitions or dependencies fall back independently. Dry short grass prefers Small Island, then West Coast, then Hirochi; dry tufts prefer Johnson Valley, then West Coast, then Hirochi. Light grass prefers Italy, then Hirochi, then West Coast; lush grass prefers Hirochi, then Italy, then West Coast. Shrubs prefer West Coast, then Johnson Valley, Small Island or Hirochi. Lawn fallbacks use short definitions and exclude tall types. If all alternatives fail, the definition is skipped with a diagnostic in `groundcover.status()`.

Providers' stock populations differ, so per-role counts, radii, fades and grid sizes retain the existing BeamNG.Earth budgets. Rural palettes each retain 121,940 elements, scrub palettes 58,080 and lawns 30,600 before density/distance multipliers. Species retain their stock UVs, sizes and clumps. Four static terrain-material variants duplicate the original grass/dusty physics and detail appearance, avoiding expensive runtime terrain-material allocation.

The client resolver was exercised with actual installed JSON and a VFS archive index: all 19 preferred definitions resolved without fallback or missing dependencies. The reporter subsequently confirmed successful in-game testing across multiple environments. Engine GPU-memory and frame-time effects remain unmeasured; the definition count increases from 7 to 19.
