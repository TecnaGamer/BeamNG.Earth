#!/usr/bin/env python3
"""The graded roads' numbers on real places (docs/VERTICAL_SOLVER_RFC.md, step 1: the baseline any change to grading is
judged against). For each place it builds the road tiles round it as the game does (blockworker.roads_tile, which plans
their cells), then measures every cell it planned (server/grademetrics.py): junction and seam steps, grades over their
class's ceiling that the plans made, bumps, distance from the ground, clearance under decks, planning CPU.

    python3 tools/grade_metrics.py                          # the benchmark places with roads (docs/benchmarks.json)
    python3 tools/grade_metrics.py --at 38.0446,23.7371,athens --tiles 1 --out build/grade_metrics.json

Downloads what the cache lacks (--offline: the cache only). Set BNE_REVISIT=off BNGE_PLANCACHE=off so the plans are
made here rather than read from earlier runs (the default: on, as the game). Changes nothing.
"""
import argparse
import json
import os
import queue
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

ROAD_TAGS = ("city", "village", "megacity")
EXTRA = [("athens_omonia", 38.0446, 23.7371)]      # GitHub #1327's place: rail, metro and dense streets


def places(args):
    if args.at:
        out = []
        for s in args.at:
            v = s.split(",")
            out.append((v[2] if len(v) > 2 else f"{v[0]},{v[1]}", float(v[0]), float(v[1])))
        return out
    with open(os.path.join(ROOT, "docs", "benchmarks.json"), encoding="utf-8") as f:
        bench = json.load(f)["places"]
    out = [(p["id"], float(p["lat"]), float(p["lon"])) for p in bench
           if set(p.get("tags", ())) & set(ROAD_TAGS) and (args.heldout or not p.get("heldout"))]
    return out + EXTRA


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", action="append", help="lat,lon[,name] (repeatable); default: the benchmark places with roads")
    ap.add_argument("--heldout", action="store_true", help="the held-out benchmark places too")
    ap.add_argument("--tiles", type=int, default=1, help="road tiles from -n to n on each axis round a place (256 m)")
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/beamng-earth"))
    ap.add_argument("--offline", action="store_true", help="the cache only")
    ap.add_argument("--out", default=None, help="write every place's numbers here (JSON)")
    ap.add_argument("--vsolve", action="store_true", help="the one solve beside the passes too (BNE_VSOLVE=shadow, "
                    "server/vsolve.py): both measured on the same cells, the solve's numbers under each place's vsolve")
    a = ap.parse_args()
    if a.vsolve:
        os.environ["BNE_VSOLVE"] = "shadow"         # (before the server's modules read it)

    import blockworker
    blockworker.init(a.cache_dir, a.offline, True, True, True, True, queue.Queue(), 30, True, True)
    import dem as demmod
    import grading
    import grademetrics as gm
    import roads as roadsmod

    cpu = {}
    lock = threading.Lock()
    make = grading._Planner._make_cell

    def timed(self, key, *args, **kw):
        c0 = time.thread_time()
        try:
            return make(self, key, *args, **kw)
        finally:
            with lock:
                cpu[str(key)] = cpu.get(str(key), 0.0) + time.thread_time() - c0
    grading._Planner._make_cell = timed

    out_dir = os.path.join(ROOT, "build", "grade_metrics_tiles")
    os.makedirs(out_dir, exist_ok=True)
    results = {}
    seen_cells = set()
    for n, (name, lat, lon) in enumerate(places(a), start=1):
        t0 = time.perf_counter()
        zoom = demmod.zoom_for(lat, 2.5)
        for tx in range(-a.tiles, a.tiles + 1):
            for ty in range(-a.tiles, a.tiles + 1):
                try:
                    blockworker.roads_tile(n, lat, lon, zoom, tx, ty, out_dir, "/temp/x", True, 0.0, True, False, True)
                except Exception as e:      # (a place without data offline: measured with what it has)
                    print(f"{name}: road tile ({tx},{ty}): {type(e).__name__}: {e}", file=sys.stderr)
        store = getattr(blockworker._s, "plan_store", None)
        if store is None:
            print(f"{name}: no plans (grading off?)", file=sys.stderr)
            continue
        variants = ("passes", "vsolve") if a.vsolve else ("passes",)
        acc = {v: {"j": [], "s": [], "p": [], "c": []} for v in variants}
        cells_cpu, deck_ground, n_cells, infos = 0.0, [], 0, []
        # (the cells this place's road tiles planned, taken now: a cell planned later is no part of this place; one
        # measured with the next place planned its own ring round it, and so on: Tokyo's spread for an hour, 2026-11-16)
        todo = []
        for planner in list(store.planners.values()):
            with planner.lock:
                keys = [k for k in planner.cells if str(k) not in seen_cells]
            seen_cells.update(str(k) for k in keys)
            if keys:
                todo.append((planner, keys))
        for planner, keys in todo:
            frame = planner.frame
            n_cells += len(keys)

            def ground(xy, frame=frame, planner=planner):
                la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
                return planner.elev(la, lo)
            boxes, feats_all, spec_of = {}, [], {}
            for k in keys:
                boxes[k] = planner._clip_box(k, 0.0)
                feats = blockworker._s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
                feats_all.append(feats)
                by_fid = {}
                if feats is not None and len(feats):
                    specs = roadsmod.road_specs(feats, None, planner.widths)
                    by_fid = {str(feats.ids[i]): sp for i, sp in enumerate(specs) if sp is not None}
                spec_of[k] = by_fid
                cells_cpu += cpu.get(str(k), 0.0)
                got = getattr(planner, "_vsolve", {}).get(str(k))
                if got is not None:
                    where = {}
                    for name_ in ("grade_worst", "clear_worst"):
                        w = got[1].get(name_)
                        if w is not None:   # (where a ceiling / a cap broke the most, as lat/lon)
                            la, lo, _ = planner.frame.enu_to_geodetic(w["x"], w["y"], 0.0)
                            w = {**w, "lat": round(float(la), 6), "lon": round(float(lo), 6)}
                        where[name_] = w
                    infos.append({**got[1], "key": str(k), **where})
            # (the decks as the road tiles solved them, in this planner's frame)
            decks = []
            bl = [b for b in boxes.values() if b is not None]
            if bl:
                bb = (min(b[0] for b in bl), min(b[1] for b in bl), max(b[2] for b in bl), max(b[3] for b in bl))
                try:
                    view = blockworker._bridges(n, frame, None)
                    # (only the decks the road tiles solved: solving the others plans the cells round them, the
                    # roads past the tiles' reach; Tokyo's measure took 29 min of its 60 that way, 2026-11-16)
                    for fid, sp, xy, z, _lon in view._parts(feats_all, bb, solved_only=True):
                        if len(xy) < 2:
                            continue
                        # (the view's z is the frame's up, the Earth's curve in it; the plans' z is the elevation's
                        # height, as their ground: a deck 9 km from the region's origin stood 6.2 m "under" its ground)
                        xy = np.asarray(xy, np.float64)
                        _la, _lo, h = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.asarray(z, np.float64))
                        # (with its depth: the clearance it was solved for, grademetrics.clearance)
                        decks.append((fid, xy[:, :2], np.asarray(h, np.float64), float(sp.width) / 2,
                                      float(view.depth_of(fid))))
                except Exception as e:
                    print(f"{name}: decks: {type(e).__name__}: {e}", file=sys.stderr)
                    decks = []
            for _f, xy, z, *_ in decks:     # (a check of the frames: a deck's ends stand on its abutments' ground)
                g = ground(np.asarray(xy, np.float64)[[0, -1], :2])
                deck_ground.extend((np.asarray(z, np.float64)[[0, -1]] - g).tolist())

            def geo(rows, frame=frame):
                for r in rows:
                    if "x" in r and "lat" not in r:
                        la, lo, _ = frame.enu_to_geodetic(r["x"], r["y"], 0.0)
                        r["lat"], r["lon"] = round(float(la), 6), round(float(lo), 6)
            for v in variants:
                cells, names = {}, set()
                for k in keys:
                    if v == "passes":
                        plans = planner.cells[k]
                    else:
                        plans = getattr(planner, "_vsolve", {}).get(str(k), ([], None))[0]
                    ceilings = {fid: grading.guard_limits(spec_of[k][fid], p)[0] for fid, p in plans
                                if fid in spec_of[k]}
                    road_plans = [(f, p) for f, p in plans if f in ceilings]
                    names |= set(ceilings)
                    cells[k] = (road_plans, boxes[k])
                    acc[v]["j"] += gm.junction_steps(road_plans, boxes[k])
                    acc[v]["p"] += gm.profiles(road_plans, ceilings, ground)
                acc[v]["s"] += gm.seam_steps(cells)
                acc[v]["c"] += gm.clearance([fp for plans, _b in cells.values() for fp in plans], decks, names)
                geo(acc[v]["j"])
                geo(acc[v]["s"])
                geo(acc[v]["c"])
        for planner in list(store.planners.values()):     # (any cell planned while measuring: no place's)
            with planner.lock:
                seen_cells.update(str(k) for k in planner.cells)
        for v in variants:
            q = acc[v]
            cpu_s = round(cells_cpu, 2) if v == "passes" else round(sum(i.get("elapsed_s", 0.0) for i in infos), 2)
            s = gm.summary(q["j"], q["s"], q["p"], q["c"], cpu_s)
            s["cells"] = n_cells
            if v == "passes":
                s["deck_end_over_ground_median"] = float(np.median(deck_ground)) if deck_ground else None
                s["wall_s"] = round(time.perf_counter() - t0, 1)
                results[name] = s
            else:
                bad = [i for i in infos if i.get("fallback")]
                s["solves"] = {"cells": len(infos), "unknowns": int(sum(i.get("n", 0) for i in infos)),
                               "joins": int(sum(i.get("joins", 0) for i in infos)), "fallback": len(bad),
                               "grade_broken": int(sum(i.get("grade_broken", 0) for i in infos)),
                               "grade_broken_cells": int(sum(1 for i in infos if i.get("grade_broken"))),
                               "grade_excess_m": round(max((i.get("grade_excess_m", 0.0) for i in infos), default=0.0), 3),
                               "caps": int(sum(i.get("caps", 0) for i in infos)),
                               "floors": int(sum(i.get("floors", 0) for i in infos)),
                               "clear_broken": int(sum(i.get("clear_broken", 0) for i in infos)),
                               "clear_excess_m": round(max((i.get("clear_excess_m", 0.0) for i in infos), default=0.0), 3),
                               "statuses": sorted({str(i.get("status")) for i in infos}),
                               "worst_s": round(max((i.get("elapsed_s", 0.0) for i in infos), default=0.0), 2),
                               "by_cell": {i["key"]: {"fallback": i.get("fallback"), "culprit": i.get("culprit"),
                                                      "grade_broken": i.get("grade_broken"),
                                                      "grade_excess_m": round(i.get("grade_excess_m", 0.0), 3),
                                                      "grade_worst": i.get("grade_worst"),
                                                      "clear_broken": i.get("clear_broken"),
                                                      "clear_worst": i.get("clear_worst"),
                                                      "rounds": i.get("rounds_grade"), "s": round(i.get("elapsed_s", 0), 2)}
                                           for i in infos}}
                results[name]["vsolve"] = s
            label = name if v == "passes" else "  (one solve)"
            j, sm, g, c = s["junction"], s["seam"], s["grade"], s["clearance"]
            print(f"{label:22s} roads {s['roads']:5d}  junction steps >=30cm {j['bad']:4d}/{j['n']:5d} max {j['max']:5.2f}"
                  f"  seams >=30cm {sm['bad']:3d}/{sm['n']:4d}  made steep {g['made_steep']:3d}  bump p99 "
                  f"{s['bump']['p99_median']:.3f} (ground {s['bump']['ground_p99_median']:.3f})  low decks {c['n']:3d} "
                  f"(through {c['through_deck']:2d}) worst {c['max']:4.1f}  cpu {cpu_s:6.1f} s", flush=True)
        if a.out:               # (after every place: a long run stopped keeps the places it measured)
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out + ".tmp", "w", encoding="utf-8") as f:
                json.dump(results, f, indent=1)
            os.replace(a.out + ".tmp", a.out)


import numpy as np  # noqa: E402  (after sys.path: the server's numpy users)

if __name__ == "__main__":
    main()
