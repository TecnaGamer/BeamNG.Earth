"""Bus bays of a station (task 020): where each bay's painted outline and number go, (the numbers are plates on posts: busstationsign.plate_spots).

    layout_bays(st) -> [Bay]       (pure: from busstations.Station: mapped stops, else a row along the main roof)

A bay is a rectangle beside a platform's (or a roof's) edge, on the side buses drive in from: the near side is the kerb,
the far side and the two ends are painted. Deterministic (no randomness).
"""
import math

import numpy as np

import busstations as bs

BAY_LEN_M = 12.0            # along the kerb (a 12 m bus)
BAY_WID_M = 3.4             # from the kerb out
BAY_GAP_M = 0.6             # between the bays of a synthesised row
KERB_GAP_M = 0.3            # the painted outline's near side is this far from the platform's edge
REF_M = 14.0                # a stop this near a platform or a roof edge is its bay
MAX_SYNTH = 10
SYNTH_BELOW = 3             # fewer mapped bays than this: a numbered row is added

class Bay:
    """One bay: centre c of its rectangle, t along the kerb (reads left to right), n out from the kerb, label."""

    def __init__(self, c, t, n, label, synth=False, length=BAY_LEN_M, width=BAY_WID_M):
        self.c, self.t, self.n, self.label, self.synth = np.asarray(c, float), np.asarray(t, float), \
            np.asarray(n, float), label, synth
        self.length, self.width = length, width

    def corners(self):
        """(near start, near end, far start, far end) of the rectangle."""
        a, b = self.c - self.t * self.length / 2, self.c + self.t * self.length / 2
        h = self.n * self.width / 2
        return a - h, b - h, a + h, b + h


def _unit(v):
    v = np.asarray(v, float)
    n = float(np.hypot(*v))
    return v / n if n > 1e-9 else np.array([1.0, 0.0])


def _reading(t):
    """t turned (or not) so that the text along it reads left to right: x > 0, or straight up."""
    t = _unit(t)
    return -t if (t[0] < -1e-9 or (abs(t[0]) <= 1e-9 and t[1] < 0)) else t


def _nearest(path, closed, p):
    """(distance, nearest point, unit tangent there) of p on the polyline (closing edge too when closed)."""
    pts = np.asarray(path, float)
    if closed:
        pts = np.vstack((bs._open(pts), bs._open(pts)[:1]))
    a, b = pts[:-1], pts[1:]
    ab = b - a
    ab2 = np.maximum((ab ** 2).sum(1), 1e-9)
    t = np.clip(((p[0] - a[:, 0]) * ab[:, 0] + (p[1] - a[:, 1]) * ab[:, 1]) / ab2, 0.0, 1.0)
    q = a + ab * t[:, None]
    d = np.hypot(q[:, 0] - p[0], q[:, 1] - p[1])
    k = int(np.argmin(d))
    return float(d[k]), q[k], _unit(ab[k])


def _refs(st):
    """The kerbs buses draw up to: [(polyline, closed, half width, kind)]: the platforms and the roofs' edges."""
    out = []
    for it in st.platforms:
        if it["kind"] == "line":
            try:
                hw = min(max(float(it["tags"].get("width", "")) / 2.0, 1.0), 4.0)
            except ValueError:
                hw = 1.6
            out.append((np.asarray(it["xy"], float), False, hw, "platform"))
        elif it["kind"] == "polygon":
            out.append((bs._open(it["xy"]), True, 0.0, "platform"))
    out += [(c["ring"], True, 0.0, "roof") for c in st.canopies]
    return out


def _side(st, q, t, p=None):
    """The unit normal of t that points to the side buses come from: toward the nearest service road, else toward the
    stop, else the left."""
    left = np.array([-t[1], t[0]])
    best = None
    for r in st.roads:
        d, rq, _ = _nearest(r["xy"], False, q)
        if best is None or d < best[0]:
            best = (d, rq)
    if best is not None and best[0] < 80.0:
        s = float(np.dot(best[1] - q, left))
        if abs(s) > 0.5:
            return left if s > 0 else -left
    if p is not None:
        s = float(np.dot(np.asarray(p) - q, left))
        if abs(s) > 0.5:
            return left if s > 0 else -left
    return left


