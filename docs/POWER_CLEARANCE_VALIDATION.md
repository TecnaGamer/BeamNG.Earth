# Native power-line road avoidance

Road geotiles still spent time placing power-line towers and poles off public
carriageways. For every vertex, the original consumer constructed NumPy
point-by-segment arrays once per road, then repeated that work for the ordered
search candidates. Profiling identified this work inside `powerlines._off_roads`.

The consumer now packs the nearby segments once per vertex and calls
`earthfast.power_off_roads`. The native loop checks the initial position, the
straight-away candidate, then the existing search offsets in their original
order. It returns at the first acceptable candidate; when none is acceptable,
it retains the first candidate with the greatest clearance. Temporary storage
is linear in the nearby segments rather than candidates times segments.

## Numerical and lifecycle contract

The caller still supplies the same roads, half-widths, offsets and clearance.
Search offsets are computed by the existing NumPy expressions, preserving their
trigonometric rounding. Road ends are explicit: the nearest segment is chosen
by distance *within each road*, before roads are compared by distance minus
half-width. Comparing flattened segment margins first can change a rounding tie.

The denominator floor (`1e-12`), centreline direction fallback (`1e-6`) and accepted
candidate margin (`-1e-6`) remain the original rules. Ordinary unchanged points
and straight moves retain the GIL. Substantial fallback searches use the existing
native work-threshold helper to release it. No global caches or mutable native
state are added. Inputs are checked for shape, finite values, ordered road ends
and one half-width per road.

Native API **159** is required: rebuild with `python tools/build_native.py`.
The tracked platform binary is not part of the change. No new dependencies,
protocol changes, road-facts changes or grading-plan changes are introduced.

## Reproduction

`tools/profile_road_build.py` measures the current `blockworker.roads_geo` path:
content, anchor skin and automatic exact variants. It uses temporary output
folders outside the game and records wall/process/thread CPU time, source
versions, input-fault counts and every generated file's SHA-256. Exceptions and
paired output mismatches return nonzero. Empty offline data was checked to
produce a failed report, rather than a successful fast timing.

Populate a dedicated cache with normal provider limits, then measure offline:

```text
python tools/profile_road_build.py --at=36.17,-115.145 --cache-dir build/profile-cache/city --out build/profiles/city-prime.json --online --repeat 2
python tools/profile_road_build.py --at=36.17,-115.145 --cache-dir build/profile-cache/city --out build/profiles/city-paired.json --compare-power-ref 1de7cf62 --repeat 9
```

Use `--profile` in a separate run for cProfile attribution; its timings include
profiler overhead. `--compare-power-ref` loads the original Python consumer from
the explicit Git revision and alternates it with the proposed consumer in one
worker, using the same proposed native build for both. This isolates the
consumer change from unrelated native-build and process-start differences.
There is one warmup per variant, excluded from comparisons, followed by the requested pairs.
The normal mode forces geometry generation with the generated revisit cache
disabled; source data and worker memory remain warm. Use `--revisit` separately
to check generated-cache restoration and inspect `geotile_cache_delta` for actual
hits. `--source-root` and `--native-dir` also allow separate-process comparisons.

The focused benchmark includes Python input packing but holds road lookup
constant with synthetic centrelines:

```text
python tools/benchmark_power_clearance.py --baseline-ref 1de7cf62 --points 64 --repeat 9 --json build/profiles/power-clearance.json
```

It checks both consumers' positions on every repetition. The old NumPy algorithm
is retained only in tests and explicit benchmark baselines, not as a production
fallback.

## Measurements on the current base

Base: `1de7cf62`; implementation: `9e7a53bb`, native 159. Windows x64, Core
i5-10600 (6 cores / 12 threads), Python 3.10.11, NumPy 2.2.3, Rust 1.99.1 / MSVC
target, Release with LTO. The machine was not an isolated benchmark host.

Nine alternating pairs per location, after the warmups; median whole-geotile
wall time, including output writes and native-input packing:

