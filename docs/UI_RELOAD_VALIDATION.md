# Earth UI reloads during a BeamMP join

The 1.9.1 UI entry point imports `earthServers.js` again in `onUnload`. BeamNG's UI mod manager can reset the mod's
module graph while refreshing its Vue components, before calling the old entry point's `onUnload`. That import then
returns a fresh server-browser module. Stopping it leaves the original module's observer, Vue watchers, timer and
router hook alive. The next load adds another browser instance; their private server-list ownership sets do not
recognize each other's arrays.

The fix keeps the namespace that was started and stops that exact namespace. A window-scoped owner stops any
previous browser instance before a new one starts. Run and request generations reject imports, clicks and Lua
replies that finish after unload or a newer refresh. A shared entry-point queue orders load/unload calls across
module copies; a stale entry point cannot unregister the current owner's pause tab and routes.

## Automated regression

Use Node 22.22.2+ or Node 24.15.0+:

```sh
npm ci --prefix mod/tests --ignore-scripts
npm test --prefix mod/tests
node --test mod/tests/test_mapmath.mjs server/tests/test_mapmath.mjs
python tools/run_lua_tests.py
python tools/package.py --beammp --out build/reload-fix-packages
```

The UI tests run the production source modules with real Vue 3.5.43 (the installed BeamNG 0.40.5 version) and jsdom.
Only game bridge/router calls, the BeamMP shared state and the polling clock are fixtures. Separate module
evaluations model graph invalidation rather than reusing the same ES module cache entry. Cases cover graph resets
before unload, duplicate instances, 50 reloads, stale callbacks, out-of-order replies, latest-public-list restoration,
authentication route gates, overlapping entry points and cleanup when a bridge call fails.

On Windows, all 20 UI tests pass in development and production Vue builds. The unmodified 1.9.1 source passes 6 and
fails 14 of the same cases. Both existing map-math suites and all 113 offline Lua test modules on the upstream-based
branch pass. Applying the same two runtime files to the supplied 1.9.1 snapshot also passes the 20 UI tests and all
103 Lua modules in that snapshot. Player and BeamMP server packages build. The patched 1.9.1 client ZIP passes CRC
and source-content checks. The full foundation gate subsequently passed in hosted CI on Ubuntu 22.04 and Windows
for functional-source commit `cf962f9` (run `43117670493`), including 59 Python modules and native builds/tests.
The larger server suite beyond that gate was not run.

## In-game validation

In an isolated BeamNG.drive 0.40.5 profile with the official BeamMP source mod present, the actual UI compiler and
`useModManager().reloadModByName('beamngEarth')` completed ten sequential reloads and three overlapping reloads.
Every completed snapshot retained exactly one Earth browser stylesheet and all four Earth routes. The same game
with the pristine 1.9.1 UI files reproduced the leak: one stylesheet before the reload, two afterwards. The test used
the game's normal router and Lua bridge, without replacing either. Recorded results are in
`docs/research/2026-11-11-ui-reload.json`.

This engine test ran at the initial main-menu/legal screen. It did not join a multiplayer server or load the streamed
world; those require the staging-server check below.

One preliminary game launch with `-windowed` crashed during startup: the installed game's `parseArgs.lua` called the
missing global `setFullScreen`. The successful runs omitted that option. The final test log records a normal engine
shutdown; the preliminary crash is not counted as a passed test or evidence about multiplayer stability.

## Update both the app copy and the server copy

The app installs an unpacked copy of the mod before the player joins. A stock app can therefore start the original
UI before BeamMP downloads a patched server ZIP. A later live staging check confirmed this mixed deployment: the
patched ZIP was active and the streamed world loaded, but two Earth browser styles remained. The original local
files matched pristine 1.9.1. This is not a clean first-join pass with the fixed client.

The new owner guard cannot identify a legacy instance that never registered an owner. A clean validation requires
the fixed UI in the app's embedded mod as well as the server ZIP: close the game and old app, start an updated app
build, then join the updated server. The stock app's installer compares file contents and can replace a manual local
patch on its next start, so updating only the installed files is not a durable app update.

For a staging server, put the changed UI files in its `Resources/Client/beamng_earth.zip`; the server sends this copy
to players and can replace a locally patched mod. Use the updated client app described above, and restart the game
after updating to clear instances left by the old code. Then check:

1. Start the companion and BeamMP, open the Earth server category, and join after a server restart.
2. Check that the browser stays responsive through mod download/reload and that the world finishes loading.
3. Reconnect repeatedly; refresh the Earth list while a request is outstanding and leave the category before a reply.
4. Return to BeamMP's normal categories and verify its latest public list, favorites and login gates.
5. Open Earth settings, the welcome page manually, and both map routes after a reload.

The first-join/reconnect check requires a staging server carrying the patched client ZIP. BeamMP's own independent
route-unregistration behavior is outside this change.
