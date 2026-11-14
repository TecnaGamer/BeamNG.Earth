#!/usr/bin/env python3
"""build_tall_shards.py OUT [--center LAT,LON,KM ...] [--min-h 60]: the tall-building index of the horizon ring
(server horizon.py, BNE_BLDHZ_DIR): every Overture building and building part of the release at least MIN_H tall
(a building with no height but NUM_FLOORS floors or more counts at FLOOR_M a floor), in shards of the zoom-7 Web
Mercator tile of its middle, written as OUT/7/<x>/<y>.json.gz plus OUT/manifest.json.

Why prebaked (2026-11-14 measurements, release 2026-10-21.0): a 40 km radius of tall buildings from the sources costs
Manhattan 210 MB of z14 PMTiles (every building; the lower zooms carry no heights) or 84 MB of GeoParquet row groups
(one tower makes its ~45k-building row group pass, its whole geometry chunk read), Columbus 72 / 11 MB, Frankfurt
114 / 39 MB; a shard is ~200 KB for Manhattan's ~8.7k records at >= 60 m, ~2 KB for Columbus. A worldwide build reads
~17 GB of ranges (>= 60 m) once per release. Not published anywhere by this tool: OUT is a local folder (publishing to
the data repo / R2 needs the user's go).

A record: {"id", "c": [lat, lon] (the outline's middle), "r": [[lat, lon], ...] (the outer ring, open, at most
MAX_CORNERS corners: every k-th one; the server simplifies further as the skyline does), "t": {the Overture columns the
buildings' plan() reads}, "part": 1 for a building part}. A building with parts (has_parts) is left out when one of its
parts is in (the parts draw it, as the skyline's SKY_PARTS). Records sorted by id; the shard's bytes deterministic
(json with sorted keys, gzip mtime 0).

    python3 tools/build_tall_shards.py ~/bnge-tests/tall --center 40.754,-73.984,45 --center 39.9612,-82.9988,45
"""
import argparse
import gzip
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

FORMAT = 1                  # the shards' layout (server horizon.TALL_FORMAT reads only its own)
SHARD_Z = 7
MAX_CORNERS = 24
FLOOR_M = 3.4               # m a floor of a building mapped by floors alone
COLUMNS = ("height", "min_height", "num_floors", "roof_shape", "roof_height", "facade_color", "roof_color",
           "facade_material", "roof_material", "class")


