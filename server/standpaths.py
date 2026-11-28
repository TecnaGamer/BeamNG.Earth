"""The stands' lead-ins: lanes first, then each stand's lens off its lane (taxiwaymarks._lead_lines).

Plain data in, plain polylines out (the scene's plane, metres): a pure geometry step with no state, to port as it is.

Stage 1, the lanes. The painted centrelines of the mapped ways (taxilanes and taxiways), and a spine of our own behind each row
of stands whose reversed axes reach none of them: through the points SPINE_CLEAR_M plus the row's largest half span behind the
tails, in order along the row (round the point the stands face, for a fan round a satellite or a pier tip), ending at its end
stands, joined at the end nearer the mapped network by a connector (so the stand at that end can be reached from both sides and
the one at the far end from one).

Stage 2, the stands, each on its own. The stand's axis, run back from its nose, meets the first lane beyond its tail at X (the
lane need not run across the axis square: a satellite's stands meet theirs at a slant). For each way along that lane from which an
aircraft can arrive, a half of the lens: an arc that leaves the lane along it and turns onto the axis at T = X + u tl. A way is
open where the lane runs on far enough behind X for the arc to leave it (a spine's end stand: one half). From the furthest T a
straight run to the nose, at least the final straight (taxiwaymarks.lead_straight). The slit, the straight from X to T, only where
a lane runs head on into the stand (on behind X along the axis). The arc's tangent length: the stand's radius's (LENS_R_K of its
span: 25 m for a 36 m span, as painted at Brisbane), stretched to half the room between X and the final straight where the lane is
far back (a satellite's: 40 m to 55 m), but no more than half the way along the lane to the next stand's X on that side (the
lenses of a row side by side do not cross), and never under the smallest radius. A stand whose axis meets no lane is joined to
the nearest lane by one smooth curve (a single half lens).
"""
import numpy as np

import dmath
import dnp
import taxiwaymarks as tm

LENS_R_K = 0.7              # a lens's preferred radius: this of the wingspan (at least the kind's smallest: taxiwaymarks._rmin)
LENS_R_MAX_K = 2.2          # ... stretched where the lane is far back to at most this of the preferred
LENS_ROOM_K = 0.5           # ... its tangent length at least this of the room between X and the final straight
REACH_SPANS = 2.5           # a lane further behind the tail than this many wingspans is not the stand's (a spine of its own row is)
TURN_MAX_RAD = dmath.radians(165.0)     # a half turning more than this off the lane is none (the lane runs the other way)
SLIT_TURN_RAD = dmath.radians(12.0)     # a lane within this of the axis runs into the stand: the slit, no arc
MIN_SIN = 0.17              # a lane within 10 degrees of the axis is not met by it (it runs alongside)
SLIT_BACK_M = (6.0, 14.0, 22.0)         # the slit: a lane along the axis (SLIT_COS) within SLIT_NEAR_M of these points behind X
SLIT_NEAR_M = 2.5
SLIT_COS = 0.94
CHAIN_M = 1.0               # a lane's end this near another's end (or its line) runs on into it (a way split at a node)
CHAIN_COS = 0.87            # ... where the two run on within 30 degrees
WING_MARGIN_M = 1.0         # a half lens under another parked aircraft's envelope (and this) is none
LENS_TRIES = ((1.0, 1.0), (1.4, 1.0), (1.0, 1.4), (0.7, 1.0), (1.8, 1.0), (1.0, 1.8), (1.4, 1.4))   # (along the lane, along the axis) x tl
RADIUS_STEPS = (1.0, 0.85, 0.7, 0.55)   # a half that crosses a pad or an aircraft tried again at these of its radius
CONN_TRIES = 24             # a spine's (or a lone stand's) connector: the lane points nearest its end tried, this many
CONN_STEP_M = 8.0           # ... the lanes sampled this often for them
CONN_MAX_M = 300.0          # ... a connector longer than this is none
FAN_COS = 0.766             # a row whose headings stray more than 40 degrees from their mean is a fan (ordered round its centre)
SPINE_BACK_STEPS = (1.0, 0.8, 0.6, 0.45, 0.3)    # a spine's point behind a tail: this of the clearance, the first on the paving
PAVED_STEP_M = 2.0          # a line is on the paving where no more than OFF_PAVED of its points, this far apart, are off it
OFF_PAVED = 0.05
TRIM_TOL_M = 0.5
FREE_ON_M = 1.5             # a mapped lane's end this near another lane is joined to it ...
FREE_EDGE_M = 4.0           # ... or this near the paving's edge stops there as meant; any other is free: it must be joined up
FREE_PREFER_K = 1.5         # a spine starts off a free lane end where that joint is no longer than this of the nearest lane side's
FREE_PREFER_M = 30.0        # ... and this
FREE_REACH_M = 150.0        # (a free end left after the lead-ins: joined to a spine or lead-in by a curve at most this long)            # a lens leaves a spine where its first point is this near it
ROW_RING_MIN = 4            # (the rows for the service roads: a ring round a satellite, at least this many stands facing every way)
ROW_RING_MEAN = 0.5


