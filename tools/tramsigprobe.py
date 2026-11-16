#!/usr/bin/env python3
"""tramsigprobe.py LAT LON R [--server DIR] [--cache DIR]: the tram signals (server/tramsignals.py, plan 046 stage H) the tiles
within R metres of a place would get, without the game: for each, where it stands, which way it faces, the junction's cycle,
and its distance to the nearest carriageway edge and tram axis (a post on a lane or in a tram's gauge is a bug). Run it with the
server's switches in the environment (BNE_TRAM=1 BNE_CATENARY=1 ...), e.g.

    PYTHONPATH= python -I tools/tramsigprobe.py 49.2348 28.4128 700
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
ap.add_argument("--country", default="")
ap.add_argument("--left", action="store_true", help="left-hand traffic")
args = ap.parse_args()
sys.path.insert(0, args.server)

import numpy as np                      # noqa: E402
import blockworker                      # noqa: E402
import catenary as cat                  # noqa: E402
import furniture                        # noqa: E402
import geodesy                          # noqa: E402
import railways                         # noqa: E402
import roads                            # noqa: E402
import tramcat                          # noqa: E402
import tramsignals                      # noqa: E402

blockworker.init(args.cache, False, True, True, True, False, multiprocessing.Queue(1), logging.ERROR, True, True)
s = blockworker._s
frame = geodesy.EnuFrame(args.lat, args.lon)
cell = lambda k: s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")


def latlon(x, y):
    la, lo, _ = frame.enu_to_geodetic(np.array([x]), np.array([y]), 0.0)
    return float(la[0]), float(lo[0])


rows, bad = {}, 0
for tx, ty in roads.tiles_near(0.0, 0.0, args.r):
    box = roads.tile_bbox(tx, ty)
    cells = [cell(k) for k in roads.cells_of_tile(frame, tx, ty, railways.REACH_M)]
    sig = furniture.signals_for(s.md, frame, roads.cells_of_tile(frame, tx, ty, blockworker.SIGNALS_MARGIN_M))
    got = tramsignals.tile_rows(frame, box, cells, lambda xy: np.zeros(len(np.atleast_2d(xy))), sig, args.country, args.left)
    parts = [xy for _f, xy, k in tramcat.tram_parts(frame, cells, box, tramsignals.REACH_M) if k == "ground"]
    lines = cat._road_lines(frame, cells, box, tramsignals.REACH_M)
    for r in got:
        p = np.array(r[:2])
        dr = min(cat._dist_to_lines(p, [ln]) - hw for ln, hw in lines) if lines else 99.0
        dt = min(cat._dist_to_lines(p, [xy]) for xy in parts) if parts else 99.0
        rows[(r[0], r[1])] = (r, dr, dt)
        bad += dr < 0.5 or dt < 1.5
print("tram signals within %.0f m: %d, on a lane or in a gauge: %d" % (args.r, len(rows), bad))
for (r, dr, dt) in sorted(rows.values(), key=lambda v: (v[0][5], v[0][0])):
    print("  %.5f,%.5f yaw %5.1f style %d cycle %d off %d green %d   to the road's edge %.1f m, to the track %.1f m" % (
        *latlon(r[0], r[1]), r[3], r[4], r[5], r[6], r[7], dr, dt))
sys.exit(1 if bad else 0)
