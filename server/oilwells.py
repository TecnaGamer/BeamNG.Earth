"""Oil wells for the pump jacks (the mod's pumpjacks.lua; the user, 2026-10-26: matching props for the pump jack sound):
OpenStreetMap's man_made=petroleum_well / pumping_rig points, built once by tools/build_oilwells.py into oil_wells.npz
beside the modules (ODbL, (c) OpenStreetMap contributors; Overture has no well class). A well gets a pump jack unless
it is a gas well (substance=gas) or tagged pump=no; closed ones (an end_date, disused / abandoned) are left out of the
file. Rows per road tile [x, y, z, yaw] in the anchor frame (z: the ground; yaw degrees counter-clockwise from +x,
hashed from the position)."""
import os
import threading

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oil_wells.npz")
GAS, NO_PUMP = 1, 2         # flags (tools/build_oilwells.py)
SCALE = 1e6                 # lat / lon stored as int32 millionths of a degree, sorted by lat

_data = None
_lock = threading.Lock()


def _load():
    global _data
    with _lock:
        if _data is None:
            if not os.path.exists(PATH):
                _data = (np.zeros(0, np.int32), np.zeros(0, np.int32), np.zeros(0, np.uint8))
            else:
                with np.load(PATH, allow_pickle=False) as f:
                    _data = (f["lat"], f["lon"], f["flags"])
        return _data


def near(la0, lo0, la1, lo1):
    """(lat, lon) degrees of the pump jack wells in the box."""
    lat, lon, flags = _load()
    i0, i1 = np.searchsorted(lat, [int(la0 * SCALE), int(la1 * SCALE) + 1])
    a, o, fl = lat[i0:i1], lon[i0:i1], flags[i0:i1]
    sel = (o >= int(lo0 * SCALE)) & (o <= int(lo1 * SCALE)) & ((fl & (GAS | NO_PUMP)) == 0)
    return a[sel] / SCALE, o[sel] / SCALE


def tile_wells(frame, tx, ty, elev):
    """The rows of road tile (tx, ty)'s pump jacks; elev(lat, lon): the ground (the tile's graded one)."""
    import buildings
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1], np.float64), np.array([y0, y0, y1, y1], np.float64),
                                      np.zeros(4))
    wla, wlo = near(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
    if len(wla) == 0:
        return []
    e, n, _ = frame.geodetic_to_enu(wla, wlo, np.zeros(len(wla)))
    e, n = np.asarray(e), np.asarray(n)
    keep = (e >= x0) & (e < x1) & (n >= y0) & (n < y1)
    if not keep.any():
        return []
    wla, wlo, e, n = wla[keep], wlo[keep], e[keep], n[keep]
    _, _, z = frame.geodetic_to_enu(wla, wlo, np.asarray(elev(wla, wlo), np.float64))
    out = []
    for a, b, c, la_, lo_ in zip(e, n, np.asarray(z), wla, wlo):
        yaw = buildings._hash01(int(round(la_ * SCALE)), int(round(lo_ * SCALE)), 7) * 360.0
        out.append([round(float(a), 2), round(float(b), 2), round(float(c), 2), round(float(yaw), 1)])
    return out
