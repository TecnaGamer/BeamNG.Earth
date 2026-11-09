"""trackrules.py: the pure rule checks trackcheck.py runs on what the server builds round a racetrack (2026-11-06).

Every check reads geometry only (plan coordinates in one ENU frame, m): race lines [(k, 2)] with their carriageway half
widths, and the items the server streams (Forest rows, building rings, a surface height function). No track names,
no per-track lists; thresholds come from the server's own constants (passed in) or the physics noted at each.

Rules (classes):
  join      height step and cross-slope (bank) mismatch where one raceway piece meets another (pit road, apron and
            infield pieces onto the oval) and at a closed way's own seam (start = end)
  stand     grandstand models: open front (local +y; the row's yaw is counter-clockwise from +x, trees.lua turns the
            engine's clockwise heading by -yaw) toward the track, the model's box >= clear_m off the carriageway, the
            model on its mapped footprint, the footprint covered
  prop      any Forest item (poles, signs, fences, trees, vegetation) whose centre is on a carriageway + shoulder
  building  a built footprint on the band, or round the race line (a roof over the oval / its infield)
  tunnel    terrain hole samples on the band; a tube's vault nearer the surface than race_tube_depth under the band
  walls     (info) the share of a closed loop's length with a barrier row within reach of its edge
"""
import math

import numpy as np


# ---------------------------------------------------------------------------------------------------- geometry
def edge_dist(Q, lines, halves):
    """m from points Q (k, 2) to the nearest carriageway edge of lines (negative: on a carriageway), and the nearest
    centreline point (k, 2)."""
    Q = np.asarray(Q, np.float64).reshape(-1, 2)
    best = np.full(len(Q), np.inf)
    foot = np.zeros_like(Q)
    for ln, h in zip(lines, halves):
        ln = np.asarray(ln, np.float64)[:, :2]
        if len(ln) < 2:
            continue
        a, d = ln[:-1], np.diff(ln, axis=0)
        ll = np.maximum((d * d).sum(1), 1e-12)
        for s in range(0, len(Q), 512):
            q = Q[s:s + 512]
            t = np.clip(((q[:, None, :] - a[None]) * d[None]).sum(2) / ll[None], 0.0, 1.0)
            p = a[None] + t[..., None] * d[None]
            dist = np.hypot(q[:, None, 0] - p[..., 0], q[:, None, 1] - p[..., 1])
            j = np.argmin(dist, 1)
            dm = dist[np.arange(len(q)), j] - float(h)
            better = dm < best[s:s + 512]
            best[s:s + 512][better] = dm[better]
            foot[s:s + 512][better] = p[np.arange(len(q)), j][better]
    return best, foot


def inside(ring, pts):
    """Even-odd point in polygon (ring (n, 2), pts (k, 2))."""
    ring = np.asarray(ring, np.float64)[:, :2]
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    x, y = pts[:, 0][:, None], pts[:, 1][:, None]
    a, b = ring[None], np.roll(ring, -1, axis=0)[None]
    cond = (a[..., 1] > y) != (b[..., 1] > y)
    dy = np.where(b[..., 1] == a[..., 1], 1e-12, b[..., 1] - a[..., 1])
    xi = a[..., 0] + (y - a[..., 1]) * (b[..., 0] - a[..., 0]) / dy
    return (cond & (x < xi)).sum(1) % 2 == 1


def densify(line, step):
    line = np.asarray(line, np.float64)[:, :2]
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
    if s[-1] <= 0:
        return line[:1]
    t = np.arange(0.0, s[-1] + 1e-9, step)
    return np.stack([np.interp(t, s, line[:, 0]), np.interp(t, s, line[:, 1])], 1)


def ring_area(ring):
    r = np.asarray(ring, np.float64)[:, :2]
    return 0.5 * abs(float(np.dot(r[:, 0], np.roll(r[:, 1], -1)) - np.dot(r[:, 1], np.roll(r[:, 0], -1))))


# ---------------------------------------------------------------------------------------------------- stands
def stand_axes(yaw_deg):
    """(long axis, open front) unit vectors of a grandstand row of yaw yaw_deg (counter-clockwise from +x): local +x,
    local +y."""
    a = math.radians(float(yaw_deg))
    return np.array([math.cos(a), math.sin(a)]), np.array([-math.sin(a), math.cos(a)])


