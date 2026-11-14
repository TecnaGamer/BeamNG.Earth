"""Alternate uncached and native-cached block class sampling on identical decoded inputs, without a server/game.

Default: seeded synthetic WorldCover-like rasters. --inputs reads standalone z_x_y.npz snapshots (never a live
cache or a provider); missing coverage fails before timing. Geometry/coordinate generation and SHA checks are
outside the timers. Includes one cold cache fill per route/pass. Does not benchmark downloads, sea, roads, all
of terrain generation or gameplay. Do not run alongside the game, compilers or other benchmarks.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import fast
import geodesy
import mapcells
import mapdata
import maprasters
import surface


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs", type=Path)
    ap.add_argument("--lat", type=float, default=39.282)
    ap.add_argument("--lon", type=float, default=-76.59)
    ap.add_argument("--size", type=int, choices=(512,1024,2048), default=1024)
    ap.add_argument("--pairs", type=int, default=9)
    ap.add_argument("--cpus", help="optional logical CPUs of this benchmark process only, e.g. 6,7,8,9,10,11")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if args.pairs < 3: ap.error("at least three alternating pairs")
    fast.require(171, "class grid benchmark")
    if args.cpus:
        import cpupin
        cpupin.pin_self({int(c) for c in args.cpus.split(",")})
    frame = geodesy.EnuFrame(args.lat,args.lon)
    n = args.size
    routes = {
        "straight": [(k*128,0) for k in range(9)],
        "diagonal": [(k*128,k*128) for k in range(9)],
        "turn_revisit": [(0,0),(128,0),(256,0),(256,128),(256,256),(128,256),(0,256),(0,128),(0,0)],
    }
    grids = {}
    keys = set()
    for cx,cy in {p for route in routes.values() for p in route}:
        x, y = cx-n/2+np.arange(n), cy-n/2+np.arange(n)
        la,lo,_ = frame.enu_to_geodetic(x[None,:],y[:,None],0.)
        bounds = fast._ef.class_grid_bounds(la,lo,surface.WARP_OCTAVES)
        if bounds is None: ap.error("benchmark route must fit the cacheable latitude/longitude range")
        keys.update(mapcells.cells_for_bbox(*bounds))
        grids[(cx,cy)] = (la,lo)
    rasters, inputs = {}, []
    for key in sorted(keys):
        if args.inputs:
            path = args.inputs / (str(key).replace("/","_")+".npz")
            if not path.is_file(): ap.error("missing standalone snapshot: "+str(path))
            with np.load(path,allow_pickle=False) as z:
                a = {k:z[k] for k in z.files}
            r = None if "missing" in a else mapdata._raster_from(a)
            inputs.append(dict(cell=str(key),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            w,s,e,north = key.bounds()
            rng = np.random.default_rng([key.z,key.x,key.y,731])
            a = rng.choice(np.array([10,20,30,40,50,60,70,80,90],np.uint8),(256,256))
            r = maprasters.Raster(a,"EPSG:4326",w,north,(e-w)/256,(north-s)/256)
        rasters[key] = r
    class Fixture:
        def has(self,key,layer): return layer == "landcover" and key in rasters
        def cell(self,key,layers,kind="background"): return SimpleNamespace(layers={"landcover":rasters[key]})
        def prefetch(self,*args): raise AssertionError("benchmark never fetches/writes cache inputs")
    base, cached = surface.Surface(Fixture()), surface.Surface(Fixture())
    report = dict(kind="decoded-input class sampling only; not a whole-world/gameplay speedup",
                  native=fast._ef.VERSION, python=sys.version.split()[0], size=n, square=1,
                  frame=[args.lat,args.lon], cpus=args.cpus, pairs=args.pairs,
                  inputs="seeded synthetic rasters" if not args.inputs else inputs,
                  source_sha256={str(p):hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
                                 for p in (Path("server/surface.py"),Path("native/earthfast/src/classgrid.rs"))}, routes={})
    try:
        for name,route in routes.items():
            reference = []
            for pos in route:
                out,complete = base.classes(*grids[pos])
                if not complete: raise RuntimeError("incomplete fixture")
                reference.append(hashlib.sha256(out.tobytes()).hexdigest())
            def run(which):
                if which == "cached" and hasattr(cached,"_class_grid"): cached._class_grid.clear()
                times, hashes = [], []
                for cx,cy in route:
                    la,lo = grids[(cx,cy)]
                    t0 = time.perf_counter()
                    out,complete = (base.classes(la,lo) if which == "base"
                                    else cached.block_classes(frame,cx,cy,la,lo))
                    times.append((time.perf_counter()-t0)*1000)
                    if not complete: raise RuntimeError("incomplete fixture")
                    hashes.append(hashlib.sha256(out.tobytes()).hexdigest())
                if hashes != reference: raise AssertionError("class bytes differ: "+name+" "+which)
                return times
            run("base"); run("cached")  # excluded warmup per consumer; every measured cached pass starts empty
            results = {"base":[], "cached":[]}
            for k in range(args.pairs):
                for which in (("base","cached") if k%2 == 0 else ("cached","base")):
                    results[which].append(run(which))
            totals = {k:[sum(p) for p in v] for k,v in results.items()}
            med = {k:statistics.median(v) for k,v in totals.items()}
            row = dict(points=len(route)*n*n, passes_ms=totals, per_block_ms=results, median_route_ms=med,
                       change_percent=(med["cached"]/med["base"]-1)*100,
                       output_sha256=reference, cold_fill_included=True,
                       class_sample_cache_bytes=cached._class_grid.bytes)
            report["routes"][name] = row
            print(json.dumps(dict(route=name,median_ms=med,change_percent=row["change_percent"])),flush=True)
    finally:
        base.close();cached.close()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2),encoding="utf-8")


if __name__ == "__main__":
    main()
