#!/usr/bin/env python3
"""Offline road-worker benchmark with isolated caches and geometry fingerprints.

Copies source inputs (including auxiliary manifests/sea data) into a new folder.
No links, network or game. Derived caches are cold unless --warm-from is given;
that copies only this harness's earlier cache. A spawned worker builds three tiles.
Initialization, copy/hash work and the extra terrain parity sample are not timed.
OS file caches and external machine activity are not controlled.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import platform
import shutil
import sys
import time


def manifest(root):
    h, count, size = hashlib.sha256(), 0, 0
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            h.update(path.relative_to(root).as_posix().encode() + b"\0")
            h.update(hashlib.sha256(data).digest())
            count += 1
            size += len(data)
    return dict(sha256=h.hexdigest(), files=count, bytes=size)


def no_links(root):
    for path in [root, *root.rglob("*")]:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Use a physical copy, not a cache link: {path}")


def initialize(config):
    sys.path.insert(0, config["server_dir"])
    os.environ.update(BNGE_CACHE=config["cache"], BNE_REVISIT="off", BNGE_GPU="0",
                      BNGE_PLAN_RUN="stage-one-benchmark")
    import blockworker
    blockworker.init(config["cache"], True, True, True, True, True, None, 30,
                     airports=False, sea=True, roads_in_ground=True, gas_stations=True)


def ready():
    import fast
    import numpy as np
    return dict(python=platform.python_version(), numpy=np.__version__, native_api=fast._ef.VERSION,
                native_sha256=hashlib.sha256(Path(fast._ef.__file__).read_bytes()).hexdigest())


def build(config, delta, submitted):
    import blockworker as bw
    import dem
    import geotiles
    import revisit
    lat, lon = config["at"]
    gx, gy = geotiles.tile_of(lat, lon)
    gx, gy = gx + delta[0], gy + delta[1]
    zoom = dem.zoom_for(lat, 2.5)
    before = revisit.faults()
    start, cpu = time.perf_counter(), time.process_time()
    kw = {"submitted_at": submitted} if "submitted_at" in inspect.signature(bw.roads_geo).parameters else {}
    result = bw.roads_geo(1, lat, lon, zoom, gx, gy, zoom, config["shapes"], "/temp/stage-one",
                          True, 0.0, True, True, False, 1/256, True, country="NZ", exact="auto", **kw)
    return dict(key=geotiles.key(gx, gy), wall_s=time.perf_counter() - start,
                cpu_s=time.process_time() - cpu, queue_s=start - submitted,
                stages=result["stages"], dependencies=result.get("dependencies"),
                worker=result.get("worker"), faults=revisit.faults() - before)


def terrain(config):
    import blockworker as bw
    import dem
    import geotiles
    import grading
    import numpy as np
    import revisit
    lat, lon = config["at"]
    gx, gy = geotiles.tile_of(lat, lon)
    hashes, count, f0 = {}, 0, revisit.faults()
    for dx, dy in ((0, 0), (1, 0), (0, 1)):
        tile = geotiles.geobox(gx + dx, gy + dy)
        x0, y0, x1, y1 = tile.box
        x, y = np.meshgrid(np.linspace(x0, x1, 65), np.linspace(y0, y1, 65))
        la, lo, _ = tile.frame.enu_to_geodetic(x.ravel(), y.ravel(), 0)
        z = bw.ground(tile.frame, la, lo, bw._s.dem.sample(la, lo, dem.zoom_for(lat, 2.5)))
        z = bw._frame_grader(tile.frame).apply(la, lo, z, grading.ROAD_LIFT)
        hashes[tile.key] = hashlib.sha256(np.asarray(z, np.float64).tobytes()).hexdigest()
        count += len(z)
    return dict(sha256=hashes, points=count, faults=revisit.faults() - f0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server-dir", type=Path, required=True)
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--warm-from", type=Path)
    ap.add_argument("--label", required=True)
    args = ap.parse_args()
    out, source = args.out.resolve(), args.snapshot.resolve()
    if out.exists():
        ap.error("--out must be new")
    no_links(source)
    out.mkdir(parents=True)
    cache, shapes = out / "cache", out / "shapes"
    if args.warm_from:
        warm = args.warm_from.resolve()
        if not (warm / "results.json").is_file():
            ap.error("--warm-from must be an earlier completed benchmark run")
        no_links(warm / "cache")
        shutil.copytree(warm / "cache", cache)
    else:
        for folder in ("mapdata", "dem", "pagesdata", "seacarve"):
            shutil.copytree(source / folder, cache / folder)
    shapes.mkdir()
    config = dict(server_dir=str(args.server_dir.resolve()), cache=str(cache), shapes=str(shapes),
                  at=[-36.85325222344017, 174.759521484375], label=args.label,
                  warm_from=str(args.warm_from) if args.warm_from else None)
    record = dict(scope=__doc__, config=config, input_manifest=manifest(cache), rows=[],
                  server_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(args.server_dir.glob("*.py"))})
    with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"),
                             initializer=initialize, initargs=(config,)) as pool:
        record["environment"] = pool.submit(ready).result(timeout=90)
        for delta in ((0, 0), (1, 0), (0, 1)):
            row = pool.submit(build, config, delta, time.perf_counter()).result(timeout=300)
            record["rows"].append(row)
            print(json.dumps(dict(label=args.label, **row)), flush=True)
        record["terrain"] = pool.submit(terrain, config).result(timeout=120)
    record["geometry_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(shapes.iterdir()) if p.suffix in (".cdae", ".json")}
    record["skin_input_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in sorted(shapes.glob("*.pkl"))}
    record["mirror"] = manifest(cache / "mapdata-mirror")
    (out / "results.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