def box_pts(c, ax, front, length, depth, step=2.0):
    u = np.linspace(-0.5, 0.5, max(2, int(length / step) + 1))
    v = np.linspace(-0.5, 0.5, max(2, int(depth / step) + 1))
    uu, vv = np.meshgrid(u, v)
    return np.asarray(c)[None] + uu.reshape(-1, 1) * length * ax + vv.reshape(-1, 1) * depth * front


def in_box(pts, c, ax, front, length, depth):
    d = np.asarray(pts, np.float64) - np.asarray(c)[None]
    return (np.abs(d @ ax) <= length / 2) & (np.abs(d @ front) <= depth / 2)


def stand_check(stands, lines, halves, footprints, stand_len, stand_depth, clear_m, piece_min):
    """stands: [(x, y, yaw, scale)]; footprints: the mapped seating rings near the track. Per stand a dict: faces (the
    open front's dot with the direction to the nearest carriageway point: > 0 faces the track), gap (m, the model's
    box to the nearest carriageway edge), on_fp (share of its box on a footprint); faults: away (faces <= 0), band
    (gap < clear_m), off (on_fp < piece_min with footprints given). And per footprint its covered share."""
    out = []
    boxes = []
    for x, y, yaw, sc in stands:
        ax, fr = stand_axes(yaw)
        c = np.array([x, y], np.float64)
        L, D = stand_len * sc, stand_depth * sc
        bp = box_pts(c, ax, fr, L, D)
        gap = float(edge_dist(bp, lines, halves)[0].min())
        _, foot = edge_dist(c[None], lines, halves)
        to = foot[0] - c
        faces = float(np.dot(fr, to / max(np.hypot(*to), 1e-9)))
        on = 1.0
        if footprints:
            m = np.zeros(len(bp), bool)
            for r in footprints:
                m |= inside(r, bp)
            on = float(m.mean())
        faults = []
        if faces <= 0.0:
            faults.append("away")
        if gap < clear_m:
            faults.append("band")
        if footprints and on < piece_min:
            faults.append("off")
        out.append(dict(x=x, y=y, yaw=yaw, scale=sc, faces=round(faces, 2), gap=round(gap, 2), on_fp=round(on, 2),
                        faults=faults))
        boxes.append((c, ax, fr, L, D))
    cover = []
    for r in footprints:
        r = np.asarray(r, np.float64)[:, :2]
        lo, hi = r.min(0), r.max(0)
        step = max(2.0, math.sqrt(max(ring_area(r), 1.0) / 400.0))
        gx, gy = np.meshgrid(np.arange(lo[0], hi[0] + step, step), np.arange(lo[1], hi[1] + step, step))
        g = np.c_[gx.ravel(), gy.ravel()]
        g = g[inside(r, g)]
        if not len(g):
            continue
        m = np.zeros(len(g), bool)
        for b in boxes:
            m |= in_box(g, *b)
        cover.append(float(m.mean()))
    return out, cover


# ---------------------------------------------------------------------------------------------------- props
def props_on_band(rows, lines, halves, clear_m, skip_groups=()):
    """Forest rows [group, x, y, ...] whose centre is on a carriageway + clear_m: [(group, x, y, edge distance m)]."""
    rows = [r for r in rows if str(r[0]) not in skip_groups]
    if not rows or not lines:
        return []
    P = np.array([[float(r[1]), float(r[2])] for r in rows])
    allr = np.concatenate([np.asarray(l)[:, :2] for l in lines])
    pad = max(halves) + clear_m + 1
    k = np.flatnonzero(((P >= allr.min(0) - pad) & (P <= allr.max(0) + pad)).all(1))
    if not len(k):
        return []
    d, _ = edge_dist(P[k], lines, halves)
    return [(str(rows[i][0]), float(P[i, 0]), float(P[i, 1]), round(float(dd), 2)) for i, dd in zip(k, d) if dd < clear_m]


