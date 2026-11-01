# Distant traffic: visual-road stream and renderer

Follow-up to the renderer probe for GitHub #565, on dev `55ddc418` (the running companion is dev.97).

The feature branch was subsequently rebased onto dev `3ed3950c` without conflicts (two upstream updates, native
kernels 122). Route/stream tests (14), navgraph tests (12), protocol table agreement (3), and the upstream lane-shift,
median-gap and parapet-cover cases (10) passed on that base. Live snapshot measurements below used the dev.97 world.

## Implementation

`server/distanttraffic.py` builds ground-road parts from the same map cells, road specs, lane shifts and driving
widths as the road builder. Major paved classes only: motorway through tertiary. One direction per one-way road,
two separately offset lanes otherwise; left-hand traffic and mapped speed limits follow the shared road rules.
Long chords are sampled at 20 m, mapped bends retained. Each lane samples its own heights; Z includes the tile
frame's curvature. Bridges and tunnels are omitted rather than assigned ground heights.

Routes are whole ground parts, keyed by feature id / part / direction. Adjacent tiles may own the same route:
the client keeps one prepared path until its last owner is dropped. Each path stays in its source geotile frame,
re-placed in the current anchor through `geotile.lua`. No changes to the physical AI graph, vehicles or collisions.

The server negotiates `hello.trafficVisuals: 1`, then waits for `traffic_visuals.enabled`. One existing
`vegetation.Tracker` streams zoom-15 geotiles, at most one build in flight on an existing worker pool. The pool
falls back to the road pool when there is no separate far-building pool; building settings do not gate the stream.
Missing road cells are prefetched, not waited on. Session files avoid rebuilding the same tile after a rebase.
Older companions do not answer the capability, so the new client module remains inactive with them.

`trafficvisuals.lua` owns a pool of at most 64 cosmetic cars / 128 planes, including retiring actors. Route
preparation, selection and pool growth are incremental; production preparation is charged to the existing frame
governor. Motion uses simulation time, fades use real time. Night, traffic, roads and the new Distant traffic lights
setting gate requests; disabling retains geotiles, fades actors out and deletes idle objects after 30 s.

The camera's range is 350–4000 m, with 300 / 600 m fades. The physical terrain box has an additional 100 m exclusion
and 400 m fade. Path ends fade over 30 m; changes of selection fade over 2 s. Density follows the existing pure
local-hour curve, affecting both stable per-slot ranks and the global population limit (otherwise a large city
would fill all 64 slots even during quiet hours). Each plane has a shared radial texture/material, directional
white/red colour, opacity and no real lighting, collision, shadows or vehicle simulation.

Payload caps: 32 paths / 4096 points per tile, 512 points / 8 km per path; client caps 192 tiles, 512 prepared routes
and 32768 prepared or pending points. Routes beyond these caps are omitted. These are bounded cosmetic data,
not a promise to reproduce all traffic in dense areas. The fixed zoom and tile cap need evaluation at high latitudes.

## Verification

- Python route/stream tests: 14 passed. Shared directions, speeds, lane sides, bridge/tunnel exclusion, finite and
  curved heights, payload bounds, neighbouring-frame agreement, missing/ready MapData contracts, old-client
  negotiation, switch gating, inline messages, geotile reuse at an anchor change and drops.
- Protocol agreement: 32 tests run, 31 passed and 1 skipped. The initial run exposed the new optional message
  types missing from the synthetic terrain session's exclusions; that table was updated and the full run passed.
- LuaJIT path/ownership tests: 50 assertions passed, including memory reserved by pending preparation. Engine
  fixtures cover actor/plane bounds, blocked frames, paused simulation, near-traffic exclusion, night/traffic gates,
  density, re-placement, drops and complete reset. Settings tests: 285 passed. No added runtime dependencies.
- The production worker built all 65 requested tiles within 3.5 km of the existing Sochi camera from cached data,
  offline: 593 unique routes, 12361 source points, 554448 bytes across tile JSON files; 17.086 s total. The bounded
  client retained 512 routes / 10425 points.
- The isolated in-game snapshot uses that producer and the production renderer. Initial saturated-cap test:
  64 cars / 128 planes, mean hook time 0.459 ms across 7018 frames; maximum 2.765 ms including preparation/startup.
  After the density quota was applied, at roughly 00:50 local time: 25 actors including transitions / 72 allocated
  planes, 20 eligible cars, mean 0.294 ms over 2381 frames and maximum 2.273 ms. These are Lua update measurements,
  not complete CPU/GPU frame costs or an FPS comparison; eligibility is not screen-visible pixels.
- The user confirmed that the real far-road preview looked good, with no reported flicker or abrupt appearances.
  The user moved the free camera during the test. The tools did not move it or alter physical traffic (21 vehicles).
- Disable test in the game: zero cars, planes and eligible lights after the idle cleanup; all 65 tiles / 512 routes
  retained. Re-enable and final cleanup/restart results are recorded in the workspace MODLOG.

## Reproduce the isolated check

Build the three original graphics assets with `python probes/traffic_glow_assets.py --production DEST`, placing
them at the mod's `levels/beamng_earth/art/shapes/beamngEarth/traffic/`. No extracted game assets are used.
Build cached routes with `python probes/distant_routes.py --lat LAT --lon LON --x CAM_X --y CAM_Y`. This uses the
production worker with downloads disabled. Copy its output into the user's `temp/beamngEarthTrafficProbe/routes/`,
the current pure module and renderer into that scratch folder, and `distant_stream.lua` as `stream.lua`.

`python probes/distant_traffic.py start --mode far` starts the snapshot; `status --mode far` reports counters;
`stop --mode far` removes its own objects and extension. It expires after 10 minutes. It intercepts only its own
module's request callback; the active core, companion, settings, camera and ordinary traffic are left running.

## Remaining before upstream review

The checkout includes the real core/settings/protocol wiring, but the running packaged dev.97 companion does not
support it. This check intentionally used the cached snapshot adapter to preserve the user's loaded world. A full
live companion/client run, actual world rebases/teleports, driving near the transition, distant height/occlusion
checks in more terrain, and CPU/GPU comparisons remain. The snapshot's own height policy excludes bridges and
tunnels; their profiles need a separate implementation and visual check. This is a verified development slice,
not a finished release or PR. Nothing was pushed or published.
