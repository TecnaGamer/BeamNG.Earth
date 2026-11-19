"""structprobe.py LAT LON R [--cache DIR] [--json OUT] [--timing OUT] [--offline] [--server DIR]

The road revamp's structure probe (track RS, step S0: .claude/work/road-revamp-structures-design-2026-11-18.md). It
builds the road tiles round a point the way the server does (blockworker.roads_tile, as tileprobe / deckedges /
rampcheck) and measures TODAY's bridge decks as one structure per level would draw them:

  overlap_m2      drawn deck tops (the slab tops, DECK_TOP, and the junction pads') of one level overlapping: two faces
                  within FACE_DZ of each other at one point of a RASTER_M raster (z-fighting or a slab in a slab)
  hole_m2         the ideal union of the members' footprints (each deck chunk's own ribbon before any trim, merge or
                  pad: its deck frame with the walks) not covered by a drawn deck top within FACE_DZ; the free ends'
                  last END_EXCL_M left out (an abutment's end is not a hole)
  parapet_in_cw_m parapet (and junction pad railing) length standing inside a carriageway (a deck's or a ground road's,
                  more than CW_INSET_M inside its edge) within PARAPET_DZ of its height
  seam_step       deck outline steps at the joints of two deck chunks meeting end to end (deckedges.py's test, here
                  from 5 cm and with the height step too)
  twin_dz         the height difference across two close parallel decks of one level (the BESIDE rule: gap <= 2.5 m
                  over >= 20 m, within 25 degrees, |dz| <= 1 m)
  skew_err_deg    at a free deck end near a road / railway crossing under its deck, the angle between the drawn end
                  line and the crossed line's direction (a square end over a skew crossing: 90 - the crossing angle)
  groups          the structures the design's grouping rules (its section "Grouping") WOULD form from the members: a
                  prediction only, nothing built from it; "simple": the groups (singles too) of one or two road decks
                  with no END_ON / OVERLAP edge (road revamp v2 P1b's simple structures, roadshape)
  simple          the P1b targets on the simple groups' members alone (road revamp v2, BNE_ROADSHAPE=decks):
                  chord_err_m   the drawn deck centre line (its ribbon rows, sampled between them) off the smooth line
                                through the member's own rows (centripetal Catmull-Rom): the chord rule as drawn
                  smooth_dev_m  the drawn deck centre line off the road's smooth line (earthfast.deck_shape's curve of
                                the member and the roads going on from its ends, here, to 1 mm; None without the
                                kernel): a deck kept on the map's chord across a curving road shows here
                  end_kink_deg  at a free end with a road going on (its end within JOIN_ROAD_M), the angle between the
                                drawn deck's direction and that road's (DIR_M of each)
                  seam_step, skew_err_deg, parapet_in_cw_m as above, on those members
  deck_tris       the deck meshes' triangles (all colliding) per tile that has one: total, mean, max

Members are the bridge parts as the tiles get them (bridges.parts, shifted as roads.shifted_parts: the deck_pads input),
their carriageway half width spec.width / 2. Everything is rounded and sorted: the same code and data give the same JSON
byte for byte. Wall clock and memory go to --timing only. Items count only inside R of the point.

A measurement tool (Python is fine here); nothing in the game reads it. The scratch cache must not be the player's
~/.cache/beamng-earth (refused) nor link into it. tools/roadcorpus/run.py build --probe struct runs it per corpus place.
The metric functions are pure (plain arrays in, numbers out): tests/test_structprobe.py checks them on small cases."""
import argparse
import json
import logging
import os
import resource
import sys
import time

import numpy as np

RASTER_M = 0.25         # m: the raster of the overlap and hole tests
FACE_DZ = 0.3           # m: two faces this near in height are one level
END_EXCL_M = 1.0        # m of a member's free end left out of the hole test
JOIN_M = 1.5            # m: chunk ends this near meet (deckedges.py, rampcheck.py)
CUT_EPS = 0.05          # m: a chunk end this near its tile's box edge is the tile's cut, not the deck's end
CW_INSET_M = 0.25       # m inside a carriageway's edge a parapet counts as standing in it
PARAPET_DZ = 1.2        # m: a parapet within this of a carriageway's height blocks it (rampcheck BARRIER_DZ)
PARAPET_STEP = 0.25     # m between parapet samples
SEAM_M = 0.05           # m: a deck edge step over this counts
SEAM_TURN_DEG = 30.0    # chunks meeting end to end run on within this (deckedges.py)
TWIN_STEP = 2.0         # m between the twin test's samples
TWIN_OVER = 0.02        # m: the design's twin target
TWIN_TOUCH_M = 0.5      # m two twins' carriageways may overlap (one deck); more is an overlap (overlap_m2's)
SKEW_REACH_M = 40.0     # m along the deck from a free end to the crossing it is the end of
SKEW_OVER = 3.0         # degrees: the design's skew target
SKEW_NEAR_M = 1.0       # m: a crossing nearer the end than this is a road joining it, not crossed
SKEW_MIN_CROSS = 15.0   # degrees: a line meeting the deck flatter than this runs along it
SKEW_BEND_DEG = 30.0    # degrees: a deck turning more between the crossing and its end is not that crossing's end
GROUP_STEP = 1.0        # m between the grouping tests' samples
BIN_M = 8.0             # m: overlap / hole cells pooled for the worst-places list
JOIN_ROAD_M = 0.75      # m: a ground road's end this near a deck's free end goes on from it (end_kink_deg)
DIR_M = 2.0             # m of a deck / road its end direction is read over (end_kink_deg: the tangent there)
CHORD_SAMPLE_M = 0.25   # m between the samples of the drawn deck centre line (chord_err_m)
OWN_M = 4.0             # m: a drawn ribbon's samples this near a member's line are that member's (chord_err_m)
WALK_CLASSES = ("footway", "path", "cycleway", "pedestrian", "steps", "bridleway", "corridor", "track_foot")
# the design's grouping rules (section "Grouping"): name -> (|dz| limit m, geometry)
JOIN_END_M, JOIN_DZ = 0.5, 0.3
END_ON_DZ = 0.5
ALONG_MIN_M, ALONG_DEG, ALONG_DZ = 2.0, 30.0, 1.0
CROSS_DZ = 0.6
BESIDE_GAP_M, BESIDE_MIN_M, BESIDE_DEG, BESIDE_DZ = 2.5, 20.0, 25.0, 1.0
BESIDE_SHORT = 0.8      # ... or this share of the shorter deck when that is shorter (an addition to the design's rule:
                        # E6 Vist's twins on one ~17 m deck never run 20 m side by side)


def beside_min(la, lb):
    """m two parallel decks of lengths la, lb must run side by side (BESIDE, BESIDE_OUTLINE, twin_dz)."""
    return min(BESIDE_MIN_M, BESIDE_SHORT * min(la, lb))
OUTLINE_GAP_M = 6.0
CONFLICT_DZ = 2.0       # m: one OSM level, a geometry test met, farther apart than this: a data conflict (the design)
EDGE_ORDER = ("JOIN", "END_ON", "OVERLAP_ALONG", "OVERLAP_CROSS", "BESIDE", "BESIDE_OUTLINE")

_OFF = 1 << 24          # raster keys: (i + _OFF) * _MUL + (j + _OFF)
_MUL = 1 << 26
_JX, _JY = 0.0123457, 0.0098765     # m: the raster's sample points off the cell centres (no mesh vertex lies on one)


def _dnp():
    import dnp
    return dnp


# ---------------------------------------------------------------- raster
def raster_tris(tri, cell=RASTER_M):
    """Samples of triangles tri (n, 3, 3) at the raster's points inside them: (keys int64 (m,), z (m,), tri index (m,)).
    Either winding; triangles under 1e-9 m^2 in plan left out. Basic arithmetic only (bit-stable)."""
    tri = np.asarray(tri, np.float64).reshape(-1, 3, 3)
    empty = (np.zeros(0, np.int64), np.zeros(0), np.zeros(0, np.int64))
    if not len(tri):
        return empty
    area = (tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) - \
        (tri[:, 1, 1] - tri[:, 0, 1]) * (tri[:, 2, 0] - tri[:, 0, 0])
    tri = np.where((area < 0)[:, None, None], tri[:, [0, 2, 1]], tri)     # (all counter-clockwise)
    area = np.abs(area)
    ax, ay, az = tri[:, 0, 0], tri[:, 0, 1], tri[:, 0, 2]
    bx, by, bz = tri[:, 1, 0], tri[:, 1, 1], tri[:, 1, 2]
    cx, cy, cz = tri[:, 2, 0], tri[:, 2, 1], tri[:, 2, 2]

    def owns(px, py, qx, qy):
        """A point on edge p -> q belongs to its triangle (the top-left rule: a shared edge runs the other way in
        the neighbour, which then does not own it: no point counted twice, none dropped)."""
        dy, dx = qy - py, qx - px
        return (dy < 0) | ((dy == 0) & (dx < 0))

    def side(px, py, qx, qy, x, y):
        """Twice the signed area of (p, q, point), > 0 left of p -> q; from the edge's lexicographically first end,
        so a shared edge seen the other way gives exactly the negated value (its rounding the same)."""
        swap = (px > qx) | ((px == qx) & (py > qy))
        ox, oy = np.where(swap, qx, px), np.where(swap, qy, py)
        ex, ey = np.where(swap, px, qx), np.where(swap, py, qy)
        w = (ex - ox) * (y - oy) - (ey - oy) * (x - ox)
        return np.where(swap, -w, w)
    xmin, xmax = np.minimum(np.minimum(ax, bx), cx), np.maximum(np.maximum(ax, bx), cx)
    ymin, ymax = np.minimum(np.minimum(ay, by), cy), np.maximum(np.maximum(ay, by), cy)
    i0 = np.ceil((xmin - _JX) / cell - 0.5).astype(np.int64)
    i1 = np.floor((xmax - _JX) / cell - 0.5).astype(np.int64)
    j0 = np.ceil((ymin - _JY) / cell - 0.5).astype(np.int64)
    j1 = np.floor((ymax - _JY) / cell - 0.5).astype(np.int64)
    ni, nj = np.maximum(i1 - i0 + 1, 0), np.maximum(j1 - j0 + 1, 0)
    cnt = ni * nj
    cnt[np.abs(area) < 2e-9] = 0
    keys, zs, owners = [], [], []
    order = np.flatnonzero(cnt)
    start = 0
    while start < len(order):           # (batches of about 4 M samples)
        csum = np.cumsum(cnt[order[start:]])
        stop = start + max(1, int(np.searchsorted(csum, 4_000_000, side="right")))
        idx = order[start:stop]
        start = stop
        c = cnt[idx]
        rep = np.repeat(np.arange(len(idx)), c)
        loc = np.arange(int(c.sum())) - np.repeat(np.cumsum(c) - c, c)
        t = idx[rep]
        ii = i0[t] + loc // nj[t]
        jj = j0[t] + loc % nj[t]
        x = (ii + 0.5) * cell + _JX
        y = (jj + 0.5) * cell + _JY
        w_ab = side(ax[t], ay[t], bx[t], by[t], x, y)
        w_bc = side(bx[t], by[t], cx[t], cy[t], x, y)
        w_ca = side(cx[t], cy[t], ax[t], ay[t], x, y)
        inside = (((w_ab > 0) | ((w_ab == 0) & owns(ax[t], ay[t], bx[t], by[t])))
                  & ((w_bc > 0) | ((w_bc == 0) & owns(bx[t], by[t], cx[t], cy[t])))
                  & ((w_ca > 0) | ((w_ca == 0) & owns(cx[t], cy[t], ax[t], ay[t]))))
        a_ = area[t]
        z = (w_bc * az[t] + w_ca * bz[t] + w_ab * cz[t]) / a_
        keys.append(((ii + _OFF) * _MUL + (jj + _OFF))[inside])
        zs.append(z[inside])
        owners.append(t[inside])
    if not keys:
        return empty
    return np.concatenate(keys), np.concatenate(zs), np.concatenate(owners)


