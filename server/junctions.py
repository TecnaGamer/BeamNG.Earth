"""Street intersections: curb returns (rounded corners), crosswalks and curb ramps (docs/ARCHITECTURE.md
"Raised sidewalks"; the user, 2026-10-11: "the sidewalks have sharp corners at the intersections and
don't join together seamlessly, also there are no crosswalks or ramps leading to the crosswalks").

Overture splits roads at their junctions, so a junction is a node shared by several road centrelines.
At a node the branches (each road leaving it) are sorted by angle; between two neighbouring branches
i (its left side) and j (its right side) whose roads both have sidewalks and which meet at an angle
within CORNER_ANGLES, the corner is:
    - a curb return: an arc of radius RADIUS tangent to both curb lines (curb line = the carriageway
      edge); the sidewalks are cut exactly at the tangent points (a cross-section square to their
      road), and a corner piece fills between: the curb along the arc, the slab out to the corner of
      the two sidewalks' outer edges;
    - the asphalt between the arc and the curb lines' corner (the old square corner) filled.
On each branch with sidewalks on both sides, a crosswalk (WCUSA crossing_white) crosses the road just
outside its corners, and both sidewalks get a curb ramp there (the curb down to the road, the slab
rising back over RAMP_RUN, a tactile pad on the slope).
Pure functions of the road centrelines in the tile frame; deterministic (sorted inputs).
"""
import math

import numpy as np

import fast     # Rust kernels (earthfast)

RADIUS = 5.0                    # m, curb return radius when the class is not listed in RADII
# by the larger road's class (street-view study of Fells Point, 2026-10-11: city corners are tight)
RADII = {"residential": 3.0, "living_street": 3.0, "unclassified": 3.0, "service": 3.0,
         "tertiary": 4.5, "secondary": 6.0, "primary": 6.0, "trunk": 8.0}
STOP_LINE = {"m": "line_white", "tl": 6.4, "p": 2, "w": 0.45}   # 1 m before the crosswalk
STOP_GAP = 1.0
BULB_M = 6.0                    # m a curb extension runs along the street past the corner
TAPER_M = 3.0                   # m over which it returns to the parking curb
CORNER_ANGLES = (15.0, 150.0)   # degrees between two branches for a curb return (an acute one under ACUTE_DEG:
                                # its sidewalks met in a sharp wedge, the user's report #46, Fells Point 2026-10-17)
RADIUS_TRIES = (1.0, 0.5, 0.25)  # a corner's radius tried at these shares of it until it fits
ACUTE_DEG = 50.0                # under this the return's radius shrinks so it starts within ACUTE_T_M of the
ACUTE_T_M = 9.0                 # carriageways' corner (radius / tan(theta / 2) from it), at least ACUTE_R_MIN,
ACUTE_R_MIN = 0.8               # and the corner gets no curb extension
BRANCH_M = 10.0                 # m of a branch used for its direction
CROSSWALK = {"m": "crossing_white", "tl": 4.8, "p": 3, "w": 3.0}
CW_GAP = 0.5                    # m between a corner's tangent point and its crosswalk
NODE_TOL = 0.5                  # m: centreline vertices this close are one node
MERGE_M = 3.0                   # m: junction nodes this close are one junction
CONNECTOR_M = 0.0              # m: ... and nodes joined by a road shorter than this
CONNECTOR_WIDTHS = 1.0          # ... or by a connector no longer than this many times the widest road at its two
                                # nodes: an offset junction lying within the carriageway (Fells Point, 10 m between
                                # two nodes of 10.6 m streets, 2026-10-17)


def _unit(v):
    n = math.hypot(v[0], v[1])
    return v / n if n > 1e-9 else v


def _left(u):
    return np.array([-u[1], u[0]])


def _line_x(p, d, q, e):
    """Intersection of lines p + t d and q + s e, or None if parallel."""
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-9:
        return None
    w = q - p
    t = (w[0] * e[1] - w[1] * e[0]) / den
    return p + t * d


