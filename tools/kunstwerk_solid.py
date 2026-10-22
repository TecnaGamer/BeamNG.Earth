#!/usr/bin/env python3
"""A kunstwerk built as one closed surface: the plan is a single triangulation of the whole area (no holes possible),
each triangle a column of solid (the ground up to its surface; in the trench the floor, and under a deck the slab
over it too), and the faces are the columns' tops, the decks' undersides, and the sides where neighbouring columns
differ (kerbs where they differ a little, concrete walls where they differ a lot).

The plan is designed, not traced (the user, 2026-10-21: "logisch dat de baan klopt, geen 1:1 van de map"):
- the trench's carriageways are smooth curves (tools/kunstwerk_design.py's fit) with a fixed width and a smooth floor
  profile; its walls are those curves' edges, its decks clean slabs square to them where the BGT has its passages;
- the streets at grade are the BGT's surfaces rounded off (each kind's area blurred, the strongest kind wins, its edge
  the blur's contour: no slivers, no jags), at the AHN's height smoothed per kind;
- copings and railings on the walls, parapets on the decks' ends, lane markings in the trench, lamp posts.

    python3 tools/kunstwerk_solid.py ../design/kunstwerken/droogleever/ref

Writes ../parts.json and ../decks.json (Blender: tools/blender_kunstwerk_import.py; the game: tools/kunstwerk_export.py).
"""
import argparse
import json
import os
import sys

import contourpy
import numpy as np
from matplotlib.path import Path
from scipy.ndimage import distance_transform_edt as ndi_edt, gaussian_filter
from scipy.spatial import Delaunay

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kunstwerk_build import Ground, Part, earcut, prism, railing  # noqa: E402
from kunstwerk_design import carriageways, smooth  # noqa: E402

RES = 0.25              # m: the plan raster
GRID = 2.5              # m between the triangulation's free points
EDGE_STEP = 0.5         # m between points along an edge
EDGE_CLEAR = 1.0        # m kept free of grid points either side of an edge (twice the side rows' offset)
AREA_T = (-110.0, 120.0)  # m along the trench's axis the kunstwerk reaches
AREA_W = 92.0           # m to either side of it
LANE, SHOULDER = 3.25, 0.5
DECK_T = 0.9            # m: a deck's slab
PARAPET = (0.35, 1.0)   # m: a deck end's parapet (thick, over the deck)
COPING = (0.5, 0.1)     # m: a wall's coping (wide, over the ground behind)
PARAPET_T = 0.35        # m: the trench's parapet's thickness
BGT_WALL_UP = 1.4       # m: the BGT's walls' tops over the ground behind them (eye height by the tram, Street View A200)
PARAPET_UP = 1.1        # m: its top over the ground behind it
CUT_WALL_FROM_M = 0.8   # m the sunk ground lies under the streets beside it before a parapet stands on the edge
RAIL_FROM = 1.0         # m a wall drops before it gets a railing
KERB_MAX = 0.35         # m: a step up to this between paved surfaces is a kerb, more a wall
BASE_SMOOTH_M = 2.0     # m: the grass's smoothing
STREET_SMOOTH_M = 4.0   # m: the streets'
SUNK_UNDER_M = 1.8      # m under the streets' typical height a place is sunk (a cut)
LEVEL_SMOOTH_M = 1.0    # m: the levels' rounding
GRASS_JOIN_M = 3.0      # m over which a verge goes from its street's level to the ground's
CUT_MEET_M = 0.3        # m: a carriageway joins the cut round it where its floor is this near the cut's streets
CLEARANCE = 4.5         # m: the least headroom under a deck (the Dutch standard for a new one)
DIP_GRADE = 0.04        # the steepest the floor dips to it
BLUR_M = 0.7            # m: the plan's rounding
LAMP_H = 8.0
TRAM_POLE_M = 3.5       # m: a light mast this near a tram track is the tram line's
STOP_W = (0.2, 0.45)    # m: a stop line's width
STOP_L = (2.4, 16.0)    # m: and its length (a lane or more)
STOP_REACH = 45.0       # m: at most this from its junction's signals
MAST_FROM_M = 9.0       # m: a stop line this long (three lanes) gets a mast with an arm
JUNCTION_M = 22.0       # m: signals this near each other (chained) are one junction's
SIGNAL_H = 3.4          # m: a traffic light's pole
RAIL_W, RAIL_UP = 0.07, 0.025   # m: a tram rail's head as seen (with its groove), over the street or the grass
RED_LIFT = 0.03          # m: a red crossing over its asphalt (clear of it in the depth buffer; the white over it)
PAINT_LIFT = 0.045       # m: paint over its street (the red of a crossing half that)

# the plan's kinds: name, material, lift over the streets' surface (m), the BGT's functions painted as it (in painting order: the
# later win where they overlap)
KINDS = [
    ("grass", "bne_grass", 0.04, ("berm",)),
    ("island", "sidewalk_concrete", 0.12, ("verkeerseiland",)),     # (grey setts, Street View A330)
    ("foot", "sidewalk_concrete", 0.12, ("voetpad", "voetpad op trap")),
    ("cycle", "bne_cycle_red", 0.02, ("fietspad",)),
    ("tram", "bne_grass", 0.10, ("OV-baan:half verhard",)),     # the tram's grass bed (its rails drawn on it)
    ("platform", "sidewalk_concrete", 0.30, ("perron",)),      # a tram stop's platform (OSM's, ref/osm_map.xml)
    ("road", "road_asphalt_2lane", 0.0, ("rijbaan lokale weg", "OV-baan", "parkeervlak")),
]
GRASS, ROAD = 0, len(KINDS) - 1
TRENCH = len(KINDS)     # a triangle in a carriageway's strip


def osm_platforms(ref):
    """OSM's tram platforms (ref/osm_map.xml: closed highway=platform ways) as plan features of the kind "perron"."""
    import xml.etree.ElementTree as ET
    path = os.path.join(REF_DIR[0], "osm_map.xml") if REF_DIR else None
    if not path or not os.path.exists(path):
        return []
    sys.path.insert(0, os.path.join(HERE, "..", "server"))
    import ahn
    root = ET.parse(path).getroot()
    nodes = {n.get("id"): n for n in root.findall("node")}
    o = ref["origin_rd"]
    out = []
    for w in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        refs = [nd.get("ref") for nd in w.findall("nd")]
        if tags.get("highway") != "platform" or len(refs) < 4 or refs[0] != refs[-1]:
            continue
        ring = []
        for r_ in refs:
            x, y = ahn.wgs_to_rd(float(nodes[r_].get("lat")), float(nodes[r_].get("lon")))
            ring.append([x - o[0], y - o[1]])
        out.append({"layer": "wegdeel", "functie": "perron", "level": 0, "rings": [ring]})
    return out


REF_DIR = []