STREET_CLEAR_M = 3.2        # a bay's corners keep this far off a public street's centreline
SYNTH_SPACING_M = 15.0      # along a kerb: one bay every this (a 12 m bus and a gap)
SYNTH_EDGE_MIN_M = 10.0     # a kerb edge shorter than this takes no bay
SERVED_EDGE_MIN_M = 20.0    # a platform that has a mapped stop is filled along its edges of this length
SYNTH_EXCLUDE_M = 7.0       # a synthesised bay's centre keeps this far from a mapped one
MAX_BAYS = 60


def _rect(bay):
    a, b, c, d = bay.corners()
    return np.array([a, b, d, c])


def _sat_overlap(r1, r2):
    """Penetration depth (m) of two convex quadrilaterals (0 when they are apart): the smallest overlap over the
    normals of their edges."""
    depth = 1e9
    for r in (r1, r2):
        for i in range(len(r)):
            e = r[(i + 1) % len(r)] - r[i]
            n = np.array([-e[1], e[0]])
            ln = float(np.hypot(*n))
            if ln < 1e-9:
                continue
            n /= ln
            p1, p2 = r1 @ n, r2 @ n
            ov = min(p1.max(), p2.max()) - max(p1.min(), p2.min())
            if ov <= 0:
                return 0.0
            depth = min(depth, ov)
    return depth


def _samples(bay):
    """Points over the bay's rectangle: corners, edge middles, centre and the quarter points."""
    a, b, c, d = bay.corners()
    pts = [a, b, c, d, (a + b) / 2, (c + d) / 2, (a + c) / 2, (b + d) / 2, bay.c, (a + b + c + d) / 4]
    pts += [bay.c + bay.t * bay.length / 4, bay.c - bay.t * bay.length / 4]
    return np.array(pts)


def _lane_hit(st, bay):
    """Does the bay lie on a drive lane (the drawn road) or in a mouth?"""
    return bool(st.lanes) and bool(bs.lane_blocked(st, _samples(bay), pad=-0.2, core=True).any())


def _parallel_to_lanes(st, bay):
    """Is the bay along the lane it lies on (a bus pulls up to the kerb on the road itself)? Not in a mouth."""
    pts = _samples(bay)
    for mx, my in st.mouths:
        if bool((np.hypot(pts[:, 0] - mx, pts[:, 1] - my) < bs.MOUTH_R_M).any()):
            return False
    for ln in st.lanes:
        d = bs.path_dist(ln["xy"], pts)
        if not bool((d < ln["core"] - 0.2).any()):
            continue
        k = int(np.argmin(bs.path_dist(ln["xy"], bay.c[None]) if len(ln["xy"]) == 1 else
                          np.hypot(*(np.asarray(ln["xy"][:-1], float) + np.asarray(ln["xy"][1:], float) - 2 * bay.c).T)))
        seg = np.asarray(ln["xy"][k + 1], float) - np.asarray(ln["xy"][k], float)
        ln_ = float(np.hypot(*seg))
        if ln_ < 1e-6 or abs(float(np.dot(seg / ln_, bay.t))) < 0.9:
            return False
    return True


def _shifted(st, bay, along):
    return Bay(bay.c + bay.t * along, bay.t, bay.n, bay.label, bay.synth, bay.length, bay.width)


def _place(st, bay, parallel_ok=True):
    """The bay kept off the drive lanes and mouths: as it is, or slid along its kerb (3 m steps, 12 m at most) to a place
    where it is clear, or (a bay along the lane it stands on: the bus pulls up to the kerb on the road) as it is; else None."""
    if not _usable(st, bay, lanes=False):
        return None
    if not _lane_hit(st, bay):
        return bay
    if parallel_ok and _parallel_to_lanes(st, bay):
        return bay
    for k in range(1, 5):
        for sgn in (1.0, -1.0):
            b = _shifted(st, bay, sgn * 3.0 * k)
            if _usable(st, b, lanes=False) and not _lane_hit(st, b):
                return b
    return None


