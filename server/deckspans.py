"""Where the bridge decks are before their road tiles arrive, for the mod's deck guard (deckguard.lua; 2026-11-15).

A bridge deck is part of its road tile, and the terrain block, which arrives first, already has the gap under it (the
river, the valley, the road below). When the tile is late the car drives into the gap: the user, in a BeamMP session in
Baltimore, "I've fallen off the road many times because a small bridge didn't generate in time" (its road tiles waited
on road plans from other processes: median 6.8 s, p90 15.7 s, max 46 s, at 40-160 mph).

This lists the bridges near the car from the map cells already on disk (no download of its own, as reach.py) as a few
polylines in the anchor's world, sent as `deck_spans` {anchorId, cx, cy, r, spans, abut}: spans [[x0, y0, x1, y1, ...]],
each a chain of the map's tagged bridge pieces of drivable roads (reach.RANK's classes) joined at their shared ends;
abut its ends' flags (1: the first end, 2: the last, on the ground: no other bridge piece ends there). The mod knows
whether each place's road tile is in (roads.lua deckAt) and its block's ground under it, so it slows the car down
before a deck whose tile has not come, and only then.

Only the map's own bridge / level tags (roads.tag_level_ranges, as a stand-in block's road paint: no lidar decision
waited for); the decks the road tiles infer from the elevation (gradesep, lidar) are not listed. Nothing here changes
what a plan, block or tile builds. Deterministic: the pieces by feature id, the chains in that order, coordinates
rounded to 0.1 m.
"""
import logging
import math
import time

import numpy as np

import dmath
import dnp

log = logging.getLogger("earthd.deckspans")

RADIUS_M = 1200.0       # m round the car the spans are listed for (the guard looks at most ~900 m ahead)
CELL_MARGIN_M = 400.0   # m beyond it the map cells are read (a long bridge's piece listed by a cell farther off)
MOVE_M = 150.0          # m the car moves before the list is made again ...
EVERY_S = 4.0           # ... or this long (cells read meanwhile)
RESEND_S = 20.0         # s after which the same list is sent again (the mod's level started again: it has none)
JOIN_Q = 0.05           # m: pieces' ends this near (rounded to it) are one point
MAX_PTS = 64            # points of a span at most (thinned evenly, its ends kept)
MAX_SPANS = 400         # spans in a message at most (the nearest)
MAX_CELLS = 24          # cells kept read at most
PROTO = 1               # hello.deckGuard: the mod's deck guard (deckguard.lua PROTO)
WAIT_KEYS = 8           # pose.deckWait: road geotile keys taken at most


def wait_keys(v):
    """The road geotiles (gx, gy) the mod's deck guard waits on (pose.deckWait: their keys 'g<z>_<x>_<y>', the
    geotiles' zoom only; anything else left out): built first (roads.GeoTracker deck_first)."""
    import geotiles
    if isinstance(v, dict):         # (an empty Lua table may encode as {})
        v = list(v.values())
    out = set()
    if not isinstance(v, list):
        return out
    for k in v[:WAIT_KEYS]:
        got = geotiles.parse(k) if isinstance(k, str) else None
        if got is not None and got[0] == geotiles.Z:
            out.add((got[1], got[2]))
    return out


def drivable(feats):
    """(n,) bool: the features a car drives on (reach.RANK's classes, subtype road)."""
    import reach
    n = len(feats)
    cls = feats.attrs.get("class")
    if cls is None:
        return np.zeros(n, bool)
    ok = np.fromiter((str(c) in reach.RANK for c in cls.tolist()), bool, n)
    sub = feats.attrs.get("subtype")
    if sub is not None:
        ok &= np.asarray(sub) == "road"
    return ok


