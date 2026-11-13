"""Guard rails along a rural road's edge where the ground falls away beside it (GitHub #1063: Trollstigen's hairpins and
Ørnevegen above Geiranger had no barrier on the drop side; a car could be driven off the edge). OpenStreetMap maps
few roadside rails outside some countries (guardrails.py draws the mapped ones), so where none is mapped a rule puts
the stock steel rail along a paved rural road wherever the ground DROP_REACH_M beyond the rail lies DROP_M or more
below the road: a high fill, a cliff, a hillside road's valley side. Roadside practice warrants one about there
(AASHTO's Roadside Design Guide and EN 1317's national guides: an embankment of about 3 m steeper than 1:3).

Runs: a station every MODULE_STEP_M of arc length from the road part's start (the same stations in every tile), a
station warranted where the drop is; gaps of GAP_STATIONS or fewer closed, runs shorter than MIN_STATIONS left out,
each run carried LEAD_STATIONS further at both ends (as a rail starts before the hazard), a cap at each end. No module
on another road's carriageway (a junction opens the rail), near a mapped barrier (it stands for this one), on a deck
(its parapet) or in town (spec.urban). The smooth face is toward the road. Only stations within WINDOW_M of the tile
are looked at, the tile keeps the modules it owns (geotiles.box_owns).

BNE_DROP_RAILS=on turns them on (off by default until they are checked in game)."""
import hashlib
import math
import os

import numpy as np
import dmath
import dnp

import geotiles

VERSION = "drop-rails-1"
CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified"}
OUT_M = 0.75                # m from the carriageway's edge to the rail's line
DROP_M = 3.0                # m the ground falls below the road ...
DROP_REACH_M = (2.0, 4.0, 6.0, 8.0)     # ... at one of these distances beyond the rail
MODULE_STEP_M = 2.8         # guardrails' module step and length
MODULE_LENGTH_M = 3.0
MODULE_HEIGHT_M = 0.9
GAP_STATIONS = 2            # a gap this short (~6 m) in a run is closed
MIN_STATIONS = 4            # a run shorter (~11 m) is left out
LEAD_STATIONS = 2           # stations a run is carried on past its last warranted one, both ends
WINDOW_M = 60.0             # m round the tile whose stations are looked at (more than a run's closing and lead)
CLEAR_M = 0.55              # m a module keeps from another carriageway (euguardrails._clear's margin)
MAPPED_M = 2.5              # m: a mapped barrier this near stands for the rail
G_RAIL, G_START, G_END = 0, 1, 2     # guardrails.GROUPS: Italy's rail and its caps


def enabled():
    return os.environ.get("BNE_DROP_RAILS", "off").lower() in {"1", "on", "true"}


def key():
    """The road tiles' revisit key part: VERSION while on, nothing otherwise (every key built without them unchanged)."""
    return (VERSION,) if enabled() else ()


def wanted(spec):
    return (spec is not None and spec.kind == "paved" and spec.cls in CLASSES and not spec.urban
            and float(spec.width) > 0)


def runs(flags, gap=GAP_STATIONS, min_len=MIN_STATIONS, lead=LEAD_STATIONS):
    """Bool array (n,): the stations a rail covers, from flags (n,) bool, the warranted ones. Pure."""
    f = np.asarray(flags, bool).copy()
    n = len(f)
    idx = np.flatnonzero(f)
    for a, b in zip(idx[:-1], idx[1:]):     # close short gaps
        if 1 < b - a <= gap + 1:
            f[a:b] = True
    out = np.zeros(n, bool)
    i = 0
    while i < n:
        if not f[i]:
            i += 1
            continue
        j = i
        while j < n and f[j]:
            j += 1
        if j - i >= min_len:
            out[max(0, i - lead):min(n, j + lead)] = True
        i = j
    return out


def _clear(q, segs, margin):
    """Whether points q (m, 2) are each clear of every carriageway segment of segs (guardrails._road_segments)."""
    if segs is None:
        return np.ones(len(q), bool)
    v = q[:, None, :] - segs["a"][None]
    s = np.clip((v * segs["unit"][None]).sum(2), 0, segs["len"][None])
    d = dnp.norm(v - s[..., None] * segs["unit"][None], axis=2)
    return ~(d < segs["half"][None] + margin).any(1)


def _near(q, a, b, reach):
    """Whether points q (m, 2) are each within reach of one of segments a-b (k, 2)."""
    if not len(a):
        return np.zeros(len(q), bool)
    d = b - a
    t = np.clip(((q[:, None, :] - a[None]) * d[None]).sum(2) / np.maximum((d * d).sum(1), 1e-9)[None], 0, 1)
    return (dnp.norm(q[:, None, :] - (a[None] + t[..., None] * d[None]), axis=2) < reach).any(1)


def _ground(frame, elev, xy):
    """The ground's z (frame metres) at points xy (m, 2)."""
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    h = np.asarray(elev(la, lo), np.float64).reshape(-1)
    return np.asarray(frame.geodetic_to_enu(la, lo, h)[2], np.float64).reshape(-1)