class Plan:
    """The plan: per kind a blurred share on RES cells over the area's box; the kind of a point is the strongest."""

    def __init__(self, ref, frame, box):
        self.x0, self.y0, self.x1, self.y1 = box
        self.nx = int(np.ceil((self.x1 - self.x0) / RES))
        self.ny = int(np.ceil((self.y1 - self.y0) / RES))
        lab = np.zeros((self.ny, self.nx), np.int8)
        gx = self.x0 + (np.arange(self.nx) + 0.5) * RES
        gy = self.y0 + (np.arange(self.ny) + 0.5) * RES
        by_fn = {fn: k for k, (_, _, _, fns) in enumerate(KINDS) for fn in fns}

        def kind_key(f):
            """The BGT surface's key in KINDS: its function, or its function and how it is surfaced where that has a
            kind of its own (a tram's half-paved bed is grass, not road)."""
            fn = f.get("functie") or ""
            both = f"{fn}:{f.get('voorkomen')}"
            return both if both in by_fn else fn
        feats = [f for f in ref["features"] if f["layer"] in ("wegdeel", "ondersteunendwegdeel") and f["level"] >= 0
                 and kind_key(f) in by_fn]
        feats += osm_platforms(ref)
        feats.sort(key=lambda f: by_fn[kind_key(f)])
        for f in feats:
            k = by_fn[kind_key(f)]
            for ring in f["rings"][:1]:
                r = np.asarray(ring)
                c0, c1 = np.searchsorted(gx, [r[:, 0].min(), r[:, 0].max()])
                r0, r1 = np.searchsorted(gy, [r[:, 1].min(), r[:, 1].max()])
                if c1 <= c0 or r1 <= r0:
                    continue
                X, Y = np.meshgrid(gx[c0:c1], gy[r0:r1])
                m = Path(r).contains_points(np.c_[X.ravel(), Y.ravel()]).reshape(X.shape)
                if len(f["rings"]) > 1:         # holes
                    for h in f["rings"][1:]:
                        m &= ~Path(np.asarray(h)).contains_points(np.c_[X.ravel(), Y.ravel()]).reshape(X.shape)
                lab[r0:r1, c0:c1][m] = k
        self.share = np.stack([gaussian_filter((lab == k).astype(np.float32), BLUR_M / RES) for k in range(len(KINDS))])
        self.gx, self.gy = gx, gy

    def _bilinear(self, a, x, y):
        fc = np.clip((np.asarray(x) - self.x0) / RES - 0.5, 0, self.nx - 1.001)
        fr = np.clip((np.asarray(y) - self.y0) / RES - 0.5, 0, self.ny - 1.001)
        c, r = fc.astype(int), fr.astype(int)
        u, v = fc - c, fr - r
        return (a[..., r, c] * (1 - u) * (1 - v) + a[..., r, c + 1] * u * (1 - v) + a[..., r + 1, c] * (1 - u) * v
                + a[..., r + 1, c + 1] * u * v)

    def kind(self, x, y):
        return np.argmax(self._bilinear(self.share, x, y), axis=0)

    def road_edges(self):
        """The road's own edges (where it meets anything else): polylines (k, 2) and per point the way into the road."""
        f = self.share[ROAD] - np.delete(self.share, ROAD, axis=0).max(0)
        out = []
        for ln in contourpy.contour_generator(self.gx, self.gy, f, line_type="Separate").lines(0.0):
            if len(ln) < 3:
                continue
            ln = resample(ln, 1.0)
            dv = np.gradient(ln, axis=0)
            nv = np.c_[-dv[:, 1], dv[:, 0]] / np.maximum(np.hypot(*dv.T), 1e-9)[:, None]
            probe = self._bilinear(self.share[ROAD], *(ln + nv * 0.5).T) - self._bilinear(self.share[ROAD], *(ln - nv * 0.5).T)
            out.append((ln, nv * np.sign(np.median(probe))))
        return out

    def edges(self):
        """The boundaries between kinds: polylines (k, 2) where the strongest kind changes."""
        out = []
        for k in range(len(KINDS)):
            others = np.delete(self.share, k, axis=0).max(0)
            f = self.share[k] - others
            gen = contourpy.contour_generator(self.gx, self.gy, f, line_type="Separate")
            out += [ln for ln in gen.lines(0.0) if len(ln) >= 3]
        return out


class Heights:
    """The streets on smooth surfaces of their own, one per level: each place is sunk (the AHN's ground well under the
    streets around: SUNK_UNDER_M under their typical height, the lidar's holes (cars) taking their surroundings') or at
    grade; per level the AHN over the plan's paved cells of that level only (the deck's top where the lidar sees a
    deck), a normalised Gaussian over STREET_SMOOTH_M: flat as laid, not the lidar's ripple, and a street in a cut not
    pulled up to the street above it (north of the ring the whole road lies in a cut 50 m wide, 2026-10-21). Per kind a
    fixed lift over its street (footways and islands a kerb up); the grass the ground's own, finer, but level with the
    streets along their edges (GRASS_JOIN_M) so a verge meets its kerb. Where the levels meet the columns' sides are
    the walls."""

    def __init__(self, g, plan, trench_ahn):
        kind = np.load(os.path.join(g.ref, "ahn.npz"))["kind"]
        h = np.where(kind == 2, g.dsm, g.dtm)
        ok = (kind != 1) & np.isfinite(h) & (~trench_ahn | (kind == 2))      # a deck's top is a street's
        h = np.where(ok, h, 0.0)
        H, W = h.shape
        X, Y = np.meshgrid(g.x0 + (np.arange(W) + 0.5) * g.res, g.y1 - (np.arange(H) + 0.5) * g.res)
        paved = plan.kind(X.ravel(), Y.ravel()).reshape(H, W) != GRASS

        def normconv(mask, sigma_m, a=h):
            s = sigma_m / g.res
            den = gaussian_filter(mask.astype(float), s)
            return gaussian_filter(a * mask, s) / np.maximum(den, 1e-6), den
        # the levels
        ground_ok = (kind == 0) & np.isfinite(g.dtm)
        typical = float(np.percentile(g.dtm[ground_ok & paved], 60))
        raw = (np.where(ground_ok, g.dtm, 99.0) < typical - SUNK_UNDER_M).astype(float)
        self.sunk, _ = normconv(ground_ok, LEVEL_SMOOTH_M, raw)          # (0..1: the share of ground sunk around)
        sunk = self.sunk > 0.5
        far, _ = normconv(ok, 12.0)
        streets = []
        for lvl in (sunk, ~sunk):
            st, den = normconv(ok & paved & lvl, STREET_SMOOTH_M)
            wide, wden = normconv(ok & paved & lvl, 12.0)
            wide = np.where(wden > 1e-3, wide, far)
            w = np.clip(den / 0.2, 0, 1)
            streets.append(st * w + wide * (1 - w))
        self.street_lo, self.street_hi = streets
        street = np.where(sunk, self.street_lo, self.street_hi)
        ground, den = normconv(ok, BASE_SMOOTH_M)
        w = np.clip(den / 0.3, 0, 1)
        ground = ground * w + far * (1 - w)
        d = ndi_edt(~paved) * g.res
        j = np.clip((d - 0.5) / GRASS_JOIN_M, 0, 1)
        self.grass = street * (1 - j) + ground * j
        self.g = g
        print(f"levels: streets typically at {typical:.2f} m, sunk under {typical - SUNK_UNDER_M:.2f} m: "
              f"{sunk.mean() * 100:.0f}% of the box")

    def level(self, x, y):
        """True where the place is sunk."""
        return self.g.at(x, y, self.sunk) > 0.5

    def at(self, k, x, y, sunk=None):
        if k == GRASS:
            return self.g.at(x, y, self.grass) + KINDS[k][2]
        sunk = self.level(x, y) if sunk is None else sunk
        return np.where(sunk, self.g.at(x, y, self.street_lo), self.g.at(x, y, self.street_hi)) + KINDS[k][2]

    def level_edges(self):
        """The lines where the levels meet (the sunk share's 0.5 contour), as polylines (k, 2)."""
        g = self.g
        H, W = self.sunk.shape
        xs = g.x0 + (np.arange(W) + 0.5) * g.res
        ys = g.y1 - (np.arange(H) + 0.5) * g.res
        gen = contourpy.contour_generator(xs, ys[::-1], self.sunk[::-1], line_type="Separate")
        return [ln for ln in gen.lines(0.5) if len(ln) >= 3]


