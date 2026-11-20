"""rideprobe.py LAT LON R --cache DIR [--json OUT] [--records OUT] [--score-r M] [--online]

Road revamp v2 P3's `ride` metric (.claude/work/road-revamp-v2-road-shape-first-2026-11-19.md, section 5: "vertical
acceleration at 70 mph <= 0.3 g at every abutment"). For every bridge part within R of (lat, lon) its chain is solved
as the road tiles' chain store solves it (roads.ChainStore over BridgeProfiles, the physics ground graded by the plans as
blockworker.roads_tile's elev) with BNE_ROADSHAPE=profile, whose shadow (roadshape.profile_chain, run inline here)
gives each chain's stretches from ground anchor to ground anchor: today's heights (the plans' graded ground along the
approaches, the decks as built) and the joint solve's. Per abutment (a deck end with a ground approach):

  today_g / solved_g   the largest |vertical acceleration| of a car at RIDE_V (70 mph) within ABUT_WIN_M of the
                       abutment along the drive (the approach, then the deck and the decks running straight on from it),
                       over a 2 x RIDE_H_M chord: a = v^2 (2 z(s) - z(s - h) - z(s + h)) / h^2 / g (+ a crest, - a sag)
  sign                 the sign of today's worst (+ crest: a launch, - sag)

and the corpus numbers (p50 / p90 / p99 / max, the share over RIDE_TARGET_G), the statuses (fallbacks) per chain and
the solve's seconds. Everything rounded and sorted (the same JSON from the same code and data); seconds go to
"timing" only.

A measurement tool (Python is fine here); nothing in the game reads it. The scratch cache must not be the player's
~/.cache/beamng-earth (refused) nor a frozen corpus (pass a work copy)."""
import argparse
import json
import logging
import math
import os
import sys
import time

SERVER = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)      # (the server's modules: this probe lives in tools/probes/)

import numpy as np  # noqa: E402

import dmath  # noqa: E402
import dnp  # noqa: E402

RIDE_V = 70.0 * 0.44704     # m/s: 70 mph
RIDE_H_M = 10.0             # m: the half chord (a 20 m chord, the roadcorpus probe's launch chord)
RIDE_TARGET_G = 0.3         # g: the P3 target at every abutment
ABUT_WIN_M = 25.0           # m either side of an abutment its worst is read over
STEP_RUN_M = 0.05           # m a step in height between two members' joint knots is driven over
FOLLOW_M = 80.0             # m of deck read past the abutment (decks running straight on followed)
G = 9.81


def ride_g(z, step, h=RIDE_H_M, v=RIDE_V):
    """Vertical acceleration (g) of a car at v along heights z sampled every `step` m: v^2 (2 z(s) - z(s - h) - z(s + h))
    / h^2 / g (+ a crest); 0 where the chord does not fit."""
    z = np.asarray(z, np.float64)
    k = max(1, int(round(h / step)))
    hh = k * step
    a = np.zeros(len(z))
    if len(z) > 2 * k:
        a[k:-k] = v * v * (2.0 * z[k:-k] - z[:-2 * k] - z[2 * k:]) / (hh * hh) / G
    return a


def _deck_run(rec, d, e, key, reach):
    """[(member, reversed)] from deck d entered at its end e onwards, through C1 joins between decks, up to reach m."""
    ms = rec["members"]
    c1 = {}
    for a, ea, b, eb, f in rec["joins"]:
        if f and ms[a]["kind"] == "deck" and ms[b]["kind"] == "deck":
            c1[(a, ea)] = (b, eb)
            c1[(b, eb)] = (a, ea)
    out, seen, got = [], set(), 0.0
    while d not in seen and got < reach:
        seen.add(d)
        out.append((d, e == 1))
        xy = np.asarray(ms[d]["xy"])
        got += float(dnp.hypot(*np.diff(xy, axis=0).T).sum())
        nxt = c1.get((d, 1 - e))
        if nxt is None:
            break
        d, e = nxt
    return out


def _path(rec, parts, key):
    """(stations, z) of members [(m, reversed)] driven end to end. Every member keeps its own end knots: where two
    meet at one point with one height the joint is taken once; a step in height there (today's deck end over its
    approach) stays a step (STEP_RUN_M long), and a gap in plan is driven across (its length)."""
    ms = rec["members"]
    S, Z, last = [], [], None
    for m, rev in parts:
        xy = np.asarray(ms[m]["xy"], np.float64)
        z = np.asarray(ms[m][key], np.float64)
        if rev:
            xy, z = xy[::-1], z[::-1]
        s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
        if last is not None:
            gap = float(dnp.hypot(*(xy[0] - last[0])))
            if gap < 1e-6 and abs(float(z[0]) - last[2]) < 1e-6:
                s, z = s[1:], z[1:]
                off = last[1]
            else:
                off = last[1] + max(gap, STEP_RUN_M)
            s = s + off
        S.append(s)
        Z.append(z)
        last = (xy[-1], float(s[-1]) if len(s) else last[1], float(z[-1]) if len(z) else last[2])
    return np.concatenate(S), np.concatenate(Z)


