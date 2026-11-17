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
    a = ap.parse_args()

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
        junctions, seams, profs, clear, cells_cpu = [], [], [], [], 0.0
        deck_ground = []
        n_cells = 0
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
            cells = {}
            names = set()
            feats_all = []
            for k in keys:
                plans = planner.cells[k]
                box = planner._clip_box(k, 0.0)
                feats = blockworker._s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
                feats_all.append(feats)
                ceilings = {}
                if feats is not None and len(feats):
                    specs = roadsmod.road_specs(feats, None, planner.widths)
                    by_fid = {str(feats.ids[i]): sp for i, sp in enumerate(specs) if sp is not None}
                    for fid, p in plans:
                        sp = by_fid.get(fid)
                        if sp is not None:
                            ceilings[fid] = grading.guard_limits(sp, p)[0]
                road_plans = [(f, p) for f, p in plans if f in ceilings]
                names |= set(ceilings)
                cells[k] = (road_plans, box)
                junctions += gm.junction_steps(road_plans, box)
                profs += gm.profiles(road_plans, ceilings, ground)
                cells_cpu += cpu.get(str(k), 0.0)
            seams += gm.seam_steps(cells)
            # (the decks as the road tiles solved them, in this planner's frame)
            boxes = [b for _p, b in cells.values() if b is not None]
            if boxes:
                bb = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                      max(b[3] for b in boxes))
                try:
                    view = blockworker._bridges(n, frame, None)
                    decks = []
                    # (only the decks the road tiles solved: solving the others plans the cells round them, the
                    # roads past the tiles' reach; Tokyo's measure took 29 min of its 60 that way, 2026-11-16)
                    for fid, sp, xy, z, _lon in view._parts(feats_all, bb, solved_only=True):
                        if len(xy) < 2:
                            continue
                        # (the view's z is the frame's up, the Earth's curve in it; the plans' z is the elevation's
                        # height, as their ground: a deck 9 km from the region's origin stood 6.2 m "under" its ground)
                        xy = np.asarray(xy, np.float64)
                        _la, _lo, h = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.asarray(z, np.float64))
                        decks.append((fid, xy[:, :2], np.asarray(h, np.float64), float(sp.width) / 2))
                except Exception as e:
                    print(f"{name}: decks: {type(e).__name__}: {e}", file=sys.stderr)
                    decks = []
                for _f, xy, z, _h in decks:     # (a check of the frames: a deck's ends stand on its abutments' ground)
                    xy = np.asarray(xy, np.float64)
                    g = ground(xy[[0, -1], :2])
                    deck_ground.extend((np.asarray(z, np.float64)[[0, -1]] - g).tolist())
                all_plans = [fp for plans, _b in cells.values() for fp in plans]
                clear += gm.clearance(all_plans, decks, names)

            def geo(rows, frame=frame):
                for r in rows:
                    if "x" in r and "lat" not in r:
                        la, lo, _ = frame.enu_to_geodetic(r["x"], r["y"], 0.0)
                        r["lat"], r["lon"] = round(float(la), 6), round(float(lo), 6)
            geo(junctions)
            geo(seams)
            geo(clear)
            geo([p[k] for p in profs for k in ("bump_at", "dev_at") if k in p])
        for planner in list(store.planners.values()):     # (any cell planned while measuring: no place's)
            with planner.lock:
                seen_cells.update(str(k) for k in planner.cells)
        s = gm.summary(junctions, seams, profs, clear, round(cells_cpu, 2))
        s["cells"] = n_cells
        s["deck_end_over_ground_median"] = float(np.median(deck_ground)) if deck_ground else None
        s["wall_s"] = round(time.perf_counter() - t0, 1)
        results[name] = s
        j, sm, g, c = s["junction"], s["seam"], s["grade"], s["clearance"]
        print(f"{name:22s} roads {s['roads']:5d}  junction steps >=30cm {j['bad']:4d}/{j['n']:5d} max {j['max']:5.2f}  "
              f"seams >=30cm {sm['bad']:3d}/{sm['n']:4d}  made steep {g['made_steep']:3d}  "
              f"bump p99 {s['bump']['p99_median']:.3f} (ground {s['bump']['ground_p99_median']:.3f})  "
              f"low decks {c['n']:3d} (through {c['through_deck']:2d}) worst {c['max']:4.1f}  cpu {cells_cpu:6.1f} s", flush=True)
        if a.out:               # (after every place: a long run stopped keeps the places it measured)
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out + ".tmp", "w", encoding="utf-8") as f:
                json.dump(results, f, indent=1)
            os.replace(a.out + ".tmp", a.out)


import numpy as np  # noqa: E402  (after sys.path: the server's numpy users)

if __name__ == "__main__":
    main()
