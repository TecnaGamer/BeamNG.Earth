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

VERSION = 5                     # bump when the arcs change (mapdata.versions(): the derived caches are built again)
# On by default since 2026-11-12 (the user: "the road don't smoothly curve in some places ... most obvious with
# racetracks with sloped turns because the car has a bumpy ride with these low res turns"; Daytona's oval is mapped as
# 19 m chords turning 2-7 degrees each). BNE_ROAD_SMOOTH=off: the roads as mapped.
ON = os.environ.get("BNE_ROAD_SMOOTH", "on") not in ("off", "0", "false")
# roads.simplify's RDP tolerance while on: its own 0.25 m put a vertex back every ~16-20 m of a R 130 m arc, turning
# 5-7 degrees, the corners again (the user, 2026-11-08, near Korostivtsi: "still corners"; on three cells the visible
# corners, > 3 degrees between pieces >= 5 m, were 2024 with the arcs against 2013 without; 1291 at 0.03 m, +10 % nodes)
SIMPLIFY_M = 0.03

# class -> radius (m) of a bend's arc when the line's pieces are long enough (street-level reading of Overture's
# classes: a motorway's curves are gentle, a lane's tight)
RADIUS_M = {"motorway": 300.0, "trunk": 250.0, "primary": 150.0, "secondary": 100.0, "tertiary": 50.0,
            "unclassified": 30.0, "residential": 25.0, "unknown": 25.0, "track": 15.0, "living_street": 12.0,
            "raceway": 30.0, "busway": 50.0}
# (no "service": driveways and parking aisles are most of a cell's lines, and their rounded corners (at 12 m) made
# 4.5x the points, every road tile ~20 % slower to build, for corners nobody drives fast; 2026-11-12)
# Curvature continuous (G2, roadsmooth.rs road_smooth_g2): the arcs above meet their straights and each other with a
# step in curvature (a car's lateral pull jumps there: on a mapped curve whose vertices turn unevenly, at every vertex),
# so each vertex's turn is spread over a triangle of half length up to the class's G2_WINDOW_M (transition spirals,
# 2 x the window long), at most what moves the line G2_SHIFT_M at a curve's end or corner, within G2_MAX_DEV_M of the
# arcs. The vertices where another road joins the line (its interior connectors, Overture's) stay where they are, as
# its ends do. BNE_ROAD_G2=off: the arcs alone.
G2_ON = os.environ.get("BNE_ROAD_G2", "on") not in ("off", "0", "false")
G2_WINDOW_M = {"motorway": 40.0, "trunk": 35.0, "raceway": 40.0, "primary": 25.0, "secondary": 20.0, "busway": 15.0,
               "tertiary": 15.0, "unclassified": 10.0, "residential": 8.0, "unknown": 8.0, "track": 6.0,
               "living_street": 5.0, "service": 5.0}
G2_LINK_WINDOW_M = 20.0         # a ramp's
G2_SHIFT_M = 0.4                # m the spreading may move a curve's end or a corner
G2_CELL_M = 1.0                 # m: the integration step
G2_OUT_TOL_M = 0.02             # m: the points kept on a curve are as few as stay this close to it ...
# (... the fast classes' closer: at 0.02 m a R 300 m curve's points are 7 m apart and turn 1.3 degrees each, felt at
# speed and read by the banking; the slow classes' are most of a cell's lines)
G2_FAST_TOL_M = 0.005
G2_FAST = {"motorway", "trunk", "raceway", "primary"}
G2_OUT_M = 20.0                 # ... at most this far apart ...
G2_OUT_DEG = 6.0                # ... and turning at most this between two
G2_MAX_DEV_M = 1.0              # m from the arcs' line, else smaller windows (twice), then the arcs alone
G2_PARAMS = np.array([G2_SHIFT_M, G2_CELL_M, G2_OUT_M, G2_OUT_DEG, G2_MAX_DEV_M, G2_OUT_TOL_M], np.float64)
PIN_M = 1.0                     # m: an interior connector this near a vertex pins it (else the piece's two vertices)
# a railway's bend (plan 035: a train's flange panels strike a rail at every corner of the mapped polyline; the sharpest in
# Kyiv-Pasazhyrskyi turned 14 degrees in 2 m): a line's arcs, class by class. BNE_RAIL_CURVES=on rounds them.
# (Off unless asked since the roads' smoothing is on by default, 2026-11-12: the rails were only ever rounded under
# BNE_ROAD_SMOOTH=on, and their switch pins (pinned_vertices, every rail end against every line in Python) cost ~1 s
# a cell in Chicago's rail yards.)
RAIL_ON = os.environ.get("BNE_RAIL_CURVES", "off") in ("on", "1", "true")
RAIL_RADIUS_M = {"standard_gauge": 250.0, "narrow_gauge": 80.0, "light_rail": 80.0}
# A switch: another rail line's end lying on a line (the map has it a few tenths of a metre off its vertex, a cell's
# lines are not split at every switch) keeps the line's vertices beside it as mapped, else the arc moved the line up to
# MAX_SHIFT_M off the branch's end and the branch's bed no longer met it (Kalynivka's crossover, plan 040).
# BNE_RAIL_SWITCH_FIX=off: rounded as before.
SWITCH_PIN_ON = os.environ.get("BNE_RAIL_SWITCH_FIX", "on") not in ("off", "0", "false")
SWITCH_PIN_M = 1.0              # m: a line's end this near another rail line is on it
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


