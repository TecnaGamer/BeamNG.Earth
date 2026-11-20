#!/usr/bin/env python3
"""probe_localpoi.py: what the local POIs (server localpoi.py) would draw around a point, from the real map data.

    python tools/probe_localpoi.py LAT LON [RADIUS_M] [--png out.png]

Reads the Overture cells from the Earth cache (nothing new downloaded for them) and the OpenStreetMap picnic rows from
Layercake (a few range requests). Prints each candidate, the spots chosen and the rows drawn; --png draws them over
the roads of the cache. No game, no server.
"""
import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import geodesy  # noqa: E402
import localpoi  # noqa: E402
import mapdata  # noqa: E402
import netfetch  # noqa: E402

COLOURS = {"lp_table": (140, 90, 40), "lp_altan": (30, 110, 40), "lp_firepit": (200, 40, 40), "picnicgrill": (200, 40, 40)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("radius", type=float, nargs="?", default=300.0)
    ap.add_argument("--png")
    ap.add_argument("--offline", action="store_true", help="no Layercake (Overture cache only)")
    a = ap.parse_args()
    frame = geodesy.EnuFrame(a.lat, a.lon, 0.0)
    md = mapdata.MapData(os.path.expanduser("~/.cache/beamng-earth"), netfetch.Fetcher(offline=a.offline))
    layer = lambda name: (lambda key: md.cell(key, layers=(name,), kind="demand").layers.get(name))
    r = a.radius
    box = (-r, -r, r, r)
    osm = None if a.offline else (lambda keys: md.picnic_osm.cells(keys))
    found, buses, parks, keys = localpoi.candidates(frame, layer("infrastructure"), layer("land_use"), box, osm)
    print(f"{len(keys)} map cells, {len(found)} candidates, {len(buses)} bus stops, {len(parks)} park rings")
    for kind, fid, x, y, ring, area in sorted(found, key=lambda c: (c[2] ** 2 + c[3] ** 2)):
        print(f"  {kind:8s} {fid:24s} {x:8.1f} {y:8.1f}  {np.hypot(x, y):6.0f} m  area {area:7.1f}")
    spots = localpoi.plan(found, buses, parks, box)
    print(f"{len(spots)} spots planned:")
    for group, fid, x, y, yaw in spots:
        print(f"  {group:12s} {fid:24s} {x:8.1f} {y:8.1f} yaw {yaw:5.1f}")
    if a.png:
        from PIL import Image, ImageDraw
        size = 900
        sc = size / (2 * r)
        img = Image.new("RGB", (size, size), (238, 240, 232))
        d = ImageDraw.Draw(img)
        to = lambda x, y: (size / 2 + x * sc, size / 2 - y * sc)
        for ring in parks:
            d.polygon([to(*p) for p in ring], fill=(205, 225, 190))
        for x, y in buses:
            px, py = to(x, y)
            d.rectangle((px - 4, py - 4, px + 4, py + 4), outline=(0, 0, 160))
        for group, fid, x, y, yaw in spots:
            px, py = to(x, y)
            d.ellipse((px - 6, py - 6, px + 6, py + 6), fill=COLOURS.get(group, (0, 0, 0)))
        d.ellipse((size / 2 - 3, size / 2 - 3, size / 2 + 3, size / 2 + 3), outline=(0, 0, 0))
        img.save(a.png)
        print("wrote", a.png)


if __name__ == "__main__":
    main()
