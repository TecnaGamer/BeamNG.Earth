#!/usr/bin/env python3
"""tramcheck.py LAT LON R [--server DIR] [--worst N]: what a tram and a car feel on Earth's tram track round a place,
without the game (plan 046, stage 0). Per electrified ground/bridge tram part (tramcat.tram_parts) within R of the point:
1) vertical: the graded ground's height step per 2 m along the axis (the surface the track is draped on) against the raw
   DEM's; steps over STEP_M are listed with their coordinates (the graded surface jumps where the track leaves the
   grader's road bed; the DEM itself is flat there);
2) joints: where one part ends next to another's end (a switch or a way joint), the height difference of the two ends.
Run it with the server's switches in the environment (play.env.d: BNE_TRAM=1 BNE_CATENARY=1 ...), e.g.

    python tools/tramcheck.py 49.2348 28.4128 700
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
ap.add_argument("--server", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
ap.add_argument("--cache", default=os.path.expanduser("~/.cache/beamng-earth"))
ap.add_argument("--worst", type=int, default=15)
ap.add_argument("--step", type=float, default=0.12, help="a height step per 2 m over this (m) is listed")
args = ap.parse_args()
sys.path.insert(0, args.server)

import numpy as np                      # noqa: E402
import blockworker                      # noqa: E402
import geodesy                          # noqa: E402
import grading as gradingmod            # noqa: E402
import railways                         # noqa: E402
import roads                            # noqa: E402
import tramcat                          # noqa: E402

JOINT_M = 1.5       # two part ends this close are one joint

blockworker.init(args.cache, False, True, True, True, False, multiprocessing.Queue(1), logging.ERROR, True, True)
s = blockworker._s
g, _ = s.anchor(1, args.lat, args.lon)
frame = geodesy.EnuFrame(args.lat, args.lon)
cell = lambda k: s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")


def heights(xy, graded):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
    if graded and g is not None:
        z = g.apply(la, lo, z, gradingmod.ROAD_LIFT)
    return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(z, np.float64))[2], np.float64)


def latlon(x, y):
    la, lo, _ = frame.enu_to_geodetic(np.array([x]), np.array([y]), 0.0)
    return float(la[0]), float(lo[0])


parts = {}
for tx, ty in roads.tiles_near(0.0, 0.0, args.r):
    box = roads.tile_bbox(tx, ty)
    cells = [cell(k) for k in roads.cells_of_tile(frame, tx, ty, railways.REACH_M)]
    for fid, xy, kind in tramcat.tram_parts(frame, cells, box):
        parts[(fid, round(float(xy[0, 0])), round(float(xy[0, 1])))] = (fid, xy, kind)
print("tram parts: %d (graded surface: %s)" % (len(parts), "yes" if g is not None else "no, raw DEM only"))

steps, ends, lengths = [], [], 0.0
for fid, xy, kind in parts.values():
    zg, zr = heights(xy, True), heights(xy, False)
    ends.append((fid, kind, xy[0], zg[0], xy[-1], zg[-1]))
    near = np.hypot(xy[:, 0], xy[:, 1]) <= args.r
    d = np.diff(zg)
    seg = np.hypot(*np.diff(xy, axis=0).T)
    lengths += float(seg[near[:-1]].sum())
    for i in np.flatnonzero(near[:-1]):
        steps.append((abs(float(d[i])), float(d[i]), float(zr[i + 1] - zr[i]), fid[:8], kind, float(xy[i, 0]), float(xy[i, 1]),
                      float(seg[i])))
n = len(steps)
print("\n== vertical: graded height step per ~2 m over %.1f km of axis (%d steps)" % (lengths / 1000, n))
a = np.array([x[0] for x in steps]) if steps else np.zeros(0)
raw = np.abs(np.array([x[2] for x in steps])) if steps else np.zeros(0)
for lim in (0.03, 0.06, 0.12, 0.30):
    print("  graded > %3.0f cm: %5d (%.1f %%)   raw DEM > %3.0f cm: %5d" % (
        lim * 100, (a > lim).sum(), 100 * (a > lim).mean() if n else 0, lim * 100, (raw > lim).sum()))
steps.sort(reverse=True)
shown = [x for x in steps if x[0] > args.step][:args.worst]
print("  worst (graded dz, raw dz, part, kind, lat,lon):")
for dz, d, r, fid, kind, x, y, L in shown:
    print("   %5.2f m  graded %+.2f  raw %+.2f  %s %s  at %.5f,%.5f" % (dz, d, r, fid, kind, *latlon(x, y)))

print("\n== joints: height difference where one part ends at another's end (<= %.1f m apart)" % JOINT_M)
jd = []
for i in range(len(ends)):
    for j in range(i + 1, len(ends)):
        fi, ki, a0, za0, a1, za1 = ends[i]
        fj, kj, b0, zb0, b1, zb1 = ends[j]
        for pa, za in ((a0, za0), (a1, za1)):
            for pb, zb in ((b0, zb0), (b1, zb1)):
                if np.hypot(*(pa - pb)) <= JOINT_M:
                    jd.append((abs(float(za - zb)), fi[:8], fj[:8], *latlon(*pa)))
jd.sort(reverse=True)
print("  joints: %d, > 3 cm: %d, > 10 cm: %d" % (len(jd), sum(x[0] > 0.03 for x in jd), sum(x[0] > 0.10 for x in jd)))
for x in jd[:8]:
    print("   %.2f m  %s / %s  at %.5f,%.5f" % x)
sys.exit(1 if shown else 0)