def abutment_rides(rec):
    """[(lat, lon, cls, today_g, solved_g, sign, status)] per abutment of one profile record (roadshape.profile_chain)."""
    ms = rec["members"]
    out = []
    for i, m in enumerate(ms):
        if m["kind"] != "approach" or len(m["xy"]) < 2:
            continue
        sides = [(m["deck"], m["end"], False)]
        if m["reach"] == 2:
            for a, ea, b, eb, _f in rec["joins"]:
                if a == i and ea == 1 and ms[b]["kind"] == "deck":
                    sides.append((b, eb, True))
        for d, e, at_end in sides:
            run = _deck_run(rec, d, e, "z_today", FOLLOW_M)
            # (the drive: the approach towards the abutment, then the deck(s) on from it)
            appr = [(i, not at_end)]
            res = []
            for key in ("z_today", "z_built" if "z_built" in ms[i] else "z_solved"):
                s, z = _path(rec, appr + run, key)
                s0 = float(_path(rec, appr, key)[0][-1])        # (the abutment's station: the approach's end)
                t = np.arange(0.0, s[-1] + 1e-9, 1.0)
                zz = dnp.interp(t, s, z)
                a = ride_g(zz, 1.0)
                near = np.abs(t - s0) <= ABUT_WIN_M
                k = int(np.argmax(np.abs(a) * near))
                res.append((float(abs(a[k])), float(np.sign(a[k]))))
            ll = ms[d]["ll"][0 if e == 0 else 1]
            out.append((ll[0], ll[1], ms[d]["cls"] + ("_link" if ms[d]["link"] else ""), round(res[0][0], 3),
                        round(res[1][0], 3), int(res[0][1]), rec["status"]))
    return out


def built_heights(rec, elev):
    """P3b (BNE_ROADSHAPE=profile built): each member's "z_built", the heights as BUILT: a deck's its published
    heights (roadshape._built's per vertex, back on its knots: rec["_built"]["knots"]), an approach's the physics
    ground the road tiles read there (elev: the plans' second stage, pinned). A record not solved: today's for both."""
    b = rec.get("_built")
    fr = rec.get("_frame")
    for n, m in enumerate(rec["members"]):
        if b is None or fr is None:
            m["z_built"] = list(m["z_today"])
            continue
        if m["kind"] == "deck":
            m["z_built"] = [round(float(v), 3) for v in b["knots"][n]]
            continue
        xy = np.array(m["xy"], np.float64)
        # (an abutment knot read 5 cm inside the approach: the deck end itself is where the plans end, and the graded
        # ground falls away past a plan's abutment end (grading ABUT_UNDER_GRADE): the record's 1 mm rounding put
        # Nordschleife A's knot past it, 3.4 m down, a ride of 8 g that no car drives: the deck is there)
        ends = [0] + ([len(xy) - 1] if m.get("reach") == 2 else [])
        for k in ends:
            j = 1 if k == 0 else k - 1
            d = xy[j] - xy[k]
            n = float(dnp.hypot(d[0], d[1]))
            if n > 0.1:
                xy[k] = xy[k] + d * (BUILT_IN_M / n)
        la, lo, _ = fr.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        z = np.asarray(elev(la, lo), np.float64)
        m["z_built"] = [round(float(v), 3) for v in fr.geodetic_to_enu(la, lo, z)[2]]


BUILT_IN_M = 0.05           # m inside an approach its abutment knot's built ground is read (built_heights)
ORDERS = 1      # (--orders: the chains solved again this many times in all, each time a fresh store from another start)


def _rec_key(rec):
    b = rec.get("_built")
    return (rec["status"], tuple(tuple(m["z_solved"]) for m in rec["members"]), b["digest"] if b else None,
            tuple(tuple(np.round(b["deck_z"][k], 4).tolist()) for k in sorted(b["deck_z"], key=str)) if b else None)


