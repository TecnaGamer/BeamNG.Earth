#!/usr/bin/env python3
"""Replay the interchange pipeline (server/ix, SPEC.md) of one junction offline: the game and the eval tools are not started.

    python tools/ixreplay.py --junction yokohama --out ix-out
    python tools/ixreplay.py --junction dallas_high_five --system 1fffb880 --out ix-out
    python tools/ixreplay.py --anchor 42.17,13.83 --at 42.1743,13.8243 --out ix-out [--ring 1]
    python tools/ixreplay.py --junction yokohama --region 14/14500/6460 --system 39a6e5ec   (the region an eval's view.log names)
    python tools/ixreplay.py --ladder C:/Users/loren/earth-evals/rr/integration/all24   (the interchanges of an eval's logs, by tier)

The map data comes from the cache an eval used (Earth's cache, offline: nothing is fetched; --cache), the junction from
tools/junctions.json of the eval tree (--junctions) or --anchor / --at. The region of the junction's point (the cell the
game's planner is asked for; --ring N adds the cells round it) is run by ix.pipeline.region with BNE_SHAPES=1, and every step
of every interchange it holds is timed, as in the eval's view.log ("ix step" lines).

It writes, in --out:
  ix/                 the artifacts of every step (BNE_IX_CACHE), the input of ixcheck
  steps.json          one record per interchange per step: step, system key, pieces, crossings, seconds, status, the
                      step's own profile top 20 lines (profile_<step>.txt as well)
  profile_<step>.txt  cProfile top 20 (cumulative) of the step's own code over the whole replay; find is the region's find
                      (the paths' and the extent's share of finding the interchanges), embank the region's embankments
  ixcheck.json        what ixcheck found (python -m ixcheck.run ix)
and prints the table of each interchange (pieces, crossings, seconds per step, the step limits it is over), then the region's
pieces and crossings and the ixcheck counts. --system KEY_PREFIX solves only the interchanges whose key starts with it
(the others are found, as the region does, and not solved).
"""
import argparse
import contextlib
import cProfile
import io
import json
import logging
import os
import pstats
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server")
DEFAULT_JUNCTIONS = os.path.join(os.path.dirname(REPO), "BeamNG.Dakar-upeval", "tools", "junctions.json")
STEPS = ("paths", "extent", "order", "terrain", "datum", "heights")
LIMITS_S = {"paths": 30.0, "extent": 60.0, "order": 10.0, "terrain": 30.0, "datum": 10.0, "heights": 120.0}
LINE = re.compile(r"ix step: (\w+) key (\w+) pieces (\d+) crossings (\d+) ([\d.]+) s (ok.*?|failed.*?)\s*@ (\S+) system (\w+)")
REGION = re.compile(r"ix region: (\S+) systems (\d+) ([\d.]+) s, load ([\d.]+) s, scan ([\d.]+) s")


