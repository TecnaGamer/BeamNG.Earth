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

## Reconnect ownership and bounded outgoing queues

The disconnected interval is now covered by a server-issued game lease that
outlives the TCP connection. Complete reconnect snapshots reconcile held slots,
anchors and prior session folders; replacing an overlapping connection does not
let its old cleanup or acknowledgements release the new owner's files. The mod
retains its token across scene resets and waits for a fresh hello before sending
world requests. A timeout still never proves that a game deleted its objects.

With that ownership in place, the Lua outgoing queue is bounded to 8 MiB and
1,024 frames, including retained partial-frame prefixes. Each update shares a
256 KiB / 32-call send budget. The companion also bounds its pending write buffer
to 8 MiB. Overflow aborts the transport; the next handshake safely reconciles the
discarded acknowledgements. These are pending-traffic limits, not process-memory
limits or evidence of native crash resolution.

See [streaming reliability](STREAMING_RELIABILITY.md) for the additive protocol,
loopback/partial-write/soak tests and the conservative retention limits for games
which never resume or use an older client.

Run the regressions from a checkout with the documented development dependencies:

```text
python tools/run_tests.py test_tile_ownership test_matslots test_connection_cleanup test_launcher_lifecycle --par 2
python tools/run_lua_tests.py test_framing test_core_robustness test_core_stubbed
python tools/quality_gate.py --par 2
```

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
   messages, and verify slot/serial acknowledgements across forced reconnects.
   Offline Lua tests do not establish this engine behavior.
5. Preserve any new game log and crash report before another launch overwrites
   them. Sanitize paths and locations before sharing. Do not enable an unrelated
   branch's debug-capture instructions: this checkout has no such menu.

A short no-crash run does not prove resolution of an intermittent native crash.
The fixes are reproducible reliability improvements; full long-session stability
and reduced stuttering remain awaiting environment validation.