class _Net:
    """The lanes: polylines with a way index each (the pads' own: a lead-in may cross its own way's pad), their segments flat."""

    def __init__(self, lanes):
        self.lanes = [(np.asarray(xy, np.float64), wi) for xy, wi in lanes if len(xy) >= 2]
        a, b, li, st = [np.zeros((0, 2))], [np.zeros((0, 2))], [np.zeros(0, np.int64)], [np.zeros(0)]
        self.cum = []
        for k, (xy, _wi) in enumerate(self.lanes):
            d = dnp.hypot(*np.diff(xy, axis=0).T)
            c = np.concatenate([[0.0], np.cumsum(d)])
            self.cum.append(c)
            a.append(xy[:-1])
            b.append(xy[1:])
            li.append(np.full(len(xy) - 1, k))
            st.append(c[:-1])
        self.a, self.b, self.li, self.st = np.concatenate(a), np.concatenate(b), np.concatenate(li), np.concatenate(st)

    def hits(self, p, d, lo, hi):
        """[(s, lane, station, unit tangent)] where the ray p + d s (lo <= s <= hi) crosses a lane at more than MIN_SIN, nearest first."""
        if not len(self.a):
            return []
        e = self.b - self.a
        den = d[0] * e[:, 1] - d[1] * e[:, 0]
        w = self.a - p
        ok = np.abs(den) > 1e-12
        sden = np.where(ok, den, 1.0)
        s = (w[:, 0] * e[:, 1] - w[:, 1] * e[:, 0]) / sden
        f = (w[:, 0] * d[1] - w[:, 1] * d[0]) / sden
        ln = dnp.hypot(e[:, 0], e[:, 1])
        sin = np.abs(den) / np.maximum(ln, 1e-12)
        m = ok & (f >= 0.0) & (f <= 1.0) & (s >= lo) & (s <= hi) & (sin >= MIN_SIN)
        out = []
        for j in np.nonzero(m)[0]:
            t = e[j] / ln[j]
            out.append((float(s[j]), int(self.li[j]), float(self.st[j] + f[j] * ln[j]), t))
        out.sort(key=lambda h: (round(h[0], 6), h[1]))
        return out

    def at(self, k, station):
        """(point, unit tangent) of lane k at `station` m along it, run on into the lane it joins where the station is past an end
        (CHAIN_M, CHAIN_COS); None where the lane ends there."""
        xy, cum = self.lanes[k][0], self.cum[k]
        L = float(cum[-1])
        if -1e-6 <= station <= L + 1e-6:
            station = min(max(station, 0.0), L)
            j = int(min(max(np.searchsorted(cum, station, side="right") - 1, 0), len(xy) - 2))
            seg = xy[j + 1] - xy[j]
            sl = max(float(dnp.hypot(*seg)), 1e-12)
            return xy[j] + seg * ((station - float(cum[j])) / sl), seg / sl
        end, out_dir, over = (xy[-1], xy[-1] - xy[-2], station - L) if station > L else (xy[0], xy[0] - xy[1], -station)
        out_dir = out_dir / max(float(dnp.hypot(*out_dir)), 1e-12)
        for k2, (xy2, _wi) in enumerate(self.lanes):
            if k2 == k:
                continue
            for p, q, sign in ((xy2[0], xy2[1], 1.0), (xy2[-1], xy2[-2], -1.0)):
                if float(dnp.hypot(*(p - end))) > CHAIN_M:
                    continue
                dirn = (q - p) / max(float(dnp.hypot(*(q - p))), 1e-12)
                if float(dirn[0] * out_dir[0] + dirn[1] * out_dir[1]) < CHAIN_COS:
                    continue
                L2 = float(self.cum[k2][-1])
                if over > L2:
                    return None
                pt, t = self.at(k2, over if sign > 0 else L2 - over)
                away = t if sign > 0 else -t        # (k2's tangent away from the join; lane k's own sense is away past its end, toward it past its start)
                return pt, (away if station > L else -away)
        return None

    def samples(self, step):
        """[(point, unit tangent, way index)] along every lane, `step` apart (both ways along each are tried by the callers)."""
        out = []
        for k, (xy, wi) in enumerate(self.lanes):
            L = float(self.cum[k][-1])
            n = max(1, int(np.ceil(L / step)))
            for i in range(n + 1):
                p, t = self.at(k, L * i / n)
                out.append((p, t, wi))
        return out


