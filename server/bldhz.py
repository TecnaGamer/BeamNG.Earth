"""The horizon ring's buildings (kind "bldhz"; the user, 2026-11-14: "buildings toward the real horizon"): past the
skyline (buildings.SKY_SEND_M, 4.8 km) the tall buildings out to SEND_M x the building distance setting, from a
prebaked index of the tall buildings and their parts (tools/build_tall_index.py: one shard a zoom-12 geotile) instead
of the map cells (a city's 40 km of cells would be gigabytes; the index is ~2 MB for New York).

A zoom-12 geotile (~7 km) is built in its own frame (geotiles.placement places it rigidly: the curvature drop at 40 km
comes out right) as the skyline draws (buildings.sky_items / _sky_mesh: the same rules, a quad per wall), in a piece
per skyline square (geotiles.SKY_Z) under it: the mod hides a piece while its square has a skyline tile, a pack or a
far buildings tile (roads.lua farGeoHidden 'H'), so nothing is drawn twice.

Local shards only for now (BNE_BLDHZ_DIR=<folder with manifest.json>): publishing the index needs the user's go.
hello.farHz == PROTO (the mod's roads.FAR_HZ): an older mod is sent none. Shards are read with allow_pickle=False
and checked (offsets, sizes) before use; a shard's SHA-1 (the manifest's, checked against the file) is part of each
tile's cache key."""
import hashlib
import json
import os
import threading

import numpy as np

import mapgeom

PROTO = 1
FORMAT = 1                  # tools/build_tall_index.py FORMAT read here
Z = 12
DIR = os.environ.get("BNE_BLDHZ_DIR") or None
SEND_M = float(os.environ.get("BNE_BLDHZ_SEND_M", "30000"))     # x the building distance setting
DROP_EXTRA_M = 4000.0
PER_POSE = 1
MIN_SIZE = (0.0, 0.0)       # (every indexed building: the index holds only tall ones; a part-tall one's outline is low)

_lock = threading.Lock()
_index = {}                 # dir -> Index or None


class Index:
    """A tall-building index folder: its manifest and shards (memo of the last few decoded)."""

    def __init__(self, d):
        self.dir = d
        with open(os.path.join(d, "manifest.json")) as f:
            m = json.load(f)
        if m.get("format") != FORMAT or int(m.get("z", -1)) != Z:
            raise ValueError(f"bldhz: {d}: format {m.get('format')} z {m.get('z')}, want {FORMAT} / {Z}")
        self.release = str(m.get("release"))
        self.min_height = float(m.get("min_height", 0.0))
        self.tiles = {str(k): v for k, v in (m.get("tiles") or {}).items() if isinstance(v, dict)}
        self.memo = {}

    def has(self, key):
        return key in self.tiles

    def sig(self, key):
        """The shard's identity for cache keys: (release, its SHA-1), None without one."""
        t = self.tiles.get(key)
        return (self.release, str(t.get("sha1"))) if t else None

    def load(self, gx, gy):
        """(buildings, parts) Features of tile (gx, gy), or None (no shard, or one that fails its checks)."""
        import geotiles
        key = geotiles.key(gx, gy, Z)
        t = self.tiles.get(key)
        if t is None:
            return None
        with _lock:
            got = self.memo.get(key)
        if got is not None:
            return got
        path = os.path.join(self.dir, str(Z), f"{int(gx)}_{int(gy)}.npz")
        with open(path, "rb") as f:
            raw = f.read()
        if hashlib.sha1(raw).hexdigest() != t.get("sha1"):
            raise ValueError(f"bldhz: {path}: not the manifest's shard")
        import io
        with np.load(io.BytesIO(raw), allow_pickle=False) as z:
            a = {k: z[k] for k in z.files}
        got = (_features("buildings", a, "b_"), _features("building_parts", a, "p_"))
        with _lock:
            if len(self.memo) > 16:
                self.memo.clear()
            self.memo[key] = got
        return got