def smooth_lines_g2(xy, off, radius, window, pin_off, pins, tol=None):
    """smooth_line's arcs, then curvature continuous (G2_*), for many lines at once: xy (N, 2) their points, off
    (n + 1) into it, radius (n) each line's arc radius (NaN: left as it is), window (n) its spirals' half length,
    pin_off (n + 1) into pins (each line's interior vertex indices kept where they are), tol (n) each line's
    tolerance of the points kept (NaN or None: G2_OUT_TOL_M). Returns (points (M, 2), their
    offsets (n + 1), knot offsets (n + 1), old and new distances at the knots); a line without knots did not change.
    Ends and pins keep their coordinates bit for bit. The geometry is native/earthfast/src/roadsmooth.rs."""
    fast.require(230, "road_smooth_g2")
    tol = np.full(len(radius), np.nan) if tol is None else tol
    return fast._ef.road_smooth_g2(np.ascontiguousarray(xy, np.float64), np.ascontiguousarray(off, np.int64),
                                   np.ascontiguousarray(radius, np.float64), np.ascontiguousarray(window, np.float64),
                                   np.ascontiguousarray(tol, np.float64), np.ascontiguousarray(pin_off, np.int64),
                                   np.ascontiguousarray(pins, np.int64), PARAMS, G2_PARAMS)


def smooth_line_g2(xy, radius, pins, window, tol=None):
    """smooth_lines_g2 of one line xy (k, 2): (points (m, 2), old distances, new distances at the knots) or None when
    nothing changes."""
    xy = np.asarray(xy, np.float64)
    pins = np.asarray(pins, np.int64).reshape(-1)
    pts, _, ko, old_s, new_s = smooth_lines_g2(xy, [0, len(xy)], [radius], [window], [0, len(pins)], pins,
                                               [np.nan if tol is None else tol])
    return (pts, old_s, new_s) if ko[1] > 0 else None


def interior_connectors(con):
    """{feature: [fraction, ...]} of the connectors inside a line (not at its ends) of a "connectors" rule table."""
    out = {}
    if con is None or not len(con.feature):
        return out
    at = np.asarray(con.start, np.float64)
    m = (at > 1e-4) & (at < 1.0 - 1e-4)
    for f, t in zip(np.asarray(con.feature)[m].tolist(), at[m].tolist()):
        out.setdefault(f, []).append(t)
    return out


def connector_pins(xy, at):
    """The interior vertex indices of a road's line xy (k, 2) where its interior connectors are (at: their positions
    as fractions of its length, interior_connectors): the vertex within PIN_M of one, else both vertices of the piece
    it lies on. Pure (the feature's own rules)."""
    if not at or len(xy) < 3:
        return []
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(np.asarray(xy, np.float64), axis=0).T))]
    pins = set()
    for t in np.asarray(at, np.float64) * s[-1]:
        v = int(np.argmin(np.abs(s - t)))
        if abs(s[v] - t) <= PIN_M:
            pins.add(v)
        else:
            k = int(np.clip(np.searchsorted(s, t) - 1, 0, len(s) - 2))
            pins.update((k, k + 1))
    return sorted(v for v in pins if 0 < v < len(xy) - 1)


