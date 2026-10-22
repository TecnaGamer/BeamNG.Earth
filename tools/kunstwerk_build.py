#!/usr/bin/env python3
"""The parts of a junction's model from its reference (tools/blender_ref.py): exact from the BGT's outlines, heights from
the AHN; for Blender to show (with the game's textures), to be touched up by hand, and exported to the game.

    python3 tools/kunstwerk_build.py ../design/kunstwerken/droogleever/ref

Writes <ref>/../parts.json: {parts: [{name, material, positions (k, 3), tris, uvs}]}, local RD metres, z NAP, and
<ref>/../decks.json (each deck's ring and plane). Parts:

    walls      the BGT's walls (scheiding "muur", their own outlines: straight, their real thickness) raised from their
               foot to their top: on a deck a parapet (DECK_UNDER under the deck's plane to PARAPET over it), under a
               deck an abutment (to its underside), elsewhere from the low side's ground to the high side's
    decks      the slabs under the streets over the trench (BGT tunneldeel, overbruggingsdeel): their plane (the AHN's
               surface fitted, cars and lamp posts left out) DECK_UNDER deep
    surface_*  the BGT's road surfaces in the model's area, subdivided every STEP_M, on the ground (the AHN, smoothed) or
               their deck's plane; footways and islands KERB_M up
    kerbs      the kerb faces where a raised surface meets a lower one
    railings   steel railings: on walls whose top is at the ground behind them, and along the BGT's fences (hek)
"""
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

STEP_M = 2.0
KERB_M = 0.12
DECK_UNDER = 0.9            # m from a deck's road to its slab's underside
PARAPET = 1.0               # m a deck's parapet stands over its road
WALL_REACH = 2.5            # m to either side of a wall the ground is read
AREA_PAD = 8.0              # m round the trench and the decks the model's surfaces reach
RAIL_H = (0.55, 1.05)       # m: a railing's rails over its foot
POST_EVERY = 2.0            # m between its posts
SURF_MAT = {"rijbaan": "road_asphalt_2lane", "ov-baan": "road_asphalt_2lane", "parkeervlak": "road_asphalt_2lane",
            "fietspad": "bne_sidewalk_brick", "voetpad": "sidewalk_concrete", "voetgangersgebied": "sidewalk_concrete",
            "verkeerseiland": "italy_cobblestone", "berm": "bne_tunnel_soil"}
RAISED = ("voetpad", "verkeerseiland", "voetgangersgebied")


class Ground:
    def __init__(self, ref):
        d = np.load(os.path.join(ref, "ahn.npz"))
        self.dtm, self.dsm = d["dtm"].astype(np.float64), d["dsm"].astype(np.float64)
        self.res, self.x0, self.y1 = float(d["res"]), float(d["x0"]), float(d["y1"])
        self.H, self.W = self.dtm.shape
        # the ground smoothed (a 3 x 3 median: the lidar's noise, not its walls)
        p = np.pad(self.dtm, 1, mode="edge")
        st = np.stack([p[r:r + self.H, c:c + self.W] for r in range(3) for c in range(3)])
        self.smooth = np.median(st, axis=0)

    def at(self, x, y, a=None):
        a = self.smooth if a is None else a
        fc = (np.asarray(x) - self.x0) / self.res - 0.5
        fr = (self.y1 - np.asarray(y)) / self.res - 0.5
        c0 = np.clip(np.floor(fc).astype(int), 0, self.W - 2)
        r0 = np.clip(np.floor(fr).astype(int), 0, self.H - 2)
        u, v = np.clip(fc - c0, 0, 1), np.clip(fr - r0, 0, 1)
        return ((1 - u) * (1 - v) * a[r0, c0] + u * (1 - v) * a[r0, c0 + 1] + (1 - u) * v * a[r0 + 1, c0]
                + u * v * a[r0 + 1, c0 + 1])

    def range_round(self, x, y, reach=WALL_REACH):
        """(lowest, highest) ground within reach of each point (8 directions and the point)."""
        zs = [self.at(x, y, self.dtm)]
        for k in range(8):
            a = k * math.pi / 4
            for r in (reach * 0.5, reach):
                zs.append(self.at(x + r * math.cos(a), y + r * math.sin(a), self.dtm))
        zs = np.stack(zs)
        return zs.min(0), zs.max(0)


