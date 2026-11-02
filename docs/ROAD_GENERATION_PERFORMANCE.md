# Road generation performance audit — 2 November 2026

The PR now includes current upstream `dev`
`11cc55572fd5b828f52a0cf0edf0cf449cba1ac0` and targets `dev` on the test fork.
It contains no custom building-system or runtime-profiler commits.

The historical benchmark baseline is upstream checkpoint
`9f4739d213caab307a85cb292e4306029bf2a984`. The measurements below retain their
original baselines; merging current dev does not remeasure their speedups.

The packed-mirror follow-up continued from checkpoint `35e2e38e` and was saved
as `df2cb877`. The lane-preparation follow-up starts from that latest performance
checkpoint and was saved as `ea58fcba`. No custom-build changes have been pulled
into this branch.

## Current integration and playtest

The user reported a successful native BeamNG playtest of `ea58fcba` on
2 November 2026. The launcher verified the matching isolated client profile
and exact server checkpoint. This is manual acceptance, not an FPS benchmark.

After merging dev at `50e41cac`, 501 Python tests passed across 25 road, collision,
grading, lane-input and map-cache modules; three optional tests were skipped.
The five additional worker tests pass with their process-owned revisit file
descriptors closed before Windows temporary-directory cleanup. Without that
test cleanup adapter, two cases fail during cleanup on both this branch and
untouched upstream `50e41cac`; their assertions are unchanged.

The upstream updates include photo/vegetation worker routing and newer terrain
prefetch/planning behavior. The latest combined regression run against `11cc5557`
passes all 136 selected Python modules; its counts are recorded in the PR body.
Real localhost startup also passes with legacy and v2 building modes and both
profiler modes. These integrations were checked automatically; the user's native
playtest predates them. The performance branch itself remains independent.
On the tested performance checkpoint, all 22 real-data Auckland worker meshes
matched the original baseline, and three tiles completed with zero faults using
the expanded offline fixture. This fixture includes supporting map manifests,
elevation coverage and sea-depth rasters omitted from the historical benchmark.

## Findings and priority

| Priority | Finding | Evidence and consequence | Next action |
|---|---|---|---|
| P1, fixed here | Bridge sidewalk lookup rebuilds a whole-cell endpoint graph for every bridge on every tile. | `roads.deck_walks`: 657 calls over nine Auckland tiles; 4.88 s of 10.75 s profiled geometry time. Tokyo: 252 calls, 3.27 s of 11.48 s. | Share the immutable endpoint graph on its decoded source cell; keep width, sidewalk, and driving-side decisions per call. |
| P1, reduced here | First road content waits for expensive shared planning and thousands of map-array file operations. | Packed-mirror comparison: median first tile 43.54 → 29.04 s, 33.3% less time. Mirror files fall from 5,340 to 907. Bridge-deck planning, rather than building placement, dominates `items`. | Small arrays share one uncompressed archive per layer; large arrays remain mapped. Lane inputs are now reused too (separate comparison below). Next: deduplicate cell decoding and measure remaining neighbour/wall preparation. |
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

## Prepared lane endpoints and attributes

This follow-up continues from `df2cb877`, including its packed mirror. Each
decoded source cell now prepares float64 endpoints and lane attributes once.
The native solver consumes only first/last coordinates; zero- and one-point
features retain their short counts. Neighbourhood assembly keeps source order,
first-ID precedence, excluded roads that block nodes, and canonical home-cell
selection. Lane-tag column replacement refreshes the prepared inputs before a
first solve. Local solves with no unknown eligible mainline count skip their
neighbour reads because none can change the returned lane answers.

Three fresh sequential A/B pairs, using the same raw snapshot and settings as
above, compare this change directly with `df2cb877`:

| Unprofiled result | Checkpoint `df2cb877` | Prepared inputs |
|---|---:|---:|
| Cold first tile, pair 1 | 26.94 s | 26.34 s |
| Cold first tile, pair 2 | 29.12 s | 27.04 s |
| Cold first tile, pair 3 | 27.07 s | 24.94 s |
| Cold first tile, median | 27.07 s | 26.34 s |
| First tile `items`, median | 24.61 s | 23.57 s |
| First tile process CPU, median | 24.52 s | 23.06 s |

The difference between version medians is **0.72 s (2.7%)**. Each paired run
improves by 0.60–2.13 s; three pairs do not establish a universal latency bound.
All 66 paired mesh files match, all source/native/input hashes were checked,
and fault counts remain 7 / 0 / 0. The previous section's 29.04 s after-mirror
median was recorded in an earlier measurement batch: use this batch's matched
27.07 s baseline to attribute the lane change, rather than subtracting unrelated
run medians. No network-download or game/FPS improvement is claimed.

With raw input reads excluded, three passes over nine neighbouring cells show
lane preparation plus native solving at **0.896 → 0.301 s median (66.4% less)**.
Lane-answer hashes match in every pass. Native calls drop from 22 to 14;
coordinate points supplied drop from 1,825,823 to 356,688, and input arrays from
39,865,584 to 12,840,880 bytes. These figures describe cumulative input volume,
not peak memory or GPU transfer. Prepared arrays retain 72 bytes per feature:
3,893,976 bytes (3.71 MiB) across 40 decoded cells in this sample, plus headers.
They hold no neighbour references and are released with their source cells.

This explains the modest whole-job gain: much of the previously traced
`_lane_flow_local` time was waiting for neighbour data, not preparing or solving
lane inputs. Coalescing duplicate cell decodes and measuring remaining
neighbour/wall preparation are the next targets. No native algorithm, solver
pass order, precision, collision readiness or GPU path changes here.

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

For lane-input comparisons, use `df2cb877` as the baseline server in the cold
worker command above. To measure preparation and solving independently of I/O:

```powershell
python tools/bench_lane_flow.py --cache-dir '<raw-cache>' --server-dir '<checkpoint-df2cb877>/server' --out build/lane-before.json
python tools/bench_lane_flow.py --cache-dir '<raw-cache>' --out build/lane-after.json
```

Each pass starts with new decoded Features and caches, then solves nine cells
using their canonical neighbourhoods. Raw NPZ reads happen before timing, and
missing outer cells stay absent. Output includes source hashes, lane-answer
hashes, native input volume and retained array bytes. It does not measure disk
I/O or full road generation.

Current regression commands (from `server/tests`, real-data integration tests disabled):

```powershell
$env:BNGE_ROADGEO_REAL='0'
$env:PYTHONPATH='..'
python -B -m unittest test_lane_flow_inputs test_mapdata_mirror test_mapdata test_roads test_roadmesh test_bridge_scenarios test_roadwidth test_roadgeo
python -B -m unittest test_grading
```

The lane/cache/road suite runs 253 tests: 251 passed, two optional real-data
cases skipped. Grading runs separately: all 46 passed. Eight lane regressions
cover reversed/curved endpoints, short and excluded features, duplicate IDs,
canonical home-cell decisions, shared preparation, replaced cells/tags, and
skipping neighbour reads when no lane count can change. Eight mirror regressions
cover exact array bytes/dtypes, the 64 KiB boundary, mapped large arrays,
independent readers, many small fields, stale source stamps, missing/corrupt
files, coexistence with the old layout and disabled mirrors. Existing cache
tests cover localized coordinates, concurrent builders and pruning. The initial
graph-fix scenarios cover reversed bridge chains, different width/side settings
and replacement-cell connectivity.

Earlier combined-process runs exposed one pre-existing order-dependent
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
decoding deduplication and remaining neighbour/wall preparation next; retain the custom
build as a separate integration and acceptance exercise.