def cell_pieces(feats, frame):
    """[(fid, (k, 2) xy in frame's world)] of the tagged bridge pieces of a cell's drivable roads, in feature order."""
    import geotiles
    import mapgeom
    import roads
    out = []
    if feats is None or len(feats) == 0:
        return out
    ok = drivable(feats)
    ranges = roads._tag_ranges_list(feats)
    for i in np.flatnonzero(ok).tolist():
        if feats.kind[i] != mapgeom.LINE:
            continue
        pieces = [(a, b) for a, b, k in ranges[i] if k == "bridge" and b - a > roads.MIN_PART]
        if not pieces:
            continue
        fid = str(feats.ids[i])
        rings = [ring for part in feats.parts(i) for ring in part if len(ring) >= 2]
        for r in roads.sub_polylines(rings, pieces):
            if len(r) < 2:
                continue
            r = np.asarray(r, np.float64)
            x, y, _ = geotiles.geodetic_to_world(frame, r[:, 1], r[:, 0])
            out.append((fid, np.stack([np.asarray(x, np.float64), np.asarray(y, np.float64)], 1)))
    return out


def _end(xy, i):
    p = xy[i]
    return (int(round(float(p[0]) / JOIN_Q)), int(round(float(p[1]) / JOIN_Q)))


def chains(pieces):
    """[(xy (k, 2), abut bits)] of pieces [(fid, xy)] joined end to end where exactly two pieces' ends meet (a long
    bridge mapped as several segments: one span from abutment to abutment). abut bit 1: the first end is no other
    piece's end (the ground), bit 2: the last. Pure; the same pieces in the same order give the same chains."""
    ends = {}
    for k, (_fid, xy) in enumerate(pieces):
        for side, i in ((0, 0), (1, len(xy) - 1)):
            ends.setdefault(_end(xy, i), []).append((k, side))
    used = [False] * len(pieces)

    def other(node, k):
        at = ends.get(node, ())
        if len(at) != 2:
            return None
        a, b = at
        nxt = b if a[0] == k else a
        return None if nxt[0] == k or used[nxt[0]] else nxt

    out = []
    for k0, (_fid, xy0) in enumerate(pieces):
        if used[k0]:
            continue
        used[k0] = True
        pts = [p for p in np.asarray(xy0, np.float64)]
        # forwards from the last end, then backwards from the first
        for forward in (True, False):
            k = k0
            while True:
                node = _end(np.asarray(pts), len(pts) - 1 if forward else 0)
                nxt = other(node, k)
                if nxt is None:
                    break
                k, side = nxt
                used[k] = True
                xy = np.asarray(pieces[k][1], np.float64)
                seq = list(xy) if side == 0 else list(xy[::-1])     # (from the shared end on)
                if forward:
                    pts.extend(seq[1:])
                else:
                    pts[:0] = seq[::-1][:-1]
        arr = np.asarray(pts, np.float64)
        bits = (1 if len(ends.get(_end(arr, 0), ())) == 1 else 0) | (2 if len(ends.get(_end(arr, len(arr) - 1), ()))
                                                                          == 1 else 0)
        out.append((arr, bits))
    return out


def thin(xy, n=MAX_PTS):
    """xy with at most n points: evenly picked, both ends kept."""
    if len(xy) <= n:
        return xy
    idx = np.unique(np.round(np.linspace(0, len(xy) - 1, n)).astype(np.int64))
    return xy[idx]


def line_dist(xy, x, y):
    """m from (x, y) to the polyline xy (k >= 2, 2)."""
    a, b = xy[:-1], xy[1:]
    ab = b - a
    L2 = np.maximum((ab * ab).sum(1), 1e-12)
    t = np.clip(((x - a[:, 0]) * ab[:, 0] + (y - a[:, 1]) * ab[:, 1]) / L2, 0.0, 1.0)
    return float(np.min(dnp.hypot(a[:, 0] + t * ab[:, 0] - x, a[:, 1] + t * ab[:, 1] - y)))


