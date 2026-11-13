#!/usr/bin/env python3
"""Detail keep-up: how far ahead of the car the small details appear, and how long after their road tile (the user,
2026-11-13: sidewalks, trees, street furniture, signs, street lights, parked cars, fences and road markings "break
immersion a lot when they can't keep up with the player, even when terrain and roads do").

The mod's recorder (mod .../beamngEarth/keepup.lua, a debug switch: off unless started) logs each detail's FIRST
appearance while a drive runs, the car's trace and, every 0.5 s, what should stand within 50 m of the car and does not.
tools/nyc_speed.py --keepup starts it before each timed drive and saves <run>/keepup.csv after it; this tool reads those.

  python3 tools/nyc_speed.py --keepup --out ~/bnge-tests/ku --route midtown --speeds 40,70,110
  python3 tools/keepup.py ~/bnge-tests/ku/*/keepup.csv            (a table per layer and run; --json for the numbers)

Per layer (trees.lua groups by name, roads.lua mesh parts, the stock parking system's cars), over the details the
car passed within PASS_M (closest approach on its trace):
  n          details that first appeared during the drive and were passed
  lead_m     median / p10 distance from the car when they appeared (the item's own; a tile part's: its square)
  <100m      share that first appeared within 100 m of the car, ahead of it (pop-in the driver sees)
  late       share that appeared only after the car had passed its closest point
  road_s     median / p90 seconds after the road mesh of their road tile was made (tile_s: after their own data came)
  miss/km    distinct details that should have stood within 50 m of the car and did not (the probe), per km driven
"""
import argparse
import json
import math
import os
import sys

import numpy as np

PASS_M = 40.0           # a detail counts as passed when the car came this close (its trace's closest point)
TILE_HALF = 120.0       # a zoom-17 geotile's half side (m, ~39 deg): a tile part's square
NEAR_M = 100.0

LIGHTS = ("lamp", "light", "lantern", "polelamp", "lampglow", "high_mast", "streetlight")
SIGNS = ("stop", "giveway", "speed", "signal", "sign", "wallsign", "town", "vienna", "yield", "exit", "mile", "route",
         "dir", "kantstolpe", "se_", "de_", "fr_j", "rrcross", "rrflash")
FENCES = ("fence", "hedge", "wall", "guardrail", "jersey", "cable", "barrier", "railing", "bollard")
PART_LAYER = {"road": "road+markings", "walk": "sidewalks", "signs": "sign meshes", "bld": "buildings",
              "bld2": "buildings", "bld3": "buildings", "bld4": "buildings", "wire": "power lines"}
LAYERS = ["road+markings", "sidewalks", "trees", "lights", "signs", "sign meshes", "fences", "guardrails", "furniture",
          "parked cars", "buildings"]


def item_layer(group, kind):
    """trees.lua's group -> a layer (kind 1 vegetation tile, 2 street furniture rows, 3 guard rails)."""
    g = group.lower()
    if kind == 3:
        return "guardrails"
    if kind == 1 and not any(g.startswith(p) for p in ("fence", "sub_fence")):
        return "trees"
    if any(k in g for k in LIGHTS):
        return "lights"
    if any(k in g for k in FENCES):
        return "fences"
    if any(g.startswith(k) or k in g for k in SIGNS):
        return "signs"
    return "furniture"


def load(path):
    rows = {k: [] for k in "TIPKAQM"}
    groups, t0 = [], 0.0
    with open(path) as f:
        for line in f:
            p = line.rstrip("\n").split(",")
            if not p or not p[0]:
                continue
            if p[0] == "H":
                t0 = float(p[1])
                groups = p[2].split(";") if len(p) > 2 and p[2] else []
            elif p[0] in rows:
                rows[p[0]].append(p[1:])
    return rows, groups, t0


class Frame:
    """lat / lon -> local metres (equirectangular at a reference point: the drive's few km)."""
    def __init__(self, lat, lon):
        self.lat0, self.lon0 = lat, lon
        self.kx = 111320.0 * math.cos(math.radians(lat))

    def xy(self, lat, lon):
        return (np.asarray(lon, float) - self.lon0) * self.kx, (np.asarray(lat, float) - self.lat0) * 110540.0


def closest(trace_xy, trace_t, px, py, chunk=4000):
    """Each point's closest trace point: (distance, time)."""
    tx, ty = trace_xy
    n = len(px)
    dmin, tmin = np.full(n, np.inf), np.zeros(n)
    for a in range(0, n, chunk):
        bx, by = px[a:a + chunk, None], py[a:a + chunk, None]
        d2 = (bx - tx[None, :]) ** 2 + (by - ty[None, :]) ** 2
        k = np.argmin(d2, axis=1)
        dmin[a:a + chunk] = np.sqrt(d2[np.arange(len(k)), k])
        tmin[a:a + chunk] = trace_t[k]
    return dmin, tmin