def key_xy(keys, cell=RASTER_M):
    """The raster points (m, 2) of keys."""
    keys = np.asarray(keys, np.int64)
    return np.c_[(keys // _MUL - _OFF + 0.5) * cell + _JX, (keys % _MUL - _OFF + 0.5) * cell + _JY]


def in_disc(keys, R, cell=RASTER_M):
    xy = key_xy(keys, cell)
    return xy[:, 0] * xy[:, 0] + xy[:, 1] * xy[:, 1] <= R * R


def overlap_keys(keys, z, dz=FACE_DZ):
    """The raster points (unique keys) where two samples lie within dz in height (two faces of one level)."""
    keys = np.asarray(keys, np.int64)
    if len(keys) < 2:
        return np.zeros(0, np.int64)
    o = np.lexsort((z, keys))
    k, zz = keys[o], np.asarray(z, np.float64)[o]
    hit = (k[1:] == k[:-1]) & (zz[1:] - zz[:-1] <= dz)
    return np.unique(k[1:][hit])


def _cluster(keys, z, dz=FACE_DZ):
    """keys, z with the samples of one point within dz of the one before (sorted by height) dropped."""
    if not len(keys):
        return keys, z
    o = np.lexsort((z, keys))
    k, zz = keys[o], z[o]
    keep = np.r_[True, ~((k[1:] == k[:-1]) & (zz[1:] - zz[:-1] <= dz))]
    return k[keep], zz[keep]


def uncovered(ideal_keys, ideal_z, top_keys, top_z, dz=FACE_DZ):
    """(keys, z) of the ideal samples (one per point and level) no drawn sample covers within dz in height."""
    ik, iz = _cluster(np.asarray(ideal_keys, np.int64), np.asarray(ideal_z, np.float64), dz)
    if not len(ik):
        return ik, iz
    tk = np.asarray(top_keys, np.int64)
    if not len(tk):
        return ik, iz
    o = np.lexsort((top_z, tk))
    tk, tz = tk[o], np.asarray(top_z, np.float64)[o]
    lo, hi = np.searchsorted(tk, ik, "left"), np.searchsorted(tk, ik, "right")
    cov = np.zeros(len(ik), bool)
    for m in range(int((hi - lo).max()) if len(ik) else 0):
        idx = lo + m
        ok = idx < hi
        cov |= ok & (np.abs(tz[np.minimum(idx, len(tz) - 1)] - iz) <= dz)
    return ik[~cov], iz[~cov]


def worst_bins(keys, frame_ll, cell=RASTER_M, n=8):
    """[[m2, lat, lon]] of the BIN_M squares holding the most of keys (largest first, then by place)."""
    if not len(keys):
        return []
    xy = key_xy(keys, cell)
    b = np.floor(xy / BIN_M).astype(np.int64)
    u, cnt = np.unique(b, axis=0, return_counts=True)
    o = np.lexsort((u[:, 1], u[:, 0], -cnt))[:n]
    out = []
    for k in o:
        la, lo = frame_ll((u[k] + 0.5) * BIN_M)
        out.append([round(float(cnt[k]) * cell * cell, 2), la, lo])
    return out


# ---------------------------------------------------------------- geometry helpers
def arc(xy):
    xy = np.asarray(xy, np.float64)
    if len(xy) < 2:
        return np.zeros(len(xy))
    d = np.diff(xy[:, :2], axis=0)
    return np.r_[0.0, np.cumsum(_dnp().hypot(d[:, 0], d[:, 1]))]


def resample(xy, z, step, mid=True):
    """Points every `step` m along polyline xy (k, 2) with heights z (k,): (pts (m, 2), z (m,), piece length m each,
    unit direction (m, 2)); mid: the pieces' middles (each sample stands for one piece)."""
    xy = np.asarray(xy, np.float64)[:, :2]
    z = np.asarray(z, np.float64)
    s = arc(xy)
    L = float(s[-1]) if len(s) else 0.0
    if L <= 1e-9:
        return np.zeros((0, 2)), np.zeros(0), 0.0, np.zeros((0, 2))
    n = max(1, int(np.ceil(L / step - 1e-9)))
    piece = L / n
    t = (np.arange(n) + 0.5) * piece if mid else np.linspace(0.0, L, n + 1)
    pts = np.c_[_dnp().interp(t, s, xy[:, 0]), _dnp().interp(t, s, xy[:, 1])]
    zz = _dnp().interp(t, s, z)
    seg = np.clip(np.searchsorted(s, t, side="right") - 1, 0, len(xy) - 2)
    d = xy[seg + 1] - xy[seg]
    ln = np.maximum(_dnp().hypot(d[:, 0], d[:, 1]), 1e-12)
    return pts, zz, piece, d / ln[:, None]


def angle_deg(u, v):
    """The unsigned angle (0-90 degrees) between undirected lines of unit directions u, v (n, 2)."""
    dnp = _dnp()
    u, v = np.atleast_2d(u), np.atleast_2d(v)
    cr = np.abs(u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0])
    dt = np.abs(u[:, 0] * v[:, 0] + u[:, 1] * v[:, 1])
    return np.asarray(dnp.arctan2(cr, dt), np.float64) * (180.0 / np.pi)


class SegIndex:
    """A grid over the segments of lines [(xy (k, 2), z (k,), half m, tag)]: query(P) gives, for points P (n, 2), every
    segment whose box grown by its half width + pad holds the point, with the distance, the segment's height at the
    nearest point and its unit direction."""

    def __init__(self, lines, pad=8.0, cell=16.0):
        A, B, za, zb, half, line, s0 = [], [], [], [], [], [], []
        for li, (xy, z, h, _tag) in enumerate(lines):
            xy = np.asarray(xy, np.float64)[:, :2]
            z = np.asarray(z, np.float64)
            if len(xy) < 2:
                continue
            s = arc(xy)
            A.append(xy[:-1])
            B.append(xy[1:])
            za.append(z[:-1])
            zb.append(z[1:])
            half.append(np.full(len(xy) - 1, float(h)))
            line.append(np.full(len(xy) - 1, li, np.int64))
            s0.append(s[:-1])
        self.lines = lines
        self.cell = cell
        if not A:
            self.A = np.zeros((0, 2))
            self.grid = {}
            return
        self.A, self.B = np.concatenate(A), np.concatenate(B)
        self.za, self.zb = np.concatenate(za), np.concatenate(zb)
        self.half, self.line, self.s0 = np.concatenate(half), np.concatenate(line), np.concatenate(s0)
        d = self.B - self.A
        self.len = _dnp().hypot(d[:, 0], d[:, 1])
        self.u = d / np.maximum(self.len, 1e-12)[:, None]
        grow = self.half + pad
        lo = np.floor((np.minimum(self.A, self.B) - grow[:, None]) / cell).astype(np.int64)
        hi = np.floor((np.maximum(self.A, self.B) + grow[:, None]) / cell).astype(np.int64)
        grid = {}
        for k in range(len(self.A)):
            for gx in range(lo[k, 0], hi[k, 0] + 1):
                for gy in range(lo[k, 1], hi[k, 1] + 1):
                    grid.setdefault((gx, gy), []).append(k)
        self.grid = {g: np.asarray(v, np.int64) for g, v in grid.items()}

    def query(self, P):
        """(point index, segment index, distance, t on the segment 0-1, height there, its line's arc there)."""
        P = np.asarray(P, np.float64).reshape(-1, 2)
        none = (np.zeros(0, np.int64),) * 2 + (np.zeros(0),) * 4
        if not len(P) or not self.grid:
            return none
        g = np.floor(P / self.cell).astype(np.int64)
        u, inv = np.unique(g, axis=0, return_inverse=True)
        inv = np.asarray(inv).reshape(-1)
        pis, sis = [], []
        order = np.argsort(inv, kind="stable")
        bounds = np.searchsorted(inv[order], np.arange(len(u) + 1))
        for ci in range(len(u)):
            segs = self.grid.get((int(u[ci, 0]), int(u[ci, 1])))
            if segs is None:
                continue
            pts = order[bounds[ci]:bounds[ci + 1]]
            pis.append(np.repeat(pts, len(segs)))
            sis.append(np.tile(segs, len(pts)))
        if not pis:
            return none
        pi, si = np.concatenate(pis), np.concatenate(sis)
        a = self.A[si]
        d = self.B[si] - a
        L2 = d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1]
        w = P[pi] - a
        t = np.clip((w[:, 0] * d[:, 0] + w[:, 1] * d[:, 1]) / np.maximum(L2, 1e-24), 0.0, 1.0)
        q = a + t[:, None] * d
        dist = _dnp().hypot(P[pi, 0] - q[:, 0], P[pi, 1] - q[:, 1])
        z = self.za[si] + t * (self.zb[si] - self.za[si])
        return pi, si, dist, t, z, self.s0[si] + t * self.len[si]


def nearest_per_line(pi, si, dist, line):
    """The query rows kept to the nearest segment of each (point, line) pair: an index into the rows."""
    if not len(pi):
        return np.zeros(0, np.int64)
    o = np.lexsort((si, dist, line[si], pi))
    key_p, key_l = pi[o], line[si][o]
    first = np.r_[True, (key_p[1:] != key_p[:-1]) | (key_l[1:] != key_l[:-1])]
    return o[first]


# ---------------------------------------------------------------- metrics (pure)
def ideal_tris(ribbons, cut_ends=None):
    """Plan triangles (n, 3, 3) of the ribbons [(left (k, 2), right (k, 2), z (k,))] as built (the members' footprints
    before any trim), each free end's last END_EXCL_M left out. cut_ends: per ribbon (start cut, end cut): an end the
    tile's box cut (not free). An end is free when no other ribbon's end lies within JOIN_M."""
    ends = []
    for left, right, _z in ribbons:
        mid = (np.asarray(left) + np.asarray(right)) / 2
        ends.append((mid[0], mid[-1]))
    allends = np.array([p for e in ends for p in e], np.float64).reshape(-1, 2)
    out = []
    for ri, (left, right, z) in enumerate(ribbons):
        left, right, z = (np.asarray(a, np.float64) for a in (left, right, z))
        if len(z) < 2:
            continue
        mid = (left + right) / 2
        s = arc(mid)
        L = float(s[-1])
        free = []
        for e in (0, 1):
            if cut_ends is not None and cut_ends[ri][e]:
                free.append(False)
                continue
            p = ends[ri][e]
            d = _dnp().hypot(allends[:, 0] - p[0], allends[:, 1] - p[1])
            d[2 * ri + e] = np.inf
            free.append(not bool((d <= JOIN_M).any()))
        a = END_EXCL_M if free[0] else 0.0
        b = L - END_EXCL_M if free[1] else L
        if b - a <= 1e-6:
            continue
        keep = (s > a) & (s < b)
        t = np.r_[a, s[keep], b]
        Lr = np.c_[_dnp().interp(t, s, left[:, 0]), _dnp().interp(t, s, left[:, 1]), _dnp().interp(t, s, z)]
        Rr = np.c_[_dnp().interp(t, s, right[:, 0]), _dnp().interp(t, s, right[:, 1]), _dnp().interp(t, s, z)]
        q0, q1 = slice(0, -1), slice(1, None)
        out.append(np.stack([Lr[q0], Rr[q0], Rr[q1]], 1))
        out.append(np.stack([Lr[q0], Rr[q1], Lr[q1]], 1))
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


