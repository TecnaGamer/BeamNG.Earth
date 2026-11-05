"""How well the photo's colour tells asphalt from the rest on roads whose surface IS mapped (task 010).

    python tools/photo_surface_eval.py [--lat LAT --lon LON --around N] [--list-misses K] [--cell-dir DIR]

Offline: reads the map data cells and the z17 regional tiles already in the cache (~/.cache/beamng-earth), never the
network. Every road of photosurface.PHOTO_CLASSES with a surface value (OpenStreetMap's, else Overture's) is measured as
if it had none, and the verdict (paved / not) is set against what the map says (roads.surface_kind: paved or not).
Prints the confusion matrix, precision and recall of "paved", and the misses with their statistics.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
os.environ["BNE_PHOTO_SURFACE"] = "1"
import numpy as np  # noqa: E402

import mapcells  # noqa: E402
import mapdata  # noqa: E402
import netfetch  # noqa: E402
import photosurface  # noqa: E402
import roads  # noqa: E402


def cells(md, lat, lon, around):
    c = mapcells.cell_of(lat, lon)
    return [mapcells.CellKey(c.z, c.x + dx, c.y + dy) for dy in range(-around, around + 1)
            for dx in range(-around, around + 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=50.52754)
    ap.add_argument("--lon", type=float, default=16.27370)
    ap.add_argument("--around", type=int, default=6, help="cells each way")
    ap.add_argument("--list-misses", type=int, default=15)
    a = ap.parse_args()
    md = mapdata.MapData(mapdata.DEFAULT_CACHE, netfetch.Fetcher(offline=True))
    rows, seen, skipped, nomap = [], set(), 0, 0
    for key in cells(md, a.lat, a.lon, a.around):
        try:
            f = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        except Exception:
            skipped += 1
            continue
        if f is None or not len(f):
            continue
        cls = f.attrs.get("class")
        for i in range(len(f)):
            c = str(cls[i])
            fid = str(f.ids[i])
            if c not in photosurface.PHOTO_CLASSES or fid in seen:
                continue
            surf = roads._map_surface_value(f, i)
            if surf is None:
                continue
            seen.add(fid)
            truth = roads.surface_kind(c, surf)
            best = None
            miss = False
            for part in f.parts(i):
                for ring in part:
                    st, m = photosurface.measure(ring)
                    miss |= m
                    if st is not None and (best is None or st["n"] > best["n"]):
                        best = st
            if best is None:
                nomap += 1
                continue
            rows.append((fid[:8], c, str(surf), truth, photosurface.classify(best), best))
    print(f"cells unreadable {skipped}; roads with a mapped surface but no usable photo samples {nomap}; measured {len(rows)}")
    for label, want in (("paved", {"paved"}), ("unpaved (gravel/dirt/cobble)", {"gravel", "dirt", "cobble"})):
        sel = [r for r in rows if r[3] in want]
        yes = sum(1 for r in sel if r[4] == "paved")
        print(f"truth {label}: {len(sel)} roads, called paved {yes}")
    tp = sum(1 for r in rows if r[3] == "paved" and r[4] == "paved")
    fp = sum(1 for r in rows if r[3] != "paved" and r[4] == "paved")
    fn = sum(1 for r in rows if r[3] == "paved" and r[4] != "paved")
    print(f"paved: TP {tp} FP {fp} FN {fn}  precision {tp / max(tp + fp, 1):.3f}  recall {tp / max(tp + fn, 1):.3f}")
    by = {}
    for r in rows:
        by.setdefault((r[1], r[3]), []).append(r)
    for (c, t), rs in sorted(by.items()):
        print(f"  {c:13s} mapped {t:7s}: {len(rs):4d} roads, called paved {sum(1 for r in rs if r[4] == 'paved'):4d}")
    print("verdicts by mapped surface:")
    for t in ("paved", "gravel", "dirt", "cobble"):
        c = {}
        for r in rows:
            if r[3] == t:
                c[r[4]] = c.get(r[4], 0) + 1
        print(f"  mapped {t:7s}: {dict(sorted(c.items(), key=lambda kv: str(kv[0])))}")
    bad = [r for r in rows if (r[3] == "paved") != (r[4] == "paved")]
    bad.sort(key=lambda r: (r[3] != "paved", -r[5]["n"]))
    for r in bad[:a.list_misses]:
        s = r[5]
        print(f"  MISS {r[0]} {r[1]:12s} surface={r[2]:14s} truth={r[3]:6s} verdict={r[4]}  sat {s['sat']:.3f} exg {s['exg']:+.3f} "
              f"L {s['lum']:.0f} n {s['n']}")


if __name__ == "__main__":
    main()
