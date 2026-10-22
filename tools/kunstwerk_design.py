#!/usr/bin/env python3
"""A sunken road designed, not traced: per carriageway a smooth centre line (fitted to the BGT's carriageways in the
trench), a smooth vertical profile (the AHN along it, fitted), and a fixed cross-section swept along it (road, shoulders,
walls with a coping, a railing on the high ones); the decks over it as clean slabs square to it where the BGT has its
passages. The map's outlines are only where things are: what they look like is the design's (the user, 2026-10-21:
"logisch dat de baan klopt, geen 1:1 van de map").

    python3 tools/kunstwerk_design.py ../design/kunstwerken/droogleever/ref

Writes ../parts.json (Blender: tools/blender_kunstwerk_import.py; the game: data/kunstwerken via the export).
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kunstwerk_build import Ground, Part, inside, open_ring, railing  # noqa: E402

STEP = 2.0              # m between cross-sections
LANE = 3.25             # m per lane
SHOULDER = 0.5          # m of road beyond the outer lanes' edge lines, to the wall's foot
WALL_T = 0.4            # m: a wall's thickness
COPING_W, COPING_H = 0.6, 0.25      # m: its coping (the cap over its top, wider than it)
WALL_OVER = 0.15        # m a wall's top stands over the ground behind it
DECK_T = 0.9            # m: a deck's slab under its road
DECK_EDGE = 0.4         # m: its edge beam's width
PARAPET_H = 1.0         # m: the parapet over a deck's road
RAIL_FROM = 1.0         # m of wall over the ground behind it before it gets a railing (else: the coping alone)
AREA_T = (110.0, 120.0)  # m behind / ahead along the trench's axis the design's streets reach
AREA_W = 75.0            # m to either side of it
SUNK_UNDER_M = 1.6      # m a carriageway in the trench lies under the streets at grade
FOLLOW_M = 2.5          # m a band may move from one metre's section to the next
GAP_M = 40              # m without one (a deck) before a carriageway is taken to end
SMOOTH_M = 30.0         # m: the vertical profile's smoothing window


def fit_line(t, w, deg=3):
    c = np.polyfit(t, w, deg)
    return lambda tt: np.polyval(c, tt)


def smooth(z, s, win):
    out = np.empty_like(z)
    for i in range(len(z)):
        k = np.abs(s - s[i]) <= win / 2
        out[i] = np.median(z[k])
    # then a moving average (median steps out)
    out2 = np.empty_like(out)
    for i in range(len(out)):
        k = np.abs(s - s[i]) <= win / 4
        out2[i] = out[k].mean()
    return out2


def carriageways(ref):
    """The trench's frame ((centre, axis, normal): t along the axis, w to its left) and its carriageways: name ->
    (w(t), t from, t to, width). A carriageway is where the BGT has a carriageway and the AHN's ground lies well under
    the streets (SUNK_UNDER_M under the ground at grade): followed a metre at a time from the oval, where the two are
    plain, each step's band the one nearest the last (the decks, where the lidar sees no floor, bridged), and a
    smoothing spline through the bands' middles, not one curve over the whole length (a cubic strayed 15 m at the
    ends and cut through the street beside the trench, 2026-10-21)."""
    from matplotlib.path import Path
    from scipy.interpolate import UnivariateSpline
    ax = json.load(open(os.path.join(ref, "axis.json")))
    c0, u = np.asarray(ax["centre"]), np.asarray(ax["axis"])
    n = np.array([-u[1], u[0]])
    r = json.load(open(os.path.join(ref, "ref.json"), encoding="utf-8"))
    d = np.load(os.path.join(ref, "ahn.npz"))
    dtm, kind, res, x0, y1 = d["dtm"], d["kind"], float(d["res"]), float(d["x0"]), float(d["y1"])
    H, W = dtm.shape
    roads = [[Path(np.asarray(ring)) for ring in f["rings"]] for f in r["features"] if f["layer"] == "wegdeel"
             and f.get("functie") in ("rijbaan lokale weg", "OV-baan")]       # (outer ring, then its holes)
    grade = np.nanpercentile(dtm[(kind == 0) & np.isfinite(dtm)], 75)
    ws = np.arange(-45.0, 45.0, 0.5)

    def section(t):
        P = c0 + u * t + np.outer(ws, n)
        cc = ((P[:, 0] - x0) / res).astype(int)
        rr = ((y1 - P[:, 1]) / res).astype(int)
        ok = (cc >= 0) & (cc < W) & (rr >= 0) & (rr < H)
        z = np.full(len(ws), np.nan)
        z[ok] = np.where(kind[rr[ok], cc[ok]] == 0, dtm[rr[ok], cc[ok]], np.nan)
        road = np.zeros(len(ws), bool)
        for outer, *holes in roads:
            m = outer.contains_points(P)
            for h in holes:
                m &= ~h.contains_points(P)
            road |= m
        low = road & (z < grade - SUNK_UNDER_M)
        runs = np.split(np.flatnonzero(low), np.flatnonzero(np.diff(np.flatnonzero(low)) > 2) + 1)
        return [(ws[q].mean(), ws[q[-1]] - ws[q[0]] + 0.5) for q in runs if len(q) and 4.5 <= ws[q[-1]] - ws[q[0]] + 0.5 <= 12]
    # the oval's middle: the two bands either side of the axis
    seeds = None
    for t0 in (0.0, -10.0, 10.0, -20.0, 20.0):
        b = section(t0)
        left, right = [m for m in b if m[0] < 0], [m for m in b if m[0] > 0]
        if left and right:
            seeds = {"west": max(left)[0], "east": min(right)[0]}
            break
    lines = {}
    for name, w0 in (seeds or {}).items():
        pts = {}
        for direction in (1.0, -1.0):
            last, miss = w0, 0
            for t in np.arange(0.0, 200.0, 1.0) * direction:
                b = [m for m in section(t) if abs(m[0] - last) <= FOLLOW_M]
                if b:
                    m = min(b, key=lambda m: abs(m[0] - last))
                    pts[t] = m
                    last, miss = m[0], 0
                else:
                    miss += 1
                    if miss > GAP_M:
                        break
        tt = np.array(sorted(pts))
        ww = np.array([pts[t][0] for t in tt])
        wd = np.array([pts[t][1] for t in tt])
        spl = UnivariateSpline(tt, ww, k=3, s=len(tt) * 0.35 ** 2)
        width = float(np.median(wd))
        lines[name] = (spl, float(tt.min()), float(tt.max()), width)
        print(f"{name}: t {tt.min():.0f}..{tt.max():.0f}, width {width:.1f} m, {len(tt)} sections, "
              f"fit within {np.max(np.abs(spl(tt) - ww)):.2f} m")
    return (c0, u, n), lines

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ref")
    ap.add_argument("--lanes", type=int, default=2)
    a = ap.parse_args(argv)
    ref = json.load(open(os.path.join(a.ref, "ref.json"), encoding="utf-8"))
    g = Ground(a.ref)
    (c0, u, n), lines = carriageways(a.ref)

    def xy(tt, ww):
        return c0 + np.outer(tt, u) + np.outer(ww, n)
    decks = []
    for f in ref["features"]:
        if f["layer"] == "tunneldeel":
            decks.append(open_ring(f["rings"][0]))
    parts = {}

    def part(name, mat):
        parts.setdefault(name, Part(name, mat))
        return parts[name]
    road = part("road", "road_asphalt_2lane")
    walls = part("walls", "bne_bridge_concrete")
    deck_p = part("decks", "bne_bridge_concrete")
    rails = part("railings", "bne_truss_steel")
    marks = part("markings", "line_white")
    deck_info = []
    for name, (f, ta, tb, width) in lines.items():
        W = max(width, a.lanes * LANE) + 2 * SHOULDER
        ts = np.arange(ta, tb + 1e-6, STEP)
        mid = xy(ts, f(ts))
        d = np.gradient(mid, axis=0)
        d /= np.hypot(*d.T)[:, None]
        nr = np.c_[-d[:, 1], d[:, 0]]                       # left of the direction of travel along +t
        s = np.r_[0, np.cumsum(np.hypot(*np.diff(mid, axis=0).T))]
        # which sections are under a deck (any of the BGT's passages)
        under = np.zeros(len(ts), bool)
        for ring in decks:
            under |= inside(ring, mid[:, 0], mid[:, 1])
        # the floor: the AHN along the centre line away from the decks (their fill is the floor too), smoothed
        zc = g.at(mid[:, 0], mid[:, 1])
        z = smooth(zc, s, SMOOTH_M)
        L, Rr = mid + nr * (W / 2), mid - nr * (W / 2)
        # road: a strip
        k = len(ts)
        P = np.r_[np.c_[L, z + 0.02], np.c_[Rr, z + 0.02]]
        i = np.arange(k - 1)
        T = np.r_[np.c_[i, i + k, i + k + 1], np.c_[i, i + k + 1, i + 1]]
        road.add(P, T, np.r_[np.c_[np.full(k, 0.0), s], np.c_[np.full(k, W), s]])
        # markings: the edge lines (0.15 m, 0.3 m in) and the lane line (dashed 3 on 9) between the lanes
        for off, dash in ((W / 2 - SHOULDER, False), (-(W / 2 - SHOULDER), False), (0.0, True)):
            a_ = mid + nr * (off + 0.075)
            b_ = mid + nr * (off - 0.075)
            for j in range(k - 1):
                if dash and (s[j] % 12.0) > 3.0:
                    continue
                q = [(*a_[j], z[j] + 0.03), (*b_[j], z[j] + 0.03), (*b_[j + 1], z[j + 1] + 0.03), (*a_[j + 1], z[j + 1] + 0.03)]
                marks.add(q, [[0, 1, 2], [0, 2, 3]], [[0, 0], [1, 0], [1, 1], [0, 1]])
        # walls on both sides: foot at the road, top at the ground behind (or a deck's underside), coping over it
        deck_under = np.full(len(ts), np.nan)
        for side, sign in (("left", 1.0), ("right", -1.0)):
            face = mid + nr * sign * (W / 2)
            back = mid + nr * sign * (W / 2 + WALL_T)
            behind = np.maximum.reduce([g.at(*(mid + nr * sign * (W / 2 + WALL_T + r)).T) for r in (1.0, 2.0, 3.0)])
            top = smooth(np.maximum(behind + WALL_OVER, z + 0.8), s, 10.0)
            foot = z - 0.2
            for j in range(k - 1):
                for (p0, p1) in ((face, face), (back, back)):
                    pass
                quads = [  # face, back, top
                    ((*face[j], foot[j]), (*face[j + 1], foot[j + 1]), (*face[j + 1], top[j + 1]), (*face[j], top[j])),
                    ((*back[j + 1], foot[j + 1]), (*back[j], foot[j]), (*back[j], top[j]), (*back[j + 1], top[j + 1])),
                ]
                for q in quads:
                    walls.add(q, [[0, 1, 2], [0, 2, 3]],
                              [[s[j], q[0][2]], [s[j + 1], q[1][2]], [s[j + 1], q[2][2]], [s[j], q[3][2]]])
                # the coping: a box over the top, overhanging toward the road
                cf = mid + nr * sign * (W / 2 - (COPING_W - WALL_T) / 2)
                cb = mid + nr * sign * (W / 2 + WALL_T + (COPING_W - WALL_T) / 2)
                ct0, ct1 = top[j] + COPING_H, top[j + 1] + COPING_H
                for q in (((*cf[j], top[j]), (*cf[j + 1], top[j + 1]), (*cf[j + 1], ct1), (*cf[j], ct0)),
                          ((*cf[j], ct0), (*cf[j + 1], ct1), (*cb[j + 1], ct1), (*cb[j], ct0)),
                          ((*cb[j + 1], top[j + 1]), (*cb[j], top[j]), (*cb[j], ct0), (*cb[j + 1], ct1)),
                          ((*cf[j + 1], top[j + 1]), (*cf[j], top[j]), (*cb[j], top[j]), (*cb[j + 1], top[j + 1]))):
                    walls.add(q, [[0, 1, 2], [0, 2, 3]], [[s[j], 0], [s[j + 1], 0], [s[j + 1], 1], [s[j], 1]])
            # a railing on the coping where the wall stands well over the ground behind it
            hi = (top - behind) > RAIL_FROM
            line = mid + nr * sign * (W / 2 + WALL_T / 2)
            j = 0
            while j < k:
                if not hi[j]:
                    j += 1
                    continue
                e = j
                while e < k and hi[e]:
                    e += 1
                if e - j >= 3:
                    railing(rails, line[j:e], top[j:e] + COPING_H)
                j = e
        deck_info.append((name, mid, z, W, nr, ts, under))
    # decks: per passage, a slab square to the carriageway under it, from wall to wall plus its edge beams
    for ring in decks:
        for name, mid, z, W, nr, ts, under in deck_info:
            ins = inside(ring, mid[:, 0], mid[:, 1])
            if ins.sum() < 2:
                continue
            j0, j1 = np.flatnonzero(ins)[[0, -1]]
            # its top: the AHN's surface over it (the street on it), fitted level across, along the carriageway
            mpt = mid[j0:j1 + 1]
            top = np.median(g.at(mpt[:, 0], mpt[:, 1], g.dsm)[np.isfinite(g.at(mpt[:, 0], mpt[:, 1], g.dsm))])
            half = W / 2 + WALL_T + 0.3
            a0, a1 = mid[j0] - (mid[min(j0 + 1, len(mid) - 1)] - mid[j0]) * 0.0, mid[j1]
            dv = (a1 - a0) / max(np.hypot(*(a1 - a0)), 1e-9)
            nl = np.array([-dv[1], dv[0]])
            corners = np.array([a0 - dv * DECK_EDGE + nl * half, a1 + dv * DECK_EDGE + nl * half,
                                a1 + dv * DECK_EDGE - nl * half, a0 - dv * DECK_EDGE - nl * half])
            from kunstwerk_build import prism
            prism(deck_p, corners, np.full(4, top - 0.05 - DECK_T), np.full(4, top - 0.05), uv_m=4.0)
            # its parapets: along both open edges (the faces seen from the trench), PARAPET_H over its road
            for e0, e1 in ((corners[0], corners[3]), (corners[1], corners[2])):
                el = e1 - e0
                en = np.array([-el[1], el[0]]) / max(np.hypot(*el), 1e-9) * 0.15
                pr = np.array([e0 - en, e1 - en, e1 + en, e0 + en])
                prism(deck_p, pr, np.full(4, top - 0.05 - DECK_T), np.full(4, top + PARAPET_H), uv_m=4.0)
            deck_info_rec = {"carriageway": name, "top": float(top), "clear": float(top - 0.05 - DECK_T - z[j0:j1 + 1].max())}
            print("deck", deck_info_rec)
    # the streets round and over the trench: the BGT's surfaces in the design's area (the ring, its cycle paths,
    # footways, islands), at grade on the ground smoothed or on a deck on its plane; raised ones KERB_M up with kerbs
    deck_planes = []
    for ring in decks:
        mpt = ring
        zs = g.at(*np.mgrid[0:0, 0:0], g.dsm) if False else None
        lo_, hi_ = ring.min(0), ring.max(0)
        X, Y = np.meshgrid(np.arange(lo_[0], hi_[0], 0.5), np.arange(lo_[1], hi_[1], 0.5))
        px, py = X.ravel(), Y.ravel()
        m = inside(ring, px, py)
        zz = g.at(px[m], py[m], g.dsm)
        ok = np.isfinite(zz)
        med = np.median(zz[ok])
        ok &= np.abs(zz - med) < 0.8
        coef, *_ = np.linalg.lstsq(np.c_[np.ones(ok.sum()), px[m][ok], py[m][ok]], zz[ok], rcond=None)
        deck_planes.append((ring, coef))

    def street_z(x, y):
        z = g.at(x, y)
        for ring, (p0, p1, p2) in deck_planes:
            k = inside(ring, x, y)
            z = np.where(k, p0 + p1 * x + p2 * y, z)
        return z
    trench = np.load(os.path.join(a.ref, "trench_mask.npy"))

    def in_trench(x, y):
        c = np.clip(((np.asarray(x) - g.x0) / g.res).astype(int), 0, g.W - 1)
        r = np.clip(((g.y1 - np.asarray(y)) / g.res).astype(int), 0, g.H - 1)
        return trench[r, c]

    def on_deck(x, y):
        k = np.zeros(len(np.atleast_1d(x)), bool)
        for ring, _ in deck_planes:
            k |= inside(ring, np.atleast_1d(x), np.atleast_1d(y))
        return k
    import buildings
    import plazas
    from kunstwerk_build import SURF_MAT, RAISED, KERB_M
    import grading
    streets = 0
    kerbs = part("kerbs", "curbs")
    for f in ref["features"]:
        if f["layer"] not in ("wegdeel", "ondersteunendwegdeel") or f["level"] < 0:
            continue
        fn = f["functie"].lower()
        key = next((k for k in SURF_MAT if k in fn), None)
        if key is None:
            continue
        rings = [open_ring(r) for r in f["rings"]]
        ring = rings[0]
        if len(ring) < 3:
            continue
        cen = ring.mean(0)
        tc, wc = float((cen - c0) @ u), float((cen - c0) @ n)
        if not (-AREA_T[0] < tc < AREA_T[1] and abs(wc) < AREA_W):
            continue
        if buildings._area2(ring) < 0:
            ring = ring[::-1]
        tris = np.asarray(buildings.triangulate(ring), np.int64).reshape(-1, 3)
        if not len(tris):
            continue
        P, T = plazas._subdivide(ring, tris, 2.0)
        cc = P[T].mean(1)
        rel = cc - c0
        keep = (np.abs(rel @ n) < AREA_W) & (rel @ u > -AREA_T[0]) & (rel @ u < AREA_T[1])
        keep &= ~in_trench(cc[:, 0], cc[:, 1]) | on_deck(cc[:, 0], cc[:, 1])       # (the trench: the design's)
        for h in rings[1:]:
            if len(h) >= 3:
                keep &= ~plazas._inside_many(h, cc)
        if not keep.any():
            continue
        T = T[keep]
        used = np.unique(T)
        remap = np.full(len(P), -1, np.int64)
        remap[used] = np.arange(len(used))
        P, T = P[used], remap[T]
        up = KERB_M if key in RAISED else 0.0
        z = street_z(P[:, 0], P[:, 1]) + 0.03 + up
        part("street_" + key, SURF_MAT[key]).add(np.c_[P, z], T, P)
        streets += 1
        if up:                                      # its kerb: along its edges, from the street up
            e = grading.resample(np.r_[ring, ring[:1]], 1.0)[0]
            rel = e - c0
            inn = (np.abs(rel @ n) < AREA_W) & (rel @ u > -AREA_T[0]) & (rel @ u < AREA_T[1])
            ze = street_z(e[:, 0], e[:, 1]) + 0.03
            for i in range(len(e) - 1):
                if inn[i] and inn[i + 1]:
                    kerbs.add([(*e[i], ze[i]), (*e[i + 1], ze[i + 1]), (*e[i + 1], ze[i + 1] + KERB_M), (*e[i], ze[i] + KERB_M)],
                              [[0, 1, 2], [0, 2, 3]], [[0, 0], [1, 0], [1, 1], [0, 1]])
    print("street polygons", streets)
    out = {"origin_rd": ref["origin_rd"], "origin_wgs": ref["origin_wgs"],
           "parts": [p.out() for p in parts.values() if p.out() is not None]}
    dst = os.path.join(os.path.dirname(os.path.abspath(a.ref)), "parts.json")
    json.dump(out, open(dst, "w"))
    json.dump({"decks": [{"layer": "tunneldeel", "ring": r.round(3).tolist(), "plane": [float(v) for v in cf]}
                         for r, cf in deck_planes]},
              open(os.path.join(os.path.dirname(os.path.abspath(a.ref)), "decks.json"), "w"))
    for p in out["parts"]:
        print(f"{p['name']:12s} {p['material']:22s} tris {len(p['tris'])}")


if __name__ == "__main__":
    main()