def overlap_hole(tops, ideal, R, frame_ll=None, pads=()):
    """overlap_m2 / hole_m2 of the drawn deck top triangles tops (n, 3, 3) and the ideal footprint triangles (m, 3, 3)
    inside R of the origin; pads: junction pad rings (k, 2+): overlap inside them is also given apart."""
    tk, tz, _ = raster_tris(tops)
    ik, iz, _ = raster_tris(ideal)
    a = RASTER_M * RASTER_M
    ov = overlap_keys(tk, tz)
    ov = ov[in_disc(ov, R)]
    hk, _hz = uncovered(ik, iz, tk, tz)
    hk = hk[in_disc(hk, R)]
    hk = np.unique(hk)
    pad_n = 0
    if len(ov) and len(pads):
        from polygon import Path
        xy = key_xy(ov)
        inside = np.zeros(len(ov), bool)
        for ring in pads:
            ring = np.asarray(ring, np.float64)[:, :2]
            if len(ring) >= 3:
                inside |= Path(ring).contains_points(xy)
        pad_n = int(inside.sum())
    top_area = len(np.unique(tk[in_disc(tk, R)])) * a if len(tk) else 0.0
    ideal_area = len(np.unique(ik[in_disc(ik, R)])) * a if len(ik) else 0.0
    ll = frame_ll or (lambda p: [round(float(p[0]), 1), round(float(p[1]), 1)])
    return {"overlap_m2": round(len(ov) * a, 2), "overlap_pad_m2": round(pad_n * a, 2),
            "hole_m2": round(len(hk) * a, 2), "top_m2": round(top_area, 2), "ideal_m2": round(ideal_area, 2),
            "overlap_worst": worst_bins(ov, ll), "hole_worst": worst_bins(hk, ll)}


def top_steps(tops, R, frame_ll=None, cell=RASTER_M):
    """Height steps in the drawn deck tops (road revamp v2 P2b: the step at the shared edge where an exit's deck meets
    its road's): raster points holding exactly one top (overlaps are overlap_m2's) whose 4-neighbour also holds
    exactly one, the two more than STEP_DZ and at most FACE_DZ apart in height (a slab's own grade and bank move
    under 3 cm between raster points; farther apart is another level). n points, m = n x the raster (an edge's
    length), max dz, the worst places, and the keys (sheetview draws them; not in the JSON)."""
    tk, tz, _ = raster_tris(tops, cell)
    tk = np.asarray(tk, np.int64)
    out = {"n": 0, "m": 0.0, "max": 0.0, "worst": [], "keys": np.zeros(0, np.int64)}
    if not len(tk):
        return out
    u, inv, cnt = np.unique(tk, return_inverse=True, return_counts=True)
    zu = np.zeros(len(u))
    zu[inv] = np.asarray(tz, np.float64)
    one = cnt == 1
    u1, z1 = u[one], zu[one]
    hit_k, hit_dz = [], []
    for step in (_MUL, 1):          # (the neighbour at +1 in i, then in j)
        j = np.searchsorted(u1, u1 + step)
        ok = j < len(u1)
        j = np.minimum(j, len(u1) - 1)
        ok &= u1[j] == u1 + step
        dz = np.abs(z1[j] - z1)
        ok &= (dz > STEP_DZ) & (dz <= FACE_DZ)
        hit_k.append(u1[ok])
        hit_dz.append(dz[ok])
    k = np.concatenate(hit_k)
    d = np.concatenate(hit_dz)
    inr = in_disc(k, R, cell) if len(k) else np.zeros(0, bool)
    k, d = k[inr], d[inr]
    ll = frame_ll or (lambda p: [round(float(p[0]), 1), round(float(p[1]), 1)])
    return {"n": int(len(k)), "m": round(len(k) * cell, 2), "max": round(float(d.max()), 3) if len(d) else 0.0,
            "worst": worst_bins(np.unique(k), ll), "keys": np.unique(k)}


STEP_DZ = 0.05          # m: a drawn top's step over this between raster points (top_steps)


def parapet_in_carriageway(runs, carriageways, R, frame_ll=None):
    """Parapet length (m) inside a carriageway within PARAPET_DZ of its height. runs [(edge (k, 2), z (k,))];
    carriageways [(xy (k, 2), z (k,), half width m, (kind, id))]. Per sample of PARAPET_STEP: inside when the nearest
    point of a carriageway's centre line is nearer than its half width - CW_INSET_M (and inside R)."""
    idx = SegIndex(carriageways, pad=1.0)
    total = {"deck": 0.0, "ground": 0.0}
    worst = {}
    for ri, (edge, z) in enumerate(runs):
        pts, zz, piece, _u = resample(edge, z, PARAPET_STEP)
        if not len(pts):
            continue
        keep = pts[:, 0] * pts[:, 0] + pts[:, 1] * pts[:, 1] <= R * R
        pi, si, dist, _t, zc, _s = idx.query(pts)
        if not len(pi):
            continue
        ok = keep[pi] & (dist < idx.half[si] - CW_INSET_M) & (np.abs(zc - zz[pi]) <= PARAPET_DZ)
        if not ok.any():
            continue
        hit_p = np.unique(pi[ok])
        # (each sample once, to the first carriageway in line order that holds it)
        lines = idx.line[si[ok]]
        first = {}
        for p, ln in sorted(zip(pi[ok].tolist(), lines.tolist())):
            first.setdefault(p, ln)
        for p in hit_p.tolist():
            kind, tag = carriageways[first[p]][3]
            total[kind] = total.get(kind, 0.0) + piece
            w = worst.setdefault((ri, kind, tag), [0.0, p])
            w[0] += piece
    ll = frame_ll or (lambda p: [round(float(p[0]), 1), round(float(p[1]), 1)])
    items = []
    for (ri, kind, tag), (m, p) in worst.items():
        pts, _zz, _piece, _u = resample(runs[ri][0], runs[ri][1], PARAPET_STEP)
        items.append([round(m, 2)] + ll(pts[p]) + [kind, str(tag)[:12]])
    items.sort(key=lambda r: (-r[0], r[1], r[2], r[3], r[4]))
    return {"m": round(total["deck"] + total["ground"], 2), "deck_m": round(total["deck"], 2),
            "ground_m": round(total["ground"], 2), "worst": items[:12]}


def seam_steps(ribbons, R, frame_ll=None):
    """Deck outline steps at the joints of chunks meeting end to end (deckedges.py's test: ends within JOIN_M, running
    on within SEAM_TURN_DEG, three or more ends at a point (a fork) left out): per joint the larger sideways jump of the
    two edges and the height step. ribbons [(left (k, 2), right (k, 2), z (k,), tag)]."""
    dnp = _dnp()
    ends = []                       # (point, dir away, left edge, right edge, z, ribbon)
    for ri, (left, right, z, _tag) in enumerate(ribbons):
        left, right, z = (np.asarray(a, np.float64) for a in (left, right, z))
        mid = (left + right) / 2
        if len(mid) < 2:
            continue
        for p, q, l_, r_, zz in ((mid[0], mid[1], right[0], left[0], z[0]), (mid[-1], mid[-2], left[-1], right[-1], z[-1])):
            d = p - q
            ln = float(dnp.hypot(d[0], d[1]))
            ends.append((p, d / max(ln, 1e-9), l_, r_, float(zz), ri))
    P = np.array([e[0] for e in ends], np.float64).reshape(-1, 2)
    cos_on = float(dnp.cos(np.radians(SEAM_TURN_DEG)))
    lat_s, z_s = [], []
    worst = []
    for i, (p, d, l_, r_, zi, ri) in enumerate(ends):
        dd = dnp.hypot(P[:, 0] - p[0], P[:, 1] - p[1])
        near = np.flatnonzero(dd <= JOIN_M)
        if len(near) > 2 or p[0] * p[0] + p[1] * p[1] > R * R:
            continue
        for j in near:
            if j <= i or ends[j][5] == ri:
                continue
            q, e, l2, r2, zj, _rj = ends[j]
            if float(d[0] * e[0] + d[1] * e[1]) > -cos_on:
                continue
            lat = np.array([-d[1], d[0]])
            jl = float((l_ - p)[0] * lat[0] + (l_ - p)[1] * lat[1])
            jr = float((r_ - p)[0] * lat[0] + (r_ - p)[1] * lat[1])
            kl = float((r2 - p)[0] * lat[0] + (r2 - p)[1] * lat[1])
            kr = float((l2 - p)[0] * lat[0] + (l2 - p)[1] * lat[1])
            step = max(abs(jl - kl), abs(jr - kr))
            dz = abs(zi - zj)
            lat_s.append(step)
            z_s.append(dz)
            if max(step, dz) > SEAM_M:
                worst.append((round(max(step, dz), 3), round(step, 3), round(dz, 3), p))
    worst.sort(key=lambda w: (-w[0], round(float(w[3][0]), 2), round(float(w[3][1]), 2)))
    ll = frame_ll or (lambda p: [round(float(p[0]), 1), round(float(p[1]), 1)])
    lat_s, z_s = np.asarray(lat_s), np.asarray(z_s)
    return {"joints": int(len(lat_s)), "n_over": int(((lat_s > SEAM_M) | (z_s > SEAM_M)).sum()),
            "n_side_over": int((lat_s > SEAM_M).sum()), "n_z_over": int((z_s > SEAM_M).sum()),
            "max": round(float(max(lat_s.max(), z_s.max())) if len(lat_s) else 0.0, 3),
            "side_max": round(float(lat_s.max()) if len(lat_s) else 0.0, 3),
            "z_max": round(float(z_s.max()) if len(z_s) else 0.0, 3),
            "worst": [[w[0], w[1], w[2]] + ll(w[3]) for w in worst[:12]]}


def _pairs(members, step, pad):
    """For every member A's samples (every `step` m) the nearest segment of every other member B within reach:
    (A, B, sample point, A's z, A's direction, gap edge to edge, centre distance, B's z there, B's direction, B's arc,
    A's arc, sample length) arrays. Same-fid pairs and walkways with roads are left out."""
    lines = [(m["xy"], m["z"], m["half"], m["key"]) for m in members]
    idx = SegIndex(lines, pad=pad)
    out = []
    for ai, m in enumerate(members):
        pts, zz, piece, u = resample(m["xy"], m["z"], step)
        if not len(pts):
            continue
        sa = (np.arange(len(pts)) + 0.5) * piece
        pi, si, dist, tq, zb, sb = idx.query(pts)
        keep = nearest_per_line(pi, si, dist, idx.line)
        pi, si, dist, tq, zb, sb = pi[keep], si[keep], dist[keep], tq[keep], zb[keep], sb[keep]
        bi = idx.line[si]
        ok = bi != ai
        ok &= np.array([members[b]["fid"] != m["fid"] and members[b]["walk"] == m["walk"] for b in bi.tolist()],
                       bool) if len(bi) else ok
        pi, si, dist, tq, zb, sb, bi = pi[ok], si[ok], dist[ok], tq[ok], zb[ok], sb[ok], bi[ok]
        q = idx.A[si] + tq[:, None] * (idx.B[si] - idx.A[si])       # (B's nearest point)
        if not len(pi):
            continue
        hb = np.array([members[b]["half"] for b in bi.tolist()])
        out.append(dict(a=np.full(len(pi), ai), b=bi, p=pts[pi], za=zz[pi], ua=u[pi], gap=dist - m["half"] - hb,
                        dist=dist, zb=zb, ub=idx.u[si], sb=sb, sa=sa[pi], piece=np.full(len(pi), piece), q=q))
    if not out:
        return None
    return {k: np.concatenate([o[k] for o in out]) for k in out[0]}


