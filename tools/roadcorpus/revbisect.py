"""revbisect.py PLACE METRIC --good REV --bad REV [--checks a,b] [--root ROOT] [--points REV,REV,...]

Finds the first first-parent commit of the dev history (server/ or native/ changed) between --good and --bad where
PLACE's METRIC changes from its --good value, scoring each revision with run.py's probe on the place's seed (offline,
the same data for every revision). METRIC is a path into the place JSON's summary, e.g. end_step.blocker or
deck_shape.wavy (a number); "worse" means any value different from the good revision's. --points only scores the
listed revisions (release points) and prints the metric for each, no search. A metric that flips back and forth
has no single first-bad commit: revscan.py scores every revision instead.

Results: ROOT/results/bisect/<sha12>/<place>.json (kept: a later bisect over the same range reuses them)."""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run  # noqa: E402


def metric(js, path):
    v = js.get("summary", {})
    for k in path.split("."):
        v = v.get(k) if isinstance(v, dict) else None
    return v


def score(root, place, rev, checks):
    server, sha = run.export_rev(root, rev)
    label = os.path.join("bisect", sha[:12])
    js = os.path.join(root, "results", label, place["id"] + ".json")
    if not os.path.exists(js):
        rc = run.run_place(place, server, root, label, False, checks, run.era_of(sha))
        if rc != 0:
            return sha, None
    with open(js) as f:
        return sha, json.load(f)


def first_parent(a, b):
    out = subprocess.check_output(["git", "-C", run.REPO, "log", "--first-parent", "--format=%H", f"{a}..{b}", "--",
                                   "server", "native"], text=True).split()
    return out[::-1]        # oldest first, b last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("place")
    ap.add_argument("metric")
    ap.add_argument("--good")
    ap.add_argument("--bad")
    ap.add_argument("--points")
    ap.add_argument("--checks")
    ap.add_argument("--root", default="~/bnge-tests/roadcorpus")
    a = ap.parse_args()
    root = os.path.expanduser(a.root)
    place = run.places(ids=a.place)[0]
    if a.points:
        for rev in a.points.split(","):
            sha, js = score(root, place, rev, a.checks)
            print(f"{rev:>14} {sha[:10]} {a.metric} = {None if js is None else metric(js, a.metric)}", flush=True)
        return
    _s, gj = score(root, place, a.good, a.checks)
    _s, bj = score(root, place, a.bad, a.checks)
    gv, bv = metric(gj, a.metric), metric(bj, a.metric)
    print(f"good {a.good}: {gv}; bad {a.bad}: {bv}", flush=True)
    if gv == bv:
        sys.exit("good and bad agree: nothing to bisect")
    revs = first_parent(a.good, a.bad)
    lo, hi = -1, len(revs) - 1          # revs[lo] good (-1 = a.good), revs[hi] bad
    while hi - lo > 1:
        mid = (lo + hi) // 2
        sha, js = score(root, place, revs[mid], a.checks)
        v = None if js is None else metric(js, a.metric)
        print(f"  {sha[:10]} {a.metric} = {v}", flush=True)
        if js is None:          # (a revision that does not build: step past it towards the bad end)
            revs.pop(mid)
            hi -= 1
            continue
        if v == gv:
            lo = mid
        else:
            hi = mid
    sha = revs[hi]
    msg = subprocess.check_output(["git", "-C", run.REPO, "log", "-1", "--format=%h %ad %s", "--date=short", sha],
                                  text=True).strip()
    print(f"FIRST CHANGE: {msg[:300]}")
    print(f"  before it ({revs[lo][:10] if lo >= 0 else a.good}): {gv}")


if __name__ == "__main__":
    main()