class Carriageway:
    def __init__(self, name, f, frame, t0, t1, width, g, decks_bgt):
        self.name, self.f = name, f
        self.c0, self.u, self.n = frame
        self.W = max(width, 2 * LANE) + 2 * SHOULDER
        self.t0, self.t1 = t0, t1
        ts = np.arange(t0, t1 + 1e-6, 1.0)
        mid = self.xy(ts, 0.0)
        s = np.r_[0, np.cumsum(np.hypot(*np.diff(mid, axis=0).T))]
        under = np.zeros(len(ts), bool)
        for ring in decks_bgt:
            under |= Path(ring).contains_points(mid)
        zc = g.at(mid[:, 0], mid[:, 1])
        self.ts, self.s, self.floor = ts, s, smooth(zc, s, 30.0)
        # decks: per BGT passage the t range its ring covers across the strip, square to the carriageway
        self.decks = []
        for ring in decks_bgt:
            tt = np.arange(t0, t1, 0.25)
            hit = np.zeros(len(tt), bool)
            for w in np.linspace(-self.W / 2, self.W / 2, 7):
                hit |= Path(ring).contains_points(self.xy(tt, w))
            if hit.sum() >= 4:
                self.decks.append([float(tt[hit].min()) - 0.25, float(tt[hit].max()) + 0.25])

    def xy(self, t, w):
        t = np.atleast_1d(np.asarray(t, np.float64))
        w = np.broadcast_to(np.asarray(w, np.float64), t.shape)
        return self.c0 + np.outer(t, self.u) + np.outer(self.f(t) + w, self.n)

    def tw(self, x, y):
        d = np.c_[x, y] - self.c0
        t = d @ self.u
        return t, d @ self.n - self.f(t)

    def inside(self, x, y):
        t, w = self.tw(x, y)
        return (t >= self.t0) & (t <= self.t1) & (np.abs(w) <= self.W / 2)

    def deck_at(self, x, y):
        t, _ = self.tw(x, y)
        out = np.zeros(len(t), bool)
        for a, b in self.decks:
            out |= (t >= a) & (t <= b)
        return out & self.inside(x, y)

    def floor_at(self, x, y):
        t, _ = self.tw(x, y)
        return np.interp(t, self.ts, self.floor)

    def edges(self):
        tt = np.arange(self.t0, self.t1 + 1e-6, EDGE_STEP)
        out = [self.xy(tt, self.W / 2), self.xy(tt, -self.W / 2)]
        for a, b in self.decks:
            for t in (a, b):
                out.append(self.xy(np.full(9, t), np.linspace(-self.W / 2, self.W / 2, 9)))
        return out


