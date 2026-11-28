#!/usr/bin/env python3
"""roadsys_shadow.py: the BNE_ROADSYS=shadow product (server/roadsys.py, native/roadsys) for every geotile of a box, in
one process, for the M1 measurements: per-tile timings, read lists, checks, canonical byte hashes, seams and keys.

    python3 tools/roadsys_shadow.py --cache SCRATCH_CACHE --lat LAT --lon LON --half M --out OUT.json
                                    [--reverse] [--online] [--clear-each] [--server-dir TREE/server]

The cache must be a scratch folder (never the player's ~/.cache/beamng-earth): cells and DEM tiles it lacks are fetched
into it only with --online. --reverse builds the tiles in the opposite order; --clear-each empties the profile solve
store before each tile (every tile cold, as a fresh process would build it). Run it twice (orders, processes,
platforms) and compare the outputs with --compare A.json B.json: tile hashes, piece keys and seams must be equal.

OSM's mapped road areas (server/osmareas.py): BNE_ROADSYS_AREAS=audit adds each tile's "areas" (the faces against
the mapped area:highway polygons: edge distance, IoU, width error, splits), =on also fits the carriageway edges to them
("areas_fit"); BNE_OSMAREAS_DIR names a staged build on disk (tools/build_osm_road_areas.py's output)."""
import argparse
import json
import os
import sys
import time


def compare(a, b):
    A, B = (json.load(open(p)) for p in (a, b))
    ta, tb = A["tiles"], B["tiles"]
    common = sorted(set(ta) & set(tb))
    same_hash = sum(ta[k]["tile_hash"] == tb[k]["tile_hash"] for k in common)
    same_keys = sum(ta[k]["keys"] == tb[k]["keys"] for k in common)
    print(f"{len(common)} tiles in both; hashes equal {same_hash}, piece keys equal {same_keys}")
    for k in common:
        if ta[k]["tile_hash"] != tb[k]["tile_hash"] or ta[k]["keys"] != tb[k]["keys"]:
            print("  differs:", k)
    return 0 if same_hash == same_keys == len(common) == len(ta) == len(tb) else 1


def seams(tiles):
    """Shared sides of neighbouring tiles (east of (x, y) vs west of (x + 1, y); south / north likewise): equal?"""
    import geotiles
    by = {geotiles.parse(k)[1:]: v for k, v in tiles.items()}
    pairs = bad = 0
    for (x, y), v in by.items():
        # the quad's sides: 0 SW-SE (south), 1 SE-NE (east), 2 NE-NW (north), 3 NW-SW (west); y grows southward
        for (dx, dy, mine, theirs) in ((1, 0, 1, 3), (0, 1, 0, 2)):
            o = by.get((x + dx, y + dy))
            if o is None:
                continue
            pairs += 1
            bad += v["seams"][mine] != o["seams"][theirs]
    return pairs, bad


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--half", type=float, default=640.0)
    ap.add_argument("--out")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--clear-each", action="store_true")
    ap.add_argument("--country")
    ap.add_argument("--server-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
    ap.add_argument("--compare", nargs=2)
    ap.add_argument("--tables", help="a folder keeping each cell's segment table between runs (measurements only)")
    ap.add_argument("--only", help="build only this geotile key (g17_X_Y)")
    ap.add_argument("--dump", help="a folder for each tile's whole input (native/roadsys io::write_tile), to build "
                                   "again with `roadsys tile` on another platform")
    a = ap.parse_args()
    sys.path.insert(0, os.path.abspath(a.server_dir))
    if a.compare:
        return compare(*a.compare)
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    cache = os.path.realpath(a.cache)
    if cache == real or cache.startswith(real + os.sep):
        ap.error("a scratch cache only, never the player's")
    import npyguard
    npyguard.apply()
    import numpy as np
    import dem as demmod
    import fast
    import geodesy
    import geotiles
    import mapdata
    import netfetch
    import roadsys
    fetcher = netfetch.Fetcher(offline=not a.online, stats_dir=os.path.join(cache, "netstats"))
    md = mapdata.MapData(cache, fetcher=fetcher)
    dem = demmod.Dem(cache, fetcher=fetcher)
    frame = geodesy.EnuFrame(a.lat, a.lon)
    tiles = sorted(set((gx, gy) for gx, gy, _ in geotiles.tiles_near(frame, 0, 0, a.half, exact=True)))
    # only the tiles whose centre lies inside the box (a square of side 2 half)
    inside = []
    for gx, gy in tiles:
        la, lo = geotiles.centre(gx, gy)
        e, n, _ = frame.geodetic_to_enu(la, lo, 0.0)
        if abs(e) <= a.half and abs(n) <= a.half:
            inside.append((gx, gy))
    if a.reverse:
        inside.reverse()
    if a.only:
        inside = [t for t in inside if geotiles.key(*t) == a.only] or [tuple(geotiles.parse(a.only)[1:])]
    cc = roadsys._Cells(md)
    if a.tables:
        # (measurement runs only: each cell's segment table kept on disk between runs of this tool, so a run repeats
        # the kernels on the same read without decoding every cell again; the server never does this)
        import pickle
        os.makedirs(a.tables, exist_ok=True)
        inner = cc.table

        def table(key):
            path = os.path.join(a.tables, str(key).replace("/", "_") + ".pkl")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return pickle.load(f)
            tab = inner(key)
            with open(path + ".tmp", "wb") as f:
                pickle.dump(tab, f)
            os.replace(path + ".tmp", path)
            return tab
        cc.table = table
    out = {"lat": a.lat, "lon": a.lon, "half": a.half, "reverse": a.reverse, "clear_each": a.clear_each, "tiles": {}}
    t0 = time.perf_counter()
    for gx, gy in inside:
        if a.clear_each:
            fast._ef.roadsys_clear()
        dump = None
        if a.dump:
            os.makedirs(a.dump, exist_ok=True)
            dump = os.path.join(a.dump, f"{gx}_{gy}.rstile")
        rec, data = roadsys.build_tile(gx, gy, md, dem, cc, a.country, dump)
        out["tiles"][rec["tile"]] = rec
        print(rec["tile"], rec["wall_ms"]["total"], "ms", rec["cpu_ms"]["total"], "cpu ms", rec["cells"], "cells",
              rec["rounds"], "rounds", "" if rec["complete"] else "SHORT", rec["tile_hash"], flush=True)
    out["seconds"] = time.perf_counter() - t0
    out["seams"] = seams(out["tiles"])
    with open(a.out, "w") as f:
        json.dump(out, f, sort_keys=True, indent=0)
    print("seams (pairs, mismatched):", out["seams"])
    md.close()
    fetcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
