"""fork: Australia's yellow diamond curve and turn warning signs (AS 1742.2 W-series; tools/build_au_warnsigns.py): where a
rural road of 70 km/h or more bends sharply, a sign on the driver's left (left-hand traffic) 100-150 m before the bend,
by the speed. Nothing guessed beyond the road's own shape: what the map shows is the road's centreline, so a "sharp
curve" is a run of it whose radius, read over WINDOW_M of road, stays under SHARP_RADIUS_M (~150 m) and that turns the
road by at least MIN_DEFLECTION_DEG: a kink at one digitised node is not a curve.

The same Forest rows as kantstolpar.py (one road's near_roads, whole ground parts), called from blockworker._kantstolpe_rows
for Australia: a tile keeps the signs in its box, and each is worked out from the road alone, so tiles agree. Conservative:
a sign for a bend only where one stands clear of every junction, never closer than MIN_GAP_M after the last on that road
and way of travel, none inside the first / last WINDOW_M of a road (its bend unknown), none on motorways, urban streets,
roads with footpaths or unsealed ones.

fork (user, in-game testing in Queensland: the signs were far too rare): the radius and deflection a bend is signed at go
by the road's speed (BEND_BY_KMH: under 400 m and 20 degrees at 100 km/h, 250 m at 80, 150 m / 30 degrees below), and the
spacing is MIN_GAP_M 150 m. No advisory speed plate (it would need a model per speed).
"""
import math

import numpy as np

import guardrails
import medians
import motesfri

COUNTRIES = {"AU"}
CLASSES = {"trunk", "primary", "secondary", "tertiary"}
MIN_KMH = 70.0                  # (and an unclassified road only from 80)
UNCLASSIFIED_KMH = 80.0
OUT_M = 2.0                     # m outside the sealed edge (spec.width includes the sealed shoulders)
STEP_M = 5.0                    # m: the road is read at this spacing
WINDOW_M = 20.0                 # the bend is read over this much road either side of a station: R = WINDOW_M / turn
SHARP_RADIUS_M = 150.0          # a bend tighter than this is worth a sign (a road below 80 km/h)
MIN_DEFLECTION_DEG = 30.0       # ... if it turns the road at least this much
# fork: by the road's speed, a bend that needs a noticeably lower speed than the limit is worth a sign: (km/h at or over,
# radius m under, deflection degrees at least). At 100 a radius under 400 m (advisory ~110 km/h at best) needs slowing
# for; the deflection keeps a gentle or noisy line (a few degrees at a digitised node) from being one.
BEND_BY_KMH = ((100.0, 400.0, 20.0), (80.0, 250.0, 20.0), (0.0, SHARP_RADIUS_M, MIN_DEFLECTION_DEG))
DEFAULT_KMH = 100.0             # (a numbered rural road with no limit mapped)
MIN_RUN_M = 25.0                # ... over at least this much road
JOIN_M = 40.0                   # bends the same way this close are one
TURN_DEG, TURN_RADIUS_M = 70.0, 90.0    # a square turn rather than a curve: this much deflection at a median radius under this
MIN_GAP_M = 150.0               # m between signs of one road, one way of travel (fork: was 250)
JUNCTION_CLEAR_M = 8.0          # m a sign stands clear past a crossing road's edge
RAIL_CLEAR_M = 3.0              # m from a mapped barrier
HEIGHT_M = 2.56                 # the models' height (tools/build_au_warnsigns.py)
ADVANCE_M = ((100.0, 150.0), (80.0, 120.0), (0.0, 100.0))   # (km/h at or over, m before the bend)
GROUPS = ("au_warn_curve_l", "au_warn_curve_r", "au_warn_turn_l", "au_warn_turn_r")


def wanted(spec):
    """Whether a road of this spec gets curve warnings: a sealed rural road with a numbered class and a country speed."""
    if spec.kind != "paved" or spec.urban or spec.sides:
        return False
    if spec.cls in CLASSES:
        return spec.kmh is None or spec.kmh >= MIN_KMH
    return spec.cls == "unclassified" and spec.kmh is not None and spec.kmh >= UNCLASSIFIED_KMH


def advance_m(kmh):
    """m before a bend its sign stands, by the road's limit (None: the rural default)."""
    v = 100.0 if kmh is None else float(kmh)
    return next(m for k, m in ADVANCE_M if v >= k)


def bend_limits(kmh):
    """fork: (radius m, deflection degrees) a bend of a road of this limit (None: DEFAULT_KMH) must be under / over."""
    v = DEFAULT_KMH if kmh is None else float(kmh)
    _, r, d = next(row for row in BEND_BY_KMH if v >= row[0])
    return r, d


