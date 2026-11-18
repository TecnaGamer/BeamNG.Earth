#!/usr/bin/env python3
"""factcmp.py A B [--places a,b]: two builds' cell facts (build.py's OUT/facts: level ranges, road specs), item by item.

Each item ({kind, cell, extra, versions, data}) is compared as JSON values, floats exactly (repr: -0.0 is not 0.0), key
order included (the stored form is what players keep). Exit 0: identical; 1: an item differs; 2: items made on one
side only."""
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
    a = ap.parse_args(argv)
    rep = pcommon.Report("facts", a.show)
    names = [n for n in a.places.split(",") if n] or None
    for name in names or sorted({n for r in (a.a, a.b) for n in _ls(os.path.join(r, "facts"))}):
        pcommon.compare_trees(a.a, a.b, os.path.join("facts", name), rep)
    return rep.finish()


def _ls(d):
    return os.listdir(d) if os.path.isdir(d) else []


if __name__ == "__main__":
    sys.exit(main())
