"""Racetrack types from OpenStreetMap's raceway ways (racetracks branch, 2026-11-03): drag strips and speedway ovals,
one rule from the map's own geometry and tags, no list of tracks.

A highway=raceway way is
  "drag"  a drag strip: a dead straight way DRAG_MIN_M..DRAG_MAX_M long (DRAG_STRAIGHT_M off its chord at most) whose
          ends meet no other raceway (a circuit's straight is joined to its turns: Talladega's 954 m Superstretch),
          or one of any length named like one (dragway, drag strip). Bandimere Speedway (39.664 N 105.188 W) is one
          straight 1100 m way, sport=motor, no width: drawn 12 m with round ends before.
  "oval"  a speedway's piece: DRAG_MIN_M or longer with no bend tighter than OVAL_MIN_RADIUS_M (an oval's turns are
          250-400 m radius; a road course's corners 15-100 m). Talladega's four pieces.
  None    anything else (a road course, a kart track, a pit lane).

Widths (road_width): a mapped width wins; else a drag strip DRAG_STRIP_M (two 12 ft lanes, the guard-wall margins
and the centre: 18-24 m of pavement on the satellite photos), a speedway OVAL_M (Talladega 15-20 m, Daytona 12-18 m,
Indianapolis 15 m). The paved areas beside them (pads): a drag strip's launch / burnout apron behind its start line
(DRAG_APRON_M long, DRAG_APRON_W_M wide) and its ends squared (the painted ground drew a round cap half the width past
each end); a speedway's paved apron OVAL_APRON_M inside its inner edge (left of the driving direction on a one-way
piece: ovals run anticlockwise; else the side its bend turns to). Pure."""
import math
import re

import numpy as np

DRAG_MIN_M = 250.0          # m: an eighth mile is 201 m, plus its shutdown
DRAG_MAX_M = 1800.0         # m: a quarter mile (402 m) and a long shutdown, the longest strips ~1.3 km
DRAG_STRAIGHT_M = 3.0       # m a drag strip's vertices lie off its end-to-end chord at most
DRAG_END_M = 8.0            # m: another raceway's vertex this near an end joins it (a circuit's straight)
DRAG_NAME = re.compile(r"drag\s*(way|strip|race|racing)?|\bstrip\b|quarter[\s-]*mile", re.I)
DRAG_STRIP_M = 20.0         # m of paved width of a drag strip without a mapped width
DRAG_APRON_M = 40.0         # m of launch / burnout apron behind the start line ...
DRAG_APRON_W_M = 30.0       # ... this wide (the burnout box and the water box; staging lanes lead into it)
OVAL_MIN_RADIUS_M = 180.0   # m: a speedway piece bends no tighter than this
OVAL_RADIUS_SPAN_M = 100.0  # m along the way the bend radius is taken over
OVAL_M = 18.0               # m of racing surface of a speedway without a mapped width
OVAL_APRON_M = 12.0         # m of paved apron inside a speedway's inner edge


def _cross(a, b):
    """The z of a x b for 2-D vectors (..., 2)."""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def _xy(lonlat):
    ll = np.asarray(lonlat, np.float64)[:, :2]
    kx = 111320.0 * math.cos(math.radians(float(ll[:, 1].mean())))
    return np.c_[(ll[:, 0] - ll[0, 0]) * kx, (ll[:, 1] - ll[0, 1]) * 110540.0], kx


def _length(xy):
    return float(np.hypot(*np.diff(xy, axis=0).T).sum()) if len(xy) >= 2 else 0.0


def straight(lonlat):
    """Whether a lon/lat polyline is a dead straight line (DRAG_STRAIGHT_M off its chord at most) of drag strip
    length (DRAG_MIN_M..DRAG_MAX_M)."""
    xy, _ = _xy(lonlat)
    if len(xy) < 2:
        return False
    d = xy[-1] - xy[0]
    L = float(np.hypot(*d))
    if not DRAG_MIN_M <= L <= DRAG_MAX_M:
        return False
    off = np.abs(_cross(d / L, xy - xy[0]))
    return float(off.max()) <= DRAG_STRAIGHT_M


