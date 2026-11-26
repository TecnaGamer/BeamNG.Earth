"""Checks of Earth's drawn geometry for the debug pictures and the eval, made from the meshes and the map's lines alone (not
from Earth's grading, nor from where the viewer put things):

  vehicle_hits      a reference truck or wagon whose body (from BODY_BASE_M over its wheels to its roof) cuts a road deck, a
                    track, a tunnel tube or a road's ground: the clearance it doesn't have. Its own road or track is under
                    its wheels, under the body, so it never counts.
  penetrations      where two of the map's lines cross at different levels (one on a bridge or in a tunnel, the other not),
                    the meshes of one cutting through the other's: a deck through a track, a track through a road's ground,
                    a tube through the road over it. A surface resting on another (a track on its deck, a deck's end on its
                    embankment) is no penetration: within REST_M of it.

Both work on triangles (n, 3, 3) and are exact (edge / triangle intersections), so a count is the same whoever looks at it.
"""
import math

import numpy as np

BODY_BASE_M = 0.5           # m over its wheels a vehicle's body starts (its own road under it never counts)
REST_M = 0.3                # m one surface may sink into another and still rest on it (a track on its deck)
FLAT_NZ = 0.7               # a triangle this near horizontal is a surface something rests on
EDGE_EPS_M = 0.02           # m from its ends an edge must cross a triangle (an edge ending on a surface only touches it)
CROSS_NEAR_M = 12.0         # m round a crossing of the map's lines its meshes are compared
GROUND_CORRIDOR_M = 4.0     # m each side of a ground road's line its ground is that road's surface (Earth paints it there)
SHORT_TUNNEL_M = 60.0       # m: a tunnel this short is opened by Earth (gradesep): its road is painted on the ground


# ---- exact tests ---------------------------------------------------------------------------------------------------------
def _aabb(t):
    return t.min(axis=1), t.max(axis=1)


def _pairs(A, B, pad=0.0, chunk=4000):
    """(i, j) of the triangles of A and B whose boxes overlap (pad m apart at most)."""
    if not len(A) or not len(B):
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    alo, ahi = _aabb(A)
    blo, bhi = _aabb(B)
    I, J = [], []
    for s in range(0, len(A), chunk):
        lo, hi = alo[s:s + chunk, None], ahi[s:s + chunk, None]
        ok = ((lo <= bhi[None] + pad) & (hi >= blo[None] - pad)).all(axis=2)
        i, j = np.nonzero(ok)
        I.append(i + s)
        J.append(j)
    return np.concatenate(I), np.concatenate(J)


def _edges_cross(E0, E1, T, eps=EDGE_EPS_M):
    """Bool (n,): the segment E0 -> E1 crosses triangle T (n, 3, 3) inside it or on its edge, away from the segment's ends
    (Moller-Trumbore)."""
    d = E1 - E0
    L = np.linalg.norm(d, axis=1)
    a, b, c = T[:, 0], T[:, 1], T[:, 2]
    e1, e2 = b - a, c - a
    p = np.cross(d, e2)
    det = (e1 * p).sum(1)
    ok = np.abs(det) > 1e-12
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    s = E0 - a
    u = (s * p).sum(1) * inv
    q = np.cross(s, e1)
    v = (d * q).sum(1) * inv
    t = (e2 * q).sum(1) * inv
    m = np.where(L > 0, eps / np.maximum(L, 1e-12), 1.0)
    return ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > m) & (t < 1 - m)


def tri_tri_cross(A, B):
    """Bool (n,): triangles A[k] and B[k] (n, 3, 3 each) cut through each other: an edge of one crosses the other inside it."""
    hit = np.zeros(len(A), bool)
    for e in ((0, 1), (1, 2), (2, 0)):
        hit |= _edges_cross(A[:, e[0]], A[:, e[1]], B)
        hit |= _edges_cross(B[:, e[0]], B[:, e[1]], A)
    return hit


