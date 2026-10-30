"""Road surfaces and markings as one render-only mesh per road tile (docs/ARCHITECTURE.md "Roads").

Why not DecalRoads (2026-10-11, the user's drive at speed): a DecalRoad re-clips itself to the
terrain whenever the TerrainBlock changes, and ours moves every few seconds at speed. With 3,740
DecalRoads one block move cost a 140-148 ms frame (9 ms without them; ~37 us per decal), so the
game stuttered on every block commit. The official maps never move their terrain.

Here every chunk of roads.build_tile (asphalt, cobble strips, lines, gutters, drains) becomes a
ribbon (the DecalRoad's own layout: u 0..1 across, v = distance / texture length along) draped on the
graded ground: the ribbon is cut every DRAPE_STEP m and both edges sampled from the same elevation
function as the physics block (raised ROAD_LIFT under roads over the visible ground tiles, so the
mesh lies on what the tyres touch and above what is drawn), in one call per tile, plus its layer's
LIFT (markings over the asphalt). The stock decal
materials render on a mesh (probe 2026-10-11: they are translucent with zwrite, so overlapping
ribbons in one mesh draw in a fixed order and do not flicker). No collision: the tyres stay on the
terrain, whose surface masks carry the asphalt / dirt ground models. Pure given elev.
"""
import math

import numpy as np

import cdae

DRAPE_STEP = 2.0        # m between ribbon cross-sections (the physics block's lattice)
LIFT = {"base": 0.0, "edge": 0.003, "gutter": 0.005, "mark": 0.01}   # m above the physics ground (lifted under roads
                                                        # by grading.ROAD_LIFT over the visible ground)


def layer_of(chunk):
    if chunk.get("layer") in LIFT:             # a chunk naming its own (rails.py: sleepers over ballast, rails over both)
        return chunk["layer"]
    m = chunk["m"]
    if m.startswith("line_") or m.startswith("crossing") or m == "m_metal_grid_drain":
        return "mark"
    if m == "gutter1":
        return "gutter"
    return "base"


def _resample(pts, step, keep_vertices=False):
    """Points every `step` m along a polyline (k, 3), ends kept: (xy (m, 2), distance (m,), z (m,))."""
    seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
    s = np.concatenate(([0.0], np.cumsum(seg)))
    if s[-1] <= 1e-6:
        return pts[:1, :2], s[:1], pts[:1, 2]
    n = max(1, int(np.ceil(s[-1] / step)))
    t = np.linspace(0.0, s[-1], n + 1)
    if keep_vertices:
        # Prefer the original profile knots over almost coincident lattice
        # samples: tiny segments destabilize the ribbon's miter normals.
        j = np.searchsorted(s, t)
        distance = np.minimum(np.abs(t - s[np.maximum(j - 1, 0)]),
                              np.abs(t - s[np.minimum(j, len(s) - 1)]))
        t = np.unique(np.r_[t[distance > 1e-8], s])
    return (np.stack([np.interp(t, s, pts[:, 0]), np.interp(t, s, pts[:, 1])], axis=1), t,
            np.interp(t, s, pts[:, 2]))


def _normals(xy):
    d = np.diff(xy, axis=0)
    L = np.hypot(d[:, 0], d[:, 1])
    L[L < 1e-9] = 1e-9
    t = d / L[:, None]
    ns = np.stack([-t[:, 1], t[:, 0]], axis=1)
    n = np.empty((len(xy), 2))
    n[0], n[-1] = ns[0], ns[-1]
    if len(xy) > 2:
        m = ns[:-1] + ns[1:]
        ml = np.hypot(m[:, 0], m[:, 1])
        ml[ml < 1e-9] = 1.0
        m = m / ml[:, None]
        cos = np.clip((m * ns[1:]).sum(1), 0.5, 1.0)
        n[1:-1] = m / cos[:, None]
    return n


def ribbon_pairs(chunks):
    """[(ribbon, its chunk)]: ribbons() of each chunk with it (ribbons() leaves out a chunk too short)."""
    return [(r, c) for c in chunks for r in ribbons([c])]


def ribbons(chunks):
    """Per chunk: (left (k, 2), right (k, 2), v (k,), material, layer, z (k,) or None) in the anchor
    frame; z: the chunk's own heights where it is "fixed" (bridge decks), else None (draped). Pure. The draped and
    plain chunks' resampling and normals in one Rust call (earthfast.ribbon_edges: numpy per chunk was ~0.9 s of 8
    city road tiles, 2026-10-28); a fixed deck's (its profile's knots kept) here."""
    import fast
    out = [None] * len(chunks)
    batch, pts, half = [], [], []
    for k, c in enumerate(chunks):
        p = np.asarray(c["n"], np.float64)
        if len(p) < 2:
            continue
        if c.get("deck") and c.get("fixed"):
            xy, s, z = _resample(p, DRAPE_STEP, keep_vertices=True)
            if len(xy) < 2:
                continue
            n = _normals(xy) * (float(c["w"]) / 2)
            out[k] = (xy + n, xy - n, s / float(c.get("tl") or 10.0), c["m"], layer_of(c), z)
            continue
        batch.append(k)
        pts.append(p[:, :3])
        half.append(float(c["w"]) / 2)
    if batch:
        fast.require(90, "ribbon_edges")
        off = np.r_[0, np.cumsum([len(p) for p in pts])].astype(np.int64)
        L, R, S, Z, o = fast._ef.ribbon_edges(np.ascontiguousarray(np.concatenate(pts)), off,
                                             np.asarray(half, np.float64), float(DRAPE_STEP))
        for n, k in enumerate(batch):
            a, b = int(o[n]), int(o[n + 1])
            if b - a < 2:
                continue
            c = chunks[k]
            out[k] = (L[a:b], R[a:b], S[a:b] / float(c.get("tl") or 10.0), c["m"], layer_of(c),
                      Z[a:b] if c.get("fixed") else None)
    return [r for r in out if r is not None]


TRIM_DZ = (0.3, 4.5)                # m: another deck overlapping a deck in plan this far above or below it (two
                                    # surfaces, too near to pass over it: a ramp leaving its mainline) ...
TRIM_GAP_M = 0.05                   # ... each slab ends this short of halfway between their centre lines


def trim_overlaps(rb, context=()):
    """The deck ribbons rb ([(ribbon, chunk)]: ribbons()' (left, right, v, m, layer, z) of "fixed" decks) with each
    edge pulled in where another deck (rb's or the context's, the decks around the tile: the same trim in every tile)
    overlaps it in plan at a height TRIM_DZ apart: both end halfway between their centre lines (a gore; a parapet on the
    higher one from PARAPET_COVER_DZ apart), each road on its own surface, instead of the higher one's side standing in
    the lower one's lanes, and a car on the lower one's line riding the higher one's slab until it ends: a
    US-101 exit ramp diverging downhill under its mainline's 18.8 m deck put that deck's edge 0.9 m from the ramp's
    centre, and an AI car drove into it at 29 mph (Los Angeles, 2026-10-27). Pure."""
    if not rb:
        return rb
    others = []                         # (owner, centre (k, 2), half width (k,), z (k,))
    for n, ((left, right, v, m, layer, z), c) in enumerate(list(rb) + list(context)):
        if z is None:
            continue
        others.append((c.get("fid", ("n", n)), (left + right) / 2, np.hypot(*(left - right).T) / 2,
                       np.asarray(z, np.float64)))
    out = []
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        if z is None:
            out.append(((left, right, v, m, layer, z), c))
            continue
        own = c.get("fid", ("n", n))
        mid = (left + right) / 2
        hw = np.hypot(*(left - right).T) / 2
        u = (left - right) / np.maximum(2 * hw, 1e-9)[:, None]       # unit left
        tl, tr = np.zeros(len(mid)), np.zeros(len(mid))
        lo, hi = mid.min(0) - hw.max() - 30.0, mid.max(0) + hw.max() + 30.0
        for owner, om, ohw, oz in others:
            if owner == own or len(om) < 2:
                continue
            if (om.max(0) < lo).any() or (om.min(0) > hi).any():
                continue
            a, b = om[:-1], om[1:]
            d = b - a
            L2 = np.maximum((d * d).sum(1), 1e-12)
            t = np.clip(((mid[:, None, :] - a[None]) * d[None]).sum(2) / L2[None], 0.0, 1.0)
            q = a[None] + t[..., None] * d[None]
            dist = np.hypot(*(q - mid[:, None, :]).transpose(2, 0, 1))
            j = np.argmin(dist, 1)
            k = np.arange(len(mid))
            dc, tj, qj = dist[k, j], t[k, j], q[k, j]
            zo = oz[j] + tj * (oz[np.minimum(j + 1, len(oz) - 1)] - oz[j])
            wo = ohw[j] + tj * (ohw[np.minimum(j + 1, len(ohw) - 1)] - ohw[j])
            dz = np.abs(zo - z)
            hit = (dc < hw + wo) & (dz >= TRIM_DZ[0]) & (dz < TRIM_DZ[1]) & (dc > 1e-3)
            if not hit.any():
                continue
            cut = np.maximum(hw - (dc / 2 - TRIM_GAP_M), 0.0)
            on_left = ((qj - mid) * u).sum(1) > 0
            tl = np.where(hit & on_left, np.maximum(tl, cut), tl)
            tr = np.where(hit & ~on_left, np.maximum(tr, cut), tr)
        if not (tl.any() or tr.any()):
            out.append(((left, right, v, m, layer, z), c))
            continue
        tl = np.minimum(tl, 0.9 * hw)           # (never past the centre line: at least a tenth of the deck stays)
        tr = np.minimum(tr, 0.9 * hw)
        out.append(((left - u * tl[:, None], right + u * tr[:, None], v, m, layer, z), c))
    return out