def _through(path, blocks):
    """Does the polyline path (k, 2) cross or enter one of the buildings `blocks` [(ring (m, 2), (x0, y0, x1, y1))]?"""
    P = np.asarray(path, np.float64)
    lo, hi = P.min(axis=0), P.max(axis=0)
    for ring, (x0, y0, x1, y1) in blocks:
        if x1 < lo[0] or x0 > hi[0] or y1 < lo[1] or y0 > hi[1]:
            continue
        if tm.pa._pip(ring, P).any() or tm._crossing(P, np.vstack([ring, ring[:1]]), 0.0):
            return True
    return False


def _off_paving(path, paved):
    """Does more than OFF_PAVED of the polyline path (k, 2), sampled PAVED_STEP_M apart, lie outside every ring of `paved` (the aprons
    and the taxiways' pads)? False with no rings (nothing known)."""
    if not paved:
        return False
    P = np.asarray(path, np.float64)
    seg = np.diff(P, axis=0)
    cnt = np.maximum(1, np.ceil(dnp.hypot(seg[:, 0], seg[:, 1]) / PAVED_STEP_M).astype(np.int64))
    pts = np.concatenate([P[j] + seg[j] * (np.arange(c) / c)[:, None] for j, c in enumerate(cnt)] + [P[-1:]])
    inside = np.zeros(len(pts), bool)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    for r in paved:
        rlo, rhi = r.min(axis=0), r.max(axis=0)
        if (rhi < lo).any() or (rlo > hi).any():
            continue
        m = np.all((pts >= rlo) & (pts <= rhi), axis=1) & ~inside
        if m.any():
            inside[np.flatnonzero(m)[tm.polygon.contains_points(r, pts[m])]] = True
    return float((~inside).mean()) > OFF_PAVED


def _clear(pads, others, blocks, paved=()):
    """ok(path, own): clear of the runways' pads, of the parked aircraft `others` and of the buildings, and on the paving (a lens may
    cross a taxiway's or taxilane's pad, as painted; never a terminal, never the grass)."""
    rp = tm._runway_pads(pads)
    return lambda path, own: (not tm._crosses(rp, path, own) and not _through(path, blocks)
                              and not tm._under_aircraft(path, others, WING_MARGIN_M) and not _off_paving(path, paved))


def _curve(S, hS, T, hT, radius, rmin, ok, own):
    """The smoothest valid Dubins curve from the pose (S, hS) to (T, hT): at `radius`, else the smaller RADIUS_STEPS (no tighter than
    rmin); None where none keeps off the pads and the other aircraft."""
    for k in RADIUS_STEPS:
        r = max(rmin, radius * k)
        plan = tm._dubins(S, hS, T, hT, r)
        if plan is not None:
            pts = tm._dubins_pts(plan)
            if ok(pts, own):
                return pts
        if r <= rmin:
            break
    return None


