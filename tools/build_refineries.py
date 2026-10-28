"""build_refineries.py REFINERIES.json: server/refineries.npz from an Overpass JSON export (out geom) of OpenStreetMap's
refineries and petrochemical plants (nwr["industrial"~"^(refinery|petrochemical)$"]): every area's rings (a way's own;
a multipolygon's member ways joined end to end into closed rings, outer and inner), as int32 millionths of a degree
with ring offsets and each ring's area index (even-odd per area). Nodes (no area) are left out. ODbL: a derived
database of OpenStreetMap (c) OpenStreetMap contributors (CREDITS.md)."""
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def chain(ways):
    """Ways (lists of (lon, lat)) joined end to end into closed rings; pieces that never close are dropped."""
    ways = [list(w) for w in ways if len(w) >= 2]
    rings = []
    while ways:
        cur = ways.pop(0)
        grown = True
        while cur[0] != cur[-1] and grown:
            grown = False
            for i, w in enumerate(ways):
                if w[0] == cur[-1]:
                    cur += w[1:]
                elif w[-1] == cur[-1]:
                    cur += w[::-1][1:]
                elif w[-1] == cur[0]:
                    cur = w[:-1] + cur
                elif w[0] == cur[0]:
                    cur = w[::-1][:-1] + cur
                else:
                    continue
                ways.pop(i)
                grown = True
                break
        if cur[0] == cur[-1] and len(cur) >= 4:
            rings.append(cur)
    return rings


def main(path):
    d = json.load(open(path, encoding="utf-8"))
    coords, ring_off, ring_area = [], [0], []
    areas = 0
    for e in d.get("elements", []):
        if e["type"] == "way":
            pts = [(g["lon"], g["lat"]) for g in e.get("geometry", [])]
            rings = chain([pts])
        elif e["type"] == "relation":
            rings = chain([[(g["lon"], g["lat"]) for g in m.get("geometry", [])]
                           for m in e.get("members", []) if m.get("type") == "way" and m.get("geometry")])
        else:
            continue
        if not rings:
            continue
        for r in rings:
            coords += r
            ring_off.append(len(coords))
            ring_area.append(areas)
        areas += 1
    c = np.round(np.array(coords, np.float64) * 1e6).astype(np.int32)
    out = os.path.join(ROOT, "server", "refineries.npz")
    np.savez_compressed(out, lonlat=c, ring_off=np.array(ring_off, np.int64), ring_area=np.array(ring_area, np.int32))
    print(f"{out}: {areas} areas, {len(ring_area)} rings, {len(c)} points, {os.path.getsize(out) / 1e6:.2f} MB")


if __name__ == "__main__":
    main(sys.argv[1])