def _clip_tris(P, T, UV, M, box):
    """The triangles (positions P (n, 3), T (m, 3), UV (n, 2), material per triangle M) cut to the box (x0, y0, x1, y1):
    those inside kept, those outside dropped, those across its edges clipped (Sutherland-Hodgman, position and uv
    interpolated) and fanned again. Pure."""
    x0, y0, x1, y1 = box
    xy = P[T][:, :, :2]
    inside = ((xy[..., 0] >= x0) & (xy[..., 0] <= x1) & (xy[..., 1] >= y0) & (xy[..., 1] <= y1)).all(1)
    out_ = ((xy[..., 0] < x0).all(1) | (xy[..., 0] > x1).all(1) | (xy[..., 1] < y0).all(1) | (xy[..., 1] > y1).all(1))
    cut = ~inside & ~out_
    if not cut.any():
        return P, T[inside], UV, M[inside]
    planes = ((0, x0, 1.0), (0, x1, -1.0), (1, y0, 1.0), (1, y1, -1.0))     # keep sign * (coord - value) >= 0
    newP, newUV, newT, newM = [], [], [], []
    n = len(P)
    for t, m in zip(T[cut], M[cut]):
        poly = [(P[i], UV[i]) for i in t]
        for ax, val, sg in planes:
            if not poly:
                break
            res = []
            for j in range(len(poly)):
                (pa, ua), (pb, ub) = poly[j], poly[(j + 1) % len(poly)]
                da, db = sg * (pa[ax] - val), sg * (pb[ax] - val)
                if da >= 0:
                    res.append((pa, ua))
                if (da >= 0) != (db >= 0):
                    f = da / (da - db)
                    res.append((pa + (pb - pa) * f, ua + (ub - ua) * f))
            poly = res
        if len(poly) < 3:
            continue
        base = n + sum(len(q) for q in newP)
        newP.append(np.array([q[0] for q in poly]))
        newUV.append(np.array([q[1] for q in poly]))
        k = len(poly)
        newT.append(np.array([(base, base + a, base + a + 1) for a in range(1, k - 1)], np.int64))
        newM.append(np.full(k - 2, m))
    if not newP:
        return P, T[inside], UV, M[inside]
    return (np.concatenate([P] + newP), np.concatenate([T[inside]] + newT), np.concatenate([UV] + newUV),
            np.concatenate([M[inside]] + newM))


def _reach_past(c, box):
    """Chunk c with each end outside the box (cut at roads.OVERLAP past the tile's edge) carried on straight by
    1.5 x its width: a ribbon's square end on a road crossing the seam at a slant left a triangle of the road
    uncovered on each side once both tiles were cut exactly at the edge (the E6 in Norway, 50 degrees). Pure."""
    pts = np.asarray(c["n"] if c.get("n") is not None else (), np.float64)
    if len(pts) < 2:
        return c
    x0, y0, x1, y1 = box
    out_ = lambda p: p[0] < x0 or p[0] > x1 or p[1] < y0 or p[1] > y1     # noqa: E731
    reach = 1.5 * float(c["w"])
    ends = []
    for at, nb in ((0, 1), (-1, -2)):
        if not out_(pts[at]):
            ends.append(None)
            continue
        t = pts[at, :2] - pts[nb, :2]
        L = float(np.hypot(*t))
        if L < 1e-6:
            ends.append(None)
            continue
        q = pts[at].copy()
        q[:2] += t / L * reach
        ends.append(q)
    if ends[0] is None and ends[1] is None:
        return c
    n = [pts]
    if ends[0] is not None:
        n.insert(0, ends[0][None])
    if ends[1] is not None:
        n.append(ends[1][None])
    return {**c, "n": np.concatenate(n).tolist()}


GRID_M = 2.0            # the terrain block's square (heightmap.SQUARE): its samples on multiples of it, each square cut
                        # from its south-west corner to its north-east one (probed in-game 2026-10-20)


FOLLOW_TOL = 0.02       # m a road triangle may depart from the drawn ground inside it before it is cut along the
                        # ground's own triangles (follow): flat graded roads stay whole, bends and junctions are cut.
                        # The ground's heights step by its quantum (1/32 m): at 5 mm nearly every triangle was cut (7-15x
                        # the triangles); at 2 cm 1.7x in Baltimore, 5.7x on the E6 in Norway, 7.5x in San Francisco


def _height_grid(frame, elev, xy, q=None, cell=GRID_M):
    """(grid [j, i], i0, j0): the terrain block's samples (elev on the cell lattice, rounded to the height quantum q
    as heightmap.write_png does) over the points xy (n, 2) and a sample around. One elev call."""
    i0 = int(np.floor(xy[:, 0].min() / cell)) - 1
    j0 = int(np.floor(xy[:, 1].min() / cell)) - 1
    i1 = int(np.ceil(xy[:, 0].max() / cell)) + 1
    j1 = int(np.ceil(xy[:, 1].max() / cell)) + 1
    gx, gy = np.meshgrid(np.arange(i0, i1 + 1) * cell, np.arange(j0, j1 + 1) * cell)
    la, lo, _ = frame.enu_to_geodetic(gx.ravel(), gy.ravel(), 0.0)
    h = np.asarray(elev(la, lo), np.float64)
    u = np.asarray(frame.geodetic_to_enu(la, lo, h)[2], np.float64).reshape(gx.shape)
    if q:
        u = np.rint(u / q) * q
    return np.ascontiguousarray(u), i0, j0