def _normals(T):
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    ln = np.linalg.norm(n, axis=1)
    return n / np.where(ln > 1e-12, ln, 1.0)[:, None]


def _rests_on(A, B, rest=REST_M):
    """Bool (n,): A[k] rests on B[k]: B is a flat-ish surface and no corner of A lies more than rest under B's plane."""
    n = _normals(B)
    flat = np.abs(n[:, 2]) >= FLAT_NZ
    nz = np.where(flat, n[:, 2], 1.0)
    # B's plane's height under each corner of A
    zb = B[:, None, 0, 2] - ((A[:, :, :2] - B[:, None, 0, :2]) * n[:, None, :2]).sum(2) / nz[:, None]
    return flat & ((A[:, :, 2] - zb).min(axis=1) >= -rest)


def cutting(A, B, rest=REST_M):
    """(hit A (len A,), hit B (len B,)): the triangles of A and of B that cut through one of the other set, not resting on it."""
    i, j = _pairs(A, B)
    ha, hb = np.zeros(len(A), bool), np.zeros(len(B), bool)
    if not len(i):
        return ha, hb
    x = tri_tri_cross(A[i], B[j])
    x &= ~(_rests_on(A[i], B[j], rest) | _rests_on(B[j], A[i], rest))
    ha[i[x]] = True
    hb[j[x]] = True
    return ha, hb


# ---- vehicles -------------------------------------------------------------------------------------------------------------
def vehicle_bodies(vt, base=BODY_BASE_M):
    """The bodies of the vehicles drawn as box_triangles (12 triangles each, the bottom face first, then the top): (n, 8, 3)
    corners, the bottom raised base m towards the top (the body over the wheels). Corners: v000 v100 v110 v010 v001 v101 v111
    v011 (length, side, up)."""
    n = len(vt) // 12
    if not n:
        return np.zeros((0, 8, 3))
    t = vt[:n * 12].reshape(n, 12, 3, 3)
    bot = np.stack([t[:, 0, 0], t[:, 0, 1], t[:, 0, 2], t[:, 1, 2]], axis=1)      # v000 v100 v110 v010
    top = np.stack([t[:, 2, 0], t[:, 2, 1], t[:, 2, 2], t[:, 3, 2]], axis=1)      # v001 v101 v111 v011
    h = np.linalg.norm(top[:, 0] - bot[:, 0], axis=1)
    f = np.clip(base / np.maximum(h, 1e-9), 0.0, 0.9)[:, None, None]
    return np.concatenate([bot + f * (top - bot), top], axis=1)


def _body_triangles(c):
    """The 12 triangles (12, 3, 3) of a body's corners c (8, 3)."""
    q = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (3, 2, 6, 7), (0, 3, 7, 4), (1, 2, 6, 5)]
    return np.array([[c[a], c[b], c[d]] for a, b, cc, d in q for (a, b, d) in ((a, b, cc), (a, cc, d))])


def _inside_body(P, c):
    """Bool (k,): points P (k, 3) inside the body with corners c (8, 3)."""
    o = c[0]
    ax = [c[1] - o, c[3] - o, c[4] - o]
    ok = np.ones(len(P), bool)
    for v in ax:
        s = ((P - o) @ v) / max(float(v @ v), 1e-12)
        ok &= (s > 1e-6) & (s < 1 - 1e-6)
    return ok


def vehicle_hits(vt, others, base=BODY_BASE_M):
    """Bool per vehicle (vt: their triangles, 12 each, box_triangles' order): its body cuts one of others (n, 3, 3) or holds
    one's corner."""
    bodies = vehicle_bodies(vt, base)
    out = np.zeros(len(bodies), bool)
    if not len(others):
        return out
    olo, ohi = _aabb(others)
    for k, c in enumerate(bodies):
        lo, hi = c.min(0), c.max(0)
        near = np.nonzero(((olo <= hi) & (ohi >= lo)).all(axis=1))[0]
        if not len(near):
            continue
        O = others[near]
        if _inside_body(O.reshape(-1, 3), c).any():
            out[k] = True
            continue
        bt = _body_triangles(c)
        i, j = np.meshgrid(np.arange(len(bt)), np.arange(len(O)), indexing="ij")
        out[k] = bool(tri_tri_cross(bt[i.ravel()], O[j.ravel()]).any())
    return out


