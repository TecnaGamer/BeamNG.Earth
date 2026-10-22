#!/usr/bin/env python3
"""What the world server makes of a few points: the DEM, the water (sea, lakes, rivers) and the map data near
each, without the game. For "why is there water / a wall / no road here" questions.

  python3 tools/probe_point.py 51.9075,4.4700 51.9330,4.5150 [--zoom 15] [--radius 25] [--no-water]
  python3 tools/probe_point.py --transect 51.9060,4.4700 51.9100,4.4700 --step 20

Per point: the DEM height (the zoom the far tiles and the physics ground read), the water depth and level
(seacarve.SeaCarver.water), the carved height, whether it is a lake (lake_at) and sea for a near (2 km) and a far
(8 km) water tile (sea_at), sink() of the DEM, the water polygons covering it and the roads and
infrastructure points within --radius m. Reads the download cache (--cache-dir, default
~/.cache/beamng-earth) and fetches what is missing like the server does. The sea rasters are built in a
temporary folder (--sea-cache to keep them), so the answer is always the current code's.
"""
import argparse
import json
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import numpy as np  # noqa: E402

import dem as demmod  # noqa: E402
import mapcells  # noqa: E402
import mapdata as mapdatamod  # noqa: E402
import netfetch  # noqa: E402
import seacarve  # noqa: E402
from polygon import Path  # noqa: E402

M_PER_DEG = 111320.0
INFRA_CLASSES = ("traffic_signals", "street_lamp", "crossing", "stop", "give_way", "bollard", "bus_stop")


def latlon(s):
    a, b = s.split(",")
    return float(a), float(b)


def transect(a, b, step):
    (la0, lo0), (la1, lo1) = a, b
    dn = (la1 - la0) * M_PER_DEG
    de = (lo1 - lo0) * M_PER_DEG * math.cos(math.radians(la0))
    n = max(1, int(math.hypot(dn, de) // step))
    return [(la0 + (la1 - la0) * k / n, lo0 + (lo1 - lo0) * k / n) for k in range(n + 1)]


def covering_water(md, la, lo):
    """The water polygons of the cell around (la, lo) that contain it: subtype, class, name, and whether the
    sea or the lakes take it (seacarve's own row filters)."""
    f = md.cell(mapcells.cell_of(la, lo), layers=("water",)).layers.get("water")
    if f is None:
        return []
    sea, lakes = set(seacarve.SeaCarver._sea_rows(f)), set(seacarve.SeaCarver._lake_rows(f))
    out = []
    for i in range(len(f.ids)):
        if f.kind[i] != 3:
            continue
        inside = False
        for part in f.parts(i):
            for ring in part:
                inside ^= bool(Path(ring[:, :2]).contains_points(np.array([[lo, la]]))[0])
        if inside:
            out.append({"subtype": str(f.attrs["subtype"][i]), "class": str(f.attrs["class"][i]),
                        "name": str(f.attrs["name"][i]), "sea": i in sea, "lake": i in lakes})
    return out


def near(md, la, lo, radius):
    """Roads (class, name) and infrastructure points (class) within radius m of (la, lo)."""
    key = mapcells.cell_of(la, lo)
    layers = md.cell(key, layers=("roads", "infrastructure")).layers
    kx = M_PER_DEG * math.cos(math.radians(la))
    roads, infra = set(), {}
    f = layers.get("roads")
    if f is not None:
        for i in range(len(f.ids)):
            for part in f.parts(i):
                for ring in part:
                    d = np.hypot((ring[:, 1] - la) * M_PER_DEG, (ring[:, 0] - lo) * kx)
                    if len(d) and d.min() <= radius:
                        roads.add(f"{f.attrs['subtype'][i]}/{f.attrs['class'][i]} {f.attrs['name'][i]}".strip())
    f = layers.get("infrastructure")
    if f is not None:
        cx, cy = np.asarray(f.attrs["cx"], float), np.asarray(f.attrs["cy"], float)
        d = np.hypot((cy - la) * M_PER_DEG, (cx - lo) * kx)
        for i in np.flatnonzero((d <= radius) & np.isin(f.attrs["class"], INFRA_CLASSES)):
            c = str(f.attrs["class"][i])
            infra[c] = infra.get(c, 0) + 1
    return sorted(roads), infra


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("points", nargs="*", type=latlon, help="lat,lon")
    ap.add_argument("--transect", nargs=2, type=latlon, metavar="LAT,LON", help="points every --step m")
    ap.add_argument("--step", type=float, default=25.0)
    ap.add_argument("--zoom", type=int, default=15, help="DEM zoom (the physics ground's)")
    ap.add_argument("--radius", type=float, default=25.0, help="m around each point for roads and infrastructure")
    ap.add_argument("--no-water", action="store_true", help="skip the sea and lakes (faster)")
    ap.add_argument("--no-map", action="store_true", help="skip the polygons, roads and infrastructure")
    ap.add_argument("--cache-dir", default=os.path.join(os.path.expanduser("~"), ".cache/beamng-earth"))
    ap.add_argument("--sea-cache", help="keep the sea rasters here (default: a temporary folder)")
    ap.add_argument("--json", help="also write the rows to this file")
    args = ap.parse_args()
    pts = list(args.points) + (transect(*args.transect, args.step) if args.transect else [])
    if not pts:
        ap.error("give points or --transect")
    lat = np.array([p[0] for p in pts])
    lon = np.array([p[1] for p in pts])

    net = netfetch.Fetcher()
    md = mapdatamod.MapData(args.cache_dir, fetcher=net)
    dem = demmod.Dem(os.path.join(args.cache_dir, "dem"), fetcher=net)
    z = np.asarray(dem.sample(lat, lon, args.zoom), np.float64)
    rows = [{"lat": round(la, 6), "lon": round(lo, 6), "dem": round(float(z[k]), 2)}
            for k, (la, lo) in enumerate(pts)]

    if not args.no_water:
        sea_dir = args.sea_cache or tempfile.mkdtemp(prefix="probe_sea_")
        sc = seacarve.SeaCarver(md, sea_dir, dem=dem)
        d, lv = sc.water(lat, lon)
        carved = sc.carve(lat, lon, z)
        lake = sc.lake_at(lat, lon)
        s2, s8 = sc.sea_at(dem, lat, lon, 2048.0), sc.sea_at(dem, lat, lon, 8192.0)
        sunk = seacarve.sink(z)
        for k, r in enumerate(rows):
            r.update(depth=round(float(d[k]), 2), level=None if not np.isfinite(lv[k]) else round(float(lv[k]), 2),
                     carved=round(float(carved[k]), 2),
                     lake=None if not np.isfinite(lake[k]) else round(float(lake[k]), 2),
                     sea2k=bool(s2[k]), sea8k=bool(s8[k]), sink=round(float(sunk[k]), 2))

    if not args.no_map:
        for r in rows:
            r["water"] = covering_water(md, r["lat"], r["lon"])
            r["roads"], r["infra"] = near(md, r["lat"], r["lon"], args.radius)

    cols = ["lat", "lon", "dem"] + ([] if args.no_water else ["depth", "level", "carved", "lake", "sea2k", "sea8k",
                                                                "sink"])
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join(str(r[c]) for c in cols))
        if not args.no_map:
            for w in r["water"]:
                print(f"    water: {w['subtype']}/{w['class']} {w['name']!r} sea={w['sea']} lake={w['lake']}")
            for road in r["roads"]:
                print(f"    road: {road}")
            if r["infra"]:
                print(f"    infra: {r['infra']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
