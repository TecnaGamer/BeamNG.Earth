"""The mesh of railway stations (railstations.py's RailStation, task 037): the platforms as raised slabs whose top stands the
station's height over the rail head, with a kerb wall to the ballast, the tactile strip along the track edge and the white
safety line. In the road mesh's form (positions relative to origin, tris, uvs, [material], tri material index), merged into a
tile's road mesh like busstationmesh's (blockworker._with_rail_stations). A tile takes the triangles whose centre lies in
its box, so every tile that sees a station makes the same one and the pieces join.

Heights: the ground under a track is graded to its bed line (railways.GRADE_HALF_M either side), so the drawn ground at a
platform's edge IS the bed there; the rail head stands RAIL_TOP_M over it (railways.RAIL_TOP_M) and the platform top
HEIGHT over that (the mapped `height`, else the country's norm: railstations.norm). One height reference per platform:
the edge's points, smoothed along it, weighted by distance for the points inside.
"""
import math

import numpy as np

import busstationmesh as bm
import busstations as bs
import railbridges
import railstations as rs

RAIL_TOP_M = 0.58           # the rail head over the bed (railways.RAIL_TOP_M: ballast 0.3 - sleeper sink 0.08 + sleeper 0.2 + rail 0.16)
SMOOTH_N = 13               # reference points averaged along the track (REF_STEP_M apart): the DEM's noise out of it
REF_STEP_M = 4.0
REF_NEAR_M = 14.0           # track points this near the platform are its reference
REF_REACH_M = 25.0          # a platform with no edge at a track follows the nearest track within this
REF_K = 6                   # nearest reference points weighed for a point inside
STRIP_IN_M = 0.45           # the tactile strip stands this far in from the edge ...
STRIP_W_M = 0.40
LINE_IN_M = 1.00            # ... the safety line this far
LINE_W_M = 0.12
LIFT_M = 0.006              # strips over the slab's top (no z-fighting)
RUN_BREAK_M = 6.0           # a gap in the snapped points longer than this ends a run of the edge
TILE_MARGIN_M = 6.0

CONCRETE = bm.PLATFORM_TOP
KERB = bm.KERB
LINE = bm.TACTILE


def _runs(snapped, ring):
    """[[index, ...]] the runs of consecutive snapped ring points (the ring is closed: a run may wrap)."""
    n = len(ring)
    idx = np.flatnonzero(snapped)
    if not len(idx):
        return []
    runs, cur = [], [int(idx[0])]
    for i in idx[1:]:
        i = int(i)
        gap = float(np.hypot(*(ring[i] - ring[cur[-1]])))
        if i - cur[-1] <= 2 and gap < RUN_BREAK_M:
            cur.append(i)
        else:
            runs.append(cur)
            cur = [i]
    runs.append(cur)
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][-1] == n - 1 and             float(np.hypot(*(ring[0] - ring[-1]))) < RUN_BREAK_M:
        runs[0] = runs.pop() + runs[0]
    return runs


def _smooth(z, n):
    if len(z) < 3 or n < 3:
        return z
    k = min(n, len(z) if len(z) % 2 else len(z) - 1)
    pad = k // 2
    zp = np.concatenate((np.full(pad, z[0]), z, np.full(pad, z[-1])))
    return np.convolve(zp, np.ones(k) / k, mode="valid")


def platform_height(pl, country, n_platforms):
    """The platform top's height over the rail head: the mapped one, else the country's norm."""
    h = pl.height_tag
    return h if h is not None else rs.norm(country, n_platforms)[1]


def _resample(xy, step):
    s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))))
    if s[-1] < 1e-6:
        return xy[:1]
    t = np.arange(0.0, s[-1], step)
    return np.c_[np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])]


