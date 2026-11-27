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
STEPS = ("network", "layers", "samples", "ground", "laws", "problem", "solve")


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
    ap.add_argument("--worst", type=int, default=0, help="list the N clearances most short after the solve")
    ap.add_argument("--firm", action="store_true", help="--worst: the firm clearances only (not the doubted)")
    ap.add_argument("--block", type=float, default=1500.0, help="m: the solve's block core (0: the whole in one solve)")
    ap.add_argument("--slowest", type=int, default=6, help="list the N slowest blocks")
    ap.add_argument("--workers", type=int, default=0, help="threads solving blocks (0: solve.WORKERS)")
    ap.add_argument("--classify", action="store_true", help="the firm crossings short, grouped by what they are")
    ap.add_argument("--law", action="append", default=[], metavar="NAME=VALUE",
                    help="set a grade3d law for this run, e.g. laws.CEIL_GRADE_X=0 (no grade ceiling)")
    a = ap.parse_args(argv)
    os.environ.update(BNE_SHAPES="1", BNGE_PLANCACHE="off", BNGE_BLOCKCACHE="off")
    sys.path.insert(0, SERVER)
    import numpy as np
    import blockworker
    import mapcells
    from grade3d import ground, laws, layers, network, problem, samples
    from grade3d import solve as solve3d
    for kv in a.law:            # (a law tried: module.NAME=float)
        name, value = kv.split("=", 1)
        mod, attr = name.split(".", 1)
        mods = dict(ground=ground, laws=laws, layers=layers, network=network, problem=problem, samples=samples,
                    solve=solve3d)
        if not hasattr(mods[mod], attr):
            raise SystemExit("no law %s" % name)
        setattr(mods[mod], attr, float(value))

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
    if "samples" in steps:
        sm = samples.sample(whole, lay)
        t = clock("samples", "sample", t, samples=len(sm), joins=len(sm.joins), crossings=len(sm.crossings),
                  steps_m="%.2f-%.2f" % (sm.h[sm.h > 0].min(), sm.h.max()))
    if "ground" in steps:
        raw = ground.read(ground.planner_elev(planner), sm)
        t = clock("ground", "read", t, samples=len(raw), range="%.0f-%.0f m" % (raw.min(), raw.max()))
        g = ground.smooth(sm, raw)
        t = clock("ground", "smooth", t, moved_p99="%.2f m" % np.percentile(np.abs(g - raw), 99),
                  moved_max="%.2f m" % np.abs(g - raw).max())
    if "laws" in steps:
        lw = laws.lay(whole, sm, g)
        t = clock("laws", "lay", t, **laws.summary(lw))
    if "problem" in steps:
        pr = problem.pose(whole, sm, lw)
        t = clock("problem", "pose", t, **pr.summary())
    if "solve" in steps:
        if a.block:
            sol = solve3d.solve_blocks(pr, sm, block_m=a.block, workers=a.workers or None)
            t = clock("solve", "blocks", t, **sol.diagnostics)
            for b in sorted(sol.per_block, key=lambda b: -b["seconds"])[:a.slowest]:
                print("  block %s  chains %d samples %d couplings %d rows %s  %.2f s %s rounds %s" % (
                    np.round(b["core"]).tolist(), b["chains"], b["samples"], b["couplings"], b["rows"], b["seconds"],
                    b["status"], b["rounds"]))
        else:
            sol = solve3d.solve(pr)
            d = sol.diagnostics
            t = clock("solve", "kernel", t, status=d.get("status"), rounds=d.get("rounds"),
                      rows=d.get("constraint_rows"), slack=d.get("slack_vars"))
        got = solve3d.measure(pr, sol.z)
        t = clock("solve", "measure", t, **{k: round(v, 3) if isinstance(v, float) else v for k, v in got.items()})
        if a.classify:
            # (per firm crossing short by 5 cm or more: its kinds, whether its two paths are joined anywhere, whether
            # it lies within 20 m of an end of either path)
            short_of = {}
            for (short, k) in solve3d.worst(pr, sol.z, len(pr.couplings), min_w=layers.W_CLEAR):
                xk = pr.why[k][1]
                short_of[xk] = max(short_of.get(xk, 0.0), short)
            joined = {(min(pa, pb), max(pa, pb)) for pa, _ia, pb, _ib in sm.joins}
            groups = {}
            for xk, short in short_of.items():
                if short < 0.05:
                    continue
                pa, ia, pb, ib, lay_ = sm.crossings[xk]
                na, nb = sm.off[pa + 1] - sm.off[pa], sm.off[pb + 1] - sm.off[pb]
                near = min(ia, na - 1 - ia) * sm.h[pa] <= 20 or min(ib, nb - 1 - ib) * sm.h[pb] <= 20
                u, d_ = (lay_.a, lay_.b) if lay_.upper == "a" else (lay_.b, lay_.a)
                key = (whole.members[u].kind, whole.members[d_].kind, "joined" if (min(pa, pb), max(pa, pb)) in joined
                       else "-", "near end" if near else "-")
                g = groups.setdefault(key, [0, 0, 0.0])
                g[0] += 1
                g[1] += short > 1.0
                g[2] = max(g[2], short)
            print("  firm crossings short: %d" % sum(g[0] for g in groups.values()))
            for key, (n_, big, mx) in sorted(groups.items(), key=lambda kv: -kv[1][0]):
                print("    %-48s %4d  (%d over 1 m, max %.2f m)" % (" / ".join(key), n_, big, mx))
        for short, k in solve3d.worst(pr, sol.z, a.worst, min_w=layers.W_CLEAR if a.firm else 0.0):
            pu, iu, pl, il, _kind, need = pr.couplings[k]
            _w, xk = pr.why[k]
            lay_ = sm.crossings[xk][4]
            zu, zl = sol.z[sm.off[pu] + iu], sol.z[sm.off[pl] + il]
            mu, ml = (lay_.a, lay_.b) if lay_.upper == "a" else (lay_.b, lay_.a)
            nu, nl = sm.off[pu + 1] - sm.off[pu], sm.off[pl + 1] - sm.off[pl]
            print("  short %6.2f m  need %5.2f  w %g (%s)  upper %s %s %s [%d/%d] z %.2f t %.2f lo %.2f   lower %s %s %s [%d/%d] z %.2f t %.2f hi %.2f  at %s" % (
                short, need, lay_.weight, lay_.why, whole.members[mu].key, whole.members[mu].kind, whole.members[mu].cls,
                iu, nu, zu, lw.t[sm.off[pu] + iu], lw.lo[sm.off[pu] + iu], whole.members[ml].key, whole.members[ml].kind,
                whole.members[ml].cls, il, nl, zl, lw.t[sm.off[pl] + il], lw.hi[sm.off[pl] + il],
                np.round(sm.xy[sm.off[pl] + il], 1)))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(clock.rows, f, indent=1)


if __name__ == "__main__":
    main()
