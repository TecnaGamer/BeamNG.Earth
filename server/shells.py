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
import numpy as np

WELD_M = 0.001
SINK_M = 0.10           # m a bottom is sunk under the piece's foot (sidewalks.SINK)
UNDER_NZ = 0.5
SLIT_M2 = 1e-4          # m²: a loop enclosing less is a T-junction's slit, not an opening
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
    key = np.sort(H, 1)
    _, inv_e, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    b = cnt[inv_e.ravel()] == 1
    H, Ho = H[b], Ho[b]
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
    """Triangles (into Q (k, 3)) closing the ring Q, facing want: ear clipping in the plane âŸ‚ want, else a fan from
    the mean point (index k: the caller adds it). (tris, fanned)."""
    n = want / (np.linalg.norm(want) or 1.0)
    u = np.cross(n, [0.0, 0.0, 1.0] if abs(n[2]) < 0.9 else [1.0, 0.0, 0.0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    c = Q.mean(0)
    xy = np.c_[(Q - c) @ u, (Q - c) @ v]
    t, whole = _tri2d(xy)
    k = len(Q)
    if not whole:
        t = np.array([[k, i, (i + 1) % k] for i in range(k)], np.int64)
    # each triangle facing want
    R = np.vstack([Q, c])
    nn = np.cross(R[t[:, 1]] - R[t[:, 0]], R[t[:, 2]] - R[t[:, 0]])
    flip = nn @ n < 0
    t[flip] = t[flip][:, ::-1]
    return t, not whole


def close(P, T, TM=None, per_vertex=(), mouths=True, sink=SINK_M):
    """The mesh (P (n, 3), T (m, 3), TM (m,) or None) closed: (P, T, TM, per-vertex arrays) with the skirts, bottoms
    and caps added (per_vertex: arrays (n, ...) or None, copied for the new vertices; a "normals" array is best given
    as None and computed). Pure but for the module's stats."""
    P = np.asarray(P, np.float64).reshape(-1, 3)
    T = np.asarray(T, np.int64).reshape(-1, 3)
    stats["meshes"] += 1
    stats["tris_in"] += len(T)
    ls = loops(P, T)
    if not ls:
        return P, T, TM, list(per_vertex)
    newP, newT, newM, src = [], [], [], []
    base = len(P)
    tm = None if TM is None else np.asarray(TM).ravel()
    tri_of = None
    if tm is not None:          # a loop's material: its first vertex's triangle's
        tri_of = np.zeros(len(P), np.int64)
        tri_of[T.ravel()] = np.repeat(np.arange(len(T)), 3)
    for loop in ls:
        L = np.asarray(loop, np.int64)
        Q = P[L]
        n = np.cross(Q, np.roll(Q, -1, 0)).sum(0) * 0.5
        nl = np.linalg.norm(n)
        if nl < SLIT_M2:        # a T-junction's slit (collinear edges along another's): no gap to close
            continue
        mat = int(tm[tri_of[L[0]]]) if tm is not None else 0
        k = len(L)
        if n[2] >= UNDER_NZ * nl:
            # skirt: the loop's vertices copied (top ring) and lowered (bottom ring)
            top = base + len(src)
            src.extend(L.tolist())
            newP.append(Q)
            bot = base + len(src)
            src.extend(L.tolist())
            newP.append(Q - [0.0, 0.0, sink])
            for i in range(k):
                j = (i + 1) % k
                a, b, a2, b2 = top + i, top + j, bot + i, bot + j
                newT += [[b, a, a2], [b, a2, b2]]
                newM += [mat, mat]
            t, fan = _cap(Q - [0.0, 0.0, sink], np.array([0.0, 0.0, -1.0]))
            ring = bot
        elif not mouths:
            continue
        else:
            t, fan = _cap(Q, -n)
            ring = base + len(src)
            src.extend(L.tolist())
            newP.append(Q)
        if fan:
            stats["fanned"] += 1
            src.append(L[0])
            newP.append(Q.mean(0)[None])
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
