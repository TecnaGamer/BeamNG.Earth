"""Warning signs for bends (the user, 2026-10-25: "sharp bend signs on bends, things to make trucking feel more
real"): every bend that needs slowing for gets the sign a highway department would put up, from the road's own
geometry, the same everywhere (ROADMAP "improvements must generalise": no place is special-cased).

How a bend is found and signed (US MUTCD 11th ed., Section 2C; the km/h countries' Vienna-convention signs use
the same rules with their own models):
- the road's centreline is resampled every STEP_M and its heading's rate of change, smoothed over SMOOTH_M,
  is its curvature; a run where the radius stays under MAX_R_M is a bend, its radius the run's tightest;
- the bend's comfortable speed is AASHTO's point-mass rule, V = sqrt(15 R (e + f)) (V mph, R ft), with the
  superelevation plus side friction of a signed advisory speed (E_PLUS_F), rounded down to 5 mph;
- a bend whose advisory speed is ADVISE_MPH or more below the road's limit gets a warning sign before it
  (Table 2C-5): a Turn sign at 30 mph or less, else a Curve sign, pointing the way the road bends, with the
  advisory speed plaque under it, ADVANCE_DECEL m/s^2 of braking ahead of the bend (Table 2C-4's order);
- one CHEVRON_MPH or more below: chevrons along the outside of the bend, as far apart as Table 2C-6 says
  for its advisory speed, one face per direction of travel.
Two-way roads are signed for both directions, one-way roads along their direction only (Spec.dir). Every
position is a pure function of the road's geometry and id (ROADMAP "deterministic generation"): a road
drawn by two tiles, from any anchor, puts its signs in the same places, and each tile keeps the ones in
its box.

Rows are trees.lua's tile rows [group, x, y, z, yaw, scale, height, rank]; yaw turns the model's local +x to
the right of the travel direction it warns, and the models are built with their face along local -y, so the
face looks back at that traffic on either side of the road and in left- or right-hand countries. Pure.
"""
import math

import numpy as np

STEP_M = 5.0                # m between centreline samples
SMOOTH_M = 20.0             # m over which the heading's change is averaged (a vertex kink is no bend)
MAX_R_M = 450.0             # m: a gentler bend never needs a sign at the speeds this mod's roads post
MERGE_M = 30.0              # m: tight runs of one direction nearer than this are one bend (a sparse line's vertices)
MIN_TURN_DEG = 12.0         # a run turning less than this in all is a kink in the map line, not a bend
E_PLUS_F = 0.20             # superelevation + side friction of an advisory speed (ball-bank ~12-14 deg)
ADVISE_MPH = 10             # advisory this far under the limit: a warning sign (MUTCD: required at 10)
CHEVRON_MPH = 15            # ... this far under: chevrons too (MUTCD: recommended at 15, required at 20)
TURN_MAX_MPH = 30           # advisory at or under: the Turn sign (a right-angle arrow), else the Curve sign
ADVANCE_DECEL = 3.0         # m/s^2 of braking from the limit to the advisory before the bend starts
ADVANCE_MIN_M = 30.0        # m: the nearest a warning stands to its bend
ROAD_END_M = 15.0           # m: no sign nearer a road's end than this (the junction it starts at)
SIGN_OFF_M = 1.5            # m beyond the carriageway's edge (furniture.SIGN_OFF_M)
SIGN_H = 2.6                # m: the warning sign's height (its post, MUTCD's 5 ft rural mounting + the sign)
CHEVRON_H = 1.9
# Table 2C-6: chevron spacing (ft) by advisory speed (mph, up to)
CHEVRON_SPACING_FT = ((15, 40), (30, 80), (45, 120), (60, 160), (999, 200))
CURVE_CLASSES = {"motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "tertiary",
                 "unclassified"}
G_TURN, G_CURVE, G_CHEVRON = "warn_turn", "warn_curve", "chevron"     # + "_l" / "_r"
G_PLAQUE = "advisory"       # + the speed: "advisory25" (mph) / "advisoryk40" (km/h)

M_PER_FT = 0.3048
MPH_MS = 0.44704


