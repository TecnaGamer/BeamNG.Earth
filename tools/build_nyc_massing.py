#!/usr/bin/env python3
"""New York City's measured building massing (setbacks): the shards server/nycmassing.py reads.

The city's 3-D Building Model (NYC Office of Technology and Innovation, formerly DoITT, 2014: one multipatch per
building, BIN and DOITT_ID, EPSG:2263 US feet; "LoD1.5": every roof level more than 8 ft from its neighbours is a
flat roof of its own, bulkheads, water tanks and towers over 10 ft included, no sloped faces, spires or antennas: a tank
is a box like a bulkhead, often none at all, so no tank is told apart: buildings.WATER_TANK_*'s notes) is
turned into stacked parts: the building's roof levels sorted, and for each band between two levels the outline of
everything at or above the band's top (the union of the roofs that high): a wedding cake of flat prisms, each from the
level below to its own. Bands that set back by less than SETBACK_M2 are one; pieces under MIN_PART_M2 are left out;
courtyards (holes over COURTYARD_M2) are cut open into hole-free pieces (the server's parts are outer rings).

The model is from 2014. Each building is checked against the city's live Building Footprints (NYC Open Data
5zhs-2jue: bin, doitt_id, height_roof in feet, construction_year): gone (no live record), built in 2014 or later, or a
live roof height more than HEIGHT_TOL off every 2014 roof level: left out. A building whose bands collapse to one has
no setbacks, and one under MIN_TOP_M is a house (its pitched roof is the server's to draw): left out.

Shards: one per z14 map cell (server/mapcells.py) of the buildings whose footprint point lies in it,
<OUT>/14/<x>/<y>.npz, plus <OUT>/manifest.json. Coordinates are decimetres in the cell's ENU frame (its centre's
tangent plane, mapcells' frame): per building an interior point (pxy), per part its ring's first corner relative to
that point and then each corner relative to the one before (int16), heights in decimetres above the building's lowest
ground. See server/nycmassing.py (decode) for the arrays.

    python tools/build_nyc_massing.py fetch ROOT            the model's zip and the live footprints' CSV into ROOT
    python tools/build_nyc_massing.py build ROOT OUT [DA12 DA13 ...] [--workers N]
    python tools/build_nyc_massing.py stats OUT             shard sizes per cell

Needs pyogrio (GDAL's OpenFileGDB driver reads the multipatch), shapely 2 and pyproj. The downloads are untrusted
data: keep ROOT apart from the code and run this with python -I. Licence: NYC Open Data terms of use (free use, the
source credited): CREDITS.md.
"""
import csv
import hashlib
import json
import math
import multiprocessing as mp
import os
import sys
import time
import urllib.request
import zipfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

MODEL_URL = "https://s-media.nyc.gov/agencies/oti/DA_WISE_Multipatch.zip"
FOOTPRINTS_URL = ("https://data.cityofnewyork.us/resource/5zhs-2jue.csv?$select=bin,doitt_id,height_roof,"
                  "construction_year,last_status_type&$limit=2000000&$order=objectid")
MODEL_YEAR = 2014
FT = 0.3048                 # m per US survey foot (EPSG:2263's unit; the 2 ppm to the international foot: nothing)
FT2 = FT * FT
FLAT_FT = 0.1               # a face whose corners span less than this in z is horizontal
GROUND_FT = 1.0             # a horizontal face this close to the lowest corner is the building's bottom
LEVEL_SNAP_M = 0.5          # roof levels closer than this are one (the highest of them)
MIN_PART_M2 = 25.0          # a band's piece smaller than this is left out (an antenna's base, a chimney)
SETBACK_M2 = 25.0           # a band that sets back by less than this (and SETBACK_SHARE of its area) joins the next
SETBACK_SHARE = 0.03
COURTYARD_M2 = 25.0         # a hole in a band's outline smaller than this is filled
MAX_BANDS = 12              # more bands than this: the pairs that differ least joined
SIMPLIFY_FT = 0.5           # outline simplification (~15 cm)
GRID_FT = 0.05              # the unions' precision grid (~1.5 cm): adjacent roof faces meet without slivers
HEIGHT_TOL = 0.15           # a live roof height off every 2014 level by more than this share: rebuilt or changed
HEIGHT_TOL_M = 2.0          # ... and by more than this
MIN_TOP_M = 12.0            # a building lower than this is left out: a house and its porch or garage (the model's
                            # flat roofs would replace the pitched roof the server gives a house; setbacks are a
                            # taller building's)