def struck(vt1, others, base=BODY_BASE_M):
    """Bool (len others,): which of others (n, 3, 3) one vehicle's body (vt1: its 12 triangles) cuts or holds a corner of."""
    out = np.zeros(len(others), bool)
    if not len(others):
        return out
    c = vehicle_bodies(vt1, base)[0]
    olo, ohi = _aabb(others)
    lo, hi = c.min(0), c.max(0)
    near = np.nonzero(((olo <= hi) & (ohi >= lo)).all(axis=1))[0]
    if not len(near):
        return out
    O = others[near]
    hit = _inside_body(O.reshape(-1, 3), c).reshape(-1, 3).any(axis=1)
    bt = _body_triangles(c)
    i, j = np.meshgrid(np.arange(len(bt)), np.arange(len(O)), indexing="ij")
    x = tri_tri_cross(bt[i.ravel()], O[j.ravel()]).reshape(len(bt), len(O)).any(axis=0)
    out[near] = hit | x
    return out


def _depth_in(O, c, n_long=9, n_wide=5):
    """m the surfaces O (n, 3, 3) reach into the body c (8, 3 corners) at most: over a grid of its footprint, each surface's
    height there inside the body's span, the lesser of its height over the body's base and under its top (the shorter move
    that clears it: a deck 1 m too low, a slope 2 m into it). 0 when none is over the footprint inside the span."""
    o, ax, ay = c[0], c[1] - c[0], c[3] - c[0]
    u, v = np.meshgrid(np.linspace(0.02, 0.98, n_long), np.linspace(0.02, 0.98, n_wide))
    S = o[:2] + u.ravel()[:, None] * ax[:2] + v.ravel()[:, None] * ay[:2]
    base = o[2] + u.ravel() * ax[2] + v.ravel() * ay[2]
    top = base + (c[4, 2] - c[0, 2])
    a, b, d = O[:, 0], O[:, 1], O[:, 2]
    e1, e2 = b[:, :2] - a[:, :2], d[:, :2] - a[:, :2]
    den = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    ok = np.abs(den) > 1e-12
    if not ok.any():
        return 0.0
    a, b, d, e1, e2, den = a[ok], b[ok], d[ok], e1[ok], e2[ok], den[ok]
    w = S[:, None, :] - a[None, :, :2]
    l1 = (w[..., 0] * e2[:, 1] - w[..., 1] * e2[:, 0]) / den
    l2 = (e1[:, 0] * w[..., 1] - e1[:, 1] * w[..., 0]) / den
    inside = (l1 >= 0) & (l2 >= 0) & (l1 + l2 <= 1)
    z = a[:, 2] + l1 * (b[:, 2] - a[:, 2]) + l2 * (d[:, 2] - a[:, 2])
    pen = np.minimum(z - base[:, None], top[:, None] - z)
    pen = np.where(inside & (pen > 0), pen, 0.0)
    return float(pen.max()) if pen.size else 0.0


