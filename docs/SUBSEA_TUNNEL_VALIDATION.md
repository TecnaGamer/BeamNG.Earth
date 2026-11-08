# Subsea tunnel profiles and finite water volumes

Issue [#792](https://github.com/TecnaGamer/BeamNG.Earth/issues/792) reports a cut portal but no tube at
Ryfylketunnelen. `TunnelProfiles` deliberately excluded an entire connected component when a part crossed water:
the existing WaterBlocks would otherwise put fluid inside the tube. Removing that exclusion alone is unsafe.

This change builds source-connected wet profiles and clips the water volumes above their covered geometry.
It complements [#638](https://github.com/TecnaGamer/BeamNG.Earth/pull/638), which addresses mapped tunnel layers,
approaches and terrain/portal collision handoff. It does not establish that every problem in #790 is resolved.

## Profile and terrain

- Source-coincident ends form topology. Nearby crossing or parallel bores are not welded together. A reversed
  source part is sampled in the same continuous chain; internal source boundaries never become portals.
- Uniform arc samples at most 4 m apart feed the native sparse QP core shared with
  [#814](https://github.com/TecnaGamer/BeamNG.Earth/pull/814). The numerical Rust files and locked Clarabel version
  are identical to that contribution. The modeled maximum grade is 8%, vertical curve parameter K is 20 m/%.
  These are modeling constraints, not surveyed design specifications.
- Free mapped portals use the common raw ground datum. Open approaches and the grader use the resulting profile.
  A profile never asks its own endpoint grader to plan it recursively. Road and terrain readers solve in the same
  canonical region frames, from the same raw ground/water inputs.
- Without a survey the floor must be at least 8 m below the modeled bed. Raw ground is already sea-carved: water
  depth is not subtracted from it again. With at least 90% coverage, the existing `roadz` provider supplies its
  datum-aligned observations as strong targets. A survey may put the bore above an inferred bed; it still needs
  8 m below the water surface. The QP can smooth the observations to satisfy its hard bounds.
- Missing surface datums, incomplete portals and infeasible constraints retain the exclusion. Infeasibility
  certificates are never used as elevations; unsuccessful wet solves mark a revisit fault to prevent persistence.
- Covered rock/grass roads get an explicit colliding floor: their terrain paint is above the bore.

Plans format advances from 12 to 13. Facts stay at 4. The common-road golden had an existing local discrepancy
(`0f6960...` checked in versus `58caeaab...` when evaluated on the unchanged baseline); refreshing the plans golden
under the deliberately bumped format records `58caeaab0f182f42f4d8028923d80a873c45750d`. The fixture contains no
tunnels. If #814 merges first, its format bump must be reconciled with this one before merging.

## Water and mesh lifetime

The official [BeamNG water documentation](https://documentation.beamng.com/modding/levels/level_classes/water/)
describes finite WaterBlocks and spline Rivers. This uses those existing objects; there is no arbitrary cavity
or per-vehicle water switch.

Covered sections emit deduplicated geodetic floor polylines, vault height and a conservative half width. The Lua
consumer caps every intersecting water column at least 1 m above the vault. Tilt, block overlap and horizontal
displacement of tilted deep volumes are included, with an additional 0.2 m height envelope for mesh/protection
simplification and rounding. A bounded quadtree refines affected tiles toward 16 m; at most
256 visible blocks are produced (or the original count if the server already sends more). When the budget is
exhausted a coarser parent remains capped. The surface stays over a buried tube; deep water over a wider area can
become shallower. This is a conservative local approximation, not a reconstructed seabed or an exact cavity.

Rivers retain flow; affected source pieces become straight two-node spans with constant capped depths, avoiding
spline depth overshoot from neighbouring controls. Long affected spans are split toward 16 m. This can add River
objects and change local bends, so River transitions need gameplay validation too. Zero-depth spans inside solid
land are omitted. The player-local invisible
physics block obeys the same bound; near an exposed/landward roof its footprint shrinks or is omitted. Surface
boats keep water over a sufficiently buried tube. Existing same-position nudges refresh stationary fluid membership.

`roads` registers protection before any tunnel/deck collider is created. A tile instance and its live deck/tube
objects retain it. Dropping a tile key, reusing it, receiving water later, disconnecting or deleting objects over
several frames cannot restore a deep volume prematurely. Mesh references are released after deletion, a collision
rebuild and a subsequent update. Rebases reproject the original geodetic records into the new anchor. Level reset
clears the references; a transport clear retains references belonging to road objects still on screen.

The mod advertises `hello.waterguard = 1`. With water enabled, a server requests a mod update and starts no world
jobs for an older client until it receives a compatible hello. Without water it continues accepting older clients.
This prevents a mixed-version connection from rendering new tubes inside the old client's unrestricted fluids.

## Reproduction and limits

Rebuild native version 154 with `python tools/build_native.py` before running the Python checks. The source PR
does not update the tracked platform binary. The runtime solver adds no Python/SciPy dependency.

```
python tools/run_tests.py test_subsea_tunnels test_tunnels test_grading test_roadmesh test_seacarve test_seatiles test_bake_formats --par 3
python tools/run_lua_tests.py test_subsea_water test_sea test_roads
python tools/quality_gate.py
```

`test_subsea_tunnels` exercises the real native solver, independently created region stores for terrain/meshes,
reversed source parts, grade limits, unsupported inputs, actual deck/tube consumers and serialized protection.
Its captured fixture is one RV13 bore from Statens vegvesen's NVDB, sequence 2725982, retrieved 2026-11-06 under
NLOD 2.0; the JSON records source URL and attribution. The original 3D geometry spans over 14 km and reaches
about -289 m. Its test DEM and water are explicitly synthetic. The existing survey matcher, datum calibration
and constrained profile run afresh; this is not an Overture/DEM capture or an end-to-end replay of #792.

`test_subsea_water` checks water bounds across the full bore width and tile overlaps, the object cap, rivers,
surface water, the invisible physics volume, stationary refresh, reconnect/rebase and actual road-object lifetime.
Engine APIs are stubbed. BeamNG has not been launched for this contribution. Actual buoyancy/hydrolock, River
interpolation, portal collision, shadows and visuals still need an in-game drive, including both portals of
Ryfylketunnelen, moving/stationary vehicles, a boat above it, a rebase and a reconnect. No claim of gameplay
validation or complete resolution of #790 is made here.

Local validation on Windows x64, Python 3.10.11 / NumPy 2.2.3, native MSVC Release version 154:

- New profile/consumer/handshake module: 10 tests passed, including the captured NVDB bore. Existing tunnel
  regressions: 32 passed. Grading: 65 passed; road mesh: 49 passed; sea carving: 32 passed.
- Expanded road, bridge, geotile, river, water and bake-format regressions: 24 modules passed after correcting
  an existing Windows-only test fixture. Its loop variable retained an mmap view across truncation; the test
  now releases that view and accepts Windows' `OSError` for the intentionally truncated mapping as well as
  NumPy's `ValueError`. The original failure was independently reproduced on the unchanged baseline.
- Foundation Python gate: 38 of 40 modules passed. `test_cache` has two pre-existing WinError 1314 symlink
  privilege failures; `test_process_lifecycle` has three pre-existing `guard.json` readiness timeouts. These
  were reproduced on the unchanged baseline and remain failures, not skips or a green gate. Later targeted reruns
  cover the final handshake and profile changes.
- Complete server sessions: 53 tests passed. All 93 Lua modules passed under Lupa/LuaJIT, including 81 assertions in the new water/road lifetime module.
  Protocol agreement: 32 tests passed; additional CLI, graphics, road refresh, traffic and sign consumers passed.
- Locked native Release compilation and the targeted Rust banded-factor unit test passed. Both Node map-math
  tests and all four Rust server-list tests passed. Client and BeamMP server packages were generated locally.
- After rebasing onto `e2ff61f5`, the profile, tunnel, grading and bake-format modules passed again. An additional
  imagery run had two failures among 45 tests: the DXT1 mip byte comparison and the open-ocean source expectation
  (`eox2016` expected, `eox2023` selected). Both were independently reproduced in a clean `e2ff61f5` worktree with
  the same compiled native module. No imagery behavior or expectations were changed by this contribution.

These results measure synthetic/offline consumers on this machine. They do not replace the gameplay checks above.
