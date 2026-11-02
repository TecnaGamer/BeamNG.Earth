#!/usr/bin/env python3
"""Bake an area's road plans and cell facts into a pack (server/bakepack.py), offline, as a first visit would make them.

  python3 tools/bake_area.py --bbox 40.70,-74.02,40.80,-73.93 --cache /scratch/bake-cache --out nyc.bnge --procs 12

Each worker process is set up as the server's road processes are (blockworker.init: grading, measured widths, sea),
so the plans carry the server's signature. It plans every map cell of the box (grading.Grader.prepare at the cell's
points) and makes its roads' facts (level ranges, specs). Only what the pipeline itself keeps as complete goes in the
pack: the plans PlanCache.store persists, the facts roads._facts_store keeps (lidar rule cells are made again with
the lidar, BNE_LIDAR_GAPS_WAIT raised). --cache: a cache folder of its own (the map data and DEM downloaded there).
"""
import argparse
import json
import multiprocessing
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PASSES = 3                  # planning passes: cells short of something in one are planned again in the next
SERVER = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER)


def _worker(args):
    cache, cells, out_jsonl, wid = args
    import logging
    import numpy as np
    import blockworker as bw
    import earthd
    import mapcells
    import plancache
    import roads
    earthd.raise_open_files()
    bw.init(cache, False, True, True, True, True, None, logging.WARNING, True, True, 0, None, False)
    rec = open(out_jsonl, "a")
    orig_store = plancache.PlanCache.store
    orig_facts = roads._facts_store

    def store(self, key, plans, keeps, covers, opens, persist=False):
        ok = orig_store(self, key, plans, keeps, covers, opens, persist)
        if ok and persist:
            rec.write(json.dumps({"plans": [int(k) for k in key], "sig": self.signature,
                                  "path": self._path(key)}) + "\n")
            rec.flush()
        return ok

    def facts(feats, kind, extra, data):
        orig_facts(feats, kind, extra, data)
        cid = feats.__dict__.get("_bnge_cell")
        if cid is not None:
            rec.write(json.dumps({"facts": kind, "extra": extra, "cell": cid[0], "versions": cid[2],
                                  "data": data}) + "\n")
            rec.flush()
    plancache.PlanCache.store = store
    roads._facts_store = facts
    lat0, lon0 = cells[0][3], cells[0][4]
    g, _ = bw._s.anchor(1, lat0, lon0)
    md = bw._s.md
    done = 0
    t0 = time.time()
    for z, x, y, la, lo in cells:
        k = mapcells.CellKey(z, x, y)
        try:
            w, s, e, n = k.bounds()
            # (the cell's points: its plans and every neighbour they read)
            lat = np.linspace(s, n, 3)
            lon = np.linspace(w, e, 3)
            LA, LO = np.meshgrid(lat, lon)
            g.prepare(LA.ravel(), LO.ravel())
            f = md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
            if f is not None and len(f):
                for i in range(len(f)):
                    roads.level_ranges(f, i)
                for lh in (False, True):
                    roads.road_specs(f, None, bw._s.widths, lh)
                    roads.road_specs(f, None, None, lh)
        except Exception as ex:          # (a cell short of something: left out, the client makes it)
            print(f"worker {wid}: cell {k}: {type(ex).__name__}: {ex}", flush=True)
        done += 1
        if done % 10 == 0:
            print(f"worker {wid}: {done}/{len(cells)} cells, {time.time() - t0:.0f} s", flush=True)
    rec.close()
    return json.loads(json.dumps(md.versions(), sort_keys=True))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", help="S,W,N,E")
    ap.add_argument("--cache")
    ap.add_argument("--out")
    ap.add_argument("--index", help="a folder of packs: print their index.json (the release's) and stop")
    ap.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args()
    if a.index:
        import bakepack
        print(json.dumps(bakepack.index_of(a.index), indent=1, sort_keys=True))
        return
    if not (a.bbox and a.cache and a.out):
        ap.error("--bbox, --cache and --out (or --index)")
    import mapcells
    import plancache
    import bakepack
    s, w, n, e = map(float, a.bbox.split(","))
    keys = mapcells.cells_for_bbox(s, w, n, e, max_cells=200000)
    cells = [(k.z, k.x, k.y, *k.center()) for k in keys]
    print(f"{len(cells)} cells, {a.procs} processes", flush=True)
    os.makedirs(a.cache, exist_ok=True)
    plancache.start_run(a.cache)            # (one run: the workers share plans, lock files)
    os.environ.setdefault("BNE_LIDAR_GAPS_WAIT", "120")
    work = os.path.join(a.cache, "bake_work")
    os.makedirs(work, exist_ok=True)
    for n_ in os.listdir(work):
        os.remove(os.path.join(work, n_))
    t0 = time.time()
    todo, versions, jobs = cells, [], []
    for pass_ in range(PASSES):
        # (a cell short of something in a pass (a download that failed, the lidar late) is planned again in the next,
        # in a run of its own: its incomplete plans in the last run's folder would be read instead)
        if pass_:
            plancache.start_run(a.cache)
        pj = [(a.cache, todo[i::a.procs], os.path.join(work, f"p{pass_}w{i}.jsonl"), i) for i in range(a.procs)]
        pj = [j for j in pj if j[1]]
        with multiprocessing.get_context("spawn").Pool(len(pj)) as pool:
            versions += pool.map(_worker, pj)
        jobs += pj
        have = set()
        for j in jobs:
            if os.path.exists(j[2]):
                for line in open(j[2]):
                    r = json.loads(line)
                    if "plans" in r:
                        have.add(tuple(r["plans"]))
        todo = [c for c in cells if (c[0], c[1], c[2]) not in have]
        print(f"pass {pass_ + 1}: {len(cells) - len(todo)}/{len(cells)} cells' plans complete, "
              f"{time.time() - t0:.0f} s", flush=True)
        if not todo:
            break
    if todo:
        print("cells without complete plans (the client plans them): " +
              " ".join(f"{z}/{x}/{y}" for z, x, y, _la, _lo in todo), flush=True)
    if len({json.dumps(v, sort_keys=True) for v in versions}) != 1:
        raise SystemExit(f"the workers' map data versions differ: {versions}")
    wr = bakepack.Writer(a.out)
    sigs = set()
    n_plans = n_facts = 0
    for j in jobs:
        if not os.path.exists(j[2]):
            continue
        for line in open(j[2]):
            r = json.loads(line)
            if "plans" in r:
                try:
                    data = open(r["path"], "rb").read()
                except OSError:
                    continue
                sigs.add(r["sig"])
                wr.add(bakepack.plans_key(r["sig"], r["plans"]), data)
                n_plans += 1
            elif "facts" in r:
                wr.add(bakepack.facts_key(r["facts"], r["extra"], r["cell"]),
                       json.dumps(r["data"]).encode("utf-8"))
                n_facts += 1
    if len(sigs) > 1:
        raise SystemExit(f"several plan signatures: {sigs}")
    meta = {"bake_format": bakepack.BAKE_FORMAT, "plans_format": plancache.PLANS_FORMAT,
            "facts_format": __import__("roads").FACTS_FORMAT, "frames": plancache.FRAMES,
            "signature": next(iter(sigs)) if sigs else None, "versions": versions[0] if versions else None,
            "bbox": [s, w, n, e], "made": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    items = wr.close(meta)
    print(f"{a.out}: {items} items ({n_plans} plans, {n_facts} facts), {os.path.getsize(a.out) / 1e6:.1f} MB, "
          f"{time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    main()
