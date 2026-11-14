"""Every mesh Earth writes closed into a solid (the user, 2026-11-07: every piece Earth builds is a full closed 3D object,
sides, ends and bottom, so a piece slightly out of place, floating a while, or seen from an odd angle shows no
see-through hole; closed bottoms sunk slightly under the ground). cdae.encode closes each mesh it writes (close()).

A mesh's openings are its boundary loops: the edges (vertices welded at WELD_M) used by one triangle, chained along
their triangles' winding. Each loop is closed by its kind, from its Newell normal n (the loop in its half-edges'
direction; n points out of the opening's missing face's back):
  under    n up (n_z >= UNDER_NZ |n|: an open bottom, a ribbon's outline, a wall's foot): a skirt straight down
           SINK_M from every loop edge and the lowered loop capped in plan, facing down: the piece's sunk bottom
  plane    otherwise (an end, a side, a top opening): the loop capped in its own plane (ear clipping on the plane
           the loop's mean point and n give), facing -n
A loop the plan / plane ear clipping cannot take whole (not simple there) is fanned from its mean point, so no
opening is left whatever its shape. New vertices are copies (their uv / colour the loop vertex's; a cap's normals
the cap's own when the mesh came with normals). mouths=False (the tunnels' tubes): plane loops left open, the
tubes' mouths being the way in. Pure, deterministic."""
import os

import numpy as np

# Off by default (merged from PR #987 behind a switch: 1.1-1.37x the triangles of every road / building tile in the
# PR's own counts, and a Python pass over every mesh at its write): BNE_WATERTIGHT=on closes the meshes (cdae.write),
# the buildings (buildings.build / collision_mesh / _sky_mesh), the deck slabs from their tops and puts the markings
# on decks on the deck surface (roadmesh._marks_on_decks, _overlays_on_decks)
ON = os.environ.get("BNE_WATERTIGHT", "off").lower() in ("1", "on", "true")

WELD_M = 0.001
SINK_M = 0.10           # m a bottom is sunk under the piece's foot (sidewalks.SINK)
UNDER_NZ = 0.5
BOTTOM_TOL_M = 0.05     # m a coarse bottom may be off its loop (half the skirt: it stays under the piece)

SLIT_W_M = 0.002        # m: ... or narrower than this on average (its area over half its perimeter)
SLIT_M2 = 1e-4          # m2: a loop enclosing less is a T-junction's slit, not an opening
stats = {"meshes": 0, "loops": 0, "tris_in": 0, "tris_added": 0, "fanned": 0}


def _weld(P):
    q = np.round(np.asarray(P, np.float64) / WELD_M).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    return first, inv.ravel()


def loops(P, T):
    """The boundary loops of the mesh: [list of original vertex indices, in the half-edges' direction]. Pure."""
    T = np.asarray(T, np.int64).reshape(-1, 3)
    if not len(T):
        return []
    first, inv = _weld(P)
    Tw = inv[T]
    ok = (Tw[:, 0] != Tw[:, 1]) & (Tw[:, 1] != Tw[:, 2]) & (Tw[:, 0] != Tw[:, 2])
    T, Tw = T[ok], Tw[ok]
    H = np.concatenate([Tw[:, [0, 1]], Tw[:, [1, 2]], Tw[:, [2, 0]]])
    Ho = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]])
    # the open half-edges: per edge, its uses one way less the other way's (a manifold edge's one use; an edge three
    # faces share, the one left over), so every vertex has as many going out as coming in and every walk closes
    key = np.sort(H, 1)
    sgn = np.where(H[:, 0] < H[:, 1], 1, -1)
    _, inv_e = np.unique(key, axis=0, return_inverse=True)
    inv_e = inv_e.ravel()
    sur = np.bincount(inv_e, weights=sgn).astype(np.int64)
    s_e = sur[inv_e]
    pick = np.zeros(len(H), bool)
    order = np.argsort(inv_e, kind="stable")
    seen = {}
    for h in order.tolist():                    # each edge's surplus taken from its half-edges going its way
        e = int(inv_e[h])
        if s_e[h] != 0 and np.sign(s_e[h]) == sgn[h] and seen.get(e, 0) < abs(int(s_e[h])):
            pick[h] = True
            seen[e] = seen.get(e, 0) + 1
    H, Ho = H[pick], Ho[pick]
    nxt = {}
    for (a, c), (oa, _oc) in zip(H.tolist(), Ho.tolist()):
        nxt.setdefault(a, []).append((c, oa))
    out = []
    while nxt:
        start = next(iter(nxt))
        loop, v = [], start
        while v in nxt:
            c, oa = nxt[v].pop()
            if not nxt[v]:
                del nxt[v]
            loop.append(oa)
            v = c
            if v == start:
                break
        if len(loop) >= 3:
            out.append(loop)
    return out


