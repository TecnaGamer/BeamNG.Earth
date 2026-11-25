"""relief.py --server DIR --cache DIR --lat LAT --lon LON [--r M] --json OUT

How much of the land's relief the graded roads keep: along every drivable ground road round the place, the graded
physics ground (what a car drives on) against the bare elevation (on lidar: the real road), both less the bare
elevation's own trend over TREND_M (the hill the road climbs), sampled every STEP_M. kept = rms(graded detrended) /
rms(bare detrended): 1.0 keeps every crest and dip, 0 flattens them all. Per road class, length-weighted. Set the
switches (BNE_ROAD_FOLLOW ...) in the environment to compare builds; a scratch cache only (probe.py's rule)."""
import argparse
import json
import logging
import math
import os
import sys

STEP_M = 2.0
TREND_M = 150.0
MIN_LEN_M = 120.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--r", type=float, default=350.0)
    ap.add_argument("--json", required=True)
    a = ap.parse_args()
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    cache = os.path.realpath(os.path.expanduser(a.cache))
    if cache == real or cache.startswith(real + os.sep):
        sys.exit("refused: the player's cache")
    os.makedirs(cache, exist_ok=True)
    sys.path.insert(0, os.path.realpath(a.server))
    import numpy as np
    import blockworker
    import geodesy
    import grading
    import roads

    blockworker.init(cache, False, True, True, True, False, None, logging.ERROR, False, True)
    s = blockworker._s
    g, _ = s.anchor(1, a.lat, a.lon)
    frame = geodesy.EnuFrame(a.lat, a.lon)

    def to_ll(xy):
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(la, np.float64), np.asarray(lo, np.float64)

    def bare(la, lo):
        return np.asarray(blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15)), np.float64)

    def graded(la, lo):
        z = bare(la, lo)
        return np.asarray(g.apply(la, lo, z, grading.ROAD_LIFT), np.float64) if g is not None else z

    feats, seen = [], set()
    for tx, ty in roads.tiles_near(0.0, 0.0, a.r + 150.0):
        for k in roads.cells_of_tile(frame, tx, ty):
            if str(k) in seen:
                continue
            seen.add(str(k))
            f = s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
            if f is not None and len(f):
                feats.append((str(k), f))
    feats.sort(key=lambda kv: kv[0])
    by_cls, rows, done = {}, [], set()
    k = max(1, int(round(TREND_M / STEP_M)))
    for _key, f in feats:
        specs = roads.road_specs(f)
        names = f.attrs.get("name") if hasattr(f, "attrs") and "name" in f.attrs else None
        for i in range(len(f)):
            fid = str(f.ids[i])
            sp = specs[i]
            if fid in done or sp is None or str(f.attrs["subtype"][i]) != "road" or sp.kind not in roads.BUILT:
                continue
            done.add(fid)
            for ring in roads.ground_parts(f, i):
                ring = np.asarray(ring, np.float64)
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                xy = np.stack([np.asarray(e), np.asarray(n)], 1)
                arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
                if arc[-1] < MIN_LEN_M:
                    continue
                t = np.arange(0.0, arc[-1], STEP_M)
                p = np.stack([np.interp(t, arc, xy[:, 0]), np.interp(t, arc, xy[:, 1])], 1)
                inside = np.hypot(p[:, 0], p[:, 1]) <= a.r
                if inside.sum() * STEP_M < MIN_LEN_M:
                    continue
                la, lo = to_ll(p)
                zb, zg = bare(la, lo), graded(la, lo)
                if len(zb) < 2 * k:
                    continue
                pad = np.r_[np.full(k // 2, zb[0]), zb, np.full(k - k // 2 - 1, zb[-1])]
                trend = np.convolve(pad, np.ones(k) / k, mode="valid")
                hb, hg = (zb - trend)[inside], (zg - trend)[inside]
                rb, rg = float(np.sqrt(np.mean(hb * hb))), float(np.sqrt(np.mean(hg * hg)))
                ln = float(inside.sum() * STEP_M)
                cls = sp.cls + ("_link" if getattr(sp, "link", False) else "")
                name = str(names[i]) if names is not None else ""
                c = by_cls.setdefault(cls, [0.0, 0.0, 0.0])
                c[0] += ln
                c[1] += ln * rb * rb
                c[2] += ln * rg * rg
                rows.append({"fid": fid, "cls": cls, "name": name, "len_m": round(ln), "bare_rms_m": round(rb, 3),
                             "graded_rms_m": round(rg, 3), "kept": round(rg / rb, 3) if rb > 1e-6 else None,
                             "max_flat_m": round(float(np.max(np.abs(hb - hg))), 2),
                             "at": [round(float(la[len(la) // 2]), 5), round(float(lo[len(lo) // 2]), 5)]})
    out = {"place": [a.lat, a.lon, a.r], "switches": {k_: v for k_, v in sorted(os.environ.items())
                                                      if k_ in ("BNE_ROAD_FOLLOW", "BNE_BUMPS", "BNE_RURAL_BANK")},
           "classes": {c: {"len_m": round(v[0]), "kept": round(math.sqrt(v[2] / v[1]), 3) if v[1] > 0 else None}
                       for c, v in sorted(by_cls.items())},
           "roads": sorted(rows, key=lambda r: -r["len_m"])}
    with open(a.json, "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    print(json.dumps(out["classes"]))


if __name__ == "__main__":
    main()
