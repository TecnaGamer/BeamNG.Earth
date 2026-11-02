# Road generation performance audit — 2 November 2026

Baseline: original `TecnaGamer/BeamNG.Earth` dev checkpoint
`9f4739d213caab307a85cb292e4306029bf2a984`, fetched for this audit. This includes
the newer GPU, provisional-terrain, and high-speed-drive work after `7baa76bf`.
The audit branch starts directly at that checkpoint. It contains no custom
building-system or runtime-profiler commits. The separate PR targets the frozen
`audit/upstream-dev-9f4739d2` branch on the test fork; existing PRs #1 and #2 and
their branches are unchanged.

Cold-start follow-up continues from performance checkpoint `35e2e38e`, the
existing PR head. It changes only the map-array mirror format and adds benchmark
and regression coverage. The latest original-dev checkpoint remains the base
above; no custom-build changes have been pulled into this branch.

## Findings and priority

| Priority | Finding | Evidence and consequence | Next action |
|---|---|---|---|
| P1, fixed here | Bridge sidewalk lookup rebuilds a whole-cell endpoint graph for every bridge on every tile. | `roads.deck_walks`: 657 calls over nine Auckland tiles; 4.88 s of 10.75 s profiled geometry time. Tokyo: 252 calls, 3.27 s of 11.48 s. | Share the immutable endpoint graph on its decoded source cell; keep width, sidewalk, and driving-side decisions per call. |
| P1, reduced here | First road content waits for expensive shared planning and thousands of map-array file operations. | Three matched cold runs: median first tile 43.54 → 29.04 s, 33.3% less time. Mirror files fall from 5,340 to 907. Bridge-deck planning, rather than building placement, dominates `items`. | Small arrays now share one uncompressed archive per layer; large arrays remain mapped. Remaining work: deduplicate cell decoding and reuse prepared lane inputs before considering numerical-kernel changes. |
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

## Cold-start fix and remaining delay

Production flow is `GeoTracker` → server build wrapper → process pool →
`blockworker.roads_geo` → content/revisit lookup → `_tile_parts` → per-anchor
skin → client import and collision activation. A road job includes building
placement, grading, sidewalks, bridges, parking markings, rails and street props.

An offline snapshot of cached Auckland data (2,177 files, 206,844,913 bytes of
map/DEM inputs) was copied into isolated scratch caches. Fresh worker processes
built the geotile under the sample location with grading, measured widths,
buildings, sea and gas stations enabled; airports disabled. Revisit products were
disabled. Fresh map/DEM mirrors and derived plans were allowed to populate there.

The initial audit's single-run observations, before the cache-format change:

| First tile, unprofiled | Original dev | Endpoint graph fix only |
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

The follow-up compares `35e2e38e` (already containing the endpoint graph fix)
against that checkpoint plus the packed mirror. Three sequential A/B pairs use
new raw-only cache copies and genuinely spawned workers. No derived mirrors,
plans or revisit products are reused between runs. The OS file cache is not
flushed, and Python imports/worker initialization are outside the road-job timer.

| Unprofiled result | Checkpoint `35e2e38e` | Packed mirror |
|---|---:|---:|
| First tile, runs 1 / 2 / 3 | 43.54 / 46.19 / 41.34 s | 27.89 / 29.04 / 30.63 s |
| First tile, median | 43.54 s | 29.04 s |
| First tile `items`, median | 39.94 s | 25.37 s |
| Second tile, median | 2.03 s | 1.74 s |
| Third tile, median | 1.38 s | 1.28 s |
| Mirror files after three tiles, run 1 | 5,340 | 907 |
| Mirror bytes, run 1 | 265,301,159 | 265,817,315 |

The median first-tile saving is 14.51 s (33.3%). All 66 paired `.cdae` hashes
(22 per pair) match; all six runs have the same fault counts, 7 / 0 / 0. The
snapshot is incomplete and offline: this measures a cold derived cache with
raw inputs already present, not first-time network download or game-ready
latency. These three pairs establish a local improvement, not a universal P95.

Arrays up to 64 KiB share `small.npz` with ZIP_STORED (no recompression); larger
arrays stay in individual read-only memory-mapped `.npy` files. This preserves
array bytes, dtypes, ordering, localized coordinates and the existing source
stamp invalidation. Corrupt or missing bundles are cache misses and rebuild
from source. The versioned mirror directory lets older/custom builds retain
their own format; both layouts remain covered by the existing global pruning
budget. Old mirrors are not migrated or eagerly deleted. The measured disk-size
increase is about 0.19%, while file count falls 83.0%.

