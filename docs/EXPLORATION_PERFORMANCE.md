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
