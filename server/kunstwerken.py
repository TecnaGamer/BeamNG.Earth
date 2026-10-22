"""Hand-built structures ("kunstwerken": a junction's decks over a sunken road, its retaining walls), modelled in Blender
on the AHN and the BGT (tools/blender_ref.py, tools/kunstwerk_walls.py; design/kunstwerken/<name>/) and placed in the
road tiles instead of what the generator makes of them (--nl-bgt on). The generator's surfaces stay; a kunstwerk adds
its meshes to the tile's colliding mesh, gives the streets over its decks their decks' planes (not the lidar's surface,
cars and lamp posts on it: holes and spikes, 2026-10-21) and keeps the BGT's own walls out of its box.

    for kw in near(frame, box):                    # the kunstwerken over a road tile's box
        mesh = kw.tile_mesh(frame, box, origin)    # (positions, tris, uvs, materials, tri material index) or None
    deck_top(lat, lon)                              # heights of the kunstwerken's decks there, NaN elsewhere

A kunstwerk file (data/kunstwerken/<name>.kunstwerk.json): origin_rd, positions (k, 3) in RD metres from it and NAP,
tris, materials (the game's), tri_material, decks [{ring (k, 2) RD metres from the origin, plane [a, b, c]: z = a + b x
+ c y}].
"""
import glob
import json
import logging
import os
import threading

import numpy as np

log = logging.getLogger("earthd.kunstwerken")

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kunstwerken")
UV_M = 4.0                  # m per texture repeat on a kunstwerk's faces (concrete)
BOX_PAD_M = 2.0             # m round a kunstwerk's footprint its box reaches (the BGT's walls kept out of it)

_lock = threading.Lock()
_all = None


def _inside(ring, px, py):
    x, y = ring[:, 0], ring[:, 1]
    ins = np.zeros(len(px), bool)
    j = len(ring) - 1
    for i in range(len(ring)):
        c = ((y[i] > py) != (y[j] > py)) & (px < (x[j] - x[i]) * (py - y[i]) / (y[j] - y[i] + 1e-12) + x[i])
        ins ^= c
        j = i
    return ins


class Kunstwerk:
    def __init__(self, data):
        self.name = data.get("name", "?")
        self.origin = np.asarray(data["origin_rd"], np.float64)
        self.P = np.asarray(data["positions"], np.float64)
        self.T = np.asarray(data["tris"], np.int64)
        self.mats = list(data["materials"])
        self.tm = np.asarray(data["tri_material"], np.int64)
        self.decks = [(np.asarray(d["ring"], np.float64), np.asarray(d["plane"], np.float64)) for d in data.get("decks", ())]
        self.UV = np.asarray(data["uvs"], np.float64) if "uvs" in data else None
        fl = data.get("floor")
        if fl:
            self.floor = np.asarray(fl["z"], np.float64).reshape(fl["ny"], fl["nx"])
            self.floor[self.floor < -900] = np.nan
            self.fx0, self.fy0 = float(fl["x0"]), float(fl["y0"])
        else:
            self.floor = None
        self.lo = self.P[:, :2].min(0) - BOX_PAD_M
        self.hi = self.P[:, :2].max(0) + BOX_PAD_M
        self._enu = {}

    def _rd(self, lat, lon):
        import ahn
        x, y = ahn.wgs_to_rd(lat, lon)
        return np.asarray(x) - self.origin[0], np.asarray(y) - self.origin[1]

    def enu(self, frame):
        """Its positions in the frame (cached per frame)."""
        key = (round(frame.lat0, 9), round(frame.lon0, 9)) if hasattr(frame, "lat0") else id(frame)
        got = self._enu.get(key)
        if got is None:
            import ahn
            la, lo = ahn.rd_to_wgs(self.P[:, 0] + self.origin[0], self.P[:, 1] + self.origin[1])
            e, n, u = frame.geodetic_to_enu(la, lo, self.P[:, 2])
            got = self._enu[key] = np.c_[np.asarray(e), np.asarray(n), np.asarray(u)]
        return got

    def over(self, frame, box):
        """Does it reach into the box (x0, y0, x1, y1) in the frame?"""
        V = self.enu(frame)
        x0, y0, x1, y1 = box
        return bool(((V[:, 0] >= x0 - BOX_PAD_M) & (V[:, 0] <= x1 + BOX_PAD_M) & (V[:, 1] >= y0 - BOX_PAD_M)
                     & (V[:, 1] <= y1 + BOX_PAD_M)).any())

    def tile_mesh(self, frame, box, origin):
        """Its triangles whose centre lies in the box, as a mesh relative to origin, or None."""
        V = self.enu(frame)
        c = V[self.T].mean(1)
        x0, y0, x1, y1 = box
        keep = (c[:, 0] >= x0) & (c[:, 0] < x1) & (c[:, 1] >= y0) & (c[:, 1] < y1)
        if not keep.any():
            return None
        T = self.T[keep]
        used = np.unique(T)
        remap = np.full(len(V), -1, np.int64)
        remap[used] = np.arange(len(used))
        P = V[used]
        uv = self.UV[used] if self.UV is not None else np.c_[(P[:, 0] + P[:, 1]) / UV_M, P[:, 2] / UV_M]
        return (P - np.asarray(origin, np.float64), remap[T], uv, list(self.mats), self.tm[keep])

    def in_box(self, lat, lon):
        """Which points lie in its footprint's box."""
        x, y = self._rd(lat, lon)
        return (x >= self.lo[0]) & (x <= self.hi[0]) & (y >= self.lo[1]) & (y <= self.hi[1])

    def floor_at(self, lat, lon):
        """Its floor's height (the lowest road surface it draws) per point, NaN where it draws none."""
        x, y = self._rd(lat, lon)
        x, y = np.ravel(x), np.ravel(y)
        out = np.full(x.shape, np.nan)
        if self.floor is None:
            return out
        ny, nx = self.floor.shape
        i = np.floor(x - self.fx0).astype(np.int64)
        j = np.floor(y - self.fy0).astype(np.int64)
        k = (i >= 0) & (i < nx) & (j >= 0) & (j < ny)
        out[k] = self.floor[j[k], i[k]]
        return out

    def deck_top(self, lat, lon):
        x, y = self._rd(lat, lon)
        x, y = np.ravel(x), np.ravel(y)
        out = np.full(x.shape, np.nan)
        for ring, (a, b, c) in self.decks:
            lo_, hi_ = ring.min(0), ring.max(0)
            m = np.isnan(out) & (x >= lo_[0]) & (x <= hi_[0]) & (y >= lo_[1]) & (y <= hi_[1])
            if m.any():
                k = np.flatnonzero(m)
                k = k[_inside(ring, x[k], y[k])]
                out[k] = a + b * x[k] + c * y[k]
        return out


