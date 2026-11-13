"""Sweden's signs at the places a road's own shape asks for them (blockworker._sign_rows, country SE only): what the map
shows reliably, none guessed from anything else. Rules: VGU 2024 (Trafikverket, TRVINFRA-00396, remiss version 0.9) and
TSFS 2019:74.

    F17 + T2    a lane ends: F17 just before the first M20 arrow (VGU K237642 has it 50 m before the lane is 2.5 m wide,
                which came too late in the game; that where the arrows don't fit) and F17 with its distance 400 m
                before the arrows, on both sides of the carriageway, 300 or 200 m if the two lanes aren't that long, not at all at a junction's
                short lane (MIN_RUN_M) unless the map says the lane merges away (turn:lanes merge_to_*) and no on-ramp
                begins it: an acceleration lane has none
    F18         where a road becomes physically separated (K237644)
    F2          300-400 m before a junction with a separate road for turning left, outside towns (TSFS 8 kap. 6-7 §§): a
                spansk sväng, or "vänstersvängskörfält typ Ögla" (K236534-8)
    C1          both sides of the end of an exit ramp from a motorway or mötesfri väg, and of such an ögla (K237582)
    B3          both sides of a painted crossing without signals, at 60 km/h at most (TSFS 3 kap. 10-11 §§)
    E1, E2      where a motorway begins and ends, and on the ramps on to and off it (TSFS 6 kap. 3-4 §§, K237587)
    A19         the moose, sparingly (the user: "very typical swedish thing but dont overdo it", 2026-11-03): no data
                says where they stand (OpenStreetMap has 1 within 150 km of Borås, Trafikverket's API has no signs),
                so on a rural two-lane main road at 70-100 km/h a piece of road (a map segment, between junctions) of
                L m gets a pair with chance L / MOOSE_SPACING_M, by a hash of its id: MOOSE_FROM_M into it, each
                facing the traffic entering it. Not on motorways, 2+1 roads or divided roads: those have game fences.

Spansk sväng: the data has no turn bans (Overture's prohibited_transitions are not downloaded), so it is told by shape:
a one-way link leaving a fast main road on the right that comes back across it, or ends beside it heading along it. Of
148 such links across Sweden 92% had OpenStreetMap's no_left_turn on the main road; 42% of plain right slip roads did.

Tile independence: every place is worked out from motesfri._ways, which are the same near a place whichever tile reads
them, and a tile keeps the rows in its box. Forest rows follow guardrails.forest_rows' layout.
"""
import hashlib
import math
from collections import namedtuple

import numpy as np
import dmath
import dnp

import guardrails
import motesfri
from motesfri import _carriageway, _length, _touches, _travel

COUNTRIES = {"SE"}
# Heights (mod trees.lua SE_SIGNS, tools/build_median_props.py): a row's height over its catalog's is its scale.
HEIGHTS = {"se_f17": 2.70, "se_f17_400": 2.78, "se_f17_300": 2.78, "se_f17_200": 2.78, "se_f18_11": 2.70,
           "se_f18_12": 2.70, "se_f18_21": 2.70, "se_f2": 3.50, "se_c1": 2.10, "se_b3": 2.60, "se_e1": 2.70,
           "se_e2": 2.70, "se_a19": 2.30}

MIN_RUN_M = 300.0           # m of two lanes that are an overtaking stretch and not a junction's lane
NEAR_M = 50.0               # F17 before the lane is 2.5 m wide where the arrows don't fit; and F18 before the barrier
STEP_M = 10.0               # m a sign that doesn't fit before the arrows moves on at a time
ARROWS_LEAD_M = 20.0        # F17 before the first M20 arrow: VGU's 50 m before the lane is 2.5 m wide (K237642) came too
                            # late in the game, the user found (2026-11-03)
