#!/usr/bin/env python3
"""New York City's rooftop water tanks found on the aerial photo: the shards server/nyctanks.py reads.

The city's buildings of MIN_H or more (NYC Open Data 5zhs-2jue Building Footprints: the roof outlines and height_roof;
~108,000 of 12 m or more: Manhattan 39 k, Brooklyn 43 k, the Bronx 12 k, Queens 11 k, Staten Island 2 k) are covered
by square chips CHIP_M across (MARGIN_M more on each side, so a tank on a chip's edge is seen whole); each chip's
picture is the NYS ITS 2024 orthophoto (orthos.its.ny.gov, the server's "newyork" regional source) at MPP, fetched
through the server's netfetch (its host limits, the project's User-Agent) and kept or not (--keep-images);
tools/tankdetect.py finds the tanks on the chip's roofs. The points of all chips (overlaps deduplicated, DEDUP_M) go to
a shard per z14 map cell (server/mapcells.py) of the cells whose chips were all scanned: <OUT>/14/<x>/<y>.npz (an empty
cell too: the photo says none there), plus <OUT>/manifest.json (the cells; server/nyctanks.py asks for nothing a
manifest does not list).

Measured 2026-11-10 against hand marks of 15 areas of 250 x 250 m (Upper West Side, SoHo, Lower East Side, Midtown,
Park Slope, Chelsea, Washington Heights, the Bronx, Brooklyn Heights, Jackson Heights, Yorkville, Murray Hill, Hamilton
Heights, Crown Heights, Financial District; 104 tanks marked): precision 0.95, recall 0.73 (76 found, 4 false, 28
missed: grey or dark tops in their platform's shadow, metal tops the rule leaves for safety, tanks on a chip's edge).
The logistic weights were fitted on 11 of the areas; on the other 4 (30 tanks): precision 0.96, recall 0.73.

    python tools/build_nyc_tanks.py footprints ROOT [--box S,W,N,E]   the tall buildings' footprints (GeoJSON) into ROOT
    python tools/build_nyc_tanks.py plan ROOT [--box S,W,N,E]    the chips over them -> ROOT/chips.json (count, MB)
    python tools/build_nyc_tanks.py scan ROOT [--box ...] [--limit N] [--workers N] [--keep-images]
    python tools/build_nyc_tanks.py build ROOT OUT [--partial]   the shards and the manifest
    python tools/build_nyc_tanks.py stats OUT

Costs (estimated for the five boroughs from the 108 k footprints; `plan` counts them exactly): ~3,000-4,500 chips of
270 m at 0.1 m (2,700 px), ~1.8 MB each (measured): ~5.5-8 GB of imagery, one request a chip (netfetch's default host
policy); detection ~25 s a chip on a free core, 49-63 s with 4 workers on a busy PC (8 Chelsea chips, 2026-11-10):
~25-35 core-hours, run with --workers. The output is small (a Chelsea cell: 61 tanks in 1.3 kB; the city ~0.2-0.4 MB).
The downloads are untrusted data: keep ROOT apart from the code and run this with python -I. Licences: the NYS
orthoimagery (NYS ITS Geospatial Services, public), NYC Open Data terms of use (free use, the source credited):
CREDITS.md."""
import io
import json
import math
import multiprocessing as mp
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))
sys.path.insert(0, HERE)

FOOTPRINTS_URL = ("https://data.cityofnewyork.us/resource/5zhs-2jue.geojson?$select=bin,height_roof,the_geom"
                  "&$where=height_roof>={ft}{box}&$order=objectid&$limit={limit}&$offset={offset}")
ORTHO_URL = ("https://orthos.its.ny.gov/arcgis/rest/services/wms/Latest/MapServer/export?bbox={bbox}&bboxSR=3857"
             "&imageSR=3857&size={px},{px}&format=jpg&f=image")
