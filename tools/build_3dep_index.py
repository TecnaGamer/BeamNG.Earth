#!/usr/bin/env python3
"""Build server/usgs3dep_index.json.gz: where USGS 3DEP lidar point clouds (Entwine Point Tiles on AWS,
s3://usgs-lidar-public, public domain) cover the US, for the lidar bridge decks (server/roadz.py, BNE_LIDAR_DECKS).

The source is the EPT boundaries list Hobu keeps for the bucket (github.com/hobuinc/usgs-lidar,
boundaries/resources.geojson, ~9 MB, 2,283 datasets in 2026-10); each dataset's footprint is simplified to ~600 m and
kept with its name and year (the newest is read first where several cover a place).

    python3 tools/build_3dep_index.py resources.geojson     # a downloaded copy
    python3 tools/build_3dep_index.py                       # download it first
"""
import gzip
import json
import os
import re
import sys
import urllib.request

URL = "https://raw.githubusercontent.com/hobuinc/usgs-lidar/master/boundaries/resources.geojson"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server", "usgs3dep_index.json.gz")
TOL = 0.006         # deg (~600 m): the footprints' simplification
UA = "BeamNG.Earth-build (+https://github.com/TecnaGamer/BeamNG.Earth)"


def year(name):
    """The dataset's year: a work unit's _B<yy> suffix (CA_LosAngeles_1_B23), else its first 19xx / 20xx."""
    m = re.search(r"_B(\d\d)$", name)
    if m:
        return 2000 + int(m.group(1))
    m = re.search(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)", name)
    return int(m.group(1)) if m else 0


def simplify(pts, tol):
    """Douglas-Peucker of a closed ring [[lon, lat], ...]."""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        ll = (dx * dx + dy * dy) ** 0.5
        best, at = 0.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / ll if ll > 1e-12 else ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            if d > best:
                best, at = d, i
        if at >= 0 and best > tol:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(pts, keep) if k]


def main():
    if len(sys.argv) > 1:
        raw = json.load(open(sys.argv[1]))
    else:
        req = urllib.request.Request(URL, headers={"User-Agent": UA})
        raw = json.loads(urllib.request.urlopen(req, timeout=120).read())
    out = []
    for f in raw["features"]:
        pr, g = f["properties"], f["geometry"]
        if not g or not pr.get("url", "").endswith("/ept.json"):
            continue
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        rings = []
        for poly in polys:
            ring = simplify([[round(x, 4), round(y, 4)] for x, y in poly[0]], TOL)
            # (specks under ~300 m across are left out: the deck reader tries the next dataset, else the inference)
            if len(ring) >= 4 and (max(x for x, _ in ring) - min(x for x, _ in ring) > 0.003
                                   or max(y for _, y in ring) - min(y for _, y in ring) > 0.003):
                rings.append([[round(x, 3), round(y, 3)] for x, y in ring])
        if not rings:
            continue
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        out.append({"n": pr["name"], "y": year(pr["name"]), "c": int(pr.get("count") or 0),
                    "b": [min(xs), min(ys), max(xs), max(ys)], "p": rings})
    out.sort(key=lambda d: d["n"])
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        json.dump({"source": URL, "datasets": out}, fh, separators=(",", ":"))
    print(f"{len(out)} datasets -> {OUT} ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main()
