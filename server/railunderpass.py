"""Walkable underpasses below the tracks of a railway station (task 037, stage E): railstations.Link kind "under".

A tunnel along the mapped way: a floor, two walls, a ceiling slab whose top lies just under the rails' bed (the ballast
rests on it), lights; at each end a flight of stairs rises to the ground between two retaining walls, with a paved rim and a
handrail round the pit. The terrain over all of it is cut away (terrain holes, hole_mask: the same Rust kernel the road
tunnels' portals use), so the tunnel and the pits are open where the ground was; the lid and the rims cover the hole's
squares (1 m: the cut is by the engine's squares) that the structure itself does not reach.

    levels(frame, elev, st, link)  -> (floor z, lid top z)
    add(mesh, frame, elev, st, link, collide)         into a busstationmesh._Mesh
    hole_parts(frame, elev, st, link) / hole_mask(parts, u, cx, cy, square)   the terrain's holes
    holes(md, frame, keep, cx, cy, square, elev)      the mask of a terrain block (None without an underpass)

Pure given the ground function: the same on every tile, process and peer.
"""
import numpy as np

import busstationmesh as bm
import busstations as bs
import railbridges as rb

CLEAR_M = 2.6               # the passage's height under the ceiling
CEIL_D = 0.5                # the ceiling slab's thickness
SINK_M = 0.10               # the lid's top under the bed line (the ballast rests on it)
WALL_T = 0.35
WIDTH_M = 3.0
WIDTH_RANGE_M = (2.0, 8.0)
LID_EXTRA_M = 1.6           # the lid reaches this far past the walls: it covers the terrain hole's squares
RIM_M = 1.6                 # the paved rim round a pit, past its wall
RIM_LIFT_M = 0.02           # over the ground (no z-fighting with the terrain)
LIGHT_STEP_M = 6.0
LIGHT = bm.LIGHT
HOLE_TH_M = 7.0             # the ground this high over the floor is cut (the lid's 3.1 m and the bank over a pit's stairs)
HOLE_SIDE_M = 0.3           # the hole reaches this far past the walls (and the kernel adds its margin and a square)
CONCRETE = bm.PLATFORM_TOP
KERB = bm.KERB


def width_of(link):
    w = link.width
    return w if w and WIDTH_RANGE_M[0] <= w <= WIDTH_RANGE_M[1] else WIDTH_M


