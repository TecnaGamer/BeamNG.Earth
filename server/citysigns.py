"""Town direction boards on the poles that already stand (the user, 2026-11-07: "in a town they hang on electricity poles,
there are far fewer, and the posts we have now do not fit").

A real town board is small and hangs by two clamps on a lamp or utility pole at the roadside. dirsign_place finds the
junction and the approach (the kernel's spot, a little beyond a sidewalk's width, clear of the carriageways); this module
moves such a board onto the nearest suitable pole of the road tiles (their `props`: streetlight, pole, hvpole, the
tram and regional poles), on the driving side, a few tens of metres either way along the approach and well short of the
junction. No pole near: the board keeps the kernel's spot on a stand of its own. The mesh is dirsign_mesh (mode 0 / 1).

The poles are read from the road tiles' JSON beside the sign tile (roads_<anchor>_<tx>_<ty>.json, the tile and its eight
neighbours as far as they are written: a board near the tile's edge may meet a pole of the next one).
"""
import json
import os

import numpy as np
import dnp

MOUNT_EXTRA = ("streetlight", "eu_lamp_steel", "eu_lamp_concrete", "catmast", "catmast_eu")
SHIFT_M = 40.0              # m either way along the approach from the kernel's spot a pole may be
LATERAL_M = (2.0, 14.0)     # m from the approach's road centre: a roadside pole, not a median's or a far one
JUNCTION_CLEAR_M = 20.0     # m a board stays short of the junction it names
_CACHE = {}
_CACHE_MAX = 64


def mountable(group):
    """Does a props group carry a board: a lamp or utility pole (not the arm of one, a lens, a signal or a tower)."""
    return group in MOUNT_EXTRA or ("pole" in group and group != "polelamp")


def _poles_of(path):
    """(n, 3) [x, y, z] of the mountable poles of one road tile JSON, or None (not written yet). Kept by file stamp."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    got = _CACHE.get(key)
    if got is not None:
        return got
    try:
        with open(path, encoding="utf-8") as f:
            props = json.load(f).get("props") or {}
    except (OSError, ValueError):
        return None
    groups = props.get("groups") or []
    ok = {i for i, g in enumerate(groups) if mountable(str(g))}
    rows = [[r[1], r[2], r[3]] for r in props.get("items") or [] if int(r[0]) in ok]
    out = np.asarray(rows, np.float64).reshape(-1, 3)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def poles_near(out_dir, aid, tx, ty):
    """The mountable poles of road tile (tx, ty) and its neighbours that are written, (n, 3) sorted (x, y). (The anchor
    road tiles roads_<aid>_<tx>_<ty>.json: what the tests and tools/count_dirsigns.py build.)"""
    parts = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            got = _poles_of(os.path.join(out_dir, f"roads_{aid}_{tx + dx}_{ty + dy}.json"))
            if got is not None and len(got):
                parts.append(got)
    return _sorted(parts)


def _sorted(parts):
    if not parts:
        return np.zeros((0, 3))
    p = np.vstack(parts)
    return p[np.lexsort((p[:, 1], p[:, 0]))]


def _geo_poles_of(path, frame):
    """(n, 3) mountable poles of one road geotile JSON (rg_<key>.json; its rows are in its own frame, "frame" = [lat, lon])
    in `frame`'s metres, or None (not written yet)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size, frame.lat0, frame.lon0)
    got = _CACHE.get(key)
    if got is not None:
        return got
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    props = d.get("props") or {}
    groups = props.get("groups") or []
    ok = {i for i, g in enumerate(groups) if mountable(str(g))}
    rows = [[r[1], r[2], r[3]] for r in props.get("items") or [] if int(r[0]) in ok]
    out = np.zeros((0, 3))
    if rows and d.get("frame"):
        import geodesy
        src = geodesy.EnuFrame(float(d["frame"][0]), float(d["frame"][1]))
        a = np.asarray(rows, np.float64)
        la, lo, h = src.enu_to_geodetic(a[:, 0], a[:, 1], a[:, 2])
        x, y, z = frame.geodetic_to_enu(la, lo, h)
        out = np.column_stack([x, y, z]).astype(np.float64)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def geo_poles_near(out_dir, frame, box, margin=60.0):
    """The mountable poles of the road geotiles round box (x0, y0, x1, y1 of `frame`, plus margin m), in frame metres,
    sorted (x, y). The geotiles are the session's road tiles in the game (rg_g<z>_<x>_<y>.json beside the sign tiles)."""
    import geotiles
    x0, y0, x1, y1 = box
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - margin, x1 + margin]), np.array([y0 - margin, y1 + margin]), 0.0)
    gx0, gy1 = geotiles.tile_of(float(np.min(la)), float(np.min(lo)))
    gx1, gy0 = geotiles.tile_of(float(np.max(la)), float(np.max(lo)))
    parts = []
    for gx in range(min(gx0, gx1), max(gx0, gx1) + 1):
        for gy in range(min(gy0, gy1), max(gy0, gy1) + 1):
            got = _geo_poles_of(os.path.join(out_dir, f"rg_{geotiles.key(gx, gy)}.json"), frame)
            if got is not None and len(got):
                parts.append(got)
    return _sorted(parts)


def pick(poles, board, junction_xy, left_hand):
    """(x, y, z) of the pole a board (a dirsign_place row) hangs on, or None. board: [x, y, travel x, travel y, road x,
    road y, ...]; junction_xy: the junction it names (frame metres)."""
    if poles is None or not len(poles):
        return None
    side = -1.0 if left_hand else 1.0
    q, t, road = np.asarray(board[0:2], np.float64), np.asarray(board[2:4], np.float64), np.asarray(board[4:6], np.float64)
    t = t / max(float(dnp.hypot(*t)), 1e-9)
    right = np.array([t[1] * side, -t[0] * side])
    d = poles[:, :2] - road
    along, lat = dnp.matmul(d, t), dnp.matmul(d, right)
    latq = float(dnp.matmul(q - road, right))
    to_j = dnp.hypot(poles[:, 0] - junction_xy[0], poles[:, 1] - junction_xy[1])
    ok = (np.abs(along) <= SHIFT_M) & (lat >= LATERAL_M[0]) & (lat <= LATERAL_M[1]) & (to_j >= JUNCTION_CLEAR_M)
    if not ok.any():
        return None
    cost = np.where(ok, np.abs(along) + 2.0 * np.abs(lat - latq), np.inf)
    i = int(np.argmin(cost))          # (poles are sorted: the first of equal costs, the same in every tile)
    return float(poles[i, 0]), float(poles[i, 1]), float(poles[i, 2])