def _ref_tracks(pl):
    """The tracks the platform's height follows: those its edge was put by, else the nearest one within REF_REACH_M."""
    used = sorted({int(k) for k in pl.tidx[pl.snapped]})
    if used:
        return [pl.tracks[k][1] for k in used]
    best = None
    for _tid, xy in pl.tracks:
        d = min(bs.dist_to_path(xy, p) for p in pl.ring[::max(1, len(pl.ring) // 12)])
        if d < REF_REACH_M and (best is None or d < best[0]):
            best = (d, xy)
    return [best[1]] if best else []


def reference(frame, elev, pl):
    """(xy (k, 2), z (k,)) the rail head's height along the track(s) the platform stands by: the drawn ground at points every
    REF_STEP_M of the track within REF_NEAR_M of the platform, smoothed along the track (the DEM's noise out of it; the
    ground under a track is graded to its bed, so it is the bed), plus the rail head over the bed. None without a track."""
    pts, zs = [], []
    ring = pl.ring[::max(1, len(pl.ring) // 160)]
    for xy in _ref_tracks(pl):
        q = _resample(np.asarray(xy, float), REF_STEP_M)
        d = np.sqrt(((q[:, None, :] - ring[None, :, :]) ** 2).sum(2)).min(1)
        keep = d < REF_NEAR_M
        if not keep.any():
            continue
        i = np.flatnonzero(keep)
        lo, hi = int(i[0]), int(i[-1])
        lo, hi = max(0, lo - SMOOTH_N), min(len(q) - 1, hi + SMOOTH_N)       # (a window's width past the platform's ends)
        sel = q[lo:hi + 1]
        z = _smooth(bm.ground(frame, elev, sel), SMOOTH_N)
        near = keep[lo:hi + 1]
        pts.append(sel[near])
        zs.append(z[near] + RAIL_TOP_M)
    if not pts:
        return None
    return np.vstack(pts), np.concatenate(zs)


def top_z(ref, xy, fallback):
    """Heights (k,) of the platform's top at xy: the reference points' heights weighed by 1 / (distance^2 + 1) over the
    nearest REF_K of them. Without a reference (fallback: xy -> z) the terrain's."""
    if ref is None:
        return fallback(xy)
    rp, rz = ref
    out = np.empty(len(xy))
    k = min(REF_K, len(rp))
    for a in range(0, len(xy), 2000):
        q = xy[a:a + 2000]
        d2 = ((q[:, None, :] - rp[None, :, :]) ** 2).sum(2)
        near = np.argpartition(d2, k - 1, axis=1)[:, :k] if k < d2.shape[1] else np.tile(np.arange(k), (len(q), 1))
        dd = np.take_along_axis(d2, near, 1)
        w = 1.0 / (dd + 1.0)
        out[a:a + 2000] = (w * rz[near]).sum(1) / w.sum(1)
    return out


def _slab(mesh, frame, elev, ref, ring, height):
    """The slab of a platform: its top over the ring's points (every bm.SLAB_STEP_M) at the reference height + height, so
    that it runs straight between the two edges of an island and follows each of them along; the kerb wall from below the
    ground to the top. (Not subdivided: the ear-clipped fan of thin triangles became 400 000 points a platform.)"""
    dense = bm._densify(ring, bm.SLAB_STEP_M)
    T = bm._triangulate(dense)
    if not len(T):
        return None
    fb = lambda xy: bm.ground(frame, elev, xy) + RAIL_TOP_M
    zt = top_z(ref, dense, fb) + height
    mesh.add(CONCRETE[0], np.column_stack((dense, zt)), dense / CONCRETE[1], T)
    zd = bm.ground(frame, elev, dense)
    bm.walls(mesh, KERB[0], KERB[1], dense, np.minimum(zd, zt - 0.3) - bm.SLAB_FOOT_M, zt)
    return dense, zt


def _strips(mesh, ref, pl, height, frame, elev):
    """The tactile strip and the safety line along each run of the track edge, on the platform's side."""
    fb = lambda xy: bm.ground(frame, elev, xy) + RAIL_TOP_M
    ring = pl.ring
    n = len(ring)
    for run in _runs(pl.snapped, ring):
        if len(run) < 3:
            continue
        pts = ring[run]
        # the ring is counter-clockwise: the platform lies to the left of its direction
        d = np.zeros_like(pts)
        d[1:-1] = pts[2:] - pts[:-2]
        d[0] = pts[1] - pts[0]
        d[-1] = pts[-1] - pts[-2]
        d /= np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
        nrm = np.c_[-d[:, 1], d[:, 0]]
        for off, w in ((STRIP_IN_M, STRIP_W_M), (LINE_IN_M, LINE_W_M)):
            a, b = pts + nrm * off, pts + nrm * (off + w)
            za = top_z(ref, a, fb) + height + LIFT_M
            zb = top_z(ref, b, fb) + height + LIFT_M
            P = np.vstack((np.column_stack((a, za)), np.column_stack((b, zb))))
            m = len(pts)
            T = []
            for i in range(m - 1):
                T += [[i, i + 1, m + i + 1], [i, m + i + 1, m + i]]
            s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))))
            UV = np.vstack((np.c_[s / LINE[1], np.zeros(m)], np.c_[s / LINE[1], np.ones(m)]))
            mesh.add(LINE[0], P, UV, T)


def surface(frame, elev, st, pl):
    """xy (k, 2) -> z (k,): the top of the platform pl (the same heights its slab is made with)."""
    h = platform_height(pl, st.country, len(st.platforms))
    ref = reference(frame, elev, pl)
    fb = lambda xy: bm.ground(frame, elev, xy) + RAIL_TOP_M
    return lambda xy: top_z(ref, np.asarray(xy, float).reshape(-1, 2), fb) + h


ROOF_H_M = 4.2              # a roof's underside over the platform: a platform canopy
ROOF_THICK_M = 0.30
ROOF_STEP_M = 12.0          # the roof's faces and walls are cut to this: every road tile has its share of them
ROOF_VERTS_MAX = 30000
COLUMN_TRACK_M = 0.9        # a column stands this far in from the platform's track edge at least
STEEL = bm.STEEL


def _roof_columns(st, ring, plats, surf):
    """[(x, y)] the columns of a roof: bm._column_points, only on a platform it covers and off the track edge."""
    edge = rs.norm(st.country)[0]
    out = []
    for p in bm._column_points(ring):
        if not any(bs.contains(pl.ring, p) for pl in plats):
            continue
        if min(bs.dist_to_path(xy, p) for _t, xy in st.tracks) < edge + COLUMN_TRACK_M:
            continue
        out.append(p)
    return out


def _roofs(mesh, frame, elev, st, box, columns_only=False):
    """The roofs over the platforms (RailStation.roofs): a flat slab ROOF_H_M over the platform's top, open on columns that
    stand on the platform (a grid and along the edge, bm._column_points)."""
    placed = []
    for rid, ring in st.roofs:
        ring = bm._ccw(ring)
        lo, hi = ring.min(0), ring.max(0)
        if hi[0] < box[0] - 15 or lo[0] > box[2] + 15 or hi[1] < box[1] - 15 or lo[1] > box[3] + 15:
            continue
        plats = [pl for pl in st.platforms if rid in pl.roofs]
        if not plats:
            continue
        surfs = [surface(frame, elev, st, pl) for pl in plats]
        probe = np.vstack((ring, bs.centroid(ring)[None]))
        zs = np.concatenate([f(probe) for f in surfs])
        ztop = float(np.median(zs)) + ROOF_H_M + ROOF_THICK_M              # (one height for the whole roof: every tile alike)
        zbot = ztop - ROOF_THICK_M
        step = ROOF_STEP_M
        mat, rep = STEEL
        if not columns_only:                # (the lean collider has the columns only)
            bm._face(mesh, mat, rep, ring, lambda P, z=ztop: np.full(len(P), z), step=step)
            bm._face(mesh, mat, rep, ring, lambda P, z=zbot: np.full(len(P), z), up=False, step=step)
            bm.walls(mesh, mat, rep, bm._densify(ring, step), zbot, ztop)
        pts = [q for q in _roof_columns(st, ring, plats, surfs) if all(float(np.hypot(*(q - o))) >= 1.5 for o in placed)]
        placed += pts
        for q in pts:
            zf = float(np.median([f(np.asarray(q)[None])[0] for f in surfs]))
            half = bm.COLUMN_M / 2
            sq = np.array([[q[0] - half, q[1] - half], [q[0] + half, q[1] - half],
                           [q[0] + half, q[1] + half], [q[0] - half, q[1] + half]])
            bm.walls(mesh, mat, rep, sq, zf - 0.05, zbot)


LINK_MARGIN_M = 40.0        # a footbridge's flights may run this far past its way's ends


def _link_near(ln, box, margin=LINK_MARGIN_M):
    pts = np.vstack([np.asarray(ln.xy, float)] + [np.asarray(s, float) for s in ln.steps])
    lo, hi = pts.min(0), pts.max(0)
    return not (hi[0] < box[0] - margin or lo[0] > box[2] + margin or hi[1] < box[1] - margin or lo[1] > box[3] + margin)


def _near(pl, box, margin=TILE_MARGIN_M):
    lo, hi = pl.ring.min(0), pl.ring.max(0)
    return not (hi[0] < box[0] - margin or lo[0] > box[2] + margin or hi[1] < box[1] - margin or lo[1] > box[3] + margin)


def build(frame, box, stations, elev, mesh, tactile=True, collide=False):
    """The platforms (and, drawn, the roofs) of stations that reach the tile box into mesh (a busstationmesh._Mesh);
    collide: for the lean collider, the slabs and the roofs' columns."""
    for st in stations:
        for pl in st.platforms:
            if not _near(pl, box) or len(pl.ring) < 3:
                continue
            h = platform_height(pl, st.country, len(st.platforms))
            ref = reference(frame, elev, pl)
            _slab(mesh, frame, elev, ref, pl.ring, h)
            if tactile and ref is not None:
                _strips(mesh, ref, pl, h, frame, elev)
        _roofs(mesh, frame, elev, st, box, columns_only=collide)
        for ln in st.links:
            if ln.kind == "over" and _link_near(ln, box):
                railbridges.add(mesh, frame, elev, st, ln, collide=collide)


def mesh(frame, box, stations, elev, origin, overlap=False):
    """The station meshes of the tile box (x0, y0, x1, y1): (V, T, UV, mats, idx) in the road mesh's form, or None."""
    m = bm._Mesh()
    build(frame, box, stations, elev, m)
    return m.result(box, origin, overlap)


def collision(frame, box, stations, elev, origin):
    """(mesh, boxes) the platforms' lean collision of the tile box: the slabs' tops and kerb walls, as busstationmesh's, and
    their 20 m boxes [x0, y0, z0, x1, y1, z1] (frame metres); (None, []) without any."""
    m = bm._Mesh()
    build(frame, box, stations, elev, m, tactile=False, collide=True)
    got = m.result(box, origin)
    if got is None:
        return None, []
    V = np.asarray(got[0], float) + np.asarray(origin, float)
    boxes = []
    keys = np.floor(V[:, :2] / 20.0).astype(np.int64)
    for k in np.unique(keys, axis=0):
        sel = V[(keys[:, 0] == k[0]) & (keys[:, 1] == k[1])]
        boxes.append([round(float(sel[:, 0].min() - 0.5), 2), round(float(sel[:, 1].min() - 0.5), 2),
                      round(float(sel[:, 2].min() - 0.5), 2), round(float(sel[:, 0].max() + 0.5), 2),
                      round(float(sel[:, 1].max() + 0.5), 2), round(float(sel[:, 2].max() + 0.5), 2)])
    return got, boxes
