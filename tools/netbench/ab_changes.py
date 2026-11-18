#!/usr/bin/env python3
"""Per-change traffic, before vs after, summed over the places both runs finished: the hosts/paths each change acts on."""
import json
import os
from urllib.parse import urlsplit
ROOT = os.environ["AB_ROOT"]          # a bench.sh run: ROOT/{before,after}/{net.log,places.jsonl}
# (label, predicate on (host, path, range))
ROWS = (
    ("3DBAG (gzip)", lambda h, p, r: h == "data.3dbag.nl"),
    ("IGN WFS (gzip)", lambda h, p, r: h == "data.geopf.fr" and p.startswith("/wfs")),
    ("Mapterhorn tiles (lidar test by HEAD)", lambda h, p, r: h == "tiles.mapterhorn.com"),
    ("NOAA bathymetry", lambda h, p, r: h == "gis.ngdc.noaa.gov"),
    ("US bridge lidar", lambda h, p, r: h == "s3-us-west-2.amazonaws.com" and p.startswith("/usgs-lidar-public")),
    ("Overture (tiles + parquet)", lambda h, p, r: "overturemaps" in h),
    ("Fuel + picnic tables (pois.parquet)", lambda h, p, r: p.startswith("/layercake/pois")),
    ("Route shields (highways.parquet)", lambda h, p, r: p.startswith("/layercake/highways")),
    ("NAIP", lambda h, p, r: h == "apps.geo.fpac.usda.gov"),
    ("USGS basemap", lambda h, p, r: h == "basemap.nationalmap.gov"),
    ("Tile host DEM packs", lambda h, p, r: h == "tiles.tgforever.net" and p.startswith("/dem")),
)
def recs(run):
    return {r["label"]: r for r in map(json.loads, open(f"{ROOT}/{run}/places.jsonl"))}
rb, ra = recs("before"), recs("after")
places = [p for p in rb if p in ra]
def tally(run, rr):
    lines = open(f"{ROOT}/{run}/net.log").read().splitlines()
    out = {lab: [0, 0] for lab, _ in ROWS}
    for p in places:
        for l in lines[rr[p]["start_line"]:rr[p]["end_line"]]:
            r = l.split(" "); u = urlsplit(r[4]); h, path = u.hostname or "", u.path
            for lab, f in ROWS:
                if f(h, path, r[5:]):
                    out[lab][0] += int(r[3]); out[lab][1] += 1
    return out
b, a = tally("before", rb), tally("after", ra)
print("places:", places)
for lab, _ in ROWS:
    x, y = b[lab][0] / 1e6, a[lab][0] / 1e6
    print(f"{lab:40} {x:7.1f} -> {y:7.1f} MB ({100 * (y - x) / x if x else 0:+.0f} %)  req {b[lab][1]} -> {a[lab][1]}")
