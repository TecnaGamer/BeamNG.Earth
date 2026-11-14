"""Benches and bins at a bus station (task 020: "under the roof a pedestrian zone, benches, bins"), as forest rows of the
stock bench and bin (furniture.G_BENCH, G_BIN) like the street furniture and the bus shelters.

    furniture(st) -> [(group, x, y, fx, fy)]     pure: where each stands and the direction it faces (frame metres)
    rows(frame, md, box, elev) -> [row]          the rows of the stations near road tile box, the tile's own points only

Data first: a platform tagged bench=yes / bin=yes gets them along it. Under a roof without such a platform the bench row
stands along the back edge, facing the bays, a bin between every second pair. The walking zone itself (a slab under the
roof) is busstationmesh.zone; furniture stands on it.
"""
import math

import numpy as np
import dmath
import dnp

import busbays
import busstations as bs

BENCH, BIN = "bench", "bin"
BENCH_H, BIN_H = 0.9, 1.1               # furniture.py's heights of the two
BENCH_YAW = 90.0                        # the bench model's front is its local +y (furniture.BENCH_YAW)
BENCH_STEP_M = 5.0
BIN_STEP_M = 10.0
BACK_IN_M = 1.2                         # a bench stands this far in from the roof's back edge
MIN_ROOF_W_M = 2.5
MAX_BENCHES, MAX_BINS = 14, 6
PLATFORM_BENCH_STEP_M = 8.0
BIN_AT = 0.05                           # a platform's bin stands at its start (the benches are spread from 1/8 on)
CLEAR_M = 1.6                           # two pieces of furniture are at least this far apart
SLAB_H_M = 0.18


def _roof_axes(ring):
    """(t, left, a0, a1, l0, l1): the unit axis of the roof's longest edge, its left normal and the ring's extents."""
    pts = bs._open(ring)
    segs = np.vstack((pts, pts[:1]))
    best = max(zip(segs[:-1], segs[1:]), key=lambda s: float(dnp.hypot(*(s[1] - s[0]))))
    t = busbays._unit(best[1] - best[0])
    left = np.array([-t[1], t[0]])
    along, lat = dnp.matmul(pts, t), dnp.matmul(pts, left)
    return t, left, float(along.min()), float(along.max()), float(lat.min()), float(lat.max())


FRONT_M = 15.0                          # a bay this near a roof's edge, on its outer side, makes it a front edge
MIN_EDGE_M = 6.0


def _back_edges(st, ring, bays):
    """[(a, b, outward normal)] of the roof's long edges with no bay on their outer side: the ones the benches stand
    along (the buses draw up to the others). Without any bay information the edges on the buildings' side."""
    pts = bs._open(ring)
    if _signed(pts) < 0:
        pts = pts[::-1]
    out = []
    for a, b in zip(pts, np.roll(pts, -1, axis=0)):
        length = float(dnp.hypot(*(b - a)))
        if length < MIN_EDGE_M:
            continue
        t = (b - a) / length
        o = np.array([t[1], -t[0]])                       # the right of a counter-clockwise ring: outward
        mid = (a + b) / 2.0
        front = any(float(dnp.dot(bay.c - mid, o)) > 0 and bs.dist_to_path(np.array([a, b]), bay.c) <= FRONT_M
                    for bay in bays)
        if not bays:                                      # no bays: the side toward a building is the back
            room = min([bs.dist_to_path(it["xy"], mid + o * 4.0, True) for it in st.buildings] or [1e9])
            front = room > 8.0
        if not front:
            out.append((a, b, o, length))
    return out


def _signed(pts):
    x, y = pts[:, 0], pts[:, 1]
    return float(dnp.dot(x, np.roll(y, -1)) - dnp.dot(np.roll(x, -1), y)) * 0.5