def open_loops(P, T):
    """loops() but the slits: boundary loops touching each other (sharing a vertex) are taken together, and a group
    enclosing under SLIT_M2 (its loops' Newell vectors summed) is a T-junction's slit, a long edge along shorter ones,
    no gap. The mesh's real openings. Pure."""
    P = np.asarray(P, np.float64).reshape(-1, 3)
    ls = loops(P, T)
    if not ls:
        return []
    _first, inv = _weld(P)
    # a loop with no edge used by a single face is faces sharing edges the wrong way round (an edge three or four
    # faces meet at, a flipped face): no gap, nothing to see through
    Tw = inv[np.asarray(T, np.int64).reshape(-1, 3)]
    key = np.sort(np.concatenate([Tw[:, [0, 1]], Tw[:, [1, 2]], Tw[:, [2, 0]]]), 1)
    u, cnt = np.unique(key, axis=0, return_counts=True)
    single = {tuple(x) for x in u[cnt == 1].tolist()}

    def has_single(loop):
        w = inv[np.asarray(loop)]
        return any((min(a, b), max(a, b)) in single for a, b in zip(w.tolist(), np.roll(w, -1).tolist()))
    ls = [l for l in ls if has_single(l)]
    if not ls:
        return []
    parent = list(range(len(ls)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    owner = {}
    for i, loop in enumerate(ls):
        for v in inv[np.asarray(loop)].tolist():
            j = owner.setdefault(v, i)
            if j != i:
                parent[root(i)] = root(j)
    area, per, own = {}, {}, []
    for i, loop in enumerate(ls):
        Q = P[loop]
        own.append(np.linalg.norm(np.cross(Q, np.roll(Q, -1, 0)).sum(0)) * 0.5 >= SLIT_M2)  # (a slit on its own)
        area[root(i)] = area.get(root(i), 0.0) + np.cross(Q, np.roll(Q, -1, 0)).sum(0) * 0.5
        per[root(i)] = per.get(root(i), 0.0) + float(np.linalg.norm(np.roll(Q, -1, 0) - Q, axis=1).sum())
    return [loop for i, loop in enumerate(ls)
            if own[i] and np.linalg.norm(area[root(i)]) >= max(SLIT_M2, SLIT_W_M * 0.5 * per[root(i)])]


def _tri2d(xy):
    """Ear-clipped triangles of the ring xy (k, 2) (either winding) and whether it took the ring whole."""
    import fast
    k = len(xy)
    a = 0.5 * float(np.sum(xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1]))
    order = np.arange(k) if a > 0 else np.arange(k)[::-1]
    try:
        t = np.asarray(fast._ef.triangulate(np.ascontiguousarray(xy[order], np.float64)), np.int64).reshape(-1, 3)
    except Exception:
        t = np.zeros((0, 3), np.int64)
    t = order[t] if len(t) else t
    return t, len(t) == k - 2


def _cap(Q, want):
    """Triangles (into Q (k, 3)) closing the ring Q, facing want: ear clipping in the plane normal to want, else a fan from
    the mean point (index k: the caller adds it). (tris, fanned)."""
    n = want / (np.linalg.norm(want) or 1.0)
    u = np.cross(n, [0.0, 0.0, 1.0] if abs(n[2]) < 0.9 else [1.0, 0.0, 0.0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    c = Q.mean(0)
    xy = np.c_[(Q - c) @ u, (Q - c) @ v]
    t, whole = _tri2d(xy)
    k = len(Q)
    if whole:               # a closed fan: ring edges used once, the inner ones twice (a ring folded in the plane fools
        # the count)
        e = np.sort(np.r_[t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]], 1)
        ring = np.sort(np.c_[np.arange(k), (np.arange(k) + 1) % k], 1)
        u, cnt = np.unique(e, axis=0, return_counts=True)
        rs = {tuple(r) for r in ring.tolist()}
        isr = np.array([tuple(x) in rs for x in u.tolist()], bool)
        whole = int(isr.sum()) == k and bool((cnt[isr] == 1).all()) and bool((cnt[~isr] == 2).all())
    if not whole:
        t = np.array([[k, i, (i + 1) % k] for i in range(k)], np.int64)
    # each triangle facing want
    R = np.vstack([Q, c])
    nn = np.cross(R[t[:, 1]] - R[t[:, 0]], R[t[:, 2]] - R[t[:, 0]])
    flip = nn @ n < 0
    t[flip] = t[flip][:, ::-1]
    return t, not whole


def _coarse(Q, tol=BOTTOM_TOL_M):
    """Indices of the closed ring Q (k, 3) a coarse bottom keeps (Douglas-Peucker at tol, from 0 and the point
    farthest from it), sorted: the bottom never more than tol off the ring, so it stays under its skirt's top."""
    import sidewalks
    k = len(Q)
    if k <= 4:
        return np.arange(k)
    f = int(np.argmax(np.linalg.norm(Q - Q[0], axis=1)))
    a = sidewalks.simplify(Q[:f + 1], tol)
    b = sidewalks.simplify(np.vstack([Q[f:], Q[:1]]), tol) + f
    keep = np.unique(np.r_[a, b[b < k]])
    return keep if len(keep) >= 3 else np.arange(k)


def close(P, T, TM=None, per_vertex=(), mouths=True, sink=SINK_M, open_tris=None, skirt=True):
    """_close, and again on what it leaves (a loop that went with a slit group the first time). See _close."""
    n0 = len(np.asarray(T).reshape(-1, 3))
    P2, T2, TM2, pv = _close(P, T, TM, per_vertex, mouths, sink, open_tris, skirt)
    if len(T2) > n0:
        ot = None if open_tris is None else np.r_[np.asarray(open_tris, bool).ravel(), np.zeros(len(T2) - n0, bool)]
        stats["meshes"] -= 1
        stats["tris_in"] -= len(T2)
        P2, T2, TM2, pv = _close(P2, T2, TM2, pv, mouths, sink, ot, skirt)
    return P2, T2, TM2, pv


def _close(P, T, TM=None, per_vertex=(), mouths=True, sink=SINK_M, open_tris=None, skirt=True):
    """The mesh (P (n, 3), T (m, 3), TM (m,) or None) closed: (P, T, TM, per-vertex arrays) with the skirts, bottoms
    and caps added (per_vertex: arrays (n, ...) or None, copied for the new vertices; a "normals" array is best given
    as None and computed). open_tris (m,) bool: triangles left as they are (overlays lying on a closed surface: the
    road mesh's markings and edge blends), their edges no openings. skirt=False: a piece already sunk (a building's
    walls reach buildings.SKIRT_M under the ground): its open bottoms capped where they are, no skirt. Pure but for the module's stats."""
    P = np.asarray(P, np.float64).reshape(-1, 3)
    T = np.asarray(T, np.int64).reshape(-1, 3)
    stats["meshes"] += 1
    stats["tris_in"] += len(T)
    shut = np.arange(len(T)) if open_tris is None else np.flatnonzero(~np.asarray(open_tris, bool).ravel())
    ls = open_loops(P, T[shut])        # (a T-junction slit group left: no gap)
    if not ls:
        return P, T, TM, list(per_vertex)
    newP, newT, newM, src = [], [], [], []
    base = len(P)
    tm = None if TM is None else np.asarray(TM).ravel()
    tri_of = None
    if tm is not None:          # a loop's material: its first vertex's triangle's
        tri_of = np.zeros(len(P), np.int64)
        tri_of[T[shut].ravel()] = np.repeat(shut, 3)
    for loop in ls:
        L = np.asarray(loop, np.int64)
        Q = P[L]
        n = np.cross(Q, np.roll(Q, -1, 0)).sum(0) * 0.5
        nl = np.linalg.norm(n)
        if nl < SLIT_M2:        # a T-junction's slit (collinear edges along another's): no gap to close
            continue
        mat = int(tm[tri_of[L[0]]]) if tm is not None else 0
        k = len(L)
        if n[2] >= UNDER_NZ * nl and skirt:
            # skirt: the loop's vertices copied (top ring) down to a coarse lowered ring (the bottom's)
            K = _coarse(Q)
            top = base + len(src)
            src.extend(L.tolist())
            newP.append(Q)
            bot = base + len(src)
            src.extend(L[K].tolist())
            Qb = Q[K] - [0.0, 0.0, sink]
            newP.append(Qb)
            m_ = len(K)
            j = 0                               # the bottom vertex under top edge (i, i + 1): K[j] <= i
            for i in range(k):
                i1 = (i + 1) % k
                newT.append([top + i1, top + i, bot + j])
                newM.append(mat)
                nxt = K[j + 1] if j + 1 < m_ else k
                if i + 1 == nxt:
                    j2 = (j + 1) % m_
                    newT.append([top + i1, bot + j, bot + j2])
                    newM.append(mat)
                    j = j2 if j + 1 < m_ else j
            t, fan = _cap(Qb, np.array([0.0, 0.0, -1.0]))
            k = m_
            ring, capQ = bot, Qb
        elif not mouths:
            continue
        else:
            # capped on every ring point (edge to edge: a cap edge along several short ones, a T-join, flickers)
            K = np.arange(len(Q))     # (every ring point: edge to edge, no T-join)
            t, fan = _cap(Q[K], -n)
            ring, capQ = base + len(src), Q[K]
            src.extend(L[K].tolist())
            newP.append(Q[K])
        if fan:
            stats["fanned"] += 1
            src.append(int(L[0]))
            newP.append(capQ.mean(0)[None])
        newT += (t + ring).tolist()
        newM += [mat] * len(t)
        stats["loops"] += 1
    if not newT:
        return P, T, TM, list(per_vertex)
    src = np.asarray(src, np.int64)
    P2 = np.vstack([P] + newP)
    T2 = np.vstack([T, np.asarray(newT, np.int64)])
    stats["tris_added"] += len(newT)
    TM2 = None if tm is None else np.r_[tm, np.asarray(newM, tm.dtype)]
    pv = []
    for a in per_vertex:
        if a is None:
            pv.append(None)
            continue
        a = np.asarray(a)
        a = a.reshape(len(P), -1) if a.ndim == 1 or len(a) != len(P) else a     # (flat arrays: per vertex rows)
        pv.append(np.concatenate([a, a[src]]))
    return P2, T2, TM2, pv
