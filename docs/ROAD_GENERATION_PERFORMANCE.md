# Road generation performance audit — 2 November 2026

Baseline: original `TecnaGamer/BeamNG.Earth` dev checkpoint
`9f4739d213caab307a85cb292e4306029bf2a984`, fetched for this audit. This includes
the newer GPU, provisional-terrain, and high-speed-drive work after `7baa76bf`.
The audit branch starts directly at that checkpoint. It contains no custom
building-system or runtime-profiler commits. The separate PR targets the frozen
`audit/upstream-dev-9f4739d2` branch on the test fork; existing PRs #1 and #2 and
their branches are unchanged.

## Findings and priority

| Priority | Finding | Evidence and consequence | Next action |
|---|---|---|---|
| P1, fixed here | Bridge sidewalk lookup rebuilds a whole-cell endpoint graph for every bridge on every tile. | `roads.deck_walks`: 657 calls over nine Auckland tiles; 4.88 s of 10.75 s profiled geometry time. Tokyo: 252 calls, 3.27 s of 11.48 s. | Share the immutable endpoint graph on its decoded source cell; keep width, sidewalk, and driving-side decisions per call. |
| P1, follow-up | First road content waits for expensive shared planning before it can be sent. | A real-data, offline worker's first tile took 42.85 s; the `items` stage took 39.48 s. With the graph fix: 40.10 s overall, 37.24 s in `items`. The fix does not remove this startup delay. | Split dependency timing below `items`, then prewarm/reuse the canonical grading, lane-flow, width and bridge plans needed ahead of the player. Investigate sending essential road content before optional buildings/props, preserving collision readiness. |
| P2, reproduced | Concurrent cold map-cell readers get separate decoded `Features` objects. | Two synchronized calls to `MapData.cell` for the same key return different objects. Each can acquire its own expensive geometry/spec/lane-flow caches; the final `_remember` overwrites the previous entry. | Coalesce decoding per cell/layer, rechecking the memory cache after waiting. Keep recursive neighbour/geometry work outside the admission lock to avoid deadlocks. |
| P2, code-confirmed risk | The road watchdog counts process queue time as build time, and stale work survives cancellation. | `Server.build_roads_geo` starts its 300 s timeout immediately after `submit`; a timeout recycles the whole road pool. `GeoTracker` cancels wrapper futures on rebase, but a running build thread/process continues. | Record submitted/started/finished timestamps, queue age and anchor generation. Discard obsolete jobs before expensive work; recycle based on execution liveness. Validate with queued jobs and teleport scenarios. |
| P2, reproduced | Revisit hits can report the previous tile's stage timings. | `_geo_content` returns on a cache hit before `_stage_clock` resets thread-local state. A hit following a synthetic 12.5 s stage reports that old stage unchanged through `last_tile_stages()`. | Reset stage state at job entry and explicitly report cache hit/miss plus restore time. Check the legacy cached tile path as well. |
| P2, needs game capture | Client mesh imports and collision rebuilds can remain the visible bottleneck after server work improves. | `roads.lua:update` performs native object creation and then charges the governor. It repeatedly scans/removes items from `walkQ`; collision readiness has separate timing and player-safety gates. No game timings were measured here. | Capture receive-to-visible and receive-to-collidable latency separately from imports/reloads. Investigate spatial queue selection only if queue scans are material. Preserve collision and terrain-swap safeguards. |

## Measured improvement

Unprofiled sequential runs, nine neighbouring geotiles per city, three passes
per version (27 builds each). Windows, Python 3.14.7, NumPy 2.5.4, native API 140.
No simultaneous audit benchmarks were run. Times are wall-clock medians, so
individual machines and live-game contention will differ.

| Cached road input | Road builder, before → after | Reduction | Builder + draped mesh, before → after | Reduction |
|---|---:|---:|---:|---:|
| Auckland, 4,273 source features | 681.9 → 268.6 ms | 60.6% | 772.7 → 361.0 ms | 53.3% |
| Tokyo, 5,277 source features | 736.5 → 468.8 ms | 36.3% | 838.7 → 580.3 ms | 30.8% |

All 54 paired output hashes match exactly, covering chunks, sidewalk runs,
bridge context, mesh positions, triangles, UVs, and material assignments.
Mesh-only medians stayed approximately unchanged. These are **geometry-stage
speedups**, not whole-server loading-time or FPS claims.

The controlled geometry benchmark uses cached road geometry, flat 10 m terrain,
urban classification everywhere, and the local bridge-height fallback. It
excludes downloads, grading, building generation, shared bridge-chain solving,
worker queueing, serialization and game imports. The full-worker check below
exercises those server dependencies separately.

The graph cache changes repeated O(bridges × source features) endpoint indexing
to one O(source features) construction per decoded cell, followed by the existing
bounded chain search. Endpoint rounding, iteration order, chain length and
side-selection logic are unchanged. It stores geometry only, publishes complete
graphs, and has no global references to source cells. Its lifetime follows the
cell's existing caches. The measured graph footprint is about 2.82 MiB for the
Auckland cell and 3.11 MiB for Tokyo, per decoded cell/process; this additional
retained memory is the tradeoff to monitor on small machines.

