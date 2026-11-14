# Regional scenery integrated with official 1.8.0-beta

This is a real two-parent merge of PR #505's `europe-infrastructure` head
`d74ad20f3528b604c8190fb721036f8bc89e437b` and official `main` at
`239e5f5d73d7914123303fa2b9dc91b5c2bf6c57` (1.8.0-beta). Both histories are
retained. The ten conflicting files are resolved; this is not a file overlay.

## Integration decisions

- Keep the European profiles, country polygons, French road markings, mapped
  poles, regional houses and the latest Alsace mix. The existing regional model
  files, geographic archives and `francehouses.py` are byte-identical to the PR
  head above. No registration-plate feature is included.
- Keep official dirt/gravel strips, mountain-pass roads and their hairpin
  widening. Road specs carry both `mountain` and `france`. The French scenery
  does not override a mountain pass's width or restore its centre line. The
  French road revision becomes `fr-roads-signs-v4`.
- Advance `roads.FACTS_FORMAT` from 2 to 3, with a new facts golden digest.
  Baked packs and retained grading plans use this version too; older generated
  facts/plans must be rebuilt. The downloaded imagery/elevation cache is retained.
- Keep official custom 3D models, landmarks, ships, fences, picnic tables and
  shelters alongside the regional scenery. Buildings carry both `ship` and
  `fr_house` fields, and their material registry includes both new families.
- Prebuilding and live tiles use the same profile and local-model signature in
  their cache keys. Keep the official `areas_scratch` location and texture VFS
  prefix. A regression test bakes a local STL and a regional sign, restores both
  in another session and checks retention after an origin change.
- Keep the official revisit-sidecar implementation: it closes descriptors under
  the process lock, including after a failed write, and reopens correctly after
  cache-directory replacement. This also preserves the earlier Windows fix.
- Keep the official sparse triangle bounds when draping road meshes and retain
  the 2 mm approximation tolerance for French paint. Lua loads both the dirt-road
  and French-house materials and both landmark and French emissive materials.

Native source and binaries match the official 1.8.0 commit. No new runtime
dependency is introduced by the regional integration. Feature defaults remain
opt-in: `--regional-poles europe`, `BNE_FR_ROADS=on`, `BNE_FR_HOUSES=on`.

## Validation

Linux, Python 3.12.14, official earthfast VERSION 144:

- 70 selected Python modules: 1,362 tests run, 1,347 passed, 13 skipped, 2 failed.
  All selected regional scenery and directly affected 1.8 integration modules
  passed. The two failures are `test_launcher_linux.CaCerts.test_system_bundle`
  and `.test_certifi_fallback`; both reproduce on a separate untouched checkout
  of the official commit above. That launcher and its tests are unchanged here.
  This host has `/usr/lib/ssl/cert.pem`, which those fallback tests assume absent.
- Skips comprise two real-map/Baltimore cases, one external 3dmr model, one
  standalone LuaJIT executable check, one optional protocol case, three process
  lifecycle cases, one cache-sharing case and four distributed-worker cases.
- All 82 offline Lua modules passed with Lupa 2.8 (LuaJIT 2.1 for client tests,
  Lua 5.3 for the BeamMP server test). Both Node mapmath test files passed.
- Additional merge regressions cover French mountain-pass widths/paint/hairpins,
  serialization of both spec flags, prebuilt local models plus regional signs,
  session path replacement, rebasing and cache invalidation for both profiles
  and local-model edits.

These are targeted automated checks. The full upstream suite and complete
quality/package gate were not run. Windows and an actual BeamNG.drive session
have not been tested for this merge; earlier manual testing concerned 1.7.3.

Reproduce the main compatibility checks after `python install.py`:

```powershell
$env:BNGE_ROADGEO_REAL = '0'
.\.venv\Scripts\python.exe .\tools\run_tests.py test_europe_180_merge test_france_houses test_france_roads test_mapped_poles test_regional_geotiles test_buildings test_roads test_roadmesh test_roadcorners test_regional_poles test_countrybounds test_regional_furniture test_regional_revision test_europe_infrastructure test_delineator_density test_furniture test_navgraph test_sidewalk_collision test_blockworker test_revisit test_roadgeo test_geotiles test_areas test_signready test_markings test_road_classification test_landmarks test_ships test_threedmr test_bake_formats --par 2 --logs build/test-logs-pr505-180
Remove-Item Env:BNGE_ROADGEO_REAL
```

`python tools/run_lua_tests.py` requires the optional development dependency
`lupa==2.8`; it is not required by players. The 70-module log set and exact
results accompany the supplied merge bundle.

## Install and try

Close BeamNG.drive and the previous server before updating. On the merged
checkout, `py -3.12 install.py` refreshes the source setup and installed mod.
Launch `tester_france.bat`, or use:

```powershell
$env:BNE_FR_ROADS = 'on'
$env:BNE_FR_HOUSES = 'on'
$env:BNE_FR_HOUSES_INFER = 'on'
py -3.12 .\start.py -- --regional-poles europe
```

For a stock comparison set both `BNE_FR_ROADS=off` and `BNE_FR_HOUSES=off` and
use `--regional-poles off`. Check a French village, Alsace's house mix, rural
German delineators, a country border and a prebuilt-area revisit/origin change.