def inside(ring, px, py):
    x, y = ring[:, 0], ring[:, 1]
    ins = np.zeros(len(px), bool)
    j = len(ring) - 1
    for i in range(len(ring)):
        c = ((y[i] > py) != (y[j] > py)) & (px < (x[j] - x[i]) * (py - y[i]) / (y[j] - y[i] + 1e-12) + x[i])
        ins ^= c
        j = i
    return ins


def earcut(ring):
    """Triangles (m, 3) of a simple polygon ring (k, 2), counter-clockwise: ear clipping, an ear only when no other
    corner lies in it (a concave wall's cap never reaches outside it). Pure."""
    ring = np.asarray(ring, np.float64)
    idx = list(range(len(ring)))
    out = []

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    guard = 0
    while len(idx) > 3 and guard < 10 * len(ring) * len(ring):
        guard += 1
        n = len(idx)
        cut = False
        for i in range(n):
            a, b, c = idx[(i - 1) % n], idx[i], idx[(i + 1) % n]
            A, B, C = ring[a], ring[b], ring[c]
            if cross(A, B, C) <= 1e-12:             # reflex or flat: not an ear
                continue
            ok = True
            for j in idx:
                if j in (a, b, c):
                    continue
                P = ring[j]
                if cross(A, B, P) >= -1e-12 and cross(B, C, P) >= -1e-12 and cross(C, A, P) >= -1e-12:
                    ok = False
                    break
            if ok:
                out.append((a, b, c))
                idx.pop(i)
                cut = True
                break
        if not cut:                                 # (degenerate: the flattest corner goes)
            idx.pop(0)
    if len(idx) == 3:
        out.append(tuple(idx))
    return np.asarray(out, np.int64).reshape(-1, 3)


def _inside_ring(ring, pts):
    return inside(ring, pts[:, 0], pts[:, 1])


def open_ring(r):
    r = np.asarray(r, np.float64)
    return r[:-1] if len(r) > 3 and np.allclose(r[0], r[-1]) else r


class Part:
    def __init__(self, name, material):
        self.name, self.material = name, material
        self.P, self.T, self.UV = [], [], []
        self.n = 0

    def add(self, P, T, UV):
        P = np.asarray(P, np.float64)
        self.P.append(P)
        self.T.append(np.asarray(T, np.int64) + self.n)
        self.UV.append(np.asarray(UV, np.float64))
        self.n += len(P)

    def quad_strip(self, a, b, c, d, u_len=None):
        """A quad a b c d (each (3,)), UVs along a-b and up a-d in metres."""
        L = float(np.linalg.norm(np.asarray(b) - np.asarray(a)))
        h0, h1 = float(np.linalg.norm(np.asarray(d) - np.asarray(a))), float(np.linalg.norm(np.asarray(c) - np.asarray(b)))
        self.add([a, b, c, d], [[0, 1, 2], [0, 2, 3]], [[0, 0], [L, 0], [L, h1], [0, h0]])

    def out(self):
        if not self.P:
            return None
        return {"name": self.name, "material": self.material, "positions": np.concatenate(self.P).round(4).tolist(),
                "tris": np.concatenate(self.T).tolist(), "uvs": np.concatenate(self.UV).round(4).tolist()}


def prism(part, ring, z_foot, z_top, uv_m=1.0, thin=None):
    """A vertical prism over the ring (k, 2) from z_foot to z_top (each (k,)): its sides, its top and its foot."""
    import buildings
    ring, z_foot, z_top = np.asarray(ring, np.float64), np.asarray(z_foot, np.float64), np.asarray(z_top, np.float64)
    if buildings._area2(ring) < 0:      # (counter-clockwise: the triangulation's caps fanned out over the junction
        ring, z_foot, z_top = ring[::-1], z_foot[::-1], z_top[::-1]     # from the BGT's clockwise walls)
    k = len(ring)
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(np.r_[ring, ring[:1]], axis=0).T))]
    for i in range(k):
        j = (i + 1) % k
        a = (*ring[i], z_foot[i])
        b = (*ring[j], z_foot[j])
        c = (*ring[j], z_top[j])
        d = (*ring[i], z_top[i])
        part.add([a, b, c, d], [[0, 1, 2], [0, 2, 3]],
                 np.array([[s[i], z_foot[i]], [s[i + 1], z_foot[j]], [s[i + 1], z_top[j]], [s[i], z_top[i]]]) / uv_m)
    tris = earcut(ring)
    if len(tris) and thin is not None:      # a wall's cap: no triangle wider than a wall (a BGT outline crossing
        A, B, C = ring[tris[:, 0]], ring[tris[:, 1]], ring[tris[:, 2]]     # itself spanned the trench, 2026-10-21)
        area = 0.5 * np.abs((B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0]))
        longest = np.max([np.hypot(*(B - A).T), np.hypot(*(C - B).T), np.hypot(*(A - C).T)], axis=0)
        tris = tris[2 * area / np.maximum(longest, 1e-9) <= thin]
    if len(tris):
        part.add(np.c_[ring, z_top], tris, ring / uv_m)
        part.add(np.c_[ring, z_foot], tris[:, ::-1], ring / uv_m)


