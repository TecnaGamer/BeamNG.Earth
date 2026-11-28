#!/usr/bin/env python3
"""roadsys_heights.py: BNE_ROADSYS=heights (the road rewrite's M2, server/roadsys.py chain_heights) on every bridge chain
of a box, through the server's own chain store (roads.ChainStore / BridgeProfiles: the chains as the road tiles solve
them), for the M2 measurements: per chain its read (cells, rounds), kernel CPU, decks matched, floors (met, relaxed),
the deck heights against today's and the terrain targets; and the same bytes twice (--reverse, a second process).

    python3 tools/roadsys_heights.py --cache SCRATCH_CACHE --lat LAT --lon LON --half M --out OUT.json
                                     [--reverse] [--online] [--server-dir TREE/server]
    python3 tools/roadsys_heights.py --compare A.json B.json

The cache must be a scratch folder (never the player's ~/.cache/beamng-earth); cells and DEM tiles it lacks are fetched
into it only with --online. The ground is the plans' own (the DEM at grading.PROFILE_ZOOM) for both the profiles and
"today's" stage 1 (no grading here: the measurements are of the reads, the kernel and the decks' heights)."""
import argparse
import hashlib
import json
import os
import sys
import time


def compare(a, b):
    A, B = (json.load(open(p)) for p in (a, b))
    ca, cb = A["chains"], B["chains"]
    common = sorted(set(ca) & set(cb))
    same = sum(ca[k]["digest"] == cb[k]["digest"] for k in common)
    print(f"{len(common)} chains in both ({len(ca)} / {len(cb)}); identical heights {same}; targets "
          f"{A['pins_digest']} / {B['pins_digest']}")
    sa, sb = A["squares"], B["squares"]
    print("squares identical:", sum(sa[k]["key"] == sb.get(k, {}).get("key") for k in sa), "of", len(sa), len(sb))
    if A["pins_digest"] != B["pins_digest"]:
        return 1
    for k in common:
        if ca[k]["digest"] != cb[k]["digest"]:
            print("  differs:", k)
    return 0 if same == len(common) == len(ca) == len(cb) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--half", type=float, default=400.0)
    ap.add_argument("--out")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--server-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
    ap.add_argument("--compare", nargs=2)
    a = ap.parse_args()
    if a.compare:
        return compare(*a.compare)
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    cache = os.path.realpath(a.cache)
    if cache == real or cache.startswith(real + os.sep):
        ap.error("a scratch cache only, never the player's")
    os.environ["BNE_ROADSYS"] = "heights"
    sys.path.insert(0, os.path.abspath(a.server_dir))
    import npyguard
    npyguard.apply()
    import numpy as np
    import dem as demmod
    import geodesy
    import grading
    import mapcells
    import mapdata
    import netfetch
    import revisit
    import roads
    import roadsys
    fetcher = netfetch.Fetcher(offline=not a.online, stats_dir=os.path.join(cache, "netstats"))
    md = mapdata.MapData(cache, fetcher=fetcher)
    dem = demmod.Dem(cache, fetcher=fetcher)

    def elev(la, lo):
        return np.asarray(dem.sample(np.asarray(la, np.float64), np.asarray(lo, np.float64), grading.PROFILE_ZOOM),
                          np.float64)
    roadsys.GROUND = elev

    def cell_fn(key):
        return md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
    recs = {}
    inner = roadsys.chain_heights

    def sink(store, comp, parts, ground_ll=None):
        t0 = time.process_time()
        w0 = time.perf_counter()
        s0 = dict(roadsys.STATS)
        try:
            got = inner(store, comp, parts, ground_ll)
        except Exception:
            import traceback
            traceback.print_exc()
            raise
        today = {k: np.asarray(parts[k][2], np.float64) for k in comp if k in parts}
        row = {"cpu_s": round(time.process_time() - t0, 4), "wall_s": round(time.perf_counter() - w0, 4),
               "parts": len(comp)}
        for k in ("squares", "square_memo", "square_cells", "unmatched"):
            row[k] = roadsys.STATS[k] - s0[k]
        if got is None:
            row["digest"] = "none"
        else:
            dmax, dsum, dn = 0.0, 0.0, 0
            for k in sorted(got["deck_z"], key=str):
                z = got["deck_z"][k]
                if k in today and len(today[k]) == len(z):
                    d = np.abs(z - today[k])
                    dmax = max(dmax, float(d.max()))
                    dsum += float(d.sum())
                    dn += len(d)
            row["digest"] = got["digest"]
            row["deck_dz_max_m"] = round(dmax, 3)
            row["deck_dz_mean_m"] = round(dsum / max(dn, 1), 3)
            row["squares_used"] = [list(x) for x in got["squares"]]
        recs[str(min(comp, key=str))] = row
        return got
    roadsys.chain_heights = sink
    cs = roads.ChainStore(roads.BridgeProfiles, cell_fn, elev)
    st = cs.store_at(a.lat, a.lon)
    frame = st.frame
    e0, n0, _ = frame.geodetic_to_enu(a.lat, a.lon, 0.0)
    box = (float(e0) - a.half, float(n0) - a.half, float(e0) + a.half, float(n0) + a.half)
    la0, lo0, _ = frame.enu_to_geodetic(box[0], box[1], 0.0)
    la1, lo1, _ = frame.enu_to_geodetic(box[2], box[3], 0.0)
    keys = sorted(mapcells.cells_for_bbox(float(la0), float(lo0), float(la1), float(lo1)), key=str)
    if a.reverse:
        keys.reverse()
    t0 = time.perf_counter()
    c0 = time.process_time()
    n = 0
    with revisit.scope() as b:
        for k in keys:
            feats = cell_fn(k)
            got = st.parts([feats], box)
            n += len(got)
    t1 = time.perf_counter()
    with revisit.scope() as b2:
        la_, lo_ = np.array([la0, la1], np.float64), np.array([lo0, lo1], np.float64)
        pins = roadsys.pins_near(float(la_.min()), float(lo_.min()), float(la_.max()), float(lo_.max()), 28.0, cell_fn,
                                 elev)
    ph = hashlib.sha256()
    for p in pins:
        for arr in (p[0], p[1], p[2], p[3], p[5], p[6]):
            ph.update(np.ascontiguousarray(arr, np.float64).tobytes())
    sqinfo = {}
    for mk, sqo in roadsys._squares.items():
        sqinfo["%d_%d" % mk[1]] = json.loads(sqo.info()[0])
    out = {"lat": a.lat, "lon": a.lon, "half": a.half, "reverse": a.reverse, "parts": n, "complete": b.complete,
           "pins": len(pins), "pin_points": int(sum(len(p[0]) for p in pins)), "pins_digest": ph.hexdigest()[:16],
           "pins_complete": b2.complete, "pins_s": round(time.perf_counter() - t1, 2),
           "pin_dz_max_m": round(max([float(np.abs(p[2] * p[5]).max()) for p in pins] or [0.0]), 3),
           "squares": sqinfo,
           "seconds": round(time.perf_counter() - t0, 2), "cpu_s": round(time.process_time() - c0, 2),
           "stats": roadsys.STATS, "chains": recs}
    with open(a.out, "w") as f:
        json.dump(out, f, sort_keys=True, indent=0)
    ch = [r for r in recs.values() if r["digest"] != "none"]
    print(f"{n} parts, {len(recs)} chains ({len(ch)} solved), {out['seconds']} s, cpu {out['cpu_s']} s; pins "
          f"{out['pins']} ({out['pin_points']} points, {out['pins_s']} s, dz max {out['pin_dz_max_m']} m); squares "
          f"{len(sqinfo)}; stats {json.dumps(roadsys.STATS)}")
    md.close()
    fetcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
