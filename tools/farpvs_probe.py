#!/usr/bin/env python3
"""Far-field occlusion probe (the retro study's item C / 7.3, Quake-PVS style): for a few places, which of the far
field's selected tiles (farfield.select_global at the server's defaults: mesh mode, 64 cells, horizon on, global far
tiles) are hidden behind terrain from anywhere in a square of viewpoints round the player (the terrain block, plus a
look-ahead), and how many aerial-photo requests and texels that would save if hidden tiles took a small texture.

Offline. The DEM is read from the real cache (~/.cache/beamng-earth/dem/mapterhorn) read-only; a tile it lacks is
downloaded into --scratch (default ~/bnge-tests/farpvs), never into the real cache.

The viewshed is conservative by construction where it can be: occluders from a min-pooled raster with the
unrefracted earth's drop, targets from a max-pooled one with the refracted earth's (R'), each raster cell padded by one
cell; a tile counts as visible if any sample in it clears the horizon of any viewpoint. Viewpoints are a grid
(--step) over the square, each eye at its ground + h.

    python3 tools/farpvs_probe.py                     # every place, eye 1.8 / 50 / 300 m, block +-0.5 and +-2.5 km
    python3 tools/farpvs_probe.py --places zermatt --eyes 1.8 --halves 500
"""
import argparse
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import fast  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402
import imagery as im  # noqa: E402

PLACES = {
    "zermatt": (46.020, 7.749),
    "stelvio": (46.529, 10.453),
    "leadville": (39.250, -106.292),
    "vail": (39.640, -106.374),
    "yosemite": (37.740, -119.590),
    "asheville": (35.595, -82.551),
    "pittsburgh": (40.440, -79.996),
    "genoa": (44.407, 8.934),
    "sanfrancisco": (37.775, -122.419),
    "nyc": (40.712, -74.006),
    "wichita": (37.687, -97.330),
    "gardencity": (37.971, -100.873),      # (western Kansas farmland: the flattest of these)
    "flevoland": (52.457, 5.588),          # (a Dutch polder)
}
REAL = os.path.expanduser("~/.cache/beamng-earth/dem/mapterhorn")
URL = "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"
UA = "BeamNG.Earth/farpvs-probe (+https://github.com/TecnaGamer/BeamNG.Earth)"
R_OCC = ff.R_EARTH              # occluders drop more (no refraction): lower, conservative
R_TGT = ff.R_REFRACTED          # targets drop less: higher, conservative
LAYOUT = ff.Layout(64)          # the server's default (mesh mode, 64 cells, 1 km block)
US_BOX = (24.0, -125.0, 50.0, -66.0)


