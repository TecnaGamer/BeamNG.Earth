"""Kantstolpar, the white posts with a black band along Sweden's main rural roads, as VGU 2024 12.2.2.1 (Trafikverket,
TRVINFRA-00396, remiss version 0.9, 2024-07-01): on both sides of a road of 80 km/h or more (K237852, K237868), 1.0 m
outside its edge (K237869), every 100 m, every 25 m on a radius under 700 m (K237875-6), behind a barrier where there is
one (K237878: none here, they would stand in it). Its 2000 vehicles a day is the classes here: no traffic counts.

Tile independence: they stand on a grid of arc length along a whole ground part, and a tile keeps those in it.

# fork: Australia's guide posts (AS 1742.2 / Austroads Guide to Road Design Part 6: white, 1.05 m, a reflector on the
# face the driver sees: red on the post on his left, white on his right; one metre off the sealed edge of a rural
# road; ~150 m apart on straights, closer on bends by their radius; none in built-up streets). The same rows, two groups.
"""
import math

import numpy as np

import guardrails
import medians
import motesfri

COUNTRIES = {"SE"}
CLASSES = {"motorway", "trunk", "primary"}
MIN_KMH = 80.0
OUT_M = 1.0
TIGHT_M, SPACING_M = 25.0, 100.0    # the grid, and the spacing on straights: a whole number of TIGHT_M
TIGHT_RADIUS_M = 700.0
HEIGHT_M = 1.05
CLEAR_M = 1.5                       # m from a mapped barrier
PAVEMENT_M = 0.5                    # m past a carriageway of its own class beside it
GROUP = guardrails.GROUPS.index("kantstolpe")

# fork: Australia (left-hand traffic). Rural sealed roads of every numbered class, outside towns (urban, or a limit of
# under 70 km/h) and without OpenStreetMap sidewalks; the model's reflector faces -X (the traffic coming along +X), red
# on one face and white on the other: a post on a driver's left shows him red, on his right white, so a two-way road's
# posts need no second set for the other direction.
AU_COUNTRIES = {"AU"}
AU_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified"}
AU_MIN_KMH = 70.0
AU_OUT_M = 1.0                      # m outside the sealed edge (spec.width includes the sealed shoulders)
AU_GRID_M = 5.0                     # the grid (a whole number in every spacing below)
AU_TURN_M = 20.0                    # the bend is read over this much road either side of a station
# (radius m at or over, spacing m): AS 1742.2 / Austroads: ~150 m on straights down to a post every few metres on a hairpin
AU_SPACING = ((1000.0, 150.0), (600.0, 100.0), (400.0, 60.0), (250.0, 40.0), (150.0, 30.0), (80.0, 20.0), (40.0, 10.0),
              (0.0, 5.0))
AU_RED = guardrails.GROUPS.index("au_guidepost_red")
AU_WHITE = guardrails.GROUPS.index("au_guidepost_white")


def wanted(spec):
    return (spec.kind == "paved" and spec.cls in CLASSES and not spec.urban and spec.kmh is not None
            and spec.kmh >= MIN_KMH)


def stations(xy):
    """(points (k, 2), unit tangents (k, 2)) along polyline xy (k, >=2) where posts stand, before they move aside."""
    seg = np.hypot(*np.diff(xy, axis=0).T)
    keep = np.r_[True, seg > 1e-6]
    xy = xy[keep]
    cum = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    s = np.arange(0.0, cum[-1], TIGHT_M)
    if len(s) < 2:
        return np.zeros((0, 2)), np.zeros((0, 2))
    p = np.c_[np.interp(s, cum, xy[:, 0]), np.interp(s, cum, xy[:, 1])]
    d = np.diff(np.vstack([p, xy[-1:]]), axis=0)
    t = d / np.maximum(np.hypot(*d.T), 1e-9)[:, None]
    turn = np.abs(np.arctan2(t[:-1, 0] * t[1:, 1] - t[:-1, 1] * t[1:, 0], (t[:-1] * t[1:]).sum(1)))
    tight = np.r_[False, turn > TIGHT_M / TIGHT_RADIUS_M]
    pick = tight | (np.arange(len(s)) % int(SPACING_M / TIGHT_M) == 0)
    return p[pick], t[pick]


def au_wanted(spec):
    """fork: whether Australia's guide posts stand along a road of this spec (a rural sealed road)."""
    return (spec.kind == "paved" and spec.cls in AU_CLASSES and not spec.urban and not spec.sides
            and (spec.kmh is None or spec.kmh >= AU_MIN_KMH))