# ---------------------------------------------------------------------------------------------------- buildings
def building_on_band(ring, lines, halves, clear_m):
    """How a built footprint meets the track: (outline m on the band (carriageway + clear_m, every 1 m), race line m
    inside it (a roof over the track or its infield: the line round under it))."""
    ring = np.asarray(ring, np.float64)[:, :2]
    if len(ring) < 3:
        return 0, 0
    lo, hi = ring.min(0), ring.max(0)
    pad = max(halves) + clear_m + 1
    near = [(l, h) for l, h in zip(lines, halves)
            if not ((np.asarray(l)[:, :2].max(0) < lo - pad).any() or (np.asarray(l)[:, :2].min(0) > hi + pad).any())]
    if not near:
        return 0, 0
    nl, nh = [l for l, _ in near], [h for _, h in near]
    dense = densify(np.vstack([ring, ring[:1]]), 1.0)
    on = int((edge_dist(dense, nl, nh)[0] < clear_m).sum())
    under = sum(int(inside(ring, densify(l, 1.0)).sum()) for l in nl)
    return on, under


# ---------------------------------------------------------------------------------------------------- joins
def join_profile(surface, p, t, half, reach=6.0, step=0.5, lanes=(-0.8, -0.4, 0.0, 0.4, 0.8)):
    """At point p, a piece arriving along unit t, half width half: (step m, edge twist m). surface(P (k, 2)) -> z.
    step: the largest 1 m height change along t across the join (+-2 m) beyond the profile's own grade (its median 1 m
    change over +-reach), over lanes across the piece; twist: the change of bank (cross slope) across the join beyond
    the bank's own steady change along it (a line and a step fitted to the bank over the reach before and after),
    times the half width (the height one edge moves against the other). (A banking easing in at the bank rate,
    0.0025 per m, through Interlagos' Subida dos Boxes join read as a 0.12 m twist from the reach before to the reach
    after, 8 m apart, 2026-11-07: the step nets out the grade, the twist now the bank's steady change.)"""
    p, t = np.asarray(p, np.float64), np.asarray(t, np.float64)
    t = t / max(np.hypot(*t), 1e-9)
    n = np.array([-t[1], t[0]])
    s = np.arange(-reach, reach + 1e-9, step)
    k1 = int(round(1.0 / step))
    worst = 0.0
    Z = []
    for f in lanes:
        P = p[None] + s[:, None] * t[None] + f * half * n[None]
        z = np.asarray(surface(P), np.float64)
        Z.append(z)
        if not np.isfinite(z).all():
            continue
        d1 = z[k1:] - z[:-k1]
        mid = (s[k1:] + s[:-k1]) / 2
        near = np.abs(mid) <= 2.0
        if near.any():
            worst = max(worst, float(np.max(np.abs(d1[near] - np.median(d1)))))
    Z = np.array(Z)
    twist = 0.0
    if np.isfinite(Z[[0, -1]]).all():
        bank = (Z[-1] - Z[0]) / (2 * lanes[-1] * half)
        m = np.abs(s) >= 2.0
        A = np.c_[np.ones(int(m.sum())), s[m], (s[m] > 0).astype(np.float64)]
        coef = np.linalg.lstsq(A, bank[m], rcond=None)[0]
        # (the lesser of the fitted step and the mean change: on a bank curving through the join the fit's slope
        # extrapolates the step past anything the edges do, Nurburgring 7.5 m over a 5.3 m mean change)
        mean = float(bank[s >= 2.0].mean() - bank[s <= -2.0].mean())
        twist = min(abs(float(coef[2])), abs(mean)) * half
    return round(worst, 3), round(twist, 3)


def joins(ways, tol=1.0):
    """The joins between raceway pieces: [(fid, p (2,), t (2,) arriving direction, kind)], kind "seam" (a closed way's
    own start = end) or "join" (an end on another piece's line, its end or its middle). ways: {fid: xy (k, 2)}."""
    out = []
    for fid, xy in ways.items():
        xy = np.asarray(xy, np.float64)
        if len(xy) < 2:
            continue
        if np.hypot(*(xy[0] - xy[-1])) < tol:
            t = xy[1] - xy[-2]
            out.append((fid, xy[0], t, "seam"))
            continue
        for p, q in ((xy[0], xy[1]), (xy[-1], xy[-2])):
            for f2, xy2 in ways.items():
                if f2 == fid or len(xy2) < 2:
                    continue
                d, _ = edge_dist(p[None], [xy2], [0.0])
                if d[0] < tol:
                    out.append((fid, p, p - q, "join"))
                    break
    return out


