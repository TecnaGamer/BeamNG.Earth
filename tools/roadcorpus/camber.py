"""camber.py --server DIR --cache DIR --lat LAT --lon LON [--r M] --json OUT [--samples NPZ]

How the graded roads lean across, against the real roads in the elevation: along every drivable ground road round
the place, every STEP_M,
  built   the graded physics ground's cross slope over the middle of the carriageway (+-MID_OFF_M),
  road    the real road's: grading.bench_lean of the bare elevation (the most level strip beside the map line) read
          every FINE_STEP_M across at every sample (finer than the plans read it: grading.RURAL_BENCH_STEP_M,
          RURAL_BENCH_EVERY), smoothed along as the plans smooth it (grading.rural_lean); meaningful on lidar only,
  mid     the bare elevation's over +-MID_OFF_M of the map line itself (what a reading that trusts the line gets),
  rule    the design rule's lean into the bend (grading.bank, all of it),
  bend    the centreline's signed curvature over +-BEND_M (in a bend: tighter than BEND_R_M; straight: gentler than
          STRAIGHT_R_M).
The first and last END_M of a part are left out (a junction's mouth: the other road's ground).
In the bends a lean is "in" when it falls toward the inside by more than LEVEL, "out" when it falls away, else level.
share: the least-squares share of the rule's lean the real roads take in their bends (grading.RURAL_CURVE_SHARE).
Per road, and over all the roads the rural banking takes (length-weighted). Set the switches (BNE_RURAL_BANK ...) in
the environment to compare builds; a scratch cache only (probe.py's rule)."""
import argparse
import json
import logging
import os
import sys

