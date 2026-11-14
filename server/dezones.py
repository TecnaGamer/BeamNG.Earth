"""Germany's zone signs, Zeichen 274.1 (a zone begins) and 274.2 (it ends), where a mapped zone (the lane shards'
osm_zone_maxspeed: Tempo 30 / 20 / 10) meets a street outside it, in place of the round 274 there (the German junction
signs on, near Germany): speedsigns.build_tile's hook (tile), the zone edges and the pairs' spots (zones), the 274s they
replace (outside_zones) and their mesh (de_zone_mesh), beside designs.py (the junction signs) and designsigns.py (their
sizes and faces). The rows are speedsigns' (_cell_rows: its seventh element, the zone's km/h and the class)."""
from collections import namedtuple

import numpy as np

import designs
import designsigns
import exitsigns
import germany
import speedsigns

# Germany's zone signs: Zeichen 274.1 / 274.2 (designsigns' ZONE_*: tools/build_de_signs.py's sheet
# bne_de_zones.png) where a road of a zone (the lane shards' osm_zone_maxspeed, designs.ZONES) meets one outside it
MATERIALS_ZONE = ['bne_de_zones', 'bne_exit_metal']
ZONE_KMH = {z: int(z[3:]) for z in designs.ZONES}      # osm_zone_maxspeed -> the zone's limit (km/h)
ZONE_IN_M = 5.0         # m from a zone's edge its signs stand, in or out of it (at a junction: from its widest other
                        # road's edge)
ZONE_TOL_M = 0.5        # m: "on" a road, and the map's junction fractions (the kernel snaps one within 1e-3 of an end)
ZONE_REACH_M = 100.0    # m round a tile the roads whose zone edges can stand a sign in it or clear a 274 there
# The kernel's own (native/earthfast/src/speedsigns.rs, which names this twin: change them together), for the zone
# signs' spots (a blocked one tried farther on, ALONG_STEPS_M's first three, then out: OUT_STEP_M, OUT_MAX_M, clear()'s
# CLEAR_M) and for the 274 a zone sign replaces (outside_zones): a junction's straight on continuation
# (STRAIGHT_COS, STRAIGHT_MARGIN over the first TANGENT_M of each arm) and how far past it the kernel stands its 274
# (JUNCTION_IN_URBAN_M, then moved on by up to ALONG_STEPS_M's last when its spot is blocked: zones are in town, StVO
# § 45 Abs. 1c "innerhalb geschlossener Ortschaften"). (_clears sees the street arms only, _arms': a service road at
# the node counts in the kernel's straight(), not there.)
ZONE_ALONG_M = (0.0, 5.0, 10.0)
OUT_STEP_M, OUT_MAX_M, CLEAR_M = 0.5, 3.0, 0.25
STRAIGHT_COS, STRAIGHT_MARGIN, TANGENT_M = 0.75, 0.1, 10.0
JUNCTION_IN_URBAN_M, ALONG_STEPS_MAX_M = 30.0, 30.0
ZONE_PAST_M = JUNCTION_IN_URBAN_M + ALONG_STEPS_MAX_M
# the signs (k, 9) [x, y, travel x, travel y, sign (1: 274.1, 2: 274.2), zone km/h, size (designsigns.ZONE_SIZE), road
# x, road y], the zone edges (m, 2), the zone roads' lines, and the clears: [(the line (k, 2) of an arm out of an edge,
# ZONE_PAST_M of it from the edge on, its limit posted there)] whose 274 a zone sign replaces, of a tile in Germany
Zones = namedtuple("Zones", "signs edges lines clears")


def _snap(f):
    return 0.0 if f < 1e-3 else 1.0 if f > 1.0 - 1e-3 else float(f)       # (as the kernel's junctions)


