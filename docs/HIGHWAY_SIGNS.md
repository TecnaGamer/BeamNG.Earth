# Highway signs on 1.6.3 beta

This custom migration brings the highway sign system from `BeamNG.Earth-1.6.2-modified/BeamNG.Earth`
at `a3352a4` onto upstream 1.6.3 beta (`d230840`).

## Included

- Exit and advance panels, exit numbers, destination lettering and blue service/rest-area panels.
- Overhead destination panels, directional arrows and Interstate, US and supported state route shields.
- The latest portrait exit layouts outside Michigan, Michigan panel styles and paired advance/overhead placement.
- Interstate mile markers from the bundled Michigan/Kentucky references and mapped OSM milestones.
- Entrance-ramp merge warnings, including the state-dependent post arrangements.
- The combined **Highway Signs** switch under **Mods > BeamNG.Earth > World**, enabled by default.
- Regional OSM downloading/indexing, cached-index compatibility, lettering atlases, materials, attribution,
  source font and the data/lettering generation tools.

Signs require Roads. The switch applies to existing and queued signs, retaining their shape slots so enabling it
again restores them without rebuilding road collision. The older `osmExitSigns`, `mileMarkers` and `mergeSigns`
keys remain accepted; saved combined-switch preferences take precedence when settings are applied at startup.

## Data and streaming

Sign destinations come from mapped OpenStreetMap tags. Regional Geofabrik extracts download and index in the
background, using the shared `mapdata/raw/osm-lanes` cache and process locks. Previously indexed signs remain
available while an index is upgraded. Offline mode uses existing indexes and bundled mile references.
Mile-reference coverage is regional; a mapped road does not guarantee a mapped destination or mile marker.

`signstream.py` allows at most two jobs per connection after their corresponding road tiles have been sent.
Process mode uses a dedicated two-worker sign pool; fallback mode uses the same builder in threads. Signs are
render-only shapes with separate `s<tx>_<ty>` keys, pooled slots and inline JSON messages. Old-anchor, departed-tile,
closed-connection and superseded-index results are discarded. A roads reset resets both streaming trackers.

Placement uses 1.6.3's road widths, source geometry and grading. Its road revisit cache restores roads while signs
are built independently, so a newly completed regional index can refresh signs at an already visited place.
The Lua loader keeps sign coverage separate from road readiness and preserves railway collision and fuel records.
Sign materials participate in the existing startup material prewarm.

1.6.3's road generation, native kernels, cache format and machine-based road-worker allocation provide the base.
This port's added server dependency is `osmium>=4.0` (`server/requirements.txt`).

## Validation

Automated checks cover sign layouts and lettering, state shields, mile references, merge placement, OSM import,
offline/redirect behavior, index refresh, cancellation, settings migration, pooled slots, anchor transforms and
road readiness. Additional 1.6.3 checks cover spawned-process/fallback equivalence, signs after a road revisit-cache
hit, and coexistence with railway colliders and refuelling records. Packaging checks exercise frozen data lookup
and compare the actual EXE and BeamMP client ZIP against the source assets.

The existing road, grading, mesh, map-data, worker, cache, gas-station, railway, protocol and server suites were
also run. The full server suite completed 48 tests with `OK` in about 603 seconds; the wrapper marked its run as
over-time because it had been given a 240-second limit. Use the default 1500-second limit when repeating it.
Four existing checks were skipped (three optional real-world gas-station fixtures and one check requiring an
external LuaJIT executable). The standalone Lua suites ran using Lupa's LuaJIT 2.1 with a high-resolution timer,
matching BeamNG's timer API; both the original and migrated road suites pass with that timer.

Useful test commands from the repository root:

```powershell
.venv/Scripts/python.exe -B tools/run_tests.py test_advance_sign_layout test_exit_signs test_highway_signs test_merge_signs test_mile_markers test_portrait_exit_signs test_sign_packaging test_state_route_markers test_through_signs test_prewarm
.venv/Scripts/python.exe -B tools/run_tests.py test_roads test_roadmesh test_grading test_mapdata test_blockworker test_server test_protocol_agreement test_gasstations test_railways test_revisit test_pools test_budget test_staging
```

The generated Windows app passed an isolated offline server startup and client handshake. Its glyph data,
state boundaries, mile references, Osmium modules, native kernel, Lua/settings files and sign materials/textures
were verified in the built artifact. The matching BeamMP ZIP contains the same client files.

Visual driving validation in BeamNG remains outstanding. Check a known Michigan exit and a non-Michigan portrait
exit, overhead lettering/shields, merge warnings and mile markers; toggle Highway Signs, teleport away and back,
and drive through an origin rebase. Check sign placement near bridges and railways and confirm nearby fuel stations
still work. These observations require the game and are not established by the offline tests.

## Local build outputs

The workspace's `builds` folder contains:

- `BeamNG.Earth-1.6.3-beta-highway-signs.exe`
- `BeamNG.Earth-BeamMP-Server-1.6.3-beta-highway-signs.zip`

The upstream `VERSION` remains `1.6.3-beta`; the artifact names distinguish this custom build. Local build and
verification scripts/logs are under the ignored `build` folder, including `highway-signs-build-verification.json`.