STEP_M = 2.0
MIN_LEN_M = 120.0
END_M = 16.0
MID_OFF_M = 1.25
FINE_STEP_M = 0.5
BEND_M = 15.0
BEND_R_M = 150.0
STRAIGHT_R_M = 500.0
LEVEL = 0.01
SWITCHES = ("BNE_ROAD_FOLLOW", "BNE_BUMPS", "BNE_RURAL_BANK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--r", type=float, default=600.0)
    ap.add_argument("--json", required=True)
    ap.add_argument("--samples", help="write every sample of the roads (npz), for plots")
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

    def bare(xy):
        la, lo = to_ll(xy)
        return np.asarray(blockworker.ground(frame, la, lo, s.dem.sample(la, lo, grading.PROFILE_ZOOM)), np.float64)

    def graded(xy):
        la, lo = to_ll(xy)
        z = np.asarray(blockworker.ground(frame, la, lo, s.dem.sample(la, lo, grading.PROFILE_ZOOM)), np.float64)
        return np.asarray(g.apply(la, lo, z, grading.ROAD_LIFT), np.float64)

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
    offs = grading.bench_offsets(FINE_STEP_M)
    tot = {}
    rows, done, keep = [], set(), {}
    for _key, f in feats:
        specs = roads.road_specs(f)
        names = f.attrs.get("name") if hasattr(f, "attrs") and "name" in f.attrs else None
        for i in range(len(f)):
            fid = str(f.ids[i])
            sp = specs[i]
            if fid in done or sp is None or str(f.attrs["subtype"][i]) != "road" or sp.kind not in roads.BUILT:
                continue
            done.add(fid)
            for part, ring in enumerate(roads.ground_parts(f, i)):
                ring = np.asarray(ring, np.float64)
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                xy = np.stack([np.asarray(e), np.asarray(n)], 1)
                arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
                if arc[-1] < MIN_LEN_M:
                    continue
                t = np.arange(0.0, arc[-1], STEP_M)
                p = np.stack([np.interp(t, arc, xy[:, 0]), np.interp(t, arc, xy[:, 1])], 1)
                inside = (np.hypot(p[:, 0], p[:, 1]) <= a.r) & (t >= END_M) & (t <= t[-1] - END_M)
                h = int(round(BEND_M / STEP_M))
                if inside.sum() * STEP_M < MIN_LEN_M or len(p) < 2 * h + 3:
                    continue
                tan = np.gradient(p, axis=0)
                tan /= np.maximum(np.hypot(tan[:, 0], tan[:, 1]), 1e-9)[:, None]
                nrm = np.stack([-tan[:, 1], tan[:, 0]], 1)          # the left
                # (the bend: the heading's turn between BEND_M behind and BEND_M ahead, + to the left)
                ia, ib = np.clip(np.arange(len(p)) - h, 0, len(p) - 1), np.clip(np.arange(len(p)) + h, 0, len(p) - 1)
                da = p[np.arange(len(p))] - p[ia]
                db = p[ib] - p[np.arange(len(p))]
                turn = np.arctan2(da[:, 0] * db[:, 1] - da[:, 1] * db[:, 0], (da * db).sum(1))
                span = np.maximum((ib - ia) * STEP_M / 2.0, STEP_M)
                curv = turn / span
                curv[:h] = curv[len(p) - h:] = 0.0

                def lean(fn, off):
                    return (fn(p + nrm * off) - fn(p - nrm * off)) / (2.0 * off)   # (+ raises the left: grading's)

                built, mid = lean(graded, MID_OFF_M), lean(bare, MID_OFF_M)
                zc = np.stack([bare(p + nrm * o) for o in offs], 1)
                raw = grading.bench_lean(zc, FINE_STEP_M)
                road = grading.rural_lean(raw, STEP_M, (False, False))
                rule = grading.bank(p, sp, (False, False), (1, 1))
                rule = np.zeros(len(p)) if rule is None else np.asarray(rule, np.float64)
                m = inside
                bend, straight = m & (np.abs(curv) > 1.0 / BEND_R_M), m & (np.abs(curv) < 1.0 / STRAIGHT_R_M)
                sgn = np.sign(curv)                                  # (+ left bend: leaning in = the left low = -)

                def inout(x, sel=bend):
                    v = -x[sel] * sgn[sel]                           # (+ in)
                    return [int((v > LEVEL).sum()), int((np.abs(v) <= LEVEL).sum()), int((v < -LEVEL).sum())]

                def share(c3):
                    nn = max(sum(c3), 1)
                    return [round(c / nn, 3) for c in c3]

                def pct(x, q):
                    return round(100.0 * float(np.percentile(np.abs(x), q)), 2) if len(x) else None

                cls = sp.cls + ("_link" if getattr(sp, "link", False) else "")
                rural = bool(grading.rural_banked(sp))
                row = {"fid": fid, "part": part, "cls": cls, "name": str(names[i]) if names is not None else "",
                       "width_m": round(float(sp.width), 1), "urban": bool(sp.urban), "rural_banked": rural,
                       "len_m": round(float(m.sum() * STEP_M)), "bend_m": round(float(bend.sum() * STEP_M)),
                       "straight_m": round(float(straight.sum() * STEP_M)),
                       "seen": round(float(np.isfinite(raw[m]).mean()), 3),
                       "bend_in_level_out": {"built": share(inout(built)), "road": share(inout(road)),
                                             "mid": share(inout(mid))},
                       "abs_pct_p50_p90_max": {k_: [pct(v[m], 50), pct(v[m], 90), pct(v[m], 100)]
                                               for k_, v in (("built", built), ("road", road), ("mid", mid))},
                       "straight_abs_pct_p50_p90": {k_: [pct(v[straight], 50), pct(v[straight], 90)]
                                                    for k_, v in (("built", built), ("road", road), ("mid", mid))},
                       "built_minus_road_pct_rms": round(100.0 * float(np.sqrt(np.mean((built[m] - road[m]) ** 2))), 2),
                       "at": [round(float(v), 5) for v in (lambda ll: (ll[0][len(p) // 2], ll[1][len(p) // 2]))(to_ll(p))]}
                rows.append(row)
                for tk in (("all", cls) if rural else ()):
                    t_ = tot.setdefault(tk, {"len": 0, "bend": 0, "straight": 0, "built": [0, 0, 0], "road": [0, 0, 0],
                                             "mid": [0, 0, 0], "rr": 0.0, "r2": 0.0, "err2": 0.0, "seen": 0,
                                             "abs": {"built": [], "road": [], "mid": []},
                                             "sabs": {"built": [], "road": [], "mid": []}})
                    t_["len"] += int(m.sum())
                    t_["bend"] += int(bend.sum())
                    t_["straight"] += int(straight.sum())
                    t_["seen"] += int(np.isfinite(raw[m]).sum())
                    for k_, v in (("built", built), ("road", road), ("mid", mid)):
                        t_[k_] = [x + y for x, y in zip(t_[k_], inout(v))]
                        t_["abs"][k_].append(np.abs(v[m]))
                        t_["sabs"][k_].append(np.abs(v[straight]))
                    t_["rr"] += float((road[bend] * rule[bend]).sum())
                    t_["r2"] += float((rule[bend] * rule[bend]).sum())
                    t_["err2"] += float(((built[m] - road[m]) ** 2).sum())
                if a.samples:
                    keep[f"{fid}:{part}"] = np.stack([t, p[:, 0], p[:, 1], curv, built, road, mid, rule, raw,
                                                      m.astype(np.float64)], 1)
    def q3(xs):
        x = np.concatenate(xs) if xs else np.zeros(0)
        return [round(100.0 * float(np.percentile(x, q)), 2) for q in (50, 90, 100)] if len(x) else None

    summary = {}
    for tk, t_ in sorted(tot.items()):
        nn = max(t_["len"], 1)
        summary[tk] = {"len_m": round(t_["len"] * STEP_M), "bend_m": round(t_["bend"] * STEP_M),
                       "straight_m": round(t_["straight"] * STEP_M), "seen": round(t_["seen"] / nn, 3),
                       "bend_in_level_out": {k_: [round(c / max(sum(t_[k_]), 1), 3) for c in t_[k_]]
                                             for k_ in ("built", "road", "mid")},
                       "abs_pct_p50_p90_max": {k_: q3(t_["abs"][k_]) for k_ in ("built", "road", "mid")},
                       "straight_abs_pct_p50_p90_max": {k_: q3(t_["sabs"][k_]) for k_ in ("built", "road", "mid")},
                       "built_minus_road_pct_rms": round(100.0 * float(np.sqrt(t_["err2"] / nn)), 2),
                       "share": round(t_["rr"] / t_["r2"], 3) if t_["r2"] > 0 else None}
    out = {"place": [a.lat, a.lon, a.r], "zoom": grading.PROFILE_ZOOM,
           "switches": {k_: v for k_, v in sorted(os.environ.items()) if k_ in SWITCHES},
           "rural_banked": summary, "roads": sorted(rows, key=lambda r: -r["len_m"])}
    with open(a.json, "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    if a.samples:
        np.savez_compressed(a.samples, **keep)
    for tk, v in summary.items():
        print(tk, json.dumps(v))


if __name__ == "__main__":
    main()
