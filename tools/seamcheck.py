#!/usr/bin/env python3
"""seamcheck.py LAT LON R [--server DIR] [--worst N]: the seams of the bridge decks the server plans round a place, without
the game (plan 028). Per deck: the step at each end (the deck's end height minus the graded road 1 m past it) and the
steepest grade inside it over 2 m. An end where another deck goes on is a joint of the chain, not a seam. A seam is a step over STEP_M or a grade over GRADE_MAX (the deck's own limit, 15 %).
Exit status 1 when there is one. Run it with the server's switches in the environment (play.env.d), e.g.

    cd server && python ../tools/seamcheck.py 50.5171 16.2125 400
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
ap.add_argument("--worst", type=int, default=12)
ap.add_argument("--step", type=float, default=0.15)
ap.add_argument("--grade", type=float, default=0.15)
args = ap.parse_args()
sys.path.insert(0, args.server)

import numpy as np                      # noqa: E402
import blockworker                      # noqa: E402
import geodesy                          # noqa: E402
import grading as gradingmod            # noqa: E402
import roads                            # noqa: E402

lat, lon, R = args.lat, args.lon, args.r
blockworker.init(args.cache, False, True, True, True, False, multiprocessing.Queue(1), logging.ERROR, True, True)
s = blockworker._s
g, _ = s.anchor(1, lat, lon)
frame = geodesy.EnuFrame(lat, lon)


def graded_z(xy):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
    if g is not None:
        z = g.apply(la, lo, z, gradingmod.ROAD_LIFT)
    return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(z, np.float64))[2], np.float64)


bridges = blockworker._bridges(1, frame, lambda la, lo: g.apply(la, lo, blockworker.ground(
    frame, la, lo, s.dem.sample(la, lo, 15)), gradingmod.ROAD_LIFT) if g is not None else blockworker.ground(
    frame, la, lo, s.dem.sample(la, lo, 15)))
decks = {}
for tx, ty in roads.tiles_near(0.0, 0.0, R):
    keys = roads.cells_of_tile(frame, tx, ty)
    feats = [s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads") for k in keys]
    for fid, spec, xy, z, *_ in bridges.parts(feats, roads.tile_bbox(tx, ty)):
        xy = np.asarray(xy, np.float64)[:, :2]
        if np.hypot(*xy.T).min() <= R:
            decks[(str(fid), round(xy[0, 0]), round(xy[0, 1]))] = (xy, np.asarray(z, np.float64), spec)

JOINT_M = 0.75
ends = [v[0] for v in decks.values()]
rows = []
for (fid, *_), (xy, z, spec) in decks.items():
    arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if arc[-1] < 3.0 or len(xy) < 2:
        continue
    steps = []
    for end in (0, -1):
        p, q = xy[end], xy[1 if end == 0 else -2]
        if any((o is not xy) and min(float(np.hypot(*(o[0] - p))), float(np.hypot(*(o[-1] - p)))) < JOINT_M
               for o in ends):            # (another deck goes on from here: a joint of the chain, not an abutment)
            steps.append(0.0)
            continue
        d = (p - q) / max(float(np.hypot(*(p - q))), 1e-9)
        steps.append(float(z[end] - graded_z((p + d)[None])[0]))
    grid = np.arange(0.0, arc[-1] + 1e-9, 1.0)
    zg = np.interp(grid, arc, z)
    gr = float(np.max(np.abs(zg[2:] - zg[:-2])) / 2.0) if len(zg) > 2 else 0.0
    la, lo, _ = frame.enu_to_geodetic(xy[len(xy) // 2:len(xy) // 2 + 1, 0], xy[len(xy) // 2:len(xy) // 2 + 1, 1], 0.0)
    rows.append((max(abs(steps[0]), abs(steps[1])), gr, steps, spec.cls, float(arc[-1]), float(la[0]), float(lo[0])))

bad = [r for r in rows if r[0] > args.step or r[1] > args.grade]
print(f"place {lat},{lon} R {R}: {len(rows)} decks, {len(bad)} with a seam (step > {args.step} m or grade > {args.grade:.0%})")
for r in sorted(rows, key=lambda r: -max(r[0] / args.step, r[1] / args.grade))[:args.worst]:
    print(f"  {r[3]:12s} {r[4]:6.0f} m  steps {r[2][0]:+5.2f} {r[2][1]:+5.2f}  grade {r[1]:5.1%}  {r[5]:.5f},{r[6]:.5f}")
sys.exit(1 if bad else 0)