def _arms(rows):
    """{connector: [(row, arc m at it, +1 the arm runs along the row's line / -1 against it, its piece's length m)]}
    of the connectors where two or more streets' rows meet (designs.CLS's classes: no service road, driveway or
    track); a piece runs to the next such connector on the row, or its end (a footway's or a driveway's connector
    halfway along is none)."""
    nodes, rows_at = {}, {}
    for r, row in enumerate(rows):
        pts, cons, (_, cls) = row[1], row[3], row[6]
        if cls not in designs.CLS or len(pts) < 2:
            continue
        total = float(np.hypot(*np.diff(pts, axis=0).T).sum())
        if total < 1.0:
            continue
        here = []
        for f, c in sorted((_snap(f), c) for f, c in cons):
            if not here or f - here[-1][0] > 1e-9:
                here.append((f, c))
                rows_at.setdefault(c, set()).add(r)
        nodes[r] = (total, here)
    arms = {}
    for r, (total, here) in nodes.items():
        stops = [f * total for f, c in here if len(rows_at[c]) > 1]
        for f, c in here:
            if len(rows_at[c]) < 2:
                continue
            at = f * total
            ahead = min((s for s in stops if s > at + 1e-9), default=total) - at
            behind = at - max((s for s in stops if s < at - 1e-9), default=0.0)
            if ahead >= 1.0:
                arms.setdefault(c, []).append((r, at, 1, ahead))
            if behind >= 1.0:
                arms.setdefault(c, []).append((r, at, -1, behind))
    return arms


def _at(pts, s):
    """(the point, the unit tangent along the line) at arc s (m) of the polyline pts (k, 2)."""
    seg = np.hypot(*np.diff(pts, axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    k = int(np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(seg) - 1))
    while seg[k] < 1e-9 and k > 0:
        k -= 1
    d = (pts[k + 1] - pts[k]) / max(seg[k], 1e-9)
    return pts[k] + d * (s - cum[k]), d


def _cut(pts, a, b):
    """The polyline pts (k, 2) from arc a to arc b (m; b < a: backwards)."""
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))]
    lo, hi = min(a, b), max(a, b)
    inner = pts[(s > lo) & (s < hi)]
    line = np.vstack([_at(pts, lo)[0], inner, _at(pts, hi)[0]])
    return line if b >= a else line[::-1]


def _clears(rows, here, totals):
    """[(line, posted limit)] of the arms out of one zone edge (its arms here) whose 274 is the boundary's (spec B2: the
    zone signs replace that 274), each its first ZONE_PAST_M (to the next street junction at most): an arm outside every
    zone that traffic out of a zone arm drives straight on to (the kernel's pred: of the arms coming in, the best
    aligned with it within STRAIGHT_COS and STRAIGHT_MARGIN ahead of the next, is a zone arm), the outer arm of a 2-arm
    connector, or the lone street of a lone-street edge, every other arm one zone's (_pairs stands the pair on it, and
    every car on it comes from that zone: the kernel's turn-in 274 there, a tertiary off a secondary, faced the 274.2's
    traffic). Any other arm's 274s (the road through a zone street's mouth: its own changes, its junction reminders) are
    not the zone's."""
    def heading(a):
        r, at, sense, piece = a
        p, q = _at(rows[r][1], at)[0], _at(rows[r][1], at + sense * min(TANGENT_M, piece))[0]
        return (q - p) / max(float(np.hypot(*(q - p))), 1e-9)

    def drives(a, out):
        d = rows[a[0]][2][1]
        return not d or d == (a[2] if out else -a[2])
    out = []
    for arm in here:
        if rows[arm[0]][6][0] or not drives(arm, True):
            continue
        # (every other arm one zone's: a 2-arm connector's outer arm, at any angle as the kernel's straight() takes
        # two pieces, or a lone street; else the straight on test)
        if len({rows[a[0]][6][0] for a in here if a != arm}) > 1:
            u = heading(arm)
            cands = sorted(((float(-heading(a) @ u), k) for k, a in enumerate(here) if a != arm and drives(a, False)),
                           reverse=True)
            if not cands:
                continue
            c, k = cands[0]
            second = cands[1][0] if len(cands) > 1 else -2.0
            if not (c >= STRAIGHT_COS and c - second >= STRAIGHT_MARGIN and rows[here[k][0]][6][0]):
                continue
        r, at, sense, piece = arm
        line = _cut(rows[r][1], at, at + sense * min(ZONE_PAST_M + ZONE_TOL_M, piece))
        out.append((line, _posted(rows[r][4], at / max(totals[r], 1e-9), sense)))
    return out


