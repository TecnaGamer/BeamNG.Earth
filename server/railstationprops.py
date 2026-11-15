"""Benches, bins and lamps on railway platforms (task 037), as forest rows of the stock furniture like busfurniture's:
the same groups and row format [group, x, y, z, yaw, scale, height, rank], standing on the platform's top.

    rows(frame, md, box, elev, country_at=None) -> [row] of the stations near road tile box, the tile's own points only

Data first: bench=yes / bin=yes on a platform put them along it; a platform under a roof (or shelter=yes) gets benches
too; lamps along every platform unless lit=no. All stand along the track edge's run, inside it by a fixed distance, facing
the track, never nearer than FAR_EDGE_M to the platform's other edges.
"""
import math

import numpy as np
import dmath
import dnp

import busfurniture as bf
import busstations as bs
import railstationmesh as rm
import railstations as rs

BENCH_IN_M = 2.4            # a bench stands this far in from the track edge (the safety line is at 1.0 m)
LAMP_IN_M = 1.8
BENCH_STEP_M = 16.0
BIN_STEP_M = 32.0
LAMP_STEP_M = 28.0
FAR_EDGE_M = 0.9            # nothing stands nearer than this to the platform's other edges
MIN_WIDTH_M = 3.4           # a platform narrower than this has no furniture
MAX_BENCHES, MAX_BINS, MAX_LAMPS = 10, 5, 14        # per platform


def _run_points(pl, step, inset, start=0.0):
    """[(x, y, tx, ty, nx, ny)] every `step` m along each run of the platform's track edge, `inset` m in: the point, the
    run's direction and the inward normal; only where the far side is at least FAR_EDGE_M further."""
    out = []
    for run in rm._runs(pl.snapped, pl.ring, pl.tidx):
        if len(run) < 3:
            continue
        pts = pl.ring[run]
        s = np.concatenate(([0.0], np.cumsum(dnp.hypot(*np.diff(pts, axis=0).T))))
        if s[-1] < step * 0.5:
            continue
        for at in np.arange(start + step / 2.0, s[-1], step):
            x, y = np.interp(at, s, pts[:, 0]), np.interp(at, s, pts[:, 1])
            k = min(int(np.searchsorted(s, at)), len(pts) - 1)
            a, b = pts[max(0, k - 1)], pts[min(len(pts) - 1, k)]
            d = b - a
            dl = float(dnp.hypot(*d))
            if dl < 1e-6:
                continue
            t = d / dl
            n = np.array([-t[1], t[0]])                     # (the platform is to the left of its ccw ring)
            p = np.array([x, y]) + n * inset
            if not bs.contains(pl.ring, p) or bs.dist_to_path(pl.ring, p, True) < FAR_EDGE_M:
                continue
            out.append((float(p[0]), float(p[1]), float(t[0]), float(t[1]), float(n[0]), float(n[1])))
    return out


def _length(pl):
    """The platform's length: the longest run of its track edge, else the bounding box's long side."""
    best = 0.0
    for run in rm._runs(pl.snapped, pl.ring, pl.tidx):
        pts = pl.ring[run]
        best = max(best, float(dnp.hypot(*np.diff(pts, axis=0).T).sum()))
    return best if best > 0.0 else float(np.ptp(pl.ring, axis=0).max())


def furniture(pl):
    """[(group, x, y, fx, fy, kind)] of one platform: kind bench / bin / lamp, the direction it faces (to the track)."""
    out = []
    if bs.area(pl.ring) / max(1.0, _length(pl)) < MIN_WIDTH_M:
        return out
    tags = pl.tags
    bench = tags.get("bench") == "yes" or (tags.get("bench") != "no" and pl.covered)
    if bench:
        for x, y, tx, ty, nx, ny in _run_points(pl, BENCH_STEP_M, BENCH_IN_M)[:MAX_BENCHES]:
            out.append((bf.BENCH, x, y, -nx, -ny, "bench"))
    if tags.get("bin") == "yes" or bench:
        for x, y, tx, ty, nx, ny in _run_points(pl, BIN_STEP_M, BENCH_IN_M, start=BENCH_STEP_M / 2.0)[:MAX_BINS]:
            out.append((bf.BIN, x, y, -nx, -ny, "bin"))
    if tags.get("lit") != "no":
        for x, y, tx, ty, nx, ny in _run_points(pl, LAMP_STEP_M, LAMP_IN_M)[:MAX_LAMPS]:
            out.append((bf.LAMP, x, y, -nx, -ny, "lamp"))
    return out


def rows(frame, md, box, elev, country_at=None):
    """Forest rows of the furniture of the railway stations near road tile box, the tile's own points only."""
    if not rs.ON or md is None:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    out, lamp_rows = [], []
    for st in rs.stations_near(md, frame, box):
        for pl in st.platforms:
            lo, hi = pl.ring.min(0), pl.ring.max(0)
            if hi[0] < x0 or lo[0] > x1 or hi[1] < y0 or lo[1] > y1:
                continue
            items = [it for it in furniture(pl) if x0 <= it[1] < x1 and y0 <= it[2] < y1]
            if not items:
                continue
            z = rm.surface(frame, elev, st, pl)(np.array([[it[1], it[2]] for it in items]))
            for (group, x, y, fx, fy, kind), zz in zip(items, z):
                yaw = math.degrees(dmath.atan2(fy, fx)) + (bf.BENCH_YAW if group == bf.BENCH else 0.0)
                row = [group, round(x, 2), round(y, 2), round(float(zz), 2), round(yaw % 360.0, 1), 1.0,
                       {"bench": bf.BENCH_H, "bin": bf.BIN_H, "lamp": bf.LAMP_H}[kind], 0.0]
                (lamp_rows if kind == "lamp" else out).append(row)
    if lamp_rows:
        import furniture as streetfurniture
        if country_at is not None:
            import citylamps
            lamp_rows = citylamps.localize(lamp_rows, country_at)
        out += lamp_rows + streetfurniture.lamp_lenses(lamp_rows)
    return out