def _roof_furniture(st, canopy, bays):
    ring = canopy["ring"]
    if min(np.ptp(bs._open(ring), axis=0)) < MIN_ROOF_W_M:
        return []
    out = []
    for a, b, o, length in sorted(_back_edges(st, ring, bays), key=lambda e: -e[3]):
        t = (b - a) / length
        face = -o
        pos = BENCH_STEP_M / 2.0 + 1.0
        while pos <= length - BENCH_STEP_M / 2.0 - 1.0:
            p = a + t * pos + face * BACK_IN_M
            if bs.contains(ring, p) and len([x for x in out if x[0] == BENCH]) < MAX_BENCHES:
                out.append((BENCH, float(p[0]), float(p[1]), float(face[0]), float(face[1])))
                nb = len([x for x in out if x[0] == BENCH])
                q = p + t * BENCH_STEP_M / 2.0
                if nb % 2 == 0 and len([x for x in out if x[0] == BIN]) < MAX_BINS and bs.contains(ring, q):
                    out.append((BIN, float(q[0]), float(q[1]), float(face[0]), float(face[1])))
            pos += BENCH_STEP_M
    return out


def _platform_furniture(st, platform):
    """A platform tagged bench=yes / bin=yes: benches along its middle line, one bin at its start."""
    tags = platform["tags"]
    xy = np.asarray(platform["xy"], float)
    if platform["kind"] == "polygon":
        t, left, a0, a1, l0, l1 = _roof_axes(xy)
        start, end, mid_lat = t * a0 + left * (l0 + l1) / 2.0, t * a1 + left * (l0 + l1) / 2.0, left
    else:
        start, end, mid_lat = xy[0], xy[-1], None
        t = busbays._unit(end - start)
        mid_lat = np.array([-t[1], t[0]])
    length = float(dnp.hypot(*(end - start)))
    face = mid_lat
    if st.roads:
        mid = (start + end) / 2.0
        d, rq, _ = busbays._nearest(st.roads[0]["xy"], False, mid)
        for r in st.roads[1:]:
            dd, rq2, _ = busbays._nearest(r["xy"], False, mid)
            if dd < d:
                d, rq = dd, rq2
        face = mid_lat if float(dnp.dot(rq - mid, mid_lat)) >= 0 else -mid_lat
    out = []
    if tags.get("bench") == "yes" and length >= 3.0:
        k = int(min(4, max(1, length // PLATFORM_BENCH_STEP_M)))
        for i in range(k):
            p = start + (end - start) * ((i + 0.5) / k)
            out.append((BENCH, float(p[0]), float(p[1]), float(face[0]), float(face[1])))
    if tags.get("bin") == "yes" and length >= 3.0:
        p = start + (end - start) * BIN_AT
        out.append((BIN, float(p[0]), float(p[1]), float(face[0]), float(face[1])))
    return out


BAY_BENCH_IN_M = 0.9                    # a waiting bench stands this far in from the bay's kerb edge
BAY_BENCH_NEAR_M = 3.5                  # ... and not where a bench stands this near (the roofs' own sit by the back wall)
BAY_BENCH_MAX = 10


def bay_benches(st, have):
    """[(BENCH, x, y, fx, fy)]: the benches the station adds on its own: the stations OSM maps with no bench at all stand bare
    (the user 2026-11-09, Nachod: "no benches"). One at every second bay (the odd ones: the shelters take the even ones),
    backed away from the lane, facing the bays; never within BAY_BENCH_NEAR_M of a mapped bench (`have`: [(x, y)]), in a
    building, in a lane or a mouth."""
    import busstationmesh
    out = []
    for k, bay in enumerate(busbays.layout_bays(st)):
        if len(out) >= BAY_BENCH_MAX:
            break
        if k % SHELTER_EVERY == 0 and k:
            continue
        if k % 2 == 0 and len(busbays.layout_bays(st)) > 1:
            continue
        p = bay.c - bay.n * (bay.width / 2.0 + busbays.KERB_GAP_M + BAY_BENCH_IN_M)
        if any(bs.contains(b["xy"], p) for b in st.buildings):
            continue
        if not bool(busstationmesh._walk_ok(st, p[None])[0]):
            continue
        if any(float(dnp.hypot(p[0] - q[0], p[1] - q[1])) < BAY_BENCH_NEAR_M for q in list(have) + [(o[1], o[2]) for o in out]):
            continue
        out.append((BENCH, float(p[0]), float(p[1]), float(bay.n[0]), float(bay.n[1])))
    return out


def furniture(st):
    """[(group, x, y, fx, fy)] of the station: tagged platforms' own, then rows under the roofs that no tagged platform
    lies under; none closer than CLEAR_M to another."""
    out = []
    covered = []
    for pl in st.platforms:
        got = _platform_furniture(st, pl)
        if got:
            out += got
            covered.append(pl)
    bays = busbays.layout_bays(st)
    for c in st.canopies:
        if any(bs.contains(c["ring"], bs.centroid(p["xy"])) or c["src"] == p["id"] for p in covered):
            continue
        out += _roof_furniture(st, c, bays)
    out += bay_benches(st, [(o[1], o[2]) for o in out if o[0] == BENCH])
    if out and (st.lanes or st.mouths):                 # nothing stands in a drive lane or a mouth
        ok = ~bs.lane_blocked(st, np.array([[o[1], o[2]] for o in out]), pad=0.3, mouths=False)
        out = [o for o, k in zip(out, ok) if k]
    kept = []
    shelters = shelter_spots(st)
    for item in out:
        if any(float(dnp.hypot(item[1] - s_[0], item[2] - s_[1])) < 2.2 for s_ in shelters):
            continue                                    # (not in a waiting shelter)
        if all(float(dnp.hypot(item[1] - k[1], item[2] - k[2])) >= CLEAR_M for k in kept):
            kept.append(item)
    return kept


LAMP, LAMP_H = "streetlight", 9.0      # furniture.G_LAMP and its catalogue height
LAMP_KERB_IN_M = 0.9                    # the pole stands this far in from the bay near side (0.6 m on the platform)
LAMP_SPACING_M = 15.0                   # lamps at least this far apart
LAMP_EVERY_OTHER_FROM = 6               # a station with more bays than this lights every other one


def lamps(st):
    """[(x, y, dx, dy, on_slab)]: street lamps on the platform edge by the bays, their arms (dx, dy) over the bays; not
    under a roof (fixtures hang there: busstationmesh.roofs) nor in a building."""
    bays = busbays.layout_bays(st)
    out = []
    step = 2 if len(bays) > LAMP_EVERY_OTHER_FROM else 1
    for bay in bays[::step]:
        p = bay.c - bay.n * (bay.width / 2.0 + LAMP_KERB_IN_M) - bay.t * bay.length / 2.0
        if any(bs.contains(c["ring"], p) for c in st.canopies) or any(bs.contains(b["xy"], p) for b in st.buildings):
            continue
        if (st.lanes or st.mouths) and bool(bs.lane_blocked(st, p[None], pad=0.3, mouths=False)[0]):
            continue
        if any(float(dnp.hypot(p[0] - q[0], p[1] - q[1])) < LAMP_SPACING_M for q in out):
            continue
        slab = any(bs.dist_to_path(pl["xy"], p, pl["kind"] == "polygon") <= 2.5 or
                   (pl["kind"] == "polygon" and bs.contains(pl["xy"], p)) for pl in st.platforms)
        out.append((float(p[0]), float(p[1]), float(bay.n[0]), float(bay.n[1]), slab))
    return out + yard_lamps(st, out)


YARD_LAMP_STEP_M = 30.0                 # along the yard's edge
YARD_LAMP_IN_M = 1.4                    # in from the yard's edge (a pole stands on the kerb, not in the aisle)
YARD_LAMP_APART_M = 24.0                # yard lamps at least this far apart; bay lamps this far from them: LAMP_SPACING_M
YARD_LAMP_MOUTH_M = 16.0                # a mouth gets a lamp within this of its point (an exit stands lit)
YARD_LAMP_OUTER_M = 4.0                 # a yard lamp stands within this of the yard's outline (its hull)
YARD_LAMP_BAY_M = 18.0                  # ... and this far from the bays' own lamps (those light the bays already)


def _edge_points(st, step=3.0):
    """[(point, inward unit vector)] along the edge of the yard (the outline of its rectangles' union): points of a
    rectangle's edge whose outer side (1.2 m out) is not yard, pulled YARD_LAMP_IN_M in."""
    import busyard
    import busstationkeep
    polys = [np.asarray(r, float) for r in busyard.yard_polys(st)]
    if not polys:
        return []
    keep = busstationkeep.Keepout(polys)
    cand, probe = [], []
    for r in polys:
        area = float(dnp.dot(r[:, 0], np.roll(r[:, 1], -1)) - dnp.dot(np.roll(r[:, 0], -1), r[:, 1]))
        for a, b in zip(r, np.roll(r, -1, axis=0)):
            ln = float(dnp.hypot(*(b - a)))
            if ln < 1e-6:
                continue
            t = (b - a) / ln
            inward = np.array([-t[1], t[0]]) * (1.0 if area >= 0 else -1.0)
            k = max(1, int(ln // step))
            for i in range(k):
                q = a + (b - a) * ((i + 0.5) / k)
                cand.append((q + inward * YARD_LAMP_IN_M, inward))
                probe.append(q - inward * 1.2)
    if not cand:
        return []
    # (the probes 1.2 m out tested at once: point by point, a city's yard (thousands of edge points, each against every
    # rectangle) was 8 s of a road tile at Port Authority; the same answer per point)
    shared = keep.contains(np.asarray(probe, float), 0.0)       # (an edge shared with the next rectangle: not the yard's)
    return [c for c, s in zip(cand, shared.tolist()) if not s]


def yard_lamps(st, taken=()):
    """[(x, y, dx, dy, on_slab)]: street lamps standing along the yard's edge, never in its open asphalt (the user
    2026-11-09: the poles stood in the middle of the yard): every YARD_LAMP_STEP_M of the edge, off the drive lanes, the
    roofs and the buildings, their arms over the yard; each mouth gets one near it. The bays' own lamps (taken) are kept
    apart."""
    cached = st.__dict__.get("_yard_lamps")
    if cached is not None and tuple(taken) == cached[0]:
        return list(cached[1])
    got = _yard_lamps(st, taken)
    st._yard_lamps = (tuple(taken), got)
    return list(got)


def _yard_lamps(st, taken):
    out = []
    pts = _edge_points(st)
    if not pts:
        return out
    walls = [bs._open(np.asarray(it["xy"], float)) for it in st.buildings] + [np.asarray(o, float) for o in st.obstacles]
    P = np.array([p for p, _ in pts])
    import busyard
    import busstationmesh
    hull = bs.hull(np.vstack([np.asarray(r, float) for r in busyard.yard_polys(st)]))
    plats = busstationmesh._platform_rings(st)
    # the yard's outer edge only (the edges round a lawn or a building inside it stood the poles in the middle of the
    # yard: Rakovnik, the user's shots 2026-11-09), or a platform
    free = np.array([bs.dist_to_path(hull, p, True) <= YARD_LAMP_OUTER_M or
                     any(bs.contains(r, p) for r in plats) for p in P], bool) if len(hull) >= 3 else np.ones(len(P), bool)
    if st.lanes or st.mouths:
        free &= ~bs.lane_blocked(st, P, pad=0.6, core=True, mouths=False)
    for i in np.flatnonzero(free):
        p = P[i]
        if any(bs.contains(cn["ring"], p) for cn in st.canopies) or any(len(w) >= 3 and bs.contains(w, p) for w in walls):
            free[i] = False

    def take(i):
        p, inward = pts[i]
        slab = any(bs.dist_to_path(pl["xy"], p, pl["kind"] == "polygon") <= 2.5 or
                   (pl["kind"] == "polygon" and bs.contains(pl["xy"], p)) for pl in st.platforms)
        out.append((float(p[0]), float(p[1]), float(inward[0]), float(inward[1]), slab))

    def apart(i, d):
        q = P[i]
        return all(float(dnp.hypot(q[0] - o[0], q[1] - o[1])) >= d for o in out) and             all(float(dnp.hypot(q[0] - t[0], q[1] - t[1])) >= YARD_LAMP_BAY_M for t in taken)
    for mx, my in st.mouths:                                    # the exits first
        near = [i for i in np.flatnonzero(free) if float(dnp.hypot(P[i][0] - mx, P[i][1] - my)) <= YARD_LAMP_MOUTH_M]
        near.sort(key=lambda i: float(dnp.hypot(P[i][0] - mx, P[i][1] - my)))
        for i in near:
            if apart(i, YARD_LAMP_APART_M):
                take(i)
                break
    cand = [i for i in np.flatnonzero(free)]
    chosen_s = 0.0
    for i in cand:                                              # (the edge in the order its rectangles were made)
        if apart(i, YARD_LAMP_STEP_M):
            take(i)
    return out


LAMP_GROUPS = None


def _lamp_groups():
    global LAMP_GROUPS
    if LAMP_GROUPS is None:
        import citylamps
        import furniture
        LAMP_GROUPS = frozenset({furniture.G_LAMP, furniture.G_LENS, furniture.G_GLOW, *citylamps.GROUPS})
    return LAMP_GROUPS


POLE_GROUPS = frozenset({"pole", "polelamp"})          # furniture.G_POLE, G_POLELAMP: utility poles along the streets


def _is_pole(group):
    """A utility pole's group: the stock ones and the regional styles (poleprofiles: pole_concrete_recessed, fr_wood_pole,
    eu_wood_pole, eu_pole_<family>; the Czech A-frame poles stood on Trutnov's platforms under these names)."""
    g = str(group)
    return g in POLE_GROUPS or g.startswith(("pole_", "eu_pole_")) or g.endswith("_pole")


def drop_stock_lamps(rows, frame, md, box, wires=None):
    """rows without the street lamps (and their lenses and glows) and the utility poles that stand on a bus station's
    ground, and wires without the spans that end at such a pole: the station lights its yard itself (yard_lamps, lamps),
    and the poles along the yard's service lanes stood on the platforms with their wires over the roofs (the user's shot
    of Trutnov 2026-11-09). The rows we add are not in rows."""
    if not rows or not bs.ON:
        return rows
    import busstationkeep
    keep = busstationkeep.keepout(md, frame, box)
    if not keep:
        return rows
    groups = _lamp_groups()
    idx = [i for i, r in enumerate(rows) if r[0] in groups or _is_pole(r[0])]
    if not idx:
        return rows
    hit = keep.contains(np.array([[rows[i][1], rows[i][2]] for i in idx]), 0.0)
    gone = {i for i, h in zip(idx, hit) if h}
    if wires is not None and gone:
        poles = np.array([[rows[i][1], rows[i][2]] for i in gone if _is_pole(rows[i][0])])
        if len(poles):
            kept = []
            for w in wires:
                line = np.asarray(w[0] if isinstance(w, tuple) else w, float)
                ends = line[[0, -1], :2] if len(line) else np.zeros((0, 2))
                near = len(ends) and (dnp.hypot(*(ends[:, None, :] - poles[None, :, :]).transpose(2, 0, 1)) < 4.0).any()
                if not near:
                    kept.append(w)
            wires[:] = kept
    return [r for i, r in enumerate(rows) if i not in gone]


SHELTER, SHELTER_H = "busshelter", 2.9   # shelters.G_SHELTER (the stock glass shelter: local +x away from the road)
SHELTER_IN_M = 1.4                      # its origin stands this far in from the bay's kerb edge (the shape's depth 0.74 + room)
SHELTER_EVERY = 2                       # a shelter at every second bay ...
SHELTER_APART_M = 9.0                   # ... at least this far apart
SHELTER_MAX = 12
SHELTER_ROOF_M = 5.0                    # not within this of a roof (a mapped shelter is a roof: Rakovnik's)


def shelter_spots(st):
    """[(x, y, yaw)] of the waiting shelters the station adds: on a platform that has no roof over the bay (a covered bay
    waits under the roof), every SHELTER_EVERY bays, backed away from the lane, never in a lane, a mouth, a building or on
    a bench or plate spot. The map's own bus stop shelters on the station's ground are dropped (busstationkeep), so this
    is the station's own (the user's list 2026-11-09: islands without a roof stood bare). Memoised on the station."""
    got = st.__dict__.get("_shelters")
    if got is not None:
        return got
    import busstationmesh
    out = []
    bays = busbays.layout_bays(st)
    for k, bay in enumerate(bays):
        if len(out) >= SHELTER_MAX:
            break
        if k % SHELTER_EVERY:
            continue
        p = bay.c - bay.n * (bay.width / 2.0 + busbays.KERB_GAP_M + SHELTER_IN_M)
        if any(bs.contains(c["ring"], p) or bs.dist_to_path(c["ring"], p, True) < SHELTER_ROOF_M for c in st.canopies)                 or any(bs.contains(b["xy"], p) for b in st.buildings):
            continue
        if not bool(busstationmesh._walk_ok(st, p[None])[0]):
            continue
        if (st.lanes or st.mouths) and bool(bs.lane_blocked(st, p[None], pad=0.5, mouths=True)[0]):
            continue
        if any(float(dnp.hypot(p[0] - q[0], p[1] - q[1])) < SHELTER_APART_M for q in [(o[0], o[1]) for o in out]):
            continue
        away = -bay.n
        out.append((float(p[0]), float(p[1]), float(math.degrees(dmath.atan2(away[1], away[0])) % 360.0)))
    st._shelters = out
    return out


GIVEWAY, GIVEWAY_H = "giveway", 3.0    # furniture.G_YIELD (trees.lua: the Italian triangle, EU-shaped) and its height
EXIT_SIGN_BACK_M = 2.5                  # the sign stands this far before the lane's end
EXIT_SIGN_SIDE_M = 0.9                  # ... this far beyond the drawn lane's right edge
EXIT_SIGN_CLEAR_M = 10.0                # none where a sign of the street's own stands this near


STREET_HALF_M = {"motorway": 7.0, "trunk": 6.0, "primary": 5.0, "secondary": 4.6, "tertiary": 4.2}   # a carriageway's half width
STREET_HALF_DEFAULT_M = 3.6
SIGN_STREET_CLEAR_M = 0.9               # a sign stands this far off the carriageway's edge


def _off_streets(st, p):
    """True when the point is off every public street's carriageway: the give-way signs
    stood in the middle of the street at a mouth (the user 2026-11-09: "signs right in the middle of the road"; the yard's own
    lanes are the caller's)."""
    for it in st.streets:
        path = np.asarray(it["xy"], float)
        if len(path) < 2:
            continue
        half = STREET_HALF_M.get(str(it.get("cls", "")).split("_")[0], STREET_HALF_DEFAULT_M)
        if float(bs.dist_to_path(path, p)) < half + SIGN_STREET_CLEAR_M:
            return False
    return True


def exit_signs(st):
    """[(x, y, yaw)]: a give-way sign on the drivers' right where each lane leaves the yard onto a street (a lane's end, or
    a two-way lane's start, at a mouth), facing the drivers. Memoised on the station."""
    got = st.__dict__.get("_exit_signs")
    if got is not None:
        return got
    import furniture as streetfurniture
    out = []
    for ln in st.lanes:
        path = np.asarray(ln["xy"], float)
        if len(path) < 2:
            continue
        ends = [path] if ln["oneway"] else [path, path[::-1]]
        for p in ends:
            end = p[-1]
            if not any(float(dnp.hypot(end[0] - mx, end[1] - my)) < 1.0 for mx, my in st.mouths):
                continue
            t = p[-1] - p[-2]
            n = float(dnp.hypot(*t))
            if n < 1e-6:
                continue
            t = t / n
            side, yaw = streetfurniture.approach_pose(-t, GIVEWAY)
            q = None
            for more in (0.0, 1.0, 2.5, 4.0, 6.0):      # (off any other lane's drawn width and off every street's carriageway)
                c = end - t * EXIT_SIGN_BACK_M + side * (ln["core"] + EXIT_SIGN_SIDE_M + more)
                if not bool(bs.lane_blocked(st, c[None], pad=0.3, core=True, mouths=False)[0]) and _off_streets(st, c):
                    q = c
                    break
            if q is None or any(float(dnp.hypot(q[0] - o[0], q[1] - o[1])) < 4.0 for o in out):
                continue
            out.append((float(q[0]), float(q[1]), float(yaw)))
    st._exit_signs = out
    return out


def rows(frame, md, box, elev, country_at=None, existing=None):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the benches, bins and lamps whose point is in tile box.
    country_at(x, y, z) -> ISO code or None (poleprofiles.lamp_country_selector): the lamps become the local posts."""
    if not bs.ON:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    pend, lamp_pend, sign_pend, shelter_pend = [], [], [], []
    for st in bs.stations_near(md, frame, box):
        sign = bs.sign_spot(st) if st.name else None
        board = bs.board_spot(st)
        for group, x, y, fx, fy in furniture(st):
            if group == BENCH and any(s is not None and abs(x - s[0]) < 0.01 and abs(y - s[1]) < 0.01
                                      for s in (sign, board)):
                continue                                # (the name sign or the board stands in its place)
            if x0 <= x < x1 and y0 <= y < y1:
                pend.append((group, x, y, fx, fy))
        for x, y, dx, dy, slab in lamps(st):
            if x0 <= x < x1 and y0 <= y < y1:
                lamp_pend.append((x, y, dx, dy, slab))
        for x, y, yaw in shelter_spots(st):
            if x0 <= x < x1 and y0 <= y < y1:
                shelter_pend.append((x, y, yaw))
        for x, y, yaw in exit_signs(st):
            if not (x0 <= x < x1 and y0 <= y < y1):
                continue
            if existing and any(r[0] in (GIVEWAY, "stop", "fr_giveway_m9c", "giveway_b1") and
                                float(dnp.hypot(r[1] - x, r[2] - y)) < EXIT_SIGN_CLEAR_M for r in existing):
                continue                                # (the street's own junction sign is there)
            sign_pend.append((x, y, yaw))
    if not pend and not lamp_pend and not sign_pend and not shelter_pend:
        return []
    import busstationmesh
    import furniture as streetfurniture
    out = []
    if pend:
        z = busstationmesh.ground(frame, elev, np.array([[p[1], p[2]] for p in pend])) + SLAB_H_M
        for (group, x, y, fx, fy), zz in zip(pend, z):
            yaw = math.degrees(dmath.atan2(fy, fx)) + (BENCH_YAW if group == BENCH else 0.0)
            out.append([group, round(x, 2), round(y, 2), round(float(zz), 2), round(yaw % 360.0, 1), 1.0,
                        BENCH_H if group == BENCH else BIN_H, 0.0])
    if lamp_pend:
        z = busstationmesh.ground(frame, elev, np.array([[p[0], p[1]] for p in lamp_pend]))
        lamp_rows = [[LAMP, round(x, 2), round(y, 2), round(float(zz) + (SLAB_H_M if slab else 0.0), 2),
                      round(math.degrees(dmath.atan2(dy, dx)) % 360.0, 1), 1.0, LAMP_H, 0.0]
                     for (x, y, dx, dy, slab), zz in zip(lamp_pend, z)]
        if country_at is not None:
            import citylamps
            lamp_rows = citylamps.localize(lamp_rows, country_at)
        out += lamp_rows + streetfurniture.lamp_lenses(lamp_rows)
    if shelter_pend:
        z = busstationmesh.ground(frame, elev, np.array([[p[0], p[1]] for p in shelter_pend]))
        out += [[SHELTER, round(x, 2), round(y, 2), round(float(zz) + SLAB_H_M, 2), round(yaw, 1), 1.0, SHELTER_H, 0.0]
                for (x, y, yaw), zz in zip(shelter_pend, z)]
    if sign_pend:
        z = busstationmesh.ground(frame, elev, np.array([[p[0], p[1]] for p in sign_pend]))
        out += [[GIVEWAY, round(x, 2), round(y, 2), round(float(zz), 2), round(yaw % 360.0, 1), 1.0, GIVEWAY_H, 0.0]
                for (x, y, yaw), zz in zip(sign_pend, z)]
    return out


SHELTER_BAY_M = 2.0         # a stop's shelter this near a bay's outline belongs to the station


def clear_of_stations(shelter_rows, frame, md, box):
    """The bus shelters [group, x, y, ...] that are not part of a station: not under its roofs, not on its platforms nor
    in or beside its bays (the station builds its own roofs, benches and plates; the stop's shelter stood beside them)."""
    if not shelter_rows or not bs.ON:
        return shelter_rows
    stations = bs.stations_near(md, frame, box)
    if not stations:
        return shelter_rows
    import busstationkeep
    keep = busstationkeep.of_stations(stations)
    out = []
    for row in shelter_rows:
        p = np.array([row[1], row[2]], float)
        hit = bool(keep.contains(p[None])[0])          # (the yard, the platforms, the roofs, 1.5 m round them)
        for st in stations:
            if hit:
                break
            if any(bs.contains(c["ring"], p) for c in st.canopies) or                     any(pl["kind"] == "polygon" and bs.contains(pl["xy"], p) for pl in st.platforms):
                hit = True
            else:
                for bay in busbays.layout_bays(st):
                    if float(dnp.hypot(*(bay.c - p))) <= bay.length / 2 + bay.width / 2 + SHELTER_BAY_M and                             bs.contains(busbays._rect(bay), p):
                        hit = True
                        break
            if hit:
                break
        if not hit:
            out.append(row)
    return out