def pct(a, p):
    a = np.asarray(a, float)
    a = a[np.isfinite(a)]
    return round(float(np.percentile(a, p)), 1) if len(a) else None


def analyze(path, moving_ms=5.0):
    rows, groups, t0 = load(path)
    tr = np.array([[float(v) for v in r] for r in rows["T"]]) if rows["T"] else np.zeros((0, 5))
    if len(tr) < 10:
        return {"path": path, "error": "no trace"}
    sp = np.hypot(tr[:, 3], tr[:, 4])
    mov = np.nonzero(sp > moving_ms)[0]
    if len(mov) == 0:
        return {"path": path, "error": "never moved"}
    ta, tb = tr[mov[0], 0], tr[mov[-1], 0]          # the drive: first to last moving sample
    fr = Frame(tr[0, 1], tr[0, 2])
    txy = fr.xy(tr[:, 1], tr[:, 2])
    seg = np.hypot(np.diff(txy[0]), np.diff(txy[1]))
    inwin = (tr[1:, 0] >= ta) & (tr[1:, 0] <= tb) & (seg < 200)         # (a teleport's jump left out)
    km = float(seg[inwin].sum()) / 1000.0
    win = (tr[:, 0] >= ta) & (tr[:, 0] <= tb)
    wxy, wt = (txy[0][win], txy[1][win]), tr[win, 0]
    out = {"path": path, "km": round(km, 2), "drive_s": round(tb - ta, 1),
           "mph": round(float(np.median(sp[win])) * 2.237, 1), "layers": {}}
    per = {}

    def add(layer, d, ra, ta_, dpass, lates, tapp):
        e = per.setdefault(layer, {"d": [], "near": 0, "late": 0, "ra": [], "ta": [], "n": 0})
        for i in range(len(d)):
            if not (ta - 1.0 <= tapp[i] <= tb) or dpass[i] > PASS_M:
                continue
            e["n"] += 1
            late = bool(lates[i])
            if late:
                e["late"] += 1
            elif d[i] < NEAR_M:
                e["near"] += 1
            if not late:
                e["d"].append(d[i])
            if 0 <= ra[i] < 600:
                e["ra"].append(ra[i])
            if 0 <= ta_[i] < 600:
                e["ta"].append(ta_[i])

    # trees.lua items: t, group, lat, lon, d, along, kind, roadAge, tileAge, speed
    if rows["I"]:
        a = np.array([[float(v) for v in r] for r in rows["I"]])
        px, py = fr.xy(a[:, 2], a[:, 3])
        dpass, tpass = closest(wxy, wt, px, py)
        lay = np.array([item_layer(groups[int(g) - 1] if 0 < int(g) <= len(groups) else "?", int(k))
                        for g, k in zip(a[:, 1], a[:, 6])])
        for L in set(lay):
            m = lay == L
            add(L, a[m, 4], a[m, 7], a[m, 8], dpass[m], a[m, 0] > tpass[m], a[m, 0])
    # roads.lua parts: t, what, gkey, lat, lon, d, along, boxd, roadAge, tileAge, speed
    if rows["P"]:
        P = [r for r in rows["P"] if r[1] in PART_LAYER]
        if P:
            a = np.array([[float(r[0]), float(r[3]), float(r[4]), float(r[7]), float(r[8]), float(r[9])] for r in P])
            px, py = fr.xy(a[:, 1], a[:, 2])
            dpass, tpass = closest(wxy, wt, px, py)
            dpass = np.maximum(dpass - TILE_HALF, 0)          # (to the tile's square, roughly)
            lay = np.array([PART_LAYER[r[1]] for r in P])
            for L in set(lay):
                m = lay == L
                # (a part's lead: the car's distance to its square; late: made with the car already in it)
                add(L, a[m, 3], a[m, 4], a[m, 5], dpass[m], a[m, 3] < 1.0, a[m, 0])
    # parked cars: t, lat, lon, d, along, speed
    if rows["K"]:
        a = np.array([[float(v) for v in r] for r in rows["K"]])
        px, py = fr.xy(a[:, 1], a[:, 2])
        dpass, tpass = closest(wxy, wt, px, py)
        neg = -np.ones(len(a))
        add("parked cars", a[:, 3], neg, neg, dpass, a[:, 0] > tpass, a[:, 0])
        # (every parked car moved in, passed or not: the stock system's own spacing)
        inw = (a[:, 0] >= ta) & (a[:, 0] <= tb)
        out["parked_all"] = {"n": int(inw.sum()), "d_median": pct(a[inw, 3], 50), "d_p10": pct(a[inw, 3], 10),
                             "near_share": round(float((a[inw, 3] < NEAR_M).mean()), 3) if inw.any() else None}
    # the probe's misses: t, group, lat, lon, d, along, kind, key#i, reason
    miss = {}
    for r in rows["M"]:
        t = float(r[0])
        if not (ta <= t <= tb):
            continue
        g = groups[int(r[1]) - 1] if 0 < int(r[1]) <= len(groups) else "?"
        miss.setdefault(item_layer(g, int(r[6])), set()).add(r[7])
    q = np.array([[float(v) for v in r] for r in rows["Q"]]) if rows["Q"] else np.zeros((0, 14))
    qm = (q[:, 0] >= ta) & (q[:, 0] <= tb) & (q[:, 3] > moving_ms) if len(q) else np.zeros(0, bool)
    qq = q[qm]
    # Q: t, lat, lon, speed, nTree, nProp, nRail, noTile, noSkin, noRoad, noWalk, noSigns, noVeg, noProps
    probe = {}
    if len(qq):
        for name, col in (("trees_missing", 4), ("furniture_missing", 5), ("guardrails_missing", 6),
                          ("road_tile_missing", 7), ("skin_missing", 8), ("road_mesh_missing", 9),
                          ("sidewalks_missing", 10), ("sign_meshes_missing", 11), ("veg_tile_missing", 12),
                          ("furniture_rows_missing", 13)):
            probe[name] = round(float((qq[:, col] > 0).mean()), 3)
        # (details missing while the road under the car is there: the user's case)
        ok_road = (qq[:, 7] == 0) & (qq[:, 8] == 0) & (qq[:, 9] == 0)
        probe["road_ok_share"] = round(float(ok_road.mean()), 3)
        probe["detail_missing_with_road_ok"] = round(float(((qq[:, 4] + qq[:, 5] + qq[:, 6] + qq[:, 10] + qq[:, 11]
                                                             + qq[:, 12] + qq[:, 13]) > 0)[ok_road].mean()), 3) \
            if ok_road.any() else None
    out["probe"] = probe
    for L, e in per.items():
        n = e["n"]
        out["layers"][L] = {"n": n, "lead_m_median": pct(e["d"], 50), "lead_m_p10": pct(e["d"], 10),
                            "near_share": round(e["near"] / n, 3) if n else None,
                            "late_share": round(e["late"] / n, 3) if n else None,
                            "road_s_median": pct(e["ra"], 50), "road_s_p90": pct(e["ra"], 90),
                            "tile_s_median": pct(e["ta"], 50), "tile_s_p90": pct(e["ta"], 90)}
    for L, s in miss.items():
        out["layers"].setdefault(L, {"n": 0})["miss_per_km"] = round(len(s) / max(km, 0.1), 1)
        out["layers"][L]["miss_n"] = len(s)
    return out