BOX = (40.49, -74.27, 40.92, -73.68)    # the five boroughs
FT = 0.3048
MIN_H = 12.0                # m: the buildings scanned (tankdetect.MIN_H: a tank's roof)
CHIP_M = 250.0              # m: a chip's own square (its picture MARGIN_M more on each side)
MARGIN_M = 10.0
MPP = 0.1
PAGE = 50000
DEDUP_M = 2.0               # m: two chips' points closer than this are one tank
R_EARTH = 6378137.0


def merc(lat, lon):
    return R_EARTH * math.radians(lon), R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def unmerc(x, y):
    return math.degrees(2 * math.atan(math.exp(y / R_EARTH)) - math.pi / 2), math.degrees(x / R_EARTH)


def fetcher(root):
    import netfetch
    return netfetch.Fetcher(stats_dir=os.path.join(root, "netstats"))


# --- the footprints -------------------------------------------------------------------------------------------------
def footprints(root, box=None, min_h=MIN_H):
    """The footprints of MIN_H or more (in box: south, west, north, east) as GeoJSON pages in ROOT/footprints."""
    d = os.path.join(root, "footprints")
    where = "" if not box else f"%20AND%20within_box(the_geom,{box[2]},{box[1]},{box[0]},{box[3]})"
    os.makedirs(d, exist_ok=True)
    f = fetcher(root)
    offset = 0
    while True:
        p = os.path.join(d, f"page_{offset // PAGE:03d}.geojson")
        if not os.path.exists(p):
            body = f.get(FOOTPRINTS_URL.format(ft=round(min_h / FT, 1), box=where, limit=PAGE, offset=offset)).body
            with open(p + ".part", "wb") as fh:
                fh.write(body)
            os.replace(p + ".part", p)
        n = len(json.load(open(p))["features"])
        print(os.path.basename(p), n, flush=True)
        if n < PAGE:
            break
        offset += PAGE


def load_footprints(root):
    """[(rings [(k, 2) lon, lat], height m)] of the tall buildings."""
    out = []
    d = os.path.join(root, "footprints")
    for name in sorted(os.listdir(d)):
        if not name.endswith(".geojson"):
            continue
        for ft in json.load(open(os.path.join(d, name)))["features"]:
            g, p = ft.get("geometry"), ft.get("properties") or {}
            try:
                h = float(p.get("height_roof") or 0) * FT
            except (TypeError, ValueError):
                continue
            if g is None or g.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
            rings = [np.asarray(poly[0], np.float64)[:-1] for poly in polys if len(poly[0]) >= 4]
            if rings:
                out.append((rings, h))
    return out


# --- the chips ------------------------------------------------------------------------------------------------------
def chip_grid(lat):
    """The chips' size in degrees at latitude lat (a grid over BOX: chip (i, j) south-west corner BOX + (i, j) steps)."""
    return CHIP_M / 110540.0, CHIP_M / (111320.0 * math.cos(math.radians(lat)))