class StepLog(logging.Handler):
    """The 'ix step' lines of the pipeline, as records."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.steps, self.regions, self.other = [], [], []

    def emit(self, rec):
        msg = rec.getMessage()
        m = LINE.search(msg)
        if m:
            st, key, p, c, t, ok, hk, sy = m.groups()
            self.steps.append(dict(step=st, key=key, pieces=int(p), crossings=int(c), seconds=float(t), status=ok.strip(),
                                   region=hk, system=sy))
            return
        m = REGION.search(msg)
        if m:
            self.regions.append(dict(region=m.group(1), systems=int(m.group(2)), seconds=float(m.group(3)),
                                     load=float(m.group(4)), scan=float(m.group(5))))
            return
        if rec.levelno >= logging.WARNING:
            self.other.append(msg)


class Profiles:
    """One cProfile per step, on only while that step's own code runs (the outermost wrapped call: a step's callee that is
    wrapped for another step is counted in the first)."""

    def __init__(self):
        self.p = {}
        self.depth = 0

    def wrap(self, owner, name, step):
        fn = getattr(owner, name)
        prof = self.p.setdefault(step, cProfile.Profile())

        def run(*a, **k):
            if self.depth:
                return fn(*a, **k)
            self.depth = 1
            prof.enable()
            try:
                return fn(*a, **k)
            finally:
                prof.disable()
                self.depth = 0
        run.__name__ = getattr(fn, "__name__", name)
        setattr(owner, name, run)

    def top(self, step, n=20):
        prof = self.p.get(step)
        if prof is None:
            return ""
        out = io.StringIO()
        try:
            pstats.Stats(prof, stream=out).sort_stats("cumulative").print_stats(n)
        except TypeError:       # (a step that never ran: no stats)
            return ""
        txt = out.getvalue()
        return txt[txt.find("ncalls") - 0:] if "ncalls" in txt else txt


def junction(a):
    """(aid, anchor (lat, lon), point (lat, lon)) of the junction asked for."""
    import geodesy
    if a.junction:
        with open(a.junctions, encoding="utf-8") as f:
            data = json.load(f)
        found = [j for j in (data if isinstance(data, list) else data["junctions"]) if j["name"] == a.junction]
        if not found:
            raise SystemExit("no junction %r in %s" % (a.junction, a.junctions))
        j = found[0]
        aid = int(j.get("aid", 9))
        if j.get("at"):
            at = tuple(j["at"])
            anchor = tuple(j["anchor"]) if "anchor" in j else (round(float(at[0]), 2), round(float(at[1]), 2))
        else:
            anchor = tuple(j["anchor"])      # (a point by xy needs its anchor, as the eval does)
            fr = geodesy.EnuFrame(*anchor)
            la, lo, _ = fr.enu_to_geodetic(float(j["xy"][0]), float(j["xy"][1]), 0.0)
            at = (float(la), float(lo))
        return aid, anchor, at
    if not (a.anchor and a.at):
        raise SystemExit("--junction NAME, or --anchor lat,lon and --at lat,lon")
    return int(a.aid), a.anchor, a.at


def pair(s):
    la, lo = s.split(",")
    return float(la), float(lo)


def cells(at, ring, region=None):
    import mapcells
    if region:
        z, x, y = (int(v) for v in region.split("/"))
        c = mapcells.CellKey(z, x, y)
    else:
        c = mapcells.cell_of(at[0], at[1])
    out = [c]
    if ring:
        for dx in range(-ring, ring + 1):
            for dy in range(-ring, ring + 1):
                if dx or dy:
                    out.append(mapcells.CellKey(c.z, c.x + dx, c.y + dy))
    return out


TIERS = ((20, "tiny"), (100, "small"), (500, "medium"), (2000, "large"), (10 ** 9, "huge"))


def ladder(folder):
    """The interchanges of an eval's junction logs (<folder>/<junction>/after/view.log): per interchange its pieces and
    crossings (the most any step line gave), where the eval ran it (its region) and its steps' seconds; tiers by crossings."""
    import collections
    rows = {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name, "after", "view.log")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = LINE.search(ln)
                if m:
                    st, _k, p, c, t, ok, hk, sy = m.groups()
                    r = rows.setdefault((name, sy), dict(junction=name, system=sy, region=hk, pieces=0, crossings=0, t={}, bad=[]))
                    r["pieces"], r["crossings"] = max(r["pieces"], int(p)), max(r["crossings"], int(c))
                    r["t"][st] = float(t)
                    if not ok.startswith("ok"):
                        r["bad"].append(st)
    tiers = collections.defaultdict(list)
    for r in rows.values():
        tiers[next(n for lim, n in TIERS if r["crossings"] <= lim)].append(r)
    for _lim, n in TIERS:
        L = sorted(tiers[n], key=lambda r: (r["crossings"], r["junction"], r["system"]))
        print("== %s: %d interchanges in %d junctions" % (n, len(L), len({r["junction"] for r in L})))
        for r in (L if n != "tiny" else L[-6:]):
            print("  %5d crossings %5d pieces  %-28s %s  %s  %s" % (r["crossings"], r["pieces"], r["junction"], r["region"],
                  r["system"][:8], " ".join("%s=%.1f" % (k[:3], v) for k, v in r["t"].items())) + (" FAILED " + ",".join(r["bad"]) if r["bad"] else ""))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--junction", help="a name of the junctions file")
    ap.add_argument("--junctions", default=DEFAULT_JUNCTIONS)
    ap.add_argument("--anchor", type=pair)
    ap.add_argument("--aid", default=9)
    ap.add_argument("--at", type=pair)
    ap.add_argument("--region", help="the cell to run, z/x/y (an eval's 'ix step' line names it), instead of the point's")
    ap.add_argument("--ladder", help="an eval's output folder: print its interchanges by tier of crossings and stop")
    ap.add_argument("--ring", type=int, default=0, help="cells round the point's cell run as well")
    ap.add_argument("--system", help="solve only the interchanges whose key starts with this")
    ap.add_argument("--out", default="ix-replay")
    ap.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth"))
    ap.add_argument("--no-profile", action="store_true")
    ap.add_argument("--no-check", action="store_true", help="skip ixcheck")
    a = ap.parse_args(argv)
    if a.ladder:
        ladder(a.ladder)
        return 0

    os.environ.update(BNE_SHAPES="1", BNGE_PLANCACHE="off", BNGE_BLOCKCACHE="off")
    sys.path.insert(0, SERVER)
    os.makedirs(a.out, exist_ok=True)
    ix_dir = os.path.abspath(os.path.join(a.out, "ix"))
    os.makedirs(ix_dir, exist_ok=True)
    aid, anchor, at = junction(a)

    import blockworker
    import gradeshapes as gsh
    import grading
    from ix import cache, owner, pipeline, s1_paths, s2_extent, s3_order, s4_terrain, s5_datum, s6_heights
    assert grading.SHAPES_ON
    cache.ROOT = ix_dir

    handler = StepLog()
    lg = logging.getLogger("earthd.grading")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(handler)
    lg.propagate = False

    prof = Profiles()
    if not a.no_profile:
        prof.wrap(s2_extent, "find", "find")
        prof.wrap(gsh, "pieces", "paths")
        prof.wrap(s1_paths, "run", "paths")
        prof.wrap(s2_extent, "run", "extent")
        prof.wrap(s3_order, "run", "order")
        prof.wrap(gsh, "sample", "terrain")
        prof.wrap(s4_terrain, "run", "terrain")
        prof.wrap(s5_datum, "run", "datum")
        prof.wrap(s6_heights, "solve", "heights")
        prof.wrap(s6_heights, "run", "heights")
        prof.wrap(s6_heights, "region_of", "embank")

    if a.system:           # (the interchanges found and not wanted are given back as they are, the region finds them all)
        real = owner.interchange

        def only(planner, net, ix, hk, clock):
            if not owner.key_of(planner, net, ix.sysm).startswith(a.system):
                return {"key": owner.key_of(planner, net, ix.sysm), "roads": {}, "failed": False, "relaxed": 0}
            return real(planner, net, ix, hk, clock)
        owner.interchange = only

    t0 = time.perf_counter()
    state = blockworker._State(a.cache, True, True, True, True, True, airports=False, sea=False)
    blockworker._s = state           # (the module's one state: the plans' ground reads its DEM)
    state.anchor(aid, *anchor)
    store = state.plan_store
    t_state = time.perf_counter() - t0

    regions = []
    for home in cells(at, a.ring, a.region):
        t1 = time.perf_counter()
        planner = store.planner_of_cell(home)
        got = pipeline.region(planner, home, str(home))
        regions.append(dict(region=str(home), roads=len(got), seconds=round(time.perf_counter() - t1, 2)))

    # the profile of a step: its top 20 as text files and in the records
    tops = {}
    for st in STEPS + ("find", "embank"):
        tops[st] = prof.top(st)
        if tops[st]:
            with open(os.path.join(a.out, "profile_%s.txt" % st), "w", encoding="utf-8") as f:
                f.write(tops[st])
    with open(os.path.join(a.out, "steps.json"), "w", encoding="utf-8") as f:
        json.dump(dict(junction=a.junction, point=at, state_s=round(t_state, 2), regions=regions, steps=handler.steps,
                       region_lines=handler.regions, warnings=handler.other[:50],
                       profiles={k: v.splitlines()[:25] for k, v in tops.items() if v}), f, indent=1)

    # the table: one row per interchange
    rows = {}
    for s in handler.steps:
        r = rows.setdefault(s["system"], dict(system=s["system"], pieces=0, crossings=0, t={}, bad=[]))
        r["pieces"] = max(r["pieces"], s["pieces"])
        r["crossings"] = max(r["crossings"], s["crossings"])
        r["t"][s["step"]] = r["t"].get(s["step"], 0.0) + s["seconds"]
        if not s["status"].startswith("ok"):
            r["bad"].append("%s %s" % (s["step"], s["status"][:60]))
    print("junction %s at %.5f, %.5f; map state %.1f s; regions %s" % (a.junction, at[0], at[1], t_state, regions))
    print("%-10s %6s %9s  %s  over" % ("interchange", "pieces", "crossings", "  ".join("%8s" % s[:8] for s in STEPS)))
    for r in sorted(rows.values(), key=lambda r: (r["crossings"], r["system"])):
        over = [s for s in STEPS if r["t"].get(s, 0.0) > LIMITS_S[s]]
        print("%-10s %6d %9d  %s  %s%s" % (r["system"][:10], r["pieces"], r["crossings"],
                                           "  ".join("%8.2f" % r["t"].get(s, 0.0) for s in STEPS), ",".join(over),
                                           "  " + "; ".join(r["bad"]) if r["bad"] else ""))
    for r in handler.regions:
        print("region %s: %d systems %.2f s (load %.2f, scan %.2f)" % (r["region"], r["systems"], r["seconds"], r["load"], r["scan"]))
    print("region: %d interchanges, %d pieces, %d crossings" % (len(rows), sum(r["pieces"] for r in rows.values()),
                                                               sum(r["crossings"] for r in rows.values())))
    for w in handler.other[:10]:
        print("warning:", w[:200])

    if not a.no_check:
        from ixcheck import load, run
        scanned = load.scan(ix_dir)
        checked = run.check_all(scanned)
        lines, fail_lines, counts = run.report(checked)
        print("\n".join(lines))
        print("fails (first 20):\n" + ("\n".join(fail_lines) if fail_lines else "  none"))
        for path, why in scanned["errors"]:
            print("did not load: %s (%s)" % (path, why))
        with open(os.path.join(a.out, "ixcheck.json"), "w", encoding="utf-8") as f:
            json.dump({"counts": counts, "findings": [run._plain(x) for _a, fs in checked for x in fs]}, f, indent=1,
                      sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
