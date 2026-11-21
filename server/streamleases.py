"""Game resource ownership survives TCP connections. Mutations run on the event loop.

Tile workers only read held(): snapshots replace dictionaries atomically, never clear and refill them.
No timeout establishes that a disconnected game has stopped drawing a pooled file.
"""
from dataclasses import dataclass, field
import secrets

import fast

_native = fast.native(557, "stream leases")
integer = _native.lease_integer
snapshot = _native.lease_snapshot
Registry = _native.StreamLeases
slots_gone = _native.slots_gone
attach_stream = _native.attach_stream
claim_sessions = _native.claim_sessions
slot_held = _native.slot_held
slots_let_go = _native.slots_let_go
held_anchors = _native.held_anchors


@dataclass
class Lease:
    connection: object
    resumable: bool
    token: str = field(default_factory=lambda: secrets.token_hex(16))
    slots: dict = field(default_factory=dict)
    anchors: set = field(default_factory=set)
    sessions: set = field(default_factory=set)
    detached_at: float = None       # (time.monotonic of its last connection's close; None while connected)