def twin_dz(members, R, frame_ll=None):
    """Height differences across close parallel decks of one level (BESIDE: gap <= BESIDE_GAP_M, within BESIDE_DEG,
    |dz| <= BESIDE_DZ) where they run so for beside_min: max, p95 of the samples, pairs, pairs over TWIN_OVER."""
    pr = _pairs(members, TWIN_STEP, BESIDE_GAP_M + 12.0)
    lengths = [float(arc(m["xy"])[-1]) for m in members]
    ll = frame_ll or (lambda p: [round(float(p[0]), 1), round(float(p[1]), 1)])
    res = {"pairs": 0, "max": 0.0, "p95": 0.0, "n_over": 0, "worst": []}
    if pr is None:
        return res
    ang = angle_deg(pr["ua"], pr["ub"])
    dz = np.abs(pr["za"] - pr["zb"])
    ok = (pr["gap"] <= BESIDE_GAP_M) & (pr["gap"] >= -TWIN_TOUCH_M) & (ang <= BESIDE_DEG) & (dz <= BESIDE_DZ)
    pairs = {}
    for k in np.flatnonzero(ok).tolist():
        a, b = int(pr["a"][k]), int(pr["b"][k])
        ka, kb = members[a]["key"], members[b]["key"]
        pairs.setdefault((min(ka, kb), max(ka, kb)), []).append(k)
    vals, worst = [], []
    for (ka, kb), ks in sorted(pairs.items()):
        # (from both sides: each side's samples measure its own length beside the other)
        by = {}
        for k in ks:
            by.setdefault(int(pr["a"][k]), []).append(k)
        length = max(float(pr["piece"][v].sum()) for v in by.values())
        if length < beside_min(lengths[int(pr["a"][ks[0]])], lengths[int(pr["b"][ks[0]])]):
            continue
        inr = [k for k in ks if float(pr["p"][k][0] * pr["p"][k][0] + pr["p"][k][1] * pr["p"][k][1]) <= R * R]
        if not inr:
            continue
        d = dz[inr]
        vals.extend(d.tolist())
        k = inr[int(np.argmax(d))]
        worst.append([round(float(d.max()), 3)] + ll(pr["p"][k]) + [ka[:10], kb[:10], round(length, 1)])
    if not vals:
        return res
    v = np.sort(np.asarray(vals))
    worst.sort(key=lambda w: (-w[0], w[1], w[2]))
    return {"pairs": len(worst), "max": round(float(v[-1]), 3),
            "p95": round(float(v[min(len(v) - 1, int(0.95 * len(v)))]), 3),
            "n_over": sum(1 for w in worst if w[0] > TWIN_OVER), "worst": worst[:12]}


def _crossings(a, b):
    """[(arc on a, point, unit direction of b there)] where polyline a (k, 2) crosses polyline b (m, 2)."""
    out = []
    a, b = np.asarray(a, np.float64)[:, :2], np.asarray(b, np.float64)[:, :2]
    if len(a) < 2 or len(b) < 2:
        return out
    if (a.min(0) > b.max(0)).any() or (b.min(0) > a.max(0)).any():
        return out
    sa = arc(a)
    for i in range(len(a) - 1):
        p, r = a[i], a[i + 1] - a[i]
        lo, hi = np.minimum(a[i], a[i + 1]), np.maximum(a[i], a[i + 1])
        cand = np.flatnonzero((np.maximum(b[:-1], b[1:]) >= lo).all(1) & (np.minimum(b[:-1], b[1:]) <= hi).all(1))
        for j in cand.tolist():
            q, s_ = b[j], b[j + 1] - b[j]
            den = r[0] * s_[1] - r[1] * s_[0]
            if abs(den) < 1e-12:
                continue
            qp = q - p
            t = (qp[0] * s_[1] - qp[1] * s_[0]) / den
            u = (qp[0] * r[1] - qp[1] * r[0]) / den
            if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
                ln = float(_dnp().hypot(s_[0], s_[1]))
                out.append((float(sa[i] + t * (sa[i + 1] - sa[i])), p + t * r, s_ / max(ln, 1e-12)))
    return out


def skew_errors(ends, members, crossed, R, frame_ll=None):
    """At each free member end (ends [(member index, 0 | 1 (start | end), end line unit direction (2,))]) with a
    crossed line (crossed [(xy (k, 2), tag, fid)]: not the member's own) crossing its member within SKEW_REACH_M of it: the angle between the
    drawn end line and the crossed line there (a deck turning more than SKEW_BEND_DEG between them left out, counted
    as curved; 0 = the end parallel to the crossed road, as built over a skew
    crossing), and the crossing angle (90 = square)."""
    ll = frame_ll or (lambda p: [round(float(p[0]), 1), round(float(p[1]), 1)])
    items = []
    curved = 0
    for mi, at, end_dir in ends:
        m = members[mi]
        xy = np.asarray(m["xy"], np.float64)[:, :2]
        p = xy[0] if at == 0 else xy[-1]
        if p[0] * p[0] + p[1] * p[1] > R * R:
            continue
        s = arc(xy)
        L = float(s[-1])
        best = None
        for cxy, tag, cfid in crossed:
            if cfid == m["fid"]:
                continue            # (its own road going on from the deck)
            for sa, pt, cdir in _crossings(xy, cxy):
                reach = sa if at == 0 else L - sa
                if not (SKEW_NEAR_M < reach <= SKEW_REACH_M) or (best is not None and reach >= best[0]):
                    continue
                seg = min(int(np.searchsorted(s, sa, side="right")) - 1, len(xy) - 2)
                d = xy[seg + 1] - xy[seg]
                ln = float(_dnp().hypot(d[0], d[1]))
                mdir = d / max(ln, 1e-12)
                if float(angle_deg(mdir, cdir)[0]) < SKEW_MIN_CROSS:
                    continue        # (running along it, not crossed)
                best = (reach, pt, cdir, mdir, tag)
        if best is None:
            continue
        reach, pt, cdir, mdir, tag = best
        t = xy[-1] - xy[-2] if at == 1 else xy[1] - xy[0]
        t = t / max(float(_dnp().hypot(t[0], t[1])), 1e-12)
        if float(angle_deg(t, mdir)[0]) > SKEW_BEND_DEG:
            curved += 1         # (the deck turns between the crossing and its end: not that crossing's end)
            continue
        err = float(angle_deg(np.asarray(end_dir, np.float64), cdir)[0])
        cross = float(angle_deg(mdir, cdir)[0])
        items.append([round(err, 2), round(cross, 2)] + ll(p) + [m["key"][:10], str(tag)[:16], round(reach, 1)])
    items.sort(key=lambda r: (-r[0], r[2], r[3], r[4]))
    errs = np.array([r[0] for r in items])
    return {"ends": len(items), "max": round(float(errs.max()), 2) if len(errs) else 0.0,
            "mean": round(float(errs.mean()), 2) if len(errs) else 0.0,
            "n_over": int((errs > SKEW_OVER).sum()), "curved": curved, "worst": items[:12]}


