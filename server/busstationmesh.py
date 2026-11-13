"""The mesh of bus stations (busstations.py's Station): raised platforms with a kerb, a walking zone and open roofs on
columns, the bays' painted outlines and numbers, in the road mesh's form: (positions relative to origin (k, 3), tris, uvs, [material], tri material index), merged
into a tile's road mesh by plazas.merge (blockworker._with_stations). A tile takes the triangles whose centre lies in
its box, so every tile that sees a station makes the same one and the pieces join. Render only: the ground under it
collides (as plazas), so a car drives through the platform's 18 cm and the columns (a collision mesh is a later step).

Winding: counter-clockwise seen from outside (the top of a prism faces +z), as plazas' and the road mesh's.
"""
import math
import os

import numpy as np
import dnp

import busbays
import busstations as bs

PLATFORM_H_M = 0.18         # a platform's height over the yard (a 15-18 cm kerb)
PLATFORM_HALF_W_M = 1.6     # a platform mapped as a line is 3.2 m wide, unless it has width=
COLUMN_M = 0.28             # a column's side
COLUMN_STEP_M = 8.0         # along a roof's edge
COLUMN_GRID_M = 12.0        # inside a roof wider than this
ROOF_THICK_M = 0.30
REPEAT_M = 3.0              # m per texture repeat of the concrete and steel

PLATFORM_TOP = ("bne_bridge_concrete", REPEAT_M)
KERB = ("bne_bridge_concrete", REPEAT_M)
STEEL = ("bne_truss_steel", REPEAT_M)
LIGHT = ("bne_tunnel_light", 1.0)   # the emissive material of the tunnel lamps (glows at night): the roof fixtures
FIXTURE_W_M, FIXTURE_L_M = 0.35, 1.1
FIXTURE_IN_M = 1.2          # a fixture hangs this far in from its column


class _Mesh:
    def __init__(self):
        self.mats, self.P, self.UV, self.T, self.M, self.n = [], [], [], [], [], 0

    def add(self, mat, P, UV, T):
        if mat not in self.mats:
            self.mats.append(mat)
        P = np.asarray(P, np.float64).reshape(-1, 3)
        T = np.asarray(T, np.int64).reshape(-1, 3)
        self.P.append(P)
        self.UV.append(np.asarray(UV, np.float64).reshape(-1, 2))
        self.T.append(T + self.n)
        self.M.append(np.full(len(T), self.mats.index(mat), np.int64))
        self.n += len(P)

    def result(self, box, origin, overlap=False):
        """(V, T, UV, mats, idx) of the triangles whose centre is in box (x0, y0, x1, y1), or None. overlap: those that
        touch the box at all (a geotile's slanted quad is cut exactly afterwards, blockworker._clip_mesh: a 12 m roof
        triangle whose centre lay in the neighbour's box was lost from this tile, leaving a saw-tooth of sky along the
        seam, the user 2026-11-09)."""
        if not self.P:
            return None
        P, UV = np.concatenate(self.P), np.concatenate(self.UV)
        T, M = np.concatenate(self.T), np.concatenate(self.M)
        if overlap:
            tri = P[T]
            keep = ((tri[:, :, 0].max(1) >= box[0]) & (tri[:, :, 0].min(1) < box[2]) &
                    (tri[:, :, 1].max(1) >= box[1]) & (tri[:, :, 1].min(1) < box[3]))
        else:
            c = P[T].mean(1)
            keep = (c[:, 0] >= box[0]) & (c[:, 0] < box[2]) & (c[:, 1] >= box[1]) & (c[:, 1] < box[3])
        if not keep.any():
            return None
        T, M = T[keep], M[keep]
        used = np.unique(T)
        remap = np.full(len(P), -1, np.int64)
        remap[used] = np.arange(len(used))
        mats = [self.mats[i] for i in np.unique(M)]
        idx = np.searchsorted(np.unique(M), M)
        V = P[used] - np.asarray(origin, np.float64)
        return V, remap[T], UV[used], mats, idx


def _ccw(xy):
    xy = bs._open(xy)
    return xy if _signed(xy) >= 0 else xy[::-1]


def _signed(xy):
    x, y = xy[:, 0], xy[:, 1]
    return float(dnp.dot(x, np.roll(y, -1)) - dnp.dot(np.roll(x, -1), y)) * 0.5


class DrawnGround:
    """The ground as the road surfaces lie on it (roadmesh.drawn_ground: the terrain as it is drawn, not the physics
    block's height): fn(xy (k, 2)) -> z (k,) in the frame. Paint (the bay lines and numbers) lifted 6 cm over the physics
    ground disappeared under the terrain's bumps (the user, 2026-11-06: no numbers)."""

    def __init__(self, fn):
        self.at = fn


def ground(frame, elev, xy):
    """Ground height (frame z) at xy (k, 2): elev(lat, lon) as plazas.mesh does, or a DrawnGround's."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    if isinstance(elev, DrawnGround):
        return np.asarray(elev.at(xy), np.float64).reshape(-1)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    h = np.asarray(elev(la, lo), np.float64)
    _, _, z = frame.geodetic_to_enu(la, lo, h)
    return np.asarray(z, np.float64)


def _triangulate(xy):
    import buildings
    return np.asarray(buildings.triangulate(xy), np.int64).reshape(-1, 3)


def top_face(mesh, mat, rep, xy, z, up=True):
    """A polygon's face at heights z (per point or one number): up faces +z, else -z."""
    xy = bs._open(np.asarray(xy, np.float64))
    if _signed(xy) < 0:
        xy = xy[::-1]
        z = np.asarray(z, np.float64)[::-1] if np.ndim(z) else z
    T = _triangulate(xy)
    if not len(T):
        return
    z = np.broadcast_to(np.asarray(z, np.float64), (len(xy),))
    if not up:
        T = T[:, ::-1]
    mesh.add(mat, np.column_stack((xy, z)), xy / rep, T)


def walls(mesh, mat, rep, xy, z0, z1):
    """Outward walls round a ring from heights z0 to z1 (per point or one number)."""
    xy = _ccw(xy)
    n = len(xy)
    z0 = np.broadcast_to(np.asarray(z0, np.float64), (n,))
    z1 = np.broadcast_to(np.asarray(z1, np.float64), (n,))
    run = 0.0
    for i in range(n):
        j = (i + 1) % n
        length = float(dnp.hypot(*(xy[j] - xy[i])))
        if length < 1e-3:
            continue
        P = [(*xy[i], z0[i]), (*xy[j], z0[j]), (*xy[j], z1[j]), (*xy[i], z1[i])]
        UV = [(run / rep, z0[i] / rep), ((run + length) / rep, z0[j] / rep),
              ((run + length) / rep, z1[j] / rep), (run / rep, z1[i] / rep)]
        mesh.add(mat, P, UV, [[0, 1, 2], [0, 2, 3]])
        run += length


def _segment_quads(path, half):
    """The rectangles (4, 2) round each segment of a polyline, half metres each side."""
    out = []
    path = np.asarray(path, np.float64)
    for a, b in zip(path[:-1], path[1:]):
        d = b - a
        length = float(dnp.hypot(*d))
        if length < 0.5:
            continue
        n = np.array([-d[1], d[0]]) / length * half
        out.append(np.array([a - n, b - n, b + n, a + n]))
    return out