def _usable(st, bay, lanes=True):
    """Can a bus stand here: the rectangle is clear of every building and public street, and lies in the yard's reach; and,
    with lanes, of the drive lanes and the mouths."""
    if lanes and _lane_hit(st, bay):
        return False
    rect = _rect(bay)
    pts = list(rect) + [bay.c]
    bb = getattr(st, "_obs_bb", None)
    if bb is None:
        bb = np.array([[o[:, 0].min(), o[:, 1].min(), o[:, 0].max(), o[:, 1].max()] for o in st.obstacles]).reshape(-1, 4)
        st._obs_bb = bb
    lo, hi = rect.min(0), rect.max(0)
    near = np.flatnonzero((bb[:, 0] <= hi[0]) & (bb[:, 2] >= lo[0]) & (bb[:, 1] <= hi[1]) & (bb[:, 3] >= lo[1]))
    for ob in (st.obstacles[i] for i in near):
        if any(bs.contains(ob, p) for p in pts):
            return False
        if any(bs.contains(rect, q) for q in ob):          # a small building inside the rectangle
            return False
    for s in st.streets:
        if s.get("level", 0.0) != 0.0:                     # a viaduct over the yard (Vienna's) is no street in it
            continue
        if any(bs.dist_to_path(s["xy"], p) < STREET_CLEAR_M for p in pts):
            return False
    for pl in st.platforms:                                # the platform is where people stand, not where the bus is
        if pl["kind"] == "polygon" and bs.contains(pl["xy"], bay.c):
            return False
    return True


def layout_bays(st):
    """The bays of a station: one per mapped stop beside a kerb (a platform's or a roof's edge); then bays added along the
    kerbs that serve buses but have no stop of their own (a long island with one stop mapped: every 15 m of it), and,
    when fewer than SYNTH_BELOW are mapped, along the biggest kerb. Each must be clear of buildings and public streets and
    of the bays before it (mapped ones first). Memoised on the station."""
    cached = getattr(st, "_bays", None)
    if cached is not None:
        return cached
    refs = _refs(st)
    bays = []
    for b in st.bays:
        hit = None
        for path, closed, hw, _ in refs:
            d, q, t = _nearest(path, closed, b["xy"])
            if d - hw <= REF_M and (hit is None or d - hw < hit[0]):
                hit = (d - hw, q, t, hw)
        if hit is None:
            bay = _road_bay(st, b)                  # no platform, no roof: the bus stands on the service road at the stop
        else:
            _, q, t, hw = hit
            n = _side(st, q, t, b["xy"])
            bay = Bay(q + n * (hw + KERB_GAP_M + BAY_WID_M / 2.0), _reading(t), n, b["label"])
        bay = _place(st, bay) if bay is not None else None
        if bay is not None:
            bays.append(bay)
    bays = _drop_overlaps(bays)
    mapped = list(bays)
    served = [r for r in refs if any(_nearest(r[0], r[1], bay.c)[0] - r[2] <= REF_M for bay in mapped)]
    bays += _fill_edges(st, [r for r in served if r[3] == "platform"], bays, SERVED_EDGE_MIN_M)
    if len(mapped) < SYNTH_BELOW:
        free = [r for r in refs if all(_nearest(r[0], r[1], bay.c)[0] - r[2] > REF_M for bay in mapped)]
        row = _synth_row(st, [(p, c, hw) for p, c, hw, _ in free])
        for r in row:
            r = _place(st, r)
            if r is not None and len(bays) < MAX_BAYS and all(_sat_overlap(_rect(r), _rect(o)) < 0.5 for o in bays):
                bays.append(r)
    if len(bays) < SYNTH_BELOW and not refs:        # a few stops and no kerb at all: a row along the station's road
        bays += [r for r in _road_row(st) if all(_sat_overlap(_rect(r), _rect(o)) < 0.5 for o in bays)]
    _number(bays)
    st._bays = bays
    return bays