def _piece(P, u1, w1, u2, w2, r):
    """The curb from a road along u1 (its left side, half width w1) round to one along u2 (its right side,
    w2), both from P: (points (k, 2), t1, t2, the road edges' meeting point or None): a curb return of radius
    r, or one point where the two roads run on straight (over CORNER_ANGLES[1] between them)."""
    n1, n2 = _left(u1), _left(u2)
    theta = math.degrees((math.atan2(u2[1], u2[0]) - math.atan2(u1[1], u1[0])) % (2 * math.pi))
    if theta >= CORNER_ANGLES[1]:
        return (P + n1 * w1)[None], 0.0, 0.0, None
    Z = _line_x(P + n1 * (w1 + r), u1, P - n2 * (w2 + r), u2)
    if Z is None:
        return None
    T1, T2 = Z - n1 * r, Z + n2 * r
    a0 = math.atan2(*(T1 - Z)[::-1])
    a1 = math.atan2(*(T2 - Z)[::-1])
    da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    k = max(3, int(abs(da) * r / 0.75) + 1)
    ang = a0 + da * np.linspace(0.0, 1.0, k)
    arc = np.c_[Z[0] + r * np.cos(ang), Z[1] + r * np.sin(ang)]
    return arc, float((T1 - P) @ u1), float((T2 - P) @ u2), _line_x(P + n1 * w1, u1, P - n2 * w2, u2)


def _branch_dir(pts, vi, step):
    """Unit direction leaving vertex vi along pts (step +1 forward, -1 backward), over BRANCH_M."""
    p0 = pts[vi, :2]
    k, acc, q = vi, 0.0, p0
    while 0 <= k + step < len(pts) and acc < BRANCH_M:
        k += step
        acc += math.hypot(*(pts[k, :2] - pts[k - step, :2]))
        q = pts[k, :2]
    return _unit(q - p0)


def _z_along(pts, vi, step, t):
    """The centreline height t m from vertex vi along the branch."""
    k, acc = vi, 0.0
    while 0 <= k + step < len(pts):
        seg = math.hypot(*(pts[k + step, :2] - pts[k, :2]))
        if acc + seg >= t:
            f = (t - acc) / seg if seg > 0 else 0.0
            return float(pts[k, 2] + f * (pts[k + step, 2] - pts[k, 2]))
        acc += seg
        k += step
    return float(pts[k, 2])


