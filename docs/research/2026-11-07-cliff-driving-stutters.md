# Cliff driving stutters — 2026-11-07

## Initial bounds fix

Preserve the server's per-piece cliff `bounds` when receiving road geotiles. `onGeoTile` previously copied shape, position, pivot and relief but discarded bounds. Initial import consequently used the conservative whole-tile collision check. A distant piece in a nearby tile could register with collision enabled, only for the subsequent range pass to disable it using its actual world box and call `postApply` again.

The existing pre-registration collision selection now receives the intended piece bounds. Subsequent range checks, speed lookahead, traffic foci, hysteresis and legacy payload fallback are unchanged. This applies to every cliff family. Render meshes, LODs, textures, contact geometry, road avoidance and streaming triangle limits are unchanged.

Five interleaved imports of each path using separate copies of the same 16,200-triangle cliff measured median total native setup of 14.86 ms for import-then-disable versus 11.29 ms for initially disabled collision. The avoided `postApply` itself had a 3.41 ms median. This is an isolated operation measurement, not a claim that every frame improves by that amount. Live inspection confirmed all 227 inspected cliff parts retained their bounds.

## Reversed uncached route

At the user's direction, horizontal camera travel was reversed 180 degrees into the denser cliffs. Each version used a fresh feature server with generated-content revisits disabled (`BNE_REVISIT=off`), a fresh core/roads client instance, the saved Hoover view, 120 seconds of settling, and 120 unmeasured frames. Source-data, OS, texture and shader caches were retained. No tests, screenshots or intentional heavy analysis ran during the timed captures.

The 45-second out/back camera path is original position plus `(-1200,-600,100)*sin(pi*t)`, with the original yaw variation `0.7*sin(2*pi*t)`. Original view and geographic anchor are in `build/hoover-stutter-study/view.json`. The vehicle stayed parked; this exercises camera streaming, not physical driving or a moving collision island. Both runs retained the 60 FPS foreground limit, with the background limit inactive.

| Metric | Control | Bounds retained |
|---|---:|---:|
| Average FPS | 39.83 | 41.73 |
| P95 frame time | 40.70 ms | 38.86 ms |
| P99 frame time | 57.50 ms | 53.45 ms |
| Worst frame | 87.06 ms | 72.14 ms |
| Frames over 50 ms | 40 | 28 |
| Cliff pieces imported | 76 | 85 |
| Total cliff import time | 378.68 ms | 563.40 ms |
| Worst cliff import | 6.74 ms | 49.96 ms |
| Collision rebuilds during capture | 0 | 0 |

These are single matching passes, not repeated statistical trials. Both started with 48 road tiles and ended with 69, but streaming delivery and imported piece counts differ. The fixed run's slower total imports and isolated 49.96 ms import show that native loading still hitches. The frame distribution improved in this comparison; attribution and repeatability remain limited. In particular, neither run measures collision-rebuild improvement.

Raw captures: `build/hoover-stutter-study/reverse-control.json` and `reverse-bounds.json`. Compact results and isolated measurements are committed alongside this note as `2026-11-07-cliff-driving-timings.json` when this change is committed.

## Discarded experiment and remaining stalls

Tested explicit collision subdivision at 1,024 triangles per internal part while preserving every finest-surface triangle and all render LODs. Mixed CDAE layout and legacy/client activation tests passed; 32 engine physics rays per trial matched the original surface exactly. However, the sample file grew from 197,829 to 329,722 bytes, imports slowed, and three paired native collision rebuilds showed no clear improvement (median 48.43 ms original, 49.28 ms subdivided). The experimental exporter, client flag and tests were removed. Its patch and measurements remain under `build/hoover-stutter-study/` for research only.

The initial forward-route comparisons are exploratory and are not used to validate the retained fix. Their camera path went into less demanding terrain. The original warm handoff capture also remains separate.

Hitch diagnostics on the reversed control show engine Ticking at 32.67–48.42 ms in several large frames, alongside render/shadow work; one frame had 20.03 ms Lua GC. The earlier diagnostic pass also recorded 22.38 ms GC. These costs are not established as cliff collision costs. Long driving stalls remain unresolved; future work should correlate native Ticking/render events with actual moving-vehicle or collision-focus captures before changing safety distances or collision scheduling.

## Verification and restoration

- 573 checks passed across roads, geotiles, core stubs and Lua scoping.
- New geotile tests cover distant and nearby bounds before registration and legacy payload fallback, without extra `postApply`.
- At the end of the initial camera experiment, only the geotile bounds receipt fix and its regression tests remained. Subsequent driving work is recorded below.
- Normal server caching, the original saved user view and profiler settings were restored; benchmark hooks and temporary probe objects were removed.
- Prior approved art commit remains `30369227`. This optimization is uncommitted and has not been pushed.