def min_radius(lonlat):
    """The tightest bend radius (m) of a lon/lat polyline over OVAL_RADIUS_SPAN_M of it (inf when straight)."""
    xy, _ = _xy(lonlat)
    L = _length(xy)
    if L < 2 * OVAL_RADIUS_SPAN_M:
        return math.inf
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    t = np.arange(0.0, L, 5.0)
    p = np.c_[np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])]
    k = max(1, int(round(OVAL_RADIUS_SPAN_M / 2 / 5.0)))
    if len(p) < 2 * k + 1:
        return math.inf
    a, b = p[2 * k:] - p[k:-k], p[k:-k] - p[:-2 * k]
    turn = np.abs(np.arctan2(_cross(b, a),(a * b).sum(1)))
    turn = np.maximum(turn, 1e-9)
    return float((k * 5.0 / turn).min())        # (the two chords' directions k x 5 m of arc apart)


def kind(way, ends_joined):
    """A raceway way's type ("drag", "oval" or None; see the module). way: {"coords", "name", "width"...}; ends_joined:
    whether another raceway meets either end (DRAG_END_M)."""
    c = way.get("coords")
    if c is None or len(c) < 2:
        return None
    name = str(way.get("name") or "")
    if re.search(r"\bpits?\b", name, re.I):
        return None
    if straight(c) and (not ends_joined or DRAG_NAME.search(name)):
        return "drag"
    xy, _ = _xy(c)
    if _length(xy) >= DRAG_MIN_M and min_radius(c) >= OVAL_MIN_RADIUS_M:
        return "oval"
    return None


OVAL_NETWORK_ON = True        # an oval only on a one-way main loop (speedway)
OVAL_NETWORK_MAX = 400        # ways looked at at most per network


def _end_pts(way):
    c = np.asarray(way["coords"], np.float64)
    return c[0], c[-1]


OVAL_CORNER_R_M = 600.0       # m: a loop's stretch turning tighter than this (over OVAL_CORNER_SPAN_M) is a corner
OVAL_CORNER_SPAN_M = 40.0     # m the turning is read over
OVAL_STRAIGHT_R_M = 1500.0    # m: a stretch turning the other way this tightly or more is a switch (a road course's)
OVAL_CORNER_GAP_M = 60.0     # m: corner stretches this close are one corner
OVAL_CORNER_MIN_DEG = 60.0   # deg a corner turns at least (a tri-oval's dogleg is none)
OVAL_CORNERS = (2, 4)         # a speedway's corners: an oval's 2, a tri-oval's / Pocono's 3, a paperclip's 2, 4 at most
RACE_LOOP_TURN_DEG = 60.0    # deg the line turns at a join at most (a straighter way on than a turn-off)
OVAL_SHORT_M = 1600.0         # m: a speedway loop shorter than this is a short track ("short_oval": speedway)
OVAL_LOOP_MIN_M = 780.0       # m: a short oval's loop at least (Martinsville 847 m; a half-mile bullring; a kart track or a skid pad is no speedway)


def _ll_to_m(c, lat0):
    c = np.asarray(c, np.float64)
    kx = 111320.0 * math.cos(math.radians(lat0))
    return np.c_[c[:, 0] * kx, c[:, 1] * 110540.0]