def _connect(net, p, h, rmin, radius, ok, arrive=True):
    """The shortest smooth curve between a lane and the pose (p, h): arriving at p along h from a lane (arrive), or leaving p along h
    onto one; None where none is shorter than CONN_MAX_M and clear."""
    cand = []
    for q, t, wi in net.samples(CONN_STEP_M):
        d = float(dnp.hypot(*(q - p)))
        if d <= CONN_MAX_M:
            cand.append((d, q, t, wi))
    cand.sort(key=lambda c: (round(c[0], 6), c[3]))
    best = None
    for d, q, t, wi in cand[:CONN_TRIES]:
        for sgn in (1.0, -1.0):
            tt = t * sgn
            for r in (radius, rmin):
                plan = tm._dubins(q, tt, p, h, r) if arrive else tm._dubins(p, h, q, tt, r)
                if plan is None or plan[0] > CONN_MAX_M or (best is not None and plan[0] >= best[0]):
                    continue
                pts = tm._dubins_pts(plan)
                if ok(pts, wi):
                    best = (plan[0], pts)
                    break
    return None if best is None else best[1]


def _free_ends(net, aprons):
    """[(point, unit direction out of the lane, way index)] of the mapped lanes' ends that touch no other lane (FREE_ON_M) and stop
    short of the apron's edge (FREE_EDGE_M; not a taxiway's pad: every way ends near its own): an end left open on the apron, which a spine or a lead-in must take up."""
    edges = [np.stack([r, np.roll(r, -1, axis=0)], axis=1) for r in aprons if len(r) >= 3]
    edges = np.concatenate(edges) if edges else np.zeros((0, 2, 2))
    out = []
    for k, (xy, wi) in enumerate(net.lanes):
        if wi < 0:
            continue
        m = net.li != k
        segs = np.stack([net.a[m], net.b[m]], axis=1)
        for end, nxt in ((xy[0], xy[1]), (xy[-1], xy[-2])):
            if len(segs) and float(tm._pts_dist(end[None], segs)[0]) <= FREE_ON_M:
                continue
            if len(edges) and float(tm._pts_dist(end[None], edges)[0]) <= FREE_EDGE_M:
                continue
            d = end - nxt
            out.append((end, d / max(float(dnp.hypot(*d)), 1e-12), wi))
    return out


def _from_end(p, h, ends, rmin, radius, ok, used):
    """(the shortest smooth curve leaving the pose (p, h) and running into one of the free lane ends `ends` along the lane (arriving
    against its outward direction), the end's index) or None: a spine started off the end of a mapped lane, not beside it."""
    best = None
    for j, (e, d, wi) in enumerate(ends):
        if j in used or float(dnp.hypot(*(e - p))) > CONN_MAX_M:
            continue
        for r in (radius, rmin):
            plan = tm._dubins(p, h, e, -d, r)
            if plan is None or plan[0] > CONN_MAX_M or (best is not None and plan[0] >= best[0]):
                continue
            pts = tm._dubins_pts(plan)
            if ok(pts, wi):
                best = (plan[0], pts, j)
                break
    return None if best is None else best[1:]


def _groups(items):
    """The rows: stands linked (noses within half their spans and SPINE_LINK_M, axes within 72 degrees: taxiwaymarks' rule)."""
    n = len(items)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            (ni, ui, _li, si), (nj, uj, _lj, sj) = items[i][:4], items[j][:4]
            if (float(dnp.hypot(*(ni - nj))) <= (si + sj) / 2.0 + tm.SPINE_LINK_M
                    and float(ui[0] * uj[0] + ui[1] * uj[1]) >= tm.SPINE_LINK_COS):
                ri, rj = find(i), find(j)
                parent[max(ri, rj)] = min(ri, rj)
    rows = {}
    for i in range(n):
        rows.setdefault(find(i), []).append(i)
    return [rows[k] for k in sorted(rows)]


def _row_kind(items, members):
    mean = np.array([items[i][1] for i in members]).mean(axis=0)
    return "ring" if len(members) >= ROW_RING_MIN and float(dnp.hypot(*mean)) < ROW_RING_MEAN else "row"


