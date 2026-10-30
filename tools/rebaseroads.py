#!/usr/bin/env python3
"""rebaseroads.py LAT LON [--km 15] [--procs 4] [--offline]: what a rebase costs the road tiles, old way and geotiles.

A pool of road processes (blockworker.init, as earthd's) first builds the place's road tiles for an anchor KM west (the
session before the rebase: content, skins, exact variants), then the rebase to an anchor at the place:
  - anchor tiles (--geo-tiles off): every tile within roads.SEND_M built again for the new anchor;
  - geotiles: the skins of every geotile within SEND_M and the exact variants the new anchor needs (no content);
nearest first. Prints CPU-s (the workers' own), wall s with PROCS processes, the time until the tiles within NEAR_M of
the car are in, and the shapes the mod loads again (the old way: every shape of every tile; geotiles: the skins' and
the parts whose exact variant changes). Map data from ~/.cache/beamng-earth; the revisit cache off. Run from the
repository root."""
import argparse
import concurrent.futures as cf
import json
import math
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "server"))
os.environ["BNE_REVISIT"] = "off"

NEAR_M = 300.0
SHAPES = ("road", "deck", "walk", "walkcol", "bld", "bld2", "bld3", "bld4", "bldcol", "wire", "tunnel", "rail", "railcol")


def _cpu(fn, *a, **kw):
    """fn in a worker, with its CPU time."""
    t0 = time.process_time()
    r = fn(*a, **kw)
    return r, time.process_time() - t0


def old_tile(*a, **kw):
    import blockworker
    return _cpu(blockworker.roads_tile, *a, **kw)


def geo_tile(*a, **kw):
    import blockworker
    return _cpu(blockworker.roads_geo, *a, **kw)


