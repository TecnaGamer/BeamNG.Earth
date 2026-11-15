# Dutch signals: actual city validation

Measured on 15 November 2026, source `91675ec94` (dev `8369393c2` merged),
locally built release API 302. The native binaries are not committed.

## Stationary Rotterdam ABBA

Capelseweg/Hoofdweg, 51.95153 N, 4.56458 E. BeamNG 0.40.5, Direct3D12,
RTX 3060 Ti, isolated profile, near visual detail, 100 km horizon, parked vehicle,
fixed manual daylight/clouds, no concurrent compilation. The guarded repository
benchmark used 60 s warm-up and 30 s capture per round. All rounds passed focus,
stationarity and settled-scene checks (388 far tiles, server backlog zero).
NDW signs remained enabled; only `dutchSignals` changed.

| Round | Dutch signals | Frame p50 ms | Frame p95 ms | Mean game working set MiB | Native approaches |
| --- | --- | ---: | ---: | ---: | ---: |
| A1 | off | 10.0743 | 13.8434 | 4406.4 | 0 |
| B1 | on | 10.1577 | 14.6030 | 4418.6 | 19 |
| B2 | on | 9.4647 | 12.7683 | 4418.0 | 19 |
| A2 | off | 9.2432 | 12.1703 | 4414.5 | 0 |

The arithmetic mean of the two round medians is 9.6588 ms off and 9.8112 ms on;
the mean working set differs by about 7.9 MiB. These are one stationary scene,
not a universal FPS or memory guarantee. The changing round timings show noise
and warm-cache effects. This measures the main game process, not total server,
GPU or system memory. The benchmark restores the original feature setting.

The raw local artifact is `release-city-abba.json` in the isolated test outputs;
the table preserves its measured round values without publishing machine paths.
The local NDW build is a limited public-source sample, not national coverage.

## Runtime regressions checked

Nineteen complete approaches accepted with zero stream errors. A far, parked
bridge deck correctly remained deferred until its collision mesh is available;
that group no longer prevents independent nearby complete junctions appearing.
Two live snapshots had identical transforms for all 206 fixed parts by key,
while phases changed. At most one optical face existed per key, including
pending creation: no temporary red/green overlap during budgeted swaps.

Modern terrain-painted roads have carriageway interiors absent from the marking
mesh. Planning now uses the existing server ground-cover ribbons as well as
drawn raised surfaces; ground ribbons sample committed quantised terrain and
respect holes. No extra BGT data or downloaded road model is required.

Final checks: 46 native geometry tests, 47 signal server tests, 20 NDW tests,
127 Lua modules and 4 JavaScript tests passed. Windows and Linux release builds
completed. Linux runtime was not executed locally. Seven offline roadgeo tests
passed; two real-source tests require cache shards absent on this machine.
The Rotterdam run above exercised actual generated geometry instead.

Placement remains inferred from source context and graph lanes. Surveyed stop
lines and independent protected-turn timing programmes are outside this PR.