def railing(part, line, z_foot):
    """Posts every POST_EVERY and two rails along the line (k, 2) standing on z_foot (k,)."""
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
    if s[-1] < 1.0:
        return
    t = np.arange(0, s[-1] + 1e-6, POST_EVERY)
    px, py = np.interp(t, s, line[:, 0]), np.interp(t, s, line[:, 1])
    pz = np.interp(t, s, z_foot)
    w = 0.06
    for x, y, z in zip(px, py, pz):             # posts: a 6 cm square, RAIL_H[1] tall
        sq = np.array([[x - w / 2, y - w / 2], [x + w / 2, y - w / 2], [x + w / 2, y + w / 2], [x - w / 2, y + w / 2]])
        prism(part, sq, np.full(4, z), np.full(4, z + RAIL_H[1] + 0.02), uv_m=0.5)
    for h in RAIL_H:                            # rails: 5 cm tubes as thin boxes between the posts
        for i in range(len(t) - 1):
            a, b = np.array([px[i], py[i]]), np.array([px[i + 1], py[i + 1]])
            dv = b - a
            n = np.array([-dv[1], dv[0]]) / max(np.hypot(*dv), 1e-9) * 0.025
            sq = np.array([a - n, b - n, b + n, a + n])
            za, zb = pz[i] + h, pz[i + 1] + h
            prism(part, sq, np.array([za, zb, zb, za]) - 0.025, np.array([za, zb, zb, za]) + 0.025, uv_m=0.5)


