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
    with pcommon.comparing(a.a, a.b):
        return compare(a)


def compare(a):
    rep = pcommon.Report("facts", a.show)
    names = [n for n in a.places.split(",") if n] or None
    frozen = pcommon.corpus()["places"]
    for name in pcommon.comparison_places(a.a, a.b, "facts", names):
        indexes = []
        empty = []
        for root in (a.a, a.b):
            folder = os.path.join(root, "plans", name)
            path = os.path.join(folder, "index.json")
            if not os.path.isfile(path):
                rep.item(f"{root}/{name}", ["missing plan index for fact coverage"])
                continue
            index = pcommon.read_json(path)
            indexes.append(index)
            errors = pcommon.coverage_errors(index, frozen[name]["plan_cells"] if name in frozen else None)
            counts = index.get("road_features", {})
            if not errors:
                errors.extend(pcommon.missing_fact_cells(index["cells"], counts, os.path.join(root, "facts", name)))
            for cell in index.get("cells", []):
                if not os.path.isfile(os.path.join(folder, cell.replace("/", "_") + ".npz")):
                    errors.append(f"{cell}: plan file missing")
            if errors:
                rep.item(f"{root}/{name}", errors[:5])
            empty.append(not errors and bool(counts) and all(n == 0 for n in counts.values()))
        if len(indexes) == 2:
            for field in ("cells", "versions", "facts_format", "road_features"):
                if field == "road_features" and not all(field in index for index in indexes):
                    continue  # old manifests still record demanded cells and plan completeness
                if indexes[0].get(field) != indexes[1].get(field):
                    rep.item(name, [f"different {field}"])
        if empty == [True, True] and all(os.path.isdir(os.path.join(root, "facts", name))
                                         and not os.listdir(os.path.join(root, "facts", name)) for root in (a.a, a.b)):
            rep.item(name + " verified empty", [])
            continue
        pcommon.compare_trees(a.a, a.b, os.path.join("facts", name), rep)
    return rep.finish()


if __name__ == "__main__":
    sys.exit(main())