# ---------------------------------------------------------------------------------------------------- walls
def barrier_share(line, rows, half, reach):
    """Share of a race line's length with a barrier row's centre within half + reach of the centreline (1 m steps)."""
    P = densify(line, 1.0)
    if not len(rows) or len(P) < 2:
        return 0.0
    B = np.array([[float(r[1]), float(r[2])] for r in rows])
    lo, hi = P.min(0) - half - reach, P.max(0) + half + reach
    B = B[((B >= lo) & (B <= hi)).all(1)]
    if not len(B):
        return 0.0
    hit = np.zeros(len(P), bool)
    for s in range(0, len(B), 256):
        d = np.hypot(P[:, None, 0] - B[None, s:s + 256, 0], P[:, None, 1] - B[None, s:s + 256, 1])
        hit |= (d <= half + reach).any(1)
    return float(hit.mean())


# ---------------------------------------------------------------------------------------------------- severity
# weights: a fault a driver hits or sees from the seat at the start line counts most. Per class, the flag is a count
WEIGHTS = dict(bump=20, plateau=20, join=30, seam=40, stand_away=50, stand_band=40, stand_off=5, stand_cover=5, prop=20, building=60,
               holes=80, vault=30)


def severity(flags):
    return round(sum(WEIGHTS.get(k, 0) * min(int(v), 20) for k, v in flags.items()), 1)


