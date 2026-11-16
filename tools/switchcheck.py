#!/usr/bin/env python3
"""switchcheck.py LAT LON R [COUNTRY] [--server DIR]: the turnouts of the trams' 3D open track round a place (plan 046, switches
in 3D): per turnout its zone (m before which the branch has no sleepers of its own), where its rails part from the stock rails,
the frog, and the overlaps a track's sleepers make over another track's rails, before (sleepers from the heel, as the old code
laid them) and after (from the zone). Run with the server's switches in the environment (play.env.d), e.g.

    python tools/switchcheck.py 49.2356 28.4036 250 UA
"""
import argparse
import logging
import multiprocessing
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("lat", type=float)
ap.add_argument("lon", type=float)
ap.add_argument("r", type=float)
ap.add_argument("country", nargs="?", default=None)
ap.add_argument("--server", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
ap.add_argument("--cache", default=os.path.expanduser("~/.cache/beamng-earth"))
args = ap.parse_args()
sys.path.insert(0, args.server)

import numpy as np                      # noqa: E402
import blockworker                      # noqa: E402
import geodesy                          # noqa: E402
import rails                            # noqa: E402
import railways                         # noqa: E402
import roads                            # noqa: E402
import tramtrack                        # noqa: E402

blockworker.init(args.cache, False, True, True, True, False, multiprocessing.Queue(1), logging.ERROR, True, True)
s = blockworker._s
frame = geodesy.EnuFrame(args.lat, args.lon)
cell = lambda k: s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
tramtrack.ENABLED = True
HALF_SLEEPER = railways.TRAM_SLEEPER_L / 2
HALF_G = 0.5 + tramtrack.HEAD_M / 2 if hasattr(tramtrack, "HEAD_M") else 0.5375


def crossings(p0, p1, line):
    """How many of the segments of `line` the segment p0-p1 crosses."""
    a, b = line[:-1], line[1:]
    e, f = b - a, (p1 - p0)
    den = e[:, 0] * f[1] - e[:, 1] * f[0]
    ok = np.abs(den) > 1e-12
    pq = p0 - a
    t = (pq[:, 0] * f[1] - pq[:, 1] * f[0]) / np.where(ok, den, 1.0)
    u = (pq[:, 0] * e[:, 1] - pq[:, 1] * e[:, 0]) / np.where(ok, den, 1.0)
    return int((ok & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1)).sum())


def overlaps(branch, lo, through, gauge):
    """Sleepers of `branch` (every TRAM_SLEEPER_EVERY m from arc lo, TRAM_SLEEPER_L long across it) that cross a rail of `through`."""
    rails_ = [railways.roads.offset_polyline(through, side * (gauge / 2 + 0.0375)) for side in (1, -1)]
    arc = railways._arc(branch)
    u = railways._left(branch)
    n = 0
    for sa in np.arange(max(lo, 0.0), arc[-1], railways.TRAM_SLEEPER_EVERY):
        c = np.array([np.interp(sa, arc, branch[:, 0]), np.interp(sa, arc, branch[:, 1])])
        d = np.array([np.interp(sa, arc, u[:, 0]), np.interp(sa, arc, u[:, 1])])
        d = d / max(float(np.hypot(*d)), 1e-9)
        a, b = c - d * HALF_SLEEPER, c + d * HALF_SLEEPER
        if any(crossings(a, b, r) for r in rails_):
            n += 1
    return n


rows, before, after = [], 0, 0
seen = set()
for tx, ty in roads.tiles_near(0.0, 0.0, args.r):
    box = roads.tile_bbox(tx, ty)
    cells = [cell(k) for k in roads.cells_of_tile(frame, tx, ty, railways.REACH_M)]
    runs = rails.tram_open_runs(frame, tx, ty, cells, args.country, (box[0], box[1], box[2], box[3]))
    for k, (run, g, _rs, _re, info) in enumerate(runs):
        b = info.get("branch")
        if not b:
            continue
        key = (round(float(run[0, 0]), 1), round(float(run[0, 1]), 1))
        if key in seen:
            continue
        seen.add(key)
        xy = railways._resample(run, 2.0)
        # the through run: the one with a mark for this branch (its heel at the branch's start)
        thr = None
        for run2, g2, _a, _b, info2 in runs:
            for m in info2.get("marks", ()):
                if np.hypot(*(np.asarray(m["branch"])[0] - run[0])) < 0.5:
                    thr = railways._resample(run2, 2.0)
        o0 = overlaps(xy, 0.0, thr, g) if thr is not None else -1
        o1 = overlaps(xy, b["zone"], thr, g) if thr is not None else -1
        before += max(o0, 0)
        after += max(o1, 0)
        la, lo, _ = frame.enu_to_geodetic(np.array([run[0, 0]]), np.array([run[0, 1]]), 0.0)
        rows.append((float(la[0]), float(lo[0]), b["zone"], b["div"], b["frog"] is not None, o0, o1))
print("turnouts of the 3D tram track within %.0f m: %d" % (args.r, len(rows)))
print("  frogs found: %d" % sum(r[4] for r in rows))
print("  sleepers of a branch crossing the through track's rails: before %d, after %d" % (before, after))
for r in rows[:20]:
    print("   %.5f,%.5f  zone %.1f m, rails part at %.1f m, frog %s, overlaps %d -> %d" % (r[0], r[1], r[2], r[3], "yes" if r[4] else "no", r[5], r[6]))