def chip_of(lat, lon):
    dlat, dlon = chip_grid(0.5 * (BOX[0] + BOX[2]))
    return int((lat - BOX[0]) // dlat), int((lon - BOX[1]) // dlon)


def chip_box(i, j):
    """(south, west, north, east) of chip (i, j)'s own square."""
    dlat, dlon = chip_grid(0.5 * (BOX[0] + BOX[2]))
    return BOX[0] + i * dlat, BOX[1] + j * dlon, BOX[0] + (i + 1) * dlat, BOX[1] + (j + 1) * dlon


def plan(root, box=None, min_h=MIN_H):
    blds = load_footprints(root)
    chips = {}
    for k, (rings, h) in enumerate(blds):
        if h < min_h:
            continue
        allp = np.vstack(rings)
        lo0, la0 = allp.min(0)
        lo1, la1 = allp.max(0)
        if box and (la1 < box[0] or la0 > box[2] or lo1 < box[1] or lo0 > box[3]):
            continue
        i0, j0 = chip_of(la0, lo0)
        i1, j1 = chip_of(la1, lo1)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                chips.setdefault(f"{i}_{j}", []).append(k)
    out = {"min_h": min_h, "chip_m": CHIP_M, "margin_m": MARGIN_M, "mpp": MPP, "box": box,
           "chips": {c: len(v) for c, v in sorted(chips.items())}}
    with open(os.path.join(root, "chips.json"), "w") as fh:
        json.dump(out, fh)
    px = int(round((CHIP_M + 2 * MARGIN_M) / MPP))
    print(f"buildings {len(blds)}, chips {len(chips)} ({px} px each), ~{len(chips) * 1.8 / 1000:.1f} GB of imagery, "
          f"~{len(chips) * 30 / 3600:.0f} core-hours", flush=True)


def chip_request(i, j):
    """The chip's picture: (url, its Web Mercator box, px)."""
    s, w, n, e = chip_box(i, j)
    lat = 0.5 * (s + n)
    k = 1.0 / math.cos(math.radians(lat))           # (Web Mercator metres per ground metre)
    x0, y0 = merc(s, w)
    x1, y1 = merc(n, e)
    m = MARGIN_M * k
    x0, y0, x1, y1 = x0 - m, y0 - m, x1 + m, y1 + m
    px = int(round((x1 - x0) / k / MPP))
    return ORTHO_URL.format(bbox=f"{x0},{y0},{x1},{y1}", px=px), (x0, y0, x1, y1), px


def to_px(mbox, px, lon, lat):
    x0, y0, x1, y1 = mbox
    x = R_EARTH * np.radians(lon)
    y = R_EARTH * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
    return np.stack([(x - x0) / (x1 - x0) * px, (y1 - y) / (y1 - y0) * px], 1)


def from_px(mbox, px, xs, ys):
    x0, y0, x1, y1 = mbox
    mx = x0 + np.asarray(xs) / px * (x1 - x0)
    my = y1 - np.asarray(ys) / px * (y1 - y0)
    lat = np.degrees(2 * np.arctan(np.exp(my / R_EARTH)) - np.pi / 2)
    return lat, np.degrees(mx / R_EARTH)


_BLDS = None


def _detect(job):
    """A chip's tanks: (chip, [{lat, lon, d, kind, p, h}], lean, s)."""
    import tankdetect
    from PIL import Image
    chip, body, mbox, px, bidx = job
    t0 = time.time()
    rgb = np.asarray(Image.open(io.BytesIO(body)).convert("RGB"))
    blds = [([to_px(mbox, px, r[:, 0], r[:, 1]) for r in _BLDS[k][0]], _BLDS[k][1]) for k in bidx]
    dets, v = tankdetect.detect(rgb, blds)
    out = []
    s, w, n, e = chip_box(*map(int, chip.split("_")))
    for d in dets:
        gx, gy = tankdetect.ground_point(d, v)
        lat, lon = from_px(mbox, px, [gx], [gy])
        lat, lon = float(lat[0]), float(lon[0])
        if not (s <= lat < n and w <= lon < e):
            continue                # (the margin's: its own chip has it)
        out.append({"lat": round(lat, 7), "lon": round(lon, 7), "d": round(2 * d["r"] * tankdetect.MPP, 1),
                    "kind": 0 if d["kind"] == "tan" else 1, "p": round(d["p"], 3), "h": round(d["h"], 1)})
    return chip, out, [float(v[0]), float(v[1])], time.time() - t0


def scan(root, box=None, limit=None, workers=1, keep_images=False):
    global _BLDS
    plan_ = json.load(open(os.path.join(root, "chips.json")))
    _BLDS = load_footprints(root)
    # (the buildings of each chip, again: the plan keeps only counts)
    by_chip = {}
    for k, (rings, h) in enumerate(_BLDS):
        allp = np.vstack(rings)
        (lo0, la0), (lo1, la1) = allp.min(0), allp.max(0)
        i0, j0 = chip_of(la0 - 1e-4, lo0 - 1e-4)
        i1, j1 = chip_of(la1 + 1e-4, lo1 + 1e-4)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                by_chip.setdefault(f"{i}_{j}", []).append(k)
    det_dir, img_dir = os.path.join(root, "det"), os.path.join(root, "img")
    os.makedirs(det_dir, exist_ok=True)
    todo = []
    for chip in plan_["chips"]:
        if os.path.exists(os.path.join(det_dir, chip + ".json")):
            continue
        if box:
            s, w, n, e = chip_box(*map(int, chip.split("_")))
            if n < box[0] or s > box[2] or e < box[1] or w > box[3]:
                continue
        todo.append(chip)
    if limit:
        todo = todo[:limit]
    print("chips to scan", len(todo), flush=True)
    f = fetcher(root)

    def jobs():
        for chip in todo:
            url, mbox, px = chip_request(*map(int, chip.split("_")))
            p = os.path.join(img_dir, chip + ".jpg")
            if os.path.exists(p):
                body = open(p, "rb").read()
            else:
                try:
                    body = f.get(url).body
                except Exception as e:      # (a timeout or a refusal: this chip waits for the next pass)
                    print(chip, "skipped:", str(e)[-80:], flush=True)
                    continue
                if keep_images:
                    os.makedirs(img_dir, exist_ok=True)
                    with open(p, "wb") as fh:
                        fh.write(body)
            yield chip, body, mbox, px, by_chip.get(chip, [])

    def save(res):
        chip, tanks, v, secs = res
        with open(os.path.join(det_dir, chip + ".json"), "w") as fh:
            json.dump({"tanks": tanks, "lean": v, "s": round(secs, 1)}, fh)
        print(chip, len(tanks), "tanks", f"{secs:.0f} s", flush=True)

    if workers > 1:                 # (in batches: the pool would take every chip's picture into memory at once)
        with mp.get_context("fork").Pool(workers) as pool:
            batch = []
            for job in jobs():
                batch.append(job)
                if len(batch) >= 2 * workers:
                    for res in pool.imap_unordered(_detect, batch):
                        save(res)
                    batch = []
            for res in pool.imap_unordered(_detect, batch):
                save(res)
    else:
        for job in jobs():
            save(_detect(job))


# --- the shards -----------------------------------------------------------------------------------------------------
def dedup(tanks):
    """Points of neighbouring chips closer than DEDUP_M: the likelier one."""
    tanks = sorted(tanks, key=lambda t: -t["p"])
    keep, grid = [], {}
    for t in tanks:
        y, x = t["lat"] * 110540.0, t["lon"] * 111320.0 * math.cos(math.radians(t["lat"]))
        gk = (int(x // DEDUP_M), int(y // DEDUP_M))
        near = [keep[i] for dx in (-1, 0, 1) for dy in (-1, 0, 1) for i in grid.get((gk[0] + dx, gk[1] + dy), ())]
        if any(math.hypot(x - o[0], y - o[1]) < DEDUP_M for o in near):
            continue
        grid.setdefault(gk, []).append(len(keep))
        keep.append((x, y, t))
    return [t for _x, _y, t in keep]


def build(root, out_dir, partial=False):
    """The shards of the cells whose chips were all scanned; a plan with a box (a part of the city) lists only the cells
    wholly inside it, unless partial (a local test: a cell then says "no tank" where nothing was scanned)."""
    import geodesy
    import mapcells
    import nyctanks
    plan_ = json.load(open(os.path.join(root, "chips.json")))
    det_dir = os.path.join(root, "det")
    tanks, done = [], set()
    for chip in plan_["chips"]:
        p = os.path.join(det_dir, chip + ".json")
        if os.path.exists(p):
            done.add(chip)
            tanks += json.load(open(p))["tanks"]
    tanks = dedup(tanks)
    # the cells whose chips were all scanned (a chip in two cells counts for both)
    need = {}
    for chip in plan_["chips"]:
        s, w, n, e = chip_box(*map(int, chip.split("_")))
        for key in mapcells.cells_for_bbox(s, w, n, e, z=nyctanks.ZOOM):
            need.setdefault(str(key), []).append(chip)
    cells = {k for k, cs in need.items() if all(c in done for c in cs)}
    pb = plan_.get("box")
    if pb and not partial:
        def inside(k):
            lo0, la0, lo1, la1 = mapcells.parse(k).bounds()
            return pb[0] <= la0 and la1 <= pb[2] and pb[1] <= lo0 and lo1 <= pb[3]
        cells = {k for k in cells if inside(k)}
    by = {k: [] for k in cells}
    for t in tanks:
        k = str(mapcells.cell_of(t["lat"], t["lon"], z=nyctanks.ZOOM))
        if k in by:
            by[k].append(t)
    manifest = {"format": 1, "source": "NYS ITS orthoimagery 2024 (orthos.its.ny.gov), NYC Building Footprints",
                "built": time.strftime("%Y-%m-%d"), "min_h": plan_["min_h"], "chips": len(done), "cells": {}}
    total = 0
    for key, ts in sorted(by.items()):
        ck = mapcells.parse(key)
        fr = geodesy.EnuFrame(*ck.center(), 0.0)
        la = np.array([t["lat"] for t in ts], np.float64)
        lo = np.array([t["lon"] for t in ts], np.float64)
        e, n, _ = fr.geodetic_to_enu(la, lo, np.zeros(len(ts)))
        xy = np.round(np.stack([np.atleast_1d(e), np.atleast_1d(n)], 1) * 10.0) if ts else np.zeros((0, 2))
        arrs = nyctanks.encode(xy, [round(t["d"] * 10) for t in ts], [t["kind"] for t in ts],
                               [round(t["h"] * 10) for t in ts])
        d = os.path.join(out_dir, str(ck.z), str(ck.x))
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{ck.y}.npz")
        np.savez_compressed(p, **arrs)
        total += os.path.getsize(p)
        manifest["cells"][key] = {"tanks": len(ts), "bytes": os.path.getsize(p)}
    os.makedirs(out_dir, exist_ok=True)
    manifest["tanks"] = sum(len(v) for v in by.values())
    manifest["bytes"] = total
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    print("cells", len(by), "tanks", manifest["tanks"], "kB", round(total / 1e3, 1), flush=True)


def stats(out_dir):
    m = json.load(open(os.path.join(out_dir, "manifest.json")))
    for key, c in sorted(m["cells"].items(), key=lambda kv: -kv[1]["tanks"])[:10]:
        print(key, c)
    print("cells", len(m["cells"]), "tanks", m["tanks"], "kB", round(m["bytes"] / 1e3, 1))


def _box(rest):
    if "--box" in rest:
        j = rest.index("--box")
        v = tuple(float(x) for x in rest[j + 1].split(","))
        del rest[j:j + 2]
        return v
    return None


def _opt(rest, name, default, cast=int):
    if name in rest:
        j = rest.index(name)
        v = cast(rest[j + 1])
        del rest[j:j + 2]
        return v
    return default


def main(argv):
    rest = list(argv[2:])
    if len(argv) >= 2 and argv[0] == "footprints":
        footprints(argv[1], _box(rest))
    elif len(argv) >= 2 and argv[0] == "plan":
        plan(argv[1], _box(rest))
    elif len(argv) >= 2 and argv[0] == "scan":
        box = _box(rest)
        keep = "--keep-images" in rest
        scan(argv[1], box, _opt(rest, "--limit", None), _opt(rest, "--workers", 1), keep)
    elif len(argv) >= 3 and argv[0] == "build":
        build(argv[1], argv[2], partial="--partial" in rest)
    elif len(argv) >= 2 and argv[0] == "stats":
        stats(argv[1])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