def bends(xy, radius_m=SHARP_RADIUS_M, min_deflection=MIN_DEFLECTION_DEG):
    """[(start m, end m, +1 left / -1 right going along xy, deflection degrees, median radius m)] of xy (k, >=2)'s sharp
    bends (see the module doc: radius_m and min_deflection by bend_limits), and the polyline's length. Pure."""
    seg = np.hypot(*np.diff(xy, axis=0).T)
    xy = xy[np.r_[True, seg > 1e-6]]
    cum = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    length = float(cum[-1])
    s = np.arange(0.0, length, STEP_M)
    if len(s) < 2 or length < 4 * WINDOW_M:
        return [], length

    def at(d):
        d = np.clip(d, 0.0, length)
        return np.c_[np.interp(d, cum, xy[:, 0]), np.interp(d, cum, xy[:, 1])]
    p = at(s)
    a, b = p - at(s - WINDOW_M), at(s + WINDOW_M) - p
    turn = np.arctan2(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0], (a * b).sum(1))      # + left, over WINDOW_M of road
    turn[(s < WINDOW_M) | (s > length - WINDOW_M)] = 0.0          # (the ends: straight, the bend there unknown)
    radius = WINDOW_M / np.maximum(np.abs(turn), 1e-9)
    sharp = radius < radius_m
    sign = np.where(turn >= 0, 1, -1)
    out, i, n = [], 0, len(s)
    while i < n:
        if not sharp[i]:
            i += 1
            continue
        j, way = i, sign[i]
        k = i + 1
        while k < n:                    # (the run goes on over short gaps and flickers of the other way)
            if sharp[k] and sign[k] == way:
                j = k
            elif s[k] - s[j] > JOIN_M:
                break
            k += 1
        run = slice(i, j + 1)
        deflection = float(np.degrees(np.sum(np.abs(turn[run]) * (sign[run] == way)) / WINDOW_M * STEP_M))
        if s[j] - s[i] + STEP_M >= MIN_RUN_M and deflection >= min_deflection:
            out.append((float(s[i]), float(s[j]), int(way), deflection, float(np.median(radius[run][sharp[run]]))))
        i = j + 1
    return out, length


def _row(group, q, travel, z, height=HEIGHT_M):
    """The Forest row of sign group at q (2,), its face to the traffic going in unit direction travel (2,): the models
    look down -Y with the post at their origin, so the row's X is that traffic's right."""
    fx, fy = float(travel[1]), float(-travel[0])
    return [guardrails.GROUPS.index(group), round(float(q[0]), 3), round(float(q[1]), 3), round(z, 3),
            round(math.degrees(math.atan2(fy, fx)), 2), 0.0, height, 0.0, fx, fy, 0.0, -fy, fx, 0.0, 0.0, 0.0, 1.0]


def signs(spec, xy):
    """[(group, point (2,), unit travel direction (2,))] along road xy (k, >=2) for the way of travel the spec allows."""
    found, length = bends(np.asarray(xy, np.float64)[:, :2], *bend_limits(spec.kmh))
    if not found:
        return []
    seg = np.hypot(*np.diff(xy[:, :2], axis=0).T)
    xy = np.asarray(xy, np.float64)[:, :2][np.r_[True, seg > 1e-6]]
    cum = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    adv = advance_m(spec.kmh)

    def where(d):
        k = min(max(int(np.searchsorted(cum, d, side="right")) - 1, 0), len(cum) - 2)
        t = (xy[k + 1] - xy[k]) / max(float(cum[k + 1] - cum[k]), 1e-9)
        return xy[k] + t * (d - cum[k]), t
    ways = (1.0, -1.0) if not spec.oneway else ((-1.0,) if int(spec.dir) < 0 else (1.0,))
    out = []
    for way in ways:
        last = -1e9
        for s0, s1, turning, deflection, radius in (found if way > 0 else found[::-1]):
            # (against the geometry the bend's end is where the driver comes to it, and it turns the other way)
            d = s0 - adv if way > 0 else s1 + adv
            if d < WINDOW_M or d > length - WINDOW_M:
                continue            # (no room before the road begins: its start is probably a junction)
            at_ = d if way > 0 else length - d      # (distance along the way of travel, for the gap)
            if at_ - last < MIN_GAP_M:
                continue
            p, t = where(d)
            t = t * way
            kind = "turn" if deflection >= TURN_DEG and radius < TURN_RADIUS_M else "curve"
            left = (turning > 0) == (way > 0)
            out.append((f"au_warn_{kind}_{'l' if left else 'r'}", p, t))
            last = at_
    return out


def tile_rows(frame, near_roads, box, elev, country, mapped=()):
    """Forest rows (guardrails.forest_rows' layout and GROUPS) of the warning signs of road tile box: near_roads
    build_tile's roads_out, elev(lat, lon) the ground, mapped the tile's barrier rows so far. [] outside Australia."""
    if (country or "").upper() not in COUNTRIES:
        return []
    x0, y0, x1, y1 = box
    rails = medians._mapped_segments(mapped)
    out = []
    for fid, spec, pts, *_ in near_roads:
        if not wanted(spec) or len(pts) < 2:
            continue
        others = [(p2[:, :2], s2.width / 2 + JUNCTION_CLEAR_M) for f2, s2, p2, *_ in near_roads
                  if f2 != fid and len(p2) >= 2]
        for group, p, t in signs(spec, np.asarray(pts, np.float64)):
            q = (p + np.array([-t[1], t[0]]) * (spec.width / 2 + OUT_M))[None, :]     # (the driver's left)
            if not (x0 <= q[0, 0] < x1 and y0 <= q[0, 1] < y1):
                continue
            if any(motesfri._distance(q, line)[0] <= reach for line, reach in others):
                continue
            if len(rails) and any(motesfri._distance(q, np.stack([a, b]))[0] <= RAIL_CLEAR_M
                                  for a, b in zip(rails[:, :2], rails[:, 2:])):
                continue
            la, lo, _ = frame.enu_to_geodetic(q[:, 0], q[:, 1], np.zeros(1))
            z = float(np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]).ravel()[0])
            out.append(_row(group, q[0], t, z))
    return out
