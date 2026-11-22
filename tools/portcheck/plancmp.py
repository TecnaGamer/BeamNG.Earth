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
    with pcommon.comparing(a.a, a.b):
        return compare(a)


def compare(a):
    rep = pcommon.Report("plans", a.show)
    frozen = pcommon.corpus()["places"]
    for name in pcommon.comparison_places(a.a, a.b, a.sub, list(filter(None, a.places.split(",")))):
        ia, ib = (os.path.join(r, a.sub, name, "index.json") for r in (a.a, a.b))
        if not os.path.exists(ia) and not os.path.exists(ib):
            rep.item(name, ["plan index missing on both sides"])
            continue
        if not (os.path.exists(ia) and os.path.exists(ib)):
            rep.only("A" if os.path.exists(ia) else "B", [name + " (no plans on the other side)"])
            continue
        xa, xb = pcommon.read_json(ia), pcommon.read_json(ib)
        for what in ("cells", "plans_format", "facts_format", "versions"):
            if xa.get(what) != xb.get(what):
                rep.item(f"{name} {what}", ["different inputs or format"])
        if a.sub == "plans":
            for side, index in (("A", xa), ("B", xb)):
                errors = pcommon.coverage_errors(index, frozen[name]["plan_cells"] if name in frozen else None)
                if errors:
                    rep.item(f"{name} {side}", errors[:5])
            if "road_features" in xa and "road_features" in xb and xa["road_features"] != xb["road_features"]:
                rep.item(name, ["road input counts differ"])
        pa, pb = xa["plans"], xb["plans"]
        if not pa and not pb:
            rep.item(name, ["no plans on either side"])
        for cell in sorted(set(pa) & set(pb)):
            diffs = []
            if pa[cell].get("complete") is not True or pb[cell].get("complete") is not True:
                diffs.append("plan is short or unverified")
            fa, fb = (os.path.join(r, a.sub, name, cell + ".npz") for r in (a.a, a.b))
            if os.path.exists(fa) and os.path.exists(fb):
                diffs += pcommon.npz_diff(fa, fb)
            elif os.path.exists(fa) != os.path.exists(fb):
                diffs.append("plan file on one side only")
            else:
                diffs.append("indexed plan missing on both sides")
            rep.item(f"{name} {cell}", diffs)
        rep.only("A", [f"{name} {c}" for c in sorted(set(pa) - set(pb))])
        rep.only("B", [f"{name} {c}" for c in sorted(set(pb) - set(pa))])
    return rep.finish()


if __name__ == "__main__":
    sys.exit(main())