def _row(group, p, a, b, za, zb, z, rank):
    forward = np.array([b[0] - a[0], b[1] - a[1], zb - za], np.float64)
    forward /= max(float(dnp.norm(forward)), 1e-9)
    up = np.array([0.0, 0.0, 1.0]) - forward[2] * forward
    up /= max(float(dnp.norm(up)), 1e-9)
    back = np.cross(up, forward)
    back /= max(float(dnp.norm(back)), 1e-9)
    yaw = math.degrees(dmath.atan2(float(forward[1]), float(forward[0])))
    return [group, float(p[0]), float(p[1]), float(z), yaw, 0.0, MODULE_HEIGHT_M, rank,
            *map(float, forward), *map(float, back), *map(float, up)]


def tile_rows(frame, near_roads, box, elev, mapped=(), decks=()):
    """Forest rows (guardrails.forest_rows' layout and GROUPS) of road tile box: near_roads build_tile's roads_out
    (fid, spec, centreline (k, 3), ...), elev(lat, lon) the ground, mapped the tile's barrier rows so far, decks
    guardrails' deck dicts. [] when off."""
    if not enabled():
        return []
    import euguardrails
    import guardrails
    x0, y0, x1, y1 = box[:4]
    rsegs = guardrails._road_segments(near_roads)
    ra, rb = euguardrails._rail_segments(mapped)
    deck_segs = guardrails._deck_segments(decks)
    out, seen = [], set()
    for fid, spec, line, *_ in near_roads:
        if not wanted(spec):
            continue
        line = np.asarray(line, np.float64)
        if line.ndim != 2 or len(line) < 2 or line.shape[1] < 3:
            continue
        ident = (str(fid), tuple(np.round(line[0, :2], 2)), tuple(np.round(line[-1, :2], 2)))
        if ident in seen:
            continue
        seen.add(ident)
        seg = dnp.hypot(*np.diff(line[:, :2], axis=0).T)
        line = line[np.r_[True, seg > 1e-6]]
        if len(line) < 2:
            continue
        arc = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(line[:, :2], axis=0).T))]
        s = np.arange(0.5 * MODULE_STEP_M, arc[-1], MODULE_STEP_M)
        if len(s) < MIN_STATIONS:
            continue
        p = np.c_[np.interp(s, arc, line[:, 0]), np.interp(s, arc, line[:, 1]), np.interp(s, arc, line[:, 2])]
        near = ((p[:, 0] > x0 - WINDOW_M) & (p[:, 0] < x1 + WINDOW_M)
                & (p[:, 1] > y0 - WINDOW_M) & (p[:, 1] < y1 + WINDOW_M))
        if not near.any():
            continue
        k0, k1 = int(np.flatnonzero(near)[0]), int(np.flatnonzero(near)[-1]) + 1
        s, p = s[k0:k1], p[k0:k1]
        sa, sb = s - 0.5 * MODULE_LENGTH_M, s + 0.5 * MODULE_LENGTH_M
        ca = np.c_[np.interp(sa, arc, line[:, 0]), np.interp(sa, arc, line[:, 1])]
        cb = np.c_[np.interp(sb, arc, line[:, 0]), np.interp(sb, arc, line[:, 1])]
        t = cb - ca
        t /= np.maximum(dnp.hypot(*t.T), 1e-9)[:, None]
        left = np.c_[-t[:, 1], t[:, 0]]
        off = float(spec.width) / 2.0 + OUT_M
        for side in (1.0, -1.0):        # (+1: the left of the line's direction)
            n = left * side
            q, a, b = p[:, :2] + n * off, ca + n * off, cb + n * off
            ground = np.stack([_ground(frame, elev, q + n * d) for d in DROP_REACH_M], 1)
            drop = (p[:, 2:3] - ground).max(1)
            ok = _clear(q, rsegs, CLEAR_M) & ~_near(q, ra, rb, MAPPED_M)
            if deck_segs is not None:
                ok &= np.array([guardrails._relate(qq, deck_segs, tt) is None for qq, tt in zip(q, t)], bool)
            cover = runs((drop >= DROP_M) & ok) & ok
            if not cover.any():
                continue
            za, zb, zq = _ground(frame, elev, a), _ground(frame, elev, b), _ground(frame, elev, q)
            # the smooth face (-back, the right of forward) toward the road: forward along the line on its left side
            idx = list(range(len(q))) if side > 0 else list(range(len(q)))[::-1]
            if side < 0:
                a, b, za, zb = b, a, zb, za
            prev = False
            for m, k in enumerate(idx):
                if not cover[k]:
                    prev = False
                    continue
                rank = int.from_bytes(hashlib.blake2b(f"{fid}:{side}:{round(float(s[k]), 1)}".encode(),
                                                      digest_size=4).digest(), "big") / 4294967296.0
                if geotiles.box_owns(box, q[k, 0], q[k, 1]):
                    out.append(_row(G_RAIL, q[k], a[k], b[k], za[k], zb[k], zq[k], rank))
                if not prev and geotiles.box_owns(box, a[k, 0], a[k, 1]):       # the run's first module: its cap
                    out.append(_row(G_START, a[k], a[k], b[k], za[k], zb[k], za[k], rank))
                nxt = idx[m + 1] if m + 1 < len(idx) else None
                if (nxt is None or not cover[nxt]) and geotiles.box_owns(box, b[k, 0], b[k, 1]):
                    out.append(_row(G_END, b[k], a[k], b[k], za[k], zb[k], zb[k], rank))
                prev = True
    return out
