#!/usr/bin/env python3
"""build.py: the corpus's road plans and cell facts, made again from the inputs alone (plancmp.py, factcmp.py).

    python3 tools/portcheck/build.py --cache CORPUS_CACHE --out OUT [--places a,b] [--server-dir TREE/server] [--online]

For every corpus place (corpus.json) its map cells within plan_half_m of the point are planned in a process set up
as the server's road processes are (blockworker.init: grading, measured widths, surfaces, sea, airports, gas
stations: the players' signature), and each cell's roads get their facts (level ranges, specs both hands, with and
without measured widths), as tools/bake_area.py makes them. Nothing kept is read: the revisit cache's items, decision
archives and baked packs are bypassed, so a tree's own code makes every plan and fact.

Written: OUT/plans/<place>/<z>_<x>_<y>.npz (the plan cache's own file, as the server's processes share it) and
index.json ({cell: {"complete": ...}}: a plan made short of an input is marked so); OUT/facts/<place>/<kind>_<z>_<x>_<y>
_<extra hash>.json ({kind, cell, extra, versions, data}). OUT/build-<place>.log: the run's warnings.

--online: fill the corpus cache itself (downloads; once, before it is frozen). Without it the build runs offline on a
work copy of the cache (OUT/work-cache, removed after), so the corpus cache never changes. See pcommon.py.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcommon  # noqa: E402

PASSES = 2                  # (online: cells short of a download planned again in a second pass)


def child(a):
    """One place, in a process of its own (the tree's modules, fresh in-memory caches)."""
    sys.path.insert(0, a.server_dir)
    name, place = pcommon.places(a.places)[0]
    os.environ.update(pcommon.PINNED_ENV)
    import npyguard         # (as earthd: numpy's AVX-512 kernels held back, before numpy is imported)
    npyguard.apply()
    os.environ["BNGE_PLAN_RUN"] = f"portcheck_{os.getpid()}_{int(time.time())}"
    import logging
    import numpy as np
    import blockworker as bw
    import earthd
    import geodesy
    import mapcells
    import plancache
    import revisit
    import roads
    earthd.raise_open_files()
    out_plans = os.path.join(a.out, "plans", name)
    out_facts = os.path.join(a.out, "facts", name)
    import shutil
    for d in (out_plans, out_facts):         # (an earlier build's items into this folder: never mixed in)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)

    # (nothing kept is read: every plan and fact made by this tree's code)
    revisit.load = lambda *x, **k: None
    revisit.has = lambda *x, **k: False
    revisit.locate = lambda *x, **k: None
    revisit.store = lambda *x, **k: None
    roads._facts_load = lambda feats, kind, extra: None
    plancache.PlanCache._baked = lambda self, key: None
    plancache.PlanCache._decided = lambda self, key: None
    made = {}

    orig_store = plancache.PlanCache.store

    def store(self, key, plans, keeps, covers, opens, decks=None, persist=False):
        ok = orig_store(self, key, plans, keeps, covers, opens, decks, persist=persist)
        made[tuple(int(v) for v in key)] = (self._path(key), bool(persist))
        return ok

    def facts(feats, kind, extra, data):
        cid = feats.__dict__.get("_bnge_cell")
        if cid is None:
            return
        cell = cid[0]
        cell = "_".join(str(int(v)) for v in cell) if isinstance(cell, (list, tuple)) else str(cell).replace("/", "_")
        h = pcommon.sha(repr(extra).encode())[:10]
        pcommon.write_json(os.path.join(out_facts, f"{kind}_{cell}_{h}.json"),
                           {"kind": kind, "cell": str(cid[0]), "extra": repr(extra), "versions": cid[2],
                            "data": data})
    plancache.PlanCache.store = store
    roads._facts_store = facts

    if a.online:            # (the server's main process downloads the airports table; the workers read the copy)
        import airports as airportsmod
        import netfetch
        try:
            airportsmod.Store(a.cache, netfetch.Fetcher(offline=False)).load()
        except Exception as ex:
            print(f"{name}: airports: {type(ex).__name__}: {ex}", flush=True)
    bw.init(a.cache, not a.online, True, True, True, True, None, logging.WARNING, True, True, 0, None, True)
    frame = geodesy.EnuFrame(place["lat"], place["lon"])
    h = float(place["plan_half_m"])
    la, lo, _ = frame.enu_to_geodetic(np.array([-h, h]), np.array([-h, h]), 0.0)
    keys = mapcells.cells_for_bbox(float(min(la)), float(min(lo)), float(max(la)), float(max(lo)))
    g, _ = bw._s.anchor(1, place["lat"], place["lon"])
    md = bw._s.md
    t0 = time.time()
    for pass_ in range(PASSES if a.online else 1):
        for k in keys:
            w, s, e, n = k.bounds()
            LA, LO = np.meshgrid(np.linspace(s, n, 3), np.linspace(w, e, 3))
            try:
                g.prepare(LA.ravel(), LO.ravel())
                f = md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
                if f is not None and len(f):
                    for i in range(len(f)):
                        roads.level_ranges(f, i)
                    for lh in (False, True):
                        roads.road_specs(f, None, bw._s.widths, lh)
                        roads.road_specs(f, None, None, lh)
            except Exception as ex:      # (a cell short of something: reported, the comparison shows it)
                print(f"{name}: cell {k}: {type(ex).__name__}: {ex}", flush=True)
    index = {}
    for key, (path, complete) in sorted(made.items()):
        cell = "_".join(map(str, key))
        if os.path.exists(path):
            with open(path, "rb") as src, open(os.path.join(out_plans, cell + ".npz"), "wb") as dst:
                dst.write(src.read())
        index[cell] = {"complete": complete and not os.path.exists(path + ".short")}
    pcommon.write_json(os.path.join(out_plans, "index.json"),
                       {"place": name, "cells": [str(k) for k in keys], "plans": index,
                        "plans_format": plancache.plans_format(), "facts_format": roads.FACTS_FORMAT,
                        "versions": json.loads(json.dumps(md.versions(), sort_keys=True)),
                        "seconds": round(time.time() - t0, 1)})
    print(f"{name}: {len(keys)} cells asked, {len(index)} cells planned "
          f"({sum(v['complete'] for v in index.values())} complete), {len(os.listdir(out_facts))} facts, "
          f"{time.time() - t0:.0f} s", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", required=True, help="the corpus cache (a folder of its own)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--places", default="", help="comma-separated corpus places (default: all)")
    ap.add_argument("--server-dir", default=pcommon.SERVER, help="the tree whose server builds (default: this one)")
    ap.add_argument("--online", action="store_true", help="fill the corpus cache (downloads) instead of a work copy")
    ap.add_argument("--one", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    a.cache, a.out = os.path.abspath(os.path.expanduser(a.cache)), os.path.abspath(os.path.expanduser(a.out))
    a.server_dir = os.path.abspath(a.server_dir)
    pcommon.check_cache(a.cache)
    if a.one:               # (the parent checked: the child's cache is the work copy under --out)
        return child(a)
    pcommon.check_apart(a.cache, a.out)
    pcommon.check_cache(a.out)
    pcommon.nice_self()
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.cache, exist_ok=True)
    work = a.cache if a.online else pcommon.work_copy(a.cache, os.path.join(a.out, "work-cache"))
    failed = []
    try:
        for name, _place in pcommon.places(a.places):
            cmd = [sys.executable, os.path.abspath(__file__), "--one", "--places", name, "--cache", work, "--out",
                   a.out, "--server-dir", a.server_dir] + (["--online"] if a.online else [])
            with open(os.path.join(a.out, f"build-{name}.log"), "w") as log:
                r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
            with open(os.path.join(a.out, f"build-{name}.log")) as log:
                last = [ln for ln in log.read().splitlines() if ln.startswith(name + ":")]
            print(last[-1] if last else f"{name}: no result (exit {r.returncode}, see build-{name}.log)", flush=True)
            if r.returncode:
                failed.append(name)
    finally:
        if not a.online:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
        else:       # (the fill's own plans run: derived, not part of the frozen inputs)
            pcommon.prune_derived(a.cache)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
