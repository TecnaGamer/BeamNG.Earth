"""Connection-local ownership for the forthcoming native signal stream.

No data imports, planning or engine calls. A caller captures a ticket before
dispatching native work and offers its result only for that exact snapshot.
This is not enabled by the current live protocol yet.
"""
from dataclasses import dataclass
import copy
import json


@dataclass(frozen=True)
class Ticket:
    epoch: int
    generation: int
    anchor: int


class Snapshot:
    def __init__(self):
        self.enabled = False
        self.epoch = 0
        self.generation = 0
        self.revision = 0
        self.anchor = None
        self.pending = None
        self.accepted = None

    def reset(self, enabled=False, anchor=None):
        """Disconnect, option change or anchor replacement invalidates all work.

        The caller restores stock visibility when disabling/changing anchors.
        Revisions never restart within this object, including reconnects.
        """
        if type(enabled) is not bool or enabled and (type(anchor) is not int or anchor < 0):
            raise ValueError('explicit enabled flag and anchor required')
        self.epoch += 1
        self.generation += 1
        self.enabled, self.anchor = enabled, anchor if enabled else None
        self.pending = self.accepted = None

    def begin(self):
        """New surface/context generation; keep the accepted render meanwhile."""
        if not self.enabled:
            return None
        self.generation += 1
        self.pending = None
        return Ticket(self.epoch, self.generation, self.anchor)

    def current(self, ticket):
        return self.enabled and ticket == Ticket(self.epoch, self.generation, self.anchor)

    def offer(self, ticket, parts, owners):
        """Freeze a complete successful result; pending/unsafe plans aren't offers.

        Parts already contain native poses and fixed forest optical shapes. No
        sorting or mounting inference here. Owners are stock approach identities
        which may be suppressed only following a matching client acceptance.
        """
        if not self.current(ticket):
            return None
        if not isinstance(parts, list) or len(parts) > 4096:
            raise ValueError('bounded complete signal parts required')
        if not isinstance(owners, (list, tuple)) or len(owners) > 512 or any(
                not isinstance(o, str) or not o or len(o) > 128 for o in owners) or len(set(owners)) != len(owners):
            raise ValueError('unique stock approach owners required')
        if not parts and owners:
            raise ValueError('empty replacement cannot own stock approaches')
        keys = set()
        for p in parts:
            if not isinstance(p, dict) or not isinstance(p.get('key'), str) or not p['key'] or p['key'] in keys:
                raise ValueError('unique native part keys required')
            keys.add(p['key'])
        # Reject NaN, unserialisable values and unexpectedly huge packets before
        # publishing; retain the preceding accepted generation on any failure.
        payload = json.dumps(parts, allow_nan=False, separators=(',', ':'))
        if len(payload.encode('utf-8')) > 4 * 1024 * 1024:
            raise ValueError('signal packet too large')
        self.revision += 1
        packet = dict(format=1, revision=self.revision, epoch=self.epoch,
                      anchor=self.anchor, parts=json.loads(payload))
        self.pending = (ticket, packet, tuple(owners))
        return copy.deepcopy(packet)

    def acknowledge(self, epoch, anchor, revision):
        """Return owners permitted to replace stock, or None for stale replies."""
        if any(type(v) is not int for v in (epoch, anchor, revision)):
            return None
        p = self.pending
        if not p or not self.current(p[0]) or (epoch, anchor, revision) != (
                self.epoch, self.anchor, p[1]['revision']):
            return None
        self.accepted, self.pending = p, None
        return p[2]

    def suppression(self):
        """Only confirmed ownership; never suppress for an incomplete build."""
        return self.accepted[2] if self.enabled and self.accepted else ()