def tile_of(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


def box(lat, lon, km):
    dl = km / 111.2
    dx = km / (111.32 * math.cos(math.radians(lat)))
    return lon - dx, lat - dl, lon + dx, lat + dl


def files(stac, typ, b):
    c = stac["collections"][f"buildings/{typ}"]["files"]
    return [u[0] for _, bb, u, _ in c if bb[0] <= b[2] and bb[2] >= b[0] and bb[1] <= b[3] and bb[3] >= b[1]]


def ring_of(wkb):
    """The largest polygon's outer ring [(lon, lat), ...], open, or None."""
    import mapgeom
    kind, parts = mapgeom.parse_wkb(wkb)
    best, area = None, -1.0
    for rings in parts:
        if not rings:
            continue
        r = [(float(x), float(y)) for x, y in rings[0]]
        if len(r) > 1 and r[0] == r[-1]:
            r = r[:-1]
        if len(r) < 3:
            continue
        a = abs(sum(r[i][0] * r[(i + 1) % len(r)][1] - r[(i + 1) % len(r)][0] * r[i][1] for i in range(len(r))))
        if a > area:
            best, area = r, a
    if best is None:
        return None
    if len(best) > MAX_CORNERS:
        k = int(math.ceil(len(best) / MAX_CORNERS))
        best = best[::k]
    return best


def query(con, urls, b, where):
    lst = "[" + ",".join(f"'{u}'" for u in urls) + "]"
    bb = f"bbox.xmin <= {b[2]} and bbox.xmax >= {b[0]} and bbox.ymin <= {b[3]} and bbox.ymax >= {b[1]}"
    return con.execute(f"select id, geometry, {', '.join(COLUMNS)}, {{extra}} from read_parquet({lst}) "
                       f"where {bb} and ({where})".replace("{extra}", "{}")).fetchall() if False else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--center", action="append", default=[], help="LAT,LON,KM (a square of that half size)")
    ap.add_argument("--min-h", type=float, default=60.0)
    ap.add_argument("--floors", type=int, default=18, help="a building without a height counts from this many floors")
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args()
    import duckdb
    import overture
    stac = json.load(gzip.open(os.path.join(HERE, "..", "server", f"overture_stac_{overture.RELEASE}.json.gz")))
    con = duckdb.connect()
    con.execute("LOAD httpfs")
    con.execute(f"SET threads={int(a.threads)}")
    cols = ", ".join(COLUMNS)
    recs = {}
    t0 = time.time()
    for c in a.center:
        lat, lon, km = (float(v) for v in c.split(","))
        b = box(lat, lon, km)
        bb = f"bbox.xmin <= {b[2]} and bbox.xmax >= {b[0]} and bbox.ymin <= {b[3]} and bbox.ymax >= {b[1]}"
        fb, fp = files(stac, "building", b), files(stac, "building_part", b)
        lst = lambda fs: "[" + ",".join(f"'{u}'" for u in fs) + "]"   # noqa: E731
        parts = con.execute(f"select id, geometry, {cols}, building_id from read_parquet({lst(fp)}) where {bb} and "
                            f"height >= {a.min_h}").fetchall() if fp else []
        blds = con.execute(f"select id, geometry, {cols}, has_parts from read_parquet({lst(fb)}) where {bb} and "
                           f"(height >= {a.min_h} or (height is null and num_floors >= {int(a.floors)}))").fetchall()
        with_parts = {r[-1] for r in parts}
        for row, is_part in [(r, True) for r in parts] + [(r, False) for r in blds]:
            rid = row[0]
            if not is_part and row[-1] and rid in with_parts:
                continue                # (its tall parts draw it)
            ring = ring_of(row[1])
            if ring is None:
                continue
            t = {k: v for k, v in zip(COLUMNS, row[2:2 + len(COLUMNS)]) if v is not None}
            if t.get("height") is None:
                t["height"] = round(float(t["num_floors"]) * FLOOR_M, 1)
            for k in ("height", "min_height", "roof_height"):
                if k in t:
                    t[k] = round(float(t[k]), 2)
            if "num_floors" in t:
                t["num_floors"] = int(t["num_floors"])
            clon = sum(p[0] for p in ring) / len(ring)
            clat = sum(p[1] for p in ring) / len(ring)
            rec = {"id": rid, "c": [round(clat, 7), round(clon, 7)],
                   "r": [[round(y, 7), round(x, 7)] for x, y in ring], "t": t}
            if is_part:
                rec["part"] = 1
            recs[rid] = rec
        print(f"{c}: {len(parts)} parts, {len(blds)} buildings, {time.time() - t0:.1f} s", flush=True)
    shards = {}
    for rid in sorted(recs):
        r = recs[rid]
        shards.setdefault(tile_of(r["c"][0], r["c"][1], SHARD_Z), []).append(r)
    for (x, y), rs in sorted(shards.items()):
        d = os.path.join(a.out, str(SHARD_Z), str(x))
        os.makedirs(d, exist_ok=True)
        body = json.dumps({"format": FORMAT, "release": overture.RELEASE, "min_h": a.min_h, "b": rs},
                          sort_keys=True, separators=(",", ":")).encode()
        with open(os.path.join(d, f"{y}.json.gz"), "wb") as f:
            with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
                g.write(body)
    man = {"format": FORMAT, "release": overture.RELEASE, "min_h": a.min_h, "z": SHARD_Z,
           "shards": sorted(f"{x}/{y}" for x, y in shards), "records": len(recs),
           "areas": sorted(a.center)}
    with open(os.path.join(a.out, "manifest.json"), "w") as f:
        json.dump(man, f, sort_keys=True, indent=1)
    print(f"{len(recs)} records in {len(shards)} shards -> {a.out} ({time.time() - t0:.1f} s)")


if __name__ == "__main__":
    main()