def main_loop(way, ways, lookup=None):
    """The shortest closed loop of raceway pieces (no pit lanes) through way, joined at their ends (an end within
    DRAG_END_M of another's end or vertex; at a vertex the loop goes on along it from there), as one lon/lat polyline in
    the way's own direction, or None. Walked across cells through lookup (end_boxes). Pure given lookup."""
    c0 = np.asarray(way.get("coords"), np.float64)
    if len(c0) < 2:
        return None
    lat0 = float(c0[0, 1])
    pool = {int(w["id"]): w for w in ways if w.get("coords") is not None and len(w["coords"]) >= 2}
    seen_box = set()

    def grab(e):
        if lookup is None:
            return
        k = (round(float(e[0]), 3), round(float(e[1]), 3))
        if k in seen_box:
            return
        seen_box.add(k)
        pad = 0.0003
        try:
            for o in lookup((float(e[0]) - pad, float(e[1]) - pad, float(e[0]) + pad, float(e[1]) + pad)):
                if o.get("coords") is not None and len(o["coords"]) >= 2:
                    pool.setdefault(int(o["id"]), o)
        except Exception:
            pass

    def pit(o):
        return bool(re.search(r"\bpits?\b", str(o.get("name") or ""), re.I))

    def onward(e, exclude):
        """[(way, its coords from the join at e onward)] the pieces going on from point e."""
        grab(e)
        p = _ll_to_m([e], lat0)[0]
        out = []
        for o in pool.values():
            if int(o["id"]) in exclude or pit(o):
                continue
            q = np.asarray(o["coords"], np.float64)
            d = np.hypot(*(_ll_to_m(q, lat0) - p).T)
            i = int(d.argmin())
            if d[i] > DRAG_END_M:
                continue
            if i == 0:
                out.append((o, q))
            elif i == len(q) - 1:
                out.append((o, q[::-1]))
            else:                       # (at its vertex: both ways along it)
                out.append((o, q[i:]))
                out.append((o, q[i::-1]))
        return out

    # (the straightest way on at every join, as a car on the line goes: an oval's infield exits turn off it)
    gp = _ll_to_m([c0[0]], lat0)[0]
    if float(np.hypot(*(_ll_to_m([c0[-1]], lat0)[0] - gp))) <= DRAG_END_M:
        return c0                       # (a closed way: its own loop, Bristol's)
    path, cur, used, total = [c0], c0, {int(way["id"])}, 0.0
    for _ in range(OVAL_NETWORK_MAX):
        cm = _ll_to_m(cur, lat0)
        tail = cm[-1] - cm[max(0, len(cm) - 4)]
        best = None
        for o, q in onward(cur[-1], used if len(path) < 3 else used - {int(way["id"])}):
            if len(q) < 2:
                continue
            qm = _ll_to_m(q, lat0)
            j_ = min(3, len(qm) - 1)
            hd = qm[j_] - qm[0]
            if float(np.hypot(*hd)) < 1e-6 or float(np.hypot(*tail)) < 1e-6:
                continue
            cos = float(hd @ tail) / float(np.hypot(*hd) * np.hypot(*tail))
            if best is None or cos > best[0]:
                best = (cos, o, q)
        if best is None or best[0] < math.cos(math.radians(RACE_LOOP_TURN_DEG)):
            return None
        _cos, o, q = best
        qm = _ll_to_m(q, lat0)
        dg = np.hypot(*(qm - gp).T)
        if dg[1:].min() <= DRAG_END_M:              # (back at the way's start: closed)
            last = q[:int(np.argmin(dg[1:])) + 2]
            # (... and the line goes on into way there: a pit lane's loop round the oval turns off it at the pit
            # entry, Talladega's)
            lm = _ll_to_m(last, lat0)
            tail_ = lm[-1] - lm[max(0, len(lm) - 4)]
            pick = None
            for o2, q2 in onward(c0[0], {int(o["id"])}):
                q2m = _ll_to_m(q2, lat0)
                hd = q2m[min(3, len(q2m) - 1)] - q2m[0]
                if float(np.hypot(*hd)) < 1e-6 or float(np.hypot(*tail_)) < 1e-6:
                    continue
                cs = float(hd @ tail_) / float(np.hypot(*hd) * np.hypot(*tail_))
                if pick is None or cs > pick[0]:
                    pick = (cs, int(o2["id"]))
            if pick is not None and pick[1] != int(way["id"]):
                return None
            path.append(last)
            return np.concatenate(path)
        path.append(q)
        used.add(int(o["id"]))
        total += float(np.hypot(*np.diff(qm, axis=0).T).sum())
        if total > 30000.0:
            return None
        cur = q
    return None