# --- the roof levels and their bands ------------------------------------------------------------------------------
def snap_levels(hs, snap=LEVEL_SNAP_M):
    """The distinct levels of heights hs (ascending), those within snap of the one before joined (the cluster's
    highest). Pure."""
    out = []
    for h in sorted(float(v) for v in hs):
        if out and h - out[-1] < snap:
            out[-1] = h
        else:
            out.append(h)
    return out


def level_index(h, levels, snap=LEVEL_SNAP_M):
    """The index of the level h belongs to (the lowest level not under h - snap)."""
    for i, lv in enumerate(levels):
        if h <= lv + 1e-6:
            return i
    return len(levels) - 1


def merge_plan(areas, setback_m2=SETBACK_M2, share=SETBACK_SHARE, max_bands=MAX_BANDS):
    """Which bands stay, from the outline area at each level's top (ascending levels, areas never growing upward):
    a band whose outline the next one keeps all but setback_m2 (and share) of joins it (the joined band keeps the lower
    outline and takes the higher top); then, over max_bands, the neighbours that differ least are joined. Returns the
    kept band indices (each kept band k is drawn with outline k up to the top of the last band it absorbed: the next
    kept index - 1). Pure."""
    n = len(areas)
    keep = list(range(n))
    i = 0
    while i < len(keep) - 1:
        a, b = areas[keep[i]], areas[keep[i + 1]]
        if a - b < max(setback_m2, share * a):
            del keep[i + 1]             # (band i reaches up to band i+1's top with its own outline)
        else:
            i += 1
    while len(keep) > max_bands:
        d = [areas[keep[i]] - areas[keep[i + 1]] for i in range(len(keep) - 1)]
        del keep[int(np.argmin(d)) + 1]
    return keep


def bands_of(faces, min_part_m2=MIN_PART_M2, courtyard_m2=COURTYARD_M2):
    """The stacked parts of one building from its roof faces [(height m above its base, shapely Polygon in feet)]:
    [(bottom m, top m, [Polygon without holes, feet])], bottom to top. The outline of a band is the union of every
    face at or above its top. [] when it has a single band (no setback) or nothing left."""
    import shapely
    if not faces:
        return []
    levels = snap_levels([h for h, _p in faces])
    by = [[] for _ in levels]
    for h, p in faces:
        by[level_index(h, levels)].append(p)
    # (from the top down: the union at level k is the one at k + 1 and the faces at k)
    outl = [None] * len(levels)
    acc = None
    for k in range(len(levels) - 1, -1, -1):
        geoms = by[k] + ([acc] if acc is not None else [])
        acc = shapely.union_all(geoms, grid_size=GRID_FT) if len(geoms) > 1 else shapely.set_precision(geoms[0], GRID_FT)
        outl[k] = acc
    cleaned = [_clean(o, min_part_m2, courtyard_m2) for o in outl]
    areas = [sum(p.area for p in c) * FT2 for c in cleaned]
    # (levels whose outline is gone entirely: an antenna's base under MIN_PART_M2; their tops dropped)
    live = [k for k in range(len(levels)) if areas[k] > 0]
    if len(live) < 2:
        return []
    kept = merge_plan([areas[k] for k in live])
    if len(kept) < 2:
        return []
    out = []
    bottom = 0.0
    for j, ki in enumerate(kept):
        last = kept[j + 1] - 1 if j + 1 < len(kept) else len(live) - 1
        top = levels[live[last]]
        out.append((bottom, top, cleaned[live[ki]]))
        bottom = top
    return out


def _clean(geom, min_part_m2, courtyard_m2):
    """[Polygon without holes] of a union: simplified, pieces under min_part_m2 out, small holes filled, the larger
    cut open (a courtyard)."""
    import shapely
    from shapely.geometry import Polygon
    g = shapely.simplify(geom, SIMPLIFY_FT, preserve_topology=True)
    out = []
    for p in getattr(g, "geoms", [g]):
        if not isinstance(p, Polygon) or p.is_empty or p.area * FT2 < min_part_m2:
            continue
        holes = [r for r in p.interiors if Polygon(r).area * FT2 >= courtyard_m2]
        p = Polygon(p.exterior, holes)
        out += [q for q in _split_holes(p) if q.area * FT2 >= 1.0]
    return out


