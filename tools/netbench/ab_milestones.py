#!/usr/bin/env python3
"""When each place reached its loading milestones, and how much it had downloaded by then, in the A/B runs
(AB_ROOT/{before,after}, bench.sh) as a markdown table. Prints to stdout.

Milestones (netprobe's marks, s after the teleport): anchor (the car on its provisional ground), graded (the graded
first block in), ground (that block with its final photo: also its photo analyses, background CPU work), roads (roads
and buildings round the player), trees, far (the distant land), loaded (all of them: the mod's loading line ends).
A run with a line (NB_LINK) gives a player's times on it; without, the network is free and the times are the CPU's.
"""
import json
import os

ROOT = os.environ["AB_ROOT"]
RUNS = ("before", "after")
MILESTONES = ("anchor", "graded", "ground", "roads", "trees", "far", "loaded")


def mark(marks, name):
    return marks.get(name if name == "anchor" else f"{name}_s")


def load(run):
    """{label: {milestone: (s, MB by then)}}; MB is None for runs without the teleport's time (older netprobe)."""
    path = os.path.join(ROOT, run)
    lines = open(os.path.join(path, "net.log")).read().splitlines()
    out = {}
    for rec in map(json.loads, open(os.path.join(path, "places.jsonl"))):
        rows = [l.split(" ") for l in lines[rec["start_line"]:rec["end_line"]]]
        got = {}
        for name in MILESTONES:
            t = mark(rec["marks"], name)
            if t is None:
                continue
            mb = None
            if rec.get("t0") is not None:
                mb = sum(int(r[3]) for r in rows if float(r[0]) <= rec["t0"] + t) / 1e6
            got[name] = (t, mb)
        out[rec["label"]] = got
    return out


def cell(v):
    if v is None:
        return "–"
    t, mb = v
    return f"{t:.0f} s" if mb is None else f"{t:.0f} s · {mb:.0f} MB"


def change(b, a):
    if b is None or a is None or not b[0]:
        return ""
    return f" ({(a[0] - b[0]) / b[0] * 100:+.0f} %)"


def main():
    data = {r: load(r) for r in RUNS}
    labels = [k for k in data["before"] if k in data["after"]]
    print("| Place | Build | " + " | ".join(MILESTONES) + " |")
    print("|---|---|" + "---:|" * len(MILESTONES))
    for label in labels:
        b, a = data["before"][label], data["after"][label]
        print(f"| {label} | before | " + " | ".join(cell(b.get(m)) for m in MILESTONES) + " |")
        print("| | after | " + " | ".join(cell(a.get(m)) + change(b.get(m), a.get(m)) for m in MILESTONES) + " |")
    for run in RUNS:
        sums = {m: [data[run][k][m][0] for k in labels if m in data[run][k]] for m in MILESTONES}
        data[run]["*"] = {m: (sum(v), None) for m, v in sums.items() if len(v) == len(labels) and v}
    print(f"| **sum** | before | " + " | ".join(cell(data["before"]["*"].get(m)) for m in MILESTONES) + " |")
    print("| | after | " + " | ".join(cell(data["after"]["*"].get(m)) + change(data["before"]["*"].get(m),
                                       data["after"]["*"].get(m)) for m in MILESTONES) + " |")


if __name__ == "__main__":
    main()
