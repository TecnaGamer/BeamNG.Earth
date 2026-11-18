#!/usr/bin/env python3
"""Buildings on the roads: how many of a place's building footprints stand on the drawn carriageways, before and after
the road clearance (buildings.road_clearance; the user, 2026-11-12: "have you looked at houses blocking the road
sometimes?").

    python3 tools/bldroadscan.py LAT LON [--n 2] [--name X]
    python3 tools/bldroadscan.py --places FILE [--n 2]      (one "name lat lon" a line, # comments; a summary table)

For each road tile within N tiles of the point (2 N x 2 N tiles of 256 m), the tile's buildings as the road tiles make
them (buildings.road_tile_items, with the land cover's town test and the measured widths: the server's road specs)
and its ground carriageways (buildings.carriageway_segments: road_specs' widths, the placement, the tapers) are set
against each other (earthfast.footprint_road_overlap: each drawn footprint's depth into the asphalt and its area on
it), with the clearance off and then on. Per place: buildings, those reaching more than TOL_M into a carriageway, more
than BLOCK_M (a lane's worth of a car's width), the area on the asphalt (m2), split mapped (OSM) / machine-learned
(Overture's ML sources); the clearance's outcomes (trimmed, left out, kept: through / deep / unclean). The worst
footprints after go to OUT/<name>.json (lat, lon, depth, area, source) to look at (tools/ortho_hi.py, streetview.py).

Reads the real cache (~/.cache/beamng-earth, or --cache) like the server would, fetching what a place lacks; no roof
photos (BNE_ROOF_PHOTO off: a roof's colour moves no wall). Default OUT: ~/bnge-tests/bldroad/scan.
"""
import argparse
import json
import logging
import os
import sys
import time

os.environ.setdefault("BNE_ROOF_PHOTO", "off")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import numpy as np  # noqa: E402

TOL_M = 0.3
BLOCK_M = 1.0
NARROW_M = 3.0          # m: half the narrowest two-way carriageway (two 3 m lanes)


def measure(B, fast, items, cw):
    """Per clearable item: (spec, depth, area on the asphalt, half width of the road it is deepest into)."""
    specs = [s for s, _g0, _g1 in items if B._clearable(s)]
    if not specs or cw is None:
        return [(s, -np.inf, 0.0, np.nan) for s in specs]
    rings = [np.asarray(B.drawn_ring(s), np.float64)[:, :2] for s in specs]
    off = np.r_[0, np.cumsum([len(r) for r in rings])].astype(np.int64)
    over, depth, half = fast._ef.footprint_road_overlap(np.ascontiguousarray(np.concatenate(rings)), off, cw[0], cw[1],
                                                        0.5)
    return list(zip(specs, depth, over, half))


def summary(rows):
    out = {}
    for src in ("osm", "ml"):
        sel = [(d, a, h) for s, d, a, h in rows if bool(s.ml) == (src == "ml")]
        out[src] = {"n": len(sel), "over": sum(d > TOL_M for d, _, _ in sel),
                    "block": sum(d > BLOCK_M for d, _, _ in sel),
                    "area": round(float(sum(a for d, a, _ in sel if d > TOL_M)), 1),
                    # (of those over TOL_M, the ones whose wall stands NARROW_M or more off the road's centre line:
                    # clear of a narrower street on the same line, the width drawn is the cause; nearer: the line or
                    # the outline)
                    "wide": sum(d > TOL_M and h - d >= NARROW_M for d, _, h in sel)}
    return out


