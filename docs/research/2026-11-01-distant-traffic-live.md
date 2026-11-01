# Distant traffic: continuous streaming, density and range (#565)

This follows the cached-road probe in `2026-11-01-distant-traffic-stream.md`. Its protocol 1 and 64-car figures are
historical. The revised feature is still an unpublished development branch, based on dev `3ed3950c` (native 122).

## Production integration

The actual companion and normal core now negotiate and continuously stream the feature. An initial live failure
exposed a nonexistent `Server.roads` predicate; `traffic`, `mapdata` and the worker pool are the existing gates.
The regression fixture no longer invents a `roads` attribute. No snapshot adapter is used in these live checks.

The first 64-car version spent much of its budget outside the view: 35 actors in the camera's rectangle, versus
310 eligible candidates. Selection now uses the actual 3D rectangular FOV and window aspect, with a 15% margin and
visible candidates first. The same ceiling then put 59 actors in view. These are mathematical eligibility counts,
not GPU occlusion results: terrain or buildings can still hide an eligible plane.

The user wanted a denser evening city, farther background motion and occasional traffic on minor/unpaved roads.
A fixed count independent of the roads cannot express that. The source supplies peak spacing by class; the client
derives candidate population from route length, thins it by local-hour density, then shares a bounded rendering
budget among distance bands. A lone quiet road does not receive the city's whole budget.

## Revised data and renderer

- Optional capability `trafficVisuals: 2`; the base protocol stays 4. Old clients/servers do not start the stream.
- Near Z14 stream to 5.5 km includes major, local and unpaved roads; major-only far stream reaches 11 km with an
  inner 4.5 km skip. Both use the existing geotile Tracker and worker pool, one job in flight per tracker. Geo
  trackers use `inner`, not the non-geo `skip_m`; a regression verifies exclusion of inner tiles from the far stream.
- Stable 2 km chunks bound long ways. Shared road rules, lane shifts, widths, handedness, speed and sampled lane
  heights are retained. Separate XY/relief simplification reduces flat-road point counts. Near/far routes share ids
  and ownership; dropping one stream cannot delete a route owned by the other.
- Peak spacing ranges from 50–100 m per car/direction on major roads to 220 m residential, 500 m service, and at
  least 1600 m unpaved / 3000 m track. This is a cosmetic impression, not observed traffic. Short quiet routes may
  have zero slots. Ground roads only: bridge/tunnel profiles remain deferred.
- Major routes fade out by 10 km, local routes by 6 km. Physical traffic exclusion is unchanged: its ground box
  plus 100 m, followed by 400 m fade. End and temporal fades remain. Data caps and wire fields are in server/README.
- Ceiling 512 cosmetic cars / 1024 emissive planes, including retiring actors. Range shares follow eligible demand
  in view (<2.5 km, 2.5–6 km, 6 km+), with a 20% distant reservation when that demand exists. Local-hour density also
  scales the ceiling. This is a ceiling, not a target for every scene.
- Bounded heaps replace city-sized sorted candidate arrays. Preparation/selection share a 300 us cooperative
  slice, up to 150 us / 16 route steps for preparation; native pool growth stays at 2 cars/frame. Engine calls can
  overrun an individual slice, so this is not a hard real-time guarantee. Governor charges preparation work.
- Placement and path/facing/fade calculations run at distance-dependent 45/24/12 Hz schedules. Unchanged paused
  objects avoid native render-data uploads. Every LOD is forced into the new origin on its first rebase frame.
  Disabled idle pools retire 8 pairs/frame after 30 s rather than deleting the whole pool in one frame.

## Live observations

At the Sochi view, the 256-car revision warmed to 44 near / 115 far tiles, 4168 unique routes, 85158 prepared points
and no pending points. The source contained paved, dirt and gravel roads. A capture counted 227 eligible actors in
view: 20 below 2.5 km, 139 in the middle band and 68 beyond 6 km; the farthest was 9.837 km. The user confirmed the
farther lights appeared and the effect looked very good, but wanted still more rush-hour traffic. The subsequent
512 ceiling reached about 497 eligible cars in the same view before its final timing comparison.

Camera/player geography and running manual time were preserved for installation. A forced near-origin rebase test
was unsuitable: existing `camshift.lua` distinguishes world positions from direction vectors assuming the player
is at least 2 km from the origin. This test violated that assumption and tilted the camera; the view was restored.
The route store retained 159 tiles / 4168 routes, but seamless normal-driving rebase behavior is not proven by this
test. Use a valid distant-origin driving scenario for that remaining check. No camera subsystem change is included.

