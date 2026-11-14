"""Footbridges over the tracks of a railway station (task 037, stage D): railstations.Link kind "over".

The deck runs level along the mapped way at a height that clears the highest rail head it crosses (CLEAR_M under its
slab); each end climbs down by stairs (the mapped `steps` ways; where the map has none, a flight along the way's own
direction), standing on the ground; piers stand where no track and no ballast is; handrails along the deck and the
stairs. Built into a busstationmesh._Mesh like the platforms (railstationmesh.build): the same slab on every tile, the
tile's own triangles taken by the caller. The lean collider (collide=True) has the deck, the stairs and the piers.

All pure given the ground function: no randomness.
"""
import math

import numpy as np
import dnp

import busstationmesh as bm
import busstations as bs

RAIL_TOP_M = 0.58           # the rail head over the bed (railstationmesh.RAIL_TOP_M)
CLEAR_M = 5.8               # the deck's underside over the highest rail head it crosses (a catenary 5.5 m, wires under it)
DECK_D = 0.45               # m of slab under the walking surface
WIDTH_M = 2.5               # the deck's width when the map has none; the stairs' width the same
WIDTH_RANGE_M = (1.2, 6.0)
STEP_M = 2.0                # the deck's cross-sections
RISE_M, RUN_M = 0.17, 0.30  # a stair's riser and tread
STAIR_MAX = 70              # steps in a flight, at most
STAIR_MIN_H_M = 0.4         # a flight shorter than this in height is not built
END_JOIN_M = 3.0            # a steps way this near the deck's end belongs to it
HAND_H_M = 1.05             # handrail
POST_STEP_M = 2.5
PIER_STEP_M = 22.0
PIER_TRACK_M = 3.6          # a pier stands this far from every track's axis at least (ballast and the gauge)
PIER_W_M = 0.9              # along the deck
CONCRETE = bm.PLATFORM_TOP
KERB = bm.KERB
STEEL = bm.STEEL


def _arc(xy):
    return np.concatenate(([0.0], np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))))


def resample(xy, step=STEP_M):
    """The polyline every `step` m from its start, its end kept."""
    xy = np.asarray(xy, float)
    s = _arc(xy)
    if s[-1] < 1e-6:
        return xy[:1]
    t = np.append(np.arange(0.0, s[-1], step), s[-1])
    return np.c_[np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])]