# ---------------------------------------------------------------------------------------------------- main line
def main_pieces(ways, tol=1.0, max_edges=60, beside_m=None, pits=(), parallel_dot=0.85):
    """{fid: True} for the pieces on each raceway network's racing line, False for its side pieces (pit lanes,
    paddock roads, links). From the shape alone: the pieces are split where an end meets another's middle, and the
    racing line is the longest simple cycle of each connected network (a circuit), else its longest simple path (a
    drag strip, a hill climb). A closed way on its own is its own cycle. ways: {fid: xy (k, 2)}."""
    fids = list(ways)
    xy = {f: np.asarray(ways[f], np.float64)[:, :2] for f in fids}
    arc = {f: np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy[f], axis=0).T))] for f in fids}
    cuts = {f: {0.0, float(arc[f][-1])} for f in fids}
    for f in fids:
        for p in (xy[f][0], xy[f][-1]):
            for g in fids:
                if g == f or len(xy[g]) < 2:
                    continue
                a, d = xy[g][:-1], np.diff(xy[g], axis=0)
                ll = np.maximum((d * d).sum(1), 1e-12)
                t = np.clip(((p - a) * d).sum(1) / ll, 0, 1)
                dist = np.hypot(*(p - a - t[:, None] * d).T)
                k = int(np.argmin(dist))
                if dist[k] < tol:
                    cuts[g].add(float(arc[g][k] + t[k] * math.sqrt(ll[k])))
    nodes = []

    def node(p):
        for i, q in enumerate(nodes):
            if math.hypot(p[0] - q[0], p[1] - q[1]) < tol:
                return i
        nodes.append(p)
        return len(nodes) - 1
    edges = []                                  # (u, v, length, fid)
    for f in fids:
        cs = sorted(cuts[f])
        for s0, s1 in zip(cs[:-1], cs[1:]):
            if s1 - s0 < tol:
                continue
            p0 = (float(np.interp(s0, arc[f], xy[f][:, 0])), float(np.interp(s0, arc[f], xy[f][:, 1])))
            p1 = (float(np.interp(s1, arc[f], xy[f][:, 0])), float(np.interp(s1, arc[f], xy[f][:, 1])))
            edges.append((node(p0), node(p1), s1 - s0, f))
    adj = {}
    for e, (u, v, L, f) in enumerate(edges):
        adj.setdefault(u, []).append((v, e))
        adj.setdefault(v, []).append((u, e))
    seen, comps = set(), []
    for n0 in adj:
        if n0 in seen:
            continue
        st, comp = [n0], set()
        while st:
            n = st.pop()
            if n in comp:
                continue
            comp.add(n)
            st.extend(v for v, _ in adj[n])
        seen |= comp
        comps.append(comp)
    main = set()
    lines = []                                  # per network: (its racing line's fids, length, cyclic)
    for comp in comps:
        ce = [e for e, (u, v, L, f) in enumerate(edges) if u in comp]
        best, best_L = None, -1.0
        budget = [20000]

        def dfs(start, n, used, L, path_nodes):
            nonlocal best, best_L
            budget[0] -= 1
            if budget[0] < 0:
                return
            for v, e in adj[n]:
                if e in used:
                    continue
                L2 = L + edges[e][2]
                if v == start:              # a cycle
                    if L2 > best_L:
                        best, best_L = used | {e}, L2
                    continue
                if v in path_nodes:
                    continue
                dfs(start, v, used | {e}, L2, path_nodes | {v})
        if len(ce) <= max_edges:
            for s in comp:
                dfs(s, s, frozenset(), 0.0, {s})
        if best is None:                    # no cycle: the longest path
            def far(s):
                bestp, bl = frozenset(), 0.0
                stack = [(s, frozenset(), 0.0, {s})]
                cnt = 0
                while stack and cnt < 20000:
                    cnt += 1
                    n, used, L, pn = stack.pop()
                    if L > bl:
                        bestp, bl = used, L
                    for v, e in adj[n]:
                        if e not in used and v not in pn:
                            stack.append((v, used | {e}, L + edges[e][2], pn | {v}))
                return bestp, bl
            for s in comp:
                p_, l_ = far(s)
                if l_ > best_L:
                    best, best_L = p_, l_
        lines.append(({edges[e][3] for e in best or ()}, max(best_L, 0.0), _is_cycle(best, edges)))
    # a network without a cycle running mostly beside a longer network's racing line (within beside_m: a pit lane or
    # paddock road mapped apart from the circuit it serves) is that circuit's side network
    order = sorted(range(len(lines)), key=lambda k: -lines[k][1])
    for r, k in enumerate(order):
        fs, L, cyc = lines[k]
        if not cyc and beside_m is not None and r > 0:
            pts = np.concatenate([densify(xy[f], 5.0) for f in fs]) if fs else np.zeros((0, 2))
            for k2 in order[:r]:
                other = [xy[f] for f in lines[k2][0]]
                if other and len(pts) and (edge_dist(pts, other, [0.0] * len(other))[0] <= beside_m).mean() >= 0.5:
                    fs = set()
                    break
        main |= fs
    main -= set(pits)                       # (named pit lanes: roads.pit_lane)
    # a piece running beside another for most of its length (within beside_m, parallel) is the side one of the two when
    # more of its own length runs beside: a pit lane along the main straight, the straight going on past it
    if beside_m is not None:
        dense = {f: densify(xy[f], 5.0) for f in fids}
        tang = {}
        for f in fids:
            d = np.gradient(dense[f], axis=0) if len(dense[f]) > 1 else np.zeros_like(dense[f])
            tang[f] = d / np.maximum(np.hypot(*d.T), 1e-9)[:, None] if len(d) else d

        def beside(f, g):
            P = dense[f]
            if len(P) < 2 or len(xy[g]) < 2:
                return 0.0
            dd, foot = edge_dist(P, [xy[g]], [0.0])
            near = (dd <= beside_m) & (dd > tol)
            if not near.any():
                return 0.0
            # the other line's direction at the foot
            Q = dense[g]
            j = np.argmin(np.hypot(foot[:, None, 0] - Q[None, :, 0], foot[:, None, 1] - Q[None, :, 1]), 1)
            par = np.abs((tang[f] * tang[g][j]).sum(1)) >= parallel_dot
            return float((near & par).mean())
        for f in sorted(main):
            for g in fids:
                if g == f or g not in main:
                    continue
                bf = beside(f, g)
                if bf >= 0.5 and bf > beside(g, f):
                    main.discard(f)
                    break
    # (a piece is main when any stretch of it is on the line)
    return {f: f in main for f in fids}


def _is_cycle(used, edges):
    """Whether edge set used closes on itself (every node met an even number of times)."""
    if not used:
        return False
    deg = {}
    for e in used:
        for n in edges[e][:2]:
            deg[n] = deg.get(n, 0) + 1
    return all(d % 2 == 0 for d in deg.values())
