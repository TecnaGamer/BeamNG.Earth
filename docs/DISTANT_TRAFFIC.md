# Distant traffic lights (#565)

Moving headlights and taillights make distant roads and bridges look occupied without spawning vehicles. The setting
**Distant traffic lights** is in BeamNG.Earth's graphics settings. It needs Roads and nighttime. By default it
also needs BeamNG's traffic state `on`; removing traffic stops the effect. The opt-in **Distant lights without
traffic** switch keeps the effect running when game traffic is off, using the same object/work limits. The main
switch still controls both modes. The population follows road length, class and local hour;
it is an impression of traffic, not live traffic data. A quiet road does not receive a city's allocation.

This is the implementation guide. The wire contract is in [server/README.md](../server/README.md), and the
[verification note](research/2026-11-01-distant-traffic-pr.md) records the tested scope and remaining checks.

## Code map

| Part | Responsibility |
|---|---|
| `native/earthfast/src/traffic.rs` | Filter, deduplicate, split and rank road/deck pieces; reserve class quotas; build lane geometry; retain bends and relief; enforce producer limits. |
| `server/distanttraffic.py` | Marshal shared road rules, projections, lane shifts and elevation samples to `TrafficRoutes`; write atomic tile JSON. Uses the bundled native module (132 on this dev base); no Python algorithm fallback. |
| `server/blockworker.py:traffic_geo` | Run the builder in the existing far-building worker pool; share the far bridge slabs' profile store and normal map/height sources. |
| `server/earthd.py` | Negotiate the optional capability and own two existing geotile `Tracker`s per connection. |
| `mod/.../beamngEarth/distanttraffic.lua` | Pure route validation, interpolation, ownership, density and view math; bounded incremental preparation. |
| `mod/.../beamngEarth/trafficvisuals.lua` | Own the emissive-plane pool, view selection, motion, fades, file retries and cleanup. |
| `mod/.../beamngEarth/core.lua` | Dispatch messages, supply anchor/physical box and schedule work through the governor; reset/restore at level boundaries. |
| `settings.lua`, `earthSettingsStore.js` | Persist and expose the boolean setting through the existing settings flow. |
| `levels/beamng_earth/art/shapes/beamngEarth/traffic/` | Original plane, radial DDS texture and shared emissive material. |

`mod/.../beamngEarth/` above means `mod/beamng_earth/lua/ge/extensions/beamngEarth/`.
The effect creates no AI nodes, collision, shadows or real light sources and never deletes a physics vehicle.

## Producer and coordinates

The near Z14 tracker covers 5.5 km, including minor/unpaved roads. A major-road tracker covers 11 km with a
4.5 km inner skip. Both use the existing worker pool with one job per tracker in flight. Missing map cells are
prefetched; the job returns without waiting for a download. Disabling the feature stops new requests.

The builder reuses `roads.road_specs`, `ground_parts`, `lane_shifts`, driving widths, handedness and speed rules.
Rust splits long parts into stable 2 km chunks. Ids are `feature:gPARTcCHUNK:DIRECTION` for ground and
`feature:bPARTcCHUNK:DIRECTION` for bridges, independent of the
receiving tile. Both streams can own the same route. Each direction follows its lane, sampled at up to 20 m
intervals, with the tile frame's curvature included in Z. Separate XY and height-profile simplification keeps
both bends and relief. The serialized sideways offset is zero: the points are already in their lanes.

`TrafficRoutes.next()` admits a piece before Python samples its heights. Python supplies shared lane shifts to
`lane_points()`, samples the returned lanes and calls `accept()`. Rust performs selection and compaction;
Python neither reimplements those algorithms nor samples every rejected ground candidate in a dense city.

Bridge heights come from the same anchor-free `BridgeProfiles` store as the far bridge slabs. `ChainView.parts_with_ids()`
retains the profile owner's `(fid, part index)` rather than deriving identity from the requested tile or matching
geometry. Deck samples are already world Z, including curvature; `deck_heights()` supplies them without terrain sampling
or adding curvature twice. Missing/non-finite deck profiles produce no route; there is no fallback to water height.
Solving a previously unseen bridge uses the existing road-worker profile cache, outside the render loop.

Tunnels remain excluded. No imaginary connections are made across junctions or missing parts; route ends fade.
Current speed is constant along a route and this
cosmetic stream does not simulate junction priority, signals, queues or actual vehicle movements.

## Bounds and recovery

| Resource | Limit |
|---|---|
| Near tile | 128 paths, 8192 points; 512 points per path |
| Major-only far tile | 32 paths, same point limits |
| Client route cache | 384 tiles, 8192 prepared routes, 196608 prepared + pending points |
| Deferred tile files | 384 descriptors; no retained copy of their rejected geometry |
| Render pool, including retiring assignments | 512 cosmetic cars / 1024 planes |
| Recent views | Four plans, each at most 512 key/id/phase/score records; no engine objects or geometry |

Producer quotas reserve space for major, local and unpaved roads before unused capacity is filled. Stable hashed
priorities avoid dependence on map-cell arrival order. Spacing ranges from 50–100 m per direction on major roads
to 220 m residential, 500 m service and at least 1600 m unpaved / 3000 m track. Fractional quiet-road demand uses
stable slots. Local-hour density thins those slots and the global ceiling. Visible demand shares the renderer
between below 2.5 km, 2.5–6 km and beyond 6 km, reserving at least 20% for distant demand when present.