ROAD_STOP_M = 25.0          # a stop this near a service road is on it
ROAD_ROW_M = 45.0           # a row along a service road reaches this far round the hub
ROAD_HALF_M = 2.8


def _road_bay(st, stop):
    """The bay of a mapped stop that has no kerb: along the nearest service road, on the road's side of the stop."""
    best = None
    for r in st.roads:
        d, q, t = _nearest(r["xy"], False, stop["xy"])
        if d <= ROAD_STOP_M and (best is None or d < best[0]):
            best = (d, q, t)
    if best is None:
        return None
    d, q, t = best
    left = np.array([-t[1], t[0]])
    v = q - np.asarray(stop["xy"], float)
    n = _unit(v) if d > 0.5 else left
    n = left if float(np.dot(n, left)) >= 0 else -left          # square to the road
    side = 1.0 if float(np.dot(v, left)) >= 0 else -1.0
    n = left * side if d > 0.5 else left
    return Bay(np.asarray(stop["xy"], float) + n * (KERB_GAP_M + BAY_WID_M / 2.0), _reading(t), n, stop["label"])


def _road_row(st):
    """Bays every SYNTH_SPACING_M along the biggest service road near the hub (both sides, where they fit)."""
    out = []
    roads = sorted((r for r in st.roads if bs.dist_to_path(r["xy"], st.hub) <= ROAD_ROW_M),
                   key=lambda r: -float(np.hypot(*np.diff(np.asarray(r["xy"], float), axis=0).T).sum()))
    for r in roads[:2]:
        pts = np.asarray(r["xy"], float)
        seg = np.hypot(*np.diff(pts, axis=0).T)
        total = float(seg.sum())
        if total < SYNTH_EDGE_MIN_M:
            continue
        cum = np.concatenate(([0.0], np.cumsum(seg)))
        k = max(1, int(total // SYNTH_SPACING_M))
        for i in range(k):
            at = total * (i + 0.5) / k
            j = min(int(np.searchsorted(cum, at, side="right")) - 1, len(seg) - 1)
            t = (pts[j + 1] - pts[j]) / max(float(seg[j]), 1e-9)
            q = pts[j] + t * (at - cum[j])
            if float(np.hypot(*(q - st.hub))) > ROAD_ROW_M:
                continue
            left = np.array([-t[1], t[0]])
            for n in (left, -left):
                bay = Bay(q + n * (ROAD_HALF_M + KERB_GAP_M + BAY_WID_M / 2.0), _reading(t), n, "", synth=True)
                if len(out) < 8 and _usable(st, bay) and all(_sat_overlap(_rect(bay), _rect(o)) < 0.5 for o in out):
                    out.append(bay)
    return out


def _number(bays):
    """The synthesised bays' numbers 1.. in the order they were laid out, after the mapped labels' count (a mapped label is
    never reused)."""
    taken = {b.label for b in bays if not b.synth and b.label}
    k = 0
    for b in bays:
        if not b.synth:
            continue
        k += 1
        while str(k) in taken:
            k += 1
        b.label = str(k)


def _fill_edges(st, refs, bays, min_edge):
    """Bays every SYNTH_SPACING_M along the long edges of the platforms refs (line: both sides; polygon: outside), where a
    bay fits: clear of buildings and streets, not over the bays already there."""
    out = []
    for path, closed, hw, _ in refs:
        pts = np.asarray(bs._open(path) if closed else path, float)
        segs = np.vstack((pts, pts[:1])) if closed else pts
        centre = pts.mean(0)
        for a, b in zip(segs[:-1], segs[1:]):
            length = float(np.hypot(*(b - a)))
            if length < min_edge:
                continue
            t = (b - a) / length
            left = np.array([-t[1], t[0]])
            sides = (left, -left)
            if closed:                                    # only the outside of a polygon
                mid = (a + b) / 2.0
                sides = (left,) if float(np.dot(left, mid - centre)) > 0 else (-left,)
            k = max(1, int(length // SYNTH_SPACING_M))
            for i in range(k):
                q = a + t * (length * (i + 0.5) / k)
                for n in sides:
                    bay = Bay(q + n * (hw + KERB_GAP_M + BAY_WID_M / 2.0), _reading(t), n, "", synth=True)
                    if len(bays) + len(out) >= MAX_BAYS:
                        continue
                    bay = _place(st, bay, parallel_ok=False)
                    if bay is None:
                        continue
                    if any(float(np.hypot(*(bay.c - o.c))) < SYNTH_EXCLUDE_M and not o.synth for o in bays):
                        continue
                    if any(_sat_overlap(_rect(bay), _rect(o)) >= 0.5 for o in list(bays) + out):
                        continue
                    out.append(bay)
    return out


def _drop_overlaps(bays):
    """Bays that overlap a bay before them by more than 0.5 m go (mapped stops 4 m apart along one kerb: every third)."""
    out = []
    for b in bays:
        if all(float(np.hypot(*(b.c - o.c))) > 3.0 and _sat_overlap(_rect(b), _rect(o)) < 0.5 for o in out):
            out.append(b)
    return out


def _synth_row(st, refs):
    """A numbered row of bays along the long side of the biggest reference (a roof, a platform), on the side with more
    room: the axis is its longest edge's direction, the row spans the shape's extent along it."""
    best = None
    for path, closed, hw in refs:
        pts = np.asarray(bs._open(path) if closed else path, float)
        segs = np.vstack((pts, pts[:1])) if closed else pts
        for a, b in zip(segs[:-1], segs[1:]):
            length = float(np.hypot(*(b - a)))
            if length < 3.0:
                continue
            t = (b - a) / length
            left = np.array([-t[1], t[0]])
            along, lat = pts @ t, pts @ left
            extent = float(along.max() - along.min())
            width = float(lat.max() - lat.min())
            if best is None or extent * (1.0 + 0.001 * length) > best[0]:
                best = (extent, t, left, float(along.min()), float(along.max()), float(lat.min()), float(lat.max()), hw,
                        width)
    if best is None or best[0] < BAY_LEN_M * 0.9:
        return []
    extent, t, left, a0, a1, l0, l1, hw, _ = best
    mid_along = (a0 + a1) / 2.0
    room = []
    for sign, edge in ((1.0, l1), (-1.0, l0)):                    # the room outside each long edge
        probe = t * mid_along + left * (edge + sign * (hw + KERB_GAP_M + BAY_WID_M / 2.0))
        inside = any(bs.contains(it["xy"], probe) for it in st.buildings)
        near = min([bs.dist_to_path(it["xy"], probe, True) for it in st.buildings] or [1e9])
        room.append(-1.0 if inside else near)
    sign, edge = (1.0, l1) if room[0] >= room[1] else (-1.0, l0)
    n = left * sign
    count = int(min(MAX_SYNTH, max(1, (extent + BAY_GAP_M) // (BAY_LEN_M + BAY_GAP_M))))
    used = count * BAY_LEN_M + (count - 1) * BAY_GAP_M
    kt = _reading(t)                                             # numbered in the reading direction
    out = []
    for i in range(count):
        along = mid_along - used / 2.0 + BAY_LEN_M / 2.0 + i * (BAY_LEN_M + BAY_GAP_M)
        if float(np.dot(kt, t)) < 0:
            along = 2 * mid_along - along
        c = t * along + left * (edge + sign * (hw + KERB_GAP_M + BAY_WID_M / 2.0))
        out.append(Bay(c, kt, n, str(i + 1), synth=True))
    return out
