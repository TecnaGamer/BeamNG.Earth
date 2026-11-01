# Distant lights without physical traffic (#565)

The user requested an opt-in graphics switch for players who disable BeamNG's simulated traffic. This changes
only the cosmetic effect's traffic-state gate. Main enable, Roads, nighttime, local-hour density, distance fades,
the governor and all route/object/work limits still apply. It creates no physics vehicles or light sources.

`distantTrafficWithoutTraffic` defaults to false, including when loading older settings. It is persisted and
applied through the existing settings schema/handler and Vue settings store, next to **Distant traffic lights**.
The secondary row is shown while the main setting is enabled. The protocol and native route planner are unchanged.

Checks: settings293, renderer1713 and core282 Lua assertions passed; JavaScript syntax and whitespace checks
passed. Behavioral cases cover older files, persistence, apply, no-traffic rendering, the existing object ceiling,
main switch priority and retaining the night/Roads gates. No new runtime dependency.

Actual Windows game: the new row appeared in Graphics, and clicking its switch set both the saved setting and
renderer state to true. With running physics in the user's current Anadyr-area view, all13 AI vehicles were
removed through stock `gameplay_traffic.deleteVehicles()`. Eight vehicles (player and existing parked cars)
remained, traffic state was `off`, and501 cosmetic actors stayed active. Turning independence off produced zero
actors; turning it back on produced503. Main off also produced zero despite independence being on. Restoring the
saved13 model/config entries returned21 total vehicles, traffic state `on` and about504 cosmetic actors. No crash.

The user's location, evening, camera and FOV were preserved. The updated Lua settings module required a normal
core reload to refresh its captured settings reference. A proposed direct reference refresh was denied by the
built-in MCP sandbox; it was abandoned, and the normal reload was used with captured scene and paired physics
pause/resume. No such reload helper or sandbox workaround is part of the shipped code. The UI was hard-reloaded
through its standard MCP tool. Test artifacts and rollback zip are under ignored `build/force-traffic`.

Upstream conventions were refreshed to dev5ef60f11. The two new upstream commits affect road/terrain geometry;
rules/settings instructions are unchanged. This scoped Lua/JS addition stays on the tested dev105/cff base, with
no geometry changes or unrelated integration. Open PRs were checked; none supplies this setting. Nothing published.

## Remaining multi-turn appearance

After the setting was finished, the exact left / back / forward sequence was checked in the current view, with
four seconds looking left and four looking back. Returning in-frustum actors numbered77 at0.031 s and478 at
0.156 s, then about486–488. Every sampled actor had fade1 from its first returning sample. Camera/FOV restored.
This captured case is population reassignment over a few frames, rather than restarting the two-second fade.

A separate, in-memory version of the saturated real-renderer fixture held data and the512-pair pool fixed.
Within two return frames, the production64-assignment limit produced64 bright objects /64 render-data uploads.
Changing only that limit to256 produced256 /256;512 produced512 /512 and passed the full-population assertion.
The deliberately stricter assertion failed for64 and256. No fresh source data or bigger pool was involved.
These are engine-stub work counts, not native CPU/GPU timings. They confirm the simple faster variant concentrates
eight times as many uploads into that return frame; they do not establish an exact FPS penalty.

The user explicitly preferred the accepted rendering if further smoothing required more work/resources. The
production camera/assignment limits are therefore retained. No experimental limit, extra object cache or camera
code change is shipped in this slice. Normal renderer1713 assertions still pass. Scratch comparison stays under
`build/force-traffic`; a larger performance study is not required for this requested checkbox.