def pinned_vertices(xy, ends):
    """The interior vertex indices of the line xy (k, 2) not to be rounded: both ends of every segment that a point of
    ends (m, 2: the ends of the other rail lines) lies within SWITCH_PIN_M of, but for those at the line's own ends. Pure."""
    xy = np.asarray(xy, np.float64)
    ends = np.asarray(ends, np.float64).reshape(-1, 2)
    if len(xy) < 3 or not len(ends):
        return []
    a, b = xy[:-1], xy[1:]
    ab = b - a
    den = np.maximum((ab * ab).sum(1), 1e-12)
    pins = set()
    for p in ends:
        if min(float(np.hypot(*(p - xy[0]))), float(np.hypot(*(p - xy[-1])))) < SWITCH_PIN_M:
            continue                # (a junction at the line's own end: a node, nothing to keep)
        t = np.clip(((p - a) * ab).sum(1) / den, 0.0, 1.0)
        d = np.hypot(*(a + t[:, None] * ab - p).T)
        for k in np.flatnonzero(d < SWITCH_PIN_M):
            pins.update(v for v in (int(k), int(k) + 1) if 0 < v < len(xy) - 1)
    return sorted(pins)


def smooth_pinned(xy, radius, pins):
    """smooth_line of xy with the vertices at pins (interior indices) kept: each piece between them (and the ends)
    rounded alone, its ends staying where they are. Same result as smooth_line; None when nothing changes."""
    cuts = [0] + list(pins) + [len(xy) - 1]
    pts, old_s, new_s = [], [], []
    o_off = n_off = 0.0
    any_change = False
    for c0, c1 in zip(cuts[:-1], cuts[1:]):
        seg = np.asarray(xy[c0:c1 + 1], np.float64)
        got = smooth_line(seg, radius) if len(seg) >= 3 else None
        if got is None:
            d = np.r_[0.0, np.cumsum(np.hypot(*np.diff(seg, axis=0).T))]
            got = (seg, d, d)
        else:
            any_change = True
        p, o, n = got
        first = 0 if not pts else 1
        pts.append(np.asarray(p)[first:])
        old_s.append(np.asarray(o)[first:] + o_off)
        new_s.append(np.asarray(n)[first:] + n_off)
        o_off += float(o[-1])
        n_off += float(n[-1])
    if not any_change:
        return None
    return np.concatenate(pts), np.concatenate(old_s), np.concatenate(new_s)


def _links(rules):
    """The features with a subclass "link" rule (ramps, connector roads)."""
    sc = rules.get("subclass")
    if sc is None or not len(sc.feature):
        return set()
    return set(np.asarray(sc.feature)[np.asarray(sc.value) == "link"].tolist())


def _radius(feats, i, rules, links=None):
    """The arc radius of road feature i, or None for a feature that is not smoothed."""
    sub = feats.attrs.get("subtype")
    if sub is not None and str(sub[i]) == "rail":
        return RAIL_RADIUS_M.get(str(feats.attrs["class"][i])) if RAIL_ON and "class" in feats.attrs else None
    if sub is not None and str(sub[i]) != "road":
        return None
    cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
    r = RADIUS_M.get(cls)
    if r is None:
        return None
    if i in (_links(rules) if links is None else links):
        r = min(r, LINK_RADIUS_M)
    return r


def _window(feats, i, rules, links=None):
    """The G2 window (m) of road feature i: its class's, a ramp's (subclass link) at most G2_LINK_WINDOW_M."""
    cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
    w = G2_WINDOW_M.get(cls, 8.0)
    if i in (_links(rules) if links is None else links):
        w = min(w, G2_LINK_WINDOW_M)
    return w