class LiteDem:
    """dem.Dem's sample / tiles / block_max over Mapterhorn alone: the real cache read-only, the rest fetched into
    a scratch directory. A 404 is the sea (0 m)."""

    def __init__(self, scratch):
        self.scratch = os.path.join(scratch, "dem", "mapterhorn")
        self.mem = {}
        self.fetched = 0

    def _raw(self, z, x, y):
        for root in (REAL, self.scratch):
            p = os.path.join(root, str(z), str(x), f"{y}.webp")
            if os.path.exists(p):
                with open(p, "rb") as fh:
                    return fh.read()
            if os.path.exists(p + ".404"):
                return None
        p = os.path.join(self.scratch, str(z), str(x), f"{y}.webp")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        req = urllib.request.Request(URL.format(z=z, x=x, y=y), headers={"User-Agent": UA})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                with open(p + ".tmp", "wb") as fh:
                    fh.write(data)
                os.replace(p + ".tmp", p)
                self.fetched += 1
                return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    open(p + ".404", "w").close()
                    return None
                time.sleep(1 + attempt)
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1 + attempt)
        raise IOError(f"DEM tile {z}/{x}/{y} not fetched")

    def _tile(self, key):
        a = self.mem.get(key)
        if a is None:
            data = self._raw(*key)
            if data is None:
                a = np.zeros((512, 512), np.float32)
            else:
                img = Image.open(io.BytesIO(data)).convert("RGB")
                a = fast._ef.terrarium_heights(np.ascontiguousarray(np.asarray(img)))
                a = np.maximum(np.asarray(a, np.float32), -500.0)
            self.mem[key] = a
        return a

    def tiles(self, keys, progress=None):
        keys = list(dict.fromkeys(keys))
        todo = [k for k in keys if k not in self.mem]
        if todo:
            with ThreadPoolExecutor(8) as ex:
                list(ex.map(self._tile, todo))
        return {k: self._tile(k) for k in keys}

    def block_max(self, z, x, y, block):
        a = self.tiles([(z, x, y)])[(z, x, y)]
        n = 512 // block
        return a.reshape(n, block, n, block).max(axis=(1, 3))

    def sample(self, lat, lon, zoom, progress=None):
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        shape = np.broadcast(lat, lon).shape
        lat, lon = np.broadcast_to(lat, shape).ravel(), np.broadcast_to(lon, shape).ravel()
        n = 2 ** zoom
        w = n * 512
        fx = (lon / 360.0 + 0.5) * w
        fx -= w * np.round((fx - fx[0]) / w)
        fx -= 0.5
        fy = (0.5 - np.arcsinh(np.tan(np.radians(np.clip(lat, -85.05, 85.05)))) / (2 * np.pi)) * w - 0.5
        tx0, tx1 = int(math.floor(fx.min())) // 512, int(math.floor(fx.max()) + 1) // 512
        ty0 = max(0, int(math.floor(fy.min())) // 512)
        ty1 = min(n - 1, int(math.floor(fy.max()) + 1) // 512)
        nx = tx1 - tx0 + 1
        keys = [(zoom, tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
        got = self.tiles(keys)
        out = fast._ef.bilinear_tiles([np.asarray(got[k], np.float32) for k in keys], nx, 512,
                                      fx - tx0 * 512, np.clip(fy, 0, None) - ty0 * 512)
        return np.asarray(out, np.float64).reshape(shape)


def zoom_for(lat, px_m):
    c = 40075016.686 * math.cos(math.radians(lat))
    return int(min(14, max(0, math.ceil(math.log2(c / (512 * px_m))))))


class Rings:
    """Elevation rasters in the anchor's plane: rings of growing cell size, each a min-pooled and a max-pooled raster
    (sampled at half the cell, pooled 2 x 2, then eroded / dilated by one cell)."""
    SPECS = ((24000.0, 50.0), (100000.0, 200.0), (None, 1000.0))

    def __init__(self, frame, dem, radius):
        self.rings = []
        for half, sp in self.SPECS:
            half = radius + 5000.0 if half is None else half
            n = int(math.ceil(2 * half / sp))
            half = n * sp / 2
            k = (np.arange(2 * n) + 0.5) * (sp / 2) - half
            E, N = np.meshgrid(k, k)                          # row 0 south
            lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
            z = zoom_for(frame.lat0, sp / 2)
            a = dem.sample(lat, lon, z).reshape(n, 2, n, 2)
            lo, hi = a.min(axis=(1, 3)), a.max(axis=(1, 3))
            lo, hi = _erode(lo), _dilate(hi)
            self.rings.append((half, sp, lo.astype(np.float32), hi.astype(np.float32), z))

    def lookup(self, r, x, y):
        half, sp, lo, hi, _ = self.rings[r]
        n = lo.shape[0]
        i = np.clip(((x + half) / sp).astype(np.int64), 0, n - 1)
        j = np.clip(((y + half) / sp).astype(np.int64), 0, n - 1)
        inside = (np.abs(x) < half) & (np.abs(y) < half)
        return lo[j, i], hi[j, i], inside, j, i


def _dilate(a):
    p = np.pad(a, 1, mode="edge")
    return np.max([p[dj:dj + a.shape[0], di:di + a.shape[1]] for dj in range(3) for di in range(3)], axis=0)


def _erode(a):
    p = np.pad(a, 1, mode="edge")
    return np.min([p[dj:dj + a.shape[0], di:di + a.shape[1]] for dj in range(3) for di in range(3)], axis=0)


def tile_rasters(rings, tiles):
    """Per ring: a raster of tile indexes (-1: none) painted coarse first (finer tiles win where bboxes overlap)."""
    order = sorted(range(len(tiles)), key=lambda q: -(tiles[q].bbox[2] - tiles[q].bbox[0]))
    out = []
    for half, sp, lo, _hi, _z in rings.rings:
        n = lo.shape[0]
        ids = np.full((n, n), -1, np.int32)
        for q in order:
            x0, y0, x1, y1 = tiles[q].bbox
            i0, i1 = int(math.floor((x0 + half) / sp)), int(math.ceil((x1 + half) / sp))
            j0, j1 = int(math.floor((y0 + half) / sp)), int(math.ceil((y1 + half) / sp))
            i0, j0, i1, j1 = max(i0, 0), max(j0, 0), min(i1, n), min(j1, n)
            if i0 < i1 and j0 < j1:
                ids[j0:j1, i0:i1] = q
        out.append(ids)
    return out


def ray_dists(radius):
    """Sample distances along a ray and the ring each comes from."""
    d0 = np.arange(60.0, 21000.0, 50.0)
    d1 = np.arange(21000.0, 95000.0, 200.0)
    d2 = np.arange(95000.0, radius + 1000.0, 1000.0)
    d = np.concatenate([d0, d1, d2])
    r = np.concatenate([np.zeros(len(d0), np.int64), np.ones(len(d1), np.int64), np.full(len(d2), 2, np.int64)])
    return d, r


def viewshed(rings, ids, px, py, eye, d, ring, azimuths=4096, eps=0.0):
    """Tile indexes visible from (px, py, eye m above sea level) (any sample clearing the horizon by more than eps,
    tan of the angle), and those whose best clearance is above eps2 (a sliver test) as {idx: max clearance}."""
    a = (np.arange(azimuths) + 0.5) * (2 * math.pi / azimuths)
    ca, sa = np.cos(a)[:, None], np.sin(a)[:, None]
    best = {}
    occ = np.empty((azimuths, len(d)), np.float32)
    tgt = np.empty((azimuths, len(d)), np.float32)
    tid = np.full((azimuths, len(d)), -1, np.int32)
    for r in range(3):
        m = ring == r
        if not m.any():
            continue
        dd = d[m][None, :]
        x, y = px + ca * dd, py + sa * dd
        lo, hi, inside, j, i = rings.lookup(r, x, y)
        o = (lo - dd * dd / (2 * R_OCC) - eye) / dd
        t = (hi - dd * dd / (2 * R_TGT) - eye) / dd
        o = np.where(inside, o, -np.inf)
        t = np.where(inside, t, np.inf)         # outside the rasters: visible (conservative)
        occ[:, m], tgt[:, m] = o, t
        tid[:, m] = ids[r][j, i]
        tid[:, m] = np.where(inside, tid[:, m], -2)
    hor = np.maximum.accumulate(occ, axis=1)
    hor = np.concatenate([np.full((azimuths, 1), -np.inf, np.float32), hor[:, :-1]], axis=1)
    clear = tgt - hor
    vis = clear > eps
    sel = vis & (tid >= 0)
    q, c = tid[sel], clear[sel]
    if len(q):
        order = np.argsort(q)
        q, c = q[order], c[order]
        u, start = np.unique(q, return_index=True)
        mx = np.maximum.reduceat(c, start)
        best = dict(zip(u.tolist(), mx.tolist()))
    return best


# --- photo requests ----------------------------------------------------------------------------------------------
def tex_px(tile):
    lvl = tile.level
    if lvl in ff.HORIZON_TEX:
        return ff.HORIZON_TEX[lvl]
    return LAYOUT.tex_size(lvl)


def where_of(frame, tile, px):
    if isinstance(tile, ff.GTile):
        return im.MercSquare(tile.z, tile.x, tile.y, px), ("g", tile.key)
    node = tile.base if isinstance(tile, ff.Piece) else tile
    x0, y0, _, _ = node.bbox
    return im.EnuSquare(frame, *im.gutter_square(x0, y0, node.size, px)), ("a", node.key)


def photo_keys(frame, tile, px, us, chain=None, gutter=True):
    """The source tiles imagery._render would read for a tile's texture at px (the first source of the chain that
    takes the zoom: usgs (z10-16) in the US, else eox2023 (to z14)); chain 'eox': eox alone (the coarse cover's)."""
    where, _ = where_of(frame, tile, px)
    lat, lon = where.grid(8)
    lat = np.clip(lat, -89.9, 89.9)
    zw = im.zoom_for(float(lat[4, 4]), where.size / px)
    if not gutter:          # (the footprint alone, no gutter, no bilinear tap round it: what the texels need)
        if isinstance(where, im.MercSquare):
            where.f0, where.f1 = 0.0, 1.0
        else:
            node = tile.base if isinstance(tile, ff.Piece) else tile
            where = im.EnuSquare(frame, node.bbox[0], node.bbox[1], node.size)
        lat, lon = where.grid(8)
        lat = np.clip(lat, -89.9, 89.9)
    pad = 1 if gutter else 0
    if chain != "eox" and us and zw >= 10:
        src, z = "usgs", min(zw, 16)
    else:
        src, z = "eox2023", min(zw, 14)
    while True:
        fx, fy = im.mercator_px(lat, lon, z)
        tx0, tx1 = int(math.floor(fx.min() - pad)) // 256, int(math.floor(fx.max() + pad - 1e-9)) // 256
        ty0, ty1 = int(math.floor(fy.min() - pad)) // 256, int(math.floor(fy.max() + pad - 1e-9)) // 256
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= im.MAX_MOSAIC or z <= 0:
            break
        z -= 1
    return {(src, z, tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)}


def photo_keys_parent_gutter(frame, tile, px, us):
    """The footprint's photos at the texture's zoom, its gutter (and the edge texels' bilinear taps) from the zoom
    above (one photo of it covers 2 x 2): the requests if render filled what its own zoom leaves from the parent."""
    core = photo_keys(frame, tile, px, us, gutter=False)
    src, z = next(iter(core))[:2]
    if z <= 0:
        return core | photo_keys(frame, tile, px, us)
    where, _ = where_of(frame, tile, px)
    lat, lon = where.grid(8)
    fx, fy = im.mercator_px(np.clip(lat, -89.9, 89.9), lon, z - 1)
    tx0, tx1 = int(math.floor(fx.min() - 1)) // 256, int(math.floor(fx.max() + 1)) // 256
    ty0, ty1 = int(math.floor(fy.min() - 1)) // 256, int(math.floor(fy.max() + 1)) // 256
    return core | {(src, z - 1, tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)}


# --- per place ---------------------------------------------------------------------------------------------------
def select(frame, dem, grid, h_eye_asl):
    radius, _ = grid.radius(0.0, 0.0, h_eye_asl)
    sel, _ = ff.select_global(frame, 0.0, 0.0, ff.global_zoom(frame.lat0), [], set(), LAYOUT.k, ff.HYSTERESIS,
                              radius, max_level=ff.HORIZON_MAX_LEVEL, min_level=LAYOUT.min_level,
                              radii=LAYOUT.near_radii, layout=LAYOUT, disc=True,
                              horizon=ff.Horizon(h_eye_asl, grid), forced_level=0, view=None)
    return radius, sel


def run_place(name, lat, lon, dem, eyes, halves, step, azimuths, sliver_deg, hidden_px, log):
    t0 = time.time()
    frame = geodesy.EnuFrame(lat, lon)
    ground0 = float(dem.sample(np.array([lat]), np.array([lon]), 13)[0])
    grid = ff.horizon_grid(frame, dem, ff.horizon_extent(ground0 + max(eyes), 3000.0))
    us = US_BOX[0] <= lat <= US_BOX[2] and US_BOX[1] <= lon <= US_BOX[3]
    out = []
    rings_cache = {}
    for h in eyes:
        radius, sel = select(frame, dem, grid, ground0 + h)
        tiles = list(sel.values())
        rkey = round(radius, -4)
        if rkey not in rings_cache:
            rings_cache.clear()
            rings_cache[rkey] = Rings(frame, dem, radius)
        rings = rings_cache[rkey]
        ids = tile_rasters(rings, tiles)
        d, ring = ray_dists(radius)
        # photo keys of every tile, full and at hidden_px (eox, the coarse cover's chain)
        full = [photo_keys(frame, t, tex_px(t), us) for t in tiles]
        small = [photo_keys(frame, t, min(hidden_px, tex_px(t)), us, chain="eox") for t in tiles]
        texels = [tex_px(t) ** 2 for t in tiles]
        for half in halves:
            n = max(1, int(round(2 * half / step)) + 1)
            k = np.linspace(-half, half, n) if n > 1 else np.zeros(1)
            vps = [(x, y) for y in k for x in k]
            g = dem.sample(*frame.enu_to_geodetic(np.array([p[0] for p in vps]), np.array([p[1] for p in vps]),
                                                  0.0)[:2], 13)
            best = {}
            tv = time.time()
            for (x, y), gz in zip(vps, g):
                for q, c in viewshed(rings, ids, x, y, float(gz) + h, d, ring, azimuths).items():
                    if c > best.get(q, -1.0):
                        best[q] = c
            tv = time.time() - tv
            eps = math.tan(math.radians(sliver_deg))
            hidden = [q for q in range(len(tiles)) if q not in best]
            sliver = [q for q in range(len(tiles)) if q in best and best[q] <= eps]
            nfull = set().union(*full) if full else set()
            keep = set().union(*[full[q] for q in range(len(tiles)) if q in best]) if best else set()
            hid_small = set().union(*[small[q] for q in hidden]) if hidden else set()
            keep2 = set().union(*[full[q] for q in range(len(tiles)) if q in best and best[q] > eps]) if best else set()
            hid2_small = set().union(*[small[q] for q in hidden + sliver]) if hidden or sliver else set()
            # far tiles only (level >= 3, the global ones: the anchor's nodes are within ~8 km)
            far = [q for q, t in enumerate(tiles) if t.level >= ff.GLOBAL_MIN_LEVEL]
            far_hidden = [q for q in far if q not in best]
            row = {
                "place": name, "lat": lat, "lon": lon, "ground": round(ground0), "eye": h,
                "radius_km": round(radius / 1000), "half": half, "viewpoints": len(vps),
                "tiles": len(tiles), "hidden": len(hidden), "sliver": len(sliver),
                "far_tiles": len(far), "far_hidden": len(far_hidden),
                "req_full": len(nfull), "req_pvs": len(keep | hid_small), "req_pvs_sliver": len(keep2 | hid2_small),
                "texels_full_M": round(sum(texels) / 1e6, 1),
                "texels_pvs_M": round(sum(texels[q] if q in best else min(hidden_px, tex_px(tiles[q])) ** 2
                                          for q in range(len(tiles))) / 1e6, 1),
                "hidden_by_level": _by_level(tiles, hidden), "tiles_by_level": _by_level(tiles, range(len(tiles))),
                "viewshed_s": round(tv, 1),
            }
            out.append(row)
            log(row)
    log({"place": name, "seconds": round(time.time() - t0, 1), "dem_fetched": dem.fetched})
    return out


def run_place_server(name, lat, lon, dem, eyes, halves, hidden_px, grade, log):
    """The server's own PVS (server/farpvs.py: the coarse rings, earthfast.far_pvs, the reachable viewpoints, the
    tile rule) for a player standing at the place."""
    import farpvs as pv
    t0 = time.time()
    frame = geodesy.EnuFrame(lat, lon)
    ground0 = float(dem.sample(np.array([lat]), np.array([lon]), 13)[0])
    grid = ff.horizon_grid(frame, dem, ff.horizon_extent(ground0 + max(eyes), 3000.0))
    us = US_BOX[0] <= lat <= US_BOX[2] and US_BOX[1] <= lon <= US_BOX[3]
    out = []
    for h in eyes:
        radius, sel = select(frame, dem, grid, ground0 + h)
        tiles = list(sel.values())
        tr = time.time()
        rings = pv.Rings(frame, dem, radius)
        tr = time.time() - tr
        pv.EYE_CLASSES = (h,)
        field = pv.Field(0, frame, rings)
        full = [photo_keys(frame, t, tex_px(t), us) for t in tiles]
        core = set().union(*[photo_keys(frame, t, tex_px(t), us, gutter=False) for t in tiles])
        pgut = set().union(*[photo_keys_parent_gutter(frame, t, tex_px(t), us) for t in tiles])
        by_lvl = {}
        for t, ks in zip(tiles, full):
            by_lvl.setdefault(t.level, set()).update(ks)
        small = [photo_keys(frame, t, min(hidden_px, tex_px(t)), us, chain="eox") for t in tiles]
        texels = [tex_px(t) ** 2 for t in tiles]
        g = rings.ground_hi(0.0, 0.0)
        for half in halves:
            tv = time.time()
            n0 = field.computed
            s = pv.Set(field, 0.0, 0.0, half, 0, 0.0, 0.0, g, grade=pv.GRADE if grade is None else grade)
            tv = time.time() - tv
            hidden = []
            for q, t in enumerate(tiles):
                node = t.base if isinstance(t, ff.Piece) else t
                if s.hidden(t.key, node.bbox, t.level):
                    hidden.append(q)
            hs = set(hidden)
            vis = [q for q in range(len(tiles)) if q not in hs]
            nfull = set().union(*full)
            keep = set().union(*[full[q] for q in vis]) if vis else set()
            hid_small = set().union(*[small[q] for q in hidden]) if hidden else set()
            far = [q for q, t in enumerate(tiles) if t.level >= ff.GLOBAL_MIN_LEVEL]
            # the screen's need too (SLOPE_SIZING): each tile at Set.tex_px (hidden ones from the coarse chain)
            pxs, scr_keys, reduced = [], set(), 0
            for q, t in enumerate(tiles):
                node = t.base if isinstance(t, ff.Piece) else t
                p = s.tex_px(t.key, node.bbox, t.level, node.size, tex_px(t))
                pxs.append(p)
                reduced += p < tex_px(t) and q not in hs
                scr_keys |= photo_keys(frame, t, p, us, chain="eox" if q in hs else None) if p < tex_px(t) else full[q]
            row = {
                "engine": "server", "place": name, "lat": lat, "lon": lon, "ground": round(ground0), "eye": h,
                "radius_km": round(radius / 1000), "half": half, "grade": grade, "viewpoints": s.viewpoints,
                "computed": field.computed - n0, "tiles": len(tiles), "hidden": len(hidden), "sliver": 0,
                "far_tiles": len(far), "far_hidden": len([q for q in far if q in hs]),
                "req_full": len(nfull), "req_pvs": len(keep | hid_small), "req_core": len(core),
                "req_parent_gutter": len(pgut),
                "req_by_level": {k: len(v) for k, v in sorted(by_lvl.items())},
                "texels_full_M": round(sum(texels) / 1e6, 1),
                "texels_pvs_M": round(sum(min(hidden_px, tex_px(tiles[q])) ** 2 if q in hs else texels[q]
                                          for q in range(len(tiles))) / 1e6, 1),
                "hidden_by_level": _by_level(tiles, hidden), "rings_s": round(tr, 2), "viewshed_s": round(tv, 2),
                "screen_reduced": reduced, "req_pvs_screen": len(scr_keys),
                "texels_pvs_screen_M": round(sum(p * p for p in pxs) / 1e6, 1),
                "px_by_level": {lv: sorted({pxs[q] for q, t in enumerate(tiles) if t.level == lv})
                                for lv in sorted({t.level for t in tiles})},
            }
            out.append(row)
            log(row)
    log({"place": name, "seconds": round(time.time() - t0, 1), "dem_fetched": dem.fetched})
    return out


def _by_level(tiles, qs):
    c = {}
    for q in qs:
        c[tiles[q].level] = c.get(tiles[q].level, 0) + 1
    return dict(sorted(c.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--places", default=",".join(PLACES))
    ap.add_argument("--eyes", default="1.8,50,300")
    ap.add_argument("--halves", default="500,2500", help="viewpoint square half-sizes (m)")
    ap.add_argument("--step", type=float, default=500.0, help="viewpoint grid step (m)")
    ap.add_argument("--azimuths", type=int, default=4096)
    ap.add_argument("--sliver-deg", type=float, default=0.1)
    ap.add_argument("--hidden-px", type=int, default=128)
    ap.add_argument("--scratch", default=os.path.expanduser("~/bnge-tests/farpvs"))
    ap.add_argument("--out", default=None, help="JSON lines file (default: <scratch>/farpvs.jsonl)")
    ap.add_argument("--engine", choices=("numpy", "server"), default="numpy",
                    help="numpy: the fine reference viewshed here; server: server/farpvs.py as earthd runs it")
    ap.add_argument("--grade", type=float, default=None,
                    help="--engine server: the reachable viewpoints' grade (default farpvs.GRADE; 99: every one)")
    a = ap.parse_args()
    os.makedirs(a.scratch, exist_ok=True)
    out = a.out or os.path.join(a.scratch, "farpvs.jsonl")
    dem = LiteDem(a.scratch)
    eyes = [float(v) for v in a.eyes.split(",")]
    halves = [float(v) for v in a.halves.split(",")]
    with open(out, "a") as fh:
        def log(row):
            print(json.dumps(row), flush=True)
            fh.write(json.dumps(row) + "\n")
            fh.flush()
        for name in a.places.split(","):
            lat, lon = PLACES[name]
            if a.engine == "server":
                run_place_server(name, lat, lon, dem, eyes, halves, a.hidden_px, a.grade, log)
            else:
                run_place(name, lat, lon, dem, eyes, halves, a.step, a.azimuths, a.sliver_deg, a.hidden_px, log)
            dem.mem.clear()


if __name__ == "__main__":
    main()