ADVANCE_M = (400, 300, 200)     # T2 plates, whole 50s (K237642), the best that fits
SIDE_M = 3.0                # m past the road's edge of an F sign (TSFS 1 kap. 17 §: 3.0-6.0)
PLAIN_SIDE_M = 0.7          # m of the others (TSFS 1 kap. 13 §: 0.3-4.0): a ramp's, a street's
MEDIAN_M = 0.4              # m past a carriageway's left edge, in the median
CLEAR_M = 1.0               # m past another road's edge
PAIR_MIN_M = 300.0          # m a pair of carriageways runs to be a separated road, and not a junction's island
F2_M = 350.0                # m before the junction (300-400)
RURAL_KMH = 70.0
MAIN_CLASSES = {"trunk", "primary", "secondary"}
LINK_M = (20.0, 300.0)      # m long, a spansk sväng's link: the real ones are 30-163
NEAR_MAIN_M = 3.0           # m from the main road a link starts
SIDE_ROAD_M = (6.0, 45.0)   # m from it a form B or C link ends
TAIL_M = 12.0               # m of a link's end its heading is read over
LINK_BACK = 0.7             # forms' limits: the unit heading across the main road, towards it (A, C) ...
LINK_ACROSS = 0.4
LINK_ALONG = 0.8            # ... and along it (B)
RAMP_BACK_M = (5.0, 10.0, 15.0, 20.0, 30.0)  # m before a ramp's end its C1 stand: the first with room on both sides
RAMP_E_M = 20.0             # m from a ramp's end its E sign stand
MOTORWAY_E_M = 30.0         # ... and a motorway's
ZEBRA_KMH = 60.0
MOOSE_SPACING_M = 15000.0   # m of such road to a pair of A19s, on average
MOOSE_MIN_M = 1000.0        # m a piece of road is at least
MOOSE_FROM_M = 150.0        # m into it from a junction
MOOSE_SIDE_M = 1.5          # m past the road's edge (TSFS 1 kap. 13 §: 0.3-4.0)
MOOSE_CLASSES = {"trunk", "primary", "secondary", "tertiary"}
ZEBRA_M = 2.0               # m from a crossing's middle along the road to its B3 (M15 is 3-4 m long)
ONE_ZEBRA_M = 6.0
NEAR_NODE_M = 2.0           # secrossings.NODE_NEAR_M


def wanted(country, roads_near):
    """Can tile's ground carriageways roads_near have one of the signs that need the wider cells read?"""
    if (country or "").upper() not in COUNTRIES:
        return False
    return any(s.kind == "paved" and (s.cls == "motorway" or s.link or motesfri._base(s) or _carriageway(s) or _main(s))
               for _, s, *_ in roads_near)


def _row(group, p, facing, frame, elev):
    """The Forest row of sign group at p (2,) facing the traffic going in unit direction facing (2,): the models look
    down -Y with the pole at their origin, so the row's X is that traffic's right."""
    fx, fy = float(facing[1]), float(-facing[0])
    x, y = float(p[0]), float(p[1])
    la, lo, _ = frame.enu_to_geodetic(np.array([x]), np.array([y]), np.zeros(1))
    z = float(np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]).ravel()[0])
    return [guardrails.GROUPS.index(group), round(x, 3), round(y, 3), round(z, 3),
            round(math.degrees(dmath.atan2(fy, fx)), 2), 0.0, HEIGHTS[group], 0.0, fx, fy, 0.0, -fy, fx, 0.0, 0.0, 0.0, 1.0]


Sign = namedtuple("Sign", "group p facing")


def _right(t):
    return np.array([t[1], -t[0]])


def _forward(w):
    """w's points in its direction of travel (a one-way road's)."""
    xy = w.xy[:, :2]
    return xy if _travel(w) > 0 else xy[::-1]


