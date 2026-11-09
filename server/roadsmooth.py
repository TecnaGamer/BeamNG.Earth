"""Rounded bends for the road centrelines (the user, 2026-11-03: a country road's bend near Vinnytsia, Ukraine, was a
few straight pieces with a visible corner at every vertex; the map's line has a vertex every 15-17 m turning 4-8
degrees there, the real road is one smooth curve).

Overture / OpenStreetMap draw a bend as a polyline, and everything downstream follows its vertices exactly (the road
mesh, the painted ground, the grading, the markings, the sidewalks, the AI's routes), only adding points on the same
straight pieces. So the roads layer is smoothed once, as its cell is loaded (mapdata.py), before anything reads it:
at each interior vertex that turns by at least MIN_DEFLECT_DEG an arc tangent to both pieces replaces the corner,
its radius the class's (RADIUS_M) unless the pieces are too short for it (the tangent points stay within half of each
neighbouring piece, all of a piece when the vertex at its other end is not rounded).

What stays where it is: both ends of a line (Overture splits a road at every junction: the ends are the nodes, and a
road crossing a cell's edge is the same whole feature in both cells), the vertices that are not rounded, closed
lines (roundabouts, rings), and every line that is not a drivable road class. Pure per line (its own coordinates and
class), so the cells next to each other agree.

The rules (surface, width, flags, level, ...) are linear references: fractions of the line's length. An arc is a
little shorter than the corner it replaces, so each fraction is carried over by the length at the tangent points.
"""
import os

import numpy as np

import fast

VERSION = 2                     # bump when the arcs change (mapdata.versions(): the derived caches are built again)
ON = os.environ.get("BNE_ROAD_SMOOTH", "off") == "on"       # off unless asked: it changes every road
# roads.simplify's RDP tolerance while on: its own 0.25 m put a vertex back every ~16-20 m of a R 130 m arc, turning
# 5-7 degrees, the corners again (the user, 2026-11-08, near Korostivtsi: "still corners"; on three cells the visible
# corners, > 3 degrees between pieces >= 5 m, were 2024 with the arcs against 2013 without; 1291 at 0.03 m, +10 % nodes)
SIMPLIFY_M = 0.03

# class -> radius (m) of a bend's arc when the line's pieces are long enough (street-level reading of Overture's
# classes: a motorway's curves are gentle, a lane's tight)
RADIUS_M = {"motorway": 300.0, "trunk": 250.0, "primary": 150.0, "secondary": 100.0, "tertiary": 50.0,
            "unclassified": 30.0, "residential": 25.0, "unknown": 25.0, "track": 15.0, "living_street": 12.0,
            "service": 12.0}
LINK_RADIUS_M = 50.0            # a ramp / connector road (subclass "link") bends tighter than its class
MIN_DEFLECT_DEG = 0.5           # a vertex turning less than this is left alone
MAX_DEFLECT_DEG = 150.0         # ... and one turning more is a hairpin's tip or a doubling back: left alone
ARC_STEP_M = 3.0                # m between the points of an arc ...
ARC_STEP_DEG = 6.0              # ... and at most this many degrees
FREE_SHARE = 0.9                # share of a piece a rounded vertex may take when the vertex at its other end is not
MAX_SHIFT_M = 0.75              # m a corner may move to fill its pieces with a radius above the class's
MIN_PIECE_M = 0.05             # a piece shorter than this is a duplicate vertex: the line is left alone
CLOSED_M = 0.01                 # ends this close are one point: a closed line
# what the kernel reads, in the order of roadsmooth.rs's Params (one place for the numbers)
PARAMS = np.array([MIN_DEFLECT_DEG, MAX_DEFLECT_DEG, ARC_STEP_M, ARC_STEP_DEG, FREE_SHARE, MAX_SHIFT_M, MIN_PIECE_M,
                   CLOSED_M], np.float64)


def smooth_line(xy, radius):
    """(new points (m, 2), knots (old distance, new distance) at the line's vertices and tangent points) of the line
    xy (k, 2) with its bends rounded; None when nothing changes. Ends and unrounded vertices keep their coordinates.
    The geometry is native/earthfast/src/roadsmooth.rs (the NumPy original is the tests' oracle)."""
    fast.require(169, "road_smooth_line")
    return fast._ef.road_smooth_line(np.ascontiguousarray(xy, np.float64), float(radius), PARAMS)