## Why the first tile can still take tens of seconds

Production flow is `GeoTracker` → server build wrapper → process pool →
`blockworker.roads_geo` → content/revisit lookup → `_tile_parts` → per-anchor
skin → client import and collision activation. A road job includes building
placement, grading, sidewalks, bridges, parking markings, rails and street props.

An offline snapshot of cached Auckland data (2,177 files, 206,844,913 bytes of
map/DEM inputs) was copied into isolated scratch caches. Fresh worker processes
built the geotile under the sample location with grading, measured widths,
buildings, sea and gas stations enabled; airports disabled. Revisit products were
disabled. Fresh map/DEM mirrors and derived plans were allowed to populate there.

| First tile, unprofiled | Original | Patched |
|---|---:|---:|
| Total wall time | 42.85 s | 40.10 s |
| Content `items` stage | 39.48 s | 37.24 s |
| Content `build_tile` stage | 1.20 s | 0.80 s |
| Skin | 0.25 s | 0.24 s |

Both cold runs recorded seven revisit faults from the incomplete offline
snapshot. These are diagnostic runs, not complete-world acceptance or a precise
full-pipeline A/B result. The next two tiles took 2.31 s and 1.68 s on the original,
versus 1.76 s and 1.27 s patched, with zero faults on those subsequent tiles.
This is one observation per tile, not a statistically established throughput
improvement. All 22 generated `.cdae` files match byte for byte across the runs.

The `items` stage combines `_tile_decks` and `buildings.road_tile_items`; it is
not just building meshing. Those calls force bridge-chain resolution, graded
ground, measured widths and neighbour-cell lane inference. The actual `bld`
mesh stage was only tens of milliseconds in the unprofiled first tile. The profile points
to neighbour reads, lane-flow/spec work, width preparation, and map-array mirror
I/O as investigation targets. Their inclusive timings overlap and must not be
added together. A live server may have already warmed some plans in its terrain
workers, which this isolated cold-worker check does not simulate.

Further delay multipliers visible in the original code:

- `MapData` can wait up to 180 s for another process's cell build before doing
  its own work (`BUILD_WAIT_S`). Its decoded mirror opens separate `.npy` files
  for array fields; first-use mirror creation and many small file opens deserve
  separate timing on Windows.
- `roadz.gap_answers` can wait up to eight seconds for a batch of LiDAR gap
  answers in supported regions. This was not exercised by the Auckland run.
- Each tracker admits four jobs; process capacity is chosen by CPU/RAM (two,
  three or four by default). Navgraph and terrain-patch work can share the road
  pool. Adding workers can duplicate planning/memory work and contend with the
  game; increasing the count alone is not an established fix.
- Client collision reloads normally wait up to six seconds, with separate
  urgent rules and a terrain-swap quiet window. That is an intentional readiness
  tradeoff, not evidence of six seconds of server generation work.

## Validation and reproduction

`docs/ROAD_GENERATION_PERFORMANCE.json` records the benchmark rows, input hashes,
scope, cold-worker stage measurements and deterministic diagnostic results.
No map datasets, personal configuration, native binaries or custom-build files
are included in this PR.

Run `python tools/audit_road_caches.py` to reproduce the stale-stage and concurrent
decode findings without network or cache-file I/O. It reports current behaviour
as JSON; it does not enforce that behaviour as correct.

Using a Python environment with the repository dependencies and native module:

```powershell
python tools/bench_road_generation.py --at=-36.85325222344017,174.759521484375 --country NZ --cache-dir '<existing-cache>' --out build/audit-auckland --repeat 3
python tools/bench_road_generation.py --at=35.701916732853384,139.779052734375 --country JP --cache-dir '<existing-cache>' --out build/audit-tokyo --repeat 3
```

The benchmark reads source `.npz` files directly, never mutates the source
cache, and fails if required inputs are absent. Use the same tool and input
hashes on baseline and patched checkouts. Add `--profile --repeat 1` for function
attribution; use runs without `--profile` for before/after timing. `--anchor-tiles`
also exercises the older tile layout. Outputs include source-file hashes so
uncommitted changes can be distinguished from `HEAD`.

Regression command (from `server/tests`, real-data integration tests disabled):

```powershell
$env:BNGE_ROADGEO_REAL='0'
$env:PYTHONPATH='..'
python -B -m unittest test_roads test_roadmesh test_bridge_scenarios test_roadwidth test_roadgeo
```

175 tests ran: 173 passed, two optional real-data cases skipped. New scenarios
cover reversed connected bridge chains, different width/side settings using the
same geometry, and replacement cells with changed connectivity. Windows asyncio
tests required running outside the sandbox because its loopback socket setup
stalled. An earlier broad discovery attempt was stopped; it is not counted as a
passing suite. Native BeamNG driving, visibility, collision and frame-time
acceptance remain outstanding.

Before claiming an end-to-end improvement, capture original and patched builds
at cold launch, warm drive, fast corridor travel and teleport/rebase. Record
queue/execute/restore/send/import/collision timestamps, first-road latency,
P50/P95 tile latency, road lead distance and frame-time percentiles. Prioritize
the first-use dependency path and decoding deduplication next; retain the custom
build as a separate integration and acceptance exercise.
