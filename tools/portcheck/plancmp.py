#!/usr/bin/env python3
"""plancmp.py A B [--places a,b] [--sub splans]: two builds' road plans (build.py's OUT/plans, or a session's
OUT/splans), cell by cell.

Every array of each cell's plan file bit for bit (names, dtypes, shapes, bytes; the largest difference of a float
array that moved) and its JSON part (the plans' fields, keeps, covers, opens, decks) value for value; each cell's
completeness (a plan made short of an input on one side is a difference of inputs, not of code). Exit 0: identical;
1: a cell differs; 2: cells planned on one side only."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcommon  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--places", default="")
    ap.add_argument("--show", type=int, default=20)
    ap.add_argument("--sub", default="plans", help="plans (build.py's) or splans (session.py's: the session's own run)")
    a = ap.parse_args(argv)
    rep = pcommon.Report("plans", a.show)
    for name in _places(a):
        ia, ib = (os.path.join(r, a.sub, name, "index.json") for r in (a.a, a.b))
        if not (os.path.exists(ia) and os.path.exists(ib)):
            rep.only("A" if os.path.exists(ia) else "B", [name + " (no plans on the other side)"])
            continue
        xa, xb = pcommon.read_json(ia), pcommon.read_json(ib)
        for what in ("plans_format", "facts_format", "versions"):
            if xa.get(what) != xb.get(what):
                print(f"NOTE {name}: {what} {xa.get(what)} vs {xb.get(what)} (other inputs or formats)")
        pa, pb = xa["plans"], xb["plans"]
        for cell in sorted(set(pa) & set(pb)):
            diffs = []
            if pa[cell]["complete"] != pb[cell]["complete"]:
                diffs.append(f"complete {pa[cell]['complete']} vs {pb[cell]['complete']}")
            fa, fb = (os.path.join(r, a.sub, name, cell + ".npz") for r in (a.a, a.b))
            if os.path.exists(fa) and os.path.exists(fb):
                diffs += pcommon.npz_diff(fa, fb)
            elif os.path.exists(fa) != os.path.exists(fb):
                diffs.append("plan file on one side only")
            rep.item(f"{name} {cell}", diffs)
        rep.only("A", [f"{name} {c}" for c in sorted(set(pa) - set(pb))])
        rep.only("B", [f"{name} {c}" for c in sorted(set(pb) - set(pa))])
    return rep.finish()


def _places(a):
    if a.places:
        return [n for n in a.places.split(",") if n]
    got = set()
    for r in (a.a, a.b):
        d = os.path.join(r, a.sub)
        if os.path.isdir(d):
            got |= set(os.listdir(d))
    return sorted(got)


if __name__ == "__main__":
    sys.exit(main())