def _seg_dist(p, line):
    """Distance from points p (m, 2) to a polyline (k, 2)."""
    a, b = line[:-1], line[1:]
    ab = b - a
    L2 = np.maximum((ab ** 2).sum(1), 1e-12)
    t = np.clip(((p[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0, 1)
    q = a[None] + t[..., None] * ab[None]
    return np.hypot(*(p[:, None, :] - q).transpose(2, 0, 1)).min(axis=1)


_BOXES = {}                     # id(roads list) -> (that list, its boxes (n, 4))


def _boxes(roads):
    """(n, 4) x0 y0 x1 y1 of each road's line, made once per roads list (find() asks ~70 times a tile)."""
    got = _BOXES.get(id(roads))
    if got is not None and got[0] is roads:
        return got[1]
    # every line at once (min / max over each line's run of points: four numpy calls per road took ~0.5 % a tile)
    import mapgeom
    lines = [ln for _, _, ln, _ in roads]
    b = np.empty((len(lines), 4))
    if lines:
        n = np.array([len(ln) for ln in lines], np.int64)
        allp = np.concatenate([np.asarray(ln, np.float64)[:, :2] for ln in lines])
        a = np.concatenate(([0], np.cumsum(n)[:-1]))
        b[:, 0] = mapgeom._reduce(np.minimum, allp[:, 0], a, a + n)
        b[:, 1] = mapgeom._reduce(np.minimum, allp[:, 1], a, a + n)
        b[:, 2] = mapgeom._reduce(np.maximum, allp[:, 0], a, a + n)
        b[:, 3] = mapgeom._reduce(np.maximum, allp[:, 1], a, a + n)
    if len(_BOXES) >= 16:
        _BOXES.clear()
    _BOXES[id(roads)] = (roads, b)      # (the list kept: its id cannot be another list's meanwhile)
    return b


def _on_carriageway(pts, roads, near, margin=0.3, half=None, skip=()):
    """The fid of a road with any point (m, 2) on its carriageway (centreline distance < width / 2 -
    margin), among the roads within `near` (centre (2,), radius), or None. half: {fid: half width}
    overrides; skip: fids not checked."""
    c, r = near
    half = half or {}
    fast.require(0, "junctions any_within")
    b = _boxes(roads)
    near_i = np.flatnonzero((b[:, 2] >= c[0] - r) & (b[:, 3] >= c[1] - r) & (b[:, 0] <= c[0] + r)
                            & (b[:, 1] <= c[1] + r))
    for i in near_i:
        fid, spec, line, lon = roads[i]
        if fid in skip:
            continue
        xy = line[:, :2]
        h = half.get(fid, spec.width / 2) - margin
        if fast._ef.any_within(np.ascontiguousarray(pts, np.float64)[:, :2], np.ascontiguousarray(xy, np.float64), h):
            return fid
    return None


def _branch_len(pts, vi, step):
    k, acc = vi, 0.0
    while 0 <= k + step < len(pts):
        acc += math.hypot(*(pts[k + step, :2] - pts[k, :2]))
        k += step
    return acc


def _crosses(a, b, line):
    """Whether segment a-b crosses the polyline (k, 2)."""
    P, Q = line[:-1], line[1:]
    def side(p, q, x):
        return (q[..., 0] - p[..., 0]) * (x[..., 1] - p[..., 1]) - (q[..., 1] - p[..., 1]) * (x[..., 0] - p[..., 0])
    d1, d2 = side(P, Q, a), side(P, Q, b)
    d3, d4 = side(a, b, P), side(a, b, Q)
    return bool(((d1 * d2 < 0) & (d3 * d4 < 0)).any())


def _across(roads, bi, bj, cr, X, walk_w, debug=None):
    """The corner from branch bi (its left side) to branch bj (its right side), whose nodes the road cr joins:
    (corner dict as find's, cut on bi, cut on bj) or None (the pieces do not fit, or the corner would lie on a
    carriageway). No curb extensions here: the curb at the full width."""
    _, ri, vi, si, ui, _, Pi = bi
    _, rj, vj, sj, uj, _, Pj = bj
    spi, spj, spc = roads[ri][1], roads[rj][1], roads[cr][1]
    d = Pj - Pi
    Lc = math.hypot(*d)
    if Lc < 1e-6:
        return None
    uc = d / Lc
    wi, wj, wc = spi.width / 2, spj.width / 2, spc.width / 2
    r = max(RADII.get(spi.cls, RADIUS), RADII.get(spj.cls, RADIUS), RADII.get(spc.cls, RADIUS))
    p1 = _piece(Pi, ui, wi, uc, wc, r)
    p2 = _piece(Pj, -uc, wc, uj, wj, r)
    if p1 is None or p2 is None:
        return None
    (a1, ti, tc1, C1), (a2, tc2, tj, C2) = p1, p2
    # a branch running on straight from the connector: the other node's curb return may end on it, past its node
    if len(a1) == 1 and len(a2) > 1 and tc2 > Lc:
        a1, ti, tc1, C1 = a1[:0], tc2 - Lc, 0.0, None
        tc2 = Lc
    elif len(a2) == 1 and len(a1) > 1 and tc1 > Lc:
        a2, tj, tc2, C2 = a2[:0], tc1 - Lc, 0.0, None
        tc1 = Lc
    if ti < 0 or tj < 0 or tc1 < 0 or tc2 < 0 or tc1 + tc2 > Lc + 0.25:
        if debug is not None:
            debug.append(('across: no fit', round(ti, 1), round(tc1, 1), round(tc2, 1), round(tj, 1), round(Lc, 1), X.round(1).tolist()))
        return None
    if _branch_len(roads[ri][2], vi, si) < ti + 1.0 or _branch_len(roads[rj][2], vj, sj) < tj + 1.0:
        return None
    curb2 = np.vstack([a1, a2])
    keep = np.concatenate(([True], np.hypot(*np.diff(curb2, axis=0).T) > 0.05))
    curb2 = curb2[keep]
    if len(curb2) < 2:
        return None
    zi, zj = _z_along(roads[ri][2], vi, si, ti), _z_along(roads[rj][2], vj, sj, tj)
    s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(curb2, axis=0).T))))
    z = zi + (zj - zi) * s / max(s[-1], 1e-9)
    curb = np.c_[curb2, z]
    g = np.gradient(curb2, axis=0)
    nrm = np.stack([-g[:, 1], g[:, 0]], axis=1)
    nrm /= np.maximum(np.hypot(*nrm.T), 1e-9)[:, None]
    flip = ((curb2 - X) * nrm).sum(1) < 0           # toward the sidewalk: away from the junction
    nrm[flip] = -nrm[flip]
    wki = spi.walk if walk_w is None else walk_w
    wkj = spj.walk if walk_w is None else walk_w
    wkc = max(wki, wkj)
    ni, nj, nc = _left(ui), _left(uj), _left(uc)
    Oi = _line_x(Pi + ni * (wi + wki), ui, Pi - nc * (wc + wkc), uc)
    Oj = _line_x(Pj - nc * (wc + wkc), -uc, Pj - nj * (wj + wkj), uj)
    Oi = Pi + ni * (wi + wki) if Oi is None or a1.shape[0] == 1 else Oi
    Oj = Pj - nj * (wj + wkj) if Oj is None or a2.shape[0] == 1 else Oj
    if math.hypot(*(Oi - X)) > 20.0 + Lc or math.hypot(*(Oj - X)) > 20.0 + Lc:
        return None                                 # a needle at a very sharp turn
    zo = 0.5 * (zi + zj)
    mids = [Oj] if len(a1) == 0 else [Oi] if len(a2) == 0 else [Oj, Oi]
    outer = [(*(Pj + uj * tj - nj * (wj + wkj)), zj)] + [(*o, zo) for o in mids] + \
        [(*(Pi + ui * ti + ni * (wi + wki)), zi)]
    probe = np.vstack([curb2 + nrm * 0.8, curb2 + nrm * 2.0, mids])
    hit = _on_carriageway(probe, roads, (X, 60.0), half={roads[ri][0]: wi, roads[rj][0]: wj, roads[cr][0]: wc})
    if hit is not None:
        if debug is not None:
            debug.append(('across: on carriageway', hit[:8], X.round(1).tolist()))
        return None
    fills = []
    for C, arc in ((C1, a1), (C2, a2)):
        if C is not None and len(arc) > 1:
            fills.append(np.vstack([np.r_[C, zo]] + [np.r_[q, zo] for q in arc]))
    return {"arc": curb, "inward": nrm, "outer": outer, "fill": None, "fills": fills, "ramps": []}, ti, tj