def _radius(feats, i, rules):
    """The arc radius of road feature i, or None for a feature that is not smoothed."""
    sub = feats.attrs.get("subtype")
    if sub is not None and str(sub[i]) != "road":
        return None
    cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
    r = RADIUS_M.get(cls)
    if r is None:
        return None
    sc = rules.get("subclass")
    if sc is not None and any(v == "link" for v, _, _ in sc.for_feature(i)):
        r = min(r, LINK_RADIUS_M)
    return r


def smooth(feats, frame):
    """feats (the roads layer, mapgeom.Features with lonlat, en and rules, localized in frame) with its bends rounded,
    in place; returns it. Without roads, or unless BNE_ROAD_SMOOTH=on, nothing changes."""
    if not ON or feats is None or not len(feats) or feats.en is None or not len(feats.en):
        return feats
    po, ro, co = feats.part_off, feats.ring_off, feats.coord_off
    changed = {}                                    # feature -> (new en (m, 2), old knots, new knots, old length)
    for i in range(len(feats)):
        if po[i + 1] - po[i] != 1 or ro[po[i] + 1] - ro[po[i]] != 1:
            continue
        r = _radius(feats, i, feats.rules)
        if r is None:
            continue
        a, b = co[ro[po[i]]], co[ro[po[i]] + 1]
        got = smooth_line(feats.en[a:b], r)
        if got is not None:
            changed[i] = got
    if not changed:
        return feats
    ring_len = np.diff(co).astype(np.int64)
    lines = {}                                      # ring -> feature
    for i, (pts, _, _) in changed.items():
        ring = int(ro[po[i]])
        ring_len[ring] = len(pts)
        lines[ring] = i
    new_co = np.concatenate([[0], np.cumsum(ring_len)])
    en = np.empty((int(new_co[-1]), 2), np.float64)
    lonlat = np.empty((int(new_co[-1]), 2), np.float64)
    for ring in range(len(ring_len)):
        a0, b0 = co[ring], co[ring + 1]
        a1, b1 = new_co[ring], new_co[ring + 1]
        i = lines.get(ring)
        if i is None:
            en[a1:b1] = feats.en[a0:b0]
            lonlat[a1:b1] = feats.lonlat[a0:b0]
            continue
        pts = changed[i][0]
        en[a1:b1] = pts
        # the line's own coordinates stay exact (the ends, every vertex not rounded): only new points are converted
        lonlat[a1:b1] = _lonlat(pts, feats.en[a0:b0], feats.lonlat[a0:b0], frame)
    # the rules' fractions of the line's length
    for name, rt in feats.rules.items():
        if not len(rt.feature):
            continue
        start, end = np.array(rt.start, copy=True), np.array(rt.end, copy=True)
        for row in np.nonzero(np.isin(np.asarray(rt.feature), np.fromiter(changed, np.int64, len(changed))))[0]:
            _, old_s, new_s = changed[int(rt.feature[row])]
            for arr in (start, end):
                f = float(arr[row])
                arr[row] = np.interp(f * old_s[-1], old_s, new_s) / new_s[-1] if new_s[-1] > 0 else f
        rt.start, rt.end = start.astype(rt.start.dtype, copy=False), end.astype(rt.end.dtype, copy=False)
        rt.__dict__["_rows"] = None
        rt.__dict__["_major"] = None
    feats.coord_off, feats.en, feats.lonlat = new_co, en, lonlat
    for key in ("_xy_in", "_centres", "_boxes", "_lonlat_boxes"):
        feats.__dict__.pop(key, None)
    return feats


def _lonlat(pts, old_en, old_ll, frame):
    """lon / lat (m, 2) of the new points: the old vertices' own values where a point is one of them, else converted."""
    out = np.empty((len(pts), 2), np.float64)
    lat, lon, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    out[:, 0], out[:, 1] = lon, lat
    out[0], out[-1] = old_ll[0], old_ll[-1]
    # an unrounded vertex is a point of both lines: find it by its exact coordinates
    index = {(float(e), float(n)): j for j, (e, n) in enumerate(old_en)}
    for m in range(1, len(pts) - 1):
        j = index.get((float(pts[m, 0]), float(pts[m, 1])))
        if j is not None:
            out[m] = old_ll[j]
    return out
