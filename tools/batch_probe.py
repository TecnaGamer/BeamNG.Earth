#!/usr/bin/env python3
"""HTTP requests of a download_ahead round of map cells, with and without the batched tile prefetch
(mapdata.tiles_prefetch, earthd AHEAD_BATCH; the retro optimization study's section N): each run on a fresh scratch
cache, the archives' headers warmed first by one cell elsewhere, then the round's cells built AHEAD_CELLS at once as
download_ahead does. Counts the requests (and bytes) to Overture's tile hosts.

  python3 tools/batch_probe.py --scratch ~/bnge-tests/eta/batch --lat 32.25 --lon -110.95 --cells 8
"""
import argparse
import math
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

import mapcells  # noqa: E402
import mapdata  # noqa: E402
import pmtiles  # noqa: E402

LAYERS = ("roads", "buildings", "building_parts")


def run(cache, cells, warm_cell, batch):
    if os.path.exists(cache):
        shutil.rmtree(cache)
    md = mapdata.MapData(cache)
    md.layer_arrays(warm_cell, "roads", "demand")            # (headers, root directories, mirror ranking)
    md.layer_arrays(warm_cell, "buildings", "demand")
    calls = []
    real = md.fetcher.get

    def counted(url, *a, **kw):
        res = real(url, *a, **kw)
        if any(h in url for h in pmtiles.HOSTS):
            calls.append(len(res.body))
        return res
    md.fetcher.get = counted
    t0 = time.perf_counter()
    if batch:
        md.tiles_prefetch([(k, ly) for k in cells for ly in LAYERS], kind="demand")
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda k: md.cell(k, layers=LAYERS, kind="demand"), cells))
    dt = time.perf_counter() - t0
    md.close()
    return len(calls), sum(calls), dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=os.path.expanduser("~/bnge-tests/eta/batch"))
    ap.add_argument("--lat", type=float, default=32.25)
    ap.add_argument("--lon", type=float, default=-110.95)
    ap.add_argument("--cells", type=int, default=8)
    ap.add_argument("--heading", type=float, default=90.0, help="deg: the line of cells (a road ahead)")
    a = ap.parse_args()
    c0 = mapcells.cell_of(a.lat, a.lon)
    dx, dy = round(math.sin(math.radians(a.heading))), -round(math.cos(math.radians(a.heading)))
    cells = []
    for i in range(a.cells // 2):           # (two cells across: the block's reach round each point)
        for j in (0, 1):
            cells.append(mapcells.CellKey(14, c0.x + dx * i + (j if dx == 0 else 0), c0.y + dy * i + (j if dy == 0 else 0)))
    warm = mapcells.CellKey(14, c0.x - 40, c0.y - 40)
    for batch in (False, True):
        n, b, dt = run(os.path.join(a.scratch, "b" if batch else "a"), cells, warm, batch)
        print(f"{'batched' if batch else 'per cell'}: {n} requests, {b / 1e6:.2f} MB, {dt:.2f} s for {len(cells)} cells")


if __name__ == "__main__":
    main()