def _spine(items, members, rmin, blocks=(), paved=()):
    """The polyline of a row's own spine: through the points behind its tails, in order along it (round its centre for a fan), a
    metre past each end stand's, its corners arcs of the row's preferred radius."""
    back = max(items[i][3] for i in members) / 2.0 + tm.SPINE_CLEAR_M
    tails = {}
    for i in members:                           # (behind the tail by the clearance, nearer where that is off the paving: an apron's edge;
        for k in SPINE_BACK_STEPS:              # a stand with none on it is left out: it is joined on its own)
            t = items[i][0] - items[i][1] * (items[i][2] + back * k)
            if not _off_paving(t[None].repeat(2, axis=0), paved):
                tails[i] = t
                break
    members = [i for i in members if i in tails]
    if not members:
        return []
    mean = np.array([items[i][1] for i in members]).mean(axis=0)
    ml = float(dnp.hypot(*mean))
    fan = ml < 1e-6 or min(float(items[i][1][0] * mean[0] + items[i][1][1] * mean[1]) / ml for i in members) < FAN_COS
    if fan:
        c, _closed = tm._fan_centre(items, members)
        ang = [dmath.atan2(float(tails[i][1] - c[1]), float(tails[i][0] - c[0])) for i in members]
        order = [members[k] for k in tm._fan_order(ang, members)]
    else:
        across = np.array([-mean[1], mean[0]]) / ml
        order = sorted(members, key=lambda i: (round(float(tails[i][0] * across[0] + tails[i][1] * across[1]), 6), i))
    order = [i for i in order if i in tails]
    P = np.array([tails[i] for i in order])
    pieces, cur, who = [], [P[0]], [[order[0]]]                    # (split where the spine would run through a building: a pier between two rows)
    for j in range(1, len(P)):
        if _through(np.array([P[j - 1], P[j]]), blocks):
            pieces.append(np.array(cur))
            cur = []
            who.append([])
        cur.append(P[j])
        who[-1].append(order[j])
    pieces.append(np.array(cur))
    radius = max(rmin, LENS_R_K * max(items[i][3] for i in members))
    out = []
    for Q, w in zip(pieces, who):
        if len(Q) < 2:                          # (one stand on its own: a short lane across behind its tail, a span long)
            u = items[w[0]][1]
            half = np.array([-u[1], u[0]]) * (items[w[0]][3] / 2.0)
            Q = np.array([Q[0] - half, Q[0] + half])
        for end, nxt in ((0, 1), (-1, -2)):     # (a metre on past each end stand: its axis meets the spine, not its end)
            v = Q[end] - Q[nxt]
            Q[end] = Q[end] + v / max(float(dnp.hypot(*v)), 1e-12)
        R = tm._round_arcs(Q, radius)
        if not _through(R, blocks) and not _off_paving(R, paved):
            out.append(R)
    return out


def _lens(net, item, rmin, ok, neighbours, hit):
    """([polylines], tip distance from the nose) of a stand's lens off the lane `hit` (Net.hits'), or None where no half fits."""
    nose, u, length, span = item[:4]
    s, k, sx, t = hit
    X = nose - u * s
    straight = tm.lead_straight(length)
    room = s - straight
    r0 = max(rmin, LENS_R_K * span)
    wi = net.lanes[k][1]
    lines, tips = [], []
    slit = 0
    for b in SLIT_BACK_M:                       # (a lane on behind X along the axis: it runs head on into the stand)
        q, e = _closest(net, X - u * b)
        slit += bool(((dnp.hypot(*(q - (X - u * b)).T) <= SLIT_NEAR_M) & (np.abs(dnp.vecdot(e, u[None])) >= SLIT_COS)).any())
    slit = slit >= len(SLIT_BACK_M) - 1
    for sign in (1.0, -1.0):                    # (the aircraft travels along the lane in sign * t to X, then turns onto u)
        d = t * sign
        turn = dmath.atan2(abs(float(d[0] * u[1] - d[1] * u[0])), float(d[0] * u[0] + d[1] * u[1]))
        if turn > TURN_MAX_RAD or turn < SLIT_TURN_RAD:
            continue
        tan = dmath.tan(turn / 2.0)
        lo = rmin * tan
        if lo > room:
            continue
        tl = min(max(r0 * tan, LENS_ROOM_K * room), LENS_R_MAX_K * r0 * tan, room)
        gap = neighbours.get(-sign)             # (half the way to the next stand's X behind, along the lane)
        if gap is not None:
            tl = min(tl, max(lo, gap / 2.0))
        if net.at(k, sx - sign * tl) is None:   # (the lane ends: no aircraft comes from that side)
            continue
        for ks, kt in LENS_TRIES:               # (a curved lane: the arc leaves it a little further back, or arrives further on)
            back = net.at(k, sx - sign * tl * ks)
            if back is None or tl * kt > room:
                continue
            S, hS = back
            pts = _curve(S, hS * sign, X + u * tl * kt, u, tl / tan, rmin, ok, wi)
            if pts is not None:
                lines.append(pts)
                tips.append(tl * kt)
                break
    if slit:
        tips.append(0.0)
    if not lines and not slit:
        return None
    far = s if slit else s - min(tips)          # (the run to the nose from the tip furthest from it, or from X along the slit)
    lines.append(np.array([nose - u * far, nose]))
    return lines