def one_way_loop(loop):
    """Whether a closed lon/lat loop is a speedway's: it turns one way only (no stretch turning the other way tighter
    than OVAL_STRAIGHT_R_M), once round (its turning sums to 360 deg), in OVAL_CORNERS corners (stretches tighter than
    OVAL_CORNER_R_M), OVAL_LOOP_MIN_M long or more. Pure."""
    xy = _ll_to_m(loop, float(np.asarray(loop)[0, 1]))
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    L = float(s[-1])
    if L < OVAL_LOOP_MIN_M:
        return False
    t = np.arange(0.0, L, 5.0)
    p = np.c_[np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])]
    d = np.diff(np.vstack([p, p[:1]]), axis=0)
    h = np.arctan2(d[:, 1], d[:, 0])
    dh = np.angle(np.exp(1j * (np.roll(h, -1) - h)))          # (per 5 m, round the loop)
    total = float(dh.sum())
    if abs(abs(total) - 2 * math.pi) > 0.6:
        return False
    k = max(1, int(round(OVAL_CORNER_SPAN_M / 5.0)))
    rate = np.convolve(np.r_[dh[-k:], dh, dh[:k]], np.ones(2 * k + 1) / ((2 * k + 1) * 5.0), "valid")
    rate = rate * (1.0 if total > 0 else -1.0)                 # (+: the loop's own way)
    if float(rate.min()) < -1.0 / OVAL_STRAIGHT_R_M:
        return False
    corner = rate > 1.0 / OVAL_CORNER_R_M
    if corner.all() or not corner.any():
        return False
    rot = int(np.argmin(corner))                               # (runs counted from a straight)
    c, dd = np.roll(corner, -rot), np.roll(dh, -rot) * (1.0 if total > 0 else -1.0)
    runs, i = [], 0
    while i < len(c):
        if c[i]:
            j = i
            while j < len(c) and c[j]:
                j += 1
            if runs and (i - runs[-1][1]) * 5.0 < OVAL_CORNER_GAP_M:
                runs[-1][1] = j                                # (one corner, its rate dipping mid-way)
            else:
                runs.append([i, j])
            i = j
        else:
            i += 1
    n = sum(1 for a_, b_ in runs if float(dd[a_:b_].sum()) >= math.radians(OVAL_CORNER_MIN_DEG))
    return OVAL_CORNERS[0] <= n <= OVAL_CORNERS[1]


def speedway(way, ways, lookup=None):
    """way's speedway type: "oval" on a one-way main loop (main_loop, one_way_loop) OVAL_SHORT_M round or more,
    "short_oval" on a shorter one (a short track: Bristol's 858 m, Martinsville's 847 m; its turns 60-80 m radius,
    graded and drawn as a road course is, 30 m smoothing and the class's width: the 45 m smoothing and 18 m default
    width of a superspeedway rode its banking rougher, 2026-11-07), None for none."""
    lp = main_loop(way, ways, lookup)
    if lp is None or not one_way_loop(lp):
        return None
    xy = _ll_to_m(lp, float(np.asarray(lp)[0, 1]))
    return "oval" if _length(xy) >= OVAL_SHORT_M else "short_oval"


def ends_joined(way, others):
    """Whether another raceway (others: ways with "id" and "coords") has a vertex within DRAG_END_M of either end of
    way."""
    c = np.asarray(way["coords"], np.float64)
    kx = 111320.0 * math.cos(math.radians(float(c[0, 1])))
    for end in (c[0], c[-1]):
        for o in others:
            if o is way or int(o["id"]) == int(way["id"]):
                continue
            q = np.asarray(o["coords"], np.float64)
            d = np.hypot((q[:, 0] - end[0]) * kx, (q[:, 1] - end[1]) * 110540.0)
            if float(d.min()) <= DRAG_END_M:
                return True
    return False


def end_boxes(way, pad_deg=0.0003):
    """The (lon0, lat0, lon1, lat1) boxes round a way's two ends (the neighbours' lookup: MapData._osm_cell)."""
    c = np.asarray(way["coords"], np.float64)
    return [(float(e[0]) - pad_deg, float(e[1]) - pad_deg, float(e[0]) + pad_deg, float(e[1]) + pad_deg)
            for e in (c[0], c[-1])]