def au_stations(xy):
    """fork: (points (k, 2), unit tangents (k, 2)) along polyline xy (k, >=2) where Australia's posts stand: every
    AU_GRID_M of arc length, kept where the grid index is a multiple of the spacing there (AU_SPACING by the radius of
    the road's bend over AU_TURN_M either side)."""
    seg = np.hypot(*np.diff(xy, axis=0).T)
    xy = xy[np.r_[True, seg > 1e-6]]
    cum = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    s = np.arange(0.0, cum[-1], AU_GRID_M)
    if len(s) < 2:
        return np.zeros((0, 2)), np.zeros((0, 2))

    def at(d):
        d = np.clip(d, 0.0, cum[-1])
        return np.c_[np.interp(d, cum, xy[:, 0]), np.interp(d, cum, xy[:, 1])]
    p = at(s)
    d = np.diff(np.vstack([p, xy[-1:]]), axis=0)
    t = d / np.maximum(np.hypot(*d.T), 1e-9)[:, None]
    a, b = p - at(s - AU_TURN_M), at(s + AU_TURN_M) - p
    la, lb = np.hypot(*a.T), np.hypot(*b.T)
    turn = np.abs(np.arctan2(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0], (a * b).sum(1)))
    turn[(la < AU_TURN_M / 2) | (lb < AU_TURN_M / 2)] = 0.0      # (the line's ends: straight)
    radius = AU_TURN_M / np.maximum(turn, 1e-6)
    step = np.full(len(s), AU_SPACING[-1][1])
    for r, m in reversed(AU_SPACING):       # (from the tightest bend out: the first radius it reaches)
        step = np.where(radius >= r, m, step)
    pick = np.arange(len(s)) % np.maximum(np.rint(step / AU_GRID_M).astype(int), 1) == 0
    return p[pick], t[pick]


def tile_rows(frame, near_roads, box, elev, country, mapped=()):
    """Forest rows (guardrails.forest_rows' layout and GROUPS) of road tile box: near_roads build_tile's roads_out,
    elev(lat, lon) the ground, mapped the tile's barrier rows so far."""
    au = (country or "").upper() in AU_COUNTRIES        # fork: Australia's posts (au_wanted, au_stations, AU_*)
    if (country or "").upper() not in COUNTRIES and not au:
        return []
    x0, y0, x1, y1 = box
    rails = medians._mapped_segments(mapped)
    out = []
    for fid, spec, pts, *_ in near_roads:
        if not (au_wanted(spec) if au else wanted(spec)) or len(pts) < 2:
            continue
        others = [(p2[:, :2], s2.width / 2 + medians.OPENING_M) for f2, s2, p2, *_ in near_roads
                  if f2 != fid and s2.cls != spec.cls and s2.cls in motesfri.OPEN_CLASSES and len(p2) >= 2]
        others += [(p2[:, :2], s2.width / 2 + PAVEMENT_M) for f2, s2, p2, *_ in near_roads      # the other carriageway
                   if f2 != fid and s2.cls == spec.cls and len(p2) >= 2]
        p, t = (au_stations if au else stations)(np.asarray(pts, np.float64)[:, :2])
        for side in (1.0, -1.0):        # (+1 left of the geometry)
            q = p + np.c_[-t[:, 1], t[:, 0]] * side * (spec.width / 2 + (AU_OUT_M if au else OUT_M))
            ok = (q[:, 0] >= x0) & (q[:, 0] < x1) & (q[:, 1] >= y0) & (q[:, 1] < y1)
            for line, reach in others:
                ok &= motesfri._distance(q, line) > reach
            for a, b in zip(rails[:, :2], rails[:, 2:]) if len(rails) else ():
                ok &= motesfri._distance(q, np.stack([a, b])) > CLEAR_M
            # the traffic it faces: on its right (the other way on a road's left) or a one-way road's
            sign = (-1.0 if int(spec.dir) < 0 else 1.0) if spec.oneway else -side
            group = GROUP
            if au:      # fork: left-hand traffic: the traffic it faces is the way of travel (a two-way road's along its
                #         geometry, the other way on its far face); red on a driver's left, white on his right
                sign = (-1.0 if int(spec.dir) < 0 else 1.0) if spec.oneway else 1.0
                group = AU_RED if side * sign > 0 else AU_WHITE
            for k in np.flatnonzero(ok):
                fx, fy = t[k] * sign
                la, lo, _ = frame.enu_to_geodetic(q[k:k + 1, 0], q[k:k + 1, 1], np.zeros(1))
                z = float(np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]).ravel()[0])
                out.append([group, round(float(q[k, 0]), 3), round(float(q[k, 1]), 3), round(z, 3),
                            round(math.degrees(math.atan2(fy, fx)), 2), 0.0, HEIGHT_M,
                            round((k * 0.6180339887) % 1.0, 4),
                            float(fx), float(fy), 0.0, float(-fy), float(fx), 0.0, 0.0, 0.0, 1.0])
    return out