def _closest(net, p):
    """(the nearest lane points (m, 2), their segments' unit tangents (m, 2)) to p: every segment's nearest point."""
    a, b = net.a, net.b
    if not len(a):
        return np.zeros((0, 2)), np.zeros((0, 2))
    e = b - a
    ln = np.maximum(dnp.hypot(e[:, 0], e[:, 1]), 1e-12)
    t = e / ln[:, None]
    q = a + t * np.clip(dnp.vecdot(p[None] - a, t), 0.0, ln)[:, None]
    return q, t


def build(items, kinds, lanes, pads, blocks=(), paved=(), aprons=()):
    """(paint [(polyline (k, 2), role "spine" | "lead", rmin)], found [the stand's final run (2, 2) or None], rows [(members,
    "row" | "ring")]) of the stands items [(nose (2,), unit direction (2,), length, span)] (kinds: their aircraft kinds) off the
    lanes [(polyline (k, 2), way index)] (the painted mapped centrelines), pads: taxiwaymarks._pads' (a lead-in crosses no runway's
    pad), blocks: the buildings [(ring (m, 2), (x0, y0, x1, y1))] no spine, connector or lens runs through,
    paved: the rings [(m, 2)] of the aprons and the taxiways' pads they stay on, aprons: the aprons' alone (a mapped lane's end at
    an apron's edge stops there; one inside is joined up)."""
    rmins = [tm._rmin(k) for k in kinds]
    rows = [(m, _row_kind(items, m)) for m in _groups(items)]
    paint = []
    net = _Net(lanes)

    def reach(i):
        nose, u, length, span = items[i][:4]
        return net.hits(nose, -u, length, length + REACH_SPANS * span)

    # stage 1: a spine for each row (of two or more) none of whose stands reaches a lane, joined to the network
    own, spines = [], []                        # spines: (index in paint, joined to the network at its end)
    ends, used = _free_ends(net, aprons), set()  # (a free mapped end is where a spine starts, before any lane's side)
    for members, _kind in rows:
        lost = [i for i in members if not reach(i)]
        if len(lost) < 2:
            continue
        for sub in _groups([items[i] for i in lost]):
            sub = [lost[j] for j in sub]
            if len(sub) < 2:
                continue
            rmin = max(rmins[i] for i in sub)
            radius = max(rmin, LENS_R_K * max(items[i][3] for i in sub))
            ok = _clear(pads, [items[i][:4] for i in range(len(items))], blocks, paved)
            for sp in _spine(items, sub, rmin, blocks, paved):
                best, end = None, None
                for P in (sp, sp[::-1]):        # (joined at the end that joins the network the shorter way: off a free lane end
                    h = P[-1] - P[-2]           # if one is in reach, else off a lane's side)
                    h = h / max(float(dnp.hypot(*h)), 1e-12)
                    got = _from_end(P[-1], h, ends, rmin, radius, ok, used)
                    if got is not None and (end is None or tm._length(got[0]) < tm._length(end[1])):
                        end = (P, got[0], got[1])
                for P in (sp, sp[::-1]):
                    h = P[-1] - P[-2]
                    h = h / max(float(dnp.hypot(*h)), 1e-12)
                    c = _connect(net, P[-1], h, rmin, radius, ok, arrive=False)
                    if c is not None and (best is None or tm._length(c) < tm._length(best[1])):
                        best = (P, c)
                if end is not None and (best is None or tm._length(end[1]) <= FREE_PREFER_K * tm._length(best[1]) + FREE_PREFER_M):
                    best = end[:2]              # (a free end not much further than the nearest lane's side is taken first)
                    used.add(end[2])
                line = sp if best is None else np.vstack([best[0], best[1][1:]])
                own.append((line, -2 - len(own)))
                spines.append((len(paint), best is not None))
                paint.append((line, "spine", rmin))
    if own:
        net = _Net(net.lanes + own)

    # stage 2: each stand's lens off the first lane its reversed axis meets
    hits = [reach(i) for i in range(len(items))]
    on = {}                                     # lane: [(station, stand)] of the first hits, for the neighbours' gaps
    for i, h in enumerate(hits):
        if h:
            on.setdefault(h[0][1], []).append((h[0][2], i))
    found = [None] * len(items)
    for i, item in enumerate(items):
        others = [items[j][:4] for j in range(len(items)) if j != i]
        lens = None
        for ok in (_clear(pads, others, blocks, paved), _clear(pads, others, blocks)):     # (on the paving; else, where the mapped
            for n, hit in enumerate(hits[i][:3]):                                         # aprons leave gaps, off it)
                nb = {}
                if n == 0:
                    for st, j in on.get(hit[1], ()):
                        if j != i:
                            g = st - hit[2]
                            side = 1.0 if g > 0 else -1.0
                            if abs(g) > 1e-6 and (side not in nb or abs(g) < nb[side]):
                                nb[side] = abs(g)
                lens = _lens(net, item, rmins[i], ok, nb, hit)
                if lens is not None:
                    break
            if lens is None:                    # (no lane behind: one curve from the nearest lane onto the final straight)
                nose, u, length, span = item[:4]
                st = nose - u * tm.lead_straight(length)
                c = _connect(net, st, u, rmins[i], max(rmins[i], LENS_R_K * span), ok)
                lens = None if c is None else [c, np.array([st, nose])]
            if lens is not None:
                break
        if lens is None:                        # (nothing reachable at all: a straight stub from behind the tail; the audit shows it)
            nose, u, length, span = item[:4]
            lens = [np.array([nose - u * (length + tm.LEAD_STUB_M), nose])]
            # (no lane reachable at all: a straight stub from behind the tail, as before; the audit shows it open)
        for xy in lens[:-1]:
            paint.append((xy, "lead", rmins[i]))
        found[i] = lens[-1]
    # the spines' free ends cut back to the first lens (or slit) leaving them: no line on past the end stands
    starts = [xy[0] for xy, role, _r in paint if role == "lead"] + [f[0] for f in found if f is not None]
    for (k, joined), (line, _wi) in zip(spines, own):
        st = [tm._station(line, p) for p in starts if tm._project(line, p)[2] < TRIM_TOL_M]
        if not st:
            paint[k] = (line[:0], "spine", paint[k][2])
            continue
        lo, hi = min(st), (tm._length(line) if joined else max(st))
        paint[k] = (tm._subpath(line, lo, hi), "spine", paint[k][2]) if hi - lo > 0.3 else (line[:0], "spine", paint[k][2])
    paint = [x for x in paint if len(x[0]) >= 2]
    # a mapped lane's end still left open on the apron: joined to the nearest spine or lead-in, along it (never a dead end)
    lines = [(xy, -100 - j) for j, (xy, role, _r) in enumerate(paint) if role in ("spine", "lead")]
    if lines:
        near = _Net(lines)
        ok = _clear(pads, [it[:4] for it in items], blocks, paved)
        segs = np.concatenate([np.stack([xy[:-1], xy[1:]], axis=1) for xy, _w in lines])
        for j, (e, d, wi) in enumerate(ends):
            if j in used or float(tm._pts_dist(e[None], segs)[0]) <= FREE_ON_M:
                continue                        # (taken up already: a lens or a spine leaves it)
            c = _connect(near, e, d, tm.MIN_RADIUS_M, LENS_R_K * 36.0, ok, arrive=False)
            if c is not None and tm._length(c) <= FREE_REACH_M:
                paint.append((c, "spine", tm.MIN_RADIUS_M))
    return paint, found, rows