Restarting the companion also produced old-session pooled-shape errors while upstream road meshes were replaced
and the old session released. They did not name traffic glow assets. A clean-start/reconnect pass is still needed;
the whole session should not be described as error-free.

## Timing and verification

The temporary `probes/traffic_metrics.lua` records bounded frame samples, camera position/direction and settings
metadata. `Render` is a CPU scope, not an independent GPU measurement. Physical traffic and streaming can vary
between samples; do not infer an exact FPS percentage from module timing alone.

Earlier 256-car stationary-camera 20 s samples: module mean 1.192 ms on, 0.016 ms off, 1.104 ms on again (on p95
1.690 / 1.659 ms). Full-frame means 18.232 / 17.533 / 19.313 ms varied. Time was reset during that series; background
limiter state was not captured. Those samples are preliminary, not a controlled FPS comparison.

With both limiter switches recorded off, the final 512-ceiling on/off/on series had 1226 / 1275 / 1190 frames over
three 20 s windows. Camera position, direction and FOV were unchanged; physical vehicle count stayed 21, with time
near 18:07. The first on window started and ended at 497 actors. The second included re-enable growth 453→503,
so it is not exclusively a warmed steady-state sample.

| CPU / frame measurement, ms | On | Off | On again |
|---|---:|---:|---:|
| Module mean | 1.278 | 0.018 | 1.422 |
| Module p95 | 3.160 | 0.031 | 3.043 |
| Full frame median | 14.892 | 13.739 | 15.387 |
| Full frame mean | 16.300 | 15.665 | 16.783 |
| Full frame p95 | 24.915 | 19.772 | 26.685 |
| Render CPU mean | 6.423 | 6.402 | 6.482 |

The off window contained a 949.753 ms frame, including 928.314 ms Lua; the traffic module's maximum in that entire
window was 0.191 ms. The outlier is retained in the figures, not attributed to traffic. Hidden pooled planes were
still allocated during the short off window; idle object retirement starts later. These short live samples show
a measurable CPU cost, with additional simulation/engine variance; they do not establish an exact universal FPS
loss or GPU cost. Initial limiter/effect settings were restored after the test and the recorder unloaded. The user
also disabled the background limiter, so the restored value in this run was false.

Checks: 20 Python feature tests; full protocol 32 run / 31 passed / 1 skipped; Lua path 145, renderer 1680, core 280,
settings 285 assertions. Python AST, LuaJIT compilation (including the core's local-variable limit) and whitespace
checks passed. Coverage includes ring exclusion, chunk ids/relief, class spacing, near/far owner sharing, bounded
queues, FOV selection, clock thinning, paused uploads, first-frame far rebase placement and incremental idle cleanup.

Still required before an upstream-ready claim: normal driving across the physical-traffic transition, valid actual
rebase/teleport continuity, clean reconnect/mission cleanup, more terrain and high-latitude/dual-focus budgets,
graphics-menu interaction and broader CPU/GPU measurements. No runtime dependency was added.

## Level reload and city recovery

The user accepted the approximately 500-car density and smoothness, then reported that the city geometry had
disappeared, leaving roads over flat ground. Before a requested level restart, a scene/file audit found 468 live
TSStatic objects naming nonexistent pooled shapes from an earlier companion session. The old-session release had
already appeared in the logs. This is concrete evidence of stale geometry references, not proof of the underlying
cause or a permanent fix for the user's reported long-idle disappearance.

Reloading the level removed those references, but the continuously connected server retained delivery state.
The feature had a related lifecycle defect of its own: mission end discarded its capability and route cache,
while the core intentionally kept the connection and saved hello. Restore the negotiated capability at level
start, and mark the first enabled request after a discarded cache with optional `reset: true`. The server clears
both visual trackers' sent sets. Ordinary off/on toggles retain cached routes and omit the reset flag.

Regression checks failed before the fix at all three seams: core's level-start capability restoration, the
renderer's fresh-cache request, and the real Connection's clearing of both trackers. They then passed (core 282,
renderer 1682 assertions; feature 20 Python tests and protocol-table agreement 3). Actual level reload retained
hello/capability 2, and the evening stream refilled without another reconnect: about 503 actors at an intermediate
65-tile warmup, with both near/far routes still arriving. The geometry audit had zero missing shape files.

The same vehicle, geographic camera position, rotation and FOV were restored. The game's final loading setup
overwrote an early TOD restoration with its default June morning, so the original November evening was reapplied
after setup completed. Physics resumed. These are recovery steps; full reconnect/mission and long-idle QA remain.