The benchmark reload helper now unloads the core again immediately before clearing its modules and loading it. The game's automatic core loader can otherwise recreate it while the server restarts, leaving instrumentation pointed at a different roads instance. Live module identity was checked for the final comparisons.


## Actual driving: collision rebuilds dominate the largest stalls

The user drove two 90-second passes and reported that the second still felt similarly slow.
The passive `beamngEarth_driveProbe` records real/simulation time, vehicle speed, profiler
marks and road work units without steering the vehicle or camera. It restores profiler
preferences and its hook when the capture stops.

| Metric | Original urgency | Piece-aware urgency |
|---|---:|---:|
| Frames >50 ms | 73 | 33 |
| Frames >100 ms | 40 | 13 |
| P99 frame | 88.38 ms | 39.19 ms |
| Worst frame | 181.84 ms | 148.03 ms |
| Global collision rebuilds | 38 | 14 |
| Time in global rebuilds | 2050.6 ms | 819.1 ms |
| Worst one-second simulation/wall ratio | 0.767 | 0.893 |
| Moving median speed | 25.96 m/s | 24.57 m/s |

36 of the original 40 frames above 100 ms contained a global collision rebuild. Both runs
kept simulation speed at 1 and had no paused frames. Overall simulation progress nearly
matched wall time, but individual seconds slowed markedly. These are manual repeated
passes, with different streaming/cache state; they do not establish controlled causality.

`roads.urgentColliders` now checks a cliff object's actual XY bounds, with the existing
six-second speed lookahead and 150 m minimum, instead of its tile center plus padding.
Stale, deleted, disabled or replaced queued colliders cannot force a rebuild. Roads and
legacy shapes retain their conservative fallback. No activation distance, hysteresis,
normal rebuild interval, safety policy or simulation-speed setting was reduced.

## User-authorized simplified collision experiment

The user explicitly authorized testing simplified collision meshes after the urgency-only
fix still felt slow. The experiment is opt-in through `BNE_CLIFF_COLLISION_SIMPLIFY=1`.
Its BNE-prefixed switch participates in the existing revisit code key. Defaults retain
exact visible collision. The live feature server currently has the experiment enabled.

`cliffcollision.py` calls meshoptimizer through the native Rust module (version 152).
It simplifies each material separately with locked borders, no welding, no pruning and
original vertex positions. Sharp fractures above 60 degrees and their touching faces are
locked, with fold preservation enabled. Target: 10% of the original faces, limited by an
absolute quadric error of 0.08 m. This is NOT a global Hausdorff bound. Pieces that cannot
save at least 25% retain exact visible collision. Render mesh arrays, LODs, materials,
textures and placement are unchanged.

The CDAE exporter supports a collision detail alongside multiple split render LODs.
The server sends `collisionMesh: true` only for shapes containing that detail. The client
preserves it on receipt and placement, then records it on the loaded object so range
reactivation selects the correct collision type. Old cached shapes use their existing
visible collision behavior.

The initial clustering reducer barely reduced the fractured cliffs and was rejected.
An unprotected edge-collapse trial missed two sampled contact rays. Explicitly retaining
sharp fractures and adjacent faces eliminated those sample misses. In the final native
implementation, 3,068 valid isolated in-game rays across twelve sampled active cliffs all
hit the simplified surface. Mean absolute contact offset was roughly 2 cm; per-piece p95
was roughly 4–6 cm; largest sampled offset 11.5 cm. Sampling does not prove every surface
point or every vehicle contact is safe. The isolated probe objects were removed.

A same-scene alternating rebuild test replaced only collision while leaving all original
render objects present. Seventy eligible active cliff pieces dropped from 1,044,545 to
292,881 triangles (72%). Exact rebuild times were 46.44, 39.47 and 40.65 ms; simplified
were 47.05, 18.89 and 18.89 ms. First-use setup remains significant. Original collision
was restored immediately after this isolated test. This measures synchronous rebuild
cost, not total frame or subsequent physics-step cost.

The release native build and updated Lua were then installed for a real driving capture.
A world refresh required holding simulation and restoring the saved location/camera;
this refresh is outside the timed run. Windows native version 152 was built in release
mode. The Linux binary has not been rebuilt; the opt-in feature requires version 152,
while default behavior continues to accept the existing version 151. No commit or push.

Verification: 109 focused Python tests (collision, CDAE, LOD, streaming and generation),
plus 586 Lua assertions (roads, geotiles, core stubs and scoping), pass. Tests cover original
vertices, boundaries/material seams, opposing disconnected sheets and holes, exact sharp
fracture faces, identical render mesh data, protocol capability and collision reactivation.