def _normals(line):
    t = np.zeros_like(line)
    t[1:-1] = line[2:] - line[:-2]
    t[0] = line[1] - line[0]
    t[-1] = line[-1] - line[-2]
    t /= np.maximum(dnp.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    return t, np.c_[-t[:, 1], t[:, 0]]


def _quad(mesh, mat, P, UV):
    mesh.add(mat, P, UV, [[0, 1, 2], [0, 2, 3]])


def width_of(link):
    w = link.width
    return w if w and WIDTH_RANGE_M[0] <= w <= WIDTH_RANGE_M[1] else WIDTH_M


def deck_height(frame, elev, st, link):
    """The deck's walking surface (frame z): the highest rail head at the link's crossings plus CLEAR_M and the slab."""
    path = np.asarray(link.xy, float)
    s = _arc(path)
    pts = []
    for _k, arc in link.crosses:
        pts.append([np.interp(arc, s, path[:, 0]), np.interp(arc, s, path[:, 1])])
    if not pts:
        pts = [path[len(path) // 2]]
    return float(bm.ground(frame, elev, np.asarray(pts)).max()) + RAIL_TOP_M + CLEAR_M + DECK_D


def _strip(mesh, line, z, w, depth, mat_top, mat_side, mat_under=None):
    """A slab along `line` (k, 2) at walking height z (scalar or (k,)), w wide, depth thick: top, underside and both sides."""
    t, n = _normals(line)
    z = np.broadcast_to(np.asarray(z, float), (len(line),))
    L, R = line + n * w / 2.0, line - n * w / 2.0
    s = _arc(line)
    for i in range(len(line) - 1):
        j = i + 1
        top = [(*R[i], z[i]), (*R[j], z[j]), (*L[j], z[j]), (*L[i], z[i])]
        uv = [(s[i] / mat_top[1], 0.0), (s[j] / mat_top[1], 0.0), (s[j] / mat_top[1], w / mat_top[1]),
              (s[i] / mat_top[1], w / mat_top[1])]
        _quad(mesh, mat_top[0], top, uv)
        under = [(*R[i], z[i] - depth), (*L[i], z[i] - depth), (*L[j], z[j] - depth), (*R[j], z[j] - depth)]
        _quad(mesh, mat_side[0], under, uv)
        rs_ = [(*R[i], z[i] - depth), (*R[j], z[j] - depth), (*R[j], z[j]), (*R[i], z[i])]
        ls_ = [(*L[j], z[j] - depth), (*L[i], z[i] - depth), (*L[i], z[i]), (*L[j], z[j])]
        side_uv = [(s[i] / mat_side[1], 0.0), (s[j] / mat_side[1], 0.0), (s[j] / mat_side[1], depth / mat_side[1]),
                   (s[i] / mat_side[1], depth / mat_side[1])]
        _quad(mesh, mat_side[0], rs_, side_uv)
        _quad(mesh, mat_side[0], ls_, side_uv)
    # the two ends
    for a, sign in ((0, -1), (len(line) - 1, 1)):
        c = [(*R[a], z[a] - depth), (*L[a], z[a] - depth), (*L[a], z[a]), (*R[a], z[a])]
        _quad(mesh, mat_side[0], c if sign > 0 else c[::-1], [(0, 0), (w, 0), (w, depth), (0, depth)])


def _post(mesh, p, z0, z1, half=0.035):
    sq = np.array([[p[0] - half, p[1] - half], [p[0] + half, p[1] - half], [p[0] + half, p[1] + half],
                   [p[0] - half, p[1] + half]])
    bm.walls(mesh, STEEL[0], STEEL[1], sq, z0, z1)


def _rail(mesh, line, z, w, ends=(True, True)):
    """Handrails (a top bar, a mid bar and posts) along both edges of a deck or a stair at walking height z."""
    t, n = _normals(line)
    z = np.broadcast_to(np.asarray(z, float), (len(line),))
    s = _arc(line)
    for side in (1.0, -1.0):
        e = line + n * side * (w / 2.0 - 0.05)
        for lo, hi in ((HAND_H_M - 0.07, HAND_H_M), (0.45, 0.52)):
            ring = np.vstack((e + n * 0.035, (e - n * 0.035)[::-1]))
            zr = np.concatenate((z, z[::-1]))
            bm.walls(mesh, STEEL[0], STEEL[1], ring, zr + lo, zr + hi)
        if len(line) >= 2:
            for at in np.arange(0.0, s[-1] + 1e-6, POST_STEP_M):
                p = np.array([np.interp(at, s, e[:, 0]), np.interp(at, s, e[:, 1])])
                _post(mesh, p, float(np.interp(at, s, z)), float(np.interp(at, s, z)) + HAND_H_M)


def _stair(mesh, frame, elev, top_xy, dirs, z_top, w, collide=False, z_end=None, fill_to=None):
    """A flight of RISE_M / RUN_M steps along the polyline `dirs` (points; the first at height z_top, the deck's end or a
    tunnel's), the last tread at z_end (default: the ground there). Down from a deck or up from a tunnel's floor alike.
    fill_to: the boxes go down to it (a pit's stair is solid to the floor), else to 0.3 m under the lower of the tread and
    the ground. Returns the flight's path or None."""
    pts = np.asarray(dirs, float)
    g_end = float(bm.ground(frame, elev, pts[-1:])[0]) if z_end is None else float(z_end)
    for _ in range(3):
        H = abs(z_top - g_end)
        if H < STAIR_MIN_H_M:
            return None
        n = min(STAIR_MAX, int(math.ceil(H / RISE_M)))
        need = n * RUN_M
        s = _arc(pts)
        if s[-1] < need - 0.2:              # a mapped flight shorter than its steps need: carried on along its last leg
            d = pts[-1] - pts[-2]
            dl = float(dnp.hypot(*d))
            if dl > 1e-6:
                pts = np.vstack((pts, pts[-1] + d / dl * (need - s[-1])))
                if z_end is not None:
                    break
                g_new = float(bm.ground(frame, elev, pts[-1:])[0])
                if abs(g_new - g_end) < 0.1:
                    break
                g_end = g_new
                continue
        break
    rise = g_end - z_top
    n = min(STAIR_MAX, int(math.ceil(abs(rise) / RISE_M)))
    s = _arc(pts)
    tread = s[-1] / n
    line = resample(pts, tread / 2.0)
    sl = _arc(line)
    for i in range(n):
        a, b = i * tread, (i + 1) * tread
        za = z_top + (i + 1) * rise / n                              # the tread's height: the step i away from the start
        pa = np.array([np.interp(a, sl, line[:, 0]), np.interp(a, sl, line[:, 1])])
        pb = np.array([np.interp(b, sl, line[:, 0]), np.interp(b, sl, line[:, 1])])
        pm = (pa + pb) / 2.0
        d = pb - pa
        dl = float(dnp.hypot(*d))
        if dl < 1e-6:
            continue
        u = d / dl
        nn = np.array([-u[1], u[0]])
        rect = np.array([pa - nn * w / 2, pb - nn * w / 2, pb + nn * w / 2, pa + nn * w / 2])
        zg = float(bm.ground(frame, elev, pm[None])[0])
        z0 = (min(zg, za) - 0.3) if fill_to is None else min(fill_to, za - 0.3)
        bm.walls(mesh, CONCRETE[0], CONCRETE[1], rect, z0, za)
        bm.top_face(mesh, CONCRETE[0], CONCRETE[1], rect, za)
    return pts


def _piers(mesh, frame, elev, st, path, z_deck, w, s_from, s_to):
    """Piers under the deck between its ends where no track stands: every PIER_STEP_M, moved along the path to the nearest
    clear point."""
    s = _arc(path)
    placed = []
    at = s_from + PIER_STEP_M / 2.0
    while at < s_to:
        best = None
        for d in np.arange(0.0, PIER_STEP_M / 2.0, 1.0):
            for sg in (0.0, -1.0, 1.0) if d == 0 else (-1.0, 1.0):
                a = at + sg * d
                if a < s_from + 2.0 or a > s_to - 2.0:
                    continue
                p = np.array([np.interp(a, s, path[:, 0]), np.interp(a, s, path[:, 1])])
                if min(bs.dist_to_path(xy, p) for _t, xy in st.tracks) >= PIER_TRACK_M:
                    best = (a, p)
                    break
            if best:
                break
        if best:
            placed.append(best)
        at += PIER_STEP_M
    for a, p in placed:
        i = min(int(np.searchsorted(s, a)), len(path) - 1)
        d = path[min(len(path) - 1, i)] - path[max(0, i - 1)]
        dl = float(dnp.hypot(*d))
        u = d / dl if dl > 1e-6 else np.array([1.0, 0.0])
        nn = np.array([-u[1], u[0]])
        hw, hl = (w * 0.45), PIER_W_M / 2.0
        rect = np.array([p - nn * hw - u * hl, p + nn * hw - u * hl, p + nn * hw + u * hl, p - nn * hw + u * hl])
        rect = rect[::-1] if bm._signed(rect) < 0 else rect
        zg = float(bm.ground(frame, elev, p[None])[0])
        bm.walls(mesh, CONCRETE[0], CONCRETE[1], rect, zg - 0.5, z_deck - DECK_D)
        bm.top_face(mesh, CONCRETE[0], CONCRETE[1], rect, z_deck - DECK_D, up=False)


def _end_flights(link, path):
    """[(deck end index 0 / -1, [flight polyline from the deck end outwards])]: the mapped steps ways joined to an end of the
    deck, each oriented away from the deck; an end with none gets a flight along the path's own direction."""
    ends = {0: path[0], -1: path[-1]}
    out = {}
    for stp in link.steps:
        stp = np.asarray(stp, float)
        for e, p in ends.items():
            d0, d1 = float(dnp.hypot(*(stp[0] - p))), float(dnp.hypot(*(stp[-1] - p)))
            if min(d0, d1) <= END_JOIN_M and e not in out:
                out[e] = stp if d0 <= d1 else stp[::-1]
                break
    for e, p in ends.items():
        if e in out:
            continue
        q = path[1] if e == 0 else path[-2]
        d = p - q
        dl = float(dnp.hypot(*d))
        if dl > 1e-6:
            out[e] = np.array([p, p + d / dl * 3.0])
    return out


def add(mesh, frame, elev, st, link, collide=False):
    """The footbridge link (kind "over") into mesh: deck, handrails (not in the collider), piers and the flights."""
    path = resample(link.xy)
    if len(path) < 2:
        return
    z = deck_height(frame, elev, st, link)
    w = width_of(link)
    _strip(mesh, path, z, w, DECK_D, CONCRETE, KERB)
    if not collide:
        _rail(mesh, path, z, w)
    s = _arc(path)
    if link.crosses:
        arcs = [a for _k, a in link.crosses]
        _piers(mesh, frame, elev, st, path, z, w, max(0.0, min(arcs) - 4.0), min(float(s[-1]), max(arcs) + 4.0))
    for e, fl in _end_flights(link, path).items():
        pts = np.vstack((path[0 if e == 0 else -1][None], fl[1:])) if dnp.hypot(*(fl[0] - path[0 if e == 0 else -1])) > 0.05 else fl
        got = _stair(mesh, frame, elev, pts[0], pts, z, w, collide)
        if got is not None and not collide:
            tread_line = resample(got, 1.0)
            zz = z - (z - float(bm.ground(frame, elev, got[-1:])[0])) * _arc(tread_line) / max(1e-6, _arc(tread_line)[-1])
            _rail(mesh, tread_line, zz, w)