Headways form a stable renewal sequence rather than one car per equal spacing cell. Twenty percent of the
route length reserves a minimum gap; the rest follows bounded exponential weights, normalized to the remaining
length. Gaps stay ordered, including at wrap, because all slots share the route speed. At most 128 phase offsets
are cached per prepared route and released with it; frames only sample existing offsets. The stable rank mixer
decorrelates adjacent slot numbers before phase, density and pool thresholds; linear ranks produced repeating
three-car groups on long bridges. Expected density, constant route speed and hard budgets are unchanged;
individual fractional counts and selected identities can differ between versions. Looking away or toggling
does not reshuffle the same route's slots.

Tile admission is atomic at the point/tile limit. A rejected tile is not marked received by the route store.
The renderer retains its current-session file descriptor and retries after capacity is released, at most one
bounded tile read per 50 ms under the governor. Duplicate/dropped descriptors do not retain geometry. Descriptor
overflow or a missing retry file requests both current streams again through the existing `reset: true` field,
with a ten-second cooldown and only when space is available. Dropping either stream releases only its ownership;
the final owner releases the route. Prepared-route saturation keeps bounded queued work until routes drop.
Admission compacts stale/completed queue records at the tile limit, even while a saturated head cannot advance;
tile/drop churn cannot grow the preparation queue beyond 384 records.

Capacity can delay coverage; the ceiling is deliberate. High-latitude tile counts and multiple distant foci still
need broader field testing. Do not remove bounds to hide a coverage issue.

## Rendering and lifecycle

The camera's distance fade starts at 350 m with a 300 m ramp. The physical terrain box adds a 100 m exclusion and
400 m ramp to avoid overlap with ordinary traffic. Major roads fade out by 10 km and local roads by 6 km; ends
fade over 30 m. Facing blends white/red planes. New source data and re-enabling fade over two seconds.

A camera assignment uses the route/request age, including an existing assignment returning from retirement.
Turning back to an established stream does not restart its brightness. Four recent view plans accelerate return
turns; reuse is constrained by position (50 m), orientation, FOV/aspect, density and a 30-second age. Rebase,
teleport and reset invalidate the plans. A cooperative scan refreshes every selection. Very fast turns or new
views can still wait for bounded selection/allocation work; they are not a promise of zero-frame latency.

Preparation and scanning share a cooperative 300 microsecond slice, with up to 150 microseconds / 16 steps for
preparation. The existing governor charges this work. Native creation is limited to two car pairs per frame;
up to 64 pooled pairs can be reassigned. Placement, sampling and color uploads run at 45/24/12 Hz by distance.
An origin change forces every distance band to move in its first frame. Unchanged paused objects avoid uploads.
Engine calls and JSON reads can exceed a cooperative slice; these are work limits, not a hard frame deadline.

Traffic/night/roads/setting gates are checked twice per second. The independent switch bypasses only the traffic
gate; it neither spawns vehicles nor enables physics traffic. Its value persists across restarts and defaults to
false for existing settings files. Disabling fades actors out, retains route data,
and deletes idle plane pairs incrementally after 30 seconds. Teleport clears objects/view plans. Session/mission
reset deletes all owned objects and route/deferred data. A level start restores capability from the saved hello
if the connection survived; the first request after cache reset asks the server to resend both streams.

## Run and verify

Use the normal developer setup in [README.md](../README.md#developer-setup): rebuild the native module after a
Rust change, sync the mod and start the matching source companion with `python start.py`. A downloaded release
does not include an unmerged branch. Close the other companion first: both default to port 47613. Start the game
and load BeamNG.Earth, enable traffic and select a nighttime view. No probe extension is needed for normal use.

Read-only in-game Lua diagnostics:

```lua
beamngEarth_core.serverHello().trafficVisuals       -- 2 when negotiated
beamngEarth_core._trafficvisuals.status()          -- counts, pending/deferred work, view-cache hits and Lua timing
```

From a Python environment with the server requirements and the native module:

```text
python -m unittest discover -s server/tests -p test_distanttraffic.py
python -m unittest discover -s server/tests -p test_protocol_agreement.py
luajit mod/tests/test_distanttraffic.lua
luajit mod/tests/test_trafficvisuals.lua
luajit mod/tests/test_core_stubbed.lua
luajit mod/tests/test_settings.lua
```

The pure tests cover route ids, sides/speeds, relief, quotas, finite samples, ownership, saturation recovery and
protocol gates. Engine fixtures cover pool limits, view return during slow scans, pause, rebase, traffic removal
and cleanup. `probes/traffic_metrics.lua` samples module/frame costs without changing the scene;
`probes/traffic_glow_assets.py --production` regenerates the original glow assets into a supplied output directory.

In-game acceptance: compare the same camera/time with on/off; turn away and back after 3–5 seconds; remove and
respawn physical traffic; toggle roads and the setting; pause/resume; drive across the physical-box boundary;
then check a genuine driving rebase, teleport and reconnect. Record screenshots, counts and errors. Eligibility
counts do not establish occlusion-visible pixels; Lua timings do not measure full CPU/GPU cost. The current
[integration note](research/2026-11-01-distant-traffic-pr.md) records the tested boundary and remaining checks.