def hit_report(vt, kinds, tris, cls, hit, names, base=BODY_BASE_M):
    """[{kind, x, y, z, heading, hits: {class name: how deep m}}] of the vehicles hit marks (vt: 12 triangles each, kinds: each
    one's name, tris / cls: the scene and its classes, names: class -> name): what each one cuts, and how far into its body
    the deepest corner of it reaches (0 for an edge crossing alone). The cases an analysis starts from."""
    bodies = vehicle_bodies(vt, base)
    out = []
    for k in np.flatnonzero(hit):
        c = bodies[k]
        what, parts = {}, {}
        for cl in sorted(set(int(v) for v in np.unique(cls))):
            O = tris[cls == cl]
            if not len(O):
                continue
            s = struck(vt[12 * k:12 * (k + 1)], O, base)
            if not s.any():
                continue
            name = names.get(cl, str(cl))
            what[name] = round(_depth_in(O, c), 2)
            P = O[s]
            parts[name] = {"n": int(s.sum()), "x": round(float(P[:, :, 0].mean()), 1), "y": round(float(P[:, :, 1].mean()), 1),
                           "z_lo": round(float(P[:, :, 2].min()), 2), "z_hi": round(float(P[:, :, 2].max()), 2)}
        ax = c[1] - c[0]
        mid = c.mean(axis=0)
        out.append({"kind": kinds[k], "x": round(float(mid[0]), 1), "y": round(float(mid[1]), 1),
                    "z": round(float(c[:4, 2].mean() - base), 2), "body_base": round(float(c[:4, 2].min()), 2),
                    "body_top": round(float(c[4:, 2].max()), 2),
                    "heading": round(float(np.degrees(np.arctan2(ax[0], ax[1])) % 360), 0), "hits": what, "struck": parts})
    return out


# ---- crossings at different levels ------------------------------------------------------------------------------------------
def _level(ranges, f):
    for a, b, k in ranges:
        if a <= f <= b:
            return k
    return "ground"


def _length(xy):
    return float(np.hypot(*np.diff(xy, axis=0).T).sum()) if len(xy) >= 2 else 0.0


RANK = {"bridge": 2, "ground": 1, "tunnel": 0}


def level_crossings(lines):
    """[(x, y, upper, lower)] where two of the map's lines cross at different levels; upper / lower: (kind, xy, ranges, rail,
    length): kind "bridge", "ground" or "tunnel" there (the map's, a short tunnel included). lines: {"road": [(xy, ranges)],
    "rail": [...]} (earth_view.map_lines)."""
    L = [(np.asarray(xy, np.float64), r, kind == "rail") for kind in ("road", "rail") for xy, r in (lines or {}).get(kind, [])]
    out = []
    for p in range(len(L)):
        A, ra, rail_a = L[p]
        if len(A) < 2:
            continue
        cum_a = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(A, axis=0).T))])
        for q in range(p + 1, len(L)):
            B, rb, rail_b = L[q]
            if len(B) < 2:
                continue
            cum_b = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(B, axis=0).T))])
            a0, d1 = A[:-1], np.diff(A, axis=0)
            b0, d2 = B[:-1], np.diff(B, axis=0)
            for i in range(len(a0)):
                den = d1[i, 0] * d2[:, 1] - d1[i, 1] * d2[:, 0]
                ok = np.abs(den) > 1e-12
                den = np.where(ok, den, 1.0)
                w = b0 - a0[i]
                ta = (w[:, 0] * d2[:, 1] - w[:, 1] * d2[:, 0]) / den
                tb = (w[:, 0] * d1[i, 1] - w[:, 1] * d1[i, 0]) / den
                for j in np.nonzero(ok & (ta > 0) & (ta < 1) & (tb > 0) & (tb < 1))[0]:
                    fa = (cum_a[i] + ta[j] * (cum_a[i + 1] - cum_a[i])) / cum_a[-1]
                    fb = (cum_b[j] + tb[j] * (cum_b[j + 1] - cum_b[j])) / cum_b[-1]
                    ka, kb = _level(ra, fa), _level(rb, fb)
                    if ka == kb:
                        continue
                    x, y = a0[i] + ta[j] * d1[i]
                    la = (ka, A, ra, rail_a, cum_a[-1])
                    lb = (kb, B, rb, rail_b, cum_b[-1])
                    up, lo = (la, lb) if RANK[ka] > RANK[kb] else (lb, la)
                    out.append((float(x), float(y), up, lo))
    return out


