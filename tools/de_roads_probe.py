"""Before / after of germanroads on real map cells: how many roads find an official axis, and how their widths,
lanes and surfaces change. Reads the shards from BNE_DE_ROADS_DIR (a build_de_roads.py output).

    BNE_DE_ROADS_DIR=shards python tools/de_roads_probe.py --at 49.2394,7.0024 --cells 2 --out probe.json
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import numpy as np  # noqa: E402

import germanroads  # noqa: E402
import mapcells  # noqa: E402
import mapdata as mapdatamod  # noqa: E402
import roads  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", action="append", required=True, help="lat,lon (repeatable)")
    ap.add_argument("--cells", type=int, default=1, help="cells round each point (1 = 3x3)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache-dir", default=os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth"))
    a = ap.parse_args()
    md = mapdatamod.MapData(a.cache_dir)
    rows = []
    for at in a.at:
        lat, lon = map(float, at.split(","))
        c = mapcells.cell_of(lat, lon)
        for dx in range(-a.cells, a.cells + 1):
            for dy in range(-a.cells, a.cells + 1):
                key = mapcells.CellKey(c.z, c.x + dx, c.y + dy)
                feats = md.cell(key, layers=("roads",)).layers.get("roads")
                if feats is None or not len(feats):
                    continue
                germanroads.ON = False
                before = roads._road_specs(feats, None, False)
                germanroads.ON = True
                after = roads._road_specs(feats, None, False)
                for i, (b, n) in enumerate(zip(before, after)):
                    if b is None or n is None:
                        continue
                    s = feats.coord_off[feats.ring_off[feats.part_off[i]]]
                    e = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
                    mid = feats.lonlat[(s + e) // 2]
                    ln = float(roads._lengths(feats, i)[0])
                    rows.append({"cell": str(key), "id": str(feats.ids[i]), "cls": b.cls, "len": round(ln, 1),
                                 "oneway": bool(b.oneway), "urban": bool(b.urban),
                                 "w0": round(b.width, 2), "w1": round(n.width, 2), "l0": b.lanes, "l1": n.lanes,
                                 "k0": b.kind, "k1": n.kind, "at": [round(float(mid[1]), 6), round(float(mid[0]), 6)],
                                 "changed": (b.width, b.lanes, b.kind) != (n.width, n.lanes, n.kind)})
                print(key, len(feats), "roads", flush=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh)
    by = defaultdict(lambda: [0.0, 0.0, 0.0, []])
    for r in rows:
        s = by[r["cls"]]
        s[0] += r["len"]
        s[1] += r["len"] if r["changed"] else 0.0
        if r["w0"] != r["w1"]:
            s[2] += r["len"]
            s[3].append(r["w1"] - r["w0"])
    print(f"{'class':14} {'km':>7} {'changed':>8} {'width':>7} {'mean dw':>8}")
    for cls, (L, ch, cw, dws) in sorted(by.items(), key=lambda kv: -kv[1][0]):
        print(f"{cls:14} {L / 1000:7.1f} {ch / max(L, 1):8.0%} {cw / max(L, 1):7.0%} "
              f"{(np.mean(dws) if dws else 0):8.2f}")
    print("kinds", Counter((r["k0"], r["k1"]) for r in rows if r["k0"] != r["k1"]))


if __name__ == "__main__":
    main()