def find(roads, has_walk, walk_w, parking_m=0.0, has_parking=lambda spec: False, debug=None):
    """Corners, cuts and crosswalks of the junctions among `roads` [(fid, spec, pts (k, 3), lon)].
    has_walk(spec) -> bool; walk_w: sidewalk width. Returns
        corners: [dict(arc (n, 3) curb points, inward (n, 2) unit vectors toward the sidewalk,
                       outer [(x, y, z) ...] the slab's outer corner points, fill (m, 3) asphalt polygon)]
        cuts: {(fid, side): [(node xy, branch u, t)]}: that sidewalk starts t m from the node
        crosswalks: [dict(a (3,), b (3,), w)]: across a road, curb to curb
        ramps: {(fid, side): [(node xy, branch u, t0, t1)]}: curb ramp along that sidewalk
        line_cuts: {fid: [(node xy, branch u, t)]}: that road's markings start t m from the node
                   (beyond its crosswalk)"""
    nodes = {}
    for ri, (fid, spec, pts, lon) in enumerate(roads):
        for vi, p in enumerate(pts):
            key = (round(float(p[0]) / NODE_TOL), round(float(p[1]) / NODE_TOL))
            nodes.setdefault(key, []).append((ri, vi))
    corners, cuts, crosswalks, ramps, line_cuts, stops = [], {}, [], {}, {}, []
    # junction nodes (two roads or more), merged into one junction when within MERGE_M: a complex
    # junction is several nodes a few metres apart joined by tiny connectors (2026-10-11, a five-way
    # cobbled junction at Fells Point lost most of its corners to the short-branch check)
    jn = [nodes[k] for k in sorted(nodes) if len({ri for ri, _ in nodes[k]}) >= 2]
    pos = [roads[inc[0][0]][2][inc[0][1], :2] for inc in jn]
    parent = list(range(len(jn)))

    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a in range(len(jn)):
        for b in range(a + 1, len(jn)):
            if math.hypot(*(pos[a] - pos[b])) < MERGE_M:
                parent[root(a)] = root(b)
    # and junction nodes joined by a short connector (along a road: the five-way at Fells Point was
    # two nodes 10 m apart)
    node_of = {}
    for a, inc in enumerate(jn):
        for ri, vi in inc:
            node_of[(ri, vi)] = a
    node_w = [max(roads[ri][1].width for ri, _ in inc) for inc in jn]
    reach = {}                                  # road index -> its longest merged connector (m)
    connector = {}                              # {node a, node b} -> the road joining them (merged)
    for ri, (fid, spec, pts, lon) in enumerate(roads):
        seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
        s_at = np.concatenate(([0.0], np.cumsum(seg)))
        on = [(vi, node_of[(ri, vi)]) for vi in range(len(pts)) if (ri, vi) in node_of]
        for (v0, a), (v1, b) in zip(on[:-1], on[1:]):
            L = s_at[v1] - s_at[v0]
            if L < CONNECTOR_M or L <= CONNECTOR_WIDTHS * max(node_w[a], node_w[b]):
                parent[root(a)] = root(b)
                reach[ri] = max(reach.get(ri, 0.0), float(L) + 0.5)
                if a != b:
                    connector.setdefault(frozenset((a, b)), ri)
    groups = {}
    for a in range(len(jn)):
        groups.setdefault(root(a), []).append(a)
    at_cluster = {}                             # (ri, vi) -> cluster id
    for cid, members in groups.items():
        for a in members:
            for ri, vi in jn[a]:
                at_cluster[(ri, vi)] = cid
    internal = set()                            # roads inside one junction (both ends in it)
    for ri, (fid, spec, pts, lon) in enumerate(roads):
        c0, c1 = at_cluster.get((ri, 0)), at_cluster.get((ri, len(pts) - 1))
        if c0 is not None and c0 == c1 and _branch_len(pts, 0, 1) < max(3 * MERGE_M, CONNECTOR_M):
            internal.add(fid)

    def leads_inside(ri, vi, step, cid):
        """Whether the road from vertex vi reaches another node of the same junction within 2 MERGE_M."""
        pts = roads[ri][2]
        k, acc = vi, 0.0
        while 0 <= k + step < len(pts) and acc < max(2 * MERGE_M, CONNECTOR_M, reach.get(ri, 0.0)):
            acc += math.hypot(*(pts[k + step, :2] - pts[k, :2]))
            k += step
            if at_cluster.get((ri, k)) == cid:
                return True
        return False
    for cid in sorted(groups, key=lambda c: tuple(np.round(pos[groups[c][0]], 2))):
        inc = sorted({x for a in groups[cid] for x in jn[a]})
        X = np.mean([pos[a] for a in groups[cid]], axis=0)
        branches = []                           # (angle, ri, vi, step, u, side of the road on u's left, origin)
        for ri, vi in inc:
            pts = roads[ri][2]
            if roads[ri][0] in internal:
                continue
            for step in (1, -1):
                if 0 <= vi + step < len(pts) and not leads_inside(ri, vi, step, cid):
                    u = _branch_dir(pts, vi, step)
                    # ordered round the junction by where the branch is BRANCH_M out, seen from its centre:
                    # by direction alone, branches of a merged junction's two nodes interleave wrongly
                    q = pts[vi, :2] + u * BRANCH_M - X
                    branches.append((math.atan2(q[1], q[0]), ri, vi, step, u, step, pts[vi, :2].copy()))
        branches.sort(key=lambda b: (b[0], roads[b[1]][0]))
        n = len(branches)
        if n < 2:
            continue
        side_t = {}                             # (branch index, "L"/"R") -> cut t
        for i in range(n):
            j = (i + 1) % n
            ai, ri, vi, si, ui, li, Pi = branches[i]
            aj, rj, vj, sj, uj, lj, Pj = branches[j]
            theta = math.degrees((math.atan2(uj[1], uj[0]) - math.atan2(ui[1], ui[0])) % (2 * math.pi))
            if ri == rj and n == 2:
                continue
            spi, spj = roads[ri][1], roads[rj][1]
            na, nb = node_of[(ri, vi)], node_of[(rj, vj)]
            cr = connector.get(frozenset((na, nb))) if na != nb else None
            if cr is not None and has_walk(spi) and has_walk(spj):
                # two nodes of a merged junction: the curb turns onto the connector's edge at one node,
                # runs along it and turns off at the other (an offset junction, Fells Point 2026-10-17)
                got = _across(roads, branches[i], branches[j], cr, X, walk_w, debug)
                if got is not None:
                    corner, ci, cj = got
                    corners.append(corner)
                    cuts.setdefault((roads[ri][0], li), []).append((Pi.copy(), ui, ci))
                    cuts.setdefault((roads[rj][0], -lj), []).append((Pj.copy(), uj, cj))
                    side_t[(i, "L")] = (ci, corner, ("dec", None, ci), 0.0)
                    side_t[(j, "R")] = (cj, corner, ("inc", None, cj), 0.0)
                    # the connector's sidewalk on the corner's side goes (it keeps the other one: a side
                    # street on one side of a through road)
                    cp = roads[cr][2][:, :2]
                    ki = int(np.argmin(np.hypot(*(cp - Pi).T)))
                    kj = int(np.argmin(np.hypot(*(cp - Pj).T)))
                    uc = _unit(Pj - Pi)
                    cuts.setdefault((roads[cr][0], -1 if kj > ki else 1), []).append(
                        (Pi.copy(), uc, float(math.hypot(*(Pj - Pi)))))
                    continue
            if not (has_walk(spi) and has_walk(spj)) or not (CORNER_ANGLES[0] < theta < CORNER_ANGLES[1]):
                if debug is not None:
                    debug.append(('walk/angle', round(theta), has_walk(spi), has_walk(spj), X.round(1).tolist()))
                continue
            ni, nj = _left(ui), _left(uj)
            wi, wj = spi.width / 2, spj.width / 2
            # curb extensions: on a two-way street with parking the corner's curb is the driving
            # lanes' edge, BULB_M along the street, then back to the parking curb over TAPER_M
            # (Fells Point aerials, 2026-10-11: bulb-outs at most corners; off since GitHub #208, as most streets
            # have none: roads.build_tile passes no has_parking)
            acute = theta < ACUTE_DEG
            pi_ = parking_m if has_parking(spi) and not acute else 0.0
            pj_ = parking_m if has_parking(spj) and not acute else 0.0
            di, dj = wi - pi_, wj - pj_
            bi, bj = (BULB_M if pi_ else 0.0), (BULB_M if pj_ else 0.0)
            # a smaller radius where the corner does not fit (its street bending on past it put the curb on
            # its own carriageway: no corner, bare ground in the angle, the user's report #46, 2026-10-17)
            for rscale in RADIUS_TRIES:
                radius = max(RADII.get(spi.cls, RADIUS), RADII.get(spj.cls, RADIUS)) * rscale
                if acute:
                    radius = min(radius, max(ACUTE_R_MIN, ACUTE_T_M * math.tan(math.radians(theta / 2))))
                Z = _line_x(Pi + ni * (di + radius), ui, Pj - nj * (dj + radius), uj)
                if Z is None:
                    continue
                Ti, Tj = Z - ni * radius, Z + nj * radius
                ti, tj = float((Ti - Pi) @ ui), float((Tj - Pj) @ uj)
                if ti <= 0 or tj <= 0 or ti > 40 or tj > 40:
                    if debug is not None:
                        debug.append(('t range', round(ti, 1), round(tj, 1), X.round(1).tolist()))
                    continue
                ci = ti + (bi + TAPER_M if pi_ else 0.0)            # where the sidewalk runs resume
                cj = tj + (bj + TAPER_M if pj_ else 0.0)
                # the branches must go on past the corner (a short connector between two close
                # junctions cannot carry a curb return: 2026-10-11 survey, slabs on carriageways)
                if _branch_len(roads[ri][2], vi, si) < ci + 1.0 or _branch_len(roads[rj][2], vj, sj) < cj + 1.0:
                    if debug is not None:
                        debug.append(('short branch', round(_branch_len(roads[ri][2], vi, si), 1), round(ci, 1), round(_branch_len(roads[rj][2], vj, sj), 1), round(cj, 1), X.round(1).tolist()))
                    continue
                wki = spi.walk if walk_w is None else walk_w
                wkj = spj.walk if walk_w is None else walk_w
                O = _line_x(Pi + ni * (wi + wki), ui, Pj - nj * (wj + wkj), uj)
                if O is None:
                    continue
                a0 = math.atan2(*(Ti - Z)[::-1])
                a1 = math.atan2(*(Tj - Z)[::-1])
                da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi      # the short way round
                k = max(3, int(abs(da) * radius / 0.75) + 1)
                ang = a0 + da * np.linspace(0.0, 1.0, k)
                arc2 = np.c_[Z[0] + radius * np.cos(ang), Z[1] + radius * np.sin(ang)]
                pre = ([Pi + ui * ci + ni * wi, Pi + ui * (ti + bi) + ni * di] if pi_ else [])
                post = ([Pj + uj * (tj + bj) - nj * dj, Pj + uj * cj - nj * wj] if pj_ else [])
                curb2 = np.vstack(pre + [arc2] + post)
                # heights: the branch's road height at each point's distance along it
                tpar = np.r_[[ci, ti + bi][:len(pre)], np.full(k, np.nan), [tj + bj, cj][:len(post)]]
                zi0 = _z_along(roads[ri][2], vi, si, ti)
                zj0 = _z_along(roads[rj][2], vj, sj, tj)
                z = np.r_[[_z_along(roads[ri][2], vi, si, t) for t in tpar[:len(pre)]], np.linspace(zi0, zj0, k),
                          [_z_along(roads[rj][2], vj, sj, t) for t in tpar[len(pre) + k:]]]
                curb = np.c_[curb2, z]
                # inward (toward the sidewalk): the polyline's normals, oriented by the arc's centre
                d = np.gradient(curb2, axis=0)
                nrm = np.stack([-d[:, 1], d[:, 0]], axis=1)
                nrm /= np.maximum(np.hypot(*nrm.T), 1e-9)[:, None]
                mid = len(pre) + k // 2
                if (nrm[mid] @ (Z - curb2[mid])) < 0:
                    nrm = -nrm
                zo = 0.5 * (zi0 + zj0)
                zci = _z_along(roads[ri][2], vi, si, ci)
                zcj = _z_along(roads[rj][2], vj, sj, cj)
                outer = [(*(Pj + uj * cj - nj * (wj + wkj)), zcj), (*O, zo), (*(Pi + ui * ci + ni * (wi + wki)), zci)]
                Cc = _line_x(Pi + ni * di, ui, Pj - nj * dj, uj)
                fill = np.vstack([[*Cc, zo]] + [np.r_[arc2[m], z[len(pre) + m]] for m in range(k)]) if Cc is not None else None
                if fill is not None and math.hypot(*(Cc - X)) > 20.0 + MERGE_M:
                    if debug is not None:
                        debug.append(('needle', X.round(1).tolist()))
                    continue                        # a needle of asphalt at a very sharp corner
                # nothing of the corner on any road: other roads at their width, its own two at their
                # driving width (a bulb-out covers the parking lane)
                probe = np.vstack([curb2 + nrm * 0.8, curb2 + nrm * 2.0])
                own = {ri: di, rj: dj}
                hit = _on_carriageway(probe, roads, (X, 60.0), half={roads[r][0]: h for r, h in own.items()})
                if hit is None:     # the slab's outer corner: on other roads only (its own street bending on past the
                    # corner took it for its carriageway, the whole corner went and left bare ground, report #46)
                    outer_p = np.vstack([[(np.array(o[:2]) + O) / 2 for o in (outer[0], outer[2])], [O]])
                    hit = _on_carriageway(outer_p, roads, (X, 60.0), skip={roads[ri][0], roads[rj][0]})
                if hit is not None:
                    if debug is not None:
                        debug.append(('on carriageway', hit[:8], hit in internal, roads[ri][0][:8], roads[rj][0][:8], round(theta), X.round(1).tolist()))
                    continue
                corner = {"arc": curb, "inward": nrm, "outer": outer, "fill": fill, "ramps": []}
                corners.append(corner)
                cuts.setdefault((roads[ri][0], li), []).append((Pi.copy(), ui, ci))
                cuts.setdefault((roads[rj][0], -lj), []).append((Pj.copy(), uj, cj))
                seg = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(curb2, axis=0).T))))
                # arc length along the curb of a point t m along each branch (for the crosswalk's ramp)
                side_t[(i, "L")] = (ti, corner, ("dec", seg[len(pre) - 1] if pi_ else None, ti + bi), bi)
                side_t[(j, "R")] = (tj, corner, ("inc", seg[len(pre) + k - 1] if pj_ else None, tj), bj)
                break
            else:
                continue
        for i in range(n):                      # crosswalk + ramps on a branch with both corners
            if (i, "L") in side_t and (i, "R") in side_t:
                ai, ri, vi, si, ui, li, Pi = branches[i]
                spec = roads[ri][1]
                w = spec.width / 2
                dw = w - (parking_m if has_parking(spec) else 0.0)     # curb to curb at the bulbs
                t = max(side_t[(i, "L")][0], side_t[(i, "R")][0]) + CW_GAP + CROSSWALK["w"] / 2
                c = Pi + ui * t
                z = _z_along(roads[ri][2], vi, si, t)
                nl = _left(ui)
                a, b = c + nl * dw, c - nl * dw
                if _branch_len(roads[ri][2], vi, si) < t + CROSSWALK["w"]:
                    continue
                others = [rd for k2, rd in enumerate(roads) if k2 != ri]
                ends = np.array([a + nl * 0.5, b - nl * 0.5])
                if _on_carriageway(ends, others, (X, 60.0)) is not None or any(
                        _crosses(a, b, rd[2][:, :2]) for rd in others
                        if np.hypot(*(rd[2][:, :2] - c).T).min() < 60.0):
                    continue                    # it would cross or end on another road
                if any(min(_seg_dist(np.array([a, b, c]), np.array([q["a"][:2], q["b"][:2]]))) < 2.0
                       for q in crosswalks):
                    continue                    # one crosswalk where two would touch
                crosswalks.append({"a": np.r_[a, z], "b": np.r_[b, z], "w": CROSSWALK["w"]})
                # the stop line: across the half of the road whose traffic comes into the junction
                # (right-hand traffic heading along -u drives on u's left), 1 m before the crosswalk
                ts = t + CROSSWALK["w"] / 2 + STOP_GAP + STOP_LINE["w"] / 2
                if _branch_len(roads[ri][2], vi, si) > ts + 2.0:
                    cs = Pi + ui * ts
                    zs = _z_along(roads[ri][2], vi, si, ts)
                    stops.append({"a": np.r_[cs + nl * 0.2, zs], "b": np.r_[cs + nl * (dw - 0.3), zs], "w": STOP_LINE["w"]})
                t0, t1 = t - CROSSWALK["w"] / 2, t + CROSSWALK["w"] / 2
                for key, side in (((i, "L"), li), ((i, "R"), -li)):
                    tt, corner, (how, s0, tref), bulb = side_t[key]
                    if bulb > 0 and s0 is not None and t1 <= tt + bulb:
                        # on the bulb: the corner piece carries the ramp
                        lo, hi = (s0 + (tref - t1), s0 + (tref - t0)) if how == "dec" else (s0 + (t0 - tref), s0 + (t1 - tref))
                        corner["ramps"].append((lo, hi))
                    else:
                        ramps.setdefault((roads[ri][0], side), []).append((Pi.copy(), ui, t0, t1))
                line_cuts.setdefault(roads[ri][0], []).append((Pi.copy(), ui, t1))
    return corners, cuts, crosswalks + [dict(q, stop=True) for q in stops], ramps, line_cuts, internal