def _posted(lims, f, sense):
    """The posted value (_limits') of a row on its side sense (+1 on from fraction f, -1 back from it), 0 for none."""
    probe, v = f + 1e-6 * sense, 0.0
    for a, value in lims:
        if a <= probe:
            v = value
    return v


def _segments(pavements, box, reach):
    """(starts (m, 2), ends (m, 2), the outline of each (m,), outlines) of the pavement outlines' edges near box."""
    poly, off = speedsigns._outlines(pavements)
    x0, y0, x1, y1 = box
    a, b, ring = [], [], []
    for q in range(len(off) - 1):
        p = poly[off[q]:off[q + 1]]
        if (len(p) < 3 or p[:, 0].max() < x0 - reach or p[:, 0].min() > x1 + reach or p[:, 1].max() < y0 - reach
                or p[:, 1].min() > y1 + reach):
            continue
        a.append(p)
        b.append(np.roll(p, -1, axis=0))
        ring.append(np.full(len(p), len(ring)))
    if not a:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0, np.int64), 0
    return np.vstack(a), np.vstack(b), np.concatenate(ring), len(ring)


def _clear(c, r, segs):
    """Whether a disc (c, r) is clear of every pavement (outside each and r from its edges: the kernel's clear)."""
    a, b, ring, n = segs
    if not len(a):
        return True
    d = b - a
    t = np.clip(((c - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
    gap = a + t[:, None] * d - c
    if ((gap * gap).sum(1) < r * r).any():
        return False
    up = (a[:, 1] > c[1]) != (b[:, 1] > c[1])
    dy = np.where(up, d[:, 1], 1.0)
    cross = up & (c[0] < a[:, 0] + (c[1] - a[:, 1]) * d[:, 0] / dy)
    return not (np.bincount(ring[cross], minlength=n) % 2).any()


def _pairs(rows, here, v):
    """[(sign, arm, facing the traffic leaving the edge along the arm (True) or coming in on it)] of zone v at one edge
    (its arms here): the pair stands on the side with a single arm (a zone street's mouth: both in it, as German
    practice signs it; a lone street outside meeting the zone's streets: both on it, never a 274.2 on a road through
    for traffic that was never in the zone), on the zone's arms where neither side is single, and at a 2-arm connector
    274.1 in, 274.2 out. A 274.2 only for traffic that can leave into a
    street outside every zone, not a living street (VwV zu den Zeichen 274.1 und 274.2 Rn. 3: entering a
    verkehrsberuhigter Bereich it is not needed, that area's own sign stands there; by analogy, nor where another
    zone's 274.1 does)."""
    def ends(a):
        return rows[a[0]][6][0] == 0 and rows[a[0]][6][1] != "living_street"
    inside = [a for a in here if rows[a[0]][6][0] == v]
    outside = [a for a in here if rows[a[0]][6][0] != v]
    leaves = any(ends(a) for a in outside)
    if len(here) == 2:
        out = [(1, inside[0], True)] + ([(2, outside[0], True)] if leaves else [])
    elif len(inside) == 1:
        out = [(1, inside[0], True)] + ([(2, inside[0], False)] if leaves else [])
    elif len(outside) == 1:
        out = [(1, outside[0], False)] + ([(2, outside[0], True)] if leaves else [])
    else:
        out = [p for a in inside for p in [(1, a, True)] + ([(2, a, False)] if leaves else [])]
    return out


def zones(frame, box, context, left_hand=False, mph=False):
    """Germany's zone signs of box (x0, y0, x1, y1) (a tile's, the German junction signs on): Zones. A zone edge is a
    connector in Germany where a street of a zone (osm_zone_maxspeed: DE:30 / 20 / 10, its digits whatever limit the
    road has posted) meets one outside it (or of another zone): there a 274.1 faces the traffic going into the zone, a
    274.2 the traffic leaving it (_pairs: which arm), each ZONE_IN_M (5 m) in or out, from a junction's widest other
    road then (its half width past the node), on its driver's side as far out as the 274s (the kernel's lateral: 0.5 m
    past the pavement in town, 1.2 out of it), clear of every pavement (moved on, then out, as the kernel moves its
    274s), one way only where the traffic goes that way; in box when it stands there. Its size (VwV-StVO zu den §§ 39
    bis 43 Rn. 17a, 15, 13: designsigns.zone_size) by the limit its traffic comes from: a 274.1 the road outside's
    (the zone's limit where it comes from another zone), a 274.2 the zone's."""
    x0, y0, x1, y1 = box
    rows = [r for r in speedsigns._tile_rows(frame, box, context, left_hand, mph, speedsigns.STYLE_VIENNA,
                                             ZONE_REACH_M)
            if not (r[1][:, 0].max() < x0 - ZONE_REACH_M or r[1][:, 0].min() > x1 + ZONE_REACH_M
                    or r[1][:, 1].max() < y0 - ZONE_REACH_M or r[1][:, 1].min() > y1 + ZONE_REACH_M)]
    lines = [r[1] for r in rows if r[6][0]]
    found = []
    for c, here in sorted(_arms(rows).items()):
        values = {rows[a[0]][6][0] for a in here}
        if len(values) > 1 and any(values):
            r, at = here[0][0], here[0][1]
            found.append((c, here, _at(rows[r][1], at)[0]))
    if found:
        xy = np.array([p for _, _, p in found])
        found = [f for f, de in zip(found, germany.de_at(frame, xy[:, 0], xy[:, 1])) if de]
    if not found:
        return Zones(np.zeros((0, 9)), np.zeros((0, 2)), lines, [])
    cla, clo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    segs = _segments(context.pavements(frame, np.c_[clo, cla], left_hand), box, ZONE_REACH_M)
    side = -1.0 if left_hand else 1.0
    totals = [float(np.hypot(*np.diff(r[1], axis=0).T).sum()) for r in rows]

    def limit(a):
        """The limit on arm a by the edge: its zone's, else the one posted there."""
        return rows[a[0]][6][0] or _posted(rows[a[0]][4], a[1] / max(totals[a[0]], 1e-9), a[2])
    out, clears = [], []
    for _, here, _ in found:
        clears += _clears(rows, here, totals)
        for v in sorted({rows[a[0]][6][0] for a in here} - {0}):
            others = [a for a in here if rows[a[0]][6][0] != v]
            for sign, arm, leaving in _pairs(rows, here, v):
                r, at, sense, piece = arm
                _, pts, meta, _, _, skips, _ = rows[r]
                if meta[1] and meta[1] != (sense if leaving else -sense):
                    continue                            # (a one-way: no traffic this way)
                total = totals[r]
                # (a 274.1 leaving the edge into the zone: its traffic came from any road outside it)
                kmh = v if sign == 2 else max(limit(a) for a in others) if leaving else limit(arm)
                size = designsigns.zone_size(kmh)
                w = designsigns.ZONE_SIZE[size]
                mouth = 0.0 if len(here) == 2 else max(rows[a[0]][2][0] for a in here if a != arm)
                base = min(ZONE_IN_M + mouth, piece / 2)
                lateral = 0.5 if meta[3] else 1.2       # (the kernel's lateral(): Vienna, in town / out of it)
                spot = None
                for du in ZONE_ALONG_M:
                    u = base + du
                    if u > piece - 1.0:
                        break
                    s = at + sense * u
                    if any(a - 1e-6 <= s / total <= b + 1e-6 for a, b in skips):
                        continue                        # (on a bridge or in a tunnel)
                    road, d = _at(pts, s)
                    t = d * (sense if leaving else -sense)
                    right = np.array([t[1] * side, -t[0] * side])
                    extra = 0.0
                    while extra <= OUT_MAX_M + 1e-9:
                        q = road + right * (meta[0] + lateral + w / 2 + extra)
                        if _clear(q, w / 2 + CLEAR_M, segs):
                            spot = (q, t, road)
                            break
                        extra += OUT_STEP_M
                    if spot is not None:
                        break
                if spot is None:
                    continue
                q, t, road = spot
                if x0 <= q[0] < x1 and y0 <= q[1] < y1:
                    out.append([q[0], q[1], t[0], t[1], sign, v, size, road[0], road[1]])
    return Zones(np.array(out, np.float64).reshape(-1, 9), np.array([p for _, _, p in found]), lines, clears)


def outside_zones(frame, signs, zones):
    """Which of place()'s 274s (signs) stay beside Germany's zone signs (zones: Zones): not the ones the zone signs
    replace (spec B2: "274.1 / 274.2 replace that 274"), whose road point is in Germany and either on a
    zone road (within ZONE_TOL_M of its line: inside a zone its own limit is the zone's sign's), or on one of the
    clears (_clears: the road traffic out of the zone goes straight on to) within ZONE_PAST_M of the edge, facing the
    traffic leaving the edge there and showing the limit posted there (the kernel's 274 for it; a change farther along
    the road is another boundary's). Every other 274 stays: the road through a zone street's mouth keeps its own."""
    hit = np.zeros(len(signs), bool)
    if not len(signs):
        return ~hit
    signs = np.asarray(signs, np.float64)
    p = signs[:, 6:8]

    def near(line):
        """(distance of each road point to line, the line's direction at its nearest point)."""
        a, d = line[:-1], np.diff(line, axis=0)
        t = np.clip(((p[:, None] - a[None]) * d[None]).sum(-1) / np.maximum((d * d).sum(1), 1e-12)[None], 0.0, 1.0)
        gap = np.sqrt(((a[None] + t[..., None] * d[None] - p[:, None]) ** 2).sum(-1))
        k = gap.argmin(1)
        return gap[np.arange(len(p)), k], d[k] / np.maximum(np.hypot(d[k, 0], d[k, 1]), 1e-9)[:, None]
    for line in zones.lines:
        hit |= near(line)[0] <= ZONE_TOL_M
    for line, value in zones.clears:
        gap, d = near(line)
        hit |= (gap <= ZONE_TOL_M) & ((signs[:, 2:4] * d).sum(1) > 0.0) & (np.abs(signs[:, 4] - value) < 0.5)
    if hit.any():
        hit[hit] = germany.de_at(frame, p[hit, 0], p[hit, 1])
    return ~hit


def de_zone_mesh(table, origin):
    """(positions, triangles, uvs, materials, material of each triangle) of Germany's zone signs, or None: rows (x, y,
    ground z, road z, travel x, travel y, sign 1 (274.1) / 2 (274.2), zone km/h, size: designsigns.ZONE_SIZE's). As
    speedsigns.sweden_mesh: in a sign's frame x towards the driver, y to the driver's right, z up; its face the square
    of designsigns.edge("plate") (a 40 mm corner at 600), its lower edge designsigns.LOW_M (2.00 m: VwV-StVO zu den §§
    39 bis 43 Rn. 42) over the road (at least speedsigns.C31_MIN_GROUND over the ground), a folded edge, a grey back
    and a post (Rn. 24) of designsigns' sizes into the ground."""
    P, T, UV, M = [], [], [], []
    n = 0

    def add(pts, uvs, tris, mat, row):
        nonlocal n
        pts = np.asarray(pts, np.float64)
        x, y, zg, tx, ty = row
        world = np.column_stack([x - origin[0] - tx * pts[:, 0] + ty * pts[:, 1],
                                 y - origin[1] - ty * pts[:, 0] - tx * pts[:, 1], zg - origin[2] + pts[:, 2]])
        P.append(world)
        UV.append(np.asarray(uvs, np.float64))
        T.append(np.asarray(tris, np.int64) + n)
        M.append(np.full(len(tris), mat, np.int64))
        n += len(pts)

    for x, y, zg, zr, tx, ty, sign, kmh, size in table:
        sign, kmh, size = int(round(sign)), int(round(kmh)), int(np.clip(round(size), 0, 2))
        if sign not in (1, 2) or kmh not in designsigns.ZONE_KMH or not np.isfinite([x, y, zg, zr]).all():
            continue
        tl = max(np.hypot(tx, ty), 1e-9)
        row = (x, y, zg, tx / tl, ty / tl)
        side = designsigns.ZONE_SIZE[size]
        zc = max(zr + designsigns.LOW_M, zg + speedsigns.C31_MIN_GROUND) - zg + side / 2
        edge = designsigns.edge("plate", side)
        f = designsigns.ZONE_SIZE[1] / side             # (the sheet's face is Größe 2)
        k = len(edge)
        face = np.array([(0.0, ex, zc + ez) for ex, ez in edge])
        fan = [(0, j, j + 1) for j in range(1, k - 1)]
        add(face, [designsigns.zone_uv(sign, kmh, ex * f, ez * f) for ex, ez in edge], fan, 0, row)
        back = face + (-designsigns.DEPTH_M, 0.0, 0.0)
        add(back, np.zeros((k, 2)), [(a, c, b) for a, b, c in fan], 1, row)
        quads = np.zeros((k * 4, 3))                    # the edge, each facet its own corners
        nxt = (np.arange(k) + 1) % k
        quads[0::4], quads[1::4], quads[2::4], quads[3::4] = back, back[nxt], face[nxt], face
        add(quads, np.zeros((k * 4, 2)), [(i, j, q + 4 * p) for p in range(k)
                                          for i, j, q in ((4 * p, 4 * p + 1, 2), (4 * p, 4 * p + 2, 3))], 1, row)
        a = np.arange(9) * np.pi / 4.0
        px = -(designsigns.DEPTH_M + designsigns.POST_R_M)
        around = np.column_stack([px + designsigns.POST_R_M * np.cos(a), designsigns.POST_R_M * np.sin(a)])
        top = zc + side / 4
        pole = np.vstack([[(cx, cy, -designsigns.SINK_M), (cx, cy, top)] for cx, cy in around])
        add(pole, np.zeros((18, 2)), [t for j in range(8) for t in ((2 * j, 2 * j + 2, 2 * j + 3),
                                                                    (2 * j, 2 * j + 3, 2 * j + 1))], 1, row)
    if not n:
        return None
    return np.vstack(P), np.vstack(T), np.vstack(UV), MATERIALS_ZONE, np.concatenate(M)


def _merged(a, b):
    """Two meshes (positions, triangles, uvs, materials, material of each triangle) as one, b's materials after a's
    (one it shares, its index there)."""
    if b is None:
        return a
    if a is None:
        return b
    p, t, uv, mats, mi = a
    mats = list(mats)
    idx = []
    for m in b[3]:
        if m not in mats:
            mats.append(m)
        idx.append(mats.index(m))
    return (np.vstack([p, b[0]]), np.vstack([t, np.asarray(b[1]) + len(p)]), np.vstack([uv, b[2]]), mats,
            np.concatenate([mi, np.asarray(idx, np.int64)[b[4]]]))


def _heights(frame, elev, xy, road):
    """(ground z, road z) at the signs' spots xy (k, 2) and their road points road (k, 2)."""
    pts = np.vstack([xy, road])
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    h = np.asarray(elev(la, lo), np.float64).ravel()
    z = np.asarray(frame.geodetic_to_enu(la, lo, h)[2], np.float64).ravel()
    return z[:len(xy)], z[len(xy):]


def tile(frame, box, elev, context, signs, decks=(), left_hand=False, mph=True):
    """(place()'s signs without the 274s the zone signs replace, the zone signs' mesh or None) of a road tile's box
    (speedsigns.build_tile, the German junction signs on): the tile within ZONE_REACH_M of Germany (designs.reads_here):
    its zone signs (zones) off the decks, and not the 274s they replace (outside_zones); otherwise signs as they are."""
    x0, y0, x1, y1 = box
    m = ZONE_REACH_M
    if not designs.reads_here(True, frame, (x0 - m, y0 - m, x1 + m, y1 + m)):
        return signs, None
    extra = None
    found = zones(frame, box, context, left_hand, mph)
    signs = signs[outside_zones(frame, signs, found)]
    keep = [k for k in range(len(found.signs)) if not exitsigns._on_deck(found.signs[k, :2], decks, 1.5)]
    if keep:
        zs = found.signs[keep]
        zg, zr = _heights(frame, elev, zs[:, 0:2], zs[:, 7:9])
        extra = de_zone_mesh(np.column_stack([zs[:, 0:2], zg, zr, zs[:, 2:7]]), (float(box[0]), float(box[1]), 0.0))
    return signs, extra