def resample(line, step):
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
    if s[-1] < step:
        return line[[0, -1]]
    t = np.linspace(0, s[-1], int(np.ceil(s[-1] / step)) + 1)
    return np.c_[np.interp(t, s, line[:, 0]), np.interp(t, s, line[:, 1])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ref")
    a = ap.parse_args(argv)
    ref = json.load(open(os.path.join(a.ref, "ref.json"), encoding="utf-8"))
    REF_DIR[:] = [a.ref]
    g = Ground(a.ref)
    g.ref = a.ref
    frame, lines = carriageways(a.ref)
    c0, u, n = frame
    corners = np.array([c0 + u * t + n * w for t in AREA_T for w in (-AREA_W, AREA_W)])[[0, 1, 3, 2]]
    box = (*np.floor(corners.min(0)), *np.ceil(corners.max(0)))

    def in_area(x, y):
        d = np.c_[x, y] - c0
        t, w = d @ u, d @ n
        return (t >= AREA_T[0]) & (t <= AREA_T[1]) & (np.abs(w) <= AREA_W)

    plan = Plan(ref, frame, box)
    decks_bgt = [np.asarray(f["rings"][0]) for f in ref["features"] if f["layer"] == "tunneldeel"]
    cws = [Carriageway(nm, f, frame, max(ta, AREA_T[0] - 1), min(tb, AREA_T[1] + 1), wd, g, decks_bgt)
           for nm, (f, ta, tb, wd) in lines.items()]
    for c in cws:
        print(f"{c.name}: width {c.W:.2f} m, floor {c.floor.min():.2f}..{c.floor.max():.2f}, decks "
              + ", ".join(f"t {d0:.1f}..{d1:.1f}" for d0, d1 in c.decks))
    # the trench on the AHN's grid (its cells are left out of the streets' heights)
    H, W = g.dtm.shape
    AX, AY = np.meshgrid(g.x0 + (np.arange(W) + 0.5) * g.res, g.y1 - (np.arange(H) + 0.5) * g.res)
    trench_ahn = np.load(os.path.join(a.ref, "trench_mask.npy"))
    for c in cws:
        t, w = c.tw(AX.ravel(), AY.ravel())
        trench_ahn |= ((t >= c.t0) & (t <= c.t1) & (np.abs(w) <= c.W / 2 + 1.0)).reshape(AX.shape)
    hts = Heights(g, plan, trench_ahn)
    # a carriageway's own strip only where the trench is its own: where the ground either side is sunk too (north of
    # the ring the whole road lies in one wide cut) it is a street of the cut like the others
    for c in cws:
        tt = np.arange(c.t0, c.t1 + 1e-6, 1.0)
        both = hts.level(*c.xy(tt, c.W / 2 + 2.0).T) & hts.level(*c.xy(tt, -c.W / 2 - 2.0).T)
        # (and only where its floor meets the cut's streets: where it lies lower it stays a trench of its own, its
        # walls the cut's, else a wall stood across it where it ended, 2026-10-21)
        cut = np.minimum(hts.at(ROAD, *c.xy(tt, c.W / 2 + 2.0).T, sunk=True),
                         hts.at(ROAD, *c.xy(tt, -c.W / 2 - 2.0).T, sunk=True))
        both &= np.abs(cut - np.interp(tt, c.ts, c.floor)) < CUT_MEET_M
        for d0, d1 in c.decks:
            both &= ~((tt >= d0 - 3) & (tt <= d1 + 3))
        keep = np.flatnonzero(~both)
        if len(keep):
            t0, t1 = float(tt[keep[0]]), float(tt[keep[-1]])
            if (t0, t1) != (c.t0, c.t1):
                print(f"{c.name}: own strip t {t0:.0f}..{t1:.0f} (was {c.t0:.0f}..{c.t1:.0f}; beyond: in the cut)")
            c.t0, c.t1 = t0, t1
    # headroom: under each deck the floor at least CLEARANCE below its underside, dipping to it at DIP_GRADE
    for c in cws:
        for d0, d1 in c.decks:
            tt = np.linspace(d0, d1, 12)
            pp = np.concatenate([c.xy(tt, w) for w in (-c.W / 2, 0.0, c.W / 2)])
            top = float(np.min(hts.at(ROAD, *pp.T)))           # (its lowest: the deck slopes, round 5 mesh check)
            need = top - DECK_T - CLEARANCE
            under = (c.ts >= d0 - 1) & (c.ts <= d1 + 1)
            dip = max(0.0, float(c.floor[under].max()) - need)
            if dip <= 0:
                continue
            run = dip / DIP_GRADE
            dist = np.where(c.ts < d0 - 1, d0 - 1 - c.ts, np.where(c.ts > d1 + 1, c.ts - d1 - 1, 0.0))
            shape = np.where(dist <= 0, 1.0, np.where(dist < run, 0.5 + 0.5 * np.cos(np.pi * dist / run), 0.0))
            c.floor = np.minimum(c.floor, c.floor - dip * shape + 0 * shape)
            print(f"{c.name}: deck t {d0:.0f}..{d1:.0f} top {top:.2f}, floor lowered {dip:.2f} m (headroom {CLEARANCE} m)")

    def in_trench(x, y):
        out = np.full(len(x), -1)
        for i, c in enumerate(cws):
            out[(out < 0) & c.inside(x, y)] = i
        return out

    # --- the triangulation: the area's outline, the trench's edges, the kinds' edges (outside the trench), a grid
    edge_lines = []
    for ln in plan.edges() + hts.level_edges():
        ln = resample(ln, EDGE_STEP)
        on_deck = np.zeros(len(ln), bool)
        for c in cws:
            on_deck |= c.deck_at(*ln.T)
        keep = in_area(*ln.T) & ((in_trench(*ln.T) < 0) | on_deck)     # (a deck's top is streets: its edges too)
        # cut where the line leaves the area or enters the trench
        for run in np.split(np.arange(len(ln)), np.flatnonzero(np.diff(keep.astype(int)) != 0) + 1):
            if keep[run[0]] and len(run) >= 2:
                edge_lines.append(ln[run])
    for c in cws:
        for ln in c.edges():
            ln = ln[in_area(*ln.T)]
            if len(ln) >= 2:
                edge_lines.append(ln)
    outline = np.r_[corners, corners[:1]]
    edge_lines.append(resample(outline, 1.0))
    EP = np.concatenate(edge_lines)
    # the free points: a grid, none within EDGE_CLEAR of an edge
    gx, gy = np.meshgrid(np.arange(box[0], box[2], GRID), np.arange(box[1], box[3], GRID))
    gp = np.c_[gx.ravel(), gy.ravel()]
    gp = gp[in_area(*gp.T)]
    from scipy.spatial import cKDTree
    d, _ = cKDTree(EP).query(gp)
    gp = gp[d > EDGE_CLEAR]
    # and a row either side of each edge at EDGE_CLEAR / 2 (over half the edge's step: no circle through points of both
    # rows is empty of the edge's, so no triangle reaches across it)
    side = []
    for ln in edge_lines:
        if len(ln) < 2:
            continue
        dv = np.gradient(ln, axis=0)
        nv = np.c_[-dv[:, 1], dv[:, 0]] / np.maximum(np.hypot(*dv.T), 1e-9)[:, None]
        side += [ln + nv * EDGE_CLEAR / 2, ln - nv * EDGE_CLEAR / 2]
    SP = np.concatenate(side)
    SP = SP[in_area(*SP.T)]
    if len(SP):
        d, _ = cKDTree(EP).query(SP)
        SP = SP[d > EDGE_CLEAR * 0.4]
    # the trench's edges first: nothing else nearer one than its own side rows
    TE = np.concatenate([ln for c in cws for ln in c.edges()])
    TE = TE[in_area(*TE.T)]
    rest = np.r_[EP, SP, gp]
    d, _ = cKDTree(TE).query(rest)
    rest = rest[(d > EDGE_CLEAR * 0.45) | (d < 1e-6)]
    pts = np.unique(np.round(np.r_[TE, rest], 3), axis=0)
    tri = Delaunay(pts)
    T = tri.simplices
    cen = pts[T].mean(1)
    keep = in_area(*cen.T)
    T, cen = T[keep], cen[keep]
    # each triangle's kind and its column
    tk = plan.kind(*cen.T)
    tc = in_trench(*cen.T)
    tdeck = np.zeros(len(T), bool)
    for i, c in enumerate(cws):
        tdeck |= (tc == i) & c.deck_at(*cen.T)
    print(f"{len(pts)} points, {len(T)} triangles; trench {np.sum(tc >= 0)}, decks {tdeck.sum()}")
    # the check: no triangle across the trench's edge (its walls would zigzag)
    bad = 0
    for i, c in enumerate(cws):
        vin = np.stack([np.abs(c.tw(*pts[T[:, j]].T)[1]) for j in range(3)], 1)
        bad += int(np.sum((tc == i) & (vin > c.W / 2 + 0.02).any(1)))
        bad += int(np.sum((tc != i) & (vin < c.W / 2 - 0.02).any(1) & c.inside(*cen.T).__invert__()
                          & np.stack([c.inside(*pts[T[:, j]].T) for j in range(3)], 1).any(1)))
    print(f"triangles across the trench's edge: {bad}")
    if os.environ.get("KW_DEBUG"):
        for i, c in enumerate(cws):
            vin = np.stack([np.abs(c.tw(*pts[T[:, j]].T)[1]) for j in range(3)], 1)
            k = np.flatnonzero((tc == i) & (vin > c.W / 2 + 0.02).any(1))
            for j in k[:12]:
                print(c.name, cen[j].round(1), c.tw(*cen[j][:, None])[0].round(1), vin[j].round(2), pts[T[j]].round(2).tolist())

    # per-vertex heights, per (kind) and per carriageway floor
    ZL = np.stack([hts.at(k, pts[:, 0], pts[:, 1], sunk=True) for k in range(len(KINDS))])
    ZH = np.stack([hts.at(k, pts[:, 0], pts[:, 1], sunk=False) for k in range(len(KINDS))])
    tl = hts.level(*cen.T)
    F = np.stack([c.floor_at(pts[:, 0], pts[:, 1]) for c in cws])
    # a deck's top: its street's height, but level across the carriageway (a slab, not a warp)

    def columns(ti):
        """The triangle's solid as intervals per vertex: [(lo (3,), hi (3,), top kind)]; lo -inf: the ground."""
        v = T[ti]
        k = tk[ti]
        Z = ZL if tl[ti] else ZH
        if tc[ti] < 0:
            return [(None, Z[k, v], k)]
        out = [(None, F[tc[ti], v], TRENCH)]
        if tdeck[ti]:
            top = ZH[k, v]
            out.append((top - DECK_T, top, k))
        return out

    parts = {}

    def part(name, mat):
        parts.setdefault(name, Part(name, mat))
        return parts[name]
    top_part = {k: part("road" if k == ROAD else f"street_{nm}", mat) for k, (nm, mat, _, _) in enumerate(KINDS)}
    top_part[TRENCH] = top_part[ROAD]
    walls = part("walls", "bne_concrete_dark")
    kerbs = part("kerbs", "curbs")
    cols = [columns(i) for i in range(len(T))]
    # tops and undersides
    for i in range(len(T)):
        v = T[i]
        P2 = pts[v]
        for lo, hi, k in cols[i]:
            P3 = np.c_[P2, hi]
            if (P2[1, 0] - P2[0, 0]) * (P2[2, 1] - P2[0, 1]) - (P2[1, 1] - P2[0, 1]) * (P2[2, 0] - P2[0, 0]) < 0:
                tri3 = [[0, 2, 1]]
            else:
                tri3 = [[0, 1, 2]]
            top_part[k].add(P3, tri3, P2)
            if lo is not None:
                walls.add(np.c_[P2, lo], [tri3[0][::-1]], P2)
    # sides: per edge between two triangles, what one's solid has and the other's lacks, facing the other
    nb = tri.neighbors
    idx = -np.ones(len(tri.simplices), int)
    idx[np.flatnonzero(keep)] = np.arange(len(T))
    nb = nb[keep]
    for i in range(len(T)):
        for e in range(3):
            j = nb[i, e]
            if j < 0 or idx[j] < 0:
                continue
            j = idx[j]
            if j < i:
                continue
            va, vb = T[i][(e + 1) % 3], T[i][(e + 2) % 3]
            ia, ib = list(T[i]).index(va), list(T[i]).index(vb)
            ja, jb = list(T[j]).index(va), list(T[j]).index(vb)
            for (p, q, pa, pb, qa, qb, toward) in ((i, j, ia, ib, ja, jb, j), (j, i, ja, jb, ia, ib, i)):
                # p's solid minus q's, at both ends of the edge
                for lo, hi, k in cols[p]:
                    for end in (0, 1):
                        pass
                    l0 = -1e9 if lo is None else lo[pa]
                    l1 = -1e9 if lo is None else lo[pb]
                    segs = _minus((l0, hi[pa]), (l1, hi[pb]),
                                  [(-1e9 if qlo is None else qlo[qa], qhi[qa], -1e9 if qlo is None else qlo[qb], qhi[qb])
                                   for qlo, qhi, _ in cols[q]])
                    for (a0, a1, b0, b1) in segs:
                        h = max(a1 - a0, b1 - b0)
                        if h < 0.004:
                            continue
                        A, B = pts[va], pts[vb]
                        # facing q: the quad's normal points from p to q
                        cq = pts[T[toward]].mean(0)
                        dv = B - A
                        nrm = np.array([dv[1], -dv[0]])
                        if np.dot(nrm, cq - A) < 0:
                            A, B, a0, a1, b0, b1 = B, A, b0, b1, a0, a1
                        paved = k != GRASS and tk[toward] != GRASS
                        low = h <= KERB_MAX and tc[p] < 0 and tc[toward] < 0
                        tgt = (kerbs if paved else top_part[GRASS]) if low else walls
                        L = float(np.hypot(*(B - A)))
                        Q = [(*A, a0), (*B, b0), (*B, b1), (*A, a1)]
                        tgt.add(Q, [[0, 1, 2], [0, 2, 3]], [[0, a0], [L, b0], [L, b1], [0, a1]])

    # --- details: copings and railings on the walls, parapets on the decks' ends, markings, lamps
    rails = part("railings", "bne_truss_steel")
    marks = part("markings", "bne_paint_white")
    parapets = part("parapets", "bne_concrete_dark")
    kind_no_ = {kk[0]: n for n, kk in enumerate(KINDS)}
    for c in cws:
        tt = np.arange(c.t0, c.t1 + 1e-6, 1.0)
        fl = np.interp(tt, c.ts, c.floor)
        open_ = np.ones(len(tt), bool)
        for d0, d1 in c.decks:
            open_ &= ~((tt >= d0 - 0.5) & (tt <= d1 + 0.5))
        for sign in (1.0, -1.0):
            # the parapet: a wall PARAPET_T thick on the trench's edge, PARAPET_UP over the ground behind it, its
            # coping a little wider; a railing on it only where a cycle path or a footway runs along it (elsewhere,
            # by the tram's grass and the park, the wall alone: Street View, 2026-10-21)
            edge = c.xy(tt, sign * c.W / 2)
            back = c.xy(tt, sign * (c.W / 2 + PARAPET_T))
            mid_b = c.xy(tt, sign * (c.W / 2 + PARAPET_T + 0.3))
            kb = plan.kind(*mid_b.T)
            zb = np.array([hts.at(k, x, y) for k, (x, y) in zip(kb, mid_b)]).ravel()
            ze = np.array([hts.at(k, x, y) for k, (x, y) in zip(kb, edge)]).ravel()
            zg = smooth(np.maximum(zb, ze), tt, 8.0)
            ztop = zg + PARAPET_UP
            drop = zg - fl
            wall = open_ & (drop > 0.6) & in_area(*mid_b.T)
            cap_in = c.xy(tt, sign * (c.W / 2 - 0.05))
            cap_out = c.xy(tt, sign * (c.W / 2 + PARAPET_T + 0.05))
            for r0, r1 in runs_of(wall):       # (one body per run: its faces share their corners, smooth along)
                sl = slice(r0, r1 + 1)
                wall_run(parapets, edge[sl], back[sl], np.minimum(ze, zb)[sl] - 0.3, ztop[sl] - 0.08)
                wall_run(parapets, cap_in[sl], cap_out[sl], ztop[sl] - 0.08, ztop[sl])
            beside = c.xy(tt, sign * (c.W / 2 + PARAPET_T + 1.2))
            path = np.isin(plan.kind(*beside.T), [kind_no_["cycle"], kind_no_["foot"]])
            hi = wall & path
            line = c.xy(tt, sign * (c.W / 2 + PARAPET_T / 2))
            j = 0
            while j < len(tt):
                if not hi[j]:
                    j += 1
                    continue
                e = j
                while e < len(tt) and hi[e]:
                    e += 1
                if e - j >= 3:
                    railing(rails, line[j:e], ztop[j:e])
                j = e
        # (the decks' parapets: the BGT's walls across them, below)
        # lane markings: edge lines and the dashed lane line, on the floor
        mk = np.arange(c.t0, c.t1 + 1e-6, 1.0)
        fz = np.interp(mk, c.ts, c.floor) + 0.02
        s = np.r_[0, np.cumsum(np.hypot(*np.diff(c.xy(mk, 0), axis=0).T))]
        for off, dash in ((c.W / 2 - SHOULDER - 0.1, False), (-(c.W / 2 - SHOULDER - 0.1), False), (0.0, True)):
            A_, B_ = c.xy(mk, off + 0.075), c.xy(mk, off - 0.075)
            ok = in_area(*c.xy(mk, off).T)
            for j in range(len(mk) - 1):
                if not (ok[j] and ok[j + 1]) or (dash and (s[j] % 12.0) >= 3.0):
                    continue
                q = [(*A_[j], fz[j]), (*B_[j], fz[j]), (*B_[j + 1], fz[j + 1]), (*A_[j + 1], fz[j + 1])]
                marks.add(q, [[0, 2, 1], [0, 3, 2]], [[0, 0], [1, 0], [1, 1], [0, 1]])
    # the BGT's walls beyond the trench's own (the wing walls by the decks, the park's retaining walls): each wall's
    # outline's middle line, a dark wall PARAPET_T thick, its top PARAPET_UP over the higher ground beside it (Street
    # View A200, B130: one long dark wall between the tram's grass and the sunk park); a railing on it where a path runs
    n_cut = 0
    for f in ref["features"]:
        if f["layer"] != "scheiding" or f.get("functie") not in ("muur", "kademuur") or f["level"] != 0:
            continue
        ring = np.asarray(f["rings"][0], np.float64)
        if len(ring) > 1 and np.allclose(ring[0], ring[-1]):
            ring = ring[:-1]
        if len(ring) < 4:
            continue
        # its middle line: the outline split at its two farthest points, the two sides averaged by length
        D = np.hypot(*(ring[:, None] - ring[None]).transpose(2, 0, 1))
        i0, i1 = np.unravel_index(np.argmax(D), D.shape)
        i0, i1 = min(i0, i1), max(i0, i1)
        s1 = ring[i0:i1 + 1]
        s2 = np.r_[ring[i1:], ring[:i0 + 1]][::-1]
        r1, _ = (resample(s1, 0.5), None)
        r2 = resample(s2, 0.5)
        m_ = max(len(r1), len(r2))
        t_ = np.linspace(0, 1, m_)

        def at_t(q):
            sq = np.r_[0, np.cumsum(np.hypot(*np.diff(q, axis=0).T))]
            sq = sq / max(sq[-1], 1e-9)
            return np.c_[np.interp(t_, sq, q[:, 0]), np.interp(t_, sq, q[:, 1])]
        ln = (at_t(r1) + at_t(r2)) / 2
        if np.hypot(*(ln[-1] - ln[0])) < 2.0:
            continue
        # not where the trench's own walls are
        dv = np.gradient(ln, axis=0)
        nv = np.c_[-dv[:, 1], dv[:, 0]] / np.maximum(np.hypot(*dv.T), 1e-9)[:, None]
        # (along the trench's side: its own walls; across it, over a deck: the deck's parapet, its foot the deck's
        # underside so the passage stays open)
        near_tr = np.zeros(len(ln), bool)
        over = np.zeros(len(ln), bool)
        for c in cws:
            tq, wq = c.tw(*ln.T)
            inr = (tq >= c.t0) & (tq <= c.t1) & (np.abs(wq) <= c.W / 2 + 2.5)
            along_ = np.abs((dv / np.maximum(np.hypot(*dv.T), 1e-9)[:, None]) @ c.u) > 0.7
            near_tr |= inr & along_
            over |= inr & ~along_
        pa, pb = ln + nv * 1.0, ln - nv * 1.0
        za = np.array([float(np.ravel(hts.at(int(k), x, y))[0]) for k, (x, y) in zip(plan.kind(*pa.T), pa)])
        zb_ = np.array([float(np.ravel(hts.at(int(k), x, y))[0]) for k, (x, y) in zip(plan.kind(*pb.T), pb)])
        zhi, zlo = np.maximum(za, zb_), np.minimum(za, zb_)
        zhi = smooth(zhi, np.arange(len(ln), dtype=float), 4.0)
        top = zhi + BGT_WALL_UP
        ok = ~near_tr & in_area(*ln.T)
        ok &= over | (plan.kind(*ln.T) != ROAD)            # (never on a carriageway at grade: a wall's end, round 5)
        a_, b_ = ln + nv * PARAPET_T / 2, ln - nv * PARAPET_T / 2
        foot = np.where(over, zhi - DECK_T, zlo - 0.4)
        for r0, r1 in runs_of(ok):
            sl = slice(r0, r1 + 1)
            wall_run(parapets, a_[sl], b_[sl], foot[sl], top[sl])
            n_cut += 0.5 * (r1 - r0)
        hi_side = np.where((za >= zb_)[:, None], nv, -nv)
        path = ok & np.isin(plan.kind(*(ln + hi_side * 1.5).T), [kind_no_["cycle"], kind_no_["foot"]])
        j = 0
        while j < len(ln):
            if not path[j]:
                j += 1
                continue
            e = j
            while e < len(ln) and path[e]:
                e += 1
            if e - j >= 6:
                railing(rails, ln[j:e], top[j:e])
            j = e
    print(f"the BGT's walls beyond the trench: {n_cut:.0f} m")
    # the paint as the photo shows it (tools/kunstwerk_markings.py), on the streets outside the trench
    def surface_top(pts):
        """The top of the surface at each point (k, 2): the street of its own kind, the highest of the kinds round it
        within 0.3 m (an overlay at a kerb sits on the raised side), a deck's street where it is decked."""
        pts = np.atleast_2d(np.asarray(pts, np.float64))
        best = np.full(len(pts), -1e9)
        for dx, dy in ((0, 0), (0.3, 0), (-0.3, 0), (0, 0.3), (0, -0.3)):
            q = pts + [dx, dy]
            kk_ = plan.kind(*q.T)
            z_ = np.array([float(np.ravel(hts.at(int(k_), x, y))[0]) for k_, (x, y) in zip(kk_, pts)])
            best = np.maximum(best, z_)
        return best

    mk_path = os.path.join(a.ref, "markings.json")
    reds = part("street_crossings", "bne_cycle_red")
    n_paint = 0
    for m in (json.load(open(mk_path))["marks"] if os.path.exists(mk_path) else ()):
        ring = np.asarray(m["rings"][0], np.float64)
        if len(ring) > 1 and np.allclose(ring[0], ring[-1]):
            ring = ring[:-1]
        if len(ring) < 3:
            continue
        c = ring.mean(0)
        deck_ = np.zeros(len(ring), bool)
        for cw in cws:
            deck_ |= cw.deck_at(*ring.T)
        if not in_area(*c[:, None])[0] or ((in_trench(*ring.T) >= 0) & ~deck_).any():   # (on a deck: its street's)
            continue
        area2 = np.sum(ring[:, 0] * np.roll(ring[:, 1], -1) - np.roll(ring[:, 0], -1) * ring[:, 1])
        if area2 < 0:
            ring = ring[::-1]
        tris = earcut(ring)
        if not len(tris):
            continue
        k = int(plan.kind(*c[:, None])[0])
        lift = PAINT_LIFT if m["kind"] == "white" else RED_LIFT
        z = surface_top(ring) + lift          # (per corner, the surface there: never under a raised one, round 5)
        (marks if m["kind"] == "white" else reds).add(np.c_[ring, z], tris, ring)
        n_paint += 1
    print(f"paint: {n_paint} marks")
    # the tram's rails: two per track, 1.435 m apart, flush in the street
    rail_p = part("rails", "bne_rail_steel")
    grooves = part("rail_grooves", "bne_steel_black")
    for f in ref["features"]:
        if f["layer"] != "spoor" or f["level"] != 0:
            continue
        ln = resample(np.asarray(f["rings"][0], np.float64), 1.0)
        dv = np.gradient(ln, axis=0)
        nv = np.c_[-dv[:, 1], dv[:, 0]] / np.maximum(np.hypot(*dv.T), 1e-9)[:, None]
        for off in (-0.7175, 0.7175):
            # the groove beside the head: a dark strip (paint's black: the rail's own steel, unlit), then the head
            G1, G2 = ln + nv * (off - RAIL_W / 2 - 0.03), ln + nv * (off + RAIL_W / 2 + 0.03)
            for j in range(len(ln) - 1):
                if in_area(*ln[j][:, None])[0] and in_trench(*ln[j][:, None])[0] < 0:
                    zg, zg1 = surface_top(ln[j:j + 2]) + RAIL_UP / 2
                    q = [(*G1[j], zg), (*G2[j], zg), (*G2[j + 1], zg1), (*G1[j + 1], zg1)]
                    up = (G2[j, 0] - G1[j, 0]) * (G2[j + 1, 1] - G1[j, 1]) - (G2[j, 1] - G1[j, 1]) * (G2[j + 1, 0] - G1[j, 0]) > 0
                    grooves.add(q, [[0, 1, 2], [0, 2, 3]] if up else [[0, 2, 1], [0, 3, 2]], [[0, 0], [1, 0], [1, 1], [0, 1]])
            A_, B_ = ln + nv * (off - RAIL_W / 2), ln + nv * (off + RAIL_W / 2)
            ok = in_area(*ln.T) & (in_trench(*ln.T) < 0)
            kk = plan.kind(*ln.T)
            z = surface_top(ln) + RAIL_UP
            for j in range(len(ln) - 1):
                if ok[j] and ok[j + 1]:
                    q = [(*A_[j], z[j]), (*B_[j], z[j]), (*B_[j + 1], z[j + 1]), (*A_[j + 1], z[j + 1])]
                    up = (B_[j, 0] - A_[j, 0]) * (B_[j + 1, 1] - A_[j, 1]) - (B_[j, 1] - A_[j, 1]) * (B_[j + 1, 0] - A_[j, 0]) > 0
                    rail_p.add(q, [[0, 1, 2], [0, 2, 3]] if up else [[0, 2, 1], [0, 3, 2]],
                               [[0, j], [0.07, j], [0.07, j + 1], [0, j + 1]])
    # fences where the BGT has them
    for f in ref["features"]:
        if f["layer"] != "scheiding" or f.get("functie") != "hek" or f["level"] != 0:
            continue
        ln = resample(np.asarray(f["rings"][0], np.float64), 1.0)
        ok = in_area(*ln.T) & (in_trench(*ln.T) < 0)
        if ok.sum() < 3:
            continue
        ln = ln[ok]
        kk = plan.kind(*ln.T)
        z = np.array([hts.at(k_, *p_[:, None])[0] for k_, p_ in zip(kk, ln)])
        railing(rails, ln, z)
    # street furniture from the kit (tools/kunstwerk_props.py): traffic lights where OSM has its signals, at the nearest
    # kerb, facing away from their junction (toward the traffic coming to it); L2 signs at both kerbs of a zebra; D2 on
    # the islands; the BGT's gantry over its road
    import kunstwerk_props as kit
    props = part("props", kit.MATERIAL)
    sys.path.insert(0, os.path.join(HERE, "..", "server"))
    import ahn as ahnmod
    sig_path = os.path.join(a.ref, "osm_signals.json")
    osm = json.load(open(sig_path))["elements"] if os.path.exists(sig_path) else []

    def local(e):
        rx, ry = ahnmod.wgs_to_rd(e["lat"], e["lon"])
        return np.array([rx - ref["origin_rd"][0], ry - ref["origin_rd"][1]])

    def kerb(p, dirs=24, skip=None):
        """The nearest spot off the carriageway from p (a little way onto the pavement), and the way to it."""
        best = None
        for ang in np.linspace(0, 2 * np.pi, dirs, endpoint=False):
            dv = np.array([np.cos(ang), np.sin(ang)])
            if skip is not None and np.dot(dv, skip) > 0.5:
                continue
            ray = p + np.outer(np.arange(0.25, 12.0, 0.25), dv)
            off = np.flatnonzero((plan.kind(*ray.T) != ROAD) & (in_trench(*ray.T) < 0))
            if len(off) and (best is None or off[0] < best[0]):
                best = (off[0], ray[min(off[0] + 2, len(ray) - 1)], dv)
        return (best[1], best[2]) if best else (None, None)

    def ground_z(q):
        k = int(plan.kind(*q[:, None])[0])
        return float(hts.at(k, *q[:, None])[0])

    furniture = []

    def put(module, q, heading):
        P, T, UV = kit.place(module, q, (np.cos(heading), np.sin(heading)), ground_z(q))
        props.add(P, T, UV)
        furniture.append({"at": [float(q[0]), float(q[1])], "heading": float(heading), "verts": len(module[0])})
    kind_no = {kk[0]: n for n, kk in enumerate(KINDS)}

    def on_road(pts):
        pts = np.atleast_2d(pts)
        return (plan.kind(*pts.T) == ROAD) & (in_trench(*pts.T) < 0)

    def road_axis(p):
        """The way the carriageway runs through p: the direction with the longest road either way of it."""
        best, axis = -1, None
        steps = np.arange(0.5, 30.0, 0.5)
        for a_ in np.radians(np.arange(0, 180, 6)):
            dv = np.array([np.cos(a_), np.sin(a_)])
            run = 0
            for sgn in (1, -1):
                ok = on_road(p + sgn * np.outer(steps, dv))
                run += np.argmin(ok) if not ok.all() else len(ok)
            if run > best:
                best, axis = run, dv
        return axis

    def to_kerb(p, dv, reach=16.0):
        """From p along dv: the first spot off the carriageway (None: none within reach)."""
        steps = np.arange(0.0, reach, 0.2)
        ok = on_road(p + np.outer(steps, dv))
        if ok.all():
            return None
        return p + dv * steps[np.argmin(ok)]
    sigs = [(local(e), e["tags"].get("crossing") == "traffic_signals") for e in osm
            if e["tags"].get("highway") == "traffic_signals" or e["tags"].get("crossing") == "traffic_signals"]
    sigs = [(p, x) for p, x in sigs if in_area(*p[:, None])[0] and in_trench(*p[:, None])[0] < 0]
    # lights and signs where tools/kunstwerk_placement.py put them (ref/placement.json: each item's module, spot and the
    # way its face looks, toward the traffic it speaks to)
    pl_path = os.path.join(a.ref, "placement.json")
    items = json.load(open(pl_path))["items"] if os.path.exists(pl_path) else []
    fx_path = os.path.join(a.ref, "placement_fix.json")      # (the corrections read off Street View: by place)
    for fx in (json.load(open(fx_path))["fixes"] if os.path.exists(fx_path) else ()):
        if fx["action"] == "add":
            items.append({k: v for k, v in fx.items() if k not in ("action", "why")})
            continue
        near = np.asarray(fx["near"], np.float64)
        cand = [i for i, it in enumerate(items) if it["module"] == fx["module"]
                and np.hypot(*(np.asarray(it["at"]) - near)) < 2.0]
        if not cand:
            print(f"placement fix: nothing near {fx['near']} ({fx['why']})")
            continue
        i = min(cand, key=lambda i: np.hypot(*(np.asarray(items[i]["at"]) - near)))
        if fx["action"] == "remove":
            items.pop(i)
        else:
            items[i].update({k: v for k, v in fx.items() if k not in ("action", "why", "near")})
    if not items:
        print(f"street furniture: no {pl_path} (tools/kunstwerk_placement.py): no lights or signs")
    modules = {("traffic_light", False): kit.traffic_light(button=False),
               ("traffic_light", True): kit.traffic_light(bike=True, button=True)}
    count = {}
    for it in items:
        q = np.asarray(it["at"], np.float64)
        if not in_area(*q[:, None])[0]:
            continue
        if it["module"] == "traffic_light" and it.get("mast"):
            lanes = int(it["mast"])
            arm = -1 if it.get("arm") == "right" else 1
            reach = float(it.get("reach", min(lanes * LANE * 0.85, 9.0)))
            mod = modules.setdefault(("mast", lanes, arm, reach), kit.mast_arm(reach, heads=max(1, min(3, lanes)),
                                                                               arm=arm))
        elif it["module"] == "traffic_light" and it.get("plates"):
            key = ("traffic_light", bool(it.get("bike")), tuple(it["plates"]))
            mod = modules.setdefault(key, kit.traffic_light(bike=bool(it.get("bike")), button=False,
                                                             plates=it["plates"]))
        elif it["module"] == "traffic_light":
            mod = modules[("traffic_light", bool(it.get("bike")))]
        else:
            mod = modules.setdefault(("sign", it["code"]), kit.sign(it["code"]))
        f_ = np.asarray(it["facing"], np.float64)
        put(mod, q, np.arctan2(f_[1], f_[0]))
        key = it.get("code") or it["role"]
        count[key] = count.get(key, 0) + 1
    # the direction signs read off Street View (ref/direction_signs.json: each its module, its arguments, its place
    # and the way its face looks)
    ds_path = os.path.join(a.ref, "direction_signs.json")
    for it in (json.load(open(ds_path))["signs"] if os.path.exists(ds_path) else ()):
        q = np.asarray(it["at"], np.float64)
        f_ = np.asarray(it["facing"], np.float64)
        put(getattr(kit, it["module"])(*it["args"]), q, np.arctan2(f_[1], f_[0]))
        count[it["module"]] = count.get(it["module"], 0) + 1
    # the BGT's gantries: its posts in pairs
    posts = [np.asarray(f["rings"][0][0], np.float64).reshape(-1)[:2] for f in ref["features"]
             if f["layer"] == "paal" and f.get("functie") == "portaal"]
    used, n_g = set(), 0
    for i, p in enumerate(posts):
        if i in used:
            continue
        j = min((j for j in range(len(posts)) if j != i and j not in used), default=None,
                key=lambda j: np.hypot(*(posts[j] - p)))
        if j is None or np.hypot(*(posts[j] - p)) > 30 or not in_area(*np.c_[p, posts[j]]).all():
            continue
        used |= {i, j}
        span = float(np.hypot(*(posts[j] - p)))
        dv = (posts[j] - p) / span
        mid_ = (p + posts[j]) / 2
        # it stands just before its junction: its signs face away from the middle of the signals around it (the
        # nearest one alone can be the other way's, on its own posts)
        near_ = [p_ for p_, _ in sigs if np.hypot(*(p_ - mid_)) < 30.0]
        cen_ = np.mean(near_, axis=0) if near_ else mid_ - np.array([-dv[1], dv[0]])
        nrm = np.array([-dv[1], dv[0]])
        if np.dot(nrm, mid_ - cen_) < 0:
            p, posts[j], dv, nrm = posts[j], p, -dv, -nrm
        P, T, UV = kit.place(kit.gantry(span), p, nrm, ground_z(p))
        props.add(P, T, UV)
        n_g += 1
    print(f"street furniture: {count}, {n_g} gantries")
    json.dump(furniture, open(os.path.join(a.ref, "furniture.json"), "w"))
    # the BGT's light masts: by a tram track a tram line's mast (its cantilever over the track), else a street light
    # with its arm out over the nearest carriageway (grey both, Street View 2026-10-21)
    lamp_mod, pole_mod = kit.lamp_post(), kit.tram_pole()
    tracks = [resample(np.asarray(f["rings"][0], np.float64), 1.0) for f in ref["features"]
              if f["layer"] == "spoor" and f["level"] == 0]
    TP = np.concatenate(tracks) if tracks else np.zeros((0, 2))
    n_lamp = n_tpole = 0
    for f in ref["features"]:
        if f["layer"] != "paal" or f.get("functie") != "lichtmast":
            continue
        p = np.asarray(f["rings"][0][0], np.float64).reshape(-1)[:2]
        if not in_area(*p[:, None]).all() or in_trench(*p[:, None])[0] >= 0:
            continue
        dt = np.hypot(*(TP - p).T) if len(TP) else np.array([np.inf])
        if dt.min() < TRAM_POLE_M:
            near = TP[np.argmin(dt)]
            put(pole_mod, p, np.arctan2(*(near - p)[::-1]))
            n_tpole += 1
            continue
        best = None
        for ang in np.linspace(0, 2 * np.pi, 16, endpoint=False):
            dv = np.array([np.cos(ang), np.sin(ang)])
            ray = p + np.outer(np.arange(0.5, 12, 0.5), dv)
            hit = np.flatnonzero(plan.kind(*ray.T) == ROAD)
            if len(hit) and (best is None or hit[0] < best[0]):
                best = (hit[0], ang)
        put(lamp_mod, p, best[1] if best else 0.0)
        n_lamp += 1
    print(f"light masts: {n_lamp} street lights, {n_tpole} tram masts")

    # --- out
    out = {"origin_rd": ref["origin_rd"], "origin_wgs": ref["origin_wgs"],
           "parts": [welded(p.out()) for p in parts.values() if p.out()]}
    base = os.path.dirname(os.path.abspath(a.ref))
    json.dump(out, open(os.path.join(base, "parts.json"), "w"))
    decks_out = []
    for c in cws:
        for d0, d1 in c.decks:
            ring = np.r_[c.xy([d0, d1], c.W / 2 + 0.3), c.xy([d1, d0], -c.W / 2 - 0.3)]
            gx_, gy_ = np.meshgrid(np.linspace(d0, d1, 6), np.linspace(-c.W / 2, c.W / 2, 5))
            pp = c.xy(gx_.ravel(), 0.0) + np.outer(gy_.ravel(), c.n)
            kk = plan.kind(*pp.T)
            zz = np.array([hts.at(k, x, y) for k, (x, y) in zip(kk, pp)]).ravel()
            cf = np.linalg.lstsq(np.c_[np.ones(len(pp)), pp], zz, rcond=None)[0]
            decks_out.append({"layer": "tunneldeel", "ring": ring.round(3).tolist(), "plane": [float(v) for v in cf]})
    json.dump({"decks": decks_out}, open(os.path.join(base, "decks.json"), "w"))
    for p in out["parts"]:
        print(f"  {p['name']}: {len(p['positions'])} vertices, {len(p['tris'])} triangles")


def runs_of(mask):
    """The runs of True in mask as (first, last) index pairs, each at least two long."""
    out, i, m = [], 0, len(mask)
    while i < m:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < m and mask[j + 1]:
            j += 1
        if j > i:
            out.append((i, j))
        i = j + 1
    return out


def wall_run(part, a, b, z0, z1, uv_m=1.0):
    """A wall's body along a run: between the lines a and b (k, 2), from z0 to z1 (k,): its two long faces, its top,
    its ends; each long face one strip (shared corners: smooth shading along a curve, not a facet a metre), UVs in
    metres along it and up."""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    z0, z1 = np.asarray(z0, np.float64), np.asarray(z1, np.float64)
    k = len(a)
    s = np.r_[0, np.cumsum(np.hypot(*np.diff((a + b) / 2, axis=0).T))]
    side = np.sign(np.cross(a[-1] - a[0] if k > 1 else np.array([1.0, 0]), b[0] - a[0])) or 1.0

    def strip(L, za, zb_, flip):
        P = np.r_[np.c_[L, za], np.c_[L, zb_]]
        i = np.arange(k - 1)
        T = np.r_[np.c_[i, i + 1, i + k + 1], np.c_[i, i + k + 1, i + k]]
        if flip:
            T = T[:, ::-1]
        part.add(P, T, np.r_[np.c_[s, za], np.c_[s, zb_]] / uv_m)
    strip(a, z0, z1, side > 0)
    strip(b, z0, z1, side < 0)
    # top and bottom: between the two lines
    P = np.r_[np.c_[a, z1], np.c_[b, z1]]
    i = np.arange(k - 1)
    T = np.r_[np.c_[i, i + 1, i + k + 1], np.c_[i, i + k + 1, i + k]]
    part.add(P, T if side < 0 else T[:, ::-1], np.r_[np.c_[s, np.zeros(k)], np.c_[s, np.full(k, 0.4)]] / uv_m)
    for e in (0, k - 1):                         # its ends
        q = [(*a[e], z0[e]), (*b[e], z0[e]), (*b[e], z1[e]), (*a[e], z1[e])]
        w = float(np.hypot(*(b[e] - a[e])))
        tri = [[0, 1, 2], [0, 2, 3]] if (e == 0) == (side > 0) else [[0, 2, 1], [0, 3, 2]]
        part.add(q, tri, [[0, z0[e]], [w, z0[e]], [w, z1[e]], [0, z1[e]]])


def welded(p):
    """The part with its coinciding vertices (same place, same UV) merged and its degenerate triangles dropped."""
    P, T, UV = np.asarray(p["positions"]), np.asarray(p["tris"]), np.asarray(p["uvs"])
    key = np.c_[np.round(P, 3), np.round(UV, 3)]
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    T = inv[T]
    T = T[(T[:, 0] != T[:, 1]) & (T[:, 1] != T[:, 2]) & (T[:, 0] != T[:, 2])]
    return {**p, "positions": P[first].tolist(), "tris": T.tolist(), "uvs": UV[first].tolist()}


def _minus(pa, pb, qs):
    """An interval at an edge's two ends (lo, hi at a; lo, hi at b) minus q's intervals (lo_a, hi_a, lo_b, hi_b): the
    pieces left, as (lo_a, hi_a, lo_b, hi_b), the ends paired (both ends cut the same way, by the order at their
    middle)."""
    pieces = [(pa[0], pa[1], pb[0], pb[1])]
    for qa0, qa1, qb0, qb1 in qs:
        nxt = []
        for a0, a1, b0, b1 in pieces:
            # below q
            lo_a, lo_b = a0, b0
            hi_a, hi_b = min(a1, qa0), min(b1, qb0)
            if (hi_a - lo_a) + (hi_b - lo_b) > 0.008:
                nxt.append((lo_a, max(hi_a, lo_a), lo_b, max(hi_b, lo_b)))
            # above q
            lo_a, lo_b = max(a0, qa1), max(b0, qb1)
            if (a1 - lo_a) + (b1 - lo_b) > 0.008:
                nxt.append((min(lo_a, a1), a1, min(lo_b, b1), b1))
        pieces = nxt
    return [p for p in pieces if p[0] > -1e8 and p[2] > -1e8]


if __name__ == "__main__":
    main()