def run(pool, jobs, near):
    """[(kind, args, kwargs, distance)] nearest first -> (worker CPU-s, wall s, wall s of the near ones, results)."""
    t0 = time.perf_counter()
    futs = [(pool.submit(fn, *a, **kw), d) for fn, a, kw, d in sorted(jobs, key=lambda j: j[3])]
    cpu, t_near, out = 0.0, 0.0, []
    for f, d in futs:
        r, c = f.result()
        cpu += c
        out.append(r)
        if d <= near:
            t_near = time.perf_counter() - t0
    return cpu, time.perf_counter() - t0, t_near, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--km", type=float, default=15.0)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    import blockworker
    import geodesy
    import geotiles
    import roads
    lat, lon = args.lat, args.lon
    lon_a = lon - args.km * 1000.0 / (111320.0 * math.cos(math.radians(lat)))
    fa, fb = geodesy.EnuFrame(lat, lon_a), geodesy.EnuFrame(lat, lon)
    q = 1 / 32
    cache = os.path.expanduser("~/.cache/beamng-earth")
    out = tempfile.mkdtemp(prefix="rebaseroads_")
    ctx = mp.get_context("spawn")
    pool = cf.ProcessPoolExecutor(args.procs, mp_context=ctx, initializer=blockworker.init,
                                  initargs=(cache, args.offline, True, True, True, True, None, 30, False, True, 0,
                                            None, True))
    try:
        xa, ya, _ = fa.geodetic_to_enu(lat, lon, 0.0)
        old_a = [(old_tile, (1, lat, lon_a, 15, tx, ty, out, "/x", True, 0.0), {"buildings": True, "q": q,
                  "gas_stations": True}, roads.tile_distance(tx, ty, float(xa), float(ya)))
                 for tx, ty in roads.tiles_near(float(xa), float(ya), roads.SEND_M)]
        old_b = [(old_tile, (2, lat, lon, 15, tx, ty, out, "/x", True, 0.0), {"buildings": True, "q": q,
                  "gas_stations": True}, roads.tile_distance(tx, ty, 0.0, 0.0))
                 for tx, ty in roads.tiles_near(0.0, 0.0, roads.SEND_M)]
        near_a = geotiles.tiles_near(fa, float(xa), float(ya), roads.SEND_M)
        near_b = geotiles.tiles_near(fb, 0.0, 0.0, roads.SEND_M)
        geo_a = [(geo_tile, (1, lat, lon_a, 15, gx, gy, 15, out, "/x", True, 0.0, True, False, True, q, True),
                  {"exact": "auto"}, d) for gx, gy, d in near_a]
        # the session before the rebase: anchor A's tiles, both ways (the processes' plans and chains warm)
        print(f"anchor A ({args.km:.0f} km west): {len(old_a)} anchor tiles, {len(geo_a)} geotiles ...", flush=True)
        c_oa, w_oa, _n, _r = run(pool, old_a, NEAR_M)
        c_ga, w_ga, _n, res_a = run(pool, geo_a, NEAR_M)
        print(f"  first visit: anchor tiles {c_oa:.1f} CPU-s ({w_oa:.1f} s wall); geotiles content + skins + exact "
              f"{c_ga:.1f} CPU-s ({w_ga:.1f} s wall)")
        shown = {}
        info = {}
        for (gx, gy, _d), r in zip(sorted(near_a, key=lambda t: t[2]), res_a):
            info[(gx, gy)] = r["content"][2]
            shown[(gx, gy)] = {w: 1 for w in r["exact"]}
        # the rebase to anchor B (at the place)
        c_ob, w_ob, n_ob, res_ob = run(pool, old_b, NEAR_M)
        loads_old = 0
        for path, _n, _p in res_ob:
            d = json.load(open(os.path.join(out, os.path.basename(path))))
            loads_old += sum(1 for w in SHAPES if isinstance(d.get(w), dict))
        tr = roads.GeoTracker()
        tr.frames = {1: fa, 2: fb}
        tr.info, tr.shown, tr.aid, tr.frame = info, shown, 2, fb
        geo_b, base_back, have = [], 0, 0
        for gx, gy, d in near_b:
            k = (gx, gy)
            if k not in info:           # (a tile only the new anchor's reach has: its content too)
                geo_b.append((geo_tile, (2, lat, lon, 15, gx, gy, 15, out, "/x", True, 0.0, True, False, True, q, True),
                              {"exact": "auto"}, d))
                continue
            have += 1
            make, base = tr.exact_need(k)
            base_back += len(base)
            geo_b.append((geo_tile, (2, lat, lon, 15, gx, gy, 15, out, "/x", True, 0.0, True, False, True, q, True),
                          {"content": False, "exact": make or None}, d))
        c_gb, w_gb, n_gb, res_gb = run(pool, geo_b, NEAR_M)
        loads_geo = base_back
        content_again = 0
        for r in res_gb:
            if r["content"] is not None:
                content_again += 1
                loads_geo += len((r["content"][2] or {}).get("parts") or {})
            if r["skin"] is not None:
                d = json.load(open(os.path.join(out, os.path.basename(r["skin"][0]))))
                loads_geo += sum(1 for w in ("road", "walk", "walkcol") if isinstance(d.get(w), dict))
            loads_geo += len(r["exact"])
        print(f"rebase ({args.km:.0f} km), {args.procs} road processes:")
        print(f"  anchor tiles: {len(old_b)} tiles built again: {c_ob:.1f} CPU-s, {w_ob:.1f} s wall, the tiles within "
              f"{NEAR_M:.0f} m in after {n_ob:.1f} s; the mod loads {loads_old} shapes again")
        print(f"  geotiles: {len(geo_b)} skins ({have} kept contents, {content_again} new), {c_gb:.1f} CPU-s, "
              f"{w_gb:.1f} s wall, within {NEAR_M:.0f} m after {n_gb:.1f} s; the mod loads {loads_geo} shapes "
              f"({base_back} parts back to their base shapes, {sum(len(r['exact']) for r in res_gb)} exact variants)")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    main()
