# Local bridge levels through merged road features

Related: [#866](https://github.com/TecnaGamer/BeamNG.Earth/issues/866), Changhua System Interchange,
24.106716, 120.562440. Base: `dev` at `2cbc26ce`.

## Real place and source evidence

Taiwan's NLSC PHOTO2 orthophotos show the two main motorways and the interchange's ramps crossing at
different heights. References were inspected at zooms 18 and 16 using the
[official public WMTS service](https://maps.nlsc.gov.tw/S09SOA/pro/Wmts_ajax_main.jsp).
Mapillary credentials and a BeamNG runtime are unavailable on this machine.

The production map pipeline, Overture release `2026-10-21.0`, already imports the bridges. The problem
reproduced here is a lost local level, not absent source geometry:

- `6dd35a83-a3bb-4511-a3e8-91e519fd6ffb`, a ramp, is a bridge throughout. Its mapped levels are 3 over
  fractions 0–0.403092295, 2 over 0.403092295–0.835585535, and 1 over the remainder.
- The old `bridge_levels` chooses the highest overlapping level for the entire continuous bridge range.
  It consequently promotes the level-2 section to level 3, the same as the mainlines it crosses.
- This removes the deck-over-deck clearance requirements at both mainline crossings, roughly 0.47–0.49 miles
  west of the report point, in the same interchange.

The small JSON fixture captures the three relevant features' real lon/lat geometry and mapped rules, with
source/attribution metadata. It includes no solved heights, DEM substitute, network responses, or third-party code.
Its ordering regression fails on the unchanged owning source and passes with the correction.

## Change

A Rust kernel intersects each existing bridge range with the mapped local level rules. Adjacent equal-level
spans are coalesced; very short spans are absorbed without leaving holes. Missing levels retain the existing
default of 1. The bridge/tunnel/ground classification is unchanged. Geometry and its level list consume the same
span sequence, so each part retains its correct level and common endpoints remain identical.

This uses the existing deck solver and ground grader. It adds no geographic condition, inference of missing
bridges, replacement AI, lane-count changes, or new source. Native API 171 is required. Road facts advance 7→8
and grading plans 18→19; the bake golden is refreshed so saved/baked inputs cannot reuse the old segmentation.

## Same-input server replay

The spatial probe uses the production MapData and DEM, a fresh isolated cache, default grading, and
`BridgeProfiles` in curve mode. It compares the unchanged base with this patch, using the same cache/source
release and anchor. At the two centerline crossings between the ramp and the mainlines:

| Crossing, ENU offset from report point | Surface-height difference before | After |
| --- | ---: | ---: |
| -756.152 m east, +29.369 m north | 1.454 m | 7.742 m |
| -778.353 m east, +19.195 m north | 1.528 m | 7.681 m |

These are sampled surface separations, not a survey of real-world absolute heights or a complete full-width
collision/vehicle-clearance measurement. Other height inference, bridge endpoints, and all other Taiwanese
interchanges have not been established as correct. No in-game drive, screenshot comparison after the patch,
FPS measurement, or local Linux validation was available.

## Checks

- Windows x64 MSVC native Release build, API 171; tracked binaries are not included.
- The six new tests cover level transitions, continuous geometry, equal levels, missing levels, tiny intervals,
  preservation of tunnels/ground, and both real-data ramp crossings.
- The new regression module is included in `tools/quality_gate.py`.
- Sixteen targeted Python modules pass: 389 cases collected, including two optional real-data skips. These cover
  roads, bridge clearance, approaches, underpasses, grading, seams, meshes, tunnels, the carriageway solver,
  plan/revisit/chunk caches, input validation, and bake formats.
- Lua `test_roads` and `test_subsea_water`: 188 assertions pass, offline/stubbed.
- The additional legacy `test_otto_bridge` replay exceeds 300 seconds in setup on both this branch and the
  unchanged baseline. The baseline worktree is at `1de7cf62`; its roads, grading, test and fixture are byte-identical
  to `2cbc26ce`. Both stacks are in the fixture's captured-DEM nearest-sample lookup during cutting probes.
  This local replay is not a passing test; the complete server suite was not run.
