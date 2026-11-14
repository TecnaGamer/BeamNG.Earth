"""Game resource ownership survives TCP connections. Mutations run on the event loop.

Tile workers only read held(): snapshots replace dictionaries atomically, never clear and refill them.
No timeout establishes that a disconnected game has stopped drawing a pooled file.
"""
from dataclasses import dataclass, field
import math
import secrets
import time


def integer(value):
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, float) and math.isfinite(value) and value.is_integer())


def snapshot(value, made):
    """Validate the complete snapshot before changing ownership. Lua's empty array may be {}."""
    if value == {}:
        value = []
    if not isinstance(value, list):
        raise ValueError("slotsHeld must be a complete array")
    out = {}
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("invalid held-slot pair")
        n, serial = pair
        if not integer(n) or not 0 <= n < made or not integer(serial) or not 0 < serial <= 2**53 - 1:
            raise ValueError("invalid held-slot number or serial")
        out[int(n)] = max(out.get(int(n), 0), int(serial))
    return out


@dataclass
class Lease:
    connection: object
    resumable: bool
    token: str = field(default_factory=lambda: secrets.token_hex(16))
    slots: dict = field(default_factory=dict)
    anchors: set = field(default_factory=set)
    sessions: set = field(default_factory=set)
    detached_at: float = None       # (time.monotonic of its last connection's close; None while connected)


class Registry:
    def __init__(self):
        self.entries = {}

    def attach(self, connection, token, slots, anchors, resumable):
        existing = getattr(connection, "lease", None)
        if existing is not None:
            return existing, set(), None  # repeated hello cannot discard not-yet-delivered sends
        lease = self.entries.get(token) if isinstance(token, str) and resumable else None
        if lease is None or not lease.resumable:
            lease = Lease(connection, resumable)
            self.entries[lease.token] = lease
        previous = lease.connection
        released = set(lease.slots) - set(slots) if slots is not None else set()
        if slots is not None:
            lease.slots = slots
        lease.anchors = anchors
        lease.connection = connection
        lease.detached_at = None
        connection.lease = lease
        connection.slot_holds = lease.slots
        connection.held = set(anchors)
        connection.sessions = set(lease.sessions)
        return lease, released, previous if previous is not connection else None

    def detach(self, connection):
        lease = getattr(connection, "lease", None)
        if lease is None:
            return False
        if lease.connection is connection:
            lease.anchors = set(connection.held)
            if connection.active is not None:
                lease.anchors.add(connection.active)
            lease.sessions = set(connection.sessions)
            lease.connection = None
            lease.detached_at = time.monotonic()
            if not lease.slots and not lease.anchors and not lease.sessions:
                self.entries.pop(lease.token, None)
        return True  # superseded cleanup must not release the new connection's resources

    def expire(self, now, grace):
        """The leases detached longer than grace s (merge of PR #817: without it, a game that closed and never came
        back kept its pooled slots, anchors and session folders until the server stopped). A running game reconnects
        within seconds (net.lua retries); one gone this long is not coming back with these objects. Removed and
        returned, for the caller to release."""
        gone = [lease for lease in tuple(self.entries.values())
                if lease.connection is None and lease.detached_at is not None and now - lease.detached_at >= grace]
        for lease in gone:
            self.entries.pop(lease.token, None)
        return gone

    def held(self, n):
        return any(n in lease.slots for lease in tuple(self.entries.values()))

    def anchors(self):
        out = set()
        for lease in tuple(self.entries.values()):
            c = lease.connection
            if c is None:
                out |= lease.anchors
        return out

    def sessions(self):
        out = set()
        for lease in tuple(self.entries.values()):
            out |= lease.sessions if lease.connection is None else lease.connection.sessions
        return out