def all_kunstwerken():
    global _all
    with _lock:
        if _all is None:
            _all = []
            for path in sorted(glob.glob(os.path.join(DIR, "*.kunstwerk.json"))):
                try:
                    with open(path, encoding="utf-8") as f:
                        _all.append(Kunstwerk(json.load(f)))
                except (OSError, ValueError, KeyError) as e:
                    log.warning("kunstwerk %s: %s: %s", os.path.basename(path), type(e).__name__, e)
        return _all


def near(frame, box):
    """The kunstwerken reaching into the box (x0, y0, x1, y1) in the frame."""
    return [k for k in all_kunstwerken() if k.over(frame, box)]


def deck_top(lat, lon, fallback=None):
    """The kunstwerken's deck heights at the points, else fallback(lat, lon) (the AHN's deck surface), else NaN."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    shape = np.broadcast(lat, lon).shape
    la, lo = np.broadcast_to(lat, shape).ravel(), np.broadcast_to(lon, shape).ravel()
    out = np.full(la.shape, np.nan)
    for k in all_kunstwerken():
        b = k.in_box(la, lo)
        if b.any():
            got = k.deck_top(la[b], lo[b])
            idx = np.flatnonzero(b)
            fill = np.isnan(out[idx]) & np.isfinite(got)
            out[idx[fill]] = got[fill]
    if fallback is not None:
        rest = np.isnan(out)                    # (a deck the kunstwerk has not modelled: the lidar's, as elsewhere)
        if rest.any():
            out[rest] = np.asarray(fallback(la[rest], lo[rest]), np.float64)
    return out.reshape(shape)


def covered(lat, lon):
    """Which points the kunstwerken draw a street on (their floor)."""
    lat, lon = np.ravel(np.asarray(lat, np.float64)), np.ravel(np.asarray(lon, np.float64))
    out = np.zeros(lat.shape, bool)
    for k in all_kunstwerken():
        b = k.in_box(lat, lon)
        if b.any():
            idx = np.flatnonzero(b)
            out[idx] |= np.isfinite(k.floor_at(lat[idx], lon[idx]))
    return out


CAP_M = 0.15                # m the terrain is kept under a kunstwerk's floor


def cap(ground):
    """ground(lat, lon) -> heights, CAP_M under the kunstwerken's floors where they draw one on the ground (not the
    lidar's bumps through them, nor the grading's cuts under them), and no higher than that under their decks."""
    if not all_kunstwerken():
        return ground

    def capped(lat, lon):
        z = np.asarray(ground(lat, lon), np.float64)
        shape = z.shape
        la, lo = np.ravel(np.asarray(lat, np.float64)), np.ravel(np.asarray(lon, np.float64))
        z = z.reshape(-1).copy()
        for k in all_kunstwerken():
            b = k.in_box(la, lo)
            if b.any():
                idx = np.flatnonzero(b)
                f = k.floor_at(la[idx], lo[idx])
                m = np.isfinite(f)
                dk = np.isfinite(k.deck_top(la[idx], lo[idx]))
                at = m & ~dk                    # on the ground: just under it (the grading's raise or cut undone)
                z[idx[at]] = f[at] - CAP_M
                un = m & dk                     # under a deck: no higher than under its slab
                z[idx[un]] = np.minimum(z[idx[un]], f[un] - CAP_M)
        return z.reshape(shape)
    return capped