def table(results):
    lines = []
    for r in results:
        if "error" in r:
            lines.append(f"{r['path']}: {r['error']}")
            continue
        lines.append(f"\n{r.get('label') or r['path']}: {r['mph']} mph, {r['km']} km in {r['drive_s']} s; probe {r['probe']}")
        if r.get("parked_all"):
            lines.append(f"  parked cars moved in (all): {r['parked_all']}")
        lines.append(f"  {'layer':14} {'n':>6} {'lead_m med/p10':>15} {'<100m':>6} {'late':>6} {'road_s med/p90':>15} "
                     f"{'tile_s med/p90':>15} {'miss/km':>8}")
        for L in LAYERS + sorted(set(r["layers"]) - set(LAYERS)):
            e = r["layers"].get(L)
            if not e:
                continue

            def two(a, b):
                return f"{'-' if a is None else a}/{'-' if b is None else b}"
            lines.append(f"  {L:14} {e.get('n', 0):>6} {two(e.get('lead_m_median'), e.get('lead_m_p10')):>15} "
                         f"{'-' if e.get('near_share') is None else round(100 * e['near_share'])!s:>5}% "
                         f"{'-' if e.get('late_share') is None else round(100 * e['late_share'])!s:>5}% "
                         f"{two(e.get('road_s_median'), e.get('road_s_p90')):>15} "
                         f"{two(e.get('tile_s_median'), e.get('tile_s_p90')):>15} {e.get('miss_per_km', 0):>8}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", help="keepup.csv files (nyc_speed.py --keepup: <run>/keepup.csv)")
    ap.add_argument("--json", help="write the numbers here")
    a = ap.parse_args()
    res = []
    for p in a.csv:
        r = analyze(p)
        r["label"] = os.path.basename(os.path.dirname(os.path.abspath(p)))
        res.append(r)
    print(table(res))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    sys.exit(main())
