#!/usr/bin/env python3
"""The 3D solve's steps (server/grade3d), each timed on its own over a junction's area: what it takes in, what it gives,
the seconds of each part.

    python tools/grade3d_bench.py --junction yokohama                    (every step built so far, a 3 km square)
    python tools/grade3d_bench.py --junction yokohama --size 6000 --steps network
    python tools/grade3d_bench.py --anchor 35.46,139.68 --at 35.4637,139.6788 --size 2000

The map data comes from Earth's cache (offline: nothing is fetched; --cache), the junction from server/evals/junctions.json
(--junctions) or --anchor / --at. The area is the square of --size metres round the junction's point in the planning
frame of its cell. Prints one line per step and part; --json writes them too."""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server")
STEPS = ("network", "layers")


def junction(a):
    """(aid, anchor (lat, lon), point (lat, lon))."""
    import geodesy
    if a.junction:
        with open(a.junctions, encoding="utf-8") as f:
            data = json.load(f)
        found = [j for j in (data if isinstance(data, list) else data["junctions"]) if j["name"] == a.junction]
        if not found:
            raise SystemExit("no junction %r in %s" % (a.junction, a.junctions))
        j = found[0]
        if j.get("at"):
            at = tuple(j["at"])
            anchor = tuple(j["anchor"]) if "anchor" in j else (round(float(at[0]), 2), round(float(at[1]), 2))
        else:
            anchor = tuple(j["anchor"])      # (a point by xy needs its anchor)
            la, lo, _ = geodesy.EnuFrame(*anchor).enu_to_geodetic(float(j["xy"][0]), float(j["xy"][1]), 0.0)
            at = (float(la), float(lo))
        return int(j.get("aid", 9)), anchor, at
    if not (a.anchor and a.at):
        raise SystemExit("--junction NAME, or --anchor lat,lon and --at lat,lon")
    return int(a.aid), a.anchor, a.at


def pair(s):
    la, lo = s.split(",")
    return float(la), float(lo)


class Clock:
    """Lines of (step, part, seconds, what): printed as they come, kept for --json."""

    def __init__(self):
        self.rows = []

    def __call__(self, step, part, t0, **what):
        dt = time.perf_counter() - t0
        self.rows.append(dict(step=step, part=part, seconds=round(dt, 3), **what))
        print("%-9s %-14s %8.2f s  %s" % (step, part, dt, "  ".join("%s %s" % kv for kv in what.items())), flush=True)
        return time.perf_counter()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--junction")
    ap.add_argument("--junctions", default=os.path.join(SERVER, "evals", "junctions.json"))
    ap.add_argument("--anchor", type=pair)
    ap.add_argument("--aid", default=9)
    ap.add_argument("--at", type=pair)
    ap.add_argument("--size", type=float, default=3000.0, help="m: the side of the square round the point")
    ap.add_argument("--steps", default=",".join(STEPS), help="the steps to run, in order: " + ", ".join(STEPS))
    ap.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth"))
    ap.add_argument("--json", help="write the lines here")
    a = ap.parse_args(argv)
    os.environ.update(BNE_SHAPES="1", BNGE_PLANCACHE="off", BNGE_BLOCKCACHE="off")
    sys.path.insert(0, SERVER)
    import numpy as np
    import blockworker
    import mapcells
    from grade3d import layers, network

    aid, anchor, at = junction(a)
    clock = Clock()
    t = time.perf_counter()
    state = blockworker._State(a.cache, True, True, True, True, True, airports=False, sea=False)
    blockworker._s = state
    state.anchor(aid, *anchor)
    planner = state.plan_store.planner_of_cell(mapcells.cell_of(*at))
    e, n, _ = planner.frame.geodetic_to_enu(np.array([at[0]]), np.array([at[1]]), 0.0)
    c = np.array([float(e[0]), float(n[0])])
    lo, hi = c - a.size / 2.0, c + a.size / 2.0
    t = clock("setup", "planner", t, at="%.5f,%.5f" % at, size="%.0f m" % a.size)
    steps = [s for s in a.steps.split(",") if s]
    if "network" in steps:
        members = network.area(planner, lo, hi)
        t = clock("network", "area (read)", t, members=len(members), points=sum(len(m.line) for m in members))
        import gradeshapes as gsh
        import linenet
        net = gsh.Net(members, linenet.Network())
        t = clock("network", "line network", t, lines=len(net.network.ln), pairs=net.network.ln.pairs)
        rel = network.relations(net)
        t = clock("network", "relations", t, joins=sum(r[2] == "join" for r in rel),
                  crossings=sum(r[2] == "cross" for r in rel))
        paths = network.chain(net, rel)
        lens = sorted(p.length for p in paths)
        t = clock("network", "chain", t, paths=len(paths), longest="%.0f m" % lens[-1],
                  median="%.0f m" % lens[len(lens) // 2], members_per_path="%.2f" % (len(members) / max(1, len(paths))))
        t0 = time.perf_counter()
        whole = network.build(members, net.network)
        t = clock("network", "build (all)", t0, paths=len(whole.paths), joins=len(whole.joins),
                  crossings=len(whole.crossings))
    if "layers" in steps:
        lay = layers.order(whole)
        t = clock("layers", "order", t, **layers.summary(lay))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(clock.rows, f, indent=1)


if __name__ == "__main__":
    main()