def _point(xy, s):
    """(the point s m along polyline xy, the unit tangent there)."""
    seg = dnp.hypot(*np.diff(xy, axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    k = min(max(int(np.searchsorted(cum, s, side="right")) - 1, 0), len(seg) - 1)
    t = (xy[k + 1] - xy[k]) / max(float(seg[k]), 1e-9)
    return xy[k] + t * (s - cum[k]), t


def _behind(way, at, travel, d, ways):
    """(point, unit direction of travel, way) d m before arc length at on way for traffic going travel (+1 along its
    geometry), past joints where the road goes on straight; None where it stops."""
    seen = {id(way)}
    while True:
        xy = way.xy[:, :2] if travel > 0 else way.xy[::-1, :2]
        here = at if travel > 0 else _length(xy) - at
        if d <= here:
            p, t = _point(xy, here - d)
            return p, t, way
        d -= here
        start, heading = xy[0], _point(xy, 0.0)[1]
        for w in ways:
            if id(w) in seen or w.spec.link or w.spec.cls != way.spec.cls:
                continue
            for j, sense in ((0, -1), (-1, 1)):                 # its end at our start: drawn the way we go
                if _touches(start, w.xy[j, :2]) and (not w.spec.oneway or _travel(w) == sense):
                    o = w.xy[:, :2] if sense > 0 else w.xy[::-1, :2]
                    if float(dnp.dot(_point(o, _length(o) - 1e-6)[1], heading)) > motesfri.STRAIGHT_COS:
                        way, travel, at = w, sense, _length(o) if sense > 0 else 0.0
                        break
            else:
                continue
            seen.add(id(way))
            break
        else:
            return None


def _free(q, ways, own):
    """Is point q off every road but own (a set of ids of ways)?"""
    x, y = float(q[0]), float(q[1])
    for w in ways:
        if id(w) in own or len(w.xy) < 2:
            continue
        reach = float(w.spec.width) / 2 + CLEAR_M
        xy = w.xy[:, :2]
        if x < xy[:, 0].min() - reach or x > xy[:, 0].max() + reach or y < xy[:, 1].min() - reach \
                or y > xy[:, 1].max() + reach:
            continue
        if float(motesfri._distance(np.array([[x, y]]), xy)[0]) <= reach:
            return False
    return True


def _lanes_of(w):
    return motesfri._lanes(w.spec, w.tags) or (2 if w.spec.width >= 7.0 else 1)


def _wider(ways, unknown=False):
    """(way, 0.0, travel) of the one-way carriageways of three lanes or more: two lanes go on through them back from a
    lane end (the third an exit's, beside them: E4 at Härnösand, 62.6482,17.8979); unknown: and those with no lane
    count, for walking back to a sign's place only (Rv 27 at Borås leaves it off two pieces before a lane end)."""
    out = []
    for w in ways:
        if _carriageway(w.spec):
            lanes = motesfri._lanes(w.spec, w.tags)
            if (lanes or 0) > 2 or (unknown and lanes is None):
                out.append((w, 0.0, _travel(w)))
    return out


_merges = motesfri.merges
SPAN_MAX_M = 300.0          # m of a bridge or tunnel a sign's walk back from a lane end goes over


def _spans(ways):
    """Ways standing for the bridges and tunnels between two ground stretches of one carriageway (motesfri._ways leaves
    them out: the walk back to a sign's place stopped there, at Rv 27 and Rv 40 at Borås): a straight line, the spec of
    the stretch before. Only walked over, never a sign's place."""
    by = {}
    for w in ways:
        feat, _, k = w.fid.rpartition(":")
        if k.isdigit() and _carriageway(w.spec):
            by.setdefault(feat, {})[int(k)] = w
    out = []
    for feat, parts in by.items():
        for k, a in parts.items():
            b = parts.get(k + 1)
            if b is None or len(a.xy) < 2 or len(b.xy) < 2:
                continue
            gap = float(dnp.hypot(*(b.xy[0, :2] - a.xy[-1, :2])))
            if motesfri.JOIN_M < gap <= SPAN_MAX_M:
                out.append(motesfri.Way(f"{feat}:{k}+", a.spec, a.tags, np.vstack([a.xy[-1, :2], b.xy[0, :2]]), a.key))
    return out


def _ramp_made(r, run, ways):
    """Does the two lanes' stretch back from r begin where a link road joins (an on-ramp's acceleration lane)?"""
    here, seen = r, {id(r[0])}
    while True:
        back = next((q for q in run if id(q[0]) not in seen and motesfri._touches(motesfri._start(here), motesfri._end(q))),
                    None)
        if back is None:
            break
        seen.add(id(back[0]))
        here = back
    s = motesfri._start(here)
    return any(x.spec.link and x.spec.oneway and motesfri._touches(s, _forward(x)[-1]) for x in ways)


def _lane_signs(drop, ways):
    """F17 and F17 + T2 on both sides, before a lane end motesfri.drops found: on MIN_RUN_M of two lanes or more, or a
    shorter stretch the map says merges away (turn:lanes merge_to_*) if no on-ramp begins it."""
    run = (drop.way, drop.offset, drop.travel)
    if motesfri._chain_length(run, list(drop.run) + _wider(ways)) < MIN_RUN_M and (
            not _merges(drop.way) or _ramp_made(run, list(drop.run) + _wider(ways), ways)):
        return []
    spans = _spans(ways)
    drop = drop._replace(run=list(drop.run) + _wider(ways, unknown=True) + [(w, 0.0, _travel(w)) for w in spans])
    spans = {id(w) for w in spans}
    out = []

    def both(group, d):
        got = motesfri.back_along(drop, d, 0.0)
        if got is None:
            return False
        p, t, w, sense = got
        if id(w) in spans:
            return False                                # (on a bridge: the next place back, or a nearer plate)
        right = _right(t)
        left = motesfri.centre_offset(w) * sense if not w.spec.oneway else w.spec.width / 2 + MEDIAN_M
        spots = [p + right * (w.spec.width / 2 + SIDE_M), p - right * left]
        own = {id(w)} | {id(q[0]) for q in drop.run if any(motesfri._touches(w.xy[k, :2], q[0].xy[j, :2])
                                                           for k in (0, -1) for j in (0, -1))}
        if not _free(spots[0], ways, own):            # (own: the two lanes' ways either side of a joint, a wider one's)
            return False
        out.append(Sign(group, spots[0], t))
        if _free(spots[1], ways, own):
            out.append(Sign(group, spots[1], t))
        elif w.spec.oneway:
            q = _in_median(p, t, w, ways, own)
            if q is not None:
                out.append(Sign(group, q, t))
        return True
    gaps = motesfri.M20_GAPS_90 if (drop.way.spec.kmh or 0) >= 90.0 else motesfri.M20_GAPS_80
    arrows = drop.lane_end + motesfri.M20_FIRST_M + sum(gaps)      # (motesfri.lane_drops' last arrow, the first seen)
    # before the arrows; where the road can't be followed that far back (a bridge, a junction), as far back as it can,
    # and VGU's place at the nearest
    least = drop.lane_end + NEAR_M
    tries = [arrows + ARROWS_LEAD_M] + [float(d) for d in np.arange(arrows + ARROWS_LEAD_M - STEP_M, least, -STEP_M)]
    near = next((d for d in tries + [least] if both("se_f17", d)), None)
    if near is None:
        return out
    for m in ADVANCE_M:
        if both(f"se_f17_{m}", max(near - ARROWS_LEAD_M, drop.lane_end) + m):
            break
    return out


def _in_median(p, t, w, ways, own):
    """A one-way carriageway's median sign at p (on w, going t): halfway to the oncoming carriageway where the two are
    drawn so close that the median is the barrier's line (a 2+1 road's two one-way ways, often 7-8 m apart), or None."""
    right = _right(t)
    best = None
    for x in ways:
        if x is w or id(x) in own or not _carriageway(x.spec) or len(x.xy) < 2:
            continue
        d = float(motesfri._distance(p[None], x.xy[:, :2])[0])
        if d > w.spec.width / 2 + MEDIAN_M + x.spec.width / 2 + CLEAR_M or (best is not None and d >= best[0]):
            continue
        at, _, _ = motesfri._project(p[None], x.xy[:, :2])
        _, u = _point(x.xy[:, :2], float(at[0]))
        if float(dnp.dot(u, t)) * _travel(x) > -motesfri.STRAIGHT_COS:
            continue                                    # (not the oncoming traffic's)
        best = (d, x)
    if best is None:
        return None
    q = p - right * (best[0] / 2)
    return q if _free(q, ways, own | {id(best[1])}) else None


def _carriageway_run(w, ways, far=PAIR_MIN_M):
    """m carriageway w goes on for along its travel, as far as it matters."""
    total, here, seen = 0.0, w, {id(w)}
    while here is not None and total < far:
        total += _length(here.xy)
        e = _forward(here)[-1]
        here = next((x for x in ways if id(x) not in seen and _carriageway(x.spec) and _touches(e, _forward(x)[0])), None)
        if here is not None:
            seen.add(id(here))
    return total


def _f18(mine, oncoming):
    return {(2, 1): "se_f18_12", (1, 2): "se_f18_21", (1, 1): "se_f18_11"}.get((mine, oncoming))


def _f18_on(way, k, group, ways):
    """F18 of group on way, for traffic going to its end k."""
    length = _length(way.xy)
    got = _behind(way, length if k else 0.0, 1 if k else -1, NEAR_M, ways)
    if got is None:
        return []
    p, t, w = got
    q = p + _right(t) * (w.spec.width / 2 + SIDE_M)
    return [Sign(group, q, t)] if _free(q, ways, {id(w)}) else []


def separations(ways):
    """F18 where a road becomes physically separated: a 2+1 section out of a plain road, a road forking into a pair."""
    out = []
    pieces = motesfri.sections(ways)
    for p, ends in zip(pieces, motesfri.end_offsets(pieces, ways)):
        over = motesfri._overtaking(p.way) if p.section else None
        if over is None:
            continue
        for k, value in zip((0, -1), ends):
            if not isinstance(value, motesfri.Whole):
                continue
            e = p.way.xy[k, :2]
            on = next(w for w in ways if w is not p.way and (_touches(e, w.xy[0, :2]) or _touches(e, w.xy[-1, :2])))
            out += _f18_on(on, -1 if _touches(e, on.xy[-1, :2]) else 0,
                           "se_f18_12" if over[1] == (1 if k == 0 else -1) else "se_f18_21", ways)
    pair = [w for w in ways if _carriageway(w.spec)]
    for w in ways:
        if not motesfri._base(w.spec):
            continue
        for k in (0, -1):
            e = w.xy[k, :2]
            leaving = [c for c in pair if _touches(e, _forward(c)[0])]
            coming = [c for c in pair if _touches(e, _forward(c)[-1])]
            if len(leaving) != 1 or len(coming) != 1:
                continue
            if any(x is not w and x not in (leaving[0], coming[0]) and (_touches(e, x.xy[0, :2])
                                                                        or _touches(e, x.xy[-1, :2])) for x in ways):
                continue                                    # (a junction: it has signs of its own)
            group = _f18(_lanes_of(leaving[0]), _lanes_of(coming[0]))
            if group and _carriageway_run(leaving[0], ways) >= PAIR_MIN_M:
                out += _f18_on(w, k, group, ways)
    return out


def _main(spec):
    return (spec is not None and spec.cls in MAIN_CLASSES and spec.kind == "paved" and not spec.link and not spec.urban
            and spec.kmh is not None and spec.kmh >= RURAL_KMH)


def _link(w):
    """Could one-way w be a turning road? (no service road: the many that aren't one are half of the false ones)"""
    s = w.spec
    return (bool(s.oneway) and s.kind == "paved" and s.cls != "service" and not _main(s)
            and (s.link or s.cls in ("unclassified", "tertiary")) and LINK_M[0] <= _length(w.xy) <= LINK_M[1])


def spanish(ways):
    """[(link, main way, arc length on it of the link's start, travel on it +1 / -1)] of ways' spansk svängar."""
    mains = [w for w in ways if _main(w.spec)]
    if not mains:
        return []
    a = np.vstack([w.xy[:-1, :2] for w in mains])
    b = np.vstack([w.xy[1:, :2] for w in mains])
    own = np.repeat(np.arange(len(mains)), [len(w.xy) - 1 for w in mains])
    ab = b - a
    l2 = np.maximum((ab * ab).sum(1), 1e-12)

    def nearest(q):
        t = np.clip(((q - a) * ab).sum(1) / l2, 0.0, 1.0)
        d = dnp.hypot(*(a + t[:, None] * ab - q).T)
        k = int(d.argmin())
        return float(d[k]), k, float(t[k])
    out = []
    for w in ways:
        if not _link(w):
            continue
        xy = _forward(w)
        off, k, t = nearest(xy[0])
        if off > NEAR_MAIN_M:
            continue
        main = mains[int(own[k])]
        first = int(np.flatnonzero(own == own[k])[0])
        at = float(dnp.hypot(*ab[first:k].T).sum() + t * math.sqrt(l2[k]))
        u = ab[k] / math.sqrt(l2[k])
        left = np.array([-u[1], u[0]])
        mid = _point(xy, _length(xy) / 2)[0]
        travel = 1
        if float(dnp.dot(mid - a[k], left)) > 0:             # it leaves to the left of the geometry: the other way's road
            if main.spec.oneway:
                continue
            travel, u, left = -1, -u, -left
        elif main.spec.oneway:
            travel = _travel(main)
            if travel < 0:
                continue                                    # (on its right against the traffic)
        h = xy[-1] - _point(xy, max(_length(xy) - TAIL_M, 0.0))[0]
        h = h / max(float(dnp.hypot(*h)), 1e-9)
        back, along = float(dnp.dot(h, left)), float(dnp.dot(h, u))
        end = nearest(xy[-1])[0]
        if (end < NEAR_MAIN_M and back > LINK_BACK) or (SIDE_ROAD_M[0] < end < SIDE_ROAD_M[1]
                                                        and (back >= LINK_ACROSS or (abs(back) < LINK_ACROSS
                                                                                     and along > LINK_ALONG))):
            out.append((w, main, at, travel))
    return out


def _both(group, w, s, facing, ways, side=PLAIN_SIDE_M):
    """Signs of group either side of w at s m along its travel, facing unit direction facing or that of its travel."""
    p, t = _point(_forward(w), s)
    q = [p + _right(t) * (w.spec.width / 2 + side), p - _right(t) * (w.spec.width / 2 + side)]
    return [Sign(group, x, t * facing) for x in q if _free(x, ways, {id(w)})]


def _c1(w, ways):
    """C1 on both sides of the end of one-way w, facing whoever would drive in there."""
    length = _length(w.xy)
    for back in RAMP_BACK_M:                                # (at an angle to the road it meets, it can need room)
        if back < length:
            got = _both("se_c1", w, length - back, -1.0, ways)
            if len(got) == 2:
                return got
    return []


def turning_signs(found, ways):
    """F2 before a spansk sväng (spanish's list), and C1 where its link meets a two-way road."""
    out, done = [], set()
    for link, main, at, travel in found:
        got = _behind(main, at, travel, F2_M, ways)
        if got is not None:
            p, t, w = got
            q = p + _right(t) * (w.spec.width / 2 + SIDE_M)
            key = (round(float(q[0]) / 60.0), round(float(q[1]) / 60.0))
            if key not in done and _free(q, ways, {id(w), id(link)}):
                done.add(key)
                out.append(Sign("se_f2", q, t))
        e = _forward(link)[-1]
        if any(x is not link and not x.spec.oneway and float(motesfri._distance(e[None], x.xy[:, :2])[0]) < motesfri.JOIN_M
               for x in ways):
            out += _c1(link, ways)
    return out


def _is_link(w):
    return bool(w.spec.oneway) and bool(w.spec.link) and w.spec.kind == "paved"


def _chain(w, links, forward):
    """[way] of the link roads that lead back from w (forward: on from it) to where the links stop, w's own included."""
    out, todo, seen = [], [w], {id(w)}
    while todo:
        here = todo.pop()
        xy = _forward(here)
        end = xy[-1] if forward else xy[0]
        more = [x for x in links if id(x) not in seen and _touches(end, _forward(x)[0 if forward else -1])]
        seen.update(id(x) for x in more)
        todo += more
        if not more:
            out.append(here)
    return out


class _Lines:
    """Roads' segments, to ask of many points whether they lie on one (a city's tile asks hundreds, of thousands)."""

    def __init__(self, roads):
        roads = [w for w in roads if len(w.xy) >= 2]
        self.a = np.vstack([w.xy[:-1, :2] for w in roads]) if roads else np.zeros((0, 2))
        self.b = np.vstack([w.xy[1:, :2] for w in roads]) if roads else np.zeros((0, 2))
        self.d = self.b - self.a
        self.l2 = np.maximum((self.d * self.d).sum(1), 1e-12)

    def on(self, p):
        if not len(self.a):
            return False
        t = np.clip(((p - self.a) * self.d).sum(1) / self.l2, 0.0, 1.0)
        return float(dnp.hypot(*(self.a + t[:, None] * self.d - p).T).min()) < motesfri.JOIN_M


def ramp_signs(ways, skip=()):
    """C1 at the ends of the exit ramps from a motorway or mötesfri väg, and E1 / E2 on the ramps on to and off a motorway
    and where one begins and ends. skip: ways with their C1 already."""
    motorway = [w for w in ways if w.spec.cls == "motorway" and not w.spec.link and w.spec.kind == "paved"]
    links = [w for w in ways if _is_link(w)]
    done = {id(x) for x in skip}
    fast = motorway + [w for w in ways if _carriageway(w.spec)] + [p.way for p in motesfri.sections(ways)]
    roads = [w for w in ways if not w.spec.link and w.spec.kind == "paved" and w.spec.cls in motesfri.OPEN_CLASSES]
    on_motorway, on_fast = _Lines(motorway), _Lines(fast)
    on_two_way = _Lines([r for r in roads if not r.spec.oneway])
    on_plain = _Lines([w for w in roads if w.spec.cls != "motorway"])
    out = []
    for w in links:
        xy = _forward(w)
        roots = _chain(w, links, False)
        if id(w) not in done and on_two_way.on(xy[-1]) and any(on_fast.on(_forward(r)[0]) for r in roots):
            out += _c1(w, ways)                             # (off a fast road, and meeting oncoming traffic here)
        if not motorway:
            continue
        if on_motorway.on(xy[-1]):                          # an on-ramp: at its first ways, off another road
            for r in roots:
                if not on_motorway.on(_forward(r)[0]) and _length(r.xy) >= 2 * RAMP_E_M:
                    out += _both("se_e1", r, RAMP_E_M, 1.0, ways)
        if on_motorway.on(xy[0]):                           # an off-ramp: at its last ways, on to another road
            for r in _chain(w, links, True):
                length = _length(r.xy)
                if not on_motorway.on(_forward(r)[-1]) and length >= 2 * RAMP_E_M:
                    p, t = _point(_forward(r), length - RAMP_E_M)
                    q = p + _right(t) * (r.spec.width / 2 + PLAIN_SIDE_M)
                    if _free(q, ways, {id(r)}):
                        out.append(Sign("se_e2", q, t))
    for w in motorway:
        xy = _forward(w)
        if len(xy) < 2 or _length(xy) < 2 * MOTORWAY_E_M:
            continue
        before = [x for x in motorway if x is not w and _touches(xy[0], _forward(x)[-1])]
        after = [x for x in motorway if x is not w and _touches(xy[-1], _forward(x)[0])]
        if not before and on_plain.on(xy[0]):
            out += _both("se_e1", w, MOTORWAY_E_M, 1.0, ways, SIDE_M)
        if not after and on_plain.on(xy[-1]):
            out += _both("se_e2", w, _length(xy) - MOTORWAY_E_M, 1.0, ways, SIDE_M)
    return out


def zebra_signs(near_roads, zebras):
    """B3 before the crossings zebras [(x, y)] on near_roads (build_tile's roads_out): one each side of the road, each on
    the right just before the crossing for its own direction; on a one-way road both for its one direction. The sign
    faces both ways, so the two read for both directions on a two-way road."""
    out, taken = [], []
    lines = [(fid, s, np.asarray(pts, np.float64)[:, :2]) for fid, s, pts, *_ in near_roads
             if s.kind in ("paved", "cobble") and s.cls != "motorway" and len(pts) >= 2 and _limit(s) <= ZEBRA_KMH]
    for x, y in zebras:
        q = np.array([[x, y]], np.float64)
        best = None
        for fid, s, xy in lines:
            d = float(motesfri._distance(q, xy)[0])
            if d <= NEAR_NODE_M and (best is None or d < best[0]):
                best = (d, fid, s, xy)
        if best is None:
            continue
        _, fid, s, xy = best
        at, _, _ = motesfri._project(q, xy)
        c, t = _point(xy, float(at[0]))
        if any(f == fid and float(dnp.hypot(*(c - e))) < ONE_ZEBRA_M for f, e in taken):
            continue
        taken.append((fid, c))
        reach = s.width / 2 + PLAIN_SIDE_M
        if s.oneway:
            go = t if _sense(s) > 0 else -t
            out.append(Sign("se_b3", c - go * ZEBRA_M + _right(go) * reach, go))
            out.append(Sign("se_b3", c - go * ZEBRA_M - _right(go) * reach, go))
        else:
            for go in (t, -t):
                out.append(Sign("se_b3", c - go * ZEBRA_M + _right(go) * reach, go))
    return out


def _limit(spec):
    """km/h, the law's default where the map has none (trafikförordningen 3 kap. 17 §)."""
    return spec.kmh if spec.kmh is not None else 50.0 if spec.urban else 70.0


def _sense(spec):
    return -1 if (spec.dir or 1) < 0 else 1


def all_signs(ways):
    """Sign of every place in ways, in no tile's order: the same wherever it is read."""
    found = spanish(ways)
    out = turning_signs(found, ways) + ramp_signs(ways, [f[0] for f in found]) + separations(ways)
    for drop in motesfri.drops(ways):
        out += _lane_signs(drop, ways)
    return out


def _moose_road(s):
    return (s.kind == "paved" and not s.urban and not s.link and not s.oneway and s.cls in MOOSE_CLASSES
            and 70.0 <= _limit(s) <= 100.0 and (s.lanes or 2) <= 2)


def _chance(fid):
    """A number in [0, 1) of a road's id, the same in every tile and run."""
    return int(hashlib.sha1(str(fid).encode("utf-8")).hexdigest()[:8], 16) / 2.0 ** 32


def moose_signs(near_roads):
    """A19 on near_roads (build_tile's roads_out: whole ground pieces, the same in every tile), see the header."""
    lines = [motesfri.Way(fid, s, {}, np.asarray(pts, np.float64)[:, :2], fid) for fid, s, pts, *_ in near_roads
             if len(pts) >= 2]
    out = []
    for w in lines:
        if not _moose_road(w.spec):
            continue
        length = _length(w.xy)
        if length < MOOSE_MIN_M or _chance(w.fid) >= length / MOOSE_SPACING_M:
            continue
        for xy in (w.xy, w.xy[::-1]):
            p, t = _point(xy, MOOSE_FROM_M)
            q = p + _right(t) * (float(w.spec.width) / 2 + MOOSE_SIDE_M)
            if _free(q, lines, {id(w)}):
                out.append(Sign("se_a19", q, t))
    return out


def tile_rows(frame, ways, near_roads, nodes, box, elev, country):
    """Forest rows of road tile box: ways motesfri._ways' (None: only the zebras' B3, which need no wider cells),
    near_roads build_tile's roads_out, nodes secrossings.nodes', elev(lat, lon) the ground."""
    if (country or "").upper() not in COUNTRIES:
        return []
    signs = all_signs(ways) if ways else []
    signs = signs + zebra_signs(near_roads, (nodes or {}).get("zebras", ())) + moose_signs(near_roads)
    x0, y0, x1, y1 = box
    return [_row(s.group, s.p, s.facing, frame, elev) for s in signs if x0 <= s.p[0] < x1 and y0 <= s.p[1] < y1]