def predict_groups(members, outlines=(), R=None, frame_ll=None):
    """The structures the design's grouping rules would form (a prediction): union-find over the members in sorted-key
    order, footprints quantised to 1 mm. members [{"key", "fid", "cls", "xy" (k, 2), "z" (k,), "half", "walk"}];
    outlines: OSM man_made=bridge outline rings (k, 2) (BESIDE_OUTLINE's evidence). Edges (per unordered pair the first
    of EDGE_ORDER that holds): JOIN, END_ON, OVERLAP_ALONG, OVERLAP_CROSS, BESIDE, BESIDE_OUTLINE, each within its
    height limit (members' "level": their OSM layer / Overture level, None unknown; an edge across two levels is
    counted (cross_level_edges), one level farther apart than CONFLICT_DZ where a test's geometry holds is a data
    conflict) (BESIDE's length: beside_min, the design's 20 m or 80 % of a shorter deck; its gap from -TWIN_TOUCH_M); walkways never join roads (rail and tunnels are not bridge parts). Pairs that pass a geometry test
    but not its height limit are counted per edge kind (an overpass is OVERLAP_CROSS over its limit)."""
    dnp = _dnp()
    ll = frame_ll or (lambda p: [round(float(p[0]), 1), round(float(p[1]), 1)])
    members = sorted(members, key=lambda m: m["key"])
    for m in members:
        m["xy"] = np.round(np.asarray(m["xy"], np.float64)[:, :2], 3)
        m["z"] = np.round(np.asarray(m["z"], np.float64), 3)
    n = len(members)
    edges = {}                  # (i, j) -> (kind, dz)
    rejected = {k: 0 for k in EDGE_ORDER}
    conflicts = set()           # (i, j): one OSM level, a geometry test met, |dz| over CONFLICT_DZ (the map's data)

    def level(i):
        return members[i].get("level")

    def reject(kind, i, j, dz):
        rejected[kind] += 1
        if level(i) is not None and level(i) == level(j) and float(dz) > CONFLICT_DZ:
            conflicts.add((min(i, j), max(i, j)))

    def put(i, j, kind, dz):
        a, b = min(i, j), max(i, j)
        old = edges.get((a, b))
        if old is None or EDGE_ORDER.index(kind) < EDGE_ORDER.index(old[0]):
            edges[(a, b)] = (kind, round(float(dz), 3))

    def ok_pair(i, j):
        return members[i]["fid"] != members[j]["fid"] and members[i]["walk"] == members[j]["walk"]

    # JOIN and END_ON from the ends
    ends = [(i, e, m["xy"][0 if e == 0 else -1], float(m["z"][0 if e == 0 else -1]))
            for i, m in enumerate(members) for e in (0, 1) if len(m["xy"]) >= 2]
    E = np.array([q for _i, _e, q, _z in ends], np.float64).reshape(-1, 2)
    joined = set()
    for k, (i, e, p, z) in enumerate(ends):
        d = dnp.hypot(E[:, 0] - p[0], E[:, 1] - p[1])
        for k2 in np.flatnonzero(d <= JOIN_END_M).tolist():
            j, _e2, _q, z2 = ends[k2]
            if j == i:
                continue
            joined.add(k)
            if not ok_pair(i, j):
                continue
            if abs(z - z2) <= JOIN_DZ:
                put(i, j, "JOIN", abs(z - z2))
            else:
                reject("JOIN", i, j, abs(z - z2))
    lines = [(m["xy"], m["z"], m["half"], m["key"]) for m in members]
    idx = SegIndex(lines, pad=1.0)
    free_ends = [k for k in range(len(ends)) if k not in joined]
    if free_ends:
        P = np.array([ends[k][2] for k in free_ends], np.float64)
        pi, si, dist, _t, zb, sb = idx.query(P)
        keep = nearest_per_line(pi, si, dist, idx.line)
        for r in keep.tolist():
            k = free_ends[int(pi[r])]
            i, _e, p, z = ends[k]
            j = int(idx.line[si[r]])
            if j == i or not ok_pair(i, j) or dist[r] > members[j]["half"]:
                continue
            Lj = float(arc(members[j]["xy"])[-1])
            if sb[r] <= JOIN_END_M or sb[r] >= Lj - JOIN_END_M:
                continue            # (at its end: JOIN's)
            if abs(z - zb[r]) <= END_ON_DZ:
                put(i, j, "END_ON", abs(z - zb[r]))
            else:
                reject("END_ON", i, j, abs(z - zb[r]))
    # the footprint tests from samples
    pr = _pairs(members, GROUP_STEP, OUTLINE_GAP_M + 12.0)
    if pr is not None:
        hb = np.array([members[b]["half"] for b in pr["b"].tolist()])
        ang = angle_deg(pr["ua"], pr["ub"])
        dz = np.abs(pr["za"] - pr["zb"])
        lap = pr["gap"] < 0.0
        paths = []
        if len(outlines):
            from polygon import Path
            paths = [Path(np.asarray(o, np.float64)[:, :2]) for o in outlines if len(o) >= 3]
        lengths = [float(arc(m["xy"])[-1]) for m in members]
        acc = {}
        for k in range(len(pr["a"])):
            a, b = int(pr["a"][k]), int(pr["b"][k])
            acc.setdefault((a, b), []).append(k)
        for (a, b), ks in sorted(acc.items()):
            ks = np.asarray(ks)
            along = ks[lap[ks] & (ang[ks] <= ALONG_DEG)]
            if float(pr["piece"][along].sum()) >= ALONG_MIN_M:
                good = along[dz[along] <= ALONG_DZ]
                if float(pr["piece"][good].sum()) >= ALONG_MIN_M:
                    put(a, b, "OVERLAP_ALONG", dz[good].max())
                else:
                    reject("OVERLAP_ALONG", a, b, dz[along].min())
            cross = ks[(pr["dist"][ks] <= hb[ks]) & (ang[ks] > ALONG_DEG)]
            if len(cross):
                good = cross[dz[cross] <= CROSS_DZ]
                if len(good):
                    put(a, b, "OVERLAP_CROSS", dz[good].min())
                else:
                    reject("OVERLAP_CROSS", a, b, dz[cross].min())
            need = beside_min(lengths[a], lengths[b])
            par = ks[(pr["gap"][ks] >= -TWIN_TOUCH_M) & (ang[ks] <= BESIDE_DEG)]
            near = par[pr["gap"][par] <= BESIDE_GAP_M]
            if float(pr["piece"][near].sum()) >= need:
                good = near[dz[near] <= BESIDE_DZ]
                if float(pr["piece"][good].sum()) >= need:
                    put(a, b, "BESIDE", dz[good].max())
                else:
                    reject("BESIDE", a, b, dz[near].min())
            wide = par[(pr["gap"][par] > BESIDE_GAP_M) & (pr["gap"][par] <= OUTLINE_GAP_M)]
            if paths and float(pr["piece"][wide].sum()) >= need:
                # (one outline holding both: A's samples and B's nearest points in it over the length)
                held, passed = None, None       # (every outline tried: none's order decides)
                for path in paths:
                    both = wide[path.contains_points(pr["p"][wide]) & path.contains_points(pr["q"][wide])]
                    if float(pr["piece"][both].sum()) < need:
                        continue
                    held = both if held is None else held
                    good = both[dz[both] <= BESIDE_DZ]
                    if float(pr["piece"][good].sum()) >= need and (passed is None or dz[good].max() < passed):
                        passed = float(dz[good].max())
                if passed is not None:
                    put(a, b, "BESIDE_OUTLINE", passed)
                elif held is not None:
                    reject("BESIDE_OUTLINE", a, b, dz[held].min())
    # union-find in sorted-key order (the smaller key the root)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (a, b), _kd in sorted(edges.items()):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    def inside(i):
        if R is None:
            return True
        xy = members[i]["xy"]
        return bool((xy[:, 0] * xy[:, 0] + xy[:, 1] * xy[:, 1] <= R * R).any())
    out = []
    for root, mem in sorted(groups.items()):
        if len(mem) < 2 or not any(inside(i) for i in mem):
            continue
        es = sorted((kd[0], members[a]["key"][:10], members[b]["key"][:10], kd[1])
                    for (a, b), kd in edges.items() if find(a) == root)
        allp = np.concatenate([members[i]["xy"] for i in mem])
        c = (allp.min(0) + allp.max(0)) / 2
        out.append({"root": members[root]["key"][:10], "n": len(mem),
                    "members": [members[i]["key"][:10] for i in mem],
                    "classes": sorted({str(members[i]["cls"]) for i in mem}),
                    "levels": sorted({"?" if level(i) is None else str(level(i)) for i in mem}),
                    "edges": [list(e) for e in es], "at": ll(c)})
    by = {k: 0 for k in EDGE_ORDER}
    for (a, b), kd in edges.items():
        if inside(a) or inside(b):
            by[kd[0]] += 1
    single = sum(1 for root, mem in groups.items() if len(mem) == 1 and inside(mem[0]))
    cross_level = sum(1 for (a, b) in edges if (inside(a) or inside(b)) and level(a) is not None
                      and level(b) is not None and level(a) != level(b))
    conf = sorted([members[a]["key"][:10], members[b]["key"][:10]] for a, b in conflicts if inside(a) or inside(b))
    out.sort(key=lambda g: (-g["n"], g["root"]))
    # (road revamp v2 P1b: a group of one or two road decks with no END_ON / OVERLAP edge inside it is simple)
    # (simple_literal: the design's words, one or two members; simple: earthfast.deck_simple's rule, the JOIN edges a
    # path, as roadshape takes it: a short bridge carried onto the roads at its ends is three members end to end)
    simple_keys, n_all, n_simple, n_literal = [], 0, 0, 0
    for root, mem in sorted(groups.items()):
        if not any(inside(i) for i in mem):
            continue
        n_all += 1
        mine = [(a, b, kd) for (a, b), kd in edges.items() if find(a) == root]
        bad = any(kd[0] in ("END_ON", "OVERLAP_ALONG", "OVERLAP_CROSS") for a, b, kd in mine) or \
            any(members[i]["walk"] for i in mem)
        if bad:
            continue
        if len(mem) <= 2:
            n_literal += 1
        deg = {}
        for a, b, kd in mine:
            if kd[0] == "JOIN":
                deg[a] = deg.get(a, 0) + 1
                deg[b] = deg.get(b, 0) + 1
        if max(deg.values(), default=0) <= 2 and sum(deg.values()) // 2 < max(1, len(mem)):
            n_simple += 1
            simple_keys += [members[i]["key"] for i in mem]
    return {"n": len(out), "grouped": sum(g["n"] for g in out), "single": single, "edges": by,
            "height_rejected": rejected, "cross_level_edges": cross_level, "data_conflicts": conf, "list": out,
            "all_groups": n_all, "simple_groups": n_simple, "simple_literal": n_literal,
            "simple_keys": sorted(simple_keys)}


def _near(xy, R):
    xy = np.asarray(xy, np.float64)
    return bool((xy[:, 0] * xy[:, 0] + xy[:, 1] * xy[:, 1] <= R * R).any())


def score(inp, R, frame_ll=None):
    """Every metric from the probe's inputs (collect()'s dict, or a synthetic one)."""
    oh = overlap_hole(inp["tops"], ideal_tris([(l, r, z) for l, r, z, _t in inp["ribbons"]], inp.get("cuts")), R,
                      frame_ll, inp.get("pads", ()))
    res = {
        "overlap_m2": oh["overlap_m2"], "overlap_pad_m2": oh["overlap_pad_m2"], "hole_m2": oh["hole_m2"],
        "overlap_worst": oh["overlap_worst"], "hole_worst": oh["hole_worst"],
        "parapet_in_cw": parapet_in_carriageway(inp["parapets"], inp["carriageways"], R, frame_ll),
        "seam_step": seam_steps(inp["ribbons"], R, frame_ll),
        "top_step": {k: v for k, v in top_steps(inp["tops"], R, frame_ll).items() if k != "keys"},
        "twin_dz": twin_dz(inp["members"], R, frame_ll),
        "skew_err_deg": skew_errors(inp["ends"], inp["members"], inp["crossed"], R, frame_ll),
        "groups": predict_groups([dict(m) for m in inp["members"]], inp.get("outlines", ()), R, frame_ll),
    }
    res["simple"] = simple_metrics(inp, set(res["groups"]["simple_keys"]), R, frame_ll)
    tris = [int(t) for t in inp.get("deck_tris", ()) if t]
    res["deck_tris"] = {"tiles": len(tris), "total": int(sum(tris)),
                        "mean": round(float(np.mean(tris)), 1) if tris else 0.0, "max": max(tris) if tris else 0}
    inr = sum(1 for m in inp["members"]
              if _near(m["xy"], R))
    res["counts"] = {"members": inr, "members_all": len(inp["members"]), "chunks": len(inp["ribbons"]),
                     "top_tris": int(len(inp["tops"])), "top_m2": oh["top_m2"], "ideal_m2": oh["ideal_m2"],
                     "parapet_runs": len(inp["parapets"]), "pads": len(inp.get("pads", ())),
                     "outlines": len(inp.get("outlines", ()))}
    g = res["groups"]
    res["summary"] = {"overlap_m2": res["overlap_m2"], "hole_m2": res["hole_m2"],
                      "parapet_in_cw_m": res["parapet_in_cw"]["m"], "seam_step_n": res["seam_step"]["n_over"],
                      "seam_step_max": res["seam_step"]["max"],
                      "top_step_m": res["top_step"]["m"], "top_step_max": res["top_step"]["max"],
                      "twin_dz_max": res["twin_dz"]["max"],
                      "twin_pairs": res["twin_dz"]["pairs"], "skew_err_max": res["skew_err_deg"]["max"],
                      "skew_ends": res["skew_err_deg"]["ends"], "decks": inr, "groups": g["n"],
                      "grouped": g["grouped"], "all_groups": g["all_groups"], "simple_groups": g["simple_groups"],
                      "simple_literal": g["simple_literal"],
                      "s_chord_err_m": res["simple"]["chord_err_m"], "s_end_kink_deg": res["simple"]["end_kink_deg"],
                      "s_smooth_dev_m": res["simple"]["smooth_dev_m"],
                      "s_seam_step_max": res["simple"]["seam_step"]["max"],
                      "s_seam_step_n": res["simple"]["seam_step"]["n_over"],
                      "s_skew_err_max": res["simple"]["skew_err_deg"]["max"],
                      "s_skew_ends": res["simple"]["skew_err_deg"]["ends"],
                      "s_parapet_in_cw_m": res["simple"]["parapet_in_cw"]["m"],
                      "deck_tris": res["deck_tris"]["total"], "deck_tris_tiles": res["deck_tris"]["tiles"]}
    return res


def _dir_end(xy, at, reach=DIR_M):
    """The unit direction out of polyline xy (k, 2) at its start (at 0) or end (1), read over reach m of it."""
    xy = np.asarray(xy, np.float64)
    if at == 1:
        xy = xy[::-1]
    s = arc(xy)
    r = min(reach, float(s[-1]))
    q = np.array([_dnp().interp(r, s, xy[:, 0]), _dnp().interp(r, s, xy[:, 1])])
    d = xy[0] - q
    ln = float(_dnp().hypot(d[0], d[1]))
    return d / ln if ln > 1e-9 else None


def _smooth_ref(m, members, roads):
    """The road's smooth line over member m (earthfast.deck_shape on it with the ground roads and the other decks
    round it, rows to 1 mm and 0.25 m), or None without the kernel."""
    try:
        import fast
        import roadshape
        if not hasattr(fast._ef, "deck_shape"):
            return None
    except Exception:
        return None
    xy = np.asarray(m["xy"], np.float64)
    lo, hi = xy.min(0) - 60.0, xy.max(0) + 60.0
    lines, kinds, owns = [], [], []
    for fid, rxy in roads:
        if (rxy.max(0) < lo).any() or (rxy.min(0) > hi).any():
            continue
        lines.append(rxy)
        kinds.append(0)
        owns.append(1 if fid == m["fid"] else 0)
    for o in members:
        if o is m or (o["xy"].max(0) < lo).any() or (o["xy"].min(0) > hi).any():
            continue
        lines.append(np.asarray(o["xy"], np.float64))
        kinds.append(2)
        owns.append(0)
    prm = roadshape.PARAMS.copy()
    prm[0], prm[1] = 0.001, 0.25
    off = np.r_[0, np.cumsum([len(a) for a in lines])].astype(np.int64)
    pts = np.ascontiguousarray(np.concatenate(lines) if lines else np.zeros((0, 2)), np.float64)
    got = fast._ef.deck_shape(np.ascontiguousarray(xy), np.zeros(len(xy)), pts, off, np.asarray(kinds, np.int64),
                              np.asarray(owns, np.int64), prm)
    return np.asarray(got[0], np.float64).reshape(-1, 2)