def _split_holes(p, depth=0):
    """p (a Polygon) cut into pieces without holes: through each hole's centre, north to south, recursively."""
    import shapely
    from shapely.geometry import Polygon
    if not p.interiors:
        return [Polygon(p.exterior).normalize()]
    if depth > 8:
        return [Polygon(p.exterior).normalize()]
    x = Polygon(p.interiors[0]).centroid.x
    y0, y1 = p.bounds[1] - 1.0, p.bounds[3] + 1.0
    left = shapely.box(p.bounds[0] - 1.0, y0, x, y1)
    right = shapely.box(x, y0, p.bounds[2] + 1.0, y1)
    out = []
    for half in (left, right):
        g = shapely.intersection(p, half, grid_size=GRID_FT)
        for q in getattr(g, "geoms", [g]):
            if isinstance(q, Polygon) and not q.is_empty:
                out += _split_holes(q, depth + 1)
    return out


def faces_of(geom_wkb):
    """(zmin ft, [(height m above zmin, 2-D Polygon in feet)]) of a multipatch's horizontal faces over its bottom."""
    import shapely
    s = shapely.from_wkb(geom_wkb)
    if s is None or s.is_empty:
        return None, []
    zall = shapely.get_coordinates(s, include_z=True)[:, 2]
    zmin = float(np.min(zall))
    out = []
    for p in getattr(s, "geoms", [s]):
        c = shapely.get_coordinates(p, include_z=True)
        if len(c) < 4 or np.ptp(c[:, 2]) >= FLAT_FT:
            continue
        z = float(c[0, 2])
        if z - zmin < GROUND_FT:
            continue
        q = shapely.make_valid(shapely.force_2d(p))
        if q.is_empty or q.area <= 0:
            continue
        out.append(((z - zmin) * FT, q))
    return zmin, out