Artifacts: `build/driving-slowdown-study/` holds the raw driving captures, contact probes,
rebuild A/B inventory/timings, saved pre-refresh view, deployment and benchmark scripts.


### First simplified-collision driving capture

The user reported "Somewhat smoother, but still slow". In 90 seconds: 4,130 frames,
45 above 50 ms, 6 above 100 ms, p99 51.15 ms, worst 162.02 ms. Eighteen collision
rebuilds totaled 417.73 ms (16–29 ms each). Median moving speed 25.20 m/s; no paused
frames. Worst one-second simulation/wall ratio 0.743, with four seconds below 0.9.
This run included new content streaming after the refresh and is not a controlled warm
repeat of the earlier passes.

All six >100 ms frames had no road work unit or collision rebuild; the engine's Ticking
category took roughly 79–134 ms. They coincided with Forest updates. Collision rebuilding
is measurably cheaper, but this has exposed another major stall source. Investigation
continues with touched-shape instrumentation rather than changing rendering density.


### Rejected Forest population-budget trial

A 35-second trace found no first-time shape paths, but large Forest edits preceded
native Ticking spikes: 28 already-seen models / 628 additions + 110 removals preceded
122 ms; 30 models / 432 additions + 92 removals preceded 80 ms. Reducing the existing
SHAPE_ITEMS_PER_FRAME from 4000 to 256 was tested live, then restored. The user reported
"About the same". The 90-second run was much faster (median 54.86 m/s vs 25.20), with
70 frames >50 ms, 15 >100 ms and worst 316 ms. It is not an A/B attribution test.
Pending Forest tiles reached 27 during new streaming but later dropped to 6, none near
the vehicle. No Forest source change is retained.

The game's MCP profiler_run accepted a 20-second Optick capture and logged a save, but
no file was written (including FS:fileExists). This installed build does not provide a
usable capture through that API; exported function availability is not sufficient.


## Retained Forest scheduling fix and final state

A second trace confirmed the population budget was measuring the wrong cost for sparse
private rock models: 61 touched models (56 talus) / 1,305 instances preceded a 194 ms tick;
29 talus models / 473 instances preceded 139 ms. No first-time shape paths were involved.
The fix bounds DISTINCT models, including already-loaded ones, rather than further
shrinking the item population budget.

While driving (`frameMs < LOADING_FRAME_MS`), Forest work runs on consecutive frames,
admitting one model per frame. The item allowance changes from 750 every three frames
to 250 every frame, keeping the same overall allowance. The existing time/population
budgets remain. Calm loading keeps its original batching and permits multiple models.
Objects, transforms, ranks, densities, render models and collision distances are unchanged.
The model limit applies to both additions and removals. Tests verify warm and cold paths,
calm throughput, and identical final placement/collision.

In the 60-second model trace, the maximum engine Ticking time was 9.81 ms, with at most
one touched model per frame. The associated 90-second driving run had one frame >100 ms
(worst 102.57 ms), 57 >50 ms, p99 53.58 ms, and 23 collision rebuilds totaling 406.27 ms.
Median moving speed was 41.22 m/s. It is not a controlled route/speed comparison with the
previous captures. The user reported: "Smoother; objects look normal". Occasional GC,
render and other stalls remain; this is not a claim that every hitch is fixed.

Pending Forest tiles reached 79 during streaming, then fell to 65 with one near tile
pending at an intermediate check. Visual user feedback was normal. The near-first ordering
is unchanged. Continue checking high-speed dense-city/forest behavior before release;
this session's validation is the cliff-heavy desert drive.

All 109 focused Python tests and 741 Lua assertions pass. The Windows release module,
roads.lua and trees.lua are installed. The simplified collision experiment remains enabled
in the current feature server process (`BNE_CLIFF_COLLISION_SIMPLIFY=1`, PID recorded in
build/debug-layer-server.json); it is off by default on a new normal launch. The installed
Forest scheduler is permanent source behavior. For this already-loaded session, an equivalent
lightweight admission adapter avoids another world refresh; it reuses scratch storage and
the normal module load uses the installed implementation. Its saved debugging globals and
the driveProbe extension/file were removed; all profiling/temporary probe hooks are stopped.
The game was paused by the time cleanup ran; cleanup preserved that state.

No commit or push. Do not accidentally ship the unchanged Linux native binary as supporting
the opt-in collision feature; version 152 must be built there first. Normal default behavior
continues to require only version 151. The discarded 256-item trial has no retained source
or runtime setting.