def advisory_mph(radius_m):
    """The advisory speed (mph, a multiple of 5, at least 10) of a bend of radius_m."""
    v = math.sqrt(15.0 * (radius_m / M_PER_FT) * E_PLUS_F)
    return max(10, int(v // 5) * 5)


def chevron_spacing_m(adv_mph):
    for top, ft in CHEVRON_SPACING_FT:
        if adv_mph <= top:
            return ft * M_PER_FT
    return 200 * M_PER_FT


def resample(line, step=STEP_M):
    """Points every `step` m along polyline line (k, 3) (the last one at its end) and their arc lengths."""
    seg = np.hypot(*np.diff(line[:, :2], axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    L = float(s[-1])
    if L < step:
        return line[[0, -1]].astype(np.float64), np.array([0.0, L])
    t = np.concatenate([np.arange(0.0, L, step), [L]])
    pts = np.stack([np.interp(t, s, line[:, i]) for i in range(3)], axis=1)
    return pts, t


def curvature(pts, s):
    """Signed curvature (1/m, + turning left) at each sample, from the heading smoothed over SMOOTH_M."""
    if len(pts) < 3:
        return np.zeros(len(pts))
    d = np.diff(pts[:, :2], axis=0)
    h = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))         # heading of each segment
    hs = np.concatenate([[h[0]], h])                    # ... at each sample (the segment arriving)
    half = SMOOTH_M / 2
    k = np.zeros(len(pts))
    for i in range(len(pts)):
        a = int(np.searchsorted(s, s[i] - half))
        b = int(np.searchsorted(s, s[i] + half, side="right")) - 1
        a, b = max(a, 1), min(b, len(pts) - 1)
        # hs[j] is the heading of the segment ending at sample j: from segment a to segment b the heading
        # turns over the arc between their midpoints
        ds = 0.5 * (s[b] + s[b - 1]) - 0.5 * (s[a] + s[a - 1])
        if b > a and ds > 0:
            k[i] = (hs[b] - hs[a]) / ds
    return k


def bends(line):
    """The bends of a centreline (k, 3): [(s0, s1, radius m, +1 left / -1 right, total turn deg)], s0 < s1 the
    arc lengths (m from its first vertex) of the run where its radius stays under MAX_R_M."""
    pts, s = resample(np.asarray(line, np.float64))
    k = curvature(pts, s)
    tight = np.abs(k) > 1.0 / MAX_R_M
    out = []
    i = 0
    n = len(s)
    while i < n:
        if not tight[i]:
            i += 1
            continue
        sgn = 1.0 if k[i] > 0 else -1.0
        j = i
        while j + 1 < n and tight[j + 1] and (k[j + 1] > 0) == (sgn > 0):
            j += 1
        if j > i:
            out.append([i, j, sgn])
        i = j + 1
    # a bend drawn with few vertices (a hairpin in 4-6 map points) reads as tight runs at its vertices with
    # straight chords between: the runs of one sign nearer than MERGE_M are one bend
    merged = []
    for i, j, sgn in out:
        if merged and merged[-1][2] == sgn and s[i] - s[merged[-1][1]] <= MERGE_M:
            merged[-1][1] = j
        else:
            merged.append([i, j, sgn])
    res = []
    for i, j, sgn in merged:
        kk = np.clip(k[i:j + 1] * sgn, 0.0, None)
        turn = float(np.trapezoid(kk, s[i:j + 1]))
        if math.degrees(turn) < MIN_TURN_DEG:
            continue
        # the tightest smoothed radius, or the bend's mean radius (its length over its turn, less the smoothing
        # window's spill at either end) where that is gentler: a vertex of a sparse line spikes the curvature
        r_tight = 1.0 / float(kk.max())
        r_mean = max(s[j] - s[i] - SMOOTH_M, STEP_M) / turn
        res.append((float(s[i]), float(s[j]), max(r_tight, r_mean), sgn, math.degrees(turn)))
    return res


def _pose(line, s):
    """Point (3,) and unit direction (2,) at arc length s along line (k, 3)."""
    seg = np.hypot(*np.diff(line[:, :2], axis=0).T)
    ss = np.concatenate([[0.0], np.cumsum(seg)])
    s = min(max(s, 0.0), float(ss[-1]))
    k = int(np.clip(np.searchsorted(ss, s, side="right") - 1, 0, len(line) - 2))
    L = max(float(ss[k + 1] - ss[k]), 1e-9)
    t = (s - ss[k]) / L
    p = line[k] + (line[k + 1] - line[k]) * t
    d = (line[k + 1, :2] - line[k, :2]) / L
    return p, d


def _yaw(dx, dy):
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _plaque(adv_mph, mph):
    if mph:
        return f"{G_PLAQUE}{adv_mph}"
    kmh = int(round(adv_mph * 1.609344 / 10.0)) * 10
    return f"{G_PLAQUE}k{kmh}"


def curve_signs(roads, speeds, box, left_hand=False, mph=True, clear=None):
    """Rows of bend warnings (warning sign + advisory plaque) and chevrons in tile box (x0, y0, x1, y1).
    roads: build_tile's (fid, spec, centreline (k, 3), lon); speeds: fid -> the road's limit (mph, navgraph's);
    clear(x, y, z, fid) -> bool: the spot is off every other carriageway (None: not checked)."""
    x0, y0, x1, y1 = box
    rows = []

    def put(group, p, d_travel, off, height, far=False):
        # near: the driver's side of the travel direction d_travel (the right; the left in left-hand
        # countries); far: the other side. Wherever it stands, the face looks back at that traffic, so the yaw
        # only depends on the travel direction: the model's +x to the right of travel, its face along its
        # local -y (= -d_travel).
        right = np.array([d_travel[1], -d_travel[0]])
        near = right * (-1.0 if left_hand else 1.0)
        q = p[:2] + near * (-off if far else off)
        if not (x0 <= q[0] < x1 and y0 <= q[1] < y1):
            return
        if clear is not None and not clear(float(q[0]), float(q[1]), float(p[2]), fid):
            return
        rows.append([group, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2]), 2),
                     round(_yaw(right[0], right[1]), 1), 1.0, height, 0.0])

    for fid, spec, line, _ in roads:
        lim = speeds.get(fid)
        if lim is None or spec.cls not in CURVE_CLASSES or len(line) < 2:
            continue
        line = np.asarray(line, np.float64)
        L = float(np.hypot(*np.diff(line[:, :2], axis=0).T).sum())
        found = bends(line)
        if not found:
            continue
        half = spec.width / 2 + SIGN_OFF_M
        dirs = (1, -1)
        if spec.oneway:
            dirs = (spec.dir if spec.dir in (1, -1) else 1,)
        for s0, s1, r, sgn, _turn in found:
            adv = advisory_mph(r)
            if lim - adv < ADVISE_MPH:
                continue
            for way in dirs:
                # travelling along the line (way +1) the bend turns sgn; against it, the other way
                turn_dir = sgn * way
                lr = "_l" if turn_dir > 0 else "_r"
                v1, v2 = lim * MPH_MS, adv * MPH_MS
                adv_m = max(ADVANCE_MIN_M, (v1 * v1 - v2 * v2) / (2 * ADVANCE_DECEL))
                start = s0 if way > 0 else s1
                at = start - way * adv_m
                if way > 0:
                    at = max(at, ROAD_END_M)
                    ok = at < start - 10.0
                else:
                    at = min(at, L - ROAD_END_M)
                    ok = at > start + 10.0
                if ok:
                    p, d = _pose(line, at)
                    dt = d * way
                    put((G_TURN if adv <= TURN_MAX_MPH else G_CURVE) + lr, p, dt, half, SIGN_H)
                    put(_plaque(adv, mph), p, dt, half, SIGN_H - 0.75)
                if lim - adv >= CHEVRON_MPH:
                    # along the outside of the bend as this direction drives it, facing it: the driver's
                    # side when the road bends away from it (a left bend where traffic keeps right)
                    near_out = (turn_dir > 0) != left_hand
                    sp = chevron_spacing_m(adv)
                    n = max(3, int((s1 - s0) // sp) + 1)
                    for t in np.linspace(s0, s1, n):
                        p, d = _pose(line, float(t))
                        put(G_CHEVRON + lr, p, d * way, half, CHEVRON_H, far=not near_out)
    return rows
