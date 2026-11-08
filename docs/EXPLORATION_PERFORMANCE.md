# Exploration frame pacing

Scope: preserve graphics settings, terrain sampling, world geometry and collision coverage. Follow
`V1_2_DESIGN.md` section 5.4: sustained streaming is small and ramped; a terrain change is atomic and isolated.

## Late terrain and photo updates

Ordinary terrain-follow already requests a governor heavy slot. Previously `terrain_refresh` called
`commitTerrain` from network dispatch and `block_base` could call `terrain.setBase` there too. Neither reserved
a slot. A photo refresh rebinds every current terrain material; receiving both in one frame could therefore
swap the block and rebuild its materials separately, alongside unrelated streaming.

Receive metadata immediately, keep only the latest applicable refresh/photo, then apply after the normal
world update. Terrain-follow, a teleport, vehicle placement and a two-phase rebase keep priority. Updates
wait for their own heavy slot and respect the quiet frames. A later photo folds into a queued terrain
refresh; a later terrain supersedes an older queued photo. Stale block updates and updates from a replaced
server session are discarded. Existing speed hysteresis remains for a photo-only replacement.

Validation is initially offline: `test_core_terrain_updates`, `test_core_stubbed`, `test_terrain_swap` and
`test_governor` exercise actual Lua modules with engine stubs. They establish scheduling and engine-call
counts, not GPU cost or gameplay frame times. A combined update has one swap and no separate `updateMaterial`
calls. A photo-only update still changes every current slot. No frame-rate improvement is claimed without
a controlled in-game comparison.

Windows / LuaJIT 2.1, base `2cbc26ce`: all 98 offline Lua modules passed. Running the initial delivery assertions
against the unmodified base fails both scheduling assertions (engine work during receipt, nothing queued).
The new isolated regression has 20 passing assertions. These are not in-game measurements.

The monolithic engine terrain-file setter remains. This change prevents extra work from accumulating
around it; it does not make that native engine call asynchronous or eliminate its individual stall.

## Controlled validation

- Compare the same game build, settings, vehicle, weather, route and cache condition, alternating baseline
  and changed builds. Do not compile or run server benchmarks while measuring gameplay.
- Record frame-time percentiles and counts over 50/100/250 ms, separately for terrain commits, late photos
  and ordinary streaming; record terrain-edge holds and world-loading lead too.
- Exercise a cold region, a warm revisit, a sharp turn, stopping after fast driving, a teleport, a server
  reconnect and minimizing/restoring the window. Keep the existing single physics TerrainBlock and edge guard.
- Inspect actual screenshots and vehicle/terrain contacts. Offline engine stubs cannot establish these.

## Dispatch credits and the per-frame budget

The old dispatcher guaranteed at least `ceil(backlog / 16)` handlers per frame, even after its 4 ms budget
(12 ms with a large queue) expired. Removing that floor alone would restore an earlier regression: a slow
client's queue grew for the entire drive. The negotiated `hello.dispatchWindow` (32 in the mod, accepted
range 8..64 in the server) supplies the missing sender backpressure.

Native `StreamDispatch` retains ordinary encoded metadata in FIFO order and transmits at most the negotiated
number without dispatch credit. The mod sends cumulative `stream_ack.seq` only after Lua handlers run on the
current transport. Critical terrain/anchor answers, status and urgent UI results bypass the window. Normal
dispatch uses the 4 ms budget without the backlog-fraction override or the 12 ms catch-up budget. One atomic
handler can still overrun it. Decode/dispatch time counts before other per-frame work. Loading remains a
separate mode. Menu transitions finish the deferred FIFO and return credits too.

These are metadata credits, not far-tile import acknowledgements or pooled-file lifetime acknowledgements.
Queued metadata retains the existing lease/slot ownership; a disconnect still needs the next complete
ownership snapshot. Queued encoded data and the socket buffer share the existing 32 MiB control-buffer limit;
the native queue also caps at 4096 messages. Overflow closes the transport rather than silently losing world
messages. No surface, geometry, render-distance or graphics setting changes.

Compatibility is negotiated: old mods receive the original unsequenced stream; with an old server, the new mod
keeps the legacy drain rule because that sender cannot honor credits. No protocol/geometry format bump is
needed. Native API 159 is required for the new server logic. No outside PR code was adopted: #71's older FIFO
work overlaps the general subject; this fixes the newer backlog-fraction rule and supplies sender credits.