def drawn_ground(frame, drawn, xy, q=None, cell=GRID_M):
    """The drawn ground (n,) at the points xy (n, 2) over one _height_grid of drawn (lat, lon) -> metres: the block's
    squares, each cut from its south-west corner to its north-east one, a point on the grid's last line in the square
    before it - the ground follow_ground (earthfast.follow_tris) puts the road surfaces on. The sidewalks' curbs on
    the ground the road surfaces lie on (the user, 2026-10-21: sidewalks on the drawn ground with upstream's road
    surfaces): elev and the block differ by its height quantum q alone, more between its samples on hills. One drawn
    call."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    if not len(xy):
        return np.zeros(0)
    grid, i0, j0 = _height_grid(frame, drawn, xy, q, cell)
    gx, gy = xy[:, 0] / cell, xy[:, 1] / cell
    i, j = np.floor(gx).astype(np.int64), np.floor(gy).astype(np.int64)
    ny, nx = grid.shape
    i -= (i - i0 == nx - 1) & (gx == i)         # a point on the grid's last line belongs to the square before it
    j -= (j - j0 == ny - 1) & (gy == j)
    li, lj = i - i0, j - j0
    ok = (li >= 0) & (lj >= 0) & (li + 1 < nx) & (lj + 1 < ny)
    li, lj = np.clip(li, 0, nx - 2), np.clip(lj, 0, ny - 2)
    fx, fy = gx - i, gy - j
    a, b, c, d = grid[lj, li], grid[lj, li + 1], grid[lj + 1, li], grid[lj + 1, li + 1]
    z = np.where(fy > fx, a + fy * (c - a) + fx * (d - c), a + fx * (b - a) + fy * (d - b))
    return np.where(ok, z, np.nan)


FOLLOW_STEP_M = 0.9     # a terrain cell whose corners are further apart holds a step (a cutting's wall, the lattice's
FOLLOW_MAX_UP = 0.1     # teeth): a draped surface goes at most this over its lowest corner there (the McGraw-Graben's
                        # edge lines climbed the teeth as white tents up to 1.3 m, 2026-10-22). A road would need a
                        # ~32 % grade along a cell's diagonal to count as a step.


def follow_ground(P, UV, T, grid, i0, j0, tol=None, cell=GRID_M):
    """(points (m, 3), uvs, triangles, source triangle) of the triangles T over P (n, 2) on the drawn ground (grid:
    _height_grid), held down in the cells holding a step (FOLLOW_STEP_M): earthfast.follow_tris."""
    import fast
    tol = FOLLOW_TOL if tol is None else tol
    P = np.ascontiguousarray(np.asarray(P, np.float64)[:, :2])
    UV = np.ascontiguousarray(UV, np.float64)
    T = np.ascontiguousarray(T, np.int64)
    fast.require(52, "follow_tris")
    a, b, c, d = fast._ef.follow_tris(P, UV, T, float(cell), np.ascontiguousarray(grid, np.float64), int(i0), int(j0),
                                      float(tol), FOLLOW_STEP_M, FOLLOW_MAX_UP)
    return np.asarray(a), np.asarray(b), np.asarray(c), np.asarray(d)


def _shared(P, UV, T):
    """The mesh with its equal points (position and uv to 0.1 mm) made one."""
    key = np.round(np.c_[P, UV] * 1e4).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return P[first], UV[first], inv.reshape(-1)[T]


def build(frame, chunks, elev, origin, lift=0.0, clip=None, follow=False, q=None, drawn=None, context=(),
          clip_poly=None):
    """The road mesh of a tile (render only; on bridges it lies DECK_SKIN over deck()'s slab): (positions
    relative to origin, tris, uvs, materials, tri material index), or None. elev(lat, lon) -> m (graded,
    as the physics block). One elev call. lift: m more for the draped surfaces (detail mode: the physics
    block is drawn, so the asphalt must sit over it, not in it; earthd DETAIL_ROAD_LIFT). clip: the tile's box
    (x0, y0, x1, y1): the ribbons (built roads.OVERLAP past it, no gap at the seam) cut exactly at its edges; two
    tiles' meshes overlapping there at one height flickered in a 4 m band along every tile seam (the user,
    2026-10-20: z-fighting on the road texture). follow: the draped surfaces given the terrain block's own heights
    and cut along its triangles where it bends under them (follow_ground; q its height quantum), so they lie `lift`
    over the drawn ground (within FOLLOW_TOL; drawn(lat, lon): the block's heights where they are not elev's, its
    ground raised over low arches): a ribbon straight across the road stood under the ground's bumps (up to 30 cm on hills), so it was drawn
    5 cm over it and the tyres sank into it (the user, 2026-10-20). clip_poly (k, 2): a convex polygon the mesh is cut
    to instead of the box (a geotile's quad in the anchor's world: earthfast.clip_tris_convex, the neighbours' cuts
    along the very same lines); clip then only says where the ribbons reach past the tile (_reach_past)."""
    chunks = [c for c in chunks if not c.get("tube_only")]  # (twin tunnels' tube: no road surface, roads._tunnel_chunks)
    if clip is not None:
        chunks = [_reach_past(c, clip) for c in chunks]
    rb = ribbons(chunks)                        # deck surfaces too: their asphalt over deck()'s concrete
    if any(r[5] is not None for r in rb):       # (trimmed as deck() trims their slabs: context, the decks around)
        ctx = [(r, c) for r, c in ribbon_pairs(list(context)) if r[5] is not None]
        rb = [r for r, _c in trim_overlaps(ribbon_pairs(chunks), ctx)]
    polys = [c for c in chunks if c.get("poly")]
    if not rb and not polys:
        return None
    draped = [r for r in rb if r[5] is None]
    pv = [np.asarray(c["poly"], np.float64)[:, :2] for c in polys]
    if draped or pv:
        edges = np.concatenate([np.concatenate([l, r]) for l, r, _, _, _, _ in draped] + pv)
        la, lo, _ = frame.enu_to_geodetic(edges[:, 0], edges[:, 1], 0.0)
        h = np.asarray(elev(la, lo), np.float64)
        _, _, u = frame.geodetic_to_enu(la, lo, h)
    mats = []
    P, UV, T, M = [], [], [], []
    TL = []                                     # per triangle: its layer's lift over the ground (None: on a deck)
    k0 = nverts = 0
    for left, right, v, m, layer, fz in rb:
        k = len(left)
        if fz is None:
            z = u[k0:k0 + 2 * k] + LIFT[layer] + lift
            k0 += 2 * k
        else:                                   # on a deck: over its opaque concrete top
            z = np.concatenate((fz, fz)) + LIFT[layer] + DECK_SKIN
        if m not in mats:
            mats.append(m)
        mi = mats.index(m)
        P.append(np.c_[np.concatenate([left, right]), z])
        UV.append(np.column_stack((np.concatenate((np.zeros(k), np.ones(k))), np.concatenate((v, v)))))
        i = np.arange(k - 1)
        A, A1, B, B1 = nverts + i, nverts + i + 1, nverts + k + i, nverts + k + i + 1
        # left edge to the left of the direction: (A, B, B1) is counter-clockwise seen from above
        tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
        # where a ribbon folds over itself (a mitred joint at a sharp merge pulls its edges across: the user saw
        # black triangles in President St, 2026-10-14) its triangles turn over: dropped, the neighbours cover it
        xy = np.concatenate([left, right])[:, :2]
        loc = tri - nverts
        e1, e2 = xy[loc[:, 1]] - xy[loc[:, 0]], xy[loc[:, 2]] - xy[loc[:, 0]]
        cz = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
        keep = cz * np.sign(np.median(cz) or 1.0) > 1e-6
        T.append(tri[keep])
        M.append(np.full(int(keep.sum()), mi))
        TL.append(np.full(int(keep.sum()), np.nan if fz is not None else LIFT[layer] + lift))
        nverts += 2 * k
    for c in polys:                             # filled polygons (corner asphalt): a fan from vertex 0
        pq = np.asarray(c["poly"], np.float64)
        k = len(pq)
        z = u[k0:k0 + k] + LIFT[layer_of(c)] + lift
        k0 += k
        if c["m"] not in mats:
            mats.append(c["m"])
        P.append(np.c_[pq[:, :2], z])
        UV.append(np.asarray(c["uvq"], np.float64) if c.get("uvq") is not None else       # (a decal sheet's cell:
                  np.c_[pq[:, 0] / 8.0, pq[:, 1] / float(c.get("tl") or 10.0)])          # markings.arrow_quad)
        fan = np.array([(nverts, nverts + a, nverts + a + 1) for a in range(1, k - 1)])
        pts = np.c_[pq[:, :2], z]
        nrm = np.cross(pts[fan[:, 1] - nverts] - pts[fan[:, 0] - nverts], pts[fan[:, 2] - nverts] - pts[fan[:, 0] - nverts])
        flip = nrm[:, 2] < 0
        fan[flip] = fan[flip][:, ::-1]
        T.append(fan)
        M.append(np.full(len(fan), mats.index(c["m"])))
        TL.append(np.full(len(fan), LIFT[layer_of(c)] + lift))
        nverts += k
    P, T, UV, M = np.concatenate(P), np.concatenate(T), np.concatenate(UV), np.concatenate(M)
    TL = np.concatenate(TL)
    tid = np.arange(len(T))
    if clip_poly is not None:
        import fast
        fast.require(104, "clip_tris_convex")
        P, UV, T, src = fast._ef.clip_tris_convex(np.ascontiguousarray(P[:, :3], np.float64),
                                                  np.ascontiguousarray(UV, np.float64),
                                                  np.ascontiguousarray(T, np.int64),
                                                  np.ascontiguousarray(np.asarray(clip_poly, np.float64)[:, :2]))
        tid = tid[src]
        if not len(T):
            return None
    elif clip is not None:
        P, T, UV, tid = _clip_tris(P, T, UV, tid, clip)
        if not len(T):
            return None
    M, TL = M[tid], TL[tid]
    if follow:
        dr = np.isfinite(TL)
        if dr.any():
            grid, i0, j0 = _height_grid(frame, drawn or elev, P[np.unique(T[dr])][:, :2], q)
            fP, fUV, fT, src = follow_ground(P[:, :2], UV, T[dr], grid, i0, j0)
            if len(fT):
                lifts = TL[dr][src]
                vl = np.zeros(len(fP))              # (a cut piece's points are its fan's: one lift each, set once)
                for kk in range(3):
                    vl[fT[:, kk]] = lifts
                fP[:, 2] += vl
                n0 = len(P)
                P = np.concatenate([P, fP])
                UV = np.concatenate([UV, fUV])
                T = np.concatenate([T[~dr], fT + n0])
                M = np.concatenate([M[~dr], M[dr][src]])
            else:
                T, M = T[~dr], M[~dr]
            if not len(T):
                return None
        P, UV, T = _shared(P, UV, T)
    return P - np.asarray(origin, np.float64), T, UV, mats, M


DECK_DEPTH = 1.2        # m of deck slab under the surface
DECK_SKIN = 0.01        # m the road mesh lies over a deck's top (the decal materials are translucent:
                        # 2026-10-11 a deck topped with road_asphalt_2lane was see-through)
DECK_TOP = "bne_bridge_asphalt"     # the slab's top: plain opaque asphalt (the stock terrain's), under the road's own
DECK_TOP_TILE_M = 4.0               # m of asphalt a texture tile covers across ...
DECK_TOP_V = 10.0 / DECK_TOP_TILE_M  # ... and along (v: the road's, 10 m a tile)
DECK_SIDE = "bne_bridge_concrete"  # the slab, girders, parapets, piers and tunnel tubes: plain concrete (the stock
                        # concrete_plain textures; West Coast USA's "sidewalk1" had drain covers in it, round marks
                        # along every parapet next to Mapillary's Queensboro Bridge, 2026-10-17)
# A bridge as the official maps build theirs (Italy's road bridges on tapered wall piers, East Coast's): girders under
# the slab, a parapet along each free edge, piers (roads.pier_sites); ours was a bare floating slab (the user,
# 2026-10-17: "Bridges need a lot of work in general")
GIRDER_D = 1.0          # m of girders under the slab
GIRDER_W = 0.6          # m wide each
GIRDER_EVERY = 3.0      # m between girders across the deck
PARAPET_W = 0.45        # m: the slab reaches this far past the road's edge, a parapet on it ...
PARAPET_H = 0.84        # ... this high over the road (a jersey barrier's: JERSEY_H)
PARAPET_COVER_DZ = 2.0  # m: another deck's surface within this height at an edge point: no parapet there
BRIDGE_RAILS = True     # stock rail modules on decks with mapped support; unsupported decks keep full parapets
BRIDGE_CURB_H = 0.3     # m curb under generated guardrails, only on decks supported by mapped data
PARAPET_MIN_M = 8.0     # m: a parapet run shorter than this is left out (stubs stood in the Queensboro Bridge's lanes
                        # where its two directions' decks meet and the cover test missed a few points, 2026-10-17)
MEDIAN_JOIN_M = 6.0     # m: two decks' edges closer than this (a divided road's carriageways on one bridge) face a
                        # median: no parapets there, each slab widened to meet the other halfway (two parapets and a
                        # dark gap stood in a divided road's median over a canal, Port St. Lucie, 2026-10-23)
FOOTING_M = 2.0         # m a pier reaches into the ground
PIER_T = 1.8            # m: a wall pier's thickness along the deck (at its top; FOOTING tapers wider)
PIER_TAPER = 0.012      # its width grows this much per m down (each side)
PIER_CAP_H = 1.2        # m of pier cap under the girders, as wide as the deck
PIER_BENT_W = 16.0      # a deck wider than this stands on columns (a bent) rather than one wall
PIER_COLUMN = 1.6       # m square columns of a bent


# The parapet (2026-10-27): a closed New Jersey barrier, not three open faces (its ends and underside were missing: the
# far faces were culled where it ended, the top read as a lip floating over the water, the Sunshine Skyway's north
# end, the user's screenshots). A precast barrier's dimensions (the user's drawing): 610 mm base, 840 mm high; a 130 mm
# vertical toe, a lower slope to 380 mm (125 mm in), a steep upper face to the top (105 mm in), a 150 mm top. On a deck
# edge the half section: the outer face vertical, the road face the jersey's. (m in from the slab's outer edge, m up).
JERSEY_H = 0.84
JERSEY_TOE = 0.13
JERSEY_BREAK = 0.38
JERSEY_TOP_W = 0.15
JERSEY_UPPER_IN = 0.105
JERSEY_LOWER_IN = 0.125
JERSEY_HALF_W = JERSEY_TOP_W + JERSEY_UPPER_IN + JERSEY_LOWER_IN       # 0.38 m
JERSEY = ((0.0, 0.0), (0.0, JERSEY_H), (JERSEY_TOP_W, JERSEY_H), (JERSEY_TOP_W + JERSEY_UPPER_IN, JERSEY_BREAK),
          (JERSEY_HALF_W, JERSEY_TOE), (JERSEY_HALF_W, 0.0))
# Its ends (a precast sloped end section, Jensen Precast's; the user's photos): over END_M the top falls from full
# height to END_H, the section keeping its shape, and ends in a square low nose. At an abutment and wherever a run of
# parapet stops (a gap, a ramp's gore); not at a tile seam, where it goes on in the next tile.
END_M = 4.0
END_H = 0.18
PARAPET_TAPER_M, PARAPET_END_H = END_M, END_H       # (names of pass 2)


def jersey_section(h):
    """The half jersey section [(m in, m up)] of a barrier h high: full (JERSEY) at JERSEY_H; lower, the toe kept and
    the faces above it squeezed into what is left, so the shape narrows into a low plinth at an end. Pure."""
    toe = min(JERSEY_TOE, h)
    k = (h - toe) / (JERSEY_H - JERSEY_TOE)
    return [(x, y if y <= JERSEY_TOE else toe + (y - JERSEY_TOE) * k) for x, y in JERSEY]


def parapet_heights(line, ends):
    """Per point of a parapet's line (k, 2): its height, JERSEY_H, falling to END_H within END_M of an end point
    (abutments, the ends of a run where it stops: [(x, y)]; none: full height everywhere). Pure."""
    h = np.full(len(line), JERSEY_H)
    if ends:
        a = np.asarray(ends, np.float64)
        d = np.hypot(line[:, None, 0] - a[None, :, 0], line[:, None, 1] - a[None, :, 1]).min(1)
        h = END_H + (JERSEY_H - END_H) * np.clip(d / END_M, 0.0, 1.0)
    return h


UV_M = 0.25             # uv per m on a barrier (the concrete's repeat: 4 m)


def barrier_solid(edge, inward, z, h, v=None, section=JERSEY):
    """A closed barrier along an outer edge (k, 2): its section (offset toward `inward` (k, 2) m, share of the height
    h (k,)) swept from the base heights z (k,), both ends capped: a watertight solid whose every face looks out (no
    culled face shows from any side). (positions (k n, 3), triangles, uv). Pure."""
    edge, inward = np.asarray(edge, np.float64), np.asarray(inward, np.float64)
    z, h = np.asarray(z, np.float64), np.asarray(h, np.float64)
    k, n = len(edge), len(section)
    inward = inward / np.maximum(np.hypot(inward[:, 0], inward[:, 1]), 1e-9)[:, None]
    if section is JERSEY:                           # (m up per station: the section narrowing toward an end)
        secs = np.array([jersey_section(float(hh)) for hh in h], np.float64)        # (k, n, 2)
        off2, up2 = secs[:, :, 0], secs[:, :, 1]
    else:                                           # (a share of h)
        off2 = np.tile([q[0] for q in section], (k, 1)).astype(np.float64)
        up2 = h[:, None] * np.array([q[1] for q in section], np.float64)[None, :]
    up = up2[0] / max(float(h[0]), 1e-9)
    xy = edge[:, None, :] + inward[:, None, :] * off2[:, :, None]
    zz = z[:, None] + up2
    R = np.concatenate([xy, zz[..., None]], axis=2)                # (k, n, 3): the section at each station
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(edge, axis=0).T))] if v is None else np.asarray(v, np.float64)
    seg = np.linalg.norm(np.diff(np.concatenate([R, R[:, :1]], axis=1), axis=1), axis=2)    # (k, n) edge lengths
    per = np.concatenate([np.zeros((k, 1)), np.cumsum(seg, axis=1)], axis=1)               # (k, n + 1)
    # each face its own vertices (flat, not one normal smoothed over the barrier's corners: its top read as a soft
    # pillow) and world-scale uv: the sides along the barrier by the section's perimeter, the ends planar (their
    # uv was one column stretched across the face)
    Ps, UVs, Ts, outs, nv_ = [], [], [], [], 0
    i = np.arange(k - 1)
    for e in range(n):
        f = (e + 1) % n
        Ps.append(np.concatenate([R[:, e], R[:, f]]))
        UVs.append(np.c_[np.r_[s, s] * UV_M, np.r_[per[:, e], per[:, e + 1]] * UV_M])
        A, A1, B, B1 = nv_ + i, nv_ + i + 1, nv_ + k + i, nv_ + k + i + 1
        Ts.append(np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)]))
        mid = (R[:-1, e] + R[1:, e] + R[:-1, f] + R[1:, f]) / 4
        cen = (R[:-1].mean(1) + R[1:].mean(1)) / 2
        outs.append(np.tile(mid - cen, (2, 1)))
        nv_ += 2 * k
    for st, away in ((0, (edge[0] - edge[1]) if k > 1 else np.zeros(2)),
                     (k - 1, (edge[-1] - edge[-2]) if k > 1 else np.zeros(2))):
        Ps.append(R[st])
        UVs.append(np.c_[off2[st] * UV_M, up2[st] * UV_M])
        fan = np.arange(1, n - 1)
        Ts.append(np.stack([np.zeros(n - 2, np.int64), fan, fan + 1], 1) + nv_)
        outs.append(np.tile(np.r_[away, 0.0], (n - 2, 1)))
        nv_ += n
    P = np.concatenate(Ps)
    UV = np.concatenate(UVs)
    T = np.concatenate(Ts).astype(np.int64)
    out = np.concatenate(outs)
    nrm = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
    flip = (nrm * out).sum(1) < 0
    T[flip] = T[flip][:, ::-1]
    return P, T, UV


def deck(chunks, origin, piers=(), context=(), beams=(), ground=(), parapets=None, parapet_h=PARAPET_H,
         low_parapet_decks=(), pads=()):
    """The bridge decks of a tile as one colliding mesh: per "deck" chunk a slab of opaque concrete
    (DECK_SIDE) whose top is at the chunk's fixed heights (the road surface, drawn DECK_SKIN above it
    by build()), with sides and underside DECK_DEPTH below; the piers, and the truss beams (p, q, width, height:
    bridgetypes.truss_members) in painted steel (TRUSS_STEEL). (positions, tris, uvs, materials, tri
    material index) or None. ground: the tile's ground carriageways (roads.build_tile cover_out) for the parapet
    test, drawn or not (with the roads painted into the ground the chunks have none). Pure."""
    dch = [c for c in chunks if c.get("deck")]
    cr_all = [(r, c) for r, c in ribbon_pairs(list(context)) if r[5] is not None]
    rb = trim_overlaps([(r, c) for r, c in ribbon_pairs(dch) if r[5] is not None], cr_all)
    if not rb and not piers and not beams and not pads:
        return None
    low_parapet_decks = set(low_parapet_decks or ())
    if parapets is None:
        parapets = parapet_runs(chunks, context, _rb=rb, ground=ground, pads=pads)
    parapets_by_deck = {}
    for parapet in parapets:
        parapets_by_deck.setdefault(parapet["deck"], []).append(parapet)
    mats, P, UV, T, M = [], [], [], [], []
    nv = 0

    def quads(a, b, ua, ub, va, vb, mi, up):
        """A strip between point rows a and b (k, 3); `up`: the side its faces look to (3,) or (k, 3)."""
        nonlocal nv
        k = len(a)
        P.append(np.concatenate([a, b]))
        UV.append(np.column_stack((np.concatenate((np.full(k, ua), np.full(k, ub))), np.concatenate((va, vb)))))
        i = np.arange(k - 1)
        A, A1, B, B1 = nv + i, nv + i + 1, nv + k + i, nv + k + i + 1
        tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
        pts = np.concatenate(P)
        nrm = np.cross(pts[tri[:, 1]] - pts[tri[:, 0]], pts[tri[:, 2]] - pts[tri[:, 0]])
        want = np.broadcast_to(np.concatenate([up[:-1], up[:-1]]) if np.ndim(up) == 2 else up, nrm.shape)
        flip = (nrm * want).sum(1) < 0
        tri[flip] = tri[flip][:, ::-1]
        T.append(tri)
        M.append(np.full(len(tri), mi))
        nv += 2 * k

    if DECK_SIDE not in mats:
        mats.append(DECK_SIDE)
    side = mats.index(DECK_SIDE)                # opaque concrete; the asphalt is the road mesh's
    if DECK_TOP not in mats:
        mats.append(DECK_TOP)
    top = mats.index(DECK_TOP)
    cr = [r for r in ribbons(list(context)) if r[5] is not None]
    cov_ctx = [(r, c) for r, c in zip(cr, context)] + _ground_carriageways(list(chunks) + list(ground))
    covered = _parapet_cover([(r, c) for r, c in rb], cov_ctx)
    medians = _median_gaps([(r, c) for r, c in rb],
                           [(r, c) for r, c in zip(ribbons(list(context)), context) if r[5] is not None])
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        k = len(z)
        lat = left - right
        u = lat / np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)[:, None]     # unit left
        wide = 0.0 if c.get("tunnel") else PARAPET_W
        gl, gr = medians[n]
        covered[n] = (covered[n][0] | (gl > 0), covered[n][1] | (gr > 0))
        # the slab's edges, past the road's (to the middle of a median beside another deck)
        el = left + u * np.maximum(wide, gl / 2 + 0.05)[:, None]
        er = right - u * np.maximum(wide, gr / 2 + 0.05)[:, None]
        L = np.c_[el, z] - origin
        R = np.c_[er, z] - origin
        # (a chunk's "dd": its bridge's slab and girders, roads.deck_depth of its span, split 1.2 : 1.0 as the full deck)
        dd = float(c.get("dd") or (DECK_DEPTH + GIRDER_D))
        slab = DECK_DEPTH * dd / (DECK_DEPTH + GIRDER_D)
        gird = dd - slab
        Lb, Rb = L - [0, 0, slab], R - [0, 0, slab]
        out_l = np.c_[u, np.zeros(k)]
        # (the top plain asphalt, under the road's own surface: where that does not cover it, between decks side by
        # side, a median, a gore, the slab showed as pale concrete wedges, US-101 over the Los Angeles River)
        across = float(np.median(np.hypot(*(el - er).T))) / DECK_TOP_TILE_M
        quads(L, R, 0.0, across, v * DECK_TOP_V, v * DECK_TOP_V, top if not c.get("tunnel") else side,
              np.array([0.0, 0.0, 1.0]))
        quads(L, Lb, 0.0, 0.1, v, v, side, out_l)
        quads(R, Rb, 0.0, 0.1, v, v, side, -out_l)
        quads(Lb, Rb, 0.0, 1.0, v, v, side, np.array([0.0, 0.0, -1.0]))
        if c.get("tunnel"):                     # a tunnel's floor: the slab alone
            continue
        # girders under the slab
        half = float(np.median(np.hypot(lat[:, 0], lat[:, 1]))) / 2
        ng = max(2, int(round(2 * half / GIRDER_EVERY)))
        for off in np.linspace(-(half - 0.6), half - 0.6, ng):
            mid = (left + right) / 2 + u * off
            gl, gr = mid + u * GIRDER_W / 2, mid - u * GIRDER_W / 2
            GL, GR = np.c_[gl, z - slab] - origin, np.c_[gr, z - slab] - origin
            GLb, GRb = GL - [0, 0, gird], GR - [0, 0, gird]
            quads(GL, GLb, 0.0, 0.1, v, v, side, out_l)
            quads(GR, GRb, 0.0, 0.1, v, v, side, -out_l)
            quads(GLb, GRb, 0.0, 0.05, v, v, side, np.array([0.0, 0.0, -1.0]))
        # a parapet on each free edge (parapet_runs: not where another deck goes on beside it, a ramp leaving, a
        # carriageway); a low curb under the rail where mapped guard rails put stock rail modules on it (guardrails)
        for parapet in parapets_by_deck.get(n, ()):
            h = parapet_heights(parapet["mid"], parapet["ends"])
            h = np.minimum(h, BRIDGE_CURB_H if BRIDGE_RAILS and n in low_parapet_decks else parapet_h)
            bp, bt_, buv = barrier_solid(parapet["edge"], -parapet["out"], parapet["z"], h, parapet["v"])
            P.append(bp - origin)
            UV.append(buv)
            T.append(bt_ + nv)
            M.append(np.full(len(bt_), side))
            nv += len(bp)
    for p in _piers_clear(piers, rb):
        _pier(p, origin, P, T, UV, M, side)
        nv = sum(len(a) for a in P)
    for pad in pads:                # (junction pads on bridges: roads.deck_pads; their rim's railings)
        ring, centre = pad[0], pad[1]
        _pad(ring, centre, origin, P, T, UV, M, top, side)
        nv = sum(len(a) for a in P)
        for pts, out in (pad[2] if len(pad) > 2 else ()):
            pts = np.asarray(pts, np.float64)
            out = np.asarray(out, np.float64)
            edge = pts[:, :2] + out * (JERSEY_HALF_W + 0.02)
            ends = [tuple(edge[0]), tuple(edge[-1])]
            h = parapet_heights(edge, ends)
            bp, bt_, buv = barrier_solid(edge, -out, pts[:, 2] - PAD_DROP, h)
            P.append(bp - origin)
            UV.append(buv)
            T.append(bt_ + nv)
            M.append(np.full(len(bt_), side))
            nv += len(bp)
    if len(beams):
        for pt in sorted({b[4] if len(b) > 4 else "grey" for b in beams}):
            mats.append(steel(pt))
            _beams([b for b in beams if (b[4] if len(b) > 4 else "grey") == pt], origin, P, T, UV, M, len(mats) - 1)
    if not P:
        return None
    return (np.concatenate(P), np.concatenate(T), np.concatenate(UV), mats, np.concatenate(M))


TRUSS_STEEL = "bne_truss_steel"     # painted steel (the stock metal_paint, grey: tunnel.materials.json) ...


def steel(paint):
    """... and in the other paints (bridgetypes.PAINTS): bne_steel_<paint>."""
    return TRUSS_STEEL if paint in (None, "grey") else f"bne_steel_{paint}"


def _beams(beams, origin, P, T, UV, M, mat):
    """Box beams [(p (3,), q (3,), width, height)] appended to P, T, UV, M (all at once: a truss has hundreds)."""
    b = list(beams)
    p = np.array([x[0] for x in b], np.float64) - np.asarray(origin, np.float64)
    q = np.array([x[1] for x in b], np.float64) - np.asarray(origin, np.float64)
    w = np.array([x[2] for x in b], np.float64)[:, None] / 2
    h = np.array([x[3] for x in b], np.float64)[:, None] / 2
    d = q - p
    ln = np.maximum(np.linalg.norm(d, axis=1), 1e-9)
    d = d / ln[:, None]
    a = np.cross(d, [0.0, 0.0, 1.0])
    vert = np.linalg.norm(a, axis=1) < 1e-3                 # a vertical post: its width along x
    a[vert] = [1.0, 0.0, 0.0]
    a /= np.linalg.norm(a, axis=1)[:, None]
    c = np.cross(a, d)
    corners = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    V = np.stack([e + a * (sa * w) + c * (sc * h) for e in (p, q) for sa, sc in corners], 1)    # (n, 8, 3)
    nv0 = sum(len(x) for x in P)
    n = len(b)
    P.append(V.reshape(-1, 3))
    u = np.r_[0, 0.25, 0.5, 0.75, 0, 0.25, 0.5, 0.75]
    UV.append(np.stack([np.broadcast_to(u, (n, 8)), np.c_[np.zeros((n, 4)), np.repeat(ln[:, None] / 4.0, 4, 1)]],
                       -1).reshape(-1, 2))
    tri = []
    for i in range(4):                          # the four sides (ends left open: they meet other beams)
        j = (i + 1) % 4
        tri += [(i, j, j + 4), (i, j + 4, i + 4)]
    tri = np.array(tri, np.int64)
    Tn = (tri[None] + (np.arange(n) * 8)[:, None, None]).reshape(-1, 3)
    pts = np.concatenate([V.reshape(-1, 3)])
    cen = np.repeat(((p + q) / 2), len(tri), 0)
    nrm = np.cross(pts[Tn[:, 1]] - pts[Tn[:, 0]], pts[Tn[:, 2]] - pts[Tn[:, 0]])
    flip = (nrm * (pts[Tn].mean(1) - cen)).sum(1) < 0
    Tn[flip] = Tn[flip][:, ::-1]
    T.append(Tn + nv0)
    M.append(np.full(len(Tn), mat))


PAD_DROP = 0.06        # m a junction pad's top lies under the roads' surfaces at its edge points (they draw over it)
PAD_SKIRT = 0.5        # m of slab edge under its rim


def _pad(ring, centre, origin, P, T, UV, M, top, side):
    """A junction pad (roads.deck_pads: ring (k, 3) counter-clockwise, centre (3,)): its top a fan from the centre in
    the decks' top material, its rim a skirt PAD_SKIRT deep, appended to P, T, UV, M (part of the colliding deck
    mesh)."""
    ring = np.asarray(ring, np.float64)
    if len(ring) < 3:
        return
    c = np.asarray(centre, np.float64)
    o = np.asarray(origin, np.float64)
    nv = sum(len(a) for a in P)
    k = len(ring)
    top_v = np.vstack([c - [0, 0, PAD_DROP], ring - [0, 0, PAD_DROP]]) - o
    P.append(top_v)
    UV.append(np.c_[top_v[:, 0] / DECK_TOP_TILE_M, top_v[:, 1] / DECK_TOP_TILE_M])
    i = np.arange(k)
    T.append(np.c_[np.zeros(k, np.int64), 1 + i, 1 + (i + 1) % k] + nv)       # (counter-clockwise from above: up)
    M.append(np.full(k, top))
    nv += k + 1
    a = ring - [0, 0, PAD_DROP]
    b = a - [0, 0, PAD_SKIRT]
    sk = np.vstack([a, b]) - o
    P.append(sk)
    UV.append(np.c_[np.r_[np.zeros(k), np.ones(k)] * 0.1, np.r_[i, i] * 0.25])
    j = (i + 1) % k
    # (outward: the ring runs counter-clockwise, so (a_i, b_j, a_j) and (a_i, b_i, b_j) face out)
    T.append(np.concatenate([np.c_[i, k + j, j], np.c_[i, k + i, k + j]]) + nv)
    M.append(np.full(2 * k, side))


def _piers_clear(piers, rb):
    """The piers that cross no other deck under their top: a pier of the Queensboro Bridge's upper roadway stood on
    the ground through the lower one, across its lanes (the user, 2026-10-17: "objects blocking the bridge road")."""
    if not piers or not rb:
        return list(piers)
    lines = [(((l + r) / 2), float(np.median(np.hypot(*(l - r).T))) / 2 + PARAPET_W, z) for (l, r, v, m, layer, z), c in rb
             if not c.get("tunnel")]
    out = []
    for p in piers:
        x, y, ux, uy, zt, zb, width = p[:7]
        q = np.array([x, y])
        reach = float(np.clip(0.7 * width, 2.5, 14.0)) / 2 + PIER_T
        hit = False
        for mid, hw, z in lines:
            d = np.hypot(*(mid - q).T)
            k = int(np.argmin(d))
            if d[k] < hw + reach and z[k] < zt - 1.0:       # a deck below the pier's top, across its footprint
                hit = True
                break
        if not hit:
            out.append(p)
    return out


GROUND_COVER_MIN_W = 3.0    # m: a ground road this wide at a deck edge's height leaves it without a parapet


def _ground_carriageways(chunks):
    """[(ribbon with the nodes' own heights, chunk)] of the tile's ground roads (no decks, tunnels, sidewalks, markings,
    gutters or driveways) for _parapet_cover: a deck at grade across a road has no parapet over its lanes (GitHub #96:
    the Pitt River Bridge's ramps, at grade over Lougheed Highway, stood their walls across its lanes)."""
    out = []
    for c in chunks:
        if c.get("deck") or c.get("tunnel") or c.get("fixed") or c.get("h") is not None or c.get("drive") \
                or float(c.get("w", 0.0)) < GROUND_COVER_MIN_W or layer_of(c) != "base":
            continue
        pts = np.asarray(c["n"], np.float64)
        if len(pts) < 2 or pts.shape[1] < 3:
            continue
        xy, sv, z = _resample(pts, DRAPE_STEP)
        if len(xy) < 2:
            continue
        n = _normals(xy) * (float(c["w"]) / 2)
        out.append(((xy + n, xy - n, sv, c["m"], "base", z), c))
    return out


def _median_gaps(rb, context=()):
    """Per deck ribbon (left, right) the gap (m) from each edge point to the nearest edge of another deck within
    MEDIAN_JOIN_M at about its height (PARAPET_COVER_DZ), else 0: a median between two carriageways' decks. rb and
    context: [(ribbon, chunk)]; the context's pieces of the same deck (its fid: the deck going on in the tiles around)
    are not another deck (they had hidden every median at 0 m)."""
    edges = []                      # (owner, points (k, 3))
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        key = c.get("fid", ("own", n))
        edges.append((key, np.c_[left, z]))
        edges.append((key, np.c_[right, z]))
    for k, ((left, right, v, m, layer, z), c) in enumerate(context):
        key = c.get("fid", ("ctx", k))
        edges.append((key, np.c_[left, z]))
        edges.append((key, np.c_[right, z]))
    out = []
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        own = c.get("fid", ("own", n))
        sides = []
        for E in (np.c_[left, z], np.c_[right, z]):
            g = np.zeros(len(E))
            for owner, O in edges:
                if owner == own or not len(O):
                    continue
                # (only near ones: a cheap box test first)
                if (O[:, 0].min() > E[:, 0].max() + MEDIAN_JOIN_M or O[:, 0].max() < E[:, 0].min() - MEDIAN_JOIN_M
                        or O[:, 1].min() > E[:, 1].max() + MEDIAN_JOIN_M
                        or O[:, 1].max() < E[:, 1].min() - MEDIAN_JOIN_M):
                    continue
                d = np.hypot(E[:, None, 0] - O[None, :, 0], E[:, None, 1] - O[None, :, 1])
                dz = np.abs(E[:, None, 2] - O[None, :, 2])
                d = np.where(dz < PARAPET_COVER_DZ, d, np.inf).min(1)
                hit = (d < MEDIAN_JOIN_M) & ((g == 0) | (d < g))
                g = np.where(hit, np.maximum(d, 1e-3), g)
            sides.append(g)
        # (an edge facing away from the other deck is near it too where the decks are narrow: only the nearer edge
        # of the two faces the median)
        gl, gr = sides
        both = (gl > 0) & (gr > 0)
        gl = np.where(both & (gl > gr), 0.0, gl)
        gr = np.where(both & (gr >= sides[0]), 0.0, gr)
        out.append((_fill_short(gl, MEDIAN_FILL_PTS), _fill_short(gr, MEDIAN_FILL_PTS)))
    return out


MEDIAN_FILL_PTS = 8     # a median's run of points missing it between two that face it (this many or fewer) faces it too


def _fill_short(g, k):
    """g (per edge point the median gap, 0 none) with each run of zeros of k points or fewer between two nonzero
    points filled by the interpolation of its ends: an edge facing a median now and then missed it a point at a time
    and the slab's edge zigzagged, black teeth of the ground under the Four Level's 110 deck (Los Angeles, 2026-10-27)."""
    g = np.asarray(g, np.float64).copy()
    nz = np.flatnonzero(g > 0)
    if len(nz) < 2:
        return g
    for a, b in zip(nz[:-1], nz[1:]):
        if 1 < b - a <= k + 1:
            g[a + 1:b] = np.interp(np.arange(a + 1, b), [a, b], [g[a], g[b]])
    return g


def _parapet_cover(rb, context=()):
    """Per deck ribbon (left, right edge points) whether another deck's surface lies there (no parapet):
    earthfast.deck_edges_covered over every ribbon's centre line, its half width + PARAPET_W; Python else."""
    import fast
    out = []
    if not rb:
        return out
    pts, own, A, B, HW, OS = [], [], [], [], [], []
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        lat = left - right
        u = lat / np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)[:, None]
        mid = (left + right) / 2
        hw = float(np.median(np.hypot(lat[:, 0], lat[:, 1]))) / 2 + PARAPET_W
        oid = hash(c.get("fid", n)) & 0x3FFFFFFF
        for e in (left + u * PARAPET_W / 2, right - u * PARAPET_W / 2):
            pts.append(np.c_[e, z])
            own.append(np.full(len(z), oid))
        A.append(np.c_[mid[:-1], z[:-1]])
        B.append(np.c_[mid[1:], z[1:]])
        HW.append(np.full(len(z) - 1, hw))
        OS.append(np.full(len(z) - 1, oid))
    for (left, right, v, m, layer, z), c in context:     # the decks around the tile: surfaces only
        lat = left - right
        mid = (left + right) / 2
        hw = float(np.median(np.hypot(lat[:, 0], lat[:, 1]))) / 2 + PARAPET_W
        oid = hash(c.get("fid", "ctx")) & 0x3FFFFFFF
        A.append(np.c_[mid[:-1], z[:-1]])
        B.append(np.c_[mid[1:], z[1:]])
        HW.append(np.full(len(z) - 1, hw))
        OS.append(np.full(len(z) - 1, oid))
    P_, O_ = np.concatenate(pts), np.concatenate(own).astype(np.int64)
    A_, B_, H_, S_ = np.concatenate(A), np.concatenate(B), np.concatenate(HW), np.concatenate(OS).astype(np.int64)
    fast.require(1, "deck_edges_covered")
    cov = np.asarray(fast._ef.deck_edges_covered(np.ascontiguousarray(P_), O_, np.ascontiguousarray(A_),
                                                  np.ascontiguousarray(B_), H_, S_, PARAPET_COVER_DZ))
    k0 = 0
    for (left, right, v, m, layer, z), c in rb:
        k = len(z)
        out.append((cov[k0:k0 + k], cov[k0 + k:k0 + 2 * k]))
        k0 += 2 * k
    return out



def parapet_runs(chunks, context=(), _rb=None, ground=(), pads=()):
    """Actual generated bridge parapets as XY centre lines plus the mesh geometry.

    Uses the same coverage masks, edge offsets, tunnel exclusion, and minimum-run rule as deck().
    _rb lets deck() share its already-computed ribbons. Returned dictionaries are ordered by deck
    chunk and edge, and include the points deck() uses verbatim.
    """
    if _rb is None:
        dch = [c for c in chunks if c.get("deck")]
        cr_all = [(r, c) for r, c in ribbon_pairs(list(context)) if r[5] is not None]
        rb = trim_overlaps([(r, c) for r, c in ribbon_pairs(dch) if r[5] is not None], cr_all)
    else:
        rb = _rb
    if not rb:
        return []

    cr = [r for r in ribbons(list(context)) if r[5] is not None]
    cov_ctx = [(r, c) for r, c in zip(cr, context)] + _ground_carriageways(list(chunks) + list(ground))
    covered = _parapet_cover([(r, c) for r, c in rb], cov_ctx)
    medians = _median_gaps([(r, c) for r, c in rb],
                           [(r, c) for r, c in zip(ribbons(list(context)), context) if r[5] is not None])
    out = []
    for n, ((left, right, v, _m, _layer, z), c) in enumerate(rb):
        if c.get("tunnel"):       # deck() draws the tunnel floor without parapets
            continue
        lat = left - right
        u = lat / np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)[:, None]
        wide = 0.0 if c.get("tunnel") else PARAPET_W
        gl, gr = medians[n]
        covered[n] = (covered[n][0] | (gl > 0), covered[n][1] | (gr > 0))
        if len(pads):               # (none inside a junction pad: roads.deck_pads)
            import roads as _roads
            covered[n] = (covered[n][0] | _roads.pad_inside(pads, left), covered[n][1] | _roads.pad_inside(pads, right))
        el = left + u * np.maximum(wide, gl / 2 + 0.05)[:, None]
        er = right - u * np.maximum(wide, gr / 2 + 0.05)[:, None]
        k = len(z)
        for sgn, edge, inner, cov in ((1, el, left, covered[n][0]),
                                      (-1, er, right, covered[n][1])):
            i = 0
            while i < k:
                if cov[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < k and not cov[j + 1]:
                    j += 1
                run_len = float(np.hypot(*np.diff(edge[i:j + 1], axis=0).T).sum()) if j > i else 0.0
                if j > i and (run_len >= PARAPET_MIN_M or i == 0 or j == k - 1):
                    sl = slice(i, j + 1)
                    edge_run, inner_run = edge[sl], inner[sl]
                    mid_ = (left + right) / 2
                    ends = list(c.get("abut") or [])
                    if i > 0:                                       # the run stops there (a gap, a gore)
                        ends.append(tuple(mid_[i]))
                    if j < k - 1:
                        ends.append(tuple(mid_[j]))
                    out.append({
                        "mid": mid_[sl],
                        "ends": ends,
                        "deck": n,
                        "line": (edge_run + inner_run) * 0.5,
                        "edge": edge_run,
                        "inner": inner_run,
                        "z": z[sl],
                        "v": v[sl],
                        "out": u[sl] * sgn,
                    })
                i = j + 1
    return out


def _pier(p, origin, P, T, UV, M, mat):
    """One pier (roads.pier_sites) appended to P, T, UV, M: a tapered wall with a cap under the girders, or for a deck
    wider than PIER_BENT_W a bent: square columns under a cap beam."""
    x, y, ux, uy, zt, zb, width = p[:7]
    thick = p[7] if len(p) > 7 else None        # a truss's anchor pier: one solid wall this thick, the truss's width
    t = np.array([ux, uy])
    nrm = np.array([-uy, ux])
    c = np.array([x, y])
    boxes = []                                  # (centre, half along t, half across, z0, z1, widen per m down)
    cap_w = width / 2 + PARAPET_W
    if thick is not None:
        boxes.append((c, thick / 2, width / 2, zb, zt, PIER_TAPER))
    else:
        boxes.append((c, PIER_T / 2 + 0.3, cap_w, zt - PIER_CAP_H, zt, 0.0))
    if thick is not None:
        pass
    elif width > PIER_BENT_W:
        nc = max(2, int(round(width / 9.0)))
        for off in np.linspace(-(width / 2 - 2.0), width / 2 - 2.0, nc):
            boxes.append((c + nrm * off, PIER_COLUMN / 2, PIER_COLUMN / 2, zb, zt - PIER_CAP_H, 0.0))
    else:
        wall = float(np.clip(0.7 * width, 2.5, 14.0)) / 2
        boxes.append((c, PIER_T / 2, wall, zb, zt - PIER_CAP_H, PIER_TAPER))
    for cc, ht, hn, z0, z1, taper in boxes:
        grow = taper * max(z1 - z0, 0.0)
        top = [cc + t * a * ht + nrm * b * hn for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        bot = [cc + t * a * (ht + grow * 0.5) + nrm * b * (hn + grow) for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        nv = sum(len(a) for a in P)
        V = np.array([[*q, z1] for q in top] + [[*q, z0] for q in bot]) - np.asarray(origin, np.float64)
        P.append(V)
        UV.append(np.c_[np.r_[0, 1, 0, 1, 0, 1, 0, 1] * 0.25, np.r_[0, 0, 0, 0, 1, 1, 1, 1] * (z1 - z0) / 8.0])
        tri = []
        for i in range(4):                      # sides (outward: the corners go counter-clockwise from above)
            a, b = i, (i + 1) % 4
            tri += [(a, b + 4, b), (a, a + 4, b + 4)]
        tri += [(0, 1, 2), (0, 2, 3)]           # top
        tri = np.array(tri, np.int64)
        # outward winding: check one side's normal against the outward direction, flip all if wrong
        Vn = V[tri]
        nrm_ = np.cross(Vn[:, 1] - Vn[:, 0], Vn[:, 2] - Vn[:, 0])
        cen = V.mean(0)
        flip = (nrm_ * (Vn.mean(1) - cen)).sum(1) < 0
        tri[flip] = tri[flip][:, ::-1]
        T.append(tri + nv)
        M.append(np.full(len(tri), mat))


TUNNEL_SHOULDER = 1.2      # m of concrete floor between the road's edge and the wall
TUNNEL_WALL_H = 4.6        # m of upright wall before the vault
TUNNEL_H = 6.8             # m from the floor to the vault's top
PORTAL_FRAME = 6.0         # m of portal face around the opening (hides the terrain hole's 2 m steps)
TUNNEL_SIDE = DECK_SIDE    # the tube's concrete (portal faces) ...
TUNNEL_INNER = "bne_tunnel_inner"   # ... and inside a full tube darker (tunnel.materials.json): lit by the sky as the
                            # engine lights it, the Caldecott's tube was as bright as the day outside (its photos:
                            # dark, lit by its lamps, 2026-10-18)
TUNNEL_STONE = "bne_tunnel_stone"  # a low arch's portal faces and the retaining walls: grey coursed rubble (Central
                                    # Park's transverse walls and arches; the buildings' pale dressed stone read as
                                    # concrete, 2026-10-17)
TUNNEL_STONE_IN = "bne_tunnel_stone_in"   # the same stone darker: a low arch's vault (in its shade)
TUNNEL_SOIL = "bne_tunnel_soil"     # grass on the ground over the masonry: a retaining wall's top behind its coping,
                                    # a low arch's top (stone slabs on the grass, the user 2026-10-17: "blend the
                                    # stone and terrain better")
SOIL_UV_M = 1.0            # m per repeat of the grass (the terrain's detail texture: bngEarthGroundGrass, size 1)
WALL_COPING_M = 0.5        # m of a retaining wall's top in stone (its coping), the rest of it soil
WALL_SKIRT_M = 2.0         # m a retaining wall's back reaches down from its top
STONE_UV_M = 3.0           # m per repeat of the stone on a low arch
CONCRETE_UV_M = 4.0        # m per repeat of the concrete (as a tunnel's)
LOW_PORTAL_UP = 1.4        # m a low arch's portal face reaches over the vault (to the ground over it) ...
LOW_PORTAL_SIDE = 3.0      # ... and beside the opening
LOW_RISE = 0.4             # a low arch's vault rises this share of its height from its walls (a segmental arch)
ARCH_CAP_OVER = 0.75       # m a low arch's top lies over its vault (roads.ARCH_SOIL_MIN of soil there, + a little)
LOW_CAP_MIN = 1.0          # m: a stone top shorter than this (a road just behind the face) is left out
LOW_CAP_M = 7.0            # m a low arch's stone top runs back from its portal face over the mouth: the terrain there
                           # dips to the floor and is cut away (roads.arch_raise, LOW_HOLE_M + LOW_HOLE_STEP): from
                           # above a row of dark terrain teeth behind the face (79th Street Transverse, 2026-10-17)
TUNNEL_OVERLAP = 0.25      # m a tube piece reaches past a tile edge
TUNNEL_LIGHT = "bne_tunnel_light"   # the lamps' emissive material (levels/beamng_earth/art/shapes/beamngEarth)
TUNNEL_LAMP_EVERY = 6.0    # m between the lamp fixtures along each wall ...
TUNNEL_LAMP_L = 1.2        # ... each this long, ...
TUNNEL_LAMP_W = 0.3        # ... this tall, ...
TUNNEL_LAMP_DROP = 0.25    # ... its middle this far under the wall's top, ...
TUNNEL_LAMP_IN = 0.08      # ... this far in from the wall


def _tunnel_section(hw, th=TUNNEL_H, low=False):
    """The tube's cross-section from the left road edge's floor over the vault to the right one: (lateral offset
    from the centre line, positive = left; height over the floor) for half-width hw (centre to wall) and a vault
    th over the floor (its walls in proportion). low: a park arch's segmental vault, a shallow circular arc rising
    LOW_RISE of th from its walls (Central Park's transverse arches)."""
    if low:
        rise = LOW_RISE * th
        wall = th - rise
        r = (hw * hw + rise * rise) / (2 * rise)
        a0 = math.asin(min(1.0, hw / r))
        vault = [(r * math.sin(a), th - r + r * math.cos(a)) for a in np.linspace(a0, -a0, 9)]
    else:
        wall = TUNNEL_WALL_H * th / TUNNEL_H
        vault = [(np.cos(a) * hw, wall + np.sin(a) * (th - wall))
                 for a in np.linspace(0.0, np.pi, 9)]
    return np.array([(hw - TUNNEL_SHOULDER, 0.0), (hw, 0.0)] + vault + [(-hw, 0.0), (-hw + TUNNEL_SHOULDER, 0.0)],
                    np.float64)


WALL_THICK_M = 2.4         # a retaining wall's top: over the grading's flat strip behind it (grading.WALL_FLAT_M)


def _retaining_walls(walls, origin, P, T, UV, M, nv):
    """roads.retaining_walls' walls as stone (material index 2): the face toward the road, the top, the ends;
    appended to P, T, UV, M. Returns the new vertex count."""
    for w in walls:
        b = np.asarray(w["base"], np.float64)
        top, foot, tw = np.asarray(w["top"]), np.asarray(w["foot"]), np.asarray(w["toward"], np.float64)
        k = len(b)
        if k < 2:
            continue
        s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(b, axis=0).T))))
        thick = np.asarray(w.get("thick", np.full(k, WALL_THICK_M)), np.float64)    # (roads.retaining_walls)
        face_m, face_uv = (0, CONCRETE_UV_M) if w.get("concrete") else (2, STONE_UV_M)  # (a major road's: concrete)
        back = b - tw * thick[:, None]
        face_lo = np.c_[b, foot] - origin
        face_hi = np.c_[b, top] - origin
        back_hi = np.c_[back, top] - origin
        cop = np.minimum(WALL_COPING_M, thick)
        cop_hi = np.c_[b - tw * cop[:, None], top] - origin
        quads = [(face_lo, face_hi, foot, top, face_m), (face_hi, cop_hi, None, float(np.mean(cop)), face_m)]
        if (thick > cop + 0.01).any():          # the soil behind the coping (a parapet is all stone)
            quads.append((cop_hi, back_hi, None, float(np.mean(thick - cop)), 4))
        if w.get("parapet"):                    # a parapet's back, seen from the valley
            bot = foot
        else:                                   # a retaining wall's back: down into the ground behind it (where the
            bot = top - WALL_SKIRT_M            # ground there lies lower, its edge was open, seen from above)
        back_lo = np.c_[back, bot] - origin
        quads.append((back_lo, back_hi, bot, top, face_m))
        # its underside, from the back's bottom to the face's foot: a closed solid (2026-10-27: no face of it open,
        # its ends one-sided caps of its section; every edge shared by two faces, test_shortdecks.WallSolidTest)
        quads.append((back_lo, face_lo, None, float(np.mean(thick)), face_m))
        for qi, (lo_, hi_, vlo, vhi, mat) in enumerate(quads):
            P.append(np.concatenate([lo_, hi_]))
            if vlo is not None:
                UV.append(np.column_stack((np.concatenate((s, s)) / face_uv, np.concatenate((vlo, vhi)) / face_uv)))
            else:
                sc = SOIL_UV_M if mat == 4 else face_uv
                UV.append(np.column_stack((np.concatenate((s, s)) / sc, np.concatenate((np.zeros(k), np.full(k, vhi))) / sc)))
            i = np.arange(k - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + k + i, nv + k + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            pts = np.concatenate([lo_, hi_])
            nrm = np.cross(pts[tri[:, 1] - nv] - pts[tri[:, 0] - nv], pts[tri[:, 2] - nv] - pts[tri[:, 0] - nv])
            # the face looks toward the road, the top up, the back away from the road, the underside down
            if vlo is not None:
                sgn = -1.0 if qi == len(quads) - 2 else 1.0
                look = np.c_[sgn * np.concatenate([tw[i], tw[i]]), np.zeros(2 * (k - 1))]
            else:
                look = np.tile([0.0, 0.0, -1.0 if qi == len(quads) - 1 else 1.0], (2 * (k - 1), 1))
            flip = (nrm * look).sum(1) < 0
            tri[flip] = tri[flip][:, ::-1]
            T.append(tri)
            M.append(mat)
            nv += 2 * k
        for at, nb in ((0, 1), (k - 1, k - 2)):     # the ends: the section (face foot, face top, coping, back top,
            # back bottom) as one-sided caps facing out along the wall (they were two-sided quads foot to top, open to
            # the coping and the skirt), world-scale planar uv
            q = np.array([face_lo[at], face_hi[at], cop_hi[at], back_hi[at], back_lo[at]], np.float64)
            out = np.r_[b[at] - b[nb], 0.0]
            out /= max(float(np.hypot(out[0], out[1])), 1e-9)
            across = np.r_[-tw[at], 0.0]
            P.append(q)
            UV.append(np.column_stack(((q @ across) / face_uv, q[:, 2] / face_uv)))
            tri = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 4]], np.int64)
            e1, e2 = q[tri[:, 1]] - q[tri[:, 0]], q[tri[:, 2]] - q[tri[:, 0]]
            nrm = np.cross(e1, e2)
            keep = np.hypot(*nrm.T) > 1e-9              # (the coping at the back: no soil, a corner twice)
            tri = tri[keep]
            flip = (nrm[keep] @ out) < 0
            tri[flip] = tri[flip][:, ::-1]
            T.append(tri + nv)
            M.append(face_m)
            nv += 5
    return nv