def order_check(todo, cell_fn, first, bw, roadsmod, roadshape):
    """P3b's seamprobe-style check: the chains solved again from ORDERS - 1 other starts (a fresh bridge store each,
    its parts asked in another order: reversed, then rotated by a third, ...); every chain asked in both must give the
    same record (status, solved heights, the built decks' heights, the pins' digest). {"starts", "chains", "differ"}."""
    base = {r["chain"]: _rec_key(r) for r in first}
    keys = sorted(todo)
    differ, seen = [], 0
    for n in range(1, ORDERS):
        order = keys[::-1] if n == 1 else keys[(n * len(keys)) // ORDERS:] + keys[:(n * len(keys)) // ORDERS]
        got = []
        roadshape.PROFILE_SINKS[:] = [got.append]
        cs = roadsmod.ChainStore(roadsmod.BridgeProfiles, cell_fn, bw._elev_stage1, bw._bridge_setup)
        for k in order:
            feats, i, ring = todo[k]
            try:
                cs.store_at(float(ring[0, 1]), float(ring[0, 0])).parts_todo([(k, feats, i)])
            except Exception as ex:
                logging.getLogger("rideprobe").warning("part %s: %s: %s", k, type(ex).__name__, ex)
        for r in got:
            if r["chain"] in base:
                seen += 1
                if _rec_key(r) != base[r["chain"]]:
                    differ.append([n, r["chain"]])
    return {"starts": ORDERS, "chains": seen, "differ": differ}


def summary(vals):
    v = np.sort(np.asarray(vals, np.float64))
    if not len(v):
        return {"n": 0}
    q = lambda p: round(float(np.quantile(v, p)), 3)        # noqa: E731
    return {"n": int(len(v)), "p50": q(0.5), "p90": q(0.9), "p99": q(0.99), "max": round(float(v[-1]), 3),
            "over_target": round(float(np.mean(v > RIDE_TARGET_G)), 3)}


def score(recs, R=None, frame=None):
    """The probe's JSON from profile records (only abutments within R of the frame's origin when both given)."""
    rows, status, incomplete = [], {}, 0
    for rec in recs:
        status[rec["status"]] = status.get(rec["status"], 0) + 1
        if not rec.get("complete", True):
            incomplete += 1         # (read short: left out of the ride numbers, counted)
            continue
        for r in abutment_rides(rec):
            if R is not None and frame is not None:
                x, y, _ = frame.geodetic_to_enu(np.array([r[0]]), np.array([r[1]]), np.zeros(1))
                if float(dnp.hypot(x[0], y[0])) > R:
                    continue
            rows.append(r)
    rows.sort(key=lambda r: (-r[3], r[0], r[1]))
    return {"abutments": len(rows), "today": summary([r[3] for r in rows]), "solved": summary([r[4] for r in rows]),
            "solved_ok_only": summary([r[4] for r in rows if r[6] == "solved"]),
            "chains": len(recs), "status": dict(sorted(status.items())), "incomplete": incomplete,
            "worst_today": [list(r) for r in rows[:15]],
            "worst_solved": [list(r) for r in sorted(rows, key=lambda r: (-r[4], r[0], r[1]))[:15]],
            "rows": [list(r) for r in rows]}


def collect(lat, lon, R, cache, offline=True):
    """Every bridge part within R asked of a fresh chain store (graded physics ground); the shadow's records."""
    os.environ.setdefault("BNE_REVISIT_SKIP", "roads")
    import roadshape
    if not roadshape.PROFILE:
        raise SystemExit("rideprobe needs BNE_ROADSHAPE=profile")
    import blockworker as bw
    import geodesy
    import grading as gradingmod
    import mapcells
    import roads as roadsmod
    bw.init(cache, bool(offline), True, True, True, False, None, logging.ERROR, False, True)
    s = bw._s
    recs = []
    roadshape.PROFILE_SYNC = True
    roadshape.PROFILE_ACCEPT_SHORT = True      # (offline: a lidar or roadz source missing marks the chain short)
    roadshape.PROFILE_SINKS[:] = [recs.append]
    frame = geodesy.EnuFrame(lat, lon)
    g, _ = s.anchor(1, lat, lon)

    def elev(la, lo):           # (blockworker.roads_tile's: the physics ground, lifted under roads)
        dz = bw.sample_at_zoom(la, lo) if roadshape.PROFILE_BUILT else s.dem.sample(la, lo, 15)   # (P3b: the first
        z = bw.ground(frame, la, lo, dz, g)                 # stage's own zoom, blockworker.stage_zoom)
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    cell_fn = lambda key: s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")     # noqa: E731
    dlat, dlon = R / 111320.0, R / (111320.0 * dmath.cos(math.radians(lat)))
    ga, go = np.meshgrid(np.linspace(lat - dlat, lat + dlat, 9), np.linspace(lon - dlon, lon + dlon, 9))
    keys = sorted({str(c): c for c in mapcells.cells_of(ga.ravel(), go.ravel())}.items())
    todo = {}
    for _n, ck in keys:
        feats = cell_fn(ck)
        if feats is None or not len(feats):
            continue
        bs = roadsmod.BridgeProfiles.specs_of(feats)
        for i in range(len(feats)):
            if bs[i] is None:
                continue
            for j, ring in roadsmod._keyed(roadsmod.BridgeProfiles.keys_of, roadsmod.BridgeProfiles.parts_of, feats, i):
                k = (str(feats.ids[i]), j)
                ring = np.asarray(ring, np.float64)
                near = (np.abs(ring[:, 1] - lat) <= dlat) & (np.abs(ring[:, 0] - lon) <= dlon)
                if k not in todo and near.any():
                    todo[k] = (feats, i, ring)
    if roadshape.PROFILE_BUILT:
        # (P3b built: the process's own bridge store on the plans' first stage, the one the second stage's pins come
        # from, as the road tiles'; the built ground read through the pinned plans below)

        cs = bw._chain_store("bridges", roadsmod.BridgeProfiles, elev, bw._bridge_setup)
    else:
        cs = roadsmod.ChainStore(roadsmod.BridgeProfiles, cell_fn, elev, bw._bridge_setup)
    t0 = time.perf_counter()
    for k in sorted(todo):
        feats, i, ring = todo[k]
        st = cs.store_at(float(ring[0, 1]), float(ring[0, 0]))
        try:
            st.parts_todo([(k, feats, i)])
        except Exception as ex:
            logging.getLogger("rideprobe").warning("part %s: %s: %s", k, type(ex).__name__, ex)
    wall = time.perf_counter() - t0
    wall_built = 0.0
    orders = {}
    if roadshape.PROFILE_BUILT:
        asked = list(recs)      # (the chains of the parts asked; the pins' own solves round them come after, unscored)
        t1 = time.perf_counter()
        for rec in asked:
            built_heights(rec, elev)
        wall_built = time.perf_counter() - t1
        if ORDERS > 1:
            orders = order_check(todo, cell_fn, asked, bw, roadsmod, roadshape)
        recs = asked
    return frame, recs, {"built_s": round(wall_built, 2), "orders": orders, "parts": len(todo),
                         "wall_s": round(wall, 2),
                         "pin_field": {k: (round(v, 3) if isinstance(v, float) else v)
                                       for k, v in gradingmod.PIN_STATS.items()},
                         "profile": {k: (round(v, 3) if isinstance(v, float) else v)
                                     for k, v in roadshape.PROFILE_STATS.items()}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("r", type=float)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--json")
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--records", help="also write the profile records (JSON lines) here")
    ap.add_argument("--score-r", type=float, help="m: abutments scored within this of the point (default r; a chain "
                    "asked from a part within r can reach further)")
    ap.add_argument("--orders", type=int, default=1, help="P3b built: solve the chains again from this many starts in "
                    "all (fresh stores, other orders) and compare the records")
    a = ap.parse_args()
    global ORDERS
    ORDERS = max(1, a.orders)
    cache = os.path.abspath(os.path.expanduser(a.cache))
    player = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    if os.path.realpath(cache).startswith(player):
        raise SystemExit("refusing the player's cache")
    frame, recs, timing = collect(a.lat, a.lon, a.r, cache, offline=not a.online)
    res = score(recs, a.score_r or a.r, frame)
    res["timing"] = timing
    sol = [r["timing"]["solve_s"] for r in recs]
    res["timing"]["solve_s_per_chain"] = summary(sol) if sol else {"n": 0}
    res["timing"]["knots"] = summary([r["report"][1] for r in recs]) if recs else {"n": 0}
    if a.records:
        with open(a.records, "w") as f:
            for r in recs:
                f.write(json.dumps({k: v for k, v in r.items() if k != "timing" and not k.startswith("_")},
                                   sort_keys=True) + "\n")
    body = json.dumps(res, sort_keys=True, indent=1)
    if a.json:
        with open(a.json, "w") as f:
            f.write(body)
    t, s_ = res["today"], res["solved"]
    print(f"abutments {res['abutments']}  chains {res['chains']}  status {res['status']}")
    print(f"today  {t}\nsolved {s_}")
    for r in res["worst_today"][:8]:
        print("  worst today", r)


if __name__ == "__main__":
    sys.exit(main() or 0)