Windows validation: the Release MSVC native module built; three Rust window tests passed; seven Python
binding/Connection tests, 17 real-loopback reconnect tests and three connection-cleanup tests passed. The
32-test protocol-agreement module completed (one optional game-install check skipped). All 99 offline Lua
modules passed; the final dispatch regression has 13 assertions, including a FIFO tail crossing into the menu.

A deterministic engine-stub control gives each handler exactly 5 ms and queues 100 messages. Base
`d0e49681` handles seven in a 35 ms burst (93 deferred); the negotiated path handles one in 5 ms (99 deferred).
This proves the budget override is removed, not a measured change in actual frame rate. The real loopback
test sends 100 ordinary messages through a window of eight, checks FIFO/exactly-once delivery, and receives
the critical barrier before returning any dispatch credit.

## Raw land-cover class reuse

The broad `landcover` stage includes classification, slope/material selection, underwater overrides and road
inputs. An isolated profile of 1,048,576 points with already decoded WorldCover inputs on the i5-10600 found
raw `Surface.classes` at 172 ms median: warp 107 ms and raster sampling 33 ms in the instrumented pass. This
does not attribute the entire observed 835 ms landcover stage to this sampler.

`ClassGridCache` (native API 160) retains only raw classes in 64 x 64 sample chunks of the exact physics
lattice, up to 8 MiB of class samples plus small identity/entry metadata. It recomputes missing points with
the existing sampler. Frame, lattice spacing and warp changes invalidate the namespace. Immutable decoded
rasters contribute weak identity tokens: replacement/reload invalidates entries without pinning raster data
outside MapData's own memory budget. Source sets are conservative for the full block; changing that set can
cause extra misses, which favors correctness over hit rate. Missing coverage is never retained. Polar and
antimeridian blocks keep uncached sampling. Plans are independent and stale generations cannot repopulate
a cleared cache. Returned arrays and input arrays do not share mutable storage with retained samples.

Only raw WorldCover classes are cached. Slope, lawns, water-bed and road overrides still run on the current
block. No plan/facts format change: focused regressions compare raw classes, final material indices and `.ter`
bytes across moves. The source rasters are immutable by MapData's contract; callers cannot mutate them in place.

### Measurement on Windows

Core i5-10600, Python 3.10, Release MSVC earthfast 160, benchmark process restricted to logical CPUs 6..11.
Nine alternating pairs per route, each route nine 1024 x 1024 blocks. Every measured cached pass starts empty;
one warmup per consumer is excluded. Coordinate generation and output checks are outside timing. The same
native binary serves both consumers. Inputs are already decoded, either seeded synthetic rasters or independent
copies of six WorldCover cache files (no live cache/server, downloads, sea/road work or game running).

| Input / route | Original route ms | Cached route ms | Change |
| --- | ---: | ---: | ---: |
| Synthetic, straight | 1581.4 | 585.9 | -62.9% |
| Synthetic, diagonal | 1550.6 | 838.8 | -45.9% |
| Synthetic, turn/revisit | 1553.6 | 318.5 | -79.5% |
| WorldCover, straight | 1583.3 | 572.4 | -63.8% |
| WorldCover, diagonal | 1536.3 | 1005.0 | -34.6% |
| WorldCover, turn/revisit | 1546.9 | 683.6 | -55.8% |

All measured paired route totals favor caching. All 486 measured cached grid hashes match their uncached
references across these datasets; the unit tests also compare arrays and terrain-file bytes directly. Sample
storage peaks at 1.56..2.88 MiB in these routes. These numbers measure this sampler, not the whole terrain
pipeline or gameplay FPS. A cold isolated block can incur cache-setup overhead; the cold fill is included above.

The [synthetic measurement record](research/2026-11-07-class-grid-synthetic.json) preserves paired totals,
output hashes and source hashes. The real-raster snapshots remain local, outside the repository.

Reproduce from a source checkout without network or the player's cache:
`python tools/benchmark_class_grid.py --pairs 9 --out build/class-grid.json`.
`--inputs` accepts standalone `z_x_y.npz` snapshots; missing coverage fails before measurement.

## Combined validation before upstream integration

On the November 7 base (`2cbc26ce`), 21 focused Python modules completed, including 53 end-to-end server tests,
the protocol agreement, terrain/surface/cache checks and reconnect ownership. One optional installed-game
agreement check was skipped. The general server module exceeded the initial 300 s limit alongside other work;
it then passed alone with a 900 s limit (447 s). The class-grid module has 11 tests, including seeded lattice
moves, byte-identical material/terrain output, invalidation, incomplete data and weak-source lifetime checks.
These results predate integration onto later upstream changes; in-game validation was not yet performed.