def _near_line(pts, line, d):
    """(m,) bool: points pts (m, 2) within d of the polyline line (k, 2)."""
    if not len(pts) or len(line) < 2:
        return np.zeros(len(pts), bool)
    a, dd = line[:-1], np.diff(line, axis=0)
    l2 = np.maximum((dd * dd).sum(1), 1e-18)
    t = np.clip(((pts[:, None, :] - a[None]) * dd[None]).sum(2) / l2[None], 0.0, 1.0)
    g = pts[:, None, :] - (a[None] + t[..., None] * dd[None])
    return (g * g).sum(2).min(1) <= d * d


def _off_line(pts, ref):
    """The largest distance from points pts (m, 2) to the polyline ref (k, 2)."""
    if not len(pts):
        return 0.0
    a, d = ref[:-1], np.diff(ref, axis=0)
    l2 = np.maximum((d * d).sum(1), 1e-18)
    t = np.clip(((pts[:, None, :] - a[None]) * d[None]).sum(2) / l2[None], 0.0, 1.0)
    f = a[None] + t[..., None] * d[None]
    g = pts[:, None, :] - f
    return float(np.sqrt((g * g).sum(2).min(1)).max())


def _cr_dense(xy, n=24):
    """The centripetal Catmull-Rom curve through the rows xy (k, 2), n points per piece (the ends' tangents from
    reflected rows): the smooth line the rows stand for (chord_err_m)."""
    xy = np.asarray(xy, np.float64)
    d0 = np.diff(xy, axis=0)
    xy = xy[np.r_[True, np.sqrt((d0 * d0).sum(1)) > 1e-6]]
    if len(xy) < 3:
        return xy
    P = np.vstack([2 * xy[0] - xy[1], xy, 2 * xy[-1] - xy[-2]])
    out = [xy[:1]]
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        t0 = 0.0
        t1 = t0 + float(np.sqrt(np.sqrt(((p1 - p0) * (p1 - p0)).sum())))
        t2 = t1 + float(np.sqrt(np.sqrt(((p2 - p1) * (p2 - p1)).sum())))
        t3 = t2 + float(np.sqrt(np.sqrt(((p3 - p2) * (p3 - p2)).sum())))
        if min(t1 - t0, t2 - t1, t3 - t2) <= 1e-12:
            out.append(p2[None])
            continue
        t = np.linspace(t1, t2, n + 1)[1:, None]
        a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
        a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
        a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
        b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
        b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
        out.append((t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2)
    return np.concatenate(out)


def simple_metrics(inp, simple_keys, R, frame_ll=None):
    """The P1b targets on the simple groups' members (simple_keys): chord_err_m, end_kink_deg, seam_step,
    skew_err_deg, parapet_in_cw (module doc)."""
    dnp = _dnp()
    members = inp["members"]
    keys_of = {}
    for m in members:
        keys_of.setdefault(m["fid"], []).append(m["key"])
    sfids = {f for f, ks in keys_of.items() if all(k in simple_keys for k in ks)}
    rib = [r for r in inp["ribbons"] if r[3] in sfids]
    # (the roads on the ground round it, every piece: collect()'s crossed lines, railways left out)
    roads = [(str(fid), np.asarray(gl, np.float64)) for gl, tag, fid in inp["crossed"] if not str(tag).startswith("rail:")]
    chord = smooth = None
    kinks = []
    for m in members:
        if m["key"] not in simple_keys or not _near(m["xy"], R):
            continue
        xy = np.asarray(m["xy"], np.float64)
        mine = [r for r in rib if r[3] == m["fid"]]
        for ref, name in ((_cr_dense(xy), "chord"), (_smooth_ref(m, members, roads), "smooth")):
            if ref is None or len(ref) < 2:
                continue
            worst = 0.0
            for left, right, _z, _f in mine:
                mid = (np.asarray(left) + np.asarray(right)) / 2
                if len(mid) < 2:
                    continue
                pts, _zz, _piece, _u = resample(mid, np.zeros(len(mid)), CHORD_SAMPLE_M)
                if not len(pts):
                    continue
                far = dnp.hypot(*(pts[:, None, :] - xy[None, [0, -1], :]).transpose(2, 0, 1)).min(1) > 0.5
                far &= _near_line(pts, xy, OWN_M)        # (its own ribbons: a way's other parts are other members)
                worst = max(worst, _off_line(pts[far], ref))
            if name == "chord":
                chord = worst if chord is None else max(chord, worst)
            else:
                smooth = worst if smooth is None else max(smooth, worst)
        for at in (0, 1):
            p = xy[0] if at == 0 else xy[-1]
            dd = _dir_end(xy, at)
            best = None
            for fid, rxy in roads:
                for e in (0, 1):
                    q = rxy[0] if e == 0 else rxy[-1]
                    if float(dnp.hypot(*(q - p))) > JOIN_ROAD_M:
                        continue
                    rd = _dir_end(rxy, e)
                    if dd is None or rd is None:
                        continue
                    a = float(angle_deg(-dd, rd)[0])      # (the road leaving the deck's end: against the deck's way in)
                    if a <= 45.0 and (best is None or (fid == m["fid"], -a) > best[0]):
                        best = ((fid == m["fid"], -a), a)
            if best is not None:
                kinks.append(best[1])
    ends = [(mi, at, d) for mi, at, d in inp["ends"] if members[mi]["key"] in simple_keys]
    runs = [r for r, f in zip(inp["parapets"], inp.get("parapet_fids") or ()) if f in sfids]
    return {"members": sum(1 for m in members if m["key"] in simple_keys and _near(m["xy"], R)),
            "chord_err_m": None if chord is None else round(chord, 4),
            "smooth_dev_m": None if smooth is None else round(smooth, 4),
            "end_kink_deg": round(max(kinks), 2) if kinks else 0.0,
            "end_kinks": len(kinks), "seam_step": seam_steps(rib, R, frame_ll),
            "skew_err_deg": skew_errors(ends, members, inp["crossed"], R, frame_ll),
            "parapet_in_cw": parapet_in_carriageway(runs, inp["carriageways"], R, frame_ll)}


def text_summary(res):
    s, c = res["summary"], res["counts"]
    lines = [f"decks {c['members']} members ({c['chunks']} drawn chunks, {c['top_m2']} m2 drawn top, ideal "
             f"{c['ideal_m2']} m2, {c['pads']} pads)",
             f"overlap_m2 {s['overlap_m2']} (in pads {res['overlap_pad_m2']}) | hole_m2 {s['hole_m2']}",
             f"parapet_in_cw_m {s['parapet_in_cw_m']} (deck {res['parapet_in_cw']['deck_m']}, ground "
             f"{res['parapet_in_cw']['ground_m']})",
             f"seam_step: {s['seam_step_n']} of {res['seam_step']['joints']} joints over {SEAM_M} m, max "
             f"{s['seam_step_max']} (side {res['seam_step']['side_max']}, z {res['seam_step']['z_max']})",
             f"twin_dz: {s['twin_pairs']} pairs, max {s['twin_dz_max']} m, p95 {res['twin_dz']['p95']}, "
             f"{res['twin_dz']['n_over']} over {TWIN_OVER}",
             f"skew_err_deg: {s['skew_ends']} ends, max {s['skew_err_max']}, mean {res['skew_err_deg']['mean']}, "
             f"{res['skew_err_deg']['n_over']} over {SKEW_OVER}",
             f"groups (predicted): {s['groups']} of {s['grouped']} members, {res['groups']['single']} single; edges "
             + ", ".join(f"{k} {v}" for k, v in res["groups"]["edges"].items() if v)
             + "; over the height limit " + ", ".join(f"{k} {v}" for k, v in res["groups"]["height_rejected"].items()
                                                      if v)]
    sm = res.get("simple")
    if sm is not None:
        lines.append(f"simple groups {s.get('simple_groups')} of {s.get('all_groups')} ({sm['members']} members): "
                     f"chord_err_m {sm['chord_err_m']}, smooth_dev_m {sm['smooth_dev_m']}, end_kink_deg {sm['end_kink_deg']} ({sm['end_kinks']} ends), "
                     f"seam_step max {sm['seam_step']['max']} ({sm['seam_step']['n_over']} over), skew_err max "
                     f"{sm['skew_err_deg']['max']} ({sm['skew_err_deg']['ends']} ends), parapet_in_cw_m "
                     f"{sm['parapet_in_cw']['m']}")
        lines.append(f"deck tris: {res['deck_tris']['total']} in {res['deck_tris']['tiles']} tiles (mean "
                     f"{res['deck_tris']['mean']}, max {res['deck_tris']['max']})")
    lines.append(f"  edges across OSM levels {res['groups']['cross_level_edges']}, data conflicts (one level, "
                 f"> {CONFLICT_DZ} m) {len(res['groups']['data_conflicts'])}")
    for g in res["groups"]["list"][:8]:
        lines.append(f"  group {g['root']} x{g['n']} at {g['at'][0]},{g['at'][1]} {'/'.join(g['classes'])} levels "
                     f"{'/'.join(g['levels'])}: " + ", ".join(sorted({e[0] for e in g["edges"]})))
    if res.get("errors"):
        lines.append(f"errors: {len(res['errors'])} (first: {res['errors'][0]})")
    return "\n".join(lines)


# ---------------------------------------------------------------- collection (the server's tiles)
def collect(lat, lon, R, cache, offline=False):
    """Build the road tiles within R of (lat, lon) as the road tile pool does and gather the probe's inputs."""
    os.environ.setdefault("BNE_REVISIT_SKIP", "roads")      # (build, never the revisit cache)
    import tempfile
    import shutil
    import blockworker as bw
    import geodesy
    import roads as roadsmod
    import roadmesh as rm
    try:
        import railways
    except ImportError:
        railways = None
    bw.init(cache, bool(offline), True, True, True, False, None, logging.ERROR, False, True)
    s = bw._s
    frame = geodesy.EnuFrame(lat, lon)
    errors = []
    cur = {}
    tiles = []
    orig = (roadsmod.build_tile, roadsmod.deck_pads, rm.deck)

    def g_build(frame_, tx, ty, cells, elev, *a, **kw):
        cur["ground"] = kw.get("ground")
        return orig[0](frame_, tx, ty, cells, elev, *a, **kw)

    def g_pads(decks, ground_roads, box, ground_fn, own=True, **kw):
        cur["parts"] = list(decks)
        cur["roads"] = list(ground_roads)
        return orig[1](decks, ground_roads, box, ground_fn, own, **kw)     # (skip: v2 P2b's fork nodes)

    def g_deck(chunks, origin, *a, **kw):
        r = orig[2](chunks, origin, *a, **kw)
        cur["deck"] = (list(chunks), np.asarray(origin, np.float64), list(kw.get("parapets") or []),
                       list(kw.get("pads") or []), r)
        return r
    roadsmod.build_tile, roadsmod.deck_pads, rm.deck = g_build, g_pads, g_deck
    out = tempfile.mkdtemp(prefix="structprobe")
    try:
        for tx, ty in roadsmod.tiles_near(0.0, 0.0, R):
            cur.clear()
            try:
                bw.roads_tile("p", lat, lon, 15, tx, ty, out, "/x", True, True, buildings=False)
            except Exception as ex:
                errors.append(f"tile {tx},{ty}: {type(ex).__name__}: {ex}"[:300])
                continue
            tiles.append(((tx, ty), roadsmod.tile_bbox(tx, ty), dict(cur)))
    finally:
        roadsmod.build_tile, roadsmod.deck_pads, rm.deck = orig
        shutil.rmtree(out, True)
    tops, ribbons, cuts, parapets, pads = [], [], [], [], []
    parapet_fids, deck_tris = [], []
    parts, ground_roads = {}, {}
    grounds = []
    for (tx, ty), box, got in tiles:
        if got.get("ground") is not None:
            grounds.append((box, got["ground"]))
        for fid, spec, xy, z in got.get("parts", ()):
            xy = np.asarray(xy, np.float64)[:, :2]
            if len(xy) < 2:
                continue
            k = (str(fid), round(float(xy[0, 0]), 1), round(float(xy[0, 1]), 1))
            parts.setdefault(k, (str(fid), spec, xy, np.asarray(z, np.float64)))
        for fid, spec, pts, *_ in got.get("roads", ()):
            pts = np.asarray(pts, np.float64)
            if spec is None or len(pts) < 2:
                continue
            k = (str(fid), round(float(pts[0, 0]), 1), round(float(pts[0, 1]), 1), len(pts))
            ground_roads.setdefault(k, (str(fid), spec, pts[:, :2]))
        if "deck" not in got:
            continue
        chunks, origin, pars, tpads, mesh = got["deck"]
        deck_tris.append(0 if mesh is None else len(mesh[1]))
        if mesh is not None:
            pp, tt, _uu, mats, M = mesh
            if rm.DECK_TOP in mats:
                sel = tt[np.asarray(M) == mats.index(rm.DECK_TOP)]
                tri = (np.asarray(pp, np.float64) + origin)[sel]
                e1, e2 = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
                nz = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
                nx = e1[:, 1] * e2[:, 2] - e1[:, 2] * e2[:, 1]
                ny = e1[:, 2] * e2[:, 0] - e1[:, 0] * e2[:, 2]
                flat = np.abs(nz) > 0.3 * np.sqrt(nx * nx + ny * ny + nz * nz)
                tops.append(tri[flat])
        dch = [c for c in chunks if c.get("deck") and c.get("fixed") and not c.get("ctx") and not c.get("tunnel")]
        all_dch = [c for c in chunks if c.get("deck")]      # (deck()'s own list: a parapet's "deck" indexes its ribbons)
        rb_fid = [str(c.get("fid", "")) for r, c in rm.ribbon_pairs(all_dch) if r[5] is not None]
        for r, c in rm.ribbon_pairs(dch):
            if r[5] is None:
                continue
            left, right, z = np.asarray(r[0], np.float64), np.asarray(r[1], np.float64), np.asarray(r[5], np.float64)
            mid = (left + right) / 2
            ribbons.append((left, right, z, str(c.get("fid", ""))))
            cuts.append(tuple(min(abs(mid[e, 0] - box[0]), abs(mid[e, 0] - box[2]), abs(mid[e, 1] - box[1]),
                                  abs(mid[e, 1] - box[3])) < CUT_EPS for e in (0, -1)))
        for p in pars:
            parapets.append((np.asarray(p["edge"], np.float64)[:, :2], np.asarray(p["z"], np.float64)))
            n_ = p.get("deck")
            parapet_fids.append(rb_fid[n_] if isinstance(n_, int) and 0 <= n_ < len(rb_fid) else "")
        for pad in tpads:
            pads.append(np.asarray(pad[0], np.float64))
            for pts, outv in (pad[2] if len(pad) > 2 else ()):
                pts, outv = np.asarray(pts, np.float64), np.asarray(outv, np.float64)
                if len(pts) >= 2:
                    parapets.append((pts[:, :2] + outv * (rm.JERSEY_HALF_W + 0.02), pts[:, 2] - rm.PAD_DROP))
                    parapet_fids.append("")
    tops = np.concatenate(tops) if tops else np.zeros((0, 3, 3))

    def ground_z(xy):
        xy = np.asarray(xy, np.float64)
        z = np.full(len(xy), np.nan)
        for box, fn in grounds:
            m = (xy[:, 0] >= box[0]) & (xy[:, 0] <= box[2]) & (xy[:, 1] >= box[1]) & (xy[:, 1] <= box[3]) & np.isnan(z)
            if m.any():
                z[m] = np.asarray(fn(xy[m, 0], xy[m, 1]), np.float64).ravel()
        return z
    # members: the bridge parts (their keys: fid and part number in first-point order)
    members = []
    byfid = {}
    for k in sorted(parts):
        byfid.setdefault(k[0], []).append(parts[k])
    for fid in sorted(byfid):
        for n, (fid_, spec, xy, z) in enumerate(byfid[fid]):
            if not np.isfinite(z).all():
                continue
            cls = getattr(spec, "cls", None)
            members.append({"key": f"{fid}:{n}", "fid": fid, "cls": cls, "xy": xy, "z": z,
                            "half": float(spec.width) / 2, "walk": cls in WALK_CLASSES})
    carriageways = [(m["xy"], m["z"], m["half"], ("deck", m["key"])) for m in members]
    for k in sorted(ground_roads):
        fid, spec, xy = ground_roads[k]
        z = ground_z(xy)
        if np.isfinite(z).all():
            carriageways.append((xy, z, float(spec.width) / 2, ("ground", fid)))
    # free member ends and their drawn end lines (the deck chunk ribbon ending there)
    rib_ends = []
    for left, right, _z, fid in ribbons:
        for e in (0, -1):
            rib_ends.append(((left[e] + right[e]) / 2, left[e] - right[e], fid))
    E = np.array([q for q, _d, _f in rib_ends], np.float64).reshape(-1, 2)
    member_ends = np.array([m["xy"][e] for m in members for e in (0, -1)], np.float64).reshape(-1, 2)
    ends = []
    dnp = _dnp()
    for mi, m in enumerate(members):
        for at, e in ((0, 0), (1, -1)):
            p = m["xy"][e]
            d = dnp.hypot(member_ends[:, 0] - p[0], member_ends[:, 1] - p[1])
            d[2 * mi + at] = np.inf
            if (d <= JOIN_M).any():
                continue            # (a joint, not an abutment)
            dd = dnp.hypot(E[:, 0] - p[0], E[:, 1] - p[1]) if len(E) else np.zeros(0)
            cand = [k for k in np.argsort(dd, kind="stable").tolist() if dd[k] <= 3.0 + m["half"]
                    and rib_ends[k][2] == m["fid"]]
            if cand:
                v = rib_ends[cand[0]][1]
            else:
                q = m["xy"][1] if at == 0 else m["xy"][-2]
                t = p - q
                v = np.array([-t[1], t[0]])
            ln = float(dnp.hypot(v[0], v[1]))
            if ln > 1e-9:
                ends.append((mi, at, v / ln))
    # the lines on the ground (roads, drawn railways) a deck end can be over (probe.py's ground lines)
    crossed, seen = [], set()
    feats = {}
    for tx, ty in roadsmod.tiles_near(0.0, 0.0, R + 150.0):
        for k in roadsmod.cells_of_tile(frame, tx, ty):
            if str(k) in feats:
                continue
            try:
                feats[str(k)] = (s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads"),
                                 s.md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure"))
            except Exception as ex:
                errors.append(f"cell {k}: {type(ex).__name__}: {ex}"[:300])
    where = {}
    for ck in sorted(feats):
        f = feats[ck][0]
        if f is not None:
            for i in range(len(f)):
                where.setdefault(str(f.ids[i]), (f, i))
    for m in members:
        m["level"] = _member_level(where.get(m["fid"]), m["xy"], frame)
    outlines = []
    for ck in sorted(feats):
        f, inf = feats[ck]
        if f is not None and len(f):
            specs = roadsmod.road_specs(f)
            for i in range(len(f)):
                fid = str(f.ids[i])
                if fid in seen:
                    continue
                seen.add(fid)
                sub = str(f.attrs["subtype"][i])
                if sub == "road":
                    if specs[i] is None:
                        continue
                    tag = f"{specs[i].cls}:{fid[:8]}"
                elif sub == "rail":
                    if railways is None or railways.drawn(f, i) is None:
                        continue
                    tag = f"rail:{fid[:8]}"
                else:
                    continue
                for ring in roadsmod.ground_parts(f, i):
                    ring = np.asarray(ring, np.float64)
                    e_, n_, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                    gl = np.c_[np.asarray(e_, np.float64), np.asarray(n_, np.float64)]
                    if len(gl) >= 2 and float((gl[:, 0] * gl[:, 0] + gl[:, 1] * gl[:, 1]).min()) < (R + 100) * (R + 100):
                        crossed.append((gl, tag, fid))
        if inf is not None and len(inf):
            sub = inf.attrs.get("subtype")
            for i in range(len(inf.kind)):
                if inf.kind[i] != 3 or sub is None or str(sub[i]) != "bridge":
                    continue
                for part in inf.parts(i):
                    ring = np.asarray(part[0], np.float64)
                    if len(ring) < 4:
                        continue
                    e_, n_, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                    outlines.append(np.c_[np.asarray(e_, np.float64), np.asarray(n_, np.float64)])
    crossed.sort(key=lambda q: (q[1], round(float(q[0][0, 0]), 2), round(float(q[0][0, 1]), 2)))
    outlines.sort(key=lambda o: (round(float(o[:, 0].min()), 2), round(float(o[:, 1].min()), 2), len(o)))
    return {"tops": tops, "ribbons": ribbons, "cuts": cuts, "parapets": parapets, "pads": pads,
            "parapet_fids": parapet_fids, "deck_tris": deck_tris,
            "members": members, "carriageways": carriageways, "ends": ends, "crossed": crossed,
            "outlines": outlines, "errors": errors, "tiles": len(tiles), "frame": frame}


def _member_level(fi, xy, frame):
    """The OSM level of a member: its feature's Overture level rule at the member's middle (its fraction of the
    feature's length), else the feature's osm_layer, else None."""
    if fi is None:
        return None
    f, i = fi
    rule = getattr(f, "rules", {}).get("level")
    if rule is not None and len(rule.rows(i)):
        line = np.concatenate([np.asarray(r, np.float64) for part in f.parts(i) for r in part])
        e_, n_, _ = frame.geodetic_to_enu(line[:, 1], line[:, 0], 0.0)
        g = np.c_[np.asarray(e_, np.float64), np.asarray(n_, np.float64)]
        s = arc(g)
        mid = np.asarray(xy, np.float64)[len(xy) // 2, :2]
        idx = SegIndex([(g, np.zeros(len(g)), 0.0, 0)], pad=1e6, cell=1e7)
        _pi, si, dist, _t, _z, sa = idx.query(mid[None, :])
        if len(dist) and s[-1] > 0:
            frac = float(sa[int(np.argmin(dist))]) / float(s[-1])
            for r in rule.rows(i).tolist():
                if float(rule.start[r]) <= frac <= float(rule.end[r]):
                    try:
                        return int(float(rule.value[r]))
                    except (TypeError, ValueError):
                        break
    osm = f.attrs.get("osm_layer")
    if osm is not None:
        try:
            v = float(osm[i])
        except (TypeError, ValueError):
            v = float("nan")
        if np.isfinite(v):
            return int(v)
    return None


def _refuse_player_cache(cache):
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    cache = os.path.realpath(os.path.expanduser(cache))
    if cache == real or cache.startswith(real + os.sep):
        sys.exit("refused: the player's cache")
    os.makedirs(cache, exist_ok=True)
    for root, dirs, files in os.walk(cache):
        for n in dirs + files:
            p = os.path.join(root, n)
            if os.path.islink(p) and os.path.realpath(p).startswith(real):
                sys.exit(f"refused: {p} links into the player's cache")
    return cache


# ---------------------------------------------------------------- RS S2b: chain discovery as a fixpoint
def fixpoint(lat, lon, R, cache, offline=False, seeds=3, solve=2):
    """BNE_DECK_STRUCT=shadow's chain discovery (roads.BridgeProfiles._closure) at a real place, from every bridge
    part within R of (lat, lon): (1) discovery alone, each part's closure from `seeds` random orders of its first
    reads (its ring's cells shuffled): one answer per part and the same answer from every part of a chain (the
    chain, capped or not); (2) `solve` full runs of a fresh ChainStore asking every part in a random order (the
    first in key order), each ask's cell list shuffled: the same chains, owners (the region that published each
    part), window decisions and published parts (z, piers, trusses) byte for byte. {"parts", "chains", "capped",
    "problems": [...]} (empty problems: the property holds)."""
    import random
    import hashlib
    import blockworker as bw
    import mapcells
    import roads as roadsmod
    import structures
    if not structures.SHADOW:
        raise SystemExit("--fixpoint needs BNE_DECK_STRUCT=shadow")
    os.environ.setdefault("BNE_REVISIT_SKIP", "roads")
    bw.init(cache, bool(offline), True, True, True, False, None, logging.ERROR, False, True)
    s = bw._s
    cell_fn = lambda key: s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")     # noqa: E731
    elev = lambda la, lo: np.asarray(s.dem.sample(np.asarray(la), np.asarray(lo), 15), np.float64)  # noqa: E731
    dlat, dlon = R / 111320.0, R / (111320.0 * np.cos(np.radians(lat)))      # det-ok: the check's search box only
    ga, go = np.meshgrid(np.linspace(lat - dlat, lat + dlat, 9), np.linspace(lon - dlon, lon + dlon, 9))
    keys = sorted({str(c): c for c in mapcells.cells_of(ga.ravel(), go.ravel())}.items())
    todo = {}
    for _n, ck in keys:
        feats = cell_fn(ck)
        if feats is None or not len(feats):
            continue
        bs = roadsmod.BridgeProfiles.specs_of(feats)
        for i in range(len(feats)):
            if bs[i] is None:
                continue
            for j, ring in roadsmod._keyed(roadsmod.BridgeProfiles.keys_of, roadsmod.BridgeProfiles.parts_of,
                                           feats, i):
                k = (str(feats.ids[i]), j)
                ring = np.asarray(ring, np.float64)
                near = (np.abs(ring[:, 1] - lat) <= dlat) & (np.abs(ring[:, 0] - lon) <= dlon)
                if k not in todo and near.any():
                    todo[k] = (feats, i, ring)
    order = sorted(todo)
    problems = []

    def store_of(cs, k):
        ring = todo[k][2]
        return cs.store_at(float(ring[0, 1]), float(ring[0, 0]))

    # (1) discovery alone (one store set: the cells' entries made once, kept whole for this check)
    answers = {}
    entries = {}
    orig_entries = roadsmod.BridgeProfiles._cell_entries

    def cell_entries(self, key):
        hit = entries.get((id(self), str(key)))
        if hit is None:
            hit = entries[(id(self), str(key))] = orig_entries(self, key)
        return hit
    roadsmod.BridgeProfiles._cell_entries = cell_entries
    cs = roadsmod.ChainStore(roadsmod.BridgeProfiles, cell_fn, elev, bw._bridge_setup)
    cs.KEPT = 1 << 20
    for k in order:
        got = set()
        for seed in range(seeds):
            rnd = random.Random(f"{seed}:{k}")
            st = store_of(cs, k)
            ring = todo[k][2]
            pts = [(float(p[1]), float(p[0])) for p in ring]
            rnd.shuffle(pts)
            pool, ground, loaded = {}, {}, set()
            for la_, lo_ in pts:
                c = mapcells.cell_of(la_, lo_)
                if str(c) not in loaded:
                    loaded.add(str(c))
                    st._load(c, pool, ground)
            if k not in pool:
                got.add(("absent",))
                continue
            try:
                comp = st._resolve(k, pool, ground, loaded)
                got.add(("chain", tuple(sorted(str(x) for x in comp)), tuple(sorted(loaded))))
            except roadsmod._Capped:
                got.add(("capped",))
        if len(got) != 1:
            problems.append(f"discovery: {k}: {len(got)} answers")
        answers[k] = sorted(got)[0]
    roadsmod.BridgeProfiles._cell_entries = orig_entries
    entries.clear()
    for k, a in answers.items():
        if a[0] != "chain":
            continue
        for x in a[1]:
            kx = next((t for t in todo if str(t) == x), None)
            # (a key its own ring's cells do not hold under that key is never asked: parts_todo's _held_in)
            if kx is not None and answers.get(kx, a)[0] != "absent" and answers.get(kx, a)[:2] != a[:2]:
                problems.append(f"discovery: {k} and {x}: different chains: {a[:2]} / {answers.get(kx)[:2]}; "
                                f"stores {store_of(cs, k).region} / {store_of(cs, kx).region}")
    # (2) full solves
    runs = []
    for seed in range(solve):
        rnd = random.Random(seed)
        ask = list(order)
        if seed:
            rnd.shuffle(ask)
        windows = set()
        orig = roadsmod.BridgeProfiles._chain_of
        orig_res = roadsmod.BridgeProfiles._resolve

        def chain_of(self, key, ends, adopt=True, rnd=rnd):
            ends = list(ends)
            rnd.shuffle(ends)
            return orig(self, key, ends, adopt)

        def resolve(self, start, pool, ground, loaded, grow=True, windows=windows):
            if not grow:
                windows.add(str(start))
            return orig_res(self, start, pool, ground, loaded, grow)
        roadsmod.BridgeProfiles._chain_of, roadsmod.BridgeProfiles._resolve = chain_of, resolve
        try:
            cs = roadsmod.ChainStore(roadsmod.BridgeProfiles, cell_fn, elev, bw._bridge_setup)
            for k in ask:
                feats, i, _ring = todo[k]
                st = store_of(cs, k)
                st.parts_todo([(k, feats, i)])
        finally:
            roadsmod.BridgeProfiles._chain_of, roadsmod.BridgeProfiles._resolve = orig, orig_res
        pub = {}
        for region, st in sorted(cs.stores.items()):
            with st.lock:
                for k, v in st.done.items():
                    h = hashlib.sha256(np.asarray(v[1], np.float64).tobytes() + np.asarray(v[2], np.float64).tobytes()
                                       + repr(st.piers.get(k)).encode()).hexdigest()[:16]
                    pub[(region, str(k))] = h
                for ck, beams in st.trusses.items():
                    pub[(region, "truss", str(ck))] = hashlib.sha256(repr(beams).encode()).hexdigest()[:16]
        runs.append((pub, sorted(windows)))
    for n, (pub, win) in enumerate(runs[1:], 1):
        if pub != runs[0][0]:
            diff = sorted(set(pub.items()) ^ set(runs[0][0].items()))[:5]
            problems.append(f"published: run {n} differs from run 0 ({len(diff)}+): {diff}")
        if set(win) != set(runs[0][1]):
            # (which parts a window solve was STARTED from depends on who asked; what it published is in `pub`)
            pass
    chains = {a[1] for a in answers.values() if a[0] == "chain"}
    return {"parts": len(order), "chains": len(chains), "capped": sum(1 for a in answers.values() if a[0] == "capped"),
            "published": len(runs[0][0]) if runs else 0, "problems": problems}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("r", type=float, nargs="?", default=300.0)
    ap.add_argument("--cache", default="~/bnge-tests/structures/cache",
                    help="a scratch cache (never the player's ~/.cache/beamng-earth)")
    ap.add_argument("--json", help="write the result here (deterministic)")
    ap.add_argument("--timing", help="wall clock / CPU / memory here")
    ap.add_argument("--offline", action="store_true", help="no network: the cache's data only (a seeded cache)")
    ap.add_argument("--server", help="score this server/ checkout instead of the one beside this file")
    ap.add_argument("--fixpoint", type=int, metavar="SEEDS",
                    help="RS S2b: check chain discovery's fixpoint property here (BNE_DECK_STRUCT=shadow) with SEEDS "
                         "random orders per part and two full solves; JSON to --json; exit 1 when it fails")
    a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    if a.server:
        srv = os.path.realpath(a.server)
        sys.path[:] = [srv] + [p for p in sys.path if os.path.realpath(p or ".") != here]
    elif here not in sys.path:
        sys.path.insert(0, here)
    cache = _refuse_player_cache(a.cache)
    t0 = time.time()
    if a.fixpoint:
        res = fixpoint(a.lat, a.lon, a.r, cache, a.offline, seeds=a.fixpoint)
        print(json.dumps({k: v for k, v in res.items() if k != "problems"}), len(res["problems"]), "problems")
        for p in res["problems"][:20]:
            print("  ", p)
        if a.json:
            with open(a.json, "w") as f:
                json.dump(res, f, sort_keys=True, default=str)
        sys.stdout.flush()
        os._exit(1 if res["problems"] else 0)
    try:
        # (BNE_DECK_STRUCT=shadow: the published chains' JSONs also when their solve read something short, marked:
        # an offline seed without the sea's bathymetry makes every coastal chain short, structures.ACCEPT_SHORT)
        import structures
        structures.ACCEPT_SHORT = True
    except ImportError:             # (a server tree from before RS)
        pass
    inp = collect(a.lat, a.lon, a.r, cache, a.offline)
    frame = inp["frame"]

    def frame_ll(p):
        la, lo, _ = frame.enu_to_geodetic(np.array([float(p[0])]), np.array([float(p[1])]), 0.0)
        return [round(float(la[0]), 6), round(float(lo[0]), 6)]
    res = score(inp, a.r, frame_ll)
    res["place"] = [a.lat, a.lon, a.r]
    res["errors"] = sorted(inp["errors"])
    res["counts"]["tiles"] = inp["tiles"]
    print(text_summary(res))
    if a.json:
        with open(a.json + ".tmp", "w") as f:
            json.dump(res, f, sort_keys=True, separators=(",", ":"), allow_nan=False)
            f.write("\n")
        os.replace(a.json + ".tmp", a.json)
    if a.timing:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        with open(a.timing, "w") as f:
            json.dump({"wall_s": round(time.time() - t0, 1), "cpu_s": round(ru.ru_utime + ru.ru_stime, 1),
                       "max_rss_mb": round(ru.ru_maxrss / 1024.0)}, f, sort_keys=True)
            f.write("\n")
    st = sys.modules.get("structures")
    if st is not None:      # (BNE_DECK_STRUCT=shadow: the chain JSONs its background writer still holds)
        drained = st.flush(900.0)      # (S2b: the writer solves each chain again, without the pairwise mechanisms)
        if st.SHADOW:
            print("structures:", " ".join(f"{k} {v}" for k, v in sorted(st.STATS.items())))
        if not drained or st.STATS.get("dropped") or st.STATS.get("failed"):
            # (the chain JSONs are incomplete: a failed run, never one scored as whole; Codex review)
            print("structures: the writer did not finish every chain: incomplete", flush=True)
            sys.stdout.flush()
            os._exit(3)
    sys.stdout.flush()
    sys.stderr.flush()
    # (the server's fetch / warm threads can keep the interpreter alive after the work: Tsing Ma's run sat 7 min at
    # its exit with 110 threads, 2026-11-18)
    os._exit(0)


if __name__ == "__main__":
    main()