def levels(frame, elev, st, link):
    """(floor z, lid top z): the lid's top SINK_M under the lowest bed the link crosses, the floor CEIL_D + CLEAR_M under that."""
    path = np.asarray(link.xy, float)
    s = rb._arc(path)
    pts = [[np.interp(a, s, path[:, 0]), np.interp(a, s, path[:, 1])] for _k, a in link.crosses] or [path[len(path) // 2]]
    g = float(bm.ground(frame, elev, np.asarray(pts)).min())
    lid = g - SINK_M
    return lid - CEIL_D - CLEAR_M, lid


def flights(link, path):
    """{0 / -1: [points]} the stairs at the passage's ends, from the end outwards (railbridges._end_flights), each starting
    at the end of the passage itself."""
    out = {}
    for e, fl in rb._end_flights(link, path).items():
        end = path[0 if e == 0 else -1]
        if float(np.hypot(*(fl[0] - end))) > 0.05:
            fl = np.vstack((end[None], fl))
        out[e] = np.asarray(fl, float)
    return out


def _ceiling_and_floor(mesh, path, zf, zlid, w):
    t, n = rb._normals(path)
    L, R = path + n * w / 2.0, path - n * w / 2.0
    s = rb._arc(path)
    zc = zlid - CEIL_D
    rep = CONCRETE[1]
    for i in range(len(path) - 1):
        j = i + 1
        uv = [(s[i] / rep, 0.0), (s[j] / rep, 0.0), (s[j] / rep, w / rep), (s[i] / rep, w / rep)]
        rb._quad(mesh, CONCRETE[0], [(*R[i], zf), (*R[j], zf), (*L[j], zf), (*L[i], zf)], uv)           # floor, up
        rb._quad(mesh, CONCRETE[0], [(*R[i], zc), (*L[i], zc), (*L[j], zc), (*R[j], zc)], uv)           # ceiling, down
        h = zc - zf
        wuv = [(s[i] / rep, 0.0), (s[j] / rep, 0.0), (s[j] / rep, h / rep), (s[i] / rep, h / rep)]
        rb._quad(mesh, KERB[0], [(*R[j], zf), (*R[i], zf), (*R[i], zc), (*R[j], zc)], wuv)               # the right wall, facing in
        rb._quad(mesh, KERB[0], [(*L[i], zf), (*L[j], zf), (*L[j], zc), (*L[i], zc)], wuv)               # the left wall, facing in
    # the lid: a slab over the passage, wider than it (it covers the terrain hole's squares)
    rb._strip(mesh, path, zlid, w + 2 * WALL_T + 2 * LID_EXTRA_M, CEIL_D, CONCRETE, KERB)


def _lights(mesh, path, zlid, w):
    s = rb._arc(path)
    zc = zlid - CEIL_D - 0.02
    t, n = rb._normals(path)
    for at in np.arange(LIGHT_STEP_M / 2.0, s[-1], LIGHT_STEP_M):
        p = np.array([np.interp(at, s, path[:, 0]), np.interp(at, s, path[:, 1])])
        i = min(int(np.searchsorted(s, at)), len(path) - 1)
        u, nn = t[i], n[i]
        rect = np.array([p - u * 0.15 - nn * 0.5, p + u * 0.15 - nn * 0.5, p + u * 0.15 + nn * 0.5, p - u * 0.15 + nn * 0.5])
        bm.top_face(mesh, LIGHT[0], LIGHT[1], rect, zc, up=False)


def _pit(mesh, frame, elev, fl, zf, w, collide):
    """A flight from the passage's end at zf up to the ground, between retaining walls, with the rim and the handrail."""
    g_end = float(bm.ground(frame, elev, fl[-1:])[0])
    got = rb._stair(mesh, frame, elev, fl[0], fl, zf, w, collide, z_end=g_end, fill_to=zf - 0.3)
    if got is None:
        return
    line = rb.resample(got, 1.0)
    s = rb._arc(line)
    t, n = rb._normals(line)
    zt = zf + (g_end - zf) * s / max(1e-6, s[-1])                   # the flight's slope (the treads' mean)
    zg = np.maximum(bm.ground(frame, elev, line), zt)
    for side in (1.0, -1.0):
        e = line + n * side * (w / 2.0)
        z0, z1 = zt - 0.3, zg
        for i in range(len(line) - 1):
            j = i + 1
            P = [(*e[i], z0[i]), (*e[j], z0[j]), (*e[j], z1[j]), (*e[i], z1[i])]
            P = P if side > 0 else [P[1], P[0], P[3], P[2]]       # (facing in, towards the stairs)
            rb._quad(mesh, KERB[0], P, [(s[i], z0[i]), (s[j], z0[j]), (s[j], z1[j]), (s[i], z1[i])])
        # the paved rim past the wall, flat at the ground (+ a hair)
        a, b = e, e + n * side * RIM_M
        for i in range(len(line) - 1):
            j = i + 1
            P = [(*a[i], zg[i] + RIM_LIFT_M), (*a[j], zg[j] + RIM_LIFT_M), (*b[j], zg[j] + RIM_LIFT_M), (*b[i], zg[i] + RIM_LIFT_M)]
            P = P if side > 0 else [P[0], P[3], P[2], P[1]]       # (facing up)
            rb._quad(mesh, CONCRETE[0], P, [(s[i], 0.0), (s[j], 0.0), (s[j], RIM_M), (s[i], RIM_M)])
    # the end of the pit, across the stairs' top, and the rim there
    u = t[-1]
    nn = n[-1]
    p = line[-1]
    q0, q1 = p - nn * (w / 2 + RIM_M), p + nn * (w / 2 + RIM_M)
    far = np.array([q0, q1, q1 + u * RIM_M, q0 + u * RIM_M])
    zr = float(zg[-1]) + RIM_LIFT_M
    bm.top_face(mesh, CONCRETE[0], CONCRETE[1], far, zr)
    if not collide:
        rb._rail(mesh, line, zg, w + 0.1)


def add(mesh, frame, elev, st, link, collide=False):
    """The underpass link (kind "under") into mesh: the passage, its lid, lights, and the two pits."""
    path = rb.resample(link.xy)
    if len(path) < 2:
        return
    zf, zlid = levels(frame, elev, st, link)
    w = width_of(link)
    _ceiling_and_floor(mesh, path, zf, zlid, w)
    if not collide:
        _lights(mesh, path, zlid, w)
    for _e, fl in flights(link, path).items():
        _pit(mesh, frame, elev, fl, zf, w, collide)


def hole_parts(frame, elev, st, link):
    """[(xy (k, 2), floor z (k,), half width)] the footprint of the passage and of its pits, for the terrain's holes."""
    path = rb.resample(link.xy)
    if len(path) < 2:
        return []
    zf, _lid = levels(frame, elev, st, link)
    w = width_of(link)
    hw = w / 2.0 + WALL_T + HOLE_SIDE_M
    out = [(path, np.full(len(path), zf), hw)]
    for _e, fl in flights(link, path).items():
        g_end = float(bm.ground(frame, elev, fl[-1:])[0])
        line = rb.resample(fl, 1.0)
        s = rb._arc(line)
        out.append((line, zf + (g_end - zf) * s / max(1e-6, s[-1]), hw))
    return out


def _reaches(link, box, square):
    """Can the underpass link's holes (hole_parts: its passage and its pits' flights, half width plus the wall and side,
    the kernel's squares reaching in from beyond) fall in box (x0, y0, x1, y1)? From its plan alone: its levels (the
    graded ground at its crossings and pit tops) are not asked for a link that cannot (a station within reach of a
    block reaches cells the block never plans: Dijon's ~4.6 s of a warm block's 15 s planning them, 2026-11-11)."""
    path = rb.resample(link.xy)
    if len(path) < 2:
        return False
    import roads as roadsmod
    pts = np.vstack([path] + list(flights(link, path).values()))
    pad = width_of(link) / 2.0 + WALL_T + HOLE_SIDE_M + roadsmod.TUNNEL_HOLE_MARGIN + 3.0 * square
    lo, hi = pts.min(axis=0) - pad, pts.max(axis=0) + pad
    return not (hi[0] < box[0] or lo[0] > box[2] or hi[1] < box[1] or lo[1] > box[3])


def hole_mask(parts, u, cx, cy, square):
    """Where the terrain block (heights u [iy, ix], roads.tunnel_hole_mask's grid) is a hole for the underpasses `parts`."""
    import fast
    import roads as roadsmod
    n = u.shape[0]
    half = n * square / 2
    mask = np.zeros(u.shape, bool)
    if not parts:
        return mask
    fast.require(88, "tunnel_holes")
    u = np.ascontiguousarray(u, np.float64)
    for xy, z, hw in parts:
        fast._ef.tunnel_holes(mask, u, float(cx - half), float(cy - half), float(square), np.ascontiguousarray(xy, np.float64),
                              np.ascontiguousarray(z, np.float64), float(hw), float(HOLE_TH_M), False, True, True,
                              float(roadsmod.TUNNEL_HOLE_MARGIN), 0.0, 0.0, True)
    return mask


def holes(md, frame, keep, cx, cy, square, elev):
    """The terrain block's hole mask for the underpasses of the railway stations in it, or None. keep: heightmap's
    (lat, lon, u); the ground of the floors by elev (elev(lat, lon) -> height). Errors cost the holes, never the block."""
    import logging
    import railstations
    if not railstations.ON or md is None:
        return None
    try:
        u = keep["u"]
        half = u.shape[0] * square / 2
        box = (cx - half, cy - half, cx + half, cy + half)
        parts = []
        for st in railstations.stations_near(md, frame, box):
            for ln in st.links:
                if ln.kind == "under" and _reaches(ln, box, square):
                    parts += hole_parts(frame, elev, st, ln)
        if not parts:
            return None
        mask = hole_mask(parts, u, cx, cy, square)
        return mask if mask.any() else None
    except Exception:
        logging.getLogger(__name__).exception("underpass holes of the block at (%s, %s)", cx, cy)
        return None
