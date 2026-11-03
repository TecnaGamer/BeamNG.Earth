# Shared road-plan bundle experiment — 2026-11-02

The pinned bundle path eliminated repeated source queries for two clients and
produced exactly matching road-grading heights in different anchor frames. It did
**not** improve total cold execution time on this small synthetic fixture: the
median shared workflow took **234.79 ms**, compared with **27.10 ms** for two
independent cold graders. This is evidence of reusable, consistent road plans;
it is not evidence of faster world loading or improved gameplay FPS.

Run the reproducible benchmark from a development checkout with the native
extension built and the server dependencies installed:

```powershell
.\.venv\Scripts\python.exe tools/benchmark_world_bundle.py --iterations 5 --output docs/research/2026-11-02-world-bundle.json
```

The [machine-readable results](2026-11-02-world-bundle.json) retain all five runs,
fixture coordinates, generator identity, transfer counts and consistency checks.
The script creates temporary producer and client caches, restores the environment,
and fails if either client queries source road/elevation data, heights diverge,
or a warm fetch makes network requests.

## Method

This uses the real `grading.Grader`, native geometry code, `PlanCache` persistence,
strict bundle exporter/loader, and loopback HTTP service/fetch implementation.
The inputs are explicitly synthetic: two 600 m roads from the existing
`test_roads.features` fixture and `test_grading.FakeMD`, one residential and one
secondary, over flat 10 m elevation. There are 256 geographic evaluation points.
One anchor is at the fixture origin (39.2834, -76.5934); the other shifts latitude
and longitude by 0.01 degrees. Each client has an independent grader and disk
destination, in the same Python process.

The baseline builds plans independently for each client. The shared workflow
builds and persists plans once, exports the immutable bundle, starts a loopback
server, downloads into two client directories concurrently, and evaluates both
pinned readers. Cold wall time includes these setup and validation stages and
ends after both readers have evaluated their samples. Warm fetch timing measures
validation/reuse of both already downloaded bundles, without a further grading
pass. Individual stage medians do not necessarily add to the total median.

## Results

| Measurement | Baseline: two independent clients | Shared: producer plus two clients |
|---|---:|---:|
| Median cold wall time | 27.10 ms | 234.79 ms |
| Median time inside the two client `apply` calls | 27.07 ms | 28.72 ms |
| Source road-cell calls per iteration | 38 (19 each) | 19 producer; 0 clients |
| Source elevation calls per iteration | 8 (4 each) | 4 producer; 0 clients |
| Plan blobs read by clients | Not applicable | 2 each |
| Cold bundle HTTP requests | Not applicable | 6 total (3 each) |
| Cold HTTP payload bytes | Not applicable | 26,450 total (13,225 each) |
| Warm fetch HTTP requests | Not applicable | 0 |
| Median warm validation/fetch time | Not applicable | 38.30 ms |

Shared-stage medians were 81.43 ms for producer grading/persistence, 28.51 ms for
export, 31.35 ms for server setup/full bundle validation, and 59.84 ms for the two
concurrent downloads. The shared cold wall time ranged from 191.77 to 255.72 ms.
The baseline grading time ranged from 22.20 to 37.75 ms.

All five iterations had exactly equal geographic sample heights across the
baseline, producer and both pinned clients, with nontrivial road grading. Manifest
and height hashes were also identical across iterations:

```text
manifest: e74ec34b1c7e9e954cc40eeeda0e2d2e43338213b6fcd4c661e2df2e4bcb9aca
heights:  a3bd1fc7647b5884a30b1817463b5eb96fa06ba9631c7425d9b18722b8da13ed
```

The tested generator identity was
`a5225b0efe5d6d41-116-44136fa355b3678a`, with native version 116 and NumPy 2.5.4.
The machine ran Python 3.14.7 on Windows 11 build 26200, with an AMD Ryzen 7 7700X
and 16 logical processors. The core integration was commit `550f2183`; the
transport and benchmark files were present as local changes during measurement.

## Limits and next measurements

Each iteration begins with empty application caches, but the operating-system
filesystem cache is not flushed, and the workstation is not an isolated benchmark
host. These sub-second timings should not be extrapolated to real cities or
production traffic. Source calls are in-memory fixture operations, not provider
downloads; their elimination does not quantify internet bandwidth savings.

The bundle pins **road grading plans**. It does not pin raw DEM tiles, all
building/bridge/tunnel geometry, complete terrain collision, or vehicle state.
`input_digest` is an operator-declared source snapshot identity. No BeamNG.drive
or BeamMP gameplay session, remote network, packet-loss condition, cross-machine
comparison, FPS, frame-time spike or full-world loading time was measured here.

This result supports keeping the feature opt-in while testing a representative
city with expensive source queries, more clients and reused prepared bundles.
That test should separately measure producer amortization, downloaded bytes,
client load time and engine integration cost before claiming a performance gain.