def tunnel(chunks, origin, walls=()):
    """The tunnels of a tile as one colliding mesh: around every "tunnel" chunk (its road surface: a deck, deck()
    makes its slab) a tube of concrete (TUNNEL_SIDE) faced inwards: shoulders, walls and a vault TUNNEL_H over the
    floor; at a chunk end that is a portal ("portal") a face around the opening, seen from both sides. (positions,
    tris, uvs, materials, tri material index) or None. Pure."""
    tc = [c for c in chunks if c.get("tunnel") and not c.get("no_tube")]     # (twins: one tube_only round both)
    rb = [(r, c) for r, c in zip(ribbons(tc), tc) if r[5] is not None] if tc else []
    if not rb and not walls:
        return None
    P, T, UV, M = [], [], [], []           # M: per T entry its material index (0 concrete, 1 lamps, 2 stone, 3 dark stone,
                                            # 4 soil)
    nv = _retaining_walls(walls, np.asarray(origin, np.float64), P, T, UV, M, 0) if walls else 0
    for (left, right, v, _, _, z), c in rb:
        # a piece ending at a tile edge reaches TUNNEL_OVERLAP past it: the next tile's piece meets it there without a
        # hairline of daylight (2026-10-17, inside Eisenhower)
        portal = c.get("portal") or (False, False)
        left, right, z = left.copy(), right.copy(), z.copy()
        for at, nb in ((0, 1), (-1, -2)):
            if portal[0 if at == 0 else 1] or len(left) < 2:
                continue
            for arr in (left, right):
                t = arr[at] - arr[nb]
                arr[at] += t / max(float(np.hypot(*t)), 1e-9) * TUNNEL_OVERLAP
        mid = (left + right) / 2
        lat = left - right
        lw = np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)
        u = lat / lw[:, None]                               # unit left
        hw = float(np.median(lw)) / 2 + TUNNEL_SHOULDER
        th = float(c.get("tunnel_h") or TUNNEL_H)
        low = th < TUNNEL_H - 0.05                          # a low arch: stone, a short portal face
        # (a major road's low arch in concrete, as its cutting's walls: the Gotthard road tunnel's portal, a cut-and-cover
        # box under the meadow at Goeschenen, stood as a park's stone arch, 2026-10-28)
        stone = low and not c.get("concrete")
        mat = 3 if stone else 5                               # (its portal faces: 2, the stone in daylight; a full
                                                            # tube's inside: 5, TUNNEL_INNER)
        sec = _tunnel_section(hw, th, low)
        k, m = len(mid), len(sec)
        rows = np.empty((m, k, 3))
        for j, (off, h) in enumerate(sec):
            rows[j, :, :2] = mid + u * off
            rows[j, :, 2] = z + h
        rows -= origin
        # inward: towards the tube's axis (a point th / 2 over the floor's centre)
        axis = np.c_[mid, z + th / 2] - origin
        for j in range(m - 1):
            a, b = rows[j], rows[j + 1]
            P.append(np.concatenate([a, b]))
            if stone:                                       # the stone at its scale: around the section, along
                sj = float(np.hypot(*(sec[min(j + 1, m - 1)] - sec[j])))
                s0 = float(np.sum(np.hypot(*np.diff(sec[:j + 1], axis=0).T))) if j else 0.0
                along = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(mid, axis=0).T))))
                UV.append(np.column_stack((np.concatenate((np.full(k, s0), np.full(k, s0 + sj))) / STONE_UV_M, np.concatenate((along, along)) / STONE_UV_M)))
            else:
                UV.append(np.column_stack((np.concatenate((np.full(k, j / 4.0), np.full(k, (j + 1) / 4.0))), np.concatenate((v, v)))))
            i = np.arange(k - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + k + i, nv + k + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            pts = np.concatenate(P)
            nrm = np.cross(pts[tri[:, 1]] - pts[tri[:, 0]], pts[tri[:, 2]] - pts[tri[:, 0]])
            cen = (pts[tri[:, 0]] + pts[tri[:, 1]] + pts[tri[:, 2]]) / 3
            ax = np.concatenate([axis[i], axis[i]])
            flip = (nrm * (ax - cen)).sum(1) < 0
            tri[flip] = tri[flip][:, ::-1]
            # a low arch is seen from outside too (where its thin cover is cut away: its stone, not a hole)
            T.append(np.concatenate([tri, tri[:, ::-1]]) if low and j >= 2 and j < m - 3 else tri)
            M.append(mat)
            nv += 2 * k
        # the lamps: under the vault along both lines, every other step, faced down (a long tunnel's; not under an arch)
        if not low:
            nv = _tunnel_lamps(P, T, UV, M, nv, mid, u, z, hw, origin)
        pups = c.get("portal_up") or (None, None)
        caps = c.get("cap_m") or (None, None)       # (a road over the mouth: its stone top shorter, TunnelProfiles.caps)
        for end, at, pup, cap_m in zip(c.get("portal") or (False, False), (0, k - 1), pups, caps):
            if not end:
                continue
            # the face: from the opening's outline out to a frame PORTAL_FRAME wider and higher, both sides
            inner = rows[:, at]
            o = sec.copy()
            up, side = (LOW_PORTAL_UP, LOW_PORTAL_SIDE) if low else (PORTAL_FRAME, PORTAL_FRAME)
            if pup is not None:     # to the ground over the portal (TunnelProfiles.portal_ups); a low arch's at least
                # LOW_PORTAL_UP (under a hill higher than its top, the ground dipping into its mouth showed over it)
                up = max(LOW_PORTAL_UP, float(pup)) if low else float(pup)
            o[:, 0] = np.sign(o[:, 0]) * (np.abs(o[:, 0]) + side)
            # its foot under the ground (2 m into a hillside; a low arch's mouth is flat: just under its floor, or the
            # face's bottom stood as a dark wedge beside the opening)
            if low:     # a flat top over the arch (a parapet), not following its curve
                o[:, 1] = np.where(o[:, 1] > 0.0, th + up, -0.3)
            else:
                o[:, 1] = np.where(o[:, 1] > 0.0, o[:, 1] + up, -2.0)
            outer = np.empty((m, 3))
            outer[:, :2] = mid[at] + u[at] * o[:, :1]
            outer[:, 2] = z[at] + o[:, 1]
            outer -= origin
            P.append(np.concatenate([inner, outer]))
            sc = STONE_UV_M if stone else 4.0
            UV.append(np.column_stack((np.concatenate((sec[:, 0], o[:, 0])) / sc, np.concatenate((sec[:, 1], o[:, 1])) / sc)))
            i = np.arange(m - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + m + i, nv + m + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            T.append(np.concatenate([tri, tri[:, ::-1]]))   # seen from outside and from in the tube
            M.append(2 if stone else 0)
            nv += 2 * m
            if low and (cap_m is None or cap_m >= LOW_CAP_MIN):
                nv = _arch_cap(P, T, UV, M, nv, mid, u, z, at, k, float(np.max(o[:, 0])), float(o[:, 1].max()), th, origin,
                               LOW_CAP_M if cap_m is None else min(LOW_CAP_M, float(cap_m)), 2 if stone else 0)
    allm = [TUNNEL_SIDE, TUNNEL_LIGHT, TUNNEL_STONE, TUNNEL_STONE_IN, TUNNEL_SOIL, TUNNEL_INNER]
    Tall = np.concatenate(T)
    Mall = np.concatenate([np.full(len(t), m, np.int64) for t, m in zip(T, M)]) if M else np.zeros(len(Tall), np.int64)
    used = sorted(set(int(v) for v in np.unique(Mall)) | {0, 1})     # (concrete and lamps first, as before)
    remap = np.zeros(len(allm), np.int64)
    remap[used] = np.arange(len(used))
    return (np.concatenate(P), Tall, np.concatenate(UV), [allm[i] for i in used], remap[Mall])


def _arch_cap(P, T, UV, M, nv, mid, u, z, at, k, side, top, th, origin, depth=None, side_mat=2):
    """A low arch's stone top over its mouth (LOW_CAP_M back from the portal face at row `at`, side either way of
    the centre, `top` over the floor): its top, its sides down to the floor's level (outside the tube's walls) and
    its back down to just over the vault (th), seen from both sides. Appended to P, T, UV, M; returns the new vertex count."""
    nb = 1 if at == 0 else k - 2
    t = mid[nb] - mid[at]
    t = t / max(float(np.hypot(*t)), 1e-9)              # into the tube
    a = mid[at]
    b = a + t * (LOW_CAP_M if depth is None else depth)
    ua = u[at]
    z0 = float(z[at]) + top - (LOW_PORTAL_UP - ARCH_CAP_OVER)   # the soil's level (the face stands over it)
    corners = np.array([[*(a + ua * side), z0], [*(a - ua * side), z0], [*(b - ua * side), z0], [*(b + ua * side), z0]])
    foot = corners.copy()
    foot[:, 2] = float(z[at])
    over = corners.copy()
    over[:, 2] = float(z[at]) + th + 0.1
    quads = [corners,                                   # the top
             np.array([corners[3], corners[2], over[2], over[3]]),     # its back, toward the soil over the vault
             np.array([corners[0], corners[3], foot[3], foot[0]]),     # its sides
             np.array([corners[1], corners[2], foot[2], foot[1]])]
    for qi, q in enumerate(quads):
        P.append(q - origin)
        w = float(np.hypot(*(q[1, :2] - q[0, :2]))) or float(abs(q[1, 2] - q[0, 2]))
        h = float(np.hypot(*(q[2, :2] - q[1, :2]))) + float(abs(q[2, 2] - q[1, 2]))
        UV.append(np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float64) / (SOIL_UV_M if qi == 0 else STONE_UV_M))
        tri = np.array([[nv, nv + 1, nv + 2], [nv, nv + 2, nv + 3]], np.int64)
        T.append(np.concatenate([tri, tri[:, ::-1]]))
        M.append(4 if qi == 0 else side_mat)            # its top grass, its sides stone (concrete: side_mat 0)
        nv += 4
    return nv


def _tunnel_lamps(P, T, UV, M, nv, mid, u, z, hw, origin):
    """Lamp fixtures along both walls of a tube (material TUNNEL_LIGHT, index 1): TUNNEL_LAMP_L long every
    TUNNEL_LAMP_EVERY m, just under the springing, faced into the tube; appended to P, T, UV, M; returns the new vertex
    count. (Two dashed strips under the vault read as lane markings on the ceiling: the Caldecott Tunnel on Mapillary
    has fixtures along its walls, 2026-10-18.) Pure but for the lists."""
    k = len(mid)
    if k < 2:
        return nv
    s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(mid, axis=0).T))))
    h = TUNNEL_WALL_H - TUNNEL_LAMP_DROP
    for sc in np.arange(TUNNEL_LAMP_EVERY / 2, s[-1] - TUNNEL_LAMP_L / 2, TUNNEL_LAMP_EVERY):
        j = min(int(np.searchsorted(s, sc)), k - 1)
        jj = max(j - 1, 0)
        t = mid[j] - mid[jj]
        t = t / max(float(np.hypot(*t)), 1e-9)
        p = np.array([np.interp(sc, s, mid[:, 0]), np.interp(sc, s, mid[:, 1])])
        zc = float(np.interp(sc, s, z))
        uu = u[j]
        for side in (-1.0, 1.0):
            e = p + uu * side * (hw - TUNNEL_LAMP_IN)
            quad = [[*(e + t * a * TUNNEL_LAMP_L / 2), zc + h + b * TUNNEL_LAMP_W / 2] for a in (-1, 1) for b in (-1, 1)]
            q = np.asarray(quad, np.float64) - origin
            P.append(q)
            UV.append(np.array([[0, 0], [0, 1], [1, 0], [1, 1]], np.float64))
            tri = np.array([[nv, nv + 2, nv + 3], [nv, nv + 3, nv + 1]], np.int64)
            nrm = np.cross(q[2] - q[0], q[1] - q[0])
            inward = -side * np.r_[uu, 0.0]
            if float(nrm @ inward) < 0:     # faced into the tube
                tri = tri[:, ::-1]
            T.append(tri)
            M.append(1)
            nv += 4
    return nv


def write(path, mesh):
    P, T, UV, mats, M = mesh
    return cdae.write(path, P, T, uvs=UV, material=mats, tri_materials=M, object_name="road")