def smooth(feats, frame):
    """feats (the roads layer, mapgeom.Features with lonlat, en and rules, localized in frame) with its bends rounded,
    in place; returns it. Without roads, or with BNE_ROAD_SMOOTH=off, nothing changes."""
    if not ON or feats is None or not len(feats) or feats.en is None or not len(feats.en):
        return feats
    po, ro, co = feats.part_off, feats.ring_off, feats.coord_off
    changed = {}                                    # feature -> (new en (m, 2), old knots, new knots, old length)
    sub = feats.attrs.get("subtype")
    ends = []                                       # (the ends of the rail lines: where a switch's branch begins)
    inner = interior_connectors(feats.rules.get("connectors")) if G2_ON else {}
    batch = []                                      # (feature, first, end, radius, window, pins): the G2 lines
    tols = []
    classes = feats.attrs.get("class")

    def cls_of(i):
        return str(classes[i]) if classes is not None else "unknown"
    links = _links(feats.rules)
    if SWITCH_PIN_ON and RAIL_ON and sub is not None:
        for i in range(len(feats)):
            if str(sub[i]) == "rail" and po[i + 1] - po[i] == 1 and ro[po[i] + 1] - ro[po[i]] == 1:
                a, b = co[ro[po[i]]], co[ro[po[i]] + 1]
                if b - a >= 2:
                    ends.append((i, feats.en[a], feats.en[b - 1]))
    for i in range(len(feats)):
        if po[i + 1] - po[i] != 1 or ro[po[i] + 1] - ro[po[i]] != 1:
            continue
        r = _radius(feats, i, feats.rules, links)
        if r is None:
            continue
        a, b = co[ro[po[i]]], co[ro[po[i]] + 1]
        pins = []
        rail = sub is not None and str(sub[i]) == "rail"
        if ends and rail:
            pins = pinned_vertices(feats.en[a:b], [e for j, p, q in ends if j != i for e in (p, q)])
        if G2_ON and not rail:
            # (one kernel call for all of them, below. OpenStreetMap's raceways and busways carry no connectors:
            # their joins move with the line, by at most the arcs' and the spirals' shifts. Pins from the other
            # roads' ends in the cell would not be pure: the cells a line crosses hold different roads.)
            batch.append((i, a, b, r, _window(feats, i, feats.rules, links), connector_pins(feats.en[a:b], inner.get(i))))
            tols.append(G2_FAST_TOL_M if cls_of(i) in G2_FAST else np.nan)
            continue
        got = smooth_pinned(feats.en[a:b], r, pins) if pins else smooth_line(feats.en[a:b], r)
        if got is not None:
            changed[i] = got
    if batch:
        idx = [np.arange(a, b) for _, a, b, _, _, _ in batch]
        off = np.r_[0, np.cumsum([b - a for _, a, b, _, _, _ in batch])]
        pin_off = np.r_[0, np.cumsum([len(p) for *_, p in batch])]
        pins = np.fromiter((v for *_, p in batch for v in p), np.int64, int(pin_off[-1]))
        pts, po_, ko, old_s, new_s = smooth_lines_g2(feats.en[np.concatenate(idx)], off,
                                                     [r for _, _, _, r, _, _ in batch],
                                                     [w for _, _, _, _, w, _ in batch], pin_off, pins, tols)
        for j, (i, *_rest) in enumerate(batch):
            if ko[j + 1] > ko[j]:
                changed[i] = (pts[po_[j]:po_[j + 1]], old_s[ko[j]:ko[j + 1]], new_s[ko[j]:ko[j + 1]])
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
    new_pt = np.zeros(int(new_co[-1]), bool)        # (the points to convert to lon / lat: one call for them all)
    for ring in range(len(ring_len)):
        a0, b0 = co[ring], co[ring + 1]
        a1, b1 = new_co[ring], new_co[ring + 1]
        i = lines.get(ring)
        if i is None:
            en[a1:b1] = feats.en[a0:b0]
            lonlat[a1:b1] = feats.lonlat[a0:b0]
            continue
        pts = np.asarray(changed[i][0], np.float64)
        en[a1:b1] = pts
        new_pt[a1:b1] = True
        # the line's own coordinates stay exact (the ends, every vertex kept where it was): only new points are
        # converted. A kept vertex is a point of both lines: found by its exact coordinates.
        old = feats.en[a0:b0]
        kept = np.isin(pts[:, 0] + 1j * pts[:, 1], old[:, 0] + 1j * old[:, 1])
        if kept[1:-1].any():
            index = {(float(e), float(n)): j for j, (e, n) in enumerate(old)}
            for m in np.flatnonzero(kept[1:-1]) + 1:
                j = index.get((float(pts[m, 0]), float(pts[m, 1])))
                if j is not None:
                    lonlat[a1 + m] = feats.lonlat[a0 + j]
                    new_pt[a1 + m] = False
        lonlat[a1], lonlat[b1 - 1] = feats.lonlat[a0], feats.lonlat[b0 - 1]
        new_pt[a1] = new_pt[b1 - 1] = False
    if new_pt.any():
        lat, lon, _ = frame.enu_to_geodetic(en[new_pt, 0], en[new_pt, 1], 0.0)
        lonlat[new_pt, 0], lonlat[new_pt, 1] = lon, lat
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
    for key in ("_xy_in", "_centres", "_boxes", "_boxes_in", "_ll_boxes", "_lonlat_boxes", "_feature_boxes"):
        feats.__dict__.pop(key, None)
    return feats
