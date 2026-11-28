"""The airside's keep-out for overhead lines: no utility pole, tower or wire span on or across an airport's apron, taxiway or
runway pavement (aeroways.pads), nor within MARGIN_M of it (Brisbane, 2026-11-12: poles and power lines ran across the apron
and between the taxiways). One shared answer for the three pole sources: furniture's guessed poles, mappedpoles' mapped
ones and powerlines' towers; they ask it through the tile's RoadClearance (`airside`, set by roads.build_tile where the tile's
cells hold airport pavement), and the wires they add are cut by cut_wires.

Keep(pads_xy, margin) -> Keep: pads_xy as roads._pads_xy: [(ring (k, 2) in the tile's frame, (x0, y0, x1, y1))]. Falsy
without any. blocks(x, y): is the point on a pad or within margin of one. cut_wires(keep, wires, start): the wires[start:]
(polylines (k, 3), or (polyline, style)) without their segments that touch the keep-out, in place. Pure.
"""
import numpy as np

import dnp

MARGIN_M = 15.0
STEP_M = 4.0                    # a wire's segment is sampled this finely (a pad's corner is 15 m grown)
EXTRA_GROUPS = ("pylon", "hvpole")        # powerlines' towers and poles


def pole_groups():
    """The props groups that are overhead-line supports (poleprofiles' pole styles, the stock pole, powerlines')."""
    import poleprofiles
    return frozenset(poleprofiles.POLE_GROUPS) | frozenset(EXTRA_GROUPS)


def _inside(ring, pts):
    """Which of the points (m, 2) lie inside the ring (even-odd)."""
    a, b = ring, np.roll(ring, -1, axis=0)
    x, y = pts[:, 0:1], pts[:, 1:2]
    up = (a[:, 1] > y) != (b[:, 1] > y)
    dy = np.where(b[:, 1] != a[:, 1], b[:, 1] - a[:, 1], 1.0)
    cross = a[:, 0] + (y - a[:, 1]) * (b[:, 0] - a[:, 0]) / dy
    return (up & (x < cross)).sum(1) % 2 == 1


def _edge_dist(ring, pts):
    """(m,) the points' distances to the ring's outline."""
    a, b = ring, np.roll(ring, -1, axis=0)
    ab = b - a
    ap = pts[:, None, :] - a[None]
    t = np.clip(dnp.vecdot(ap, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12)[None], 0.0, 1.0)
    d = ap - t[..., None] * ab[None]
    return dnp.hypot(d[..., 0], d[..., 1]).min(1)


class Keep:
    def __init__(self, pads_xy=(), margin=MARGIN_M):
        self.margin = float(margin)
        self.pads = [(np.asarray(r, np.float64), tuple(float(v) for v in bb)) for r, bb in pads_xy if len(r) >= 3]

    def __bool__(self):
        return bool(self.pads)

    def contains(self, pts):
        """(m,) bool: which of the points (m, 2) are on a pad or within the margin of one."""
        pts = np.asarray(pts, np.float64).reshape(-1, 2)
        out = np.zeros(len(pts), bool)
        m = self.margin
        for ring, (x0, y0, x1, y1) in self.pads:
            near = np.flatnonzero(~out & (pts[:, 0] >= x0 - m) & (pts[:, 0] <= x1 + m) &
                                  (pts[:, 1] >= y0 - m) & (pts[:, 1] <= y1 + m))
            if not len(near):
                continue
            q = pts[near]
            hit = _inside(ring, q)
            rest = np.flatnonzero(~hit)
            if len(rest):
                hit[rest] = _edge_dist(ring, q[rest]) < m
            out[near[hit]] = True
        return out

    def blocks(self, x, y):
        """Is (x, y) on a pad or within the margin of one?"""
        return bool(self.pads) and bool(self.contains(np.array([[x, y]]))[0])

    def _pieces(self, poly):
        """[polyline] the parts of poly (k >= 2, columns x, y, ...) whose segments keep clear; [poly] itself when none touches."""
        p = np.asarray(poly, np.float64)
        if len(p) < 2:
            return [poly]
        lo, hi = p[:, :2].min(0), p[:, :2].max(0)
        m = self.margin
        if not any(x0 - m <= hi[0] and x1 + m >= lo[0] and y0 - m <= hi[1] and y1 + m >= lo[1]
                   for _r, (x0, y0, x1, y1) in self.pads):
            return [poly]
        a, b = p[:-1, :2], p[1:, :2]
        n = np.maximum(1, np.ceil(dnp.hypot(b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]) / STEP_M)).astype(np.int64)
        seg = np.repeat(np.arange(len(a)), n + 1)
        start = np.cumsum(n + 1) - (n + 1)
        t = (np.arange(len(seg)) - np.repeat(start, n + 1)) / np.repeat(n, n + 1)
        pts = a[seg] + (b - a)[seg] * t[:, None]
        blocked = np.zeros(len(a), bool)
        blocked[seg[self.contains(pts)]] = True
        if not blocked.any():
            return [poly]
        out, i = [], 0
        while i < len(a):
            if blocked[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(a) and not blocked[j + 1]:
                j += 1
            out.append(p[i:j + 2])
            i = j + 1
        return out


def cut_wires(keep, wires, start=0):
    """wires[start:] (polylines, or (polyline, style)) with the segments on or by the keep-out removed, in place; the
    wires that never come near it are the same objects. A falsy keep (or none): nothing."""
    if not keep or wires is None or start >= len(wires):
        return
    out = []
    for w in wires[start:]:
        poly, style = (w[0], w[1:]) if isinstance(w, tuple) else (w, None)
        pieces = keep._pieces(poly)
        if len(pieces) == 1 and pieces[0] is poly:
            out.append(w)
        else:
            out.extend((q,) + style if style is not None else q for q in pieces)
    wires[start:] = out