def _platform_rings(st):
    """Rings (k, 2) of the platforms: a polygon, or a rectangle round each segment of a line (half its width)."""
    rings = []
    for it in st.platforms:
        if it["kind"] == "polygon" and bs.area(it["xy"]) >= 4.0:
            rings.append(_ccw(it["xy"]))
        elif it["kind"] == "line":
            try:
                half = float(it["tags"].get("width", "")) / 2.0
            except ValueError:
                half = PLATFORM_HALF_W_M
            rings += _segment_quads(it["xy"], min(max(half, 1.0), 4.0))
    return rings


TACTILE = ("line_white", 1.0)    # the strip for the blind along a platform's kerb (EU: light, ribbed)
TACTILE_IN_M, TACTILE_W_M = 0.25, 0.40
TACTILE_UP_M = 0.006            # over the slab's top (no z-fighting)
CELL_M = 1.0                # the walkable field's lattice (a block of 2 x 2 full cells is one quad)
MAX_VERTS = 600000
SDF_CHUNK = 20000


def _sd_ring(ring, pts):
    """Signed distance (k,) from points to a polygon: positive inside."""
    import polygon
    ring = bs._open(np.asarray(ring, float))
    a, b = ring, np.roll(ring, -1, axis=0)
    ab = b - a
    ab2 = np.maximum((ab * ab).sum(1), 1e-9)
    d = np.empty(len(pts))
    step = max(1, 400000 // max(len(a), 1))
    for k in range(0, len(pts), step):
        q = pts[k:k + step]
        t = np.clip(((q[:, None, 0] - a[None, :, 0]) * ab[None, :, 0] + (q[:, None, 1] - a[None, :, 1]) * ab[None, :, 1])
                    / ab2[None, :], 0.0, 1.0)
        d[k:k + step] = dnp.hypot(a[None, :, 0] + t * ab[None, :, 0] - q[:, None, 0],
                                 a[None, :, 1] + t * ab[None, :, 1] - q[:, None, 1]).min(1)
    inside = polygon.contains_points(ring, pts)
    return np.where(inside, d, -d)


def walk_field(st):
    """(x0, y0, F (ny + 1, nx + 1)) of the station's raised walkable ground: F is the signed distance (m, positive on the
    walkable side) at the lattice points x0 + i, y0 + j. Walkable: the platforms, and the ground under the roofs that is not
    a drive lane or a mouth. A distance, not a mask, so that the edge is cut where it really runs (marching squares in
    zone()): the 1 m cells' steps showed along every slanted edge. Memoised on the station (every tile asks for the same
    field, so the pieces meet)."""
    cached = getattr(st, "_walk", None)
    if cached is not None:
        return cached
    plat = _platform_rings(st)
    own = {p["id"] for p in st.platforms if p["kind"] == "polygon"}
    roofs_ = [_ccw(c["ring"]) for c in st.canopies if c["src"] not in own and len(bs._open(c["ring"])) >= 3]
    rings = plat + roofs_
    empty = (0, 0, np.zeros((0, 0)))
    if not rings:
        st._walk = empty
        return empty
    allp = np.vstack(rings)
    x0, y0 = int(math.floor(allp[:, 0].min())) - 1, int(math.floor(allp[:, 1].min())) - 1
    nx, ny = int(math.ceil(allp[:, 0].max())) + 1 - x0, int(math.ceil(allp[:, 1].max())) + 1 - y0
    if (nx + 1) * (ny + 1) > MAX_VERTS or nx < 1 or ny < 1:
        st._walk = empty
        return empty
    gx, gy = np.meshgrid(x0 + np.arange(nx + 1, dtype=float), y0 + np.arange(ny + 1, dtype=float))
    P = np.column_stack((gx.ravel(), gy.ravel()))
    f_plat = np.full(len(P), -1e9)
    for r in plat:
        f_plat = np.maximum(f_plat, _sd_ring(r, P))
    f_roof = np.full(len(P), -1e9)
    for r in roofs_:
        f_roof = np.maximum(f_roof, _sd_ring(r, P))
    f_ex = np.full(len(P), 1e9)
    for ln in st.lanes:
        f_ex = np.minimum(f_ex, bs.path_dist(ln["xy"], P) - ln["half"])
    for mx, my in st.mouths:
        f_ex = np.minimum(f_ex, dnp.hypot(P[:, 0] - mx, P[:, 1] - my) - bs.MOUTH_R_M)
    F = np.maximum(f_plat, np.minimum(f_roof, f_ex)).reshape(ny + 1, nx + 1)
    st._walk = (x0, y0, F)
    return st._walk


def walkable(st):
    """(x0, y0, mask (ny, nx) bool) of the lattice's cells whose four corners average on the walkable side."""
    x0, y0, F = walk_field(st)
    if not F.size:
        return x0, y0, np.zeros((0, 0), bool)
    return x0, y0, (F[:-1, :-1] + F[:-1, 1:] + F[1:, :-1] + F[1:, 1:]) >= 0.0


def _clip_cell(pts, f):
    """The part of a quad (corners pts (4, 2), values f (4,), counter-clockwise) where f >= 0, as a list of
    (polygon [(x, y, source)], ...) and its cut edges [((x, y), (x, y))] in travel order. source: ("c", k) a corner,
    ("e", k) a point on the edge k -> k + 1 (interpolated: the heights are blended the same way)."""
    inside = [v >= 0.0 for v in f]
    n_in = sum(inside)
    if n_in == 0:
        return [], []
    if n_in == 4:
        return [[(pts[k][0], pts[k][1], ("c", k)) for k in range(4)]], []

    def cross(k):
        a, b = f[k], f[(k + 1) % 4]
        t = a / (a - b)
        return (pts[k][0] + (pts[(k + 1) % 4][0] - pts[k][0]) * t, pts[k][1] + (pts[(k + 1) % 4][1] - pts[k][1]) * t,
                ("e", k, t))
    if n_in == 2 and inside[0] == inside[2]:                     # two opposite corners: two triangles
        polys, cuts = [], []
        for k in range(4):
            if inside[k]:
                e_in, e_out = cross((k - 1) % 4), cross(k)
                polys.append([e_in, (pts[k][0], pts[k][1], ("c", k)), e_out])
                cuts.append(((e_out[0], e_out[1]), (e_in[0], e_in[1])))
        return polys, cuts
    poly, cuts = [], []
    ring = []
    for k in range(4):
        if inside[k]:
            ring.append((pts[k][0], pts[k][1], ("c", k)))
        if inside[k] != inside[(k + 1) % 4]:
            ring.append(cross(k))
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        if a[2][0] == "e" and b[2][0] == "e":
            cuts.append(((a[0], a[1]), (b[0], b[1])))
    return [ring], cuts


KERB_DROP_IN_M = 0.8        # a crossing's dropped kerb: the slab at the road's level this far in from the kerb ...
KERB_DROP_RAMP_M = 1.5      # ... then rising to its height over this
KERB_DROP_SIDE_M = 0.3      # the dropped part is the crossing's width and this much on each side


def _kerb_heights(st, pts):
    """(k,) the slab's height over the ground at the points: PLATFORM_H_M, lowered to the road's level where a zebra
    crossing (crossings) meets the kerb (a dropped kerb: pushchairs and wheelchairs cross, the user's list 2026-11-09)."""
    pts = np.asarray(pts, float).reshape(-1, 2)
    h = np.full(len(pts), PLATFORM_H_M)
    for c, t, half in crossings(st):
        d = pts - c
        along = np.abs(dnp.matmul(d, t))
        across = np.abs(dnp.matmul(d, np.array([-t[1], t[0]])))
        band = along <= ZEBRA_LEN_M / 2.0 + KERB_DROP_SIDE_M
        near = band & (across <= half + KERB_DROP_IN_M + KERB_DROP_RAMP_M + 1.0)
        if not near.any():
            continue
        f = np.clip((across - half - KERB_DROP_IN_M) / KERB_DROP_RAMP_M, 0.0, 1.0)
        h[near] = np.minimum(h[near], PLATFORM_H_M * f[near])
    return h


def zone(frame, elev, st, mesh, tactile=True):
    """The raised walkable ground (platforms, and the walking zone under the roofs with the drive lanes left free): marching
    squares over walk_field's distance, so the edge follows the real outline; 2 m quads inside, partial cells clipped at
    the edge, a kerb wall along every cut edge; heights from the ground at the lattice points."""
    x0, y0, F = walk_field(st)
    if not F.size or not (F >= 0).any():
        return
    ny, nx = F.shape[0] - 1, F.shape[1] - 1
    lx, ly = np.meshgrid(x0 + np.arange(nx + 1, dtype=float), y0 + np.arange(ny + 1, dtype=float))
    L = np.column_stack((lx.ravel(), ly.ravel()))
    zg = ground(frame, elev, L).reshape(ny + 1, nx + 1)
    H = _kerb_heights(st, L).reshape(ny + 1, nx + 1)        # (the slab's height: lowered at the crossings)
    ins = F >= 0.0
    full = ins[:-1, :-1] & ins[:-1, 1:] & ins[1:, 1:] & ins[1:, :-1]
    part = (ins[:-1, :-1] | ins[:-1, 1:] | ins[1:, 1:] | ins[1:, :-1]) & ~full
    P, UV, T = [], [], []
    k0 = 0
    done = np.zeros_like(full)
    for j in range(0, ny - 1, 2):
        for i in range(0, nx - 1, 2):
            if full[j:j + 2, i:i + 2].all():
                done[j:j + 2, i:i + 2] = True
                for (jj, ii) in ((j, i), (j, i + 2), (j + 2, i + 2), (j + 2, i)):
                    P.append((lx[jj, ii], ly[jj, ii], zg[jj, ii] + H[jj, ii]))
                T += [[k0, k0 + 1, k0 + 2], [k0, k0 + 2, k0 + 3]]
                k0 += 4
    for j, i in zip(*np.nonzero(full & ~done)):
        for (jj, ii) in ((j, i), (j, i + 1), (j + 1, i + 1), (j + 1, i)):
            P.append((lx[jj, ii], ly[jj, ii], zg[jj, ii] + H[jj, ii]))
        T += [[k0, k0 + 1, k0 + 2], [k0, k0 + 2, k0 + 3]]
        k0 += 4
    wall_P, wall_UV, wall_T = [], [], []
    k1 = 0
    tac_P, tac_UV, tac_T = [], [], []
    k2 = 0
    for j, i in zip(*np.nonzero(part)):
        corners = ((j, i), (j, i + 1), (j + 1, i + 1), (j + 1, i))
        pts = [(lx[a, b], ly[a, b]) for a, b in corners]
        fv = [F[a, b] for a, b in corners]
        zv = [zg[a, b] for a, b in corners]
        hv = [H[a, b] for a, b in corners]

        def height(src):
            if src[0] == "c":
                return zv[src[1]]
            k, t = src[1], src[2]
            return zv[k] + (zv[(k + 1) % 4] - zv[k]) * t

        def lift(src):
            if src[0] == "c":
                return hv[src[1]]
            k, t = src[1], src[2]
            return hv[k] + (hv[(k + 1) % 4] - hv[k]) * t
        polys, cuts = _clip_cell(pts, fv)
        for poly in polys:
            if len(poly) < 3:
                continue
            base = k0
            for x, y, src in poly:
                P.append((x, y, height(src) + lift(src)))
                k0 += 1
            for q in range(1, len(poly) - 1):
                T.append([base, base + q, base + q + 1])
        for poly in polys:
            for q in range(len(poly)):
                a, b = poly[q], poly[(q + 1) % len(poly)]
                if a[2][0] == "e" and b[2][0] == "e" and (
                        ((a[0], a[1]), (b[0], b[1])) in cuts or ((b[0], b[1]), (a[0], a[1])) in cuts):
                    za, zb = height(a[2]), height(b[2])
                    ha, hb = lift(a[2]), lift(b[2])
                    length = float(math.hypot(b[0] - a[0], b[1] - a[1]))
                    if length < 1e-4:
                        continue
                    wall_P += [(a[0], a[1], za - SLAB_FOOT_M), (b[0], b[1], zb - SLAB_FOOT_M),
                               (b[0], b[1], zb + hb), (a[0], a[1], za + ha)]
                    wall_UV += [(0, (za - SLAB_FOOT_M) / KERB[1]), (length / KERB[1], (zb - SLAB_FOOT_M) / KERB[1]),
                                (length / KERB[1], (zb + hb) / KERB[1]), (0, (za + ha) / KERB[1])]
                    wall_T += [[k1, k1 + 1, k1 + 2], [k1, k1 + 2, k1 + 3]]
                    k1 += 4
                    # the tactile strip for the blind along the kerb (TACTILE_IN_M in, TACTILE_W_M wide)
                    cx = sum(v[0] for v in poly) / len(poly)
                    cy = sum(v[1] for v in poly) / len(poly)
                    ex, ey = (b[0] - a[0]) / length, (b[1] - a[1]) / length
                    nx, ny = -ey, ex
                    if (cx - (a[0] + b[0]) / 2) * nx + (cy - (a[1] + b[1]) / 2) * ny < 0:
                        nx, ny = -nx, -ny
                    z0, z1 = za + ha + TACTILE_UP_M, zb + hb + TACTILE_UP_M
                    o0, o1 = TACTILE_IN_M, TACTILE_IN_M + TACTILE_W_M
                    tac_P += [(a[0] + nx * o0, a[1] + ny * o0, z0), (b[0] + nx * o0, b[1] + ny * o0, z1),
                              (b[0] + nx * o1, b[1] + ny * o1, z1), (a[0] + nx * o1, a[1] + ny * o1, z0)]
                    ua, ub = (a[0] * ex + a[1] * ey) / TACTILE[1], (b[0] * ex + b[1] * ey) / TACTILE[1]
                    tac_UV += [(ua, 0.0), (ub, 0.0), (ub, 1.0), (ua, 1.0)]
                    tac_T += [[k2, k2 + 2, k2 + 1], [k2, k2 + 3, k2 + 2]] if nx * ey - ny * ex > 0 else \
                        [[k2, k2 + 1, k2 + 2], [k2, k2 + 2, k2 + 3]]
                    k2 += 4
    if P:
        Pn = np.asarray(P)
        mesh.add(PLATFORM_TOP[0], Pn, Pn[:, :2] / PLATFORM_TOP[1], np.asarray(T))
    if wall_P:
        mesh.add(KERB[0], wall_P, wall_UV, wall_T)
    if tac_P and tactile:
        mesh.add(TACTILE[0], tac_P, tac_UV, tac_T)


def platforms(frame, elev, st, mesh):
    """(kept for callers; the platforms are part of zone()'s walkable cells)"""


SLAB_STEP_M = 2.0           # the slab's top is cut to this (the graded ground's 2 m lattice: a coarse top let it show through)
SLAB_FOOT_M = 0.30          # the walls reach this far below the ground at their foot


def _densify(ring, step):
    """The counter-clockwise ring with a point at least every step metres."""
    ring = _ccw(ring)
    out = []
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        k = max(1, int(math.ceil(float(dnp.hypot(*(b - a))) / step)))
        out += [a + (b - a) * (i / k) for i in range(k)]
    return np.asarray(out)


def _slab(mesh, frame, elev, ring, height):
    """A slab height m over the ground: its top subdivided to SLAB_STEP_M with the ground sampled at every point, its
    walls from below the ground to the top, so that neither the terrain's bumps nor a steep bank show through."""
    import plazas
    dense = _densify(ring, SLAB_STEP_M)
    T = _triangulate(dense)
    if not len(T):
        return
    P, T = plazas._subdivide(dense, T, SLAB_STEP_M)
    z = ground(frame, elev, P) + height
    mesh.add(PLATFORM_TOP[0], np.column_stack((P, z)), P / PLATFORM_TOP[1], T)
    zd = ground(frame, elev, dense)
    walls(mesh, KERB[0], KERB[1], dense, zd - SLAB_FOOT_M, zd + height)


GABLE_MIN_W_M, GABLE_MAX_W_M = 4.0, 24.0    # a narrow, long, nearly rectangular roof is pitched; the rest stay flat
GABLE_FILL, GABLE_RISE = 0.85, 0.12         # of its bounding box; the ridge stands this share of the width over the eaves
SPAN_H_M = (3.6, 8.5)                       # an unmapped roof is as high as its span asks (a hall of 60 m is not 3.8 m)


def _obb(ring):
    """(t, along (min, max), lateral (min, max), width) of the ring's smallest bounding rectangle among those along its
    edges; width = its short side."""
    ring = bs._open(ring)
    best = None
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        d = b - a
        ln = float(dnp.hypot(*d))
        if ln < 0.5:
            continue
        t = d / ln
        n = np.array([-t[1], t[0]])
        u, v = dnp.matmul(ring, t), dnp.matmul(ring, n)
        ext = (float(u.min()), float(u.max()), float(v.min()), float(v.max()))
        area = (ext[1] - ext[0]) * (ext[3] - ext[2])
        if best is None or area < best[0]:
            best = (area, t, ext)
    if best is None:
        return None
    _, t, (u0, u1, v0, v1) = best
    if (u1 - u0) < (v1 - v0):                      # the long side is the axis
        t = np.array([-t[1], t[0]])
        u0, u1, v0, v1 = v0, v1, -u1, -u0
    return t, (u0, u1), (v0, v1), v1 - v0


def roof_height(cn):
    """A mapped roof's height, else by its span: higher the wider it is (3.6 m under 4 m, 8.5 m over 50 m)."""
    if cn.get("mapped"):
        return float(cn["height"])
    box = _obb(cn["ring"])
    w = box[3] if box else 0.0
    return float(np.clip(SPAN_H_M[0] + 0.10 * w, *SPAN_H_M))


def _gable(ring):
    """(vertices (k, 2), ridge fraction 0..1 per vertex, ridge parts) of a pitched roof over ring, or None. The ring with
    a vertex where it crosses the ridge line, each vertex's closeness to the ridge, and the two halves to triangulate."""
    ring = _ccw(ring)
    box = _obb(ring)
    if box is None:
        return None
    t, (u0, u1), (v0, v1), width = box
    if not (GABLE_MIN_W_M <= width <= GABLE_MAX_W_M) or (u1 - u0) < 1.4 * width:
        return None
    if bs.area(ring) < GABLE_FILL * (u1 - u0) * width:
        return None
    n = np.array([-t[1], t[0]])
    mid = (v0 + v1) / 2.0
    pts = []
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        pts.append(a)
        la, lb = float(dnp.matmul(a, n)) - mid, float(dnp.matmul(b, n)) - mid
        if la * lb < 0:
            pts.append(a + (b - a) * (la / (la - lb)))
    pts = np.asarray(pts)
    lat = dnp.matmul(pts, n) - mid
    frac = 1.0 - np.minimum(np.abs(lat) / (width / 2.0), 1.0)
    halves = []
    for sign in (1.0, -1.0):
        sel = [i for i in range(len(pts)) if sign * lat[i] >= -1e-6]
        if len(sel) >= 3:
            halves.append(sel)
    return pts, frac, halves, width


ROOF_STEP_M = 12.0          # a roof's faces and walls are cut to this, so that every road tile has its share of them
LANE_CLEAR_M = 4.8          # a roof over a drive lane or a mouth is at least this high (a bus is 3.4 m)


def _face(mesh, mat, rep, xy, zfun, up=True, step=ROOF_STEP_M):
    """A polygon's face cut to step metres (plazas._subdivide), heights zfun(P (k, 2)) -> (k,)."""
    import plazas
    xy = bs._open(np.asarray(xy, np.float64))
    if _signed(xy) < 0:
        xy = xy[::-1]
    T = _triangulate(xy)
    if not len(T):
        return
    P, T = plazas._subdivide(xy, T, step)
    P = np.asarray(P, np.float64)
    if not up:
        T = np.asarray(T)[:, ::-1]
    mesh.add(mat, np.column_stack((P, zfun(P))), P / rep, T)


def _over_lane(st, ring):
    """Does a drive lane or a mouth lie under this roof?"""
    for ln in st.lanes:
        pts = bs._open(ln["xy"]) if len(ln["xy"]) > 2 else ln["xy"]
        dense = np.vstack([a + (b - a) * (np.arange(0, max(1, int(dnp.hypot(*(b - a)) // 3))) / max(1, int(dnp.hypot(*(b - a)) // 3)))[:, None]
                           for a, b in zip(pts[:-1], pts[1:])]) if len(pts) > 1 else pts
        if any(bs.contains(ring, q) for q in dense):
            return True
    return any(bs.contains(ring, m) for m in st.mouths)


ROOF_DEM_M = 4.0            # the DEM pixel a roof's base height is read at


def roof_base(frame, dem, st, cn, ring):
    """The ground height (frame z) a roof stands on: the median of the raw DEM over its footprint, the same for every
    tile and process (the tile's own ground differs: its road grading, its height quantum, whether the yard's pads had
    arrived, so one roof's pieces stood at different heights and showed the sky between them). Memoised on the station.
    dem: an object with sample(lat, lon, zoom)."""
    import dem as demmod
    import polygon
    memo = st.__dict__.setdefault("_roof_base", {})
    key = (cn["src"], round(frame.lat0, 6), round(frame.lon0, 6))
    if key in memo:
        return memo[key]
    lo, hi = ring.min(0), ring.max(0)
    step = 6.0
    while ((hi[0] - lo[0]) / step + 1) * ((hi[1] - lo[1]) / step + 1) > 400:
        step *= 1.5
    gx, gy = np.meshgrid(np.arange(lo[0], hi[0] + step, step), np.arange(lo[1], hi[1] + step, step))
    pts = np.column_stack((gx.ravel(), gy.ravel()))
    pts = pts[polygon.contains_points(ring, pts)]
    if not len(pts):
        pts = bs.centroid(ring)[None]
    la, lo_, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
    h = np.asarray(dem.sample(la, lo_, demmod.zoom_for(float(np.mean(la)), ROOF_DEM_M)), np.float64).ravel()
    _, _, z = frame.geodetic_to_enu(la, lo_, h)
    memo[key] = float(np.median(z))
    return memo[key]


def roofs(frame, elev, st, mesh, dem=None):
    """Each canopy as an open roof on columns round its edge (and a grid inside a wide one): flat, or pitched when it is a
    narrow, long, nearly rectangular one (a gable over a bus island); as high as it is mapped, else as its span asks; at
    least LANE_CLEAR_M over a drive lane. Faces and walls are cut to ROOF_STEP_M."""
    mat, rep = STEEL
    placed = []                                 # columns already standing (roofs that overlap share theirs)
    for cn in st.canopies:
        ring = _ccw(cn["ring"])
        if len(ring) < 3:
            continue
        zc = roof_base(frame, dem, st, cn, ring) if dem is not None else float(ground(frame, elev, bs.centroid(ring)[None])[0])
        height = roof_height(cn)
        if (st.lanes or st.mouths) and _over_lane(st, ring):
            height = max(height, LANE_CLEAR_M)
        zeave = zc + height
        pitched = None if cn.get("flat") else _gable(ring)
        if pitched is not None:
            pts_g, frac, halves, width = pitched
            t, (u0, u1), (v0, v1), _w = _obb(ring)
            nn = np.array([-t[1], t[0]])
            mid = (v0 + v1) / 2.0
            rise = GABLE_RISE * width

            def zfun(P, zeave=zeave, nn=nn, mid=mid, width=width, rise=rise):
                lat = dnp.matmul(np.asarray(P, float), nn) - mid
                return zeave + rise * (1.0 - np.minimum(np.abs(lat) / (width / 2.0), 1.0))
            for sel in halves:
                _face(mesh, mat, rep, pts_g[sel], zfun)
                _face(mesh, mat, rep, pts_g[sel], lambda P, z=zfun: z(P) - ROOF_THICK_M, up=False)
            edge = _densify(pts_g, ROOF_STEP_M)
            walls(mesh, mat, rep, edge, zfun(edge) - ROOF_THICK_M, zfun(edge))
            zbot = zeave - ROOF_THICK_M
        else:
            ztop = zeave
            zbot = ztop - ROOF_THICK_M
            _face(mesh, mat, rep, ring, lambda P, z=ztop: np.full(len(P), z))
            _face(mesh, mat, rep, ring, lambda P, z=zbot: np.full(len(P), z), up=False)
            edge = _densify(ring, ROOF_STEP_M)
            walls(mesh, mat, rep, edge, zbot, ztop)
        pts = [p for p in _column_points(ring, st) if all(float(dnp.hypot(*(p - q))) >= 1.5 for q in placed)]
        placed += pts
        centre = bs.centroid(ring)
        for p in pts[::2 if cn["area"] < 2000 else 3]:                 # a fixture under the roof by every other column
            v = centre - p
            d = float(dnp.hypot(*v))
            ax = v / d if d > 1e-6 else np.array([1.0, 0.0])
            q = p + ax * min(FIXTURE_IN_M, d * 0.5)
            pn = np.array([-ax[1], ax[0]])
            rect = np.array([q - ax * FIXTURE_L_M / 2 - pn * FIXTURE_W_M / 2, q + ax * FIXTURE_L_M / 2 - pn * FIXTURE_W_M / 2,
                             q + ax * FIXTURE_L_M / 2 + pn * FIXTURE_W_M / 2, q - ax * FIXTURE_L_M / 2 + pn * FIXTURE_W_M / 2])
            if bs.contains(ring, q):
                top_face(mesh, LIGHT[0], LIGHT[1], rect, zbot - 0.02, up=False)
        half = COLUMN_M / 2 * (1.0 + max(0.0, height - 4.0) * 0.12)      # taller roofs stand on thicker columns
        for p in pts:
            sq = np.array([[p[0] - half, p[1] - half], [p[0] + half, p[1] - half],
                           [p[0] + half, p[1] + half], [p[0] - half, p[1] + half]])
            zg = float(ground(frame, elev, np.asarray(p)[None])[0])
            walls(mesh, mat, rep, sq, zg - 0.05, zbot)


def _column_points(ring, st=None):
    """Column positions: round the ring inset 0.5 m every COLUMN_STEP_M, plus a grid in a roof wider than
    2 * COLUMN_GRID_M / 2."""
    ring = bs._open(ring)
    c = bs.centroid(ring)
    pts = []
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        length = float(dnp.hypot(*(b - a)))
        k = max(1, int(math.ceil(length / COLUMN_STEP_M)))
        for s in range(k):
            p = a + (b - a) * (s / k)
            v = c - p
            d = float(dnp.hypot(*v))
            pts.append(p + v / d * min(0.5, d * 0.2) if d > 1e-6 else p)
    x0, y0 = ring.min(0)
    x1, y1 = ring.max(0)
    if min(x1 - x0, y1 - y0) > COLUMN_GRID_M:
        for x in np.arange(x0 + COLUMN_GRID_M / 2, x1, COLUMN_GRID_M):
            for y in np.arange(y0 + COLUMN_GRID_M / 2, y1, COLUMN_GRID_M):
                if bs.contains(ring, (x, y)) and bs.dist_to_path(ring, (x, y), True) > 2.0:
                    pts.append(np.array([x, y]))
    if st is not None and (st.lanes or st.mouths) and pts:
        keep = ~bs.lane_blocked(st, np.asarray(pts), pad=0.5)      # a column never stands in a drive lane or a mouth
        pts = [p for p, k in zip(pts, keep) if k]
    if st is not None and pts:
        pts = [p for p in pts if not _in_wall(st, ring, p)]
    return pts


COLUMN_WALL_M = 0.8         # a column stands this far from a building's wall at least


def _in_wall(st, ring, p):
    """Is the column point p inside a building, or within COLUMN_WALL_M of its wall? (the roof's own building, the hall
    the roof is made of, does not count: its ring is the roof's)"""
    area = bs.area(ring)
    for b in [it["xy"] for it in st.buildings] + list(st.obstacles):
        b = bs._open(np.asarray(b, float))
        if len(b) < 3:
            continue
        if bs.contains(ring, bs.centroid(b)) and bs.area(b) >= 0.6 * area:
            continue
        if bs.contains(b, p) or bs.dist_to_path(b, p, True) < COLUMN_WALL_M:
            return True
    return False


def line_chunks(stations, box):
    """The bays' painted outlines (the far side and the two ends) as the road mesh's own line chunks, as the parking lots'
    stall lines are: the road mesh drapes them on the drawn ground itself (no height to guess: a mesh of mine at 7 cm was
    hidden by the terrain and at 50 cm hung in the air, the user 2026-11-07). The numbers are plates on posts
    (busstationsign). box: the road tile's (x0, y0, x1, y1); the chunks are clipped to it."""
    import parking
    segs = []
    for st in stations:
        if not st.confident():
            continue
        for bay in busbays.layout_bays(st):
            ns, ne, fs, fe = bay.corners()
            pts = [ns, fs, fe, ne]
            segs += [((float(a[0]), float(a[1]), 0.0), (float(b[0]), float(b[1]), 0.0)) for a, b in zip(pts[:-1], pts[1:])]
    return parking._boxed_chunks(segs, box) if segs else []


ARROW_STEP_M = 25.0         # an arrow every this along a one-way lane
ARROW_END_M = 8.0           # ... but none this near its ends
STOP_BACK_M = 2.5           # the stop line stands this far before a lane's exit
STOP_W_M = 0.45


def lane_chunks(stations, box):
    """Road mesh chunks that mark the yard's lanes (plan 027): a "straight" arrow every ARROW_STEP_M along each one-way
    lane, in its travel direction, and a stop line across a one-way lane at its exit to a public street. In the road
    mesh's own form (parking._line_chunk's lines, roads.py's arrow quads); only what lies in the tile box (x0, y0, x1, y1)."""
    import markings
    x0, y0, x1, y1 = (float(v) for v in box)
    out = []
    for st in stations:
        if not st.confident():
            continue
        import busbays
        rects = [busbays._rect(b) for b in busbays.layout_bays(st)]
        for ln in st.lanes:
            if not ln["oneway"]:
                continue
            path = np.asarray(ln["xy"], float)
            seg = dnp.hypot(*np.diff(path, axis=0).T)
            total = float(seg.sum())
            if total < 2 * ARROW_END_M + 2.0:
                continue
            cum = np.concatenate(([0.0], np.cumsum(seg)))

            def at(d):
                k = min(int(np.searchsorted(cum, d, side="right")) - 1, len(seg) - 1)
                t = (path[k + 1] - path[k]) / max(float(seg[k]), 1e-9)
                return path[k] + t * (d - cum[k]), t
            k = max(1, int((total - 2 * ARROW_END_M) // ARROW_STEP_M) + 1)
            for i in range(k):
                d = ARROW_END_M + (total - 2 * ARROW_END_M) * (i + 0.5) / k
                c, t = at(d)
                if not (x0 <= c[0] < x1 and y0 <= c[1] < y1):
                    continue
                if any(bs.contains(r, c) for r in rects):
                    continue                                   # (not where a bay stands on the lane)
                corners, uv = markings.arrow_quad(np.array([c[0], c[1], 0.0]), t, "through")
                out.append({"m": markings.ARROW_MATERIAL, "w": 0.0, "tl": 4.0, "p": 6, "layer": "mark",
                            "poly": [[round(float(v), 3) for v in q] for q in corners], "uvq": uv.tolist(), "n": []})
            end = path[-1]
            if any(math.hypot(end[0] - mx, end[1] - my) < 1.0 for mx, my in st.mouths):     # the exit onto a street
                c, t = at(max(total - STOP_BACK_M, 0.0))
                if x0 <= c[0] < x1 and y0 <= c[1] < y1:
                    nrm = np.array([-t[1], t[0]]) * ln["core"] * 0.9
                    a_, b_ = c - nrm, c + nrm
                    out.append({"m": "line_white", "w": STOP_W_M, "tl": 6.4, "p": 2,
                                "n": [[round(float(a_[0]), 3), round(float(a_[1]), 3), 0.0],
                                      [round(float(b_[0]), 3), round(float(b_[1]), 3), 0.0]]})
    return out


ZEBRA_STRIPE_M, ZEBRA_GAP_M, ZEBRA_LEN_M = 0.5, 0.5, 3.0     # a crossing's stripes (EU: 0.5 m bars, 3 m deep)
ZEBRA_SIDE_M = 1.4          # a crossing joins two walking grounds this far beyond the lane's drawn edges
ZEBRA_EVERY_M = 45.0        # at most one crossing per this along a lane
EDGE_IN_M, EDGE_W_M = 0.2, 0.12     # a lane's edge line: this far in from its drawn edge, this wide
EDGE_MIN_RUN_M = 4.0


def _walk_ok(st, pts):
    """(k,) bool: the points on the station's raised walking ground (walk_field >= 0.3)."""
    x0, y0, F = walk_field(st)
    pts = np.asarray(pts, float).reshape(-1, 2)
    if not F.size:
        return np.zeros(len(pts), bool)
    i = np.round(pts[:, 0] - x0).astype(int)
    j = np.round(pts[:, 1] - y0).astype(int)
    ok = (i >= 0) & (j >= 0) & (i < F.shape[1]) & (j < F.shape[0])
    out = np.zeros(len(pts), bool)
    out[ok] = F[j[ok], i[ok]] >= 0.3
    return out


def _lane_frames(ln, step=1.0):
    """(centre points (k, 2), unit tangents (k, 2), arc lengths (k,)) every step along a lane."""
    path = np.asarray(ln["xy"], float)
    seg = dnp.hypot(*np.diff(path, axis=0).T)
    if not len(seg) or float(seg.sum()) < step:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    d = np.arange(0.0, float(cum[-1]) + 1e-9, step)
    k = np.clip(np.searchsorted(cum, d, side="right") - 1, 0, len(seg) - 1)
    t = (path[k + 1] - path[k]) / np.maximum(seg[k], 1e-9)[:, None]
    c = path[k] + t * (d - cum[k])[:, None]
    return c, t, d


def _runs(ok):
    """[(i, j)] of the runs of True in ok (inclusive)."""
    out, i = [], 0
    while i < len(ok):
        if not ok[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(ok) and ok[j + 1]:
            j += 1
        out.append((i, j))
        i = j + 1
    return out


def crossings(st):
    """[(centre (2,), tangent (2,), half width)]: zebra crossings over the yard's lanes where a walking ground (a platform,
    the zone under a roof) lies on both sides, away from the bays, the mouths and the lanes' ends; one per ZEBRA_EVERY_M.
    Memoised on the station."""
    got = st.__dict__.get("_crossings")
    if got is not None:
        return got
    import busbays
    rects = [busbays._rect(b) for b in busbays.layout_bays(st)]
    out = []
    for ln in st.lanes:
        c, t, d = _lane_frames(ln)
        if len(c) < 8:
            continue
        nrm = np.column_stack((-t[:, 1], t[:, 0]))
        reach = ln["core"] + ZEBRA_SIDE_M
        ok = _walk_ok(st, c + nrm * reach) & _walk_ok(st, c - nrm * reach)
        ok &= (d > 6.0) & (d < d[-1] - 6.0)
        for mx, my in st.mouths:
            ok &= dnp.hypot(c[:, 0] - mx, c[:, 1] - my) > bs.MOUTH_R_M + 3.0
        for r in rects:
            lo, hi = r.min(0) - ZEBRA_LEN_M, r.max(0) + ZEBRA_LEN_M
            for i in np.flatnonzero(ok & (c[:, 0] >= lo[0]) & (c[:, 0] <= hi[0]) & (c[:, 1] >= lo[1]) & (c[:, 1] <= hi[1])):
                if bs.contains(r, c[i]) or bs.dist_to_path(r, c[i], True) < ZEBRA_LEN_M:
                    ok[i] = False
        last = -1e9
        for i, j in _runs(ok):
            if d[j] - d[i] >= ZEBRA_LEN_M:
                m = (i + j) // 2
                if d[m] - last >= ZEBRA_EVERY_M:
                    out.append((c[m], t[m], float(ln["core"])))
                    last = d[m]
    st._crossings = out
    return out


def crossing_chunks(stations, box):
    """The zebra crossings (crossings) as road mesh line chunks: bars along the traffic across the lane's drawn width."""
    x0, y0, x1, y1 = (float(v) for v in box)
    out = []
    for st in stations:
        if not st.confident():
            continue
        for c, t, half in crossings(st):
            if not (x0 <= c[0] < x1 and y0 <= c[1] < y1):
                continue
            nrm = np.array([-t[1], t[0]])
            pitch = ZEBRA_STRIPE_M + ZEBRA_GAP_M
            k = max(1, int((2.0 * half) // pitch))
            start = -k * pitch / 2.0 + pitch / 2.0
            for i in range(k):
                o = start + i * pitch
                a_ = c + nrm * o - t * ZEBRA_LEN_M / 2
                b_ = c + nrm * o + t * ZEBRA_LEN_M / 2
                out.append({"m": "line_white", "w": ZEBRA_STRIPE_M, "tl": 6.4, "p": 2,
                            "n": [[round(float(a_[0]), 3), round(float(a_[1]), 3), 0.0],
                                  [round(float(b_[0]), 3), round(float(b_[1]), 3), 0.0]]})
    return out


def edge_runs(st):
    """[points (k, 2)]: the yard lanes' edge lines along both drawn edges, not over another lane, a bay, a crossing, a mouth
    or a platform; runs shorter than EDGE_MIN_RUN_M left out. Memoised on the station."""
    got = st.__dict__.get("_edge_runs")
    if got is not None:
        return got
    import busbays
    rects = [busbays._rect(b) for b in busbays.layout_bays(st)]
    zebras = crossings(st)
    out = []
    for li, ln in enumerate(st.lanes):
        c, t, d = _lane_frames(ln)
        if len(c) < 4:
            continue
        nrm = np.column_stack((-t[:, 1], t[:, 0]))
        for sgn in (1.0, -1.0):
            p = c + nrm * sgn * (ln["core"] - EDGE_IN_M)
            ok = np.ones(len(p), bool)
            for lj, other in enumerate(st.lanes):
                if lj != li:
                    ok &= bs.path_dist(other["xy"], p) > other["core"] + 0.3
            for mx, my in st.mouths:
                ok &= dnp.hypot(p[:, 0] - mx, p[:, 1] - my) > bs.MOUTH_R_M
            for r in rects:
                lo, hi = r.min(0) - 1.0, r.max(0) + 1.0
                for i in np.flatnonzero(ok & (p[:, 0] >= lo[0]) & (p[:, 0] <= hi[0]) & (p[:, 1] >= lo[1]) & (p[:, 1] <= hi[1])):
                    if bs.contains(r, p[i]) or bs.dist_to_path(r, p[i], True) < 0.8:
                        ok[i] = False
            for zc, _zt, zh in zebras:
                ok &= dnp.hypot(p[:, 0] - zc[0], p[:, 1] - zc[1]) > ZEBRA_LEN_M / 2 + zh + 0.5
            ok &= ~_walk_ok(st, p)
            for i, j in _runs(ok):
                if d[j] - d[i] >= EDGE_MIN_RUN_M:
                    out.append(p[i:j + 1])
    st._edge_runs = out
    return out


def edge_chunks(stations, box):
    """edge_runs as road mesh line chunks; a run is drawn by the tile its middle is in."""
    x0, y0, x1, y1 = (float(v) for v in box)
    out = []
    for st in stations:
        if not st.confident():
            continue
        for run in edge_runs(st):
            mid = run[len(run) // 2]
            if not (x0 <= mid[0] < x1 and y0 <= mid[1] < y1):
                continue
            pts = run[::3]
            if not np.allclose(pts[-1], run[-1]):
                pts = np.vstack((pts, run[-1]))
            out.append({"m": "line_white", "w": EDGE_W_M, "tl": 6.4, "p": 2,
                        "n": [[round(float(q[0]), 3), round(float(q[1]), 3), 0.0] for q in pts]})
    return out


BOLLARD_R_M, BOLLARD_H_M = 0.09, 0.9     # a steel bollard at the tip of an island facing a lane
BOLLARD_IN_M = 0.45         # in from the tip
BOLLARD_GAP_M = 1.1         # between bollards across the tip
BOLLARD_LANE_M = 6.0        # a tip this near a drive lane gets them


def _tips(st):
    """[(tip point, unit vector out of the platform, width)] of the platforms' ends: a mapped line's two ends, a polygon's
    two short sides (its smallest bounding rectangle's)."""
    out = []
    for pl in st.platforms:
        xy = np.asarray(pl["xy"], float)
        if pl["kind"] == "line" and len(xy) >= 2:
            try:
                w = float(pl["tags"].get("width", "")) or 2 * PLATFORM_HALF_W_M
            except ValueError:
                w = 2 * PLATFORM_HALF_W_M
            for a, b in ((xy[0], xy[1]), (xy[-1], xy[-2])):
                d = a - b
                ln = float(dnp.hypot(*d))
                if ln > 1e-6:
                    out.append((a, d / ln, w))
        elif pl["kind"] == "polygon" and len(bs._open(xy)) >= 3:
            box = _obb(bs._open(xy))
            if box is None:
                continue
            t, (u0, u1), (v0, v1), w = box
            n = np.array([-t[1], t[0]])
            mid = (v0 + v1) / 2.0
            if (u1 - u0) < 2.0 * w:
                continue                                    # (not an island: no tip)
            for u, sgn in ((u0, -1.0), (u1, 1.0)):
                out.append((t * u + n * mid, t * sgn, w))
    return out


def bollard_points(st):
    """[(x, y)] of the bollards: across each platform tip that points at a drive lane (BOLLARD_LANE_M), BOLLARD_IN_M
    in from it, BOLLARD_GAP_M apart, on the walking ground and off every lane. Memoised on the station."""
    got = st.__dict__.get("_bollards")
    if got is not None:
        return got
    out = []
    for tip, out_dir, w in _tips(st):
        if w < 1.6 or not st.lanes:
            continue
        probe = tip + out_dir * 2.0
        if not bool(bs.lane_blocked(st, probe[None], pad=BOLLARD_LANE_M, core=True, mouths=False)[0]):
            continue
        side = np.array([-out_dir[1], out_dir[0]])
        k = max(2, int((w - 0.6) // BOLLARD_GAP_M) + 1)
        span = (k - 1) * BOLLARD_GAP_M
        for i in range(k):
            q = tip - out_dir * BOLLARD_IN_M + side * (-span / 2.0 + i * BOLLARD_GAP_M)
            if not _walk_ok(st, q[None])[0]:
                continue
            if bool(bs.lane_blocked(st, q[None], pad=0.2, core=True, mouths=False)[0]):
                continue
            out.append((float(q[0]), float(q[1])))
    st._bollards = out
    return out


def _bollard(mesh, frame, elev, st, p, collide=False):
    """One bollard (an octagonal steel post with a rounded cap ring) on the slab at p."""
    zg = float(ground(frame, elev, np.asarray(p)[None])[0]) + float(_kerb_heights(st, np.asarray(p)[None])[0])
    a = np.linspace(0.0, 2.0 * np.pi, 9)[:-1]
    ring = np.column_stack((p[0] + BOLLARD_R_M * dnp.cos(a), p[1] + BOLLARD_R_M * dnp.sin(a)))
    walls(mesh, STEEL[0], STEEL[1], ring, zg - 0.05, zg + BOLLARD_H_M)
    top_face(mesh, STEEL[0], STEEL[1], ring, zg + BOLLARD_H_M)


ZIG_AMP_M, ZIG_PERIOD_M, ZIG_W_M = 0.22, 1.0, 0.12      # the yellow zigzag along a bay's kerb side (no stopping)
ZIG_OUT_M = 0.4             # in from the kerb edge, into the bay


def zigzag_chunks(stations, box):
    """The yellow zigzag lines (road mesh line chunks) along every bay's kerb side: the "bus stop, no stopping" marking."""
    import busbays
    x0, y0, x1, y1 = (float(v) for v in box)
    out = []
    for st in stations:
        if not st.confident():
            continue
        for bay in busbays.layout_bays(st):
            if not bay.label and not bay.synth:
                continue
            if not (x0 <= bay.c[0] < x1 and y0 <= bay.c[1] < y1):
                continue
            n = bay.n
            base = bay.c - n * (bay.width / 2.0 - ZIG_OUT_M)
            k = max(2, int(bay.length // (ZIG_PERIOD_M / 2.0)))
            s = (np.arange(k + 1) / k - 0.5) * (bay.length - 1.0)
            side = np.where(np.arange(k + 1) % 2 == 0, 1.0, -1.0) * ZIG_AMP_M
            pts = base[None, :] + bay.t[None, :] * s[:, None] + n[None, :] * side[:, None]
            out.append({"m": "line_yellow", "w": ZIG_W_M, "tl": 6.4, "p": 2,
                        "n": [[round(float(q[0]), 3), round(float(q[1]), 3), 0.0] for q in pts]})
    return out


KIOSK_W_M, KIOSK_D_M, KIOSK_H_M = 3.2, 2.6, 2.7


def kiosk_spot(st):
    """(x, y, yaw_x, yaw_y) of a ticket kiosk for a station with no hall: on the largest platform near its middle, its front
    toward the bays, or None. Memoised on the station."""
    got = st.__dict__.get("_kiosk", False)
    if got is not False:
        return got
    spot = None
    if not st.buildings and st.platforms:
        rings = _platform_rings(st)
        big = max(rings, key=lambda r: bs.area(r)) if rings else None
        box = _obb(big) if big is not None else None
        if box is not None and box[3] >= 3.6:
            t, (u0, u1), (v0, v1), w = box
            n = np.array([-t[1], t[0]])
            for frac in (0.5, 0.3, 0.7):
                c = t * (u0 + (u1 - u0) * frac) + n * (v0 + v1) / 2.0
                hw, hd = KIOSK_W_M / 2.0 + 0.6, KIOSK_D_M / 2.0 + 0.6
                quad = np.array([c + t * hw + n * hd, c - t * hw + n * hd, c - t * hw - n * hd, c + t * hw - n * hd])
                if any(bs.contains(cn["ring"], c) for cn in st.canopies):
                    continue                                    # (not under a roof: it stands in the open)
                if _walk_ok(st, quad).all() and not bool(bs.lane_blocked(st, quad, pad=0.3, mouths=True).any()):
                    spot = (float(c[0]), float(c[1]), float(t[0]), float(t[1]))
                    break
    st._kiosk = spot
    return spot


def _kiosk(mesh, frame, elev, st, spot):
    """A small flat-roofed kiosk (concrete walls, steel roof) on the slab."""
    x, y, tx, ty = spot
    t = np.array([tx, ty])
    n = np.array([-ty, tx])
    c = np.array([x, y])
    hw, hd = KIOSK_W_M / 2.0, KIOSK_D_M / 2.0
    ring = np.array([c + t * hw + n * hd, c - t * hw + n * hd, c - t * hw - n * hd, c + t * hw - n * hd])
    zg = float(ground(frame, elev, c[None])[0]) + PLATFORM_H_M
    walls(mesh, PLATFORM_TOP[0], PLATFORM_TOP[1], ring, zg - 0.05, zg + KIOSK_H_M)
    top_face(mesh, STEEL[0], STEEL[1], ring + 0.0, zg + KIOSK_H_M + 0.15)
    big = np.array([c + t * (hw + 0.25) + n * (hd + 0.25), c - t * (hw + 0.25) + n * (hd + 0.25),
                    c - t * (hw + 0.25) - n * (hd + 0.25), c + t * (hw + 0.25) - n * (hd + 0.25)])
    top_face(mesh, STEEL[0], STEEL[1], big, zg + KIOSK_H_M + 0.15)


COL_COLUMN_H_M = 4.0        # a column's collider reaches this high (a car meets its foot, not the roof)


def collision(frame, box, stations, elev, origin):
    """The stations' lean collision mesh of the tile box (x0, y0, x1, y1): the raised walking ground (its top and kerb
    walls, zone) and the roofs' columns, on the physics ground elev; (V, T, UV, mats, idx) or None. Its colliders
    [x0, y0, z0, x1, y1, z1] (frame metres) with it: (mesh, boxes). The platforms were render-only: a car drove through
    the kerbs (plan 034)."""
    m = _Mesh()
    for st in stations:
        if not st.confident():
            continue
        zone(frame, elev, st, m, tactile=False)
        for cn in st.canopies:
            ring = _ccw(cn["ring"])
            if len(ring) < 3:
                continue
            for p in _column_points(ring, st):
                half = COLUMN_M / 2 * 1.2
                sq = np.array([[p[0] - half, p[1] - half], [p[0] + half, p[1] - half],
                               [p[0] + half, p[1] + half], [p[0] - half, p[1] + half]])
                zg = float(ground(frame, elev, np.asarray(p)[None])[0])
                walls(m, STEEL[0], STEEL[1], sq, zg - 0.3, zg + COL_COLUMN_H_M)
        for p in bollard_points(st):
            _bollard(m, frame, elev, st, p)
        ks = kiosk_spot(st)
        if ks is not None:
            hw = KIOSK_W_M / 2.0
            c, t = np.array(ks[:2]), np.array(ks[2:])
            n = np.array([-t[1], t[0]])
            walls(m, STEEL[0], STEEL[1], np.array([c + t * hw + n * KIOSK_D_M / 2, c - t * hw + n * KIOSK_D_M / 2,
                                                   c - t * hw - n * KIOSK_D_M / 2, c + t * hw - n * KIOSK_D_M / 2]),
                  float(ground(frame, elev, c[None])[0]), float(ground(frame, elev, c[None])[0]) + KIOSK_H_M)
    got = m.result(box, origin)
    if got is None:
        return None, []
    V = np.asarray(got[0], float) + np.asarray(origin, float)
    boxes = []
    step = 20.0                                         # (the colliders as 20 m boxes, like the decks' and barriers')
    keys = np.floor(V[:, :2] / step).astype(np.int64)
    for k in np.unique(keys, axis=0):
        sel = V[(keys[:, 0] == k[0]) & (keys[:, 1] == k[1])]
        boxes.append([round(float(sel[:, 0].min() - 0.5), 2), round(float(sel[:, 1].min() - 0.5), 2),
                      round(float(sel[:, 2].min() - 0.5), 2), round(float(sel[:, 0].max() + 0.5), 2),
                      round(float(sel[:, 1].max() + 0.5), 2), round(float(sel[:, 2].max() + 0.5), 2)])
    return got, boxes


def mesh(frame, box, stations, elev, origin, dem=None, overlap=False):
    """The station meshes of the tile box (x0, y0, x1, y1): (V, T, UV, mats, idx) in the road mesh's form, or None.
    (The yard's paving is the ground's: busyard.py.)"""
    m = _Mesh()
    for st in stations:
        if not st.confident():
            continue
        zone(frame, elev, st, m)
        roofs(frame, elev, st, m, dem)
        for p in bollard_points(st):
            _bollard(m, frame, elev, st, p)
        ks = kiosk_spot(st)
        if ks is not None:
            _kiosk(m, frame, elev, st, ks)
    return m.result(box, origin, overlap)
