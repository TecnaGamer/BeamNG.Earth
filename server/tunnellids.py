"""Tunnel lids (GitHub #1083): the ground a bare-earth DEM leaves out over a tunnel under a road mapped on the ground.

A lidar terrain model leaves out structures: over a short cut-and-cover underpass (Vienna's Suedtiroler Platz over the
Wiedner Guertel, Porte Maillot in Paris), a motorway's cover section, or a road bridge mapped as a plain way over a
road mapped as a tunnel, it has the cutting's floor where the lid or the deck should be. tunnel_sections then saw no
ground over the tunnel (an open road), and the road mapped on top followed the DEM down into the trench: a pit 3-7 m
deep across a square or a road, retaining walls along it, its asphalt broken over the steps.

The evidence is the road on top: a ground road for motor traffic crossing over a tunnel cannot be at its floor. Where
the DEM puts it within LID_FLOOR_M of the tunnel's floor while the same road stands TUNNEL_OPEN_COVER over that floor
on both sides of the crossing (within LID_REACH_M, along itself), the structure is missing: a lid is put back under
the road, straight between where it leaves the trench on either side, as a deck between its abutments
(lids_of). Two such roads crossing one tunnel less than LID_JOIN_M apart, the banks between them high too: the
tunnel's width between them is filled as well (the square between Suedtiroler Platz and its slip roads).

Lids.apply puts them in the shared ground (z = max(z, lid)) before anything plans from it: the road plans
(PlanStore's elev), the tunnel sections (which then find a low arch under the road), the terrain blocks, the road
tiles and the props, in every process alike (blockworker.ground, earthd.Server.ground). Off by default: BNE_TUNNEL_LIDS=on
turns them on (measured at merge, Fells Point 16 road tiles with fresh plans: 19 cells' lids built in 8.5 s of the
probe's 51 s, no lid found; each cell's build reads its roads' tunnel specs and the DEM in Python, in every process).
Before it goes on by default: port lids_of / _crossings to Rust, build a cell's lids only where it has a tunnel part
crossed by a ground road, and bump plancache.PLANS_FORMAT.
"""
import math
import os
import threading
from collections import OrderedDict

import numpy as np
import dmath
import dnp

ON = os.environ.get("BNE_TUNNEL_LIDS", "off").lower() in ("1", "on", "true")

LID_FLOOR_M = 1.0       # m: the road on top's DEM this near the tunnel's floor at the crossing: in the trench
LID_REACH_M = 60.0      # m along the road on top, either side of the crossing, where it must climb out of the trench
                        # (Capitol Crossing over I-395: a trench ~60 m wide)
LID_STEP_M = 2.0        # m: the road on top sampled this often
LID_JOIN_M = 40.0       # m: two lids over one tunnel this near each other (along it): the ground between them too
LID_BANK_M = (8.0, 16.0, 24.0)  # m past the tunnel's edge where its banks are read (a join's)
TUNNEL_STEP_M = 4.0     # m: the tunnel sampled this often (grading.VERTEX_STEP)
NOT_MOTOR = {"footway", "path", "steps", "cycleway", "pedestrian", "bridleway", "track", "corridor", "sidewalk",
             "crossing", "living_street_foot"}
CELLS_KEPT = 256        # cells' lids kept per process
M_LAT = 111320.0


def _resample(xy, step):
    """(xy, s): the polyline xy every ~step m (its vertices' arc lengths s). Pure."""
    xy = np.asarray(xy, np.float64)
    d = dnp.hypot(*np.diff(xy, axis=0).T)
    s0 = np.r_[0.0, np.cumsum(d)]
    if s0[-1] <= 0:
        return xy[:1], np.zeros(1)
    n = max(1, int(math.ceil(s0[-1] / step)))
    s = np.linspace(0.0, s0[-1], n + 1)
    return np.c_[np.interp(s, s0, xy[:, 0]), np.interp(s, s0, xy[:, 1])], s


def _cross(a, b, p, q):
    """(t, u) of the segments a-b and p-q where they cross (0 <= t, u <= 1), else None. Pure."""
    r, d = b - a, q - p
    den = r[0] * d[1] - r[1] * d[0]
    if abs(den) < 1e-12:
        return None
    w = p - a
    t = (w[0] * d[1] - w[1] * d[0]) / den
    u = (w[0] * r[1] - w[1] * r[0]) / den
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return t, u
    return None


def _crossings(T, R):
    """[(s along T, s along R, sine of the angle)] where the polylines T and R cross. Pure."""
    sT = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(T, axis=0).T))]
    sR = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(R, axis=0).T))]
    out = []
    lo, hi = T.min(axis=0), T.max(axis=0)
    for j in range(len(R) - 1):
        p, q = R[j], R[j + 1]
        if (np.minimum(p, q) > hi).any() or (np.maximum(p, q) < lo).any():
            continue
        for i in range(len(T) - 1):
            got = _cross(T[i], T[i + 1], p, q)
            if got is None:
                continue
            t, u = got
            a, b = T[i + 1] - T[i], q - p
            la, lb = dnp.hypot(*a), dnp.hypot(*b)
            if la <= 0 or lb <= 0:
                continue
            sin = abs(a[0] * b[1] - a[1] * b[0]) / (la * lb)
            c = (sT[i] + t * la, sR[j] + u * lb, float(sin))
            if not any(abs(c[0] - o[0]) < 0.5 and abs(c[1] - o[1]) < 0.5 for o in out):    # (at a shared vertex)
                out.append(c)
    return out