def fit_deck(g, ring):
    lo, hi = ring.min(0), ring.max(0)
    X, Y = np.meshgrid(np.arange(lo[0], hi[0], 0.5), np.arange(lo[1], hi[1], 0.5))
    px, py = X.ravel(), Y.ravel()
    m = inside(ring, px, py)
    if m.sum() < 40:
        return None
    qx, qy = px[m], py[m]
    zs = g.at(qx, qy, g.dsm)
    good = np.isfinite(zs)
    med = np.median(zs[good])
    good &= np.abs(zs - med) < 0.8
    coef, *_ = np.linalg.lstsq(np.c_[np.ones(good.sum()), qx[good], qy[good]], zs[good], rcond=None)
    return coef


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ref")
    a = ap.parse_args(argv)
    import buildings
    import grading
    import plazas
    ref = json.load(open(os.path.join(a.ref, "ref.json"), encoding="utf-8"))
    g = Ground(a.ref)
    feats = ref["features"]
    # the decks
    decks = []
    for f in feats:
        if f["layer"] in ("tunneldeel", "overbruggingsdeel"):
            ring = open_ring(f["rings"][0])
            coef = fit_deck(g, ring)
            if coef is not None:
                decks.append({"layer": f["layer"], "ring": ring.round(3).tolist(), "plane": [float(v) for v in coef]})
    D = [(np.asarray(d["ring"]), np.asarray(d["plane"])) for d in decks]

    def deck_at(x, y, pad=0.0):
        """The deck plane's height at the points (NaN off the decks; pad: m round them counts)."""
        x, y = np.atleast_1d(x), np.atleast_1d(y)
        out = np.full(len(x), np.nan)
        for ring, (p0, p1, p2) in D:
            m = inside(ring, x, y)
            if pad:
                c = ring.mean(0)
                grown = c + (ring - c) * (1 + pad / max(np.hypot(*(ring - c).T).mean(), 1.0))
                m |= inside(grown, x, y)
            out = np.where(m & np.isnan(out), p0 + p1 * x + p2 * y, out)
        return out
    # the model's area: the trench and the decks, padded
    trench = np.load(os.path.join(a.ref, "trench_mask.npy"))
    k = int(AREA_PAD / g.res)
    area = trench.copy()
    for _ in range(k):
        p = np.pad(area, 1)
        area = area | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]

    def in_area(x, y):
        c = np.clip(((np.asarray(x) - g.x0) / g.res).astype(int), 0, g.W - 1)
        r = np.clip(((g.y1 - np.asarray(y)) / g.res).astype(int), 0, g.H - 1)
        return area[r, c] | np.isfinite(deck_at(x, y, pad=4.0))

    parts = {}

    def part(name, mat):
        if name not in parts:
            parts[name] = Part(name, mat)
        return parts[name]
    deck_p = part("decks", "bne_bridge_concrete")
    for ring, (p0, p1, p2) in D:
        zt = p0 + p1 * ring[:, 0] + p2 * ring[:, 1] - 0.05
        prism(deck_p, ring, zt - DECK_UNDER + 0.05, zt, uv_m=4.0)

    # the walls
    wall_p = part("walls", "bne_bridge_concrete")
    rail_p = part("railings", "bne_truss_steel")
    for f in feats:
        if f["layer"] != "scheiding":
            continue
        fn = f["functie"].lower()
        ring = open_ring(f["rings"][0])
        if len(ring) < 2 or not in_area(ring[:, 0], ring[:, 1]).any():
            continue
        if fn == "hek":                             # a fence: a railing along its line
            line = ring if not (len(f["rings"][0]) > 3 and np.allclose(f["rings"][0][0], f["rings"][0][-1])) else ring
            line = grading.resample(line, 1.0)[0]
            dz = deck_at(line[:, 0], line[:, 1])
            railing(rail_p, line, np.where(np.isfinite(dz), dz, g.at(line[:, 0], line[:, 1])))
            continue
        if "muur" not in fn:
            continue
        closed = len(f["rings"][0]) > 3 and np.allclose(f["rings"][0][0], f["rings"][0][-1])
        if not closed:                              # a wall drawn as a line: 0.3 m thick round it
            line = grading.resample(ring, 1.0)[0]
            dv = np.gradient(line, axis=0)
            dv /= np.maximum(np.hypot(*dv.T), 1e-9)[:, None]
            nl = np.c_[-dv[:, 1], dv[:, 0]] * 0.15
            ring = np.r_[line + nl, (line - nl)[::-1]]
        # its corners densified (so its foot and top follow the ground), straight edges stay straight
        dense = grading.resample(np.r_[ring, ring[:1]], 2.0)[0][:-1]
        x, y = dense[:, 0], dense[:, 1]
        low, high = g.range_round(x, y)
        dk = deck_at(x, y, pad=1.5)
        level = f["level"]
        if np.isfinite(dk).mean() > 0.5 and level >= 0:           # a deck's parapet: its edge beam and the wall over it
            foot = dk - DECK_UNDER
            top = dk + PARAPET
        elif level < 0 or np.isfinite(dk).mean() > 0.5:           # under a deck: an abutment to its underside
            foot = low - 0.3
            under = np.where(np.isfinite(dk), dk - DECK_UNDER, high)
            top = np.maximum(under, foot + 0.5)
        else:
            foot = low - 0.3
            rise = high - low
            top = np.where(rise > 0.5, high + 0.05, high + 0.6)   # a retaining wall's top at the ground behind it
        # its plan the BGT's (straight edges, every 2 m a point so its top and foot follow the ground along it); its
        # top and foot the median / lowest within 3 m (the lidar's noise)
        ring = dense
        kt = np.empty(len(ring))
        kf = np.empty(len(ring))
        for i, q in enumerate(ring):
            near = np.hypot(*(dense - q).T) < 3.0
            kt[i] = np.median(top[near])
            kf[i] = np.min(foot[near])
        prism(wall_p, ring, kf, kt, uv_m=4.0, thin=2.0)
        if level == 0 and not np.isfinite(dk).any() and (kt - kf).mean() > 0.8:
            # a retaining wall: a railing on it along its face (its outline's edges toward the low side), 15 cm in
            import buildings
            rr, rt = (ring, kt) if buildings._area2(ring) > 0 else (ring[::-1], kt[::-1])
            n = len(rr)
            face = np.zeros(n, bool)
            for i in range(n):
                a_, b_ = rr[i], rr[(i + 1) % n]
                d_ = b_ - a_
                L = np.hypot(*d_)
                if L < 0.5:
                    continue
                outn = np.array([d_[1], -d_[0]]) / L          # right of a counter-clockwise ring: outward
                m_ = (a_ + b_) / 2
                face[i] = float(g.at(*(m_ + outn * 1.5), g.dtm)) < (rt[i] + rt[(i + 1) % n]) / 2 - 0.8
            i = 0
            while i < n:                                      # each run of face edges: one railing
                if not face[i]:
                    i += 1
                    continue
                j = i
                while j < n and face[j]:
                    j += 1
                idx = list(range(i, min(j, n - 1) + 1)) if j < n else list(range(i, n)) + [0]
                pts = rr[idx]
                if len(pts) >= 2 and np.hypot(*(pts[-1] - pts[0])) > 2.0:
                    dv = np.gradient(pts, axis=0)
                    dv /= np.maximum(np.hypot(*dv.T), 1e-9)[:, None]
                    inn = np.c_[-dv[:, 1], dv[:, 0]]           # left of the run: into the wall
                    line = grading.resample(pts + inn * 0.15, 1.0)[0]
                    zl = np.interp(np.r_[0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))],
                                   np.r_[0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))], rt[idx])
                    railing(rail_p, line, zl)
                i = j

    # the road surfaces
    kerb_p = part("kerbs", "curbs")
    raised_rings = []
    for f in feats:
        if f["layer"] not in ("wegdeel", "ondersteunendwegdeel"):
            continue
        fn = f["functie"].lower()
        key = next((k for k in SURF_MAT if k in fn), None)
        if key is None:
            continue
        rings = [open_ring(r) for r in f["rings"]]
        ring = rings[0]
        if len(ring) < 3 or not in_area(ring[:, 0], ring[:, 1]).any():
            continue
        if buildings._area2(ring) < 0:
            ring = ring[::-1]
        tris = np.asarray(buildings.triangulate(ring), np.int64).reshape(-1, 3)
        if not len(tris):
            continue
        P, T = plazas._subdivide(ring, tris, STEP_M)
        c = P[T].mean(1)
        keep = in_area(c[:, 0], c[:, 1])
        for h in rings[1:]:
            if len(h) >= 3:
                keep &= ~plazas._inside_many(h, c)
        if not keep.any():
            continue
        T = T[keep]
        used = np.unique(T)
        remap = np.full(len(P), -1, np.int64)
        remap[used] = np.arange(len(used))
        P, T = P[used], remap[T]
        dz = deck_at(P[:, 0], P[:, 1]) if f["level"] >= 0 else np.full(len(P), np.nan)
        z = np.where(np.isfinite(dz), dz, g.at(P[:, 0], P[:, 1]))
        up = KERB_M if key in RAISED else 0.0
        part("surface_" + key, SURF_MAT[key]).add(np.c_[P, z + 0.03 + up], T, P)
        if up:
            raised_rings.append((ring, f["level"]))
    # kerbs: along a raised surface's edges, from the road up
    for ring, level in raised_rings:
        e = grading.resample(np.r_[ring, ring[:1]], 1.0)[0]
        if len(e) < 2 or not in_area(e[:, 0], e[:, 1]).any():
            continue
        dz = deck_at(e[:, 0], e[:, 1]) if level >= 0 else np.full(len(e), np.nan)
        z = np.where(np.isfinite(dz), dz, g.at(e[:, 0], e[:, 1])) + 0.03
        for i in range(len(e) - 1):
            a_, b_ = e[i], e[i + 1]
            kerb_p.quad_strip((*a_, z[i]), (*b_, z[i + 1]), (*b_, z[i + 1] + KERB_M), (*a_, z[i] + KERB_M))

    out = {"origin_rd": ref["origin_rd"], "origin_wgs": ref["origin_wgs"],
           "parts": [p.out() for p in parts.values() if p.out() is not None]}
    dst = os.path.join(os.path.dirname(os.path.abspath(a.ref)), "parts.json")
    json.dump(out, open(dst, "w"))
    json.dump({"decks": decks}, open(os.path.join(os.path.dirname(os.path.abspath(a.ref)), "decks.json"), "w"))
    for p in out["parts"]:
        print(f"{p['name']:24s} {p['material']:22s} tris {len(p['tris'])}")
    print("->", dst)


if __name__ == "__main__":
    main()
