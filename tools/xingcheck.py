#!/usr/bin/env python3
"""xingcheck.py LAT LON R [--server DIR] [--half M] [--worst N]: what a car feels crossing a track at grade round a place,
without the game (plan 046, stage J). For every ground rail / tram part within R of the point and every road it crosses
(centre lines meeting), the road's own surface along ITS axis (the graded ground + ROAD_LIFT, which the road mesh lies on)
within HALF m either side of the crossing:
  step  the largest height step per metre along the road (a car's bump),
  dip   the surface at the crossing under (-) / over (+) the straight line between the two ends of the window (a hump or a
        hollow where the rails lie),
  rail  with --rails: the rail top of the 3D track (railways.tile_mesh) over the road's surface at the crossing.
Run it with the server's switches in the environment (play.env.d), e.g.

    python tools/xingcheck.py 49.2348 28.4128 700
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
ap.add_argument("--half", type=float, default=20.0, help="m of road either side of the crossing looked at")
ap.add_argument("--worst", type=int, default=15)
ap.add_argument("--step", type=float, default=0.05, help="a step per metre over this (m) is listed")
ap.add_argument("--rails", action="store_true", help="also the rail top of the 3D track over the road (slower)")
ap.add_argument('--hump-m', type=float, default=6.0, help='m either side the crest is measured over')
args = ap.parse_args()
HUMP_M = args.hump_m
sys.path.insert(0, args.server)

import numpy as np                      # noqa: E402
import blockworker                      # noqa: E402
import geodesy                          # noqa: E402
import grading as gradingmod            # noqa: E402
import railways                         # noqa: E402
import roads                            # noqa: E402

blockworker.init(args.cache, False, True, True, True, False, multiprocessing.Queue(1), logging.ERROR, True, True)
s = blockworker._s
g, _ = s.anchor(1, args.lat, args.lon)
frame = geodesy.EnuFrame(args.lat, args.lon)
cell = lambda k: s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")


def surface(xy, roads_only=False):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
    if g is not None:
        z = g.apply(la, lo, z, gradingmod.ROAD_LIFT, roads_only=roads_only)
    return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(z, np.float64))[2], np.float64)


def latlon(x, y):
    la, lo, _ = frame.enu_to_geodetic(np.array([x]), np.array([y]), 0.0)
    return float(la[0]), float(lo[0])


def along(line, s0, half, step=1.0):
    """Points of the polyline every step m within half m of arc s0."""
    seg = np.hypot(*np.diff(line, axis=0).T)
    arc = np.r_[0.0, np.cumsum(seg)]
    at = np.arange(max(0.0, s0 - half), min(arc[-1], s0 + half) + 1e-9, step)
    return at, np.c_[np.interp(at, arc, line[:, 0]), np.interp(at, arc, line[:, 1])]


rows, seen = [], set()
for tx, ty in roads.tiles_near(0.0, 0.0, args.r):
    box = roads.tile_bbox(tx, ty)
    cells = [cell(k) for k in roads.cells_of_tile(frame, tx, ty, railways.REACH_M)]
    rails = railways.parts(frame, cells, box, reach=60.0, clip=True, trams="only") + \
        [p for p in railways.parts(frame, cells, box, reach=60.0, clip=True) if p[2] == "ground"]
    lines = []
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        sub = np.asarray(feats.attrs["subtype"]).astype(str)
        specs = None
        for i in range(len(feats)):
            if sub[i] != "road":
                continue
            specs = specs or roads.road_specs(feats)
            if specs[i] is None:
                continue
            for k, part in enumerate(roads.ground_parts(feats, i)):
                ll = np.asarray(part, np.float64)
                xy = np.stack(frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))[:2], axis=1)
                lines.append(((str(feats.ids[i]), k), str(feats.attrs["class"][i]), specs[i].width, xy))
    for fid, cls, kind, rxy in rails:
        if np.hypot(rxy[:, 0], rxy[:, 1]).min() > args.r:
            continue
        for (rid, k), rcls, rw, lxy in lines:
            for c in roads._crossings([rxy], [lxy]):
                pt = np.asarray(c[2], np.float64)[:2]
                key = (fid, rid, k, round(float(pt[0])), round(float(pt[1])))
                if key in seen or np.hypot(*pt) > args.r:
                    continue
                seen.add(key)
                seg = np.r_[0.0, np.cumsum(np.hypot(*np.diff(lxy, axis=0).T))]
                a, b = lxy[:-1], lxy[1:]
                ab = b - a
                t = np.clip(((pt - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-9), 0.0, 1.0)
                j = int(np.argmin(np.hypot(*(a + t[:, None] * ab - pt).T)))
                s_cross = float(seg[j] + t[j] * np.hypot(*ab[j]))      # (the arc on the road, by projecting the point)
                at, p = along(lxy, s_cross, args.half)
                if len(at) < 6:
                    continue
                z = surface(p)
                dz = np.abs(np.diff(z)) / np.maximum(np.diff(at), 1e-9)
                line = np.interp(at, [at[0], at[-1]], [z[0], z[-1]])
                m = int(np.argmin(np.abs(at - s_cross)))
                # the crest / sag a car feels: the surface at the crossing against the mean of the surface HUMP_M either side
                # (a straight ramp: 0), and the grade change over those spans (kink); the steepest 4 m grade within 12 m
                zc = float(np.interp(s_cross, at, z))
                zm, zp = float(np.interp(s_cross - HUMP_M, at, z)), float(np.interp(s_cross + HUMP_M, at, z))
                hump = zc - 0.5 * (zm + zp)
                near = np.abs(at[:-1] - s_cross) <= 12.0
                steep = float(dz[near].max()) if near.any() else 0.0
                rows.append((steep, hump, abs((zp - zc) - (zc - zm)) / HUMP_M, cls, kind, rcls, rw, fid[:8], rid[:8], *latlon(*pt)))
print("crossings at grade (a track's centre line meeting a road's): %d" % len(rows))
rows.sort(key=lambda r: -max(r[0] / 0.06, abs(r[1]) / 0.15))
for lim in (0.03, 0.05, 0.10, 0.20):
    print("  road step per m > %3.0f cm: %d" % (lim * 100, sum(r[0] > lim for r in rows)))
print("  crest / sag over 15 cm against the surface %.0f m either side: %d (kink over 4 %%: %d)" % (
    HUMP_M, sum(abs(r[1]) > 0.15 for r in rows), sum(r[2] > 0.04 for r in rows)))
print("worst (steepest grade m/m within 12 m, crest(+)/sag(-) m, kink m/m, track class, kind, road class, road width, track, road, lat, lon):")
for r in rows[:args.worst]:
    print("  %.2f  %+.2f  %.3f  %-10s %-6s %-12s w%.1f  %s %s  at %.5f,%.5f" % (r[0], r[1], r[2], *r[3:]))
sys.exit(1 if rows and rows[0][0] > args.step else 0)