def _along(R, s0, ds, reach):
    """Points of the polyline R from arc length s0 on in direction ds (+1 / -1) every LID_STEP_M up to reach, carried
    on straight past its end (a road ending at a junction short of the bank goes on there): (points (k, 2), their
    distances from s0). Pure."""
    sR = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(R, axis=0).T))]
    d = np.arange(0.0, reach + 1e-9, LID_STEP_M)
    s = s0 + ds * d
    pts = np.c_[np.interp(s, sR, R[:, 0]), np.interp(s, sR, R[:, 1])]
    past = (s < 0.0) | (s > sR[-1])
    if past.any():
        end = 0 if ds < 0 else len(R) - 1
        nb = 1 if ds < 0 else len(R) - 2
        u = R[end] - R[nb]
        n = dnp.hypot(*u)
        if n > 0:
            u = u / n
            over = np.where(s < 0.0, -s, s - sR[-1])[past]
            pts[past] = R[end] + over[:, None] * u
    return pts, d


def lids_of(tunnels, roads, z_at):
    """The lids of one map cell in a metric frame: [(xy (k, 2), z (k,), half width)].

    tunnels: [(xy (k, 2), half width)] of the tunnel parts; roads: [(xy (k, 2), half width)] of the ground parts of
    the roads for motor traffic; z_at(xy) -> the raw DEM's heights. Pure (given z_at)."""
    from roads import TUNNEL_OPEN_COVER, TUNNEL_ROAD_CLEAR_M, TUNNEL_ROAD_MIN_SIN, tunnel_sections
    from grading import TUNNEL_KEEP_M
    out = []
    for T0, thw in tunnels:
        T, sT = _resample(T0, TUNNEL_STEP_M)
        if len(T) < 3:
            continue
        tb_lo, tb_hi = T.min(axis=0) - LID_REACH_M, T.max(axis=0) + LID_REACH_M
        gT = np.asarray(z_at(T), np.float64)
        if not np.isfinite(gT).all():
            continue
        floor, _kinds = tunnel_sections(sT, gT)
        lids = []                       # (s along the tunnel, the lid's height there)
        for R, rhw in roads:
            if (R.min(axis=0) > tb_hi).any() or (R.max(axis=0) < tb_lo).any():
                continue
            for s_t, s_r, sin in _crossings(T, R):
                if sin < TUNNEL_ROAD_MIN_SIN:
                    continue
                f = float(np.interp(s_t, sT, floor))
                ends = []
                for ds in (-1.0, 1.0):
                    pts, d = _along(R, s_r, ds, LID_REACH_M)
                    z = np.asarray(z_at(pts), np.float64)
                    if not np.isfinite(z).all() or z[0] - f > LID_FLOOR_M:
                        break
                    up = np.flatnonzero(z >= f + TUNNEL_OPEN_COVER)
                    if not len(up):
                        break
                    k = int(up[0])
                    ends.append((pts[:k + 1], d[k], float(z[k])))
                if len(ends) != 2:
                    continue
                (pa, da, za), (pb, db, zb) = ends
                xy = np.r_[pa[::-1], pb[1:]]
                t = np.r_[-(np.arange(len(pa)) * LID_STEP_M)[::-1], np.arange(1, len(pb)) * LID_STEP_M]
                z = za + (zb - za) * (t + da) / max(da + db, 1e-9)
                out.append((xy, z, rhw + TUNNEL_ROAD_CLEAR_M))
                lids.append((s_t, za + (zb - za) * da / max(da + db, 1e-9)))
        lids.sort()
        for (s1, z1), (s2, z2) in zip(lids, lids[1:]):
            if not 0.0 < s2 - s1 <= LID_JOIN_M:
                continue
            m = (sT >= s1) & (sT <= s2)
            if m.sum() < 2:
                continue
            seg = T[m]
            mid = len(seg) // 2
            u = seg[min(mid + 1, len(seg) - 1)] - seg[max(mid - 1, 0)]
            n = dnp.hypot(*u)
            if n <= 0:
                continue
            nrm = np.array([-u[1], u[0]]) / n
            banks = []
            for side in (-1.0, 1.0):
                pts = seg[mid] + side * (thw + np.asarray(LID_BANK_M))[:, None] * nrm
                banks.append(float(np.median(np.asarray(z_at(pts), np.float64))))
            fm = float(np.interp(0.5 * (s1 + s2), sT, floor))
            if min(banks) < fm + TUNNEL_OPEN_COVER:
                continue
            zj = min(min(banks), z1, z2)
            out.append((seg, np.full(len(seg), zj), thw + TUNNEL_KEEP_M))
    return out


