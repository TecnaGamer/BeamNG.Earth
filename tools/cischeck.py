#!/usr/bin/env python3
"""cischeck.py: which kind of housing the kernel (earthfast cis_classify, plan 048) makes of every building around a point,
from the real map data. No game, no server.

    python tools/cischeck.py LAT LON [RADIUS_M] [--offline] [--png out.png] [--csv out.csv]

Reads the Overture cells (buildings, roads) from the Earth cache (what is missing is downloaded like the server does,
--offline: only the cache), runs the tile builder's classification and prints a table: kinds against floors, the
confidence, how many are told "Soviet". --png paints the footprints by kind on a plain background (grey = left alone),
--csv writes one row per building (centre, floors, kind, confidence, length, depth) for tools/cis_calibrate.py. The
ground height is 0 (no DEM).
"""
import argparse
import collections
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import buildings  # noqa: E402
import cisbuildings  # noqa: E402
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import mapdata  # noqa: E402
import netfetch  # noqa: E402
import roads  # noqa: E402

COLORS = {"house_cis": (210, 170, 90), "stalinka": (190, 90, 60), "khrush_brick": (230, 120, 40), "khrush_panel": (230, 60, 60),
          "panel9": (160, 40, 160), "panel16": (90, 40, 200), "tower": (30, 90, 220), "brick_mid": (200, 90, 130),
          "late_soviet": (150, 110, 60), "new_mono": (30, 170, 120), "new_low": (110, 200, 120), "new_panel": (40, 130, 90)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("radius", type=float, nargs="?", default=800.0)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--png")
    ap.add_argument("--csv")
    a = ap.parse_args()
    cache = os.path.expanduser("~/.cache/beamng-earth")
    frame = geodesy.EnuFrame(a.lat, a.lon, 0.0)
    md = mapdata.MapData(cache, netfetch.Fetcher(offline=a.offline))
    r = a.radius
    print("country:", cisbuildings.country(a.lat, a.lon), "| kernel", getattr(cisbuildings.fast._ef, "VERSION", None))
    t0, t1 = int(np.floor(-r / roads.TILE)), int(np.ceil(r / roads.TILE))
    ground = lambda la, lo: np.zeros(np.shape(la))      # noqa: E731
    rows = []
    for tx in range(t0, t1):
        for ty in range(t0, t1):
            x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
            keys, layers = buildings.far_needs(frame, tx, ty)
            buildings.measured_wait(md, keys)
            cds = mapcells.load_cells(md, keys, layers)
            items = buildings.tile_buildings(
                frame, x0, y0, x1, y1, [c.layers.get("buildings") for c in cds], ground,
                part_layers=[c.layers.get("building_parts") for c in cds],
                roads_xy=buildings.road_lines(frame, [c.layers.get("roads") for c in cds], x0, y0))
            for spec, _g0, _g1 in items:
                ring = np.asarray(spec.ring, np.float64) + [x0, y0]
                c = ring.mean(0)
                if np.hypot(c[0], c[1]) <= r:
                    rows.append((spec, ring, c))
    kinds = collections.Counter()
    table = collections.defaultdict(collections.Counter)
    for spec, _ring, _c in rows:
        k = (spec.cis or {}).get("kit", "none")
        kinds[k] += 1
        table[k][min(int(spec.floors or 0), 25)] += 1
    print(f"{len(rows)} buildings within {r:.0f} m of {a.lat:.5f},{a.lon:.5f}")
    for k, n in kinds.most_common():
        fl = ", ".join(f"{f}:{c}" for f, c in sorted(table[k].items()))
        print(f"  {k:14s} {n:5d}   floors {fl}")
    soviet = sum(1 for s, _r, _c in rows if (s.cis or {}).get("soviet"))
    print(f"  Soviet {soviet}, new {sum(1 for s, _r, _c in rows if s.cis and not s.cis['soviet'])}")
    if a.csv:
        with open(a.csv, "w", encoding="utf-8") as f:
            f.write("x,y,floors,kit,soviet,conf,l,w,serial,entrances\n")
            for spec, _ring, c in rows:
                d = spec.cis or {}
                f.write(f"{c[0]:.1f},{c[1]:.1f},{spec.floors},{d.get('kit', 'none')},{int(bool(d.get('soviet')))},"
                        f"{d.get('conf', 0):.2f},{d.get('l', 0):.1f},{d.get('w', 0):.1f},{int(d.get('serial', 0))},"
                        f"{int(d.get('entrances', 0))}\n")
        print("wrote", a.csv)
    if a.png:
        from PIL import Image, ImageDraw
        size = 1400
        sc = size / (2 * r)
        img = Image.new("RGB", (size, size), (238, 240, 232))
        d = ImageDraw.Draw(img)
        to = lambda x, y: (size / 2 + x * sc, size / 2 - y * sc)       # noqa: E731
        for spec, ring, _c in rows:
            col = COLORS.get((spec.cis or {}).get("kit"), (170, 170, 170))
            d.polygon([to(*p) for p in ring], fill=col, outline=(40, 40, 40))
        y = 8
        for k, col in COLORS.items():
            d.rectangle((8, y, 22, y + 12), fill=col)
            d.text((28, y), f"{k} ({kinds.get(k, 0)})", fill=(0, 0, 0))
            y += 16
        img.save(a.png)
        print("wrote", a.png)


if __name__ == "__main__":
    main()
