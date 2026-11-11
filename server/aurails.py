"""Australia's roadside crash barriers (the user, in-game testing in Queensland: "very common on the M1, A1 and other
highways but missing"). Upstream draws only the barriers that are mapped (guardrails.forest_rows) and the median ones
(medians.py); a sealed rural highway's own edge barriers are not in the map data. So where the road itself says one stands,
a rule puts one down, with upstream's models (the stock Italy W-beam, the mod's four-rope cable barrier: no new asset):

- the outside of a bend whose radius needs slowing for (under CURVE_RADIUS_M, under TIGHT_RADIUS_M always, else where the
  ground falls away), a steel W-beam;
- along a fill: the graded ground beside the road (elev: lifted under roads, with their embankments) falls DROP_M or more
  within DROP_SPAN_M of the road's edge: a steel W-beam, or on a road whose hash says so (and on a 100 km/h motorway, trunk
  or primary) the wire-rope barrier of Queensland's highways when the fall is under STEEP_DROP_M;
- both sides of a bridge's approach, APPROACH_NEAR_M to APPROACH_M from the deck's end, a W-beam, with end terminals.

The roads: sealed motorways, trunk and primary roads (secondary from 80 km/h) outside towns without footpaths (a motorway
inside a town too), no ramps, nor a side facing another carriageway of the road (the median is medians.py's: its AU style
is wider now). Nothing across a junction (OPENING_M past a crossing road's edge), within CLEAR_M of a mapped or median
barrier, or on the median side. Centre wire rope on undivided roads is not drawn (it needs the painted median's width and the
junction openings, which are not in the data).

Tile independence: modules lie on a grid of arc length along a whole ground part (STEP_M), a tile keeps those whose middle
is in it, and each is judged from the road and the ground within MARGIN_M of it, so tiles agree. Called from
blockworker._kantstolpe_rows for Australia only (before the guide posts and warning signs, which keep clear of its rows).
"""
import hashlib
import math

import numpy as np

import guardrails

COUNTRIES = {"AU"}
CLASSES = {"motorway", "trunk", "primary"}
MIN_KMH = 80.0
DEFAULT_KMH = 100.0             # (a numbered road with no limit mapped: rural)
SECONDARY_KMH = 80.0
OUT_M = 1.5                     # m outside the sealed edge (spec.width includes the sealed shoulders)
STEP_M = guardrails.MODULE_STEP_M
TURN_M = 20.0                   # a bend is read over this much road either side of a station
CURVE_RADIUS_M = 400.0          # the outside of a bend tighter than this has a barrier where the ground falls ...
TIGHT_RADIUS_M = 200.0          # ... and one tighter than this always
CURVE_DROP_M = 1.0
DROP_M = 2.0                    # m the ground falls from the barrier line to DROP_SPAN_M further out: a fill
DROP_SPAN_M = 6.0
STEEP_DROP_M = 3.5              # a fall this deep is always a steel rail
CABLE_SHARE = 0.5               # of the fast roads' fills, by road
CABLE_KMH = 100.0
CLOSE_GAP = 4                   # stations: a gap this short in a run is closed ...
MIN_RUN = 3                     # ... and a run of fewer is dropped
APPROACH_NEAR_M, APPROACH_M = 4.0, 40.0     # m from a bridge deck's end: the approach rail
END_ROAD_M = 6.0                # m: a deck's end this near a road is the road's bridge
OPENING_M = 4.0                 # m clear past a crossing road's edge
CLEAR_M = 3.0                   # m from a mapped or median barrier
MEDIAN_REACH_M = 30.0           # m: a carriageway of the same kind this near and parallel: the median is on that side
PARALLEL_COS = 0.8
MARGIN_M = 40.0                 # m past the tile a road is judged
STEEL, CABLE = guardrails.GROUPS.index("guardrail"), guardrails.G_CABLE
CAP_START, CAP_END = 1, 2


def wanted(spec):
    """Whether the edges of a road of this spec get barriers (see the module doc)."""
    if spec.kind != "paved" or spec.sides or spec.link:
        return False
    if spec.cls == "motorway":
        return True
    if spec.urban:
        return False
    if spec.cls in CLASSES:
        return spec.kmh is None or spec.kmh >= MIN_KMH
    return spec.cls == "secondary" and spec.kmh is not None and spec.kmh >= SECONDARY_KMH