def lid_z(lids, x, y):
    """The lids' height at the points (x, y) of their frame (NaN where none). Pure."""
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    out = np.full(x.shape, np.nan)
    for xy, z, hw in lids:
        lo, hi = xy.min(axis=0) - hw, xy.max(axis=0) + hw
        m = (x >= lo[0]) & (x <= hi[0]) & (y >= lo[1]) & (y <= hi[1])
        if not m.any():
            continue
        px, py = x[m], y[m]
        best = np.full(px.shape, np.inf)
        bz = np.full(px.shape, np.nan)
        for i in range(len(xy) - 1):
            a, b = xy[i], xy[i + 1]
            ab = b - a
            L2 = float(dnp.matmul(ab, ab))
            t = np.zeros(px.shape) if L2 <= 0 else np.clip(((px - a[0]) * ab[0] + (py - a[1]) * ab[1]) / L2, 0, 1)
            d = dnp.hypot(px - (a[0] + t * ab[0]), py - (a[1] + t * ab[1]))
            nz = z[i] + t * (z[i + 1] - z[i])
            w = d < best
            best = np.where(w, d, best)
            bz = np.where(w, nz, bz)
        bz = np.where(best <= hw, bz, np.nan)
        cur = out[m]
        out[m] = np.where(np.isnan(cur), bz, np.fmax(cur, bz))
    return out


class Lids:
    """The lids of the map cells around any points, from mapdata's roads layer and the raw DEM (raw(lat, lon) -> m:
    the planning zoom's, grading.PROFILE_ZOOM, in every process alike), each cell's once."""

    def __init__(self, md, raw):
        self.md, self.raw = md, raw
        self.lock = threading.Lock()
        self.cells = OrderedDict()      # cell key -> (lat0, lon0, [lids in the cell's own metric frame])

    def _feats(self, key):
        got = self.md.cell(key, layers=("roads",), kind="demand")
        return got.layers.get("roads") if got is not None else None

    def _cell(self, key):
        with self.lock:
            got = self.cells.get(key)
            if got is not None:
                self.cells.move_to_end(key)
                return got
        try:
            got = self._build(key)
        except Exception as e:          # (no lids this time; asked again next time, not remembered)
            import logging
            logging.getLogger("earthd.tunnellids").debug("tunnel lids of %s: %s: %s", key, type(e).__name__, e)
            return 0.0, 0.0, []
        with self.lock:
            self.cells[key] = got
            while len(self.cells) > CELLS_KEPT:
                self.cells.popitem(last=False)
        return got

    def _build(self, key):
        import roads as roadsmod
        lon0, lat0, lon1, lat1 = key.bounds()
        la0, lo0 = 0.5 * (lat0 + lat1), 0.5 * (lon0 + lon1)
        k = M_LAT * dmath.cos(math.radians(la0))
        feats = self._feats(key)
        if feats is None or not len(feats):
            return la0, lo0, []

        def xy(ring):
            r = np.asarray(ring, np.float64)
            return np.c_[(r[:, 0] - lo0) * k, (r[:, 1] - la0) * M_LAT]
        ts = roadsmod.tunnel_specs(feats)
        tunnels = [(xy(r), ts[i].width / 2) for i in range(len(feats)) if ts[i] is not None
                   for r in roadsmod.tunnel_parts(feats, i) if len(r) >= 2]
        if not tunnels:
            return la0, lo0, []
        rs = roadsmod.road_specs(feats)
        cls = feats.attrs.get("class")
        roads = [(xy(r), rs[i].width / 2) for i in range(len(feats))
                 if rs[i] is not None and (cls is None or str(cls[i]) not in NOT_MOTOR)
                 for r in roadsmod.ground_parts(feats, i) if len(r) >= 2]

        def z_at(p):
            p = np.asarray(p, np.float64)
            return self.raw(la0 + p[:, 1] / M_LAT, lo0 + p[:, 0] / k)
        return la0, lo0, lids_of(tunnels, roads, z_at)

    def apply(self, la, lo, z):
        """z (the ground at (la, lo)) raised to the lids there."""
        import mapcells
        la_a, z_a = np.broadcast_arrays(np.asarray(la, np.float64), np.asarray(z, np.float64))
        shape = z_a.shape
        la_ = la_a.ravel()
        lo_ = np.broadcast_to(np.asarray(lo, np.float64), shape).ravel()
        if not len(la_):
            return z
        zz = z_a.ravel().copy()
        pad = (LID_REACH_M + 20.0) / M_LAT
        try:
            keys = mapcells.cells_for_points(la_, lo_, pad, pad / max(0.05, dmath.cos(math.radians(float(la_.mean())))))
        except Exception:
            return z
        for key in keys:
            lat0, lon0, lids = self._cell(key)
            if not lids:
                continue
            k = M_LAT * dmath.cos(math.radians(lat0))
            got = lid_z(lids, (lo_ - lon0) * k, (la_ - lat0) * M_LAT)
            zz = np.where(np.isnan(got), zz, np.fmax(zz, got))
        return zz.reshape(shape) if shape else zz.reshape(())[()]