# --- the live footprints ------------------------------------------------------------------------------------------
def load_live(path):
    """{doitt id: (bin, height ft, year)} and {bin: [(height ft, year)]} of the live footprints' CSV."""
    by_doitt, by_bin = {}, {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if (r.get("last_status_type") or "Constructed") not in ("Constructed", "Alteration", ""):
                continue
            try:
                b = int(r.get("bin") or 0)
            except ValueError:
                b = 0
            try:
                h = float(r.get("height_roof") or "nan")
            except ValueError:
                h = float("nan")
            try:
                y = int(float(r.get("construction_year") or 0))
            except ValueError:
                y = 0
            d = r.get("doitt_id")
            if d and d.isdigit():
                by_doitt[int(d)] = (b, h, y)
            if b and b % 1000000:
                by_bin.setdefault(b, []).append((h, y))
    return by_doitt, by_bin


def live_check(bin_, doitt, levels_m, by_doitt, by_bin):
    """Why a 2014 building is left out ("gone", "new", "height"), or None (still standing as modelled)."""
    rows = []
    got = by_doitt.get(int(doitt)) if doitt else None
    if got is not None and (not bin_ or not got[0] or got[0] == bin_ or not got[0] % 1000000 or not bin_ % 1000000):
        rows = [(got[1], got[2])]
    elif bin_ and bin_ % 1000000:
        rows = by_bin.get(int(bin_), [])
    if not rows:
        return "gone"
    if any(y >= MODEL_YEAR for _h, y in rows):
        return "new"
    hs = [h * FT for h, _y in rows if math.isfinite(h) and h > 0]
    if hs and levels_m:
        for h in hs:
            if any(abs(h - lv) <= max(HEIGHT_TOL * max(h, lv), HEIGHT_TOL_M) for lv in levels_m):
                return None
        return "height"
    return None


# --- the shards ---------------------------------------------------------------------------------------------------
def encode(recs):
    """The arrays of a shard from its records [{bin, pxy (2,) dm, area m2, top m, parts [(bottom m, top m, ring
    (k, 2) dm, open)]}] (see server/nycmassing.decode)."""
    B = len(recs)
    bins = np.zeros(B, np.int32)
    pxy = np.zeros((B, 2), np.int32)
    area = np.zeros(B, np.float32)
    top = np.zeros(B, np.uint16)
    nparts = np.zeros(B, np.uint16)
    pbot, ptop, pnv, xy = [], [], [], []
    for i, r in enumerate(recs):
        bins[i] = r["bin"]
        pxy[i] = r["pxy"]
        area[i] = r["area"]
        top[i] = min(65535, int(round(r["top"] * 10)))
        nparts[i] = len(r["parts"])
        for b, t, ring in r["parts"]:
            q = np.asarray(ring, np.int64)
            d = np.diff(np.vstack([np.asarray(r["pxy"], np.int64)[None], q]), axis=0)
            if np.abs(d).max() > 32767:
                raise ValueError("ring too far from its building's point")
            pbot.append(min(65535, int(round(b * 10))))
            ptop.append(min(65535, int(round(t * 10))))
            pnv.append(len(q))
            xy.append(d.astype(np.int16))
    return {"bin": bins, "pxy": pxy, "area": area, "top": top, "nparts": nparts,
            "pbot": np.array(pbot, np.uint16), "ptop": np.array(ptop, np.uint16), "pnv": np.array(pnv, np.uint16),
            "xy": np.concatenate(xy) if xy else np.zeros((0, 2), np.int16)}


def _work(args):
    """Records of one slice of a delivery area's features: (cell key str, record) pairs and the counts."""
    gdb, skip, count, live_path = args
    import pyogrio
    import shapely
    from pyproj import Transformer
    import geodesy
    import mapcells
    global _LIVE
    if _LIVE is None:
        _LIVE = load_live(live_path)
    by_doitt, by_bin = _LIVE
    to_ll = Transformer.from_crs(2263, 4326, always_xy=True)
    _meta, _fids, geoms, fields = pyogrio.raw.read(gdb, skip_features=skip, max_features=count,
                                                   columns=["BIN", "DOITT_ID"])
    bins, doitts = (np.array([_int(v) for v in col], np.int64) for col in fields)
    frames = {}
    out, n = [], {"features": 0, "gone": 0, "new": 0, "height": 0, "flat": 0, "low": 0, "kept": 0, "parts": 0}
    for i, g in enumerate(geoms):
        if g is None:
            continue
        n["features"] += 1
        zmin, faces = faces_of(g)
        if not faces:
            n["flat"] += 1
            continue
        levels = snap_levels([h for h, _p in faces])
        why = live_check(int(bins[i]), int(doitts[i]), levels, by_doitt, by_bin)
        if why is not None:
            n[why] += 1
            continue
        bands = bands_of(faces)
        if not bands or bands[-1][1] < MIN_TOP_M:
            n["flat" if not bands else "low"] += 1
            continue
        base = shapely.union_all(bands[0][2])
        rp = base.representative_point()
        lon, lat = to_ll.transform(rp.x, rp.y)
        key = mapcells.cell_of(lat, lon)
        fr = frames.get(key)
        if fr is None:
            clat, clon = key.center()
            fr = frames[key] = geodesy.EnuFrame(clat, clon, 0.0)

        def dm(xs, ys):
            lo_, la_ = to_ll.transform(np.asarray(xs, np.float64), np.asarray(ys, np.float64))
            e, nn, _u = fr.geodetic_to_enu(np.asarray(la_, np.float64), np.asarray(lo_, np.float64), 0.0)
            return np.round(np.stack([np.atleast_1d(e), np.atleast_1d(nn)], 1) * 10.0).astype(np.int64)

        parts = []
        for b, t, polys in bands:
            for p in polys:
                c = np.asarray(p.exterior.coords)[:-1]
                if len(c) < 3:
                    continue
                parts.append((b, t, dm(c[:, 0], c[:, 1])))
        out.append((str(key), {"bin": int(bins[i]), "doitt": int(doitts[i]), "pxy": dm([rp.x], [rp.y])[0],
                               "area": float(base.area * FT2), "top": float(bands[-1][1]), "parts": parts}))
        n["kept"] += 1
        n["parts"] += len(parts)
    return out, n


_LIVE = None


def _int(v):
    """An id field's value as an int (0: empty; some delivery areas have the fields as nullable reals)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0
    return int(v) if math.isfinite(v) else 0


def build(root, out_dir, das, workers):
    import pyogrio
    live = os.path.join(root, "footprints.csv")
    gdbs = []
    for da in das:
        gdb = extract(root, da)
        total = pyogrio.read_info(gdb)["features"]
        step = 1500
        gdbs += [(gdb, s, step, live) for s in range(0, total, step)]
    t0 = time.time()
    global _LIVE
    _LIVE = load_live(live)          # (read once here: the workers fork with it)
    cells, counts, seen = {}, {}, set()
    with mp.get_context("fork").Pool(workers) as pool:
        for recs, n in pool.imap_unordered(_work, gdbs):
            for k, v in n.items():
                counts[k] = counts.get(k, 0) + v
            for key, r in recs:
                if r["doitt"] and r["doitt"] in seen:     # (a building in two delivery areas: once)
                    counts["dupes"] = counts.get("dupes", 0) + 1
                    continue
                seen.add(r["doitt"])
                cells.setdefault(key, []).append(r)
    print("records", counts, round(time.time() - t0), "s", flush=True)
    manifest = {"format": 1, "source": "NYC OTI 3-D Building Model (2014), NYC Building Footprints (live)",
                "model_url": MODEL_URL, "built": time.strftime("%Y-%m-%d"), "delivery_areas": list(das),
                "counts": counts, "cells": {}}
    total = 0
    for key, recs in sorted(cells.items()):
        recs.sort(key=lambda r: (r["pxy"][1], r["pxy"][0]))
        z, x, y = key.split("/")
        d = os.path.join(out_dir, z, x)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{y}.npz")
        np.savez_compressed(p, **encode(recs))
        size = os.path.getsize(p)
        total += size
        # (sha256: the server reads only the shard its manifest names, nycshards.matches: no older or newer build's)
        manifest["cells"][key] = {"buildings": len(recs), "parts": sum(len(r["parts"]) for r in recs), "bytes": size,
                                  "sha256": hashlib.sha256(open(p, "rb").read()).hexdigest()}
    lats, lons = [], []
    import mapcells
    for key in cells:
        lo0, la0, lo1, la1 = mapcells.parse(key).bounds()
        lats += [la0, la1]
        lons += [lo0, lo1]
    if lats:
        manifest["box"] = [min(lats), min(lons), max(lats), max(lons)]
    manifest["bytes"] = total
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    print("cells", len(cells), "MB", round(total / 1e6, 2), flush=True)


def extract(root, da):
    """The path of delivery area da's GDB (DA12 ...), taken out of the zip once (only its own files)."""
    name = f"{da}_3D_Buildings_Multipatch.gdb"
    dest = os.path.join(root, "gdb")
    gdb = os.path.join(dest, "DA_WISE_Multipatch", name)
    if os.path.isdir(gdb):
        return gdb
    with zipfile.ZipFile(os.path.join(root, "DA_WISE_Multipatch.zip")) as z:
        for m in z.namelist():
            parts = m.split("/")
            if len(parts) == 3 and parts[0] == "DA_WISE_Multipatch" and parts[1] == name and parts[2] \
                    and ".." not in parts and not os.path.isabs(m):
                z.extract(m, dest)
    return gdb


def fetch(root):
    os.makedirs(root, exist_ok=True)
    for url, name in ((MODEL_URL, "DA_WISE_Multipatch.zip"), (FOOTPRINTS_URL, "footprints.csv")):
        p = os.path.join(root, name)
        if os.path.exists(p):
            continue
        req = urllib.request.Request(url, headers={"User-Agent": "BeamNG.Earth data builder"})
        with urllib.request.urlopen(req, timeout=600) as r, open(p + ".part", "wb") as fh:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                fh.write(b)
        os.replace(p + ".part", p)
        print(name, os.path.getsize(p), flush=True)


def stats(out_dir):
    m = json.load(open(os.path.join(out_dir, "manifest.json")))
    cells = sorted(m["cells"].items(), key=lambda kv: -kv[1]["bytes"])
    for key, c in cells[:10]:
        print(key, c)
    print("cells", len(cells), "MB", round(m["bytes"] / 1e6, 2), "counts", m["counts"])


def main(argv):
    if len(argv) >= 2 and argv[0] == "fetch":
        fetch(argv[1])
    elif len(argv) >= 3 and argv[0] == "build":
        workers = max(1, (os.cpu_count() or 2) - 2)
        rest = list(argv[3:])
        if "--workers" in rest:
            j = rest.index("--workers")
            workers = int(rest[j + 1])
            del rest[j:j + 2]
        das = rest or [f"DA{i}" for i in range(1, 21)]
        build(argv[1], argv[2], das, workers)
    elif len(argv) >= 2 and argv[0] == "stats":
        stats(argv[1])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
