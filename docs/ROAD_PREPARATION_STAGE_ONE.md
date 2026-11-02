# Shared road preparation: first-stage PR

Final PR preparation on 2 November 2026 verified this branch against freshly
fetched upstream dev `b41b4b04`. The shared bridge-cache docstring now matches
PR #3, eliminating their textual merge conflict. Optional dependency telemetry
also tolerates absent worker state, preserving the original build exception and
cleanup path; an additional regression covers that case.

The latest focused selection ran 307 cases: 304 passed and 3 optional skips.
The resolved four-PR integration passed 1,861 selected Python cases (10 optional
skips) and 4,117 Lua assertions, including affected-module reruns. Its worldgen
worker-argument/cache-timer resolution is recorded in merge `bd6d4084`; retain
both progress/detail/view arguments and the new `submitted_at` when integrating.
[Full merge preparation and remaining acceptance](https://github.com/PaperboyNZ/BeamNG.EarthBuildTest/blob/testing/building-system/docs/PR_DEV_PREPARATION_20261102.md).
The historical A/B measurements below do not constitute fresh benchmark or native
acceptance of the final four-PR combination. The open benchmark gates still apply.

Paused for PR preparation on 2 November 2026. Base: upstream `dev`
[`b41b4b04`](https://github.com/TecnaGamer/BeamNG.Earth/commit/b41b4b04edffab6083e78b0c2ef41982869287b2).
The initial architectural review used `0c790738`; upstream added packed map-array
storage before this implementation began. The packed format and the current
upstream lane-input cache are retained unchanged.

## Problem and change

Concurrent `MapData.cell()` calls could load and decode the same uncached layer
independently, creating separate `Features` objects and derived memo tables.
Each bridge sidewalk lookup also reconstructed its source cell's endpoint graph.

- Share a future per cell/layer through loading, decoding and publication. All
  admitted callers receive the same object, even if the memory cache evicts it
  before they consume it. A failed producer wakes its waiters and a later request
  can retry. Neighbor hooks remain lazy; cache locks do not surround I/O or decode.
- Retain the existing endpoint graph on its decoded cell. Geometry construction,
  iteration order, rounding and chain search are unchanged. Width, sidewalk and
  driving-side decisions continue to use each call's specifications.
- Reset stage timing on revisit hits and report `cache_restore`. Resume the skin's
  own timer after nested content loading. Measure bridge/dependency preparation as
  `decks`, separately from building items.
- Record actual road-worker entry/completion and queue duration. Slow-road logs
  use that duration when available. Expose decoded-load, coalescing and wait
  counters for the job's process window.
- Add offline benchmark tools and concurrency/timing/bridge regression coverage.

These are changes to caching and orchestration around the existing native kernels.
No native API, binary, geometry rule, client protocol requirement or collision
publication behavior changes. This PR does not implement cross-process producer
ownership, priority inheritance or the future deadline scheduler.

The endpoint cache and its behavioral cases build on
[test-fork PR #3](https://github.com/PaperboyNZ/BeamNG.EarthBuildTest/pull/3).
Its older packed-mirror/lane changes are omitted because the current upstream has
its own implementations. The geometry benchmark is adapted from that PR.
Security review: the ported source, behavioral tests and benchmark code were read
in full; they add no external downloads, credential reads, dependency changes,
workflow changes or executable binaries. Benchmark inputs are physical copies;
no test server uses links to a live cache.

## Completed validation

The selected regression suite ran **386 tests: 382 passed, 4 skipped**, across
15 modules. This covers map loading, concurrency/failure/eviction, road generation,
bridge scenarios and clearance, grading, geotiles, road meshes, planning caches,
rail tags, process pools and protocol agreement. The four skips are optional Lua
checks and the original real-cache roadgeo cases. The latter were replaced for
this comparison by the isolated real-data worker run below, not enabled against
the user's live cache.

The sandbox stalled two tests during Windows asyncio socket-pair creation. They
were stopped and rerun with localhost access; both modules passed. An initial
test-only nonblocking lock check was corrected to allow another thread's brief
admission lock. Temporary test folders were redirected into the audit workspace
because the sandbox denied cleanup in its default temporary folder.

### One exploratory A/B pair

Both versions used fresh physical copies of the same Auckland source fixture,
with cold derived caches, one spawned worker, revisit disabled and no network.
Road grading, measured widths, buildings, sea and gas stations were enabled;
airports were disabled. Initialization and extra terrain sampling were excluded
from the timer. Both loaded the same native API 140 binary. OS caches and external
machine activity were not controlled.

| Road job | Upstream dev | Initial candidate | Observed reduction |
|---|---:|---:|---:|
| First tile | 19.569 s | 18.644 s | 4.7% |
| Neighbor 1 | 2.076 s | 1.563 s | 24.7% |
| Neighbor 2 | 1.595 s | 1.102 s | 30.9% |
| Three-tile total | 23.241 s | 21.309 s | 8.3% |

All compared output matched exactly:

- 22 CDAE meshes, including visual and collision meshes;
- 6 JSON files containing placements/content metadata;
- 3 serialized skin-input files;
- 12,675 sampled graded-terrain heights, compared as float64 bytes.

Source-input hashes matched and both runs recorded zero missing-data faults,
including the terrain check. The candidate recorded **zero coalesced loads** in
this sequential three-tile run. Therefore this run does not establish an
end-to-end benefit from concurrent decoding reuse. The controlled contention test
does establish that eight simultaneous readers share one load/decode.

**This is one exploratory pair, not a stable speedup, P95 or FPS claim.** After it,
only the nested-timer restoration, stage categorization and timer documentation
changed in production code. Geometry computation did not change. The final PR
revision has passed the relevant regression tests but has not been rebenchmarked.
The user requested the pause before repeated A/B, warm-cache and expanded geometry
corpus runs. Those checks remain open.

[Machine-readable evidence](ROAD_PREPARATION_STAGE_ONE.json) records measured
source fingerprints, source/native hashes, output hashes, timings and test counts.
Large source/cache snapshots and raw logs remain outside Git in the local
`artifacts/road-stage-one-b41b4b04` audit folder.

## Reproduction

Use the same Python environment and source fixture for both versions. The fixture
must contain physical `mapdata`, `dem`, `pagesdata` and `seacarve` directories. The
output directory must not exist. No game needs to be running.

```text
python tools/bench_road_preparation.py --server-dir BASELINE/server --snapshot FIXTURE --out RESULTS/baseline-1 --label baseline-1
python tools/bench_road_preparation.py --server-dir CANDIDATE/server --snapshot FIXTURE --out RESULTS/candidate-1 --label candidate-1
```

For a fresh process using warm derived data, add `--warm-from RESULTS/baseline-1`
or the corresponding candidate run. Compare `geometry_sha256`,
`skin_input_sha256` and `terrain` in the JSON outputs, as well as faults and source
identity. Alternate A/B order and run benchmarks sequentially. Do not mix timings
from different source revisions.

`tools/bench_road_geometry.py` supports the larger geometry-only corpus. Set
`BNGE_BENCH_ROOT` to the checkout/export being measured, and pass `--at`,
`--country`, `--cache-dir`, `--out`, `--repeat 3`. It reads raw source arrays only,
using fixed flat ground and urban classification to isolate road/mesh building.
Its hashes include chunks, sidewalks, bridge context, positions, triangles, UVs
and materials. It does not replace the full-worker parity check.

## Review considerations

- The endpoint graph retains additional per-cell memory. Measure peak private
  memory and mapped bytes on the small-machine profile before merging broadly.
- In-flight sharing is within one `MapData` instance/process. It uses the original
  producer's request kind; it does not promote a background producer's priority.
  Validate mixed demand/background failures before expanding this mechanism.
- Counter durations aggregate overlapping threads and can include background
  activity during a road job. They are not an additive critical-path breakdown.
- The timeout policy still uses the existing executor-running behavior. Actual
  worker timestamps improve completed-job diagnostics, not watchdog liveness.

## Next stages, in order

1. **Finish this PR's acceptance evidence.** Freeze the candidate source. Run five
   alternating cold A/B pairs against the pinned dev, then warm-derived pairs.
   Expand geometry comparison to nine tiles each in Auckland and Tokyo with three
   passes; the Tokyo source fixture is already copied. Benchmark endpoint-only
   and decode-only variants, plus actual concurrent readers. Require exact output
   equality, no missing-data faults and no concurrency/memory regression. Capture
   a live drive before claiming gameplay benefit. Recheck upstream before opening
   or rebasing the PR; do not credit an upstream improvement to this patch.

2. **Pilot shared production for one expensive planning stage.** Profile the
   final baseline first. If duplicate planning remains material, implement a
   native owner registry for versioned prepared road cells/canonical plans, with
   shared completion, source-dependency manifests, bounded memory, failure recovery
   and fenced publication. Preserve canonical bridge/ground ownership and treat
   coupled solves as one unit where needed. Demonstrate one healthy producer per
   key across terrain/road consumers before widening the integration.

3. **Propagate driving deadlines through ready work.** Add actual start/heartbeat
   acknowledgement, estimated remaining cost and vehicle-arrival deadlines. Promote
   shared prerequisites, dispatch only dependency-ready work, cancel stale
   anchor-specific consumers and retain reusable work with live subscribers.
   Validate multi-vehicle demand and worker failure. Track P95/P99 physics-ready
   latency, solid-road lead, edge holds, duplicate CPU and peak memory.

4. **Optimize client delivery only when a live trace warrants it.** Separate
   optional decoration from a coherent terrain/road/bridge/collision revision.
   Preserve occupied-collider holds, edge protection and terrain-swap quiet time.
   Engine imports and collision rebuilds need their own frame-time measurement.
   Building mesh time was tiny in this sample, so progressive publication alone
   is not the next first-use optimization.

The implementation and benchmark expansion are paused. This document is the
handoff for review and resumption, not authorization to start the later stages.