def apply_cuts(line, cuts, near=40.0):
    """A sidewalk centreline (k, 3) with the parts before its junction cuts removed: points within
    `near` m of a node X along branch u with (p - X) . u < t go, and the line is closed at exactly t
    (a cross-section square to the road). Returns the remaining pieces [(pts, (start_cut, end_cut))]
    (whether each end is a junction cut). Pure."""
    pts = np.asarray(line, np.float64)
    if not cuts or len(pts) < 2:
        return [(pts, (False, False))]
    # densify to 0.5 m so a cut lands within the resolution, then exact ends
    seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
    s = np.concatenate(([0.0], np.cumsum(seg)))
    if s[-1] <= 0:
        return []
    t = np.linspace(0.0, s[-1], max(2, int(s[-1] / 0.5) + 1))
    d = np.stack([np.interp(t, s, pts[:, c]) for c in range(pts.shape[1])], axis=1)
    keep = np.ones(len(d), bool)
    alongs = []
    for X, u, tc in cuts:
        rel = d[:, :2] - X
        along = rel @ u
        lateral = np.abs(rel @ _left(u))
        inside = (along < tc) & (along > -1.0) & (lateral < near)
        keep &= ~inside
        alongs.append((along - tc, inside))

    def exact(k_out, k_in):
        """The point between a removed sample and a kept one where its cut's cross-section lies."""
        for f, inside in alongs:
            if inside[k_out] and not inside[k_in] and f[k_in] != f[k_out]:
                w = f[k_in] / (f[k_in] - f[k_out])
                return d[k_in] + w * (d[k_out] - d[k_in])
        return d[k_in]
    out, start = [], None
    for k in range(len(d) + 1):
        if k < len(d) and keep[k]:
            if start is None:
                start = k
        elif start is not None:
            run = d[start:k].copy()
            cut0 = start > 0
            cut1 = k < len(d)
            if cut0:
                run[0] = exact(start - 1, start)
            if cut1:
                run[-1] = exact(k, k - 1)
            if len(run) >= 2:
                out.append((run, (cut0, cut1)))
            start = None
    return out


def ramp_ranges(run, ramps):
    """Arc-length ranges [(s0, s1)] along a sidewalk run (k, >=2) of its curb ramps. Pure."""
    if not ramps:
        return []
    seg = np.hypot(*np.diff(run[:, :2], axis=0).T)
    s = np.concatenate(([0.0], np.cumsum(seg)))
    out = []
    for X, u, t0, t1 in ramps:
        along = (run[:, :2] - X) @ u
        lateral = np.abs((run[:, :2] - X) @ _left(u))
        m = (lateral < 40.0)
        if not m.any():
            continue
        a = np.interp(t0, along[m], s[m]) if np.all(np.diff(along[m]) > 0) else None
        b = np.interp(t1, along[m], s[m]) if np.all(np.diff(along[m]) > 0) else None
        if a is None and np.all(np.diff(along[m]) < 0):
            a = np.interp(t0, along[m][::-1], s[m][::-1])
            b = np.interp(t1, along[m][::-1], s[m][::-1])
        if a is None or b is None:
            continue
        lo, hi = min(a, b), max(a, b)
        if hi > 0 and lo < s[-1] and hi - lo > 0.5:
            out.append((max(lo, 0.0), min(hi, s[-1])))
    return out
