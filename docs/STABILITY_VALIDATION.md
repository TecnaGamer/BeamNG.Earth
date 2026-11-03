# Streaming stability fixes and remaining runtime checks

## Canceled tile workers retained ownership too briefly

Canceling an asyncio executor future does not stop a thread already writing a tile.
Previously the canceled future removed its tile from `tile_inflight`; reclamation
could then assign the same pooled shape filename to a new tile. The original
worker could overwrite that file after its new owner had written it.

Each consumer now receives a shielded waiter. The producer remains tracked until
its actual completion, and anchor pruning defers while its producers still run.
Canceled consumers no longer release a running producer's slot or cancel another
consumer. This preserves the existing worker limits. Already submitted obsolete
jobs may finish after a rebase, so a rapid sequence of teleports can still spend
CPU on work no longer displayed.

`server/tests/test_tile_ownership.py` uses real executor threads, temporary shape
files and barriers. All three regressions failed with the original methods and
pass with the fix: late overwrite, shared-consumer cancellation, and pruning
before a writer finishes. This establishes file ownership, not crash causality.

## Reconnect slot ownership and queue hardening remain incomplete

Independent review reproduced a separate lifecycle issue: a disconnected game
retains displayed shapes, but the server releases that connection's slot holds.
Its reconnect hello restores the holds without necessarily removing those slots
from the free pool. Allocation now checks whether a free candidate has become
held again and moves it to the waiting set instead of reassigning it. This closes
the post-hello allocation defect. It does not close the interval between
disconnect and the new hello.

A complete fix needs stable per-game ownership across connections, retained
disconnected holds, and atomic snapshot reconciliation, including overlapping
connections. A timeout alone cannot prove that a disconnected game stopped
drawing a shape. This remaining issue is a release limitation.

A bounded Lua send-queue implementation passed controlled backpressure tests,
but its forced disconnects would increase exposure to that lifecycle issue.
It is isolated on `experimental/bounded-send-queue` and is not included in this
release or the final PR changes. The existing outgoing queue remains unbounded
under sustained socket backpressure. Do not advertise bounded queues or native
crash resolution as shipped features.

Run the regressions from a checkout with the documented development dependencies:

```text
python tools/run_tests.py test_tile_ownership test_matslots test_connection_cleanup test_launcher_lifecycle --par 2
python tools/run_lua_tests.py test_framing test_core_robustness test_core_stubbed
python tools/quality_gate.py --par 2
```

## Optional airport refresh must fit the startup budget

PR review reproduced a startup regression: the companion waits for airport data
before listening, while the launcher allows 120 seconds for readiness. The
provider's two 120-second attempts plus backoff could take over 240 seconds before
the existing stale-cache or no-airports fallback ran. A fake-clock stalled
transport reproduced 244.222 seconds in both cases, without network access.

Startup now isolates that optional refresh in a spawned process with a 20-second
budget. The parent terminates and reaps an overdue child before reading the
transactionally committed snapshot or creating world workers. No network-bound
thread remains to change airport data later. A fresh/offline/recent-failure cache
path avoids spawning; a timed-out stale refresh preserves the normal six-hour
retry backoff. If the child cannot be reaped, startup reports an explicit error
rather than exposing a snapshot that could still change.

`test_airports_startup` covers the deadline, process cleanup and cache fallback;
`test_airports` retains parsing, refresh and snapshot validation coverage.

## Native crash investigation remains open

A local fork 1.8.2 candidate connected successfully on Windows with `--no-beammp`,
then crashed during high-speed driving after approximately 12.5 minutes. The
preserved minidump records an access violation inside BeamNG.drive 0.40.4's native
executable, using DX12. Without matching game symbols, it does not identify an
Earth function or establish either fixed defect as its cause. A previous report
has a different exception context; the Windows event log's repeated fault offset
is insufficient to classify the crashes as identical.

The log's last-minute frame summary recorded 55 FPS, a 25 FPS 1% low, and a 993 ms
worst frame. The large late hitch was not explained by the available Lua timings.
These are uncontrolled observations, not before/after performance measurements.
Private reports remain outside source and release packages because they can
contain user paths, locations and machine details.

## Manual acceptance still required

1. Use the candidate's packaged player, confirm its version, and repeat the same
   route, vehicle, renderer and graphics settings. Keep BeamMP bypassed for the
   initial comparison, matching the reported session.
2. Drive beyond the previous failure window, include high-speed streaming and
   several teleports/rebases, and record frame times, process memory and errors.
3. Close and relaunch normally; verify that owned companion workers exit and a
   second launch reconnects. Test successful BeamMP startup separately.
4. Repeat with two actual BeamMP clients and interrupted companion connections.
   Check that reconnect restores the session without stale terrain or duplicate
   messages; the known disconnect-gap issue above remains open. Offline Lua
   tests do not establish this engine behavior.
5. Preserve any new game log and crash report before another launch overwrites
   them. Sanitize paths and locations before sharing. Do not enable an unrelated
   branch's debug-capture instructions: this checkout has no such menu.

A short no-crash run does not prove resolution of an intermittent native crash.
The fixes are reproducible reliability improvements; full long-session stability
and reduced stuttering remain awaiting environment validation.
