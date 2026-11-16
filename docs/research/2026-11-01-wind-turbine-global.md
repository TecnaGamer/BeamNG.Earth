# Worldwide mapped wind turbines

The user verified the Phase 1 Happy Jack turbine in-game and authorized global generation on 2026-11-01. The implementation now enables **473,629 individually mapped OSM turbines**, including all 14 Happy Jack turbines at their verified coordinates. The current game and world server were left running; no live reload or active-cache deletion was performed.

## Data and geographic coverage

The bundled `server/wind_turbines.npz` contains **472,270 nodes and 1,359 suitable individual ways** from the OSM snapshot **2026-10-31T21:23:06Z**. The compact file is **7,456,718 bytes**. No turbine-data download or Overpass request is made during gameplay.

Source: the worldwide `power_generator.parquet` layer published by [GMWID / Geomermaids](https://gmwid.geomermaids.com/), [release catalog](https://parquetry.geomermaids.com/gmwid/catalog/power/power_generator/collection.json), version `2026-10-31`, updated `2026-11-01T17:51:55Z`. GMWID preserves original OSM rows alongside explicitly identified registry additions. The builder reads only `origin='osm'`, `lifecycle='active'`, raw OSM IDs/tags, and WGS84 geometry. It excludes registry additions and derived administrative columns. The source is a distribution of OSM/Geofabrik data under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/); attribution is **© OpenStreetMap contributors**. See [source attribution](https://parquetry.geomermaids.com/gmwid/ATTRIBUTION.txt) and `server/wind_turbines.provenance.json`.

The full developer input was 323,216,982 bytes / 6,834,691 generator rows. It produced 474,194 active OSM wind candidates before the shared validity and duplicate checks. The raw input remains in ignored developer scratch storage; only the compact OSM-derived index and provenance are shipped. Provenance records source/index hashes, exact byte counts, release timestamps, filters, coordinate conventions, dimensions, and output counts.

Global index SHA256:

```text
f325ec7bc8ee841da15eaf34cd1b62403b9ad3d0a2326f1dd8d13c497000a3a3
```

Detection requires an explicitly tagged operating wind generator. A plant/site/farm polygon does not create a turbine or an inferred array. Node coordinates remain the exact source float64 values. A way must represent one small, closed, simple footprint (no holes/multiple outlines, no open line, no self-crossing ring, maximum footprint span 100 m); its placement is the area centroid of that mapped footprint. Unsupported geometry is omitted rather than assigned an arbitrary position. Coverage remains limited by individually mapped OSM data and the existing geotile grid.

Repeated type/ID records retain the newest version where supplied. A node inside its turbine footprint takes priority over the duplicate way. Exactly coincident OSM positions retain one deterministic identity, preferring a node, then newest version, then lowest numeric ID. Twenty-two redundant node pairs were removed from this export. Distinct nearby coordinates are not rounded or merged. The GMWID source does not provide OSM edit versions; stable element type/ID is preserved.

Regression checks confirm coverage in North America, South America, Europe, Africa, Asia, and Oceania. All 14 turbine nodes from [Happy Jack relation 3792596](https://www.openstreetmap.org/relation/3792596) match the previously retrieved OSM API coordinates exactly and appear once each. The original test node remains at **41.1397814, -104.9978882**.

## Placement, scale, and installed resources

`server/windturbines.py` binary-searches the latitude-sorted index and selects longitude only in the local window. It materializes local records, rather than hundreds of thousands of Python turbine objects. Longitude windows and duplicate footprints handle the dateline. Legacy tile bounding windows use the exact world-coordinate inverse; both legacy and geographic tile ownership use the existing half-open ownership rules.

Ground sampling and world conversion are batched. Records retain `osm:node:<id>` / `osm:way:<id>`, exact geographic position, local XYZ, yaw, scale, and dimension basis. Missing/non-finite heights are not replaced by guessed altitudes, and incomplete far cells retry instead of caching a partial farm. Towers remain upright with yaw only. Nearby road owners use existing graded terrain height; far owners use the normal DEM with the existing airport/sea ground rules. Near placement has priority while both owners exist.

The ETK assembly is the user-tested Phase 1 asset:

- `/levels/driver_training/art/shapes/objects/turbine_tower.dae`
- `/levels/driver_training/art/shapes/objects/turbine_blades1.dae`

The installed materials, cooked textures, stock rotor offset, buried lower skirt, and installation origin are unchanged. No proprietary resources are copied into the project. The [asset audit](2026-11-01-etk-wind-turbine-assets.md) records actual paths and model dimensions. The same stock shape is a visual stand-in for the various real manufacturers and turbine types in the index.

Valid OSM dimensions select one uniform scale, prioritizing `height:hub`, then `rotor:diameter`, then `height`. Explicit metres/feet are supported. Small domestic turbines with usable dimensions can scale below 0.1; the sanity range is 0.005 through 5.0 times stock scale. The export retains 38,889 total-height values, 35,516 hub-height values, and 54,514 rotor-diameter values; a turbine can have more than one. Missing or unusable dimensions use the measured stock defaults: hub **101.410286 m**, rotor **69.158482 m**, swept tip **135.989527 m**. These defaults are not asserted real-world measurements. Independent tower/blade proportions are not invented.

## Visibility, animation, and farm performance

The existing `vegetation.Tracker` serves turbine landmarks in zoom-15 geographic cells, with **4 km send / 5 km drop** hysteresis and at most **two builds in flight**. Distances are measured to tile bounds, so delivery/removal is not an exact radial cutoff at each tower. The road and building tracker radii are unchanged. Empty turbine cells perform no DEM sampling; populated cells sample all mapped points in one batch. Session files are memoized for stable resends and bounded in memory.

The client retains each far cell's source frame and maps it into the active anchor. Same-session deliveries can cross a pending rebase; old server sessions cannot acquire new ownership. Teleports/rebases remap retained content, and drops/reset release their owners.

Far owners are render-only. Near road owners enable dedicated stock collision meshes on the **same** tower/rotor objects. One registry keyed by OSM identity prevents duplicate near/far assemblies; a near owner wins placement/collision priority until removed. Repeated deliveries preserve scene IDs. A turbine is deleted when its last owner goes away. Far-only creation, movement, and removal do not trigger collision rebuilds.

Resource existence/material imports are cached after one successful check per session. A failed installation retries once per delivery, avoiding hundreds of identical failed loads in a dense farm. The stationary tower uses static rendering optimization; the rotor uses the stock native ambient animation, approximately one revolution per 10 seconds. No per-turbine Lua update loop was introduced. Stock automatic LOD is retained; neither global detail preferences nor a forced high-detail mesh is used. This follows the [BeamNG TSStatic documentation](https://documentation.beamng.com/modding/levels/level_classes/tsstatic/).

The **loaded** 4 km reach is not a guarantee that every blade remains visually resolved at 4 km. Screen size, FOV, resolution, graphics settings, terrain occlusion, and the stock shape's lowest LOD control actual visibility. Distant appearance and frame time with a full farm remain in-game verification items.

Executed offline scale checks use **300 turbines / 600 scene objects**, covering repeated deliveries, near/far handoff, collision transitions, rebases, cleanup, and recovery from unavailable assets. They require only four resource existence checks and two material imports for a healthy session; repeated identical deliveries cause no object movement or recreation. These stub checks are not GPU/FPS measurements.

One fresh Python index load on this machine took approximately **78 ms**. The seven uncompressed arrays occupy **17,524,273 bytes** per process. A repeated Happy Jack window lookup averaged approximately **0.035 ms** over 1,000 calls, returning 14 records. These are local CPU measurements with the filesystem already available, not game performance claims. Evidence: `build/turbine-phase2-index-check.json`.

## Validation

### Standalone pull-request check

The turbine patch was transplanted onto upstream commit `8b0e9b04592fdc27c16a85aeaa055aa3dda58e10`, without the separate grass PR or unrelated local road fixes. On that isolated tree, **369 Python cases across 26 selected modules: 363 passed, 6 skipped, zero failures/errors**. All 49 turbine/import/streaming/packaging cases passed. The skips are four opt-in live-cache cases, one standalone LuaJIT-CLI agreement case, and one POSIX FIFO case on Windows. Protocol agreement schema checks passed.

The standalone tree also passed **1,081 Lua assertions in 15 suites**, with no failures. This includes renderer97, farstream23, roadstream16, core281, robustness64, roads95, roadgeo38, pumpjacks10, gasstations64, rebase33, surfacelayers52, graphics142, terrainswap29, terrainpatch28, and trees109. Project imports were checked to resolve within the isolated checkout. The requested grass-specific suite is absent from upstream and is not part of this turbine PR. The road timing suite uses the same isolated high-resolution clock substitution described below.

Only the compact ODbL index/provenance and turbine code/docs are included. The 323 MB developer export, local logs/runtime, proprietary game assets, and existing grass/road changes are excluded. These clean-tree checks are separate from the earlier, broader working-tree results below.

### Earlier working-tree checks

Python final results: **494 distinct cases across 32 modules: 487 passed, 7 skipped**, including an isolated successful rerun of one initially failing existing server case. The seven skips are four opt-in live-map-cache cases, two agreement cases requiring a standalone LuaJIT executable (Lupa's LuaJIT was used for the client suites), and one POSIX FIFO case on Windows.

LuaJIT via Lupa: **16 suites, 1,170 assertions passed, zero failed**. Suites: turbine renderer 97, far turbine stream 23, road-to-turbine stream 16, road geography 38, roads 95, pump jacks 10, gas stations 64, core stubbed 288, core robustness 64, rebase 33, ground-cover environment 65, surface layers 69, graphics 142, terrain swap 29, terrain patch 28, and trees 109.

New regressions cover worldwide IDs/positions, all 14 verified Happy Jack nodes, individual-way centroids and invalid areas, lifecycle/farm rejection, coincident mappings, dateline behavior, exact legacy/geotile ownership, OSM dimensions including small turbines, 300-record batched ground sampling, missing-height retries, index integrity/provenance, package inclusion, far-tracker bounds/hysteresis, source-frame placement, old-session rejection, and near/far ownership/collision. Existing road, bridge, banking, sidewalk, vegetation, terrain, server, and protocol checks are included.

The initial protocol agreement run identified the new optional capability missing from its checked schema. The agreement table now documents both directions of `turbineGeo` and `turbines_tile` / `turbines_drop`; the final run uses that schema. As in Phase 1, the Windows road timing suite substitutes a high-resolution test-process clock for the millisecond LuaJIT clock, which also fails the unchanged baseline timing assertion. Production timing code is unchanged. Live-map-cache tests are explicitly disabled for isolated checks, leaving active data untouched.

The broad server run completed all 48 cases in 389.5 seconds, exceeding the runner's too-short 300-second budget, and had one failure at `test_server.py:791` in `MeshServer64Test.test_session`. That test thread can see the new focus before the server finishes replacing its desired terrain set, misclassifying a real change as an unchanged hysteresis step. The test client does not enable turbine streaming; that focus/selection ordering was not changed. The same current-code case **passed in isolation in 71.3 seconds**; the previous server source also passed the isolated case in **67.1 seconds**. The intermittent broad-run failure did not reproduce in either isolated run. The other 47 server cases passed in the broad run. The totals above count each final case once and retain the initial failure/rerun detail in the summary JSON. No production terrain behavior or existing test assertion was weakened to obtain a pass.

Evidence: `build/turbine-phase2-test-logs/`, `build/turbine-phase2-test-summary.json`, `build/turbine-phase2-lua.json`, and `build/turbine-phase2-lua.log`. Packaging checks inspect both source-release collection and the frozen-app data command; no release executable was built or published.

## Files and update procedure

Production changes: `server/windturbines.py`, `server/earthd.py`, `server/roads.py` (comment), `tools/build_app.py`, client `core.lua`, `windturbines.lua`, and new `turbinestream.lua`. New data: `server/wind_turbines.npz` and `wind_turbines.provenance.json`. The existing block-worker and road serialization hooks from Phase 1 already carry global near records.

New/updated tests: `server/tests/test_windturbines.py`, `test_windturbines_global.py`, `test_build_windturbines.py`, `test_windturbine_farstream.py`, `test_windturbine_packaging.py`, `test_protocol_agreement.py`, and client `test_windturbines.lua`, `test_turbinestream.lua`, `test_core_stubbed.lua`. Documentation/attribution: README, CHANGELOG, CREDITS, SOURCES, server README, this report, and the historical Phase 1 report. No grass selection/density, road geometry/classification, bridge geometry, banking, sidewalk generation, or terrain-generation algorithm was changed.

To refresh the data as a developer, run `python tools/build_windturbines.py --download` in a working development environment. DuckDB and NumPy are required for the developer import; the runtime requires no new package. The tool downloads the full source and release catalog into ignored scratch storage, validates source size/row count/license, rejects incomplete or unexpectedly small exports, and writes the compact index/provenance. Replace `windturbines.INDEX_SHA256` with the emitted SHA before running the geographic/packaging regressions. Updating that Python source pin also updates the existing revisit cache fingerprint.

## User-controlled in-game verification

When finished with the current session, restart the Python world server from this updated folder, install the updated client mod through the usual working launcher, and reopen/reload BeamNG.drive. An old packaged executable must be rebuilt to include this code/index. **No manual imagery, DEM, map-data, grass, or revisit cache deletion is needed.** The source fingerprint handles generated content normally.

Return to [Happy Jack](https://www.openstreetmap.org/?mlat=41.1397814&mlon=-104.9978882#map=16/41.1397814/-104.9978882). All 14 mapped turbines should now appear as their cells finish streaming. Check that they render once each, stay upright, and use the already tested slow rotor animation. From nearby, confirm terrain contact and collision; drive several kilometres out and return, then repeat across a normal origin rebase or a user-controlled reconnect. Check distant visibility and frame time with the full farm, because those require the actual renderer. The same rules apply when visiting any individually mapped OSM turbine worldwide.

Optional GE Lua diagnostics:

```lua
dump(require('beamngEarth/turbinestream').status())
dump(require('beamngEarth/windturbines').status())
```

The stream reports retained geographic cells. The renderer reports turbine/object/owner counts, unavailable resources, colliding turbines, and records keyed by exact OSM identity. Healthy loaded assemblies have two objects per turbine; only nearby road-owned turbines should report collision. World XYZ changes with the anchor, while latitude/longitude and mapped location remain stable.