def _hash01(*vals):
    h = hashlib.blake2b(repr(vals).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64


def _nearest(points, line):
    """(distance (n,), nearest point (n, 2), unit direction of that segment (n, 2)) of points (n, 2) to polyline line."""
    line = np.asarray(line, np.float64)[:, :2]
    a, b = line[:-1], line[1:]
    d = b - a
    l2 = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((points[:, None, :] - a[None]) * d[None]).sum(2) / l2[None], 0.0, 1.0)
    q = a[None] + t[..., None] * d[None]
    dist = np.hypot(*(q - points[:, None, :]).transpose(2, 0, 1))
    k = dist.argmin(1)
    i = np.arange(len(points))
    return dist[i, k], q[i, k], d[k] / np.sqrt(l2[k])[:, None]


def _mapped_dist(points, rails):
    """Distance (n,) of points (n, 2) to the segments rails (m, 4) (inf if none)."""
    if not len(rails):
        return np.full(len(points), np.inf)
    out = np.full(len(points), np.inf)
    for lo in range(0, len(rails), 200):
        r = rails[lo:lo + 200]
        a, b = r[:, :2], r[:, 2:]
        d = b - a
        l2 = np.maximum((d * d).sum(1), 1e-12)
        t = np.clip(((points[:, None, :] - a[None]) * d[None]).sum(2) / l2[None], 0.0, 1.0)
        q = a[None] + t[..., None] * d[None]
        out = np.minimum(out, np.hypot(*(q - points[:, None, :]).transpose(2, 0, 1)).min(1))
    return out


def _grid(xy):
    """(stations (n, 2), unit tangents (n, 2), arc lengths (n,), radius (n,), turn sign (n,) +1 left) of polyline xy
    every STEP_M of arc length from its start."""
    seg = np.hypot(*np.diff(xy, axis=0).T)
    xy = xy[np.r_[True, seg > 1e-6]]
    cum = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    s = np.arange(0.0, cum[-1], STEP_M)

    def at(d):
        d = np.clip(d, 0.0, cum[-1])
        return np.c_[np.interp(d, cum, xy[:, 0]), np.interp(d, cum, xy[:, 1])]
    p = at(s)
    d = at(s + 1.0) - at(s - 1.0)
    t = d / np.maximum(np.hypot(*d.T), 1e-9)[:, None]
    a, b = p - at(s - TURN_M), at(s + TURN_M) - p
    turn = np.arctan2(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0], (a * b).sum(1))
    turn[(s < TURN_M) | (s > cum[-1] - TURN_M)] = 0.0       # (the line's ends: straight)
    radius = TURN_M / np.maximum(np.abs(turn), 1e-9)
    return p, t, s, radius, np.where(turn >= 0, 1.0, -1.0)


def _close(mask, gap=CLOSE_GAP, minimum=MIN_RUN):
    """mask (n,) with its runs of True gap or fewer apart joined, then those shorter than minimum dropped."""
    out = mask.copy()
    idx = np.flatnonzero(mask)
    for i, j in zip(idx[:-1], idx[1:]):
        if 1 < j - i <= gap + 1:
            out[i:j] = True
    i, n = 0, len(out)
    while i < n:
        if not out[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and out[j + 1]:
            j += 1
        if j - i + 1 < minimum:
            out[i:j + 1] = False
        i = j + 1
    return out


def _dilate(mask, k):
    out = mask.copy()
    for s in range(1, k + 1):
        out[s:] |= mask[:-s]
        out[:-s] |= mask[s:]
    return out


def _ground(frame, elev, xy):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64).ravel()


def _row(group, p, forward, z0, z1, height, rank):
    """A Forest row (guardrails.forest_rows' layout) of a module at p (2,) running along unit forward (2,) from height z0
    to z1 over its length: the model's visible face on the right of forward, its back on the left."""
    f = np.array([forward[0] * STEP_M, forward[1] * STEP_M, z1 - z0], np.float64)
    f /= np.linalg.norm(f)
    up = np.array([0.0, 0.0, 1.0]) - f[2] * f
    up /= np.linalg.norm(up)
    back = np.cross(up, f)
    back /= np.linalg.norm(back)
    return [group, round(float(p[0]), 3), round(float(p[1]), 3), round(float((z0 + z1) / 2), 3),
            round(math.degrees(math.atan2(f[1], f[0])), 2), 0.0, height, round(rank, 4),
            float(f[0]), float(f[1]), float(f[2]), float(back[0]), float(back[1]), float(back[2]),
            float(up[0]), float(up[1]), float(up[2])]


