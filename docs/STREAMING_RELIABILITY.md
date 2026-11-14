# Streaming ownership and bounded outgoing traffic

## Reproduced defect

On `8ebb6268`, closing a connection releases its pooled file holds even though
the game can retain its objects. A second client can then overwrite the same
`slot_00000.cdae` before the first reconnects. The existing post-hello allocation
check cannot protect that disconnected interval.

`test_stream_reconnect` reproduces this using real loopback TCP connections and
temporary hard-linked/copy-backed shape files. The first regression fails on the
unchanged baseline: the second client's replacement uses the first client's
still-held path. No map provider or BeamNG process participates in this test.

## Ownership contract

`server/streamleases.py` stores a game's resource ownership independently of its
current transport. New connections negotiate these additive Protocol 4 fields:

| Direction / field | Meaning |
| --- | --- |
| Mod hello `streamLease: 1` | Supports resumable game ownership |
| Server hello `streamLease: 1`, `leaseId: string` | Server-issued opaque ownership token |
| Mod hello `leaseSession`, `leaseId` | The server session and ownership token being resumed |
| Mod hello `slotSession`, `slotsHeld: [[slot, serial], ...]` | Complete held-slot snapshot for that session, including instances waiting to spawn/delete |
| Mod hello `held: [anchorId, ...]` | Retained anchors of that session, including the current anchor |
| Mod hello/pose `sessions: [sessionId, ...]` | Prior server folders whose files are still referenced |

The mod keeps the ownership token in its network module across transport and
scene resets. Server restarts issue a new token in their new session; older
session folders continue through the existing `sessions` mechanism. Connection
events remain FIFO behind already-received tile messages, so a snapshot includes
those messages' pending objects rather than prematurely releasing their slots.
Each new transport must complete its hello before poses/world requests are
accepted; stale scene state cannot start streaming ahead of that reconciliation.
Incoming hello/connect events carry local transport-generation metadata in a
weak-key table. A delayed hello can preserve the old objects' ownership token,
but cannot make a replacement transport ready or rewind its completed handshake.

A reconnect validates the whole snapshot before changing the owner. It replaces
the old connection, atomically publishes the new slot dictionary, and releases
only the omitted holds belonging to that game. Missing/malformed resumptions
cannot clear the previous owner. Empty Lua arrays encoded as `{}` are accepted.
An old connection's late acknowledgements, sends and final cleanup cannot release
or alter the replacement's ownership. Holds by another game remain independent.

EOF retains slots, held anchors and adopted session-folder claims. Pooled far
tile owners, including anchor-independent global far tiles, remain in the file
cache's in-use set. Normal connected anchors do not pin every tile in that
anchor: idle, unheld cached tiles remain eligible for reclamation. Mutable state
is changed on the event loop; tile workers only read the registry. No registry
lock is held while entering the material pool's lock.

There is deliberately no timer which assumes that an unresponsive game deleted
its objects. A game which never reconnects can retain its holds until server
shutdown. Clients without the new capability are protected conservatively but
cannot reconcile their detached lease; repeated legacy reconnects can retain
additional resources until shutdown. Full resumable behavior requires both the
updated mod and companion. This does not repair ownership in an older server.

## Outgoing traffic limits

The mod retains at most 8 MiB / 1,024 encoded outgoing frames. The memory count
includes the already-sent prefix of a partial head frame, which remains allocated
until that frame completes. Partial writes preserve FIFO and framing. The queue
uses head/tail indices and drops completed strings rather than shifting all
entries on every write.

All sends share a limit of 256 KiB and 32 socket calls per update, including
messages queued by callers after that update. Socket operations remain
non-blocking. The network profiler exposes pending bytes, retained bytes and
queued message counts. A fatal write/queue overflow returns false and closes the
transport; its complete reconnect snapshot reconciles any acknowledgements lost
with the outgoing queue. A new stream never receives a previous partial frame's
tail.

The companion bounds each transport's pending JSON write buffer to 8 MiB. It
aborts an overflowing/replaced transport rather than waiting indefinitely for a
blocked peer to drain it. The game's resource lease remains reserved. Connection
cleanup cancels and awaits its owned tasks; running shared tile producers keep
their existing separate ownership protection.

These limits concern pending encoded traffic. They are not total process-memory
limits: temporary JSON encoding, kernel socket buffers, decoded incoming messages,
map caches and engine objects have their own lifetimes. This change does not add
route segmentation, increase pre-cache capacity or guarantee that remote map data
arrives faster than a high-speed vehicle.

## Reproduction and acceptance

```sh
python tools/run_tests.py test_stream_reconnect test_tile_ownership test_matslots test_connection_cleanup test_world_cache test_demand test_prefetch_chain --par 3
python tools/run_lua_tests.py test_net_backpressure test_core_stubbed test_core_robustness
# Longer offline reconnection soak (PowerShell: $env:BNE_STREAM_SOAK_CYCLES='1000'):
BNE_STREAM_SOAK_CYCLES=1000 python tools/run_tests.py test_stream_reconnect --par 1
```

The normal regression uses 128 sequential file replacements/reconnections. A
separate run completed 1,000 cycles with one active game lease and at most two
pooled slots. Each prior file remains unchanged while held. After final release
and EOF there are zero leases, zero waiting slots and two reusable slots. This
establishes bounded ownership in that workload, not a bound on process RSS or
every world-cache entry.

The Lua socket fixture delivers 10,000 ordered frames and also covers blocked,
partial and fatal writes, the byte/message caps, frame budgets and preservation
of the token. Core scene/reconnect tests verify the actual outgoing hello and
the connection-event ordering requirement. Python cases cover overlapping
connections, lost acknowledgements, malformed snapshots, independent games,
global tile cache retention, old session folders and companion buffer overflow.

Validation is offline on Windows/Python 3.10, with LuaJIT/Lua scene stubs. It does
not establish driving frame times, geographic loading lead, collision behavior,
Linux runtime behavior or resolution of native game crashes. The manual drive,
teleport, multiplayer and reconnect checks in
[`STABILITY_VALIDATION.md`](STABILITY_VALIDATION.md) remain required in BeamNG.

Recorded validation on Windows / Python 3.10.11:

- 331 targeted Python tests across 13 modules: 325 passed, six optional checks
  skipped (five external-location gas-station checks and the legacy command-line
  LuaJIT check; Lua modules were separately run through Lupa). This includes all
  53 server-session tests, 32 protocol-agreement tests and 14 reconnect cases.
- 93 Lua modules passed. The backpressure fixture has 48 assertions, including
  the delayed hello/connection-generation cases.
- 1,000 real loopback reconnections completed with at most two pooled slots and
  one lease; final release left zero leases/waiting slots and two reusable slots.
- 39-module Python foundation run: 37 modules passed. `test_cache` retains two
  pre-existing Windows symlink privilege errors; `test_process_lifecycle`
  retains three pre-existing `guard.json` fixture timeouts. The full foundation
  gate is not green on this machine.
- Player and BeamMP packages built; the player archive contains the exact new
  server module and Lua transport sources. No release was published.

The existing mesh drive test now snapshots its selected set on the server's
event loop. Reading `focus` on the test thread could otherwise observe the new
pose before its selection finished, incorrectly treating a changing step as
quiet. This changes the observer's synchronization, not the streaming selector
or the expected coverage/swap assertions.