The `items` stage combines `_tile_decks` and `buildings.road_tile_items`; it is
not just building meshing. An instrumented pre-fix run attributes 43.68 s to
`_tile_decks` and only 0.24 s to `buildings.road_tile_items`. Those calls force bridge-chain resolution, graded
ground, measured widths and neighbour-cell lane inference. The actual `bld`
mesh stage was only tens of milliseconds in the unprofiled first tile. The profile points
to neighbour reads, lane-flow/spec work, width preparation, and map-array mirror
I/O as investigation targets. Their inclusive timings overlap and must not be
added together. A live server may have already warmed some plans in its terrain
workers, which this isolated cold-worker check does not simulate.

An additional trace measures the native lane, wall-ray and point-grading
kernels at about 0.21 s combined. Their preparation costs dominate this case.
[Alternative algorithms and GPU assessment](ROAD_GENERATION_ALTERNATIVES.md)
documents researched worklist, spatial-tree, ray-traversal and batched GPU
options, with sources and an order for experiments. No GPU port is included.

Further delay multipliers visible in the original code:

- `MapData` can wait up to 180 s for another process's cell build before doing
  its own work (`BUILD_WAIT_S`). Packing small arrays addresses mirror I/O;
  it does not remove source-build waits or duplicate concurrent decoded cells.
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

For the cold-start follow-up, use the same raw snapshot and one new output
directory for every invocation. Alternate baseline and patched runs; do not
run performance measurements concurrently:

```powershell
python tools/bench_road_cold_start.py --snapshot '<raw-cache>' --out build/cold-before-1 --server-dir '<checkpoint-35e2e38e>/server'
python tools/bench_road_cold_start.py --snapshot '<raw-cache>' --out build/cold-after-1
python tools/bench_road_cold_start.py --snapshot '<raw-cache>' --out build/cold-trace --tiles 1 --trace
```

The tool copies only `mapdata/v2`, `dem/mapterhorn` and `dem/aws-terrarium` from
the source cache. Its defaults reproduce the Auckland case above. Use `--at`,
`--country` and `--right-hand` for other locations. It records raw-input, server
source and native-library hashes, fault counts and output-mesh hashes. `--trace`
is diagnostic and must be excluded from performance comparisons.

Current regression commands (from `server/tests`, real-data integration tests disabled):

```powershell
$env:BNGE_ROADGEO_REAL='0'
$env:PYTHONPATH='..'
python -B -m unittest test_mapdata_mirror test_mapdata test_roads test_roadmesh test_bridge_scenarios test_roadwidth test_roadgeo
python -B -m unittest test_grading
```

The cache/road suite runs 245 tests: 243 passed, two optional real-data cases
skipped. Grading runs separately: all 46 passed. Eight new mirror regressions
cover exact array bytes/dtypes, the 64 KiB boundary, mapped large arrays,
independent readers, many small fields, stale source stamps, missing/corrupt
files, coexistence with the old layout and disabled mirrors. Existing cache
tests cover localized coordinates, concurrent builders and pruning. The initial
graph-fix scenarios cover reversed bridge chains, different width/side settings
and replacement-cell connectivity.

Running all modules in one process produces one pre-existing order-dependent
grading failure: `GraderTest.test_points_far_apart_plan_only_their_cells` counts
31 fake cell reads against its limit of 30. The same assertion and count
reproduce on untouched `35e2e38e`; its isolated test passes there too. No grading
behavior or assertion is changed to hide it. The two commands above pass in
separate processes. Windows asyncio tests required running outside the sandbox
because its loopback setup stalled; test temporary files were kept in the audit
checkout. Native BeamNG driving, visibility, collision and frame-time acceptance
remain outstanding.

Before claiming an end-to-end improvement, capture original and patched builds
at cold launch, warm drive, fast corridor travel and teleport/rebase. Record
queue/execute/restore/send/import/collision timestamps, first-road latency,
P50/P95 tile latency, road lead distance and frame-time percentiles. Prioritize
lane-input preparation and decoding deduplication next; retain the custom
build as a separate integration and acceptance exercise.