def tile_rows(frame, near_roads, box, elev, country, mapped=(), decks=()):
    """Forest rows (guardrails.GROUPS) of the edge barriers of road tile box: near_roads build_tile's roads_out, elev(lat,
    lon) the ground, mapped the tile's barrier rows so far, decks the bridges' parts ((fid, spec, xy, z, lon)). []
    outside Australia."""
    if (country or "").upper() not in COUNTRIES:
        return []
    import medians
    import motesfri
    x0, y0, x1, y1 = box
    rails = medians._mapped_segments(mapped)
    ends = np.array([e for _f, _s, xy, *_ in decks for e in (np.asarray(xy, np.float64)[0, :2],
                                                              np.asarray(xy, np.float64)[-1, :2])]).reshape(-1, 2)
    end_dirs = np.array([d for _f, _s, xy, *_ in decks for d in _end_dirs(np.asarray(xy, np.float64)[:, :2])]).reshape(-1, 2)
    out = []
    for fid, spec, pts, *_ in near_roads:
        if not wanted(spec) or len(pts) < 2:
            continue
        xy = np.asarray(pts, np.float64)[:, :2]
        if xy[:, 0].max() < x0 - MARGIN_M or xy[:, 0].min() > x1 + MARGIN_M \
                or xy[:, 1].max() < y0 - MARGIN_M or xy[:, 1].min() > y1 + MARGIN_M:
            continue
        p, t, s, radius, turn_sign = _grid(xy)
        n = len(p)
        if n < MIN_RUN + 1:
            continue
        near = (p[:, 0] >= x0 - MARGIN_M) & (p[:, 0] < x1 + MARGIN_M) & (p[:, 1] >= y0 - MARGIN_M) & (p[:, 1] < y1 + MARGIN_M)
        if not near.any():
            continue
        half = spec.width / 2
        nl = np.c_[-t[:, 1], t[:, 0]]               # (left of the geometry)
        kmh = DEFAULT_KMH if spec.kmh is None else float(spec.kmh)
        cable_road = (spec.cls in CLASSES and kmh >= CABLE_KMH and _hash01("aurail", str(fid)) < CABLE_SHARE)
        idx = np.flatnonzero(near)
        # the bridge approaches: stations 4-40 m from a deck's end that lies on this road, going the same way
        approach = np.zeros(n, bool)
        if len(ends):
            dist, _q, tg = _nearest(ends, xy)
            for e, de, d0, tg0 in zip(ends, end_dirs, dist, tg):
                if d0 > END_ROAD_M or abs(float(de @ tg0)) < PARALLEL_COS:
                    continue
                dd = np.hypot(*(p[idx] - e).T)
                approach[idx[(dd >= APPROACH_NEAR_M) & (dd <= APPROACH_M)]] = True
        # the other roads: openings (crossing roads) and the median side (a parallel carriageway of its own kind)
        crossing, median = [], np.zeros((n, 2), bool)
        for f2, s2, p2, *_ in near_roads:
            if f2 == fid or len(p2) < 2 or s2.cls not in motesfri.OPEN_CLASSES:
                continue
            xy2 = np.asarray(p2, np.float64)[:, :2]
            if xy2[:, 0].max() < x0 - MARGIN_M - 40 or xy2[:, 0].min() > x1 + MARGIN_M + 40 \
                    or xy2[:, 1].max() < y0 - MARGIN_M - 40 or xy2[:, 1].min() > y1 + MARGIN_M + 40:
                continue
            crossing.append((xy2, s2.width / 2 + OPENING_M, s2.cls == spec.cls, s2.cls == spec.cls and bool(s2.oneway)
                             and bool(spec.oneway) and not s2.link))
        for xy2, reach, same, sibling in crossing:
            d2, q2, t2 = _nearest(p[idx], xy2)
            if sibling:
                par = (d2 < MEDIAN_REACH_M) & (np.abs((t2 * t[idx]).sum(1)) >= PARALLEL_COS)
                side_of = ((q2 - p[idx]) * nl[idx]).sum(1) > 0
                median[idx[par & side_of], 0] = True
                median[idx[par & ~side_of], 1] = True
        # the ground beside the road, each side: at the barrier's line and DROP_SPAN_M past it
        sides = {}
        for k, side in ((0, 1.0), (1, -1.0)):
            q = p + nl * side * (half + OUT_M)
            qf = p[idx] + nl[idx] * side * (half + OUT_M + DROP_SPAN_M)
            z_q = np.full(n, np.nan)
            z_q[idx] = _ground(frame, elev, q[idx])
            drop = np.zeros(n)
            drop[idx] = z_q[idx] - _ground(frame, elev, qf)
            block = np.zeros(n, bool)
            for xy2, reach, same, sibling in crossing:
                if sibling:
                    continue
                d2, _q2, t2 = _nearest(q[idx], xy2)
                # (not the road's own next way, which carries on from the end of this one the same way)
                block[idx] |= (d2 <= reach) & ~(same & (np.abs((t2 * t[idx]).sum(1)) >= PARALLEL_COS))
            block[idx] |= _mapped_dist(q[idx], rails) <= CLEAR_M
            block |= median[:, k]
            outer = turn_sign == -side      # (a left turn's outside is the right-hand side)
            tight = outer & (radius < TIGHT_RADIUS_M)
            curve = outer & (radius < CURVE_RADIUS_M) & (drop >= CURVE_DROP_M)
            fill = drop >= DROP_M
            steel = tight | curve | approach | (drop >= STEEP_DROP_M)
            want = (steel | fill) & near & ~block
            want = _close(want)
            steel_near = _dilate(steel & want, 3)
            kind = np.where(steel_near | (not cable_road), STEEL, CABLE)
            sides[side] = (q, z_q, want, kind, block)
        for side, (q, z_q, want, kind, block) in sides.items():
            # a module joins stations i and i + 1: kept where both are wanted, clear, and the middle is in the tile
            kept = want[:-1] & want[1:] & ~block[:-1] & ~block[1:] & np.isfinite(z_q[:-1]) & np.isfinite(z_q[1:])
            mid = (q[:-1] + q[1:]) / 2
            own = (mid[:, 0] >= x0) & (mid[:, 0] < x1) & (mid[:, 1] >= y0) & (mid[:, 1] < y1)
            for i in np.flatnonzero(kept & own):
                group = int(kind[i])
                height = guardrails.HEIGHTS.get(group, guardrails.MODULE_HEIGHT_M)
                rank = (i * 0.6180339887) % 1.0
                a, b = q[i], q[i + 1]
                za, zb = float(z_q[i]), float(z_q[i + 1])
                fwd = (b - a) / max(float(np.hypot(*(b - a))), 1e-9)
                # (the visible face is on the right of forward: on the road's left the way it runs, on its right reversed)
                if side > 0:
                    out.append(_row(group, (a + b) / 2, fwd, za, zb, height, rank))
                else:
                    out.append(_row(group, (a + b) / 2, -fwd, zb, za, height, rank))
                if group != STEEL:
                    continue
                first = not (i > 0 and kept[i - 1])
                last = not (i + 1 < len(kept) and kept[i + 1])
                if side > 0:
                    if first:
                        out.append(_cap(CAP_START, a, fwd, za, zb, rank))
                    if last:
                        out.append(_cap(CAP_END, b, fwd, za, zb, rank))
                else:
                    if first:       # (the run's first module going its way is its far end)
                        out.append(_cap(CAP_END, a, -fwd, zb, za, rank))
                    if last:
                        out.append(_cap(CAP_START, b, -fwd, zb, za, rank))
    return out


def _end_dirs(xy):
    """Unit directions of a deck's two ends (xy (k, 2))."""
    d0, d1 = xy[min(1, len(xy) - 1)] - xy[0], xy[-1] - xy[max(len(xy) - 2, 0)]
    return [d / max(float(np.hypot(*d)), 1e-9) for d in (d0, d1)]


def _cap(group, p, forward, z0, z1, rank):
    """A terminal's row at point p (2,) of the module running along unit forward (2,) from z0 to z1."""
    r = _row(group, p, forward, z0, z1, guardrails.MODULE_HEIGHT_M, rank)
    r[3] = round(float(z0 if group == CAP_START else z1), 3)
    return r
