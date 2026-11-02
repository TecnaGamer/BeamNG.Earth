#!/usr/bin/env python3
"""Offline road geometry benchmark using existing decoded map-cell files.

Measures the road builder and draped mesh separately, with flat 10 m terrain
and all roads treated as urban. Bridges use build_tile's local fallback solver.
This deliberately excludes network, grading, buildings, process queues and game
imports. The input cache is read only; profiles and JSON go to --out.
"""
import argparse
import cProfile
import hashlib
import json
from pathlib import Path
import platform
import pstats
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import numpy as np
import fast
import geodesy
import geotiles
import mapcells
import mapdata
import mapgeom
import roadmesh
import roads


def digest(value):
    h = hashlib.sha256()
    def add(v):
        if isinstance(v, np.ndarray):
            h.update(str((v.shape, v.dtype)).encode())
            h.update(v.tobytes())
        elif isinstance(v, dict):
            for k in sorted(v):
                add(k)
                add(v[k])
        elif isinstance(v, (list, tuple)):
            h.update(str(len(v)).encode())
            for item in v:
                add(item)
        else:
            h.update(repr(v).encode())
        h.update(b"\0")
    add(value)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--at", required=True, help="latitude,longitude")
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--radius", type=int, default=1)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--country", required=True)
    ap.add_argument("--right-hand", action="store_true")
    ap.add_argument("--anchor-tiles", action="store_true", help="legacy tiles instead of default geotiles")
    a = ap.parse_args()
    if a.radius < 0 or a.repeat < 1:
        ap.error("radius must be nonnegative and repeat must be positive")
    lat, lon = map(float, a.at.split(","))
    a.out.mkdir(parents=True, exist_ok=True)
    # path() only: do not construct MapData (constructors install providers).
    md = object.__new__(mapdata.MapData)
    md.cache_dir, md.release = a.cache_dir, mapdata.overture.RELEASE
    loaded, sources = {}, {}
    def load(key):
        if key not in loaded:
            path = Path(md.path(key, "roads"))
            with np.load(path, allow_pickle=False) as archive:
                arrays = dict(archive)
            f = mapgeom.Features.from_arrays("roads", arrays, "")
            loaded[key] = f.localize(geodesy.EnuFrame(*key.center()))
            sources[str(key)] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "features": len(f)}
        return loaded[key]
    tiles = []
    gx, gy = geotiles.tile_of(lat, lon)
    for dx in range(-a.radius, a.radius + 1):
        for dy in range(-a.radius, a.radius + 1):
            if a.anchor_tiles:
                frame, tx, ty = geodesy.EnuFrame(lat, lon), dx, dy
                key = f"{dx}_{dy}"
            else:
                tx, ty = geotiles.geobox(gx + dx, gy + dy), None
                frame, key = geotiles.frame(gx + dx, gy + dy), tx.key
            cells = [load(k) for k in roads.cells_of_tile(frame, tx, ty)]
            tiles.append((key, frame, tx, ty, cells))
    def flat(la, lo):
        return np.full(np.shape(la), 10.0)
    def urban(la, lo):
        return np.ones(np.shape(la), dtype=bool)
    rows, prof = [], cProfile.Profile()
    for repeat in range(a.repeat):
        for key, frame, tx, ty, cells in tiles:
            walks, ctx = [], []
            box = roads.tile_bbox(tx, ty)
            if a.profile:
                prof.enable()
            t0, c0 = time.perf_counter(), time.process_time()
            chunks = roads.build_tile(frame, tx, ty, cells, flat, urban, walks=walks,
                                      deck_context=ctx, left_hand=not a.right_hand, country=a.country)
            t1, c1 = time.perf_counter(), time.process_time()
            mesh = roadmesh.build(frame, chunks, flat, (*box[:2], 0.0), clip=box, follow=True,
                                  q=1/256, context=ctx, clip_poly=tx.quad if ty is None else None)
            t2, c2 = time.perf_counter(), time.process_time()
            prof.disable()
            row = {"repeat": repeat, "key": key, "build_s": t1-t0, "mesh_s": t2-t1,
                   "cpu_s": c2-c0, "chunks": len(chunks), "walks": len(walks),
                   "digest": digest((chunks, walks, ctx, mesh))}
            rows.append(row)
            print(json.dumps(row), flush=True)
    result = {"scope": __doc__, "at": [lat, lon], "anchor_tiles": a.anchor_tiles,
              "country": a.country, "left_hand": not a.right_hand,
              "profiled": a.profile, "python": platform.python_version(), "numpy": np.__version__,
              "native_version": fast._ef.VERSION, "sources": sources, "rows": rows,
              "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_sha256": {name: hashlib.sha256((ROOT / 'server' / name).read_bytes()).hexdigest()
                                for name in ('roads.py', 'roadmesh.py')}}
    result["median_s"] = {k: statistics.median(r[k] for r in rows) for k in ("build_s", "mesh_s", "cpu_s")}
    (a.out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    if a.profile:
        prof.dump_stats(str(a.out / "road.prof"))
        with (a.out / "profile.txt").open("w") as f:
            pstats.Stats(prof, stream=f).sort_stats("cumulative").print_stats(65)
            pstats.Stats(prof, stream=f).sort_stats("tottime").print_stats(40)
    print(json.dumps(result["median_s"]))


if __name__ == "__main__":
    main()
