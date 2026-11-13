"""The ground a bus station keeps for itself (plan 033): its yard, platforms, roofs and drive lanes. Props that belong to the
stations' surroundings (power poles, the map's own bus stop posts and shelters, parked cars) stood on the platforms and in
the yard (the user's shots of Trutnov, Cesky Krumlov, Rakovnik, 2026-11-09: wooden poles on the platforms, a stock
shelter and a white post beside every one of our bays). One answer to "is this point the station's?" for all of them.

keepout(md, frame, box) -> Keepout (empty without a station): contains(pts, margin), push_out(pts, margin).
"""
import numpy as np
import dnp

import busstations as bs

MARGIN_M = 1.5              # props keep this far off the station's ground


class Keepout:
    def __init__(self, rings=()):
        self.rings = [np.asarray(r, float) for r in rings if len(r) >= 3]
        self.boxes = [(r.min(0), r.max(0)) for r in self.rings]

    def __bool__(self):
        return bool(self.rings)

    def depth(self, pts, margin=MARGIN_M):
        """(k,) how far inside the station's ground (grown by margin) each point is: > 0 in it, <= 0 out of it."""
        import busstationmesh as bm
        pts = np.asarray(pts, float).reshape(-1, 2)
        out = np.full(len(pts), -1e9)
        for r, (lo, hi) in zip(self.rings, self.boxes):
            near = np.flatnonzero((pts[:, 0] >= lo[0] - margin) & (pts[:, 0] <= hi[0] + margin) &
                                  (pts[:, 1] >= lo[1] - margin) & (pts[:, 1] <= hi[1] + margin))
            if len(near):
                out[near] = np.maximum(out[near], bm._sd_ring(r, pts[near]) + margin)
        return out

    def contains(self, pts, margin=MARGIN_M):
        """(k,) bool: the points on the station's ground or within margin of it."""
        if not self.rings:
            return np.zeros(len(np.asarray(pts).reshape(-1, 2)), bool)
        return self.depth(pts, margin) > 0.0

    def push_out(self, pts, margin=MARGIN_M + 0.5):
        """pts (k, 2) with the ones on the station's ground moved out to margin beyond the nearest edge of it (a power
        line's vertex steps off the yard): across the nearest ring edge whose far side is not another ring's ground (the yard
        is many touching rectangles). The rest as they were. A copy."""
        pts = np.array(pts, float).reshape(-1, 2)
        if not self.rings:
            return pts
        for i in np.flatnonzero(self.contains(pts, 0.0)):
            cands = []
            for r in self.rings:
                q, inward = _nearest_edge(r, pts[i])
                cands.append((float(dnp.hypot(*(q - pts[i]))), q, inward))
            cands.sort(key=lambda c: c[0])
            for _d, q, inward in cands:
                for grow in (margin, margin + 3.0, margin + 8.0):
                    t = q - inward * grow
                    if not self.contains(t[None], 0.0)[0]:
                        pts[i] = t
                        break
                else:
                    continue
                break
        return pts


def _nearest_edge(ring, p):
    """(nearest point of the ring's outline to p, unit vector from it into the ring)."""
    ring = bs._open(np.asarray(ring, float))
    a, b = ring, np.roll(ring, -1, axis=0)
    ab = b - a
    t = np.clip(((p[0] - a[:, 0]) * ab[:, 0] + (p[1] - a[:, 1]) * ab[:, 1]) / np.maximum((ab * ab).sum(1), 1e-9), 0.0, 1.0)
    q = a + ab * t[:, None]
    k = int(np.argmin(dnp.hypot(*(q - p).T)))
    edge = ab[k] / max(float(dnp.hypot(*ab[k])), 1e-9)
    normal = np.array([-edge[1], edge[0]])                   # (left of a counter-clockwise ring: inward)
    area = float(dnp.dot(ring[:, 0], np.roll(ring[:, 1], -1)) - dnp.dot(np.roll(ring[:, 0], -1), ring[:, 1]))
    if area < 0:
        normal = -normal
    return q[k], normal


def of_stations(stations):
    """A Keepout of the stations' yards, platforms and roofs."""
    import busstationmesh as bm
    import busyard
    rings = []
    for st in stations:
        if not st.confident():
            continue
        rings += [np.asarray(r, float) for r in busyard.yard_polys(st)]
        rings += bm._platform_rings(st)
        rings += [bm._ccw(c["ring"]) for c in st.canopies if len(c["ring"]) >= 3]
    return Keepout(rings)


def of_rail_stations(stations):
    """A Keepout of the railway stations' platforms and roofs (the tracks' own ground is the railway mesh's)."""
    rings = []
    for st in stations:
        rings += [np.asarray(p.ring, float) for p in st.platforms if len(p.ring) >= 3]
        rings += [np.asarray(r, float) for _i, r in st.roofs if len(r) >= 3]
    return Keepout(rings)


def keepout(md, frame, box):
    """The Keepout of the stations (bus and railway) near frame's box (x0, y0, x1, y1); empty (falsy) when there is none or
    the features are off. Errors cost the keepout, never the tile."""
    import logging
    rings = []
    # (the stations as laid out once per group, their rings moved into frame: a group laid out again for each road
    # tile was ~2 s of each in Lower Manhattan, 2026-11-11; busstations.stations_near's shared)
    if md is not None and bs.ON:
        try:
            for st in bs.stations_near(md, frame, box, shared=True):
                rings += [bs.to_frame(getattr(st, "group_frame", None) or frame, frame, r) for r in of_stations([st]).rings]
        except Exception:
            logging.getLogger(__name__).exception("bus station keepout")
    if md is not None:
        try:
            import railstations
            if railstations.ON:
                for st in railstations.stations_near(md, frame, box, shared=True):
                    rings += [bs.to_frame(getattr(st, "group_frame", None) or frame, frame, r) for r in of_rail_stations([st]).rings]
        except Exception:
            logging.getLogger(__name__).exception("rail station keepout")
    return Keepout(rings)