| Cached-source geometry workload | Original consumer | Native consumer | Change |
| --- | ---: | ---: | ---: |
| Urban, Las Vegas (36.17, -115.145) | 1158.5 ms | 1040.3 ms | -10.2% |
| Rural road (36.494818, -116.449960) | 86.1 ms | 83.8 ms | -2.6% |
| Highway sample (35.793610, -115.319027) | 306.6 ms | 310.3 ms | +1.2% |

The small rural/highway differences fall within the observed run ranges; they
do not establish an improvement there. The urban gain is specific to this
workload. All **27 timed pairs** matched byte for byte: **171 generated-file
comparisons**, including compressed shapes, JSON and the skin-input pickle.

The urban build reports 57 propagated input-fault markers per measured build,
including the existing missing-lidar/rule fallback. Both variants have matching
outputs and fault counts. This is an explicitly incomplete-input workload, not
a fully cached first-world acceptance test. The rural and highway timed rows
report no input faults. A separate rural `--revisit` control confirms actual
content-cache hits and equal output from both consumers.

For the complete avoidance consumer on 64 synthetic vertices (road lookup held
constant), median speedups were 5.49x for a straight road, 7.98x between dual
carriageways, 8.05x at crossroads, 8.89x on the dense network, and 5.30x when no
search candidate is clear. Already-clear vertices were 1.57x faster; no roads
was 1.09x. These are component measurements, not full-world speedups. The
preflight coordinates were bit-identical on all seven fixtures; every timed
result also matched the original within `2e-10` m.

[Raw timings, file hashes, source versions and profiling attribution](research/2026-11-07-power-clearance.json)
include the excluded warmups and sample ranges. Measurements made before
rebasing onto the current base are excluded from this report.

## Regression coverage

The native binding has 12 tests, including 240 seeded mixed networks, the
within-road versus between-road rounding tie, repeated/zero-length segments,
centreline placement, boundary tolerances, ordered fallback, malformed inputs,
noncontiguous arrays and concurrent deterministic searches. The 17 existing
power-line consumer/layout tests also pass.

```text
python tools/run_tests.py test_power_clearance test_powerlines test_roadgeo test_blockworker test_roads test_roadmesh test_furniture test_gasstations test_geotiles --par 3 --timeout 600
python tools/quality_gate.py --par 3
```

The current Windows foundation Python modules passed **43/45**. `test_cache`
has two symlink-privilege errors (WinError 1314); `test_process_lifecycle` has
three `guard.json` fixture readiness timeouts. Both sets reproduce on unmodified
`1de7cf62` with its tracked native 158. The quality gate is not claimed green.

Seven additional geometry modules pass, with their existing optional skips.
The expanded run also finds the existing `test_bake_formats` plans-golden
mismatch: actual `a1193c33cc9fa48182d77405563750560faddc14`, expected
`a25f23173ef21b02ba1b358e9b277a48ebe76f75`, plans format 18. The same mismatch
reproduces on the unmodified base; the facts-format check passes. This change
does not refresh that golden or bump the plans/facts formats.

All 97 offline Lua modules, both Node map-math tests and eight Rust server-list
tests pass. These are local checks, not gameplay or Linux runtime acceptance.

## Scope and acceptance

This measures a road-building component and controlled road-geotile workloads.
It does not measure BeamNG frame times, GPU/rendering, imagery, a complete first
world load, or driving with multiple worker processes. The profiling setup has
roads, grading, road widths, surface, sidewalks and buildings enabled; airports,
sea and gas stations are disabled. No game was started.

Online cache population is data acquisition, not a controlled before/after
network benchmark. First-worker planning and warm geometry builds must be kept
separate. Input-fault counts can include repeated propagation of a missing lidar
answer, not one missing download per count. A tile with reported input faults is
not evidence of a fully cached or complete world.

Game acceptance should repeat a fixed drive with the same settings and data,
checking first-load latency, road-loading throughput, frame-time percentiles and
power-line placement at road edges, junctions and rebase seams. Repeat on Linux
and smaller CPUs before making broader performance claims.