def scan(st, bw, B, fast, geodesy, roads, name, lat, lon, n, out_dir):
    frame = geodesy.EnuFrame(lat, lon)
    urban = st.surface.urban if st.surface is not None else None

    def ground(la, lo):
        return bw.ground(frame, la, lo, st.dem.sample(la, lo, 15))
    before, after, stats, worst = [], [], {}, []
    t0 = time.time()
    for tx in range(-n, n):
        for ty in range(-n, n):
            x0, y0, x1, _y1 = roads.tile_bbox(tx, ty)
            cds = [st.md.cell(k, layers=("roads",), kind="demand") for k in roads.cells_of_tile(frame, tx, ty, margin=0.0)]
            cw = B.carriageway_segments(frame, [c.layers.get("roads") for c in cds], x0, y0, x1 - x0, urban_fn=urban,
                                        widths=st.widths, force=True)
            B.ROAD_CLEAR_ON = False
            items, _ = B.road_tile_items(frame, tx, ty, st.md, ground, urban_fn=urban, widths=st.widths)
            before += measure(B, fast, items, cw)
            B.ROAD_CLEAR_ON = True
            items, _ = B.road_tile_items(frame, tx, ty, st.md, ground, urban_fn=urban, widths=st.widths,
                                         clear_stats=stats)
            rows = measure(B, fast, items, cw)
            after += rows
            for s, d, a, h in rows:
                if d > TOL_M:
                    c = np.asarray(s.ring, np.float64)[:, :2].mean(0) + [x0, y0]
                    la, lo, _ = frame.enu_to_geodetic(c[0], c[1], 0.0)
                    worst.append({"lat": round(float(la), 6), "lon": round(float(lo), 6), "depth": round(float(d), 2),
                                  "half": round(float(h), 2),
                                  "area": round(float(a), 1), "ml": bool(s.ml), "cls": s.cls, "roof": s.roof,
                                  "footprint": round(float(abs(B._area2(np.asarray(s.ring)[:, :2])) / 2), 1)})
    worst.sort(key=lambda r: -r["depth"])
    res = {"name": name, "lat": lat, "lon": lon, "tiles": (2 * n) ** 2, "s": round(time.time() - t0, 1),
           "before": summary(before), "after": summary(after), "actions": stats, "worst": worst[:40]}
    with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
        json.dump(res, f, indent=1)
    return res


def line(r):
    b, a = r["before"], r["after"]

    def cell(x):
        return f"{x['over']:4d}/{x['block']:4d}/{x['area']:7.0f}/{x.get('wide', 0):3d}w"
    act = r["actions"]
    acts = " ".join(f"{k}={v}" for k, v in sorted(act.items()))
    return (f"{r['name']:<18} osm {b['osm']['n']:5d} {cell(b['osm'])} -> {cell(a['osm'])}   ml {b['ml']['n']:5d} "
            f"{cell(b['ml'])} -> {cell(a['ml'])}   [{acts}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", nargs="?", type=float)
    ap.add_argument("lon", nargs="?", type=float)
    ap.add_argument("--name", default=None)
    ap.add_argument("--places", default=None)
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/beamng-earth"))
    ap.add_argument("--out", default=os.path.expanduser("~/bnge-tests/bldroad/scan"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    places = []
    if args.places:
        for ln in open(args.places):
            ln = ln.split("#")[0].split()
            if len(ln) >= 3:
                places.append((ln[0], float(ln[1]), float(ln[2])))
    elif args.lat is not None:
        places.append((args.name or f"{args.lat:.5f}_{args.lon:.5f}", args.lat, args.lon))
    else:
        ap.error("LAT LON or --places")
    os.makedirs(args.out, exist_ok=True)
    import blockworker as bw
    import buildings as B
    import fast
    import geodesy
    import roads
    st = bw._State(args.cache, False, True, True, True, True)
    bw._s = st
    print("counts: over TOL_M / over BLOCK_M / m2 on the asphalt / w: of those over, clear of a 6 m street on the same"
          " line; before -> after the clearance")
    for name, lat, lon in places:
        try:
            r = scan(st, bw, B, fast, geodesy, roads, name, lat, lon, args.n, args.out)
        except Exception as ex:         # (one place's download failing: the others go on)
            logging.exception("%s failed", name)
            print(f"{name:<18} FAILED {type(ex).__name__}: {ex}", flush=True)
            continue
        print(line(r), flush=True)


if __name__ == "__main__":
    main()