def _features(layer, a, pre):
    """mapgeom.Features of one shard's prefix (b_ / p_): polygons of one ring, the attribute columns as the map
    cells have them. Checked: offsets ascending within the coordinates, every column one row a feature."""
    ids = a.get(pre + "id")
    if ids is None:
        return mapgeom.build(layer, np.zeros(0, "<U1"), [])
    n = len(ids)
    off = np.asarray(a[pre + "ring_off"], np.int64)
    ll = np.asarray(a[pre + "ll"])
    if ll.ndim != 2 or ll.shape[1] != 2 or ll.dtype != np.int32 or len(off) != n + 1 or off[0] != 0 \
            or off[-1] != len(ll) or np.any(np.diff(off) < 3):
        raise ValueError(f"bldhz: a malformed shard ({pre})")
    lonlat = ll.astype(np.float64) / 1e7
    geoms = []
    for i in range(n):
        r = lonlat[off[i]:off[i + 1]]
        geoms.append((mapgeom.POLYGON, [[np.vstack([r, r[:1]])]]))     # (closed, as the WKB rings are)
    f = mapgeom.build(layer, np.asarray(ids), geoms)
    f.own = np.ones(n, bool)
    for k, v in a.items():
        if not k.startswith(pre) or k[len(pre):len(pre) + 2] not in ("t_", "f_", "b_"):
            continue
        if len(v) != n:
            raise ValueError(f"bldhz: a malformed shard column {k}")
        f.attrs[k[len(pre) + 2:]] = v
    return f


def index():
    """The Index of BNE_BLDHZ_DIR, None when off or unreadable (read once per process)."""
    if not DIR:
        return None
    with _lock:
        if DIR in _index:
            return _index[DIR]
    try:
        ix = Index(DIR)
    except (OSError, ValueError) as e:
        import logging
        logging.getLogger("earthd").warning("bldhz: index %s unusable: %s", DIR, e)
        ix = None
    with _lock:
        _index[DIR] = ix
    return ix


def on():
    return index() is not None


def squares(tile, ground):
    """The horizon pieces of zoom-12 geotile `tile` (geotiles.GeoBox): {skyline square key: picked items} in the
    tile's frame, rings relative to its box's corner (x0, y0) (buildings.sky_items' rules: simplified, the tallest
    first up to the 16-bit vertex budget a square); {} when the index has none there."""
    import buildings as B
    import geotiles
    ix = index()
    got = ix.load(tile.gx, tile.gy) if ix is not None else None
    if got is None or not len(got[0]):
        return {}
    bf, pf = got
    frame = tile.frame
    x0, y0, x1, y1 = tile.box
    items = B.tile_buildings(frame, x0, y0, x1, y1, [bf], ground, min_size=MIN_SIZE, own=tile.box,
                             part_layers=[pf] if len(pf) else (), parts_min_top=B.SKY_PARTS_MIN_M)
    if not items:
        return {}
    # (each building in the skyline square of its outline's centre: the square whose skyline tile shows it)
    c = np.array([np.asarray(s.ring, np.float64)[:, :2].mean(0) for s, _a, _b in items]) + [x0, y0]
    la, lo, _ = frame.enu_to_geodetic(c[:, 0], c[:, 1], 0.0)
    sx, sy = geotiles.tile_of(np.asarray(la, np.float64), np.asarray(lo, np.float64), geotiles.SKY_Z)
    by = {}
    for it, a, b in zip(items, np.atleast_1d(sx).tolist(), np.atleast_1d(sy).tolist()):
        by.setdefault(geotiles.key(int(a), int(b), geotiles.SKY_Z), []).append(it)
    out = {}
    for k in sorted(by):
        lst = by[k]
        B._sky_simplify(lst)
        lst.sort(key=lambda it: (-(float(it[0].height or 0.0) + float(it[0].min_h or 0.0)), str(it[0].seed)))
        picked, n = [], 0
        for it in lst:
            v = 5 * len(it[0].ring)
            if n + v > B.MAX_MESH_VERTS:
                break
            picked.append(it)
            n += v
        out[k] = picked
    return out