def message(aid, x, y, pieces, radius=RADIUS_M):
    """The deck_spans message for the car at (x, y) of anchor aid from the pieces [(fid, xy)] round it: the chains
    that come within radius, nearest first (MAX_SPANS of them), coordinates rounded to 0.1 m."""
    seen, uniq = set(), []
    for fid, xy in sorted(pieces, key=lambda p: (p[0], float(p[1][0][0]), float(p[1][0][1]))):
        key = (fid, tuple(np.round(xy[0], 2)), tuple(np.round(xy[-1], 2)), len(xy))
        if key in seen:                 # (a feature listed by several cells: once)
            continue
        seen.add(key)
        uniq.append((fid, xy))
    near = []
    for xy, bits in chains(uniq):
        d = line_dist(xy, x, y)
        if d <= radius:
            near.append((d, len(near), xy, bits))
    near.sort(key=lambda t: (t[0], t[1]))
    spans, abut = [], []
    for _d, _k, xy, bits in near[:MAX_SPANS]:
        xy = thin(xy)
        spans.append([round(float(v), 1) for v in xy.reshape(-1)])
        abut.append(int(bits))
    return {"type": "deck_spans", "anchorId": int(aid), "cx": round(float(x), 1), "cy": round(float(y), 1),
            "r": float(radius), "spans": spans, "abut": abut}


class Spans:
    """Per connection: the bridge pieces of the map cells round the car (read once a cell, kept per anchor) and when
    the list was last made. due() on the event loop, make() in a worker thread (one at a time)."""

    def __init__(self, mapdata):
        self.md = mapdata
        self.aid = None
        self.cells = {}         # str(cell key) -> its pieces in the anchor's world
        self.last = None        # (aid, x, y, monotonic) of the last list made
        self.sent = None        # (aid, cx, cy, spans, abut, monotonic) last sent
        self.busy = False
        self.stats = {"made": 0, "sent": 0, "cells": 0, "ms": 0.0}

    def due(self, aid, x, y, now=None):
        if self.busy or self.md is None:
            return False
        now = time.monotonic() if now is None else now
        last = self.last
        return last is None or last[0] != aid or math.hypot(x - last[1], y - last[2]) >= MOVE_M \
            or now - last[3] >= EVERY_S

    def keys_near(self, frame, x, y, r):
        import geotiles
        import mapcells
        lla = geotiles.world_to_geodetic(frame, np.array([[float(x), float(y), 0.0]]))
        la, lo = float(lla[0][0]), float(lla[1][0])
        dla = r / 111320.0
        dlo = r / (111320.0 * max(0.05, dmath.cos(math.radians(la))))
        return sorted(mapcells.cells_for_bbox(la - dla, lo - dlo, la + dla, lo + dlo), key=str)

    def make(self, aid, frame, x, y):
        """The message for the car at (x, y) of anchor aid (a worker thread), or None: nothing new to send (the same
        spans round the same place)."""
        t0 = time.perf_counter()
        self.last = (aid, x, y, time.monotonic())
        if aid != self.aid:
            self.aid, self.cells = aid, {}
        keys = self.keys_near(frame, x, y, RADIUS_M + CELL_MARGIN_M)
        want = {str(k) for k in keys}
        for sk in [sk for sk in self.cells if sk not in want]:
            del self.cells[sk]
        for k in keys:
            sk = str(k)
            if sk in self.cells or len(self.cells) >= MAX_CELLS:
                continue
            try:
                if not self.md.has(k, "roads"):
                    continue            # (not on disk yet: the next list reads it)
                feats = self.md.layer_unkept(k, "roads", kind="background")
                self.cells[sk] = cell_pieces(feats, frame)
                self.stats["cells"] += 1
            except Exception as e:      # (a cell that cannot be read: no spans from it)
                log.debug("deck spans: cell %s: %s: %s", sk, type(e).__name__, e)
        pieces = [p for sk in sorted(self.cells) for p in self.cells[sk]]
        msg = message(aid, x, y, pieces)
        self.stats["made"] += 1
        self.stats["ms"] += (time.perf_counter() - t0) * 1000.0
        s = self.sent
        now = time.monotonic()
        if s is not None and s[0] == aid and s[3] == msg["spans"] and s[4] == msg["abut"] \
                and math.hypot(s[1] - msg["cx"], s[2] - msg["cy"]) < MOVE_M and now - s[5] < RESEND_S:
            return None
        self.sent = (aid, msg["cx"], msg["cy"], msg["spans"], msg["abut"], now)
        self.stats["sent"] += 1
        return msg