def classify(ways, lookup=None):
    """Sets w["race_kind"] on each raceway way (kind). lookup(box) -> raceway ways round box (the neighbours of a
    straight way's ends, from outside the cell: the same answer in every cell); without it the cell's own ways."""
    if lookup is not None:          # (one read per box: the oval network walk asks the same ends again and again)
        _raw, _memo = lookup, {}

        def lookup(box):
            k_ = tuple(round(float(v), 5) for v in box)
            if k_ not in _memo:
                _memo[k_] = list(_raw(box))
            return _memo[k_]
    for w in ways:
        c = w.get("coords")
        if c is None or len(c) < 2:
            continue
        joined = None
        if straight(c):
            others = list(ways)
            if lookup is not None:
                for box in end_boxes(w):
                    try:
                        others.extend(lookup(box))
                    except Exception:
                        pass
            joined = ends_joined(w, others)
        w["race_kind"] = kind(w, bool(joined))
        if OVAL_NETWORK_ON and w["race_kind"] != "drag" and not re.search(r"\bpits?\b", str(w.get("name") or ""), re.I):
            # (by its loop, not by itself: a speedway's main loop turns one way round in 2-4 corners, whatever their
            # radius (Bristol, Martinsville); a road course's switches direction (Spa's Kemmel was an 18 m oval)
            w["race_kind"] = speedway(w, ways, lookup)
        ow = str(w.get("oneway") or "").strip().lower()
        w["race_dir"] = 1 if ow in ("yes", "1", "true") else -1 if ow == "-1" else 0
    return ways


def road_width(race_kind):
    """The default width (m) of a raceway of race_kind without a mapped width, None: the class's."""
    return {"drag": DRAG_STRIP_M, "oval": OVAL_M}.get(race_kind)


def _offset(xy, d):
    """xy (k, 2) offset d m to the left."""
    t = np.gradient(xy, axis=0)
    t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    return xy + np.c_[-t[:, 1], t[:, 0]] * d


def pads(lonlat, race_kind, width, direction=1):
    """[(lat (k,), lon (k,))] the paved areas of a raceway (lon/lat polyline, its width m, direction +1 / -1 along it
    when one-way, 0 two-way): a drag strip's rectangle squared past its ends (half its width: the round cap of the
    painted line) and its launch apron behind the start (DRAG_APRON_*, the start the line's first end, or its last when
    driven backward); a speedway piece's apron inside its inner edge. []: none."""
    ll = np.asarray(lonlat, np.float64)[:, :2]
    if len(ll) < 2 or race_kind not in ("drag", "oval"):
        return []
    xy, kx = _xy(ll)
    o = ll[0]

    def back(p):
        return o[1] + p[:, 1] / 110540.0, o[0] + p[:, 0] / kx

    hw = float(width) / 2
    if race_kind == "drag":
        a, b = (xy[0], xy[-1]) if direction >= 0 else (xy[-1], xy[0])
        u = (b - a) / max(float(np.hypot(*(b - a))), 1e-9)
        n = np.array([-u[1], u[0]])
        a2, b2 = a - u * DRAG_APRON_M, b + u * hw
        rect = np.array([a + n * hw, b2 + n * hw, b2 - n * hw, a - n * hw])
        aw = max(DRAG_APRON_W_M, float(width)) / 2
        apron = np.array([a2 + n * aw, a + u * 1.0 + n * aw, a + u * 1.0 - n * aw, a2 - n * aw])
        return [back(rect), back(apron)]
    # oval: the inside
    if direction:
        side = 1.0 * (1 if direction > 0 else -1)          # left of the driving direction
    else:
        turn = float(_cross(xy[-1] - xy[0],xy[len(xy) // 2] - xy[0])) if len(xy) >= 3 else 0.0
        if abs(turn) < 1.0:
            return []
        side = 1.0 if turn > 0 else -1.0
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    t = np.arange(0.0, s[-1] + 1e-6, 5.0)
    if t[-1] < s[-1]:
        t = np.r_[t, s[-1]]
    p = np.c_[np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])]
    inner = _offset(p, side * (hw - 0.5))
    outer = _offset(p, side * (hw + OVAL_APRON_M))
    return [back(np.concatenate([inner, outer[::-1]]))]