def _dist_xy(P, line):
    """Distances (k,) in plan from points P (k, 2) to the polyline line (m, 2)."""
    a, b = line[:-1], line[1:]
    ab = b - a
    t = np.clip(((P[:, None] - a[None]) * ab[None]).sum(2) / np.maximum((ab * ab).sum(1), 1e-12)[None], 0.0, 1.0)
    return np.hypot(*(a[None] + ab[None] * t[..., None] - P[:, None]).transpose(2, 0, 1)).min(axis=1)


def _surface(side, tris, cls, C, near, classes):
    """Indices of the triangles that are this line's surface at the crossing (near: the triangles round it)."""
    kind, xy, ranges, rail, length = side
    G, D, R, T = classes["ground"], classes["deck"], classes["rail"], classes["tunnel"]
    if kind == "bridge":
        want = (cls == D) | (cls == R) if rail else (cls == D)
        return near[want[near]]
    if kind == "tunnel" and not rail:
        # (a short road tunnel is opened by Earth: its road painted on the ground, like a ground road)
        tun = [(a, b) for a, b, k in ranges if k == "tunnel"]
        if not any((b - a) * length <= SHORT_TUNNEL_M for a, b in tun):
            return near[cls[near] == T]
        kind = "ground"
    if kind == "tunnel":
        if rail and not classes.get("rail_tunnels", True):
            # (the Earth tree under test draws no tube for a railway in a tunnel, railways.TUNNELS_ON off: the tunnel-class
            # triangles round the crossing are the road tunnels' and the cuttings' walls, not this railway's; counted as
            # its surface, Vienna's tram and subway tunnels under a busway and a trench "cut" 8 and 12 times there)
            return near[:0]
        return near[cls[near] == T]
    if rail:
        return near[cls[near] == R]
    g = near[np.isin(cls[near], G)]
    return g[_dist_xy(C[g, :2], xy) <= GROUND_CORRIDOR_M]


def penetrations(tris, cls, lines, classes, near_m=CROSS_NEAR_M, xings=None):
    """(hit (len tris,) bool, crossings [(x, y, cut)]): at every crossing of the map's lines at different levels, the triangles
    of the upper line's surface and of the lower's cutting through each other (cutting). classes: {"ground": (ids,), "deck":
    id, "rail": id, "tunnel": id, "rail_tunnels": whether the tree draws a railway's tunnel tube (default True)} (the viewer's
    classes). A line's surface: a road bridge its deck, a railway its track (and its
    deck on a bridge), a long tunnel its tube, a ground road its ground GROUND_CORRIDOR_M round its line. xings: those crossings
    only (level_crossings' items), else all of lines'."""
    hit = np.zeros(len(tris), bool)
    out = []
    if not len(tris):
        return hit, out
    C = tris.mean(axis=1)
    lo, hi = tris[:, :, :2].min(axis=1), tris[:, :, :2].max(axis=1)
    for x, y, up, low in (level_crossings(lines) if xings is None else xings):
        near = np.nonzero((lo[:, 0] <= x + near_m) & (hi[:, 0] >= x - near_m) &
                          (lo[:, 1] <= y + near_m) & (hi[:, 1] >= y - near_m))[0]
        if not len(near):
            continue
        a = _surface(up, tris, cls, C, near, classes)
        b = _surface(low, tris, cls, C, near, classes)
        b = np.setdiff1d(b, a)
        if not len(a) or not len(b):
            continue
        ha, hb = cutting(tris[a], tris[b])
        cut = bool(ha.any())
        hit[a[ha]] = True
        hit[b[hb]] = True
        out.append((x, y, cut))
    return hit, out


def count_cut(crossings, box=None):
    """The crossings that cut, inside box (x0, y0, x1, y1) if given; crossings closer than CROSS_NEAR_M are one."""
    pts = [(x, y) for x, y, cut in crossings if cut and (box is None or (box[0] <= x <= box[2] and box[1] <= y <= box[3]))]
    kept = []
    for p in pts:
        if all(math.hypot(p[0] - q[0], p[1] - q[1]) > CROSS_NEAR_M for q in kept):
            kept.append(p)
    return len(kept)
