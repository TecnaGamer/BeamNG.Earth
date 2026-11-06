# Happy Jack wind turbine: Phase 1 implementation

The user confirmed this turbine worked in-game, then authorized global generation. See the
[Phase 2 report](2026-11-01-wind-turbine-global.md) for the subsequent implementation. The report below records the Phase 1 checks.

Implemented on 2026-11-01. One verified turbine is enabled for the user's in-game check. No full-farm generation is enabled. No running server was restarted, no game reload was requested, and no proprietary game assets were copied into the project. Actual rendering, terrain contact, collision, LOD visibility, and ambient-animation playback still require the user's game session to verify.

**Test turbine location: 41.1397814, -104.9978882**

[OpenStreetMap centered on the selected turbine](https://www.openstreetmap.org/?mlat=41.1397814&mlon=-104.9978882#map=18/41.1397814/-104.9978882).

## Verified geographic source

The selected feature is **OSM node 1175681635**, version 4, at Happy Jack Windpower west of Cheyenne, Wyoming. It is an explicit member of [site relation 3792596](https://www.openstreetmap.org/relation/3792596). The relation's other turbines are not enabled. The exact node is about 11.3 metres from the request's approximate farm reference, which is not used for spawning.

| Field | Verified value |
|---|---|
| Latitude | `41.1397814` |
| Longitude | `-104.9978882` |
| Element type / ID | `node / 1175681635` |
| Stable streamed ID | `osm:node:1175681635` |
| OSM node timestamp | `2023-02-27T03:50:35Z` |
| Retrieval time, UTC | `2026-11-01T22:36:24.963169+00:00` |

Sources: [OSM feature](https://www.openstreetmap.org/node/1175681635), [node API](https://api.openstreetmap.org/api/0.6/node/1175681635.json), and [site relation with members](https://api.openstreetmap.org/api/0.6/relation/3792596/full.json). These were retrieved and checked against the bundled one-node snapshot, `server/wind_turbine_phase1.json`. The snapshot omits editor username/user ID and retains the feature coordinates, version, timestamp, changeset, and tags. Attribution: [OpenStreetMap contributors, ODbL 1.0](https://www.openstreetmap.org/copyright).

The node's tags are:

```text
power=generator
generator:source=wind
generator:method=wind_turbine
generator:output:electricity=2.1 MW
manufacturer=Suzlon
manufacturer:type=S88-2.1 MW
operator=Duke Energy
operator:wikidata=Q1264404
source=usgs
```

The node has no `height`, `height:hub`, or `rotor:diameter`. No real-world dimensions are inferred from the manufacturer's model name. The ETK asset is a documented visual stand-in, not a claim that Happy Jack has this exact model geometry.

## Installed asset, scale, and animation

The installed ETK Driver Experience Centre is `content/levels/driver_training.zip`. Its current turbine prefab uses two separate TSStatic objects:

| Part | Installed game VFS path |
|---|---|
| Tower and nacelle | `/levels/driver_training/art/shapes/objects/turbine_tower.dae` |
| Animated rotor | `/levels/driver_training/art/shapes/objects/turbine_blades1.dae` |
| Reference prefab | `/levels/driver_training/art/prefabs/turbine_blades1.prefab` |

The renderer references those installed resources. No mesh, material, texture, prefab, or animation file is redistributed. Both shapes contain stock LODs and dedicated collision meshes. The full [installed asset audit](2026-11-01-etk-wind-turbine-assets.md) documents measured bounds, orientation, textures, archive dependencies, and other rotor variants.

Actual mesh materials are `lod_vertcol` and `m_plastic_trim`. The renderer loads `/art/shapes/common/common.materials.json` and `/art/shapes/garage_and_dealership/main.materials.json` from the installed `content/art_shapes.zip`. The plastic trim and breakup textures resolve to installed cooked DDS files in `content/assets/materials/trim.zip` and `content/assets/materials/breakup.zip`.

With no OSM dimensions, this node uses uniform stock scale **1.0**. Measured model defaults are **101.410286 m hub height**, **69.158482 m rotor diameter**, and **135.989527 m maximum swept tip height**, relative to the installation origin. The rotor offset from that origin is `(0, -3.52027345, 101.410286)` metres. These are asset dimensions, not measured Happy Jack dimensions.

The stock tower has an extended lower skirt down to `-27.08725 m`. As in its source prefab, its installation origin is placed at terrain grade; the skirt is buried. Raising the mesh by that skirt depth would incorrectly lift the above-ground tower. The tower remains upright with yaw only, even on slopes. The selected node uses the stock heading because OSM supplies no heading.

If optional dimension tags are supplied later for this selected node, scale is uniform: valid `height:hub` takes priority, then `rotor:diameter`, then overall `height`. Metres and explicit feet are parsed; invalid values keep the defaults. Independent tower/blade proportions are not fabricated. The record carries the scale and its source for diagnostics.

The selected rotor has an embedded 10-second full revolution, approximately **6 RPM**. The renderer sets `playAmbient=1` on the rotor and uses the game's ambient animation; no per-frame Lua animation is added. Inspection and offline tests establish that the sequence exists and the flag is set. In-game playback remains to be checked.

## Placement and existing streaming

`server/windturbines.py` reads a pinned, bundled OSM snapshot and allowlists only `osm:node:1175681635`. No online OSM request is needed during play. Wind-generator detection rejects farm polygons, non-node elements, conflicting generator sources, invalid coordinates, and inactive/lifecycle tags. Duplicate selected records retain the newest version.

The server uses the existing coordinate conversion and half-open road-tile ownership. It samples the existing graded terrain elevation at the exact node latitude/longitude. The horizontal coordinates follow the project's height-zero convention, and vertical placement follows the sampled ground. Missing or non-finite terrain elevation omits the turbine and marks the build incomplete rather than caching a guessed altitude.

The one record travels in the existing road-object JSON stream for legacy tiles and geographic tiles, including the thread fallback. It is generated independently of the buildings toggle. Geographic content hashes include the turbine record. The existing client tile transform maps the installation point and yaw into the current anchor.

The client keeps a registry by stable OSM ID and tracks tile owners. Repeated delivery and overlapping old/new tile ownership produce one assembly: one tower and one rotor. Replacements and origin rebases reposition existing objects. Empty replacement content and the last tile-owner removal clean up both objects. The existing collision rebuild callback is used. Scene-object IDs are preserved while a loaded assembly is rebased.

Visibility uses the existing road tracker: normally a tile is sent when its bounds come within **640 m**, and dropped beyond **960 m**. Tile size and the existing fast-driving look-ahead can affect when a particular delivery occurs. Stock shape LODs remain active. No new long-distance landmark system or full-farm tracker was added; practical visibility must be checked in-game.

Missing shape/material files or material-load/creation failures skip the complete assembly, issue a warning, and leave no partial object or invisible placeholder. A later tile delivery can retry. Diagnostics retain geographic identity and report whether the assembly loaded.

## Source changes

| Files | Purpose |
|---|---|
| `server/windturbines.py`, `server/wind_turbine_phase1.json` | Selected OSM feature, dimensions, terrain sampling, ownership, and stable records |
| `server/blockworker.py`, `server/earthd.py`, `server/roads.py` | Add the record to existing road-object generation and serialization |
| `mod/beamng_earth/lua/ge/extensions/beamngEarth/windturbines.lua` | Installed-resource loading, two-part assembly, ownership, cleanup, rebase, diagnostics |
| `mod/beamng_earth/lua/ge/extensions/beamngEarth/roads.lua`, `core.lua` | Existing tile delivery, drop, transform, reset, and collision integration |
| `tools/build_app.py` | Include the one-node snapshot in packaged server builds |
| `server/tests/test_windturbines.py`, `test_windturbine_streaming.py` | Geographic, detection, dimensions, terrain, ownership, reload, and payload tests |
| `mod/tests/test_windturbines.lua`, `test_windturbine_streaming.lua`, `test_roads.lua`, `test_roadgeo.lua` | Assembly, resource failures, transform, duplicate, streaming, and cleanup checks |
| `CREDITS.md`, this report, the asset audit | OSM attribution and implementation/testing documentation |

## Executed checks

Python: **320 tests collected in 22 modules; 316 passed, 4 skipped, zero final failures/errors**. This includes all 15 new turbine tests and both new serialization/content-identity tests. The selected existing suites cover block workers, coordinate/geotile placement, roads and road meshes, rural classification, I-80 banking, bridge profiles and clearance, sidewalks, grass classification/exclusions/layers/lifecycle, surfaces, vegetation, graphics, installation, and packaging.

The four skipped Python cases were two opt-in real-map-cache tests, one LuaJIT-CLI agreement case (no standalone CLI; LuaJIT was available through Lupa), and one POSIX FIFO installer case on Windows. Initially, the real-map-cache cases attempted to populate missing data in the protected active cache and encountered permission errors. They were rerun with the suite's documented `BNGE_ROADGEO_REAL=0` opt-out; no escalation or cache clearing was performed. The final results above include those two skips.

Lua: **15 suites, 1,108 assertions passed, zero failed**, executed with Lupa's LuaJIT 2.1. This includes turbine renderer 59, real road-to-turbine streaming 16, road geography 38, roads 95, pump jacks 10, gas stations 64, core stubbed 287, core robustness 64, rebase 33, ground-cover environment 65, surface layers 69, graphics 142, terrain swap 29, terrain patch 28, and trees 109. The final renderer/streaming rerun includes the new identity diagnostic.

The existing road collision timing test fails with Windows LuaJIT's millisecond-resolution `os.clock` even on the unchanged baseline. That suite was executed with Python's high-resolution `time.perf_counter` substituted for `os.clock` in its isolated test process; all 95 assertions passed. Production timing code was not changed.

Changed Python sources compiled successfully. Source-package collection includes the new Python module, OSM snapshot, and client renderer; the packaged-app data list includes the snapshot. The installed shape/material/texture dependencies were inspected read-only. Tests use temporary files and stubbed game objects; they do not establish actual game rendering, native animation playback, collision response, or frame-time cost.

Local test evidence: `build/turbine-phase1-test-summary.json`, `build/turbine-phase1-test-logs/`, `build/turbine-phase1-lua.json`, and `build/turbine-phase1-lua.log`.

## User-controlled restart and in-game check

It is ready for a restart and test whenever the current play session is finished. Nothing was restarted automatically.

1. **Restart the Python world server** so it loads the new generation code. Start from this updated source folder using the normal launcher. `run_server.bat` calls `start.py`, which updates the installed mod by default; do not use `--keep-mod` for this update. An older packaged executable will not contain these changes until rebuilt.
2. **Reload BeamNG.drive with the updated mod.** Closing the game before the normal launcher updates the mod and reopening it afterward is the clearest way to ensure both Lua modules and installed resources load together.
3. **No manual cache invalidation is needed.** The existing revisit cache fingerprints server Python source, which changed. The selected snapshot is pinned by a hash in that source, including CRLF-safe validation. New-session road content contains the turbine. Do not delete imagery, DEM, map-data, or grass caches.
4. Navigate to **`41.1397814, -104.9978882`**. Use the [centered OSM map](https://www.openstreetmap.org/?mlat=41.1397814&mlon=-104.9978882#map=18/41.1397814/-104.9978882) to locate the mapped point. Approach within a few hundred metres and allow normal road/terrain streaming to finish.
5. Expect **one upright tower and nacelle with one three-blade rotor**, roughly 101.4 m hub height using the ETK defaults. The blades should rotate slowly, about one revolution in ten seconds. Other mapped turbines in the farm are intentionally absent in Phase 1.
6. Inspect the base from nearby and from the side: it should meet the terrain without an exposed floating gap, keep its tower vertical on a slope, and place the rotor at the nacelle. The stock lower skirt is intentionally underground. Confirm materials are visible and not missing-texture orange. Compare the base with the exact OSM node, rather than the approximate farm marker.
7. Drive far enough away for the containing road tile to unload (over roughly 1.3 km is a useful margin), then return. Confirm one tower/rotor assembly returns to the same terrain point. Also travel far enough to trigger a normal origin rebase and revisit; the turbine should remain upright and at the same mapped location. A user-initiated level reload/reconnect should likewise restore a single assembly.

Optional GE Lua console diagnostic, while near the turbine:

```lua
dump(require('beamngEarth/windturbines').status())
```

Expect `turbines=1`, `objects=2`, `unavailable=0`, with `items['osm:node:1175681635']` showing the exact latitude/longitude, `loaded=true`, and `scale=1`. World XYZ changes during an origin rebase; the geographic identity and actual terrain location should remain stable. If the installed resources cannot load, the console log includes a `wind turbine:` warning naming the missing resource or failure.

Phase 2 is waiting for the user's in-game verification and separate approval.
