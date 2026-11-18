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
  paused     details that appeared while the drive harness stood waiting for the world (left out of the rest)
  lead_m     median / p10 distance from the car when they appeared (the item's own; a tile part's: its square)
  warn_s     ... that distance over the car's physics speed then: the seconds of warning the driver had
  <100m      share that first appeared within 100 m of the car, ahead of it (pop-in the driver sees)
  late       share that appeared only after the car had passed its closest point (data: of those, the share made
             within 2 s of their tile's data arriving: the server's tile came late, not the mod's queue)
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

VEG = ("deciduous", "conifer", "shrub", "clump", "tuft", "fern", "rock", "palm", "oak", "tree", "bush", "cliff",
       "eucalyptus", "mediterranean", "hedge_plant", "straw", "silage")
LIGHTS = ("lamp", "light", "lantern", "polelamp", "lampglow", "high_mast", "streetlight")
SIGNS = ("stop", "giveway", "speed", "signal", "sign", "town", "vienna", "yield", "exit", "mile", "route", "dir",
         "kantstolpe", "se_", "de_", "fr_j", "rrcross", "rrflash")
FENCES = ("fence", "hedge", "wall", "chainlink", "noise", "railing", "bollard")
BARRIERS = ("guardrail", "jersey", "cable", "barrier")
PART_LAYER = {"road": "road+markings", "walk": "sidewalks", "signs": "sign meshes", "bld": "buildings",
              "bld2": "buildings", "bld3": "buildings", "bld4": "buildings", "wire": "power lines"}
LAYERS = ["road+markings", "sidewalks", "trees", "lights", "signs", "sign meshes", "fences", "guardrails", "furniture",
          "parked cars", "buildings"]


def item_layer(group, kind):
    """trees.lua's group -> a layer (kind 1 vegetation tile, 2 street furniture rows, 3 guard rails). By the group's
    name first: the furniture rows carry street trees, the guard-rail rows jersey barriers."""
    g = group.lower()
    if any(g.startswith(k) for k in VEG):
        return "trees"
    if kind == 3 or any(g.startswith(k) for k in BARRIERS):
        return "guardrails"
    if kind == 1 and not any(k in g for k in FENCES):
        return "trees"
    if any(k in g for k in FENCES):
        return "fences"
    if g.startswith("wallsign"):
        return "signs"
    if any(k in g for k in LIGHTS):
        return "lights"
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


def analyze(path, moving_ms=2.0):
    rows, groups, t0 = load(path)
    tr = np.array([[float(v) for v in r[:5]] for r in rows["T"]]) if rows["T"] else np.zeros((0, 5))
    if len(tr) < 10:
        return {"path": path, "error": "no trace"}
    fr = Frame(tr[0, 1], tr[0, 2])
    txy = fr.xy(tr[:, 1], tr[:, 2])
    seg = np.hypot(np.diff(txy[0]), np.diff(txy[1]))
    dt = np.maximum(np.diff(tr[:, 0]), 1e-3)
    # Moving: the trace interval's own speed (position change over the game's wall clock) above moving_ms and no
    # jump (a reset, a teleport). drive.py stops the profile while the world is not ready (its effectiveRatio): the
    # car stands, and the velocity the game reports then is not its speed (it read twice the profile's, 2026-11-13).
    ws = seg / dt
    phys = np.hypot(tr[1:, 3], tr[1:, 4])
    moving = (ws > moving_ms) & (seg < 200) & (ws < 3 * phys + 30)
    if not moving.any():
        return {"path": path, "error": "never moved"}
    mi = np.nonzero(moving)[0]
    ta, tb = tr[mi[0], 0], tr[mi[-1] + 1, 0]       # the drive: first to last moving interval
    inwin = (tr[1:, 0] > ta) & (tr[:-1, 0] < tb)
    km = float(seg[moving].sum()) / 1000.0
    moving_s = float(dt[moving & inwin].sum())
    win = (tr[:, 0] >= ta) & (tr[:, 0] <= tb)
    wxy, wt = (txy[0][win], txy[1][win]), tr[win, 0]
    # an event's interval: moving or not, and the car's physics speed then (the profile's speed while it drives)
    t_iv = tr[:-1, 0]

    def interval(ts):
        k = np.clip(np.searchsorted(t_iv, np.asarray(ts, float), side="right") - 1, 0, len(t_iv) - 1)
        return moving[k], phys[k]
    out = {"path": path, "km": round(km, 2), "drive_s": round(tb - ta, 1), "moving_s": round(moving_s, 1),
           "paused_share": round(1 - moving_s / max(tb - ta, 1e-3), 3),
           "mph": round(float(np.median(phys[moving])) * 2.237, 1), "layers": {}}
    summ = os.path.join(os.path.dirname(os.path.abspath(path)), "drive", "summary.json")
    if os.path.exists(summ):        # (the harness's own: the share of the profile's time it kept)
        try:
            dsum = json.load(open(summ))
            out["effective_ratio"] = dsum.get("effectiveRatio")
            out["profile_mph"] = round((dsum.get("physicsSpeedMean") or 0) * 2.237, 1)
        except (OSError, ValueError):
            pass
    per = {}

    def add(layer, d, ra, ta_, dpass, lates, tapp):
        e = per.setdefault(layer, {"d": [], "w": [], "near": 0, "late": 0, "lateRoad": 0, "ra": [], "ta": [], "n": 0,
                                    "paused": 0})
        mv, v = interval(tapp)
        for i in range(len(d)):
            if not (ta - 1.0 <= tapp[i] <= tb) or dpass[i] > PASS_M:
                continue
            if not mv[i]:       # (made while the car stood: no warning to speak of)
                e["paused"] += 1
                continue
            e["n"] += 1
            late = bool(lates[i])
            if late:
                e["late"] += 1
                if 0 <= ta_[i] <= 2.0:  # (made within 2 s of its tile's data: the data came late, not the mod)
                    e["lateRoad"] += 1
            elif d[i] < NEAR_M:
                e["near"] += 1
            if not late:
                e["d"].append(d[i])
                e["w"].append(d[i] / max(v[i], 1.0))
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
                # (a part's lead: the car's distance to its square; late: made with the car already in it, or past it)
                add(L, a[m, 3], a[m, 4], a[m, 5], dpass[m], (a[m, 3] < 1.0) | (a[m, 0] > tpass[m]), a[m, 0])
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
        if not (ta <= t <= tb) or not interval([t])[0][0]:
            continue
        g = groups[int(r[1]) - 1] if 0 < int(r[1]) <= len(groups) else "?"
        miss.setdefault(item_layer(g, int(r[6])), set()).add(r[7])
    q = np.array([[float(v) for v in (r + ["-1"] * 4)[:18]] for r in rows["Q"]]) if rows["Q"] else np.zeros((0, 18))
    qm = (q[:, 0] >= ta) & (q[:, 0] <= tb) & interval(q[:, 0])[0] if len(q) else np.zeros(0, bool)
    qq = q[qm]
    # Q: t, lat, lon, speed, nTree, nProp, nRail, noTile, noSkin, noRoad, noWalk, noSigns, noVeg, noProps
    probe = {}
    if len(qq):
        for name, col in (("trees_missing", 4), ("furniture_missing", 5), ("guardrails_missing", 6),
                          ("road_tile_missing", 7), ("skin_missing", 8), ("road_mesh_missing", 9),
                          ("sidewalks_missing", 10), ("sign_meshes_missing", 11), ("veg_tile_missing", 12),
                          ("furniture_rows_missing", 13)):
            probe[name] = round(float((qq[:, col] > 0).mean()), 3)
        if (qq[:, 14] >= 0).any():      # (the tile under the car alone: newer records)
            for name, col in (("car_tile_missing", 14), ("car_road_missing", 15), ("car_walk_missing", 16),
                              ("car_veg_tile_missing", 17)):
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
        out["layers"][L] = {"n": n, "paused_n": e["paused"], "lead_m_median": pct(e["d"], 50), "lead_m_p10": pct(e["d"], 10),
                            "warn_s_median": pct(e["w"], 50), "warn_s_p10": pct(e["w"], 10),
                            "near_share": round(e["near"] / n, 3) if n else None,
                            "late_share": round(e["late"] / n, 3) if n else None,
                            "late_data_share": round(e["lateRoad"] / max(e["late"], 1), 3),
                            "road_s_median": pct(e["ra"], 50), "road_s_p90": pct(e["ra"], 90),
                            "tile_s_median": pct(e["ta"], 50), "tile_s_p90": pct(e["ta"], 90)}
    for L, s in miss.items():
        out["layers"].setdefault(L, {"n": 0})["miss_per_km"] = round(len(s) / max(km, 0.1), 1)
        out["layers"][L]["miss_n"] = len(s)
    return out


def table(results):
    lines = []

    def two(a, b):
        return f"{'-' if a is None else a}/{'-' if b is None else b}"
    for r in results:
        if "error" in r:
            lines.append(f"{r['path']}: {r['error']}")
            continue
        lines.append(f"\n{r.get('label') or r['path']}: {r['mph']} mph moving, {r['km']} km; {r['drive_s']} s of which "
                     f"paused (waiting for the world) {round(100 * r['paused_share'])}%, harness effectiveRatio "
                     f"{r.get('effective_ratio')}; probe {r['probe']}")
        if r.get("parked_all"):
            lines.append(f"  parked cars moved in (all): {r['parked_all']}")
        lines.append(f"  {'layer':14} {'n':>6} {'paused':>6} {'lead_m med/p10':>15} {'warn_s med/p10':>15} {'<100m':>6} "
                     f"{'late':>6} {'data':>5} {'road_s med/p90':>15} {'tile_s med/p90':>15} {'miss/km':>8}")
        for L in LAYERS + sorted(set(r["layers"]) - set(LAYERS)):
            e = r["layers"].get(L)
            if not e:
                continue
            lines.append(f"  {L:14} {e.get('n', 0):>6} {e.get('paused_n', 0):>6} "
                         f"{two(e.get('lead_m_median'), e.get('lead_m_p10')):>15} "
                         f"{two(e.get('warn_s_median'), e.get('warn_s_p10')):>15} "
                         f"{'-' if e.get('near_share') is None else round(100 * e['near_share'])!s:>5}% "
                         f"{'-' if e.get('late_share') is None else round(100 * e['late_share'])!s:>5}% "
                         f"{'-' if e.get('late_data_share') is None else round(100 * e['late_data_share'])!s:>4}% "
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
