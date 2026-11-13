"""One-way roads meet a two-way road at their own lane, not its centre line (the user, 2026-10-25: one-way pairs ended in
the middle of the two-way road they join, and the reverse).

Overture (and OSM) join a one-way to a two-way at the two-way's centre line, so the one-way's carriageway, drawn about
its own centre, lands across the two-way's middle. Here each such end moves to the side of the two-way where traffic
goes the one-way's way: right of its direction of travel (left in left-hand countries), by a quarter of the two-way's
width, and the move fades out along the one-way over BLEND_M. Applied once to the roads layer when a cell is built
(mapdata), so the meshes, the grading and the AI graph all see the same lines.

BNGE_ONEWAY_JOINS=0 turns it off.
"""
import math
import os

import numpy as np
import dmath

ON = os.environ.get("BNGE_ONEWAY_JOINS", "1") != "0"
BLEND_M = 20.0
TWO_WAY_W_M = 8.0         # a two-way's width when OpenStreetMap gives no lane count
LANE_M = 3.3
MAX_SHIFT_M = 5.0
KEY_DEG = 1e-6


def _oneway(feats, i):
    v = feats.attrs.get("osm_oneway")
    if v is None:
        return 0
    s = str(v[i])
    return 1 if s in ("yes", "1", "true") else -1 if s == "-1" else 0


def apply(feats, left_hand=False):
    """Moves one-way ends in place (feats.lonlat). Returns the number of ends moved."""
    if not ON or len(feats) == 0 or "osm_oneway" not in feats.attrs:
        return 0
    ll = feats.lonlat
    co, ro, po = feats.coord_off, feats.ring_off, feats.part_off
    rings = []          # (feature, first coord, last coord) of each line's first ring
    for i in range(len(feats)):
        r = ro[po[i]]
        rings.append((i, int(co[r]), int(co[r + 1]) - 1))
    at = {}             # rounded point -> [(feature, coord index)] for every coordinate of every line
    for i, a, b in rings:
        for c in range(a, b + 1):
            at.setdefault((round(ll[c, 0] / KEY_DEG), round(ll[c, 1] / KEY_DEG)), []).append((i, c))
    lanes = feats.attrs.get("osm_lanes")
    moved = 0
    for i, a, b in rings:
        ow = _oneway(feats, i)
        if not ow or b - a < 1:
            continue
        for end, nxt, sign in ((a, a + 1, 1), (b, b - 1, -1)):
            key = (round(ll[end, 0] / KEY_DEG), round(ll[end, 1] / KEY_DEG))
            others = [(j, c) for j, c in at.get(key, ()) if j != i]
            two = [(j, c) for j, c in others if _oneway(feats, j) == 0]
            if not two or any(_oneway(feats, j) for j, _ in others):
                continue        # only a one-way meeting two-way roads (a one-way chain, or three one-ways: left alone)
            lat = math.radians(ll[end, 1])
            mx = 111320.0 * dmath.cos(lat)
            my = 110540.0
            # direction of travel at this end: along the line (end -> next) times the line's own sense, made to point
            # the way traffic goes
            dx = (ll[nxt, 0] - ll[end, 0]) * mx * sign
            dy = (ll[nxt, 1] - ll[end, 1]) * my * sign
            dx, dy = dx * ow, dy * ow
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            dx, dy = dx / L, dy / L
            j = two[0][0]
            w2 = TWO_WAY_W_M
            if lanes is not None and not np.isnan(lanes[j]):
                w2 = float(lanes[j]) * LANE_M
            off = min(w2 / 4.0, MAX_SHIFT_M)
            rx, ry = (dy, -dx) if not left_hand else (-dy, dx)
            ox, oy = rx * off / mx, ry * off / my
            # fade the move along the line over BLEND_M
            run = 0.0
            c, prev = end, end
            while a <= c <= b:
                if c != end:
                    run += math.hypot((ll[c, 0] - ll[prev, 0]) * mx, (ll[c, 1] - ll[prev, 1]) * my)
                t = max(0.0, 1.0 - run / BLEND_M)
                if t <= 0:
                    break
                ll[c, 0] += ox * t
                ll[c, 1] += oy * t
                prev = c
                c += sign
            moved += 1
    return moved
