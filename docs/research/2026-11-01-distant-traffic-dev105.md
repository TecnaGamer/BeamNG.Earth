# Distant traffic on dev.105 (#565)

The unpublished feature branch was rebased onto upstream dev `cffca80b54cfb746f916945fe12c58c81a1c8674`
(the user's dev.105). This includes the upstream held-session building-file and prop-restart changes. The
source VERSION remains `1.7.3-beta`; a source hello therefore reports that version, not the packaged dev suffix.
The implementation guide is [DISTANT_TRAFFIC.md](../DISTANT_TRAFFIC.md).

## Alignment and tests

The maintainers' current `.claude/agent-rules.md` requires new server algorithms in Rust, with Python glue only.
Route filtering, stable chunking/ranking, quotas, lane geometry and relief compaction now live in
`native/earthfast/src/traffic.rs`. Python supplies shared rules/projection/elevation. Native 124 was built for
Windows and Linux with the project's build script. A one-time comparison against the previous Python builder
matched 3912 routes exactly across 100 seeded mixed-road/relief scenes; the scratch Python twin was then removed.
Retained behavior tests exercise the native implementation.

| Check | Result |
|---|---|
| Feature Python tests | 22 passed |
| Protocol agreement | 32 run, 31 passed, one Lua executable availability skip |
| Pure Lua route/ownership tests | 149 assertions passed |
| Renderer engine fixtures | 1706 assertions passed |
| Core / settings / geotile / navgraph Lua fixtures | 282 / 287 / 17 / 46 assertions passed |
| Shared road / navgraph / geotile Python tests | 82 / 12 / 14 run; geotile Lua executable check skipped |
| Worker tests | Three passed; two fail on Windows temporary-directory cleanup |

The worker failures are `FarTileTest.test_texture_from_disk` and `RoadsTileTest.test_same_as_in_process`:
`WinError 32` deleting an open `revisit/where/*.tsv` sidecar. Both reproduce with the same Python environment
against a separate, untouched archive of upstream `cffca80b`, including native 123. They are not reported as a
passing suite or changed in this contribution. Protocol tests also emit an existing pending-async-task warning;
their final unittest verdict is OK. Linux binary build succeeded; live rendering was tested on Windows only.

The point-cap regression originally acknowledged a partly omitted tile as received. Admission now rejects the
whole tile and retries its bounded file descriptor after space is released. Regression failed before the fix
and passes afterward. Descriptor overflow uses the existing optional reset request instead of another protocol.

## Standards

The final parallel Standards review of `cffca80b...6cd53522` found no remaining hard documented standards
violations: native 124 owns the new algorithms, Python is glue, docs agree and adjacent Lua/Rust style is followed.
It identified one P2 resource-bound defect: a blocked preparation head retained stale queue records from later
tile/drop churn. Ten thousand cycles retained 10001 records despite two live tiles.

Commit `83027003` compacts live unfinished queue records at admission, independent of a saturated head. The
new 10000-cycle regression was red before the fix (148 pass / one fail), then green (149 pass). Pending FIFO
order and recovery after the old owner drops are preserved. No naming/formatting heuristics warranted changes.

## Spec

The independent Spec review found the same P2 bounded-resource defect and no further concrete missing behavior
or scope creep. Its follow-up review verified the compaction, reran both Lua suites (149 / 1706 passed) and found
no follow-on issue. The reported physical-traffic crash still requires honest separation between successful
attempts and an established cause. The user's driving acceptance remains outstanding.

Review totals at `6cd53522`: Standards one resource finding, Spec one resource finding (the same queue defect).
After correction: no remaining findings on either axis; this does not replace driving or wider performance QA.

## Physical traffic deletion

The previous crash log ends in native `veh:delete()` from stock `gameplay/traffic.lua:959`, through quick-access
traffic removal. This identifies the operation, not its root cause. Stock dev.105 deletion was successful before
installing the feature.

Three initial feature deletion attempts passed with physics stopped for code deployment. The regular pause
flag was false, but MCP `pause_physics` controls `be:setPhysicsRunning(false)` separately. That mismatch was
noticed before acceptance; physics was explicitly resumed and checked with `get_simulation_state` /
`be:getPhysicsRunning()`, not inferred from the pause flag. Those initial attempts are only paused-state evidence.

After resuming, one removal also passed while recovering the previously paused traffic population. Fresh traffic
was then restored with physics running. Two further controlled cycles began with 14 AI vehicles, normal finite
speeds, 9–12 moving vehicles and 479–495 visual actors. Each used the same stock
`gameplay_traffic.deleteVehicles()` called by quick access. Both left eight physical vehicles (player plus the
existing parked cars), zero AI and zero visual actors after the gate/fade. Physics kept running. Restoring the
same 14 saved model/config entries brought back 11–12 moving vehicles and 479–496 visual actors. No crash occurred.

The earlier paused-state test also confirmed idle pool cleanup to zero planes while retaining routes. These
are successful reproductions of the operation, not proof that the original native crash is repaired or cannot
recur. No speculative interception of BeamNG's traffic deletion or physics lifecycle was added.

## Camera, deployment and final boundary

The latest fade change also restores an existing, partially retired assignment directly to its source/request
age. After a four-second 180-degree turn in the current Japan view, the return sample at 0.016 s had 29 bright
in-frustum actors; at 0.172 s it had 464. All sampled returning assignments had fade 1, through four seconds.
The remaining initial selection delay is bounded work, not another brightness ramp. Camera position, rotation
and FOV were restored. This supplements the earlier camera note; it does not promise zero latency on every view.

The normal source `launcher.py` now runs the actual production companion on default port 47613. Its session
`20261101-104800-9565` negotiated capability 2. Previous temporary source trees were stopped. Switching server
sessions required an explicit reconnect; the resync waited for the paused physics and completed once resumed.
There was no claim of an automatic-reconnect pass during that stop/start experiment.

Final capture: 132 tiles, 1475 unique routes, 35828 prepared points, zero pending/deferred tiles, 494 actors /
1024 planes, 462 eligible lights, 14 physical AI / 22 total vehicles, normal speed and running physics. View was
restored to the user's Japan/Fuji camera and November evening. A shape-file audit found zero missing referenced
files. The inspected screenshot shows the night landscape and distant white/red lights. Module cumulative mean
was about 0.39 ms at this checkpoint, with a startup maximum 17.37 ms; these mixed-lifecycle counters are not a
controlled CPU/GPU or FPS comparison.

Raw captures, logs and installation hashes stay under ignored `build/dev105` / `build/camera-turn`. Local restart
instructions and a wrapper around the existing launcher stay outside the repository. No probe extension is
needed for normal play; no source was pushed and no PR was opened.

Next: user's driving test, traffic removal from the actual UI, physical/distant transition, genuine driving
rebase/teleport and menu interaction. Bridge/tunnel profiles, high-latitude/multiple-focus cache coverage and
broader CPU/GPU comparisons remain explicit limits rather than assumed passes.
