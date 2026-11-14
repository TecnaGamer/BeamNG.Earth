"""The horizon ring's buildings (kind "bldhz"; the user, 2026-11-14: "buildings toward the real horizon"): past the
skyline (buildings.SKY_SEND_M, 4.8 km) the tall buildings out to the building distance setting's reach (reach():
near none, normal 20 km, far 35 km, max to the horizon), from a prebaked index of the tall buildings and their parts
(tools/build_tall_index.py: every Overture building >= 60 m by its height, storeys or a part, one blob a zoom-12
geotile) instead of the map cells (a city's 40 km of cells would be gigabytes; New York's index is ~2 MB).

A zoom-12 geotile (~7 km) is built in its own frame (geotiles.placement places it rigidly in the mod: the curvature
drop at 40 km comes out right) as the skyline draws (buildings.sky_items / _sky_mesh: the same rules, a quad per wall),
in a piece per skyline square (geotiles.SKY_Z) under it: the mod hides a piece while its square has a skyline tile, a
pack or far buildings tiles (roads.lua hzCovered), so nothing is drawn twice.

Where the index comes from:
- the data site (as osmyears: <BNE_DATA_PAGES>/tallbld/manifest.json + PMTiles parts in the build's own folder, each
  tile one range read, kept on disk per build; TallShards). The build pinned on disk when the process first asks is
  used for its whole life (its key: the tiles' cache key); a newer one is pinned in the background for the next start
  (MapData.warm_manifests). BNE_BLDHZ_BASE: the dataset's folder elsewhere (a staged build).
- BNE_BLDHZ_DIR=<folder>: a local index (build_tall_index.py --out: <folder>/12/<x>_<y>.npz + manifest.json with each
  tile's SHA-1), for tests; the data site is not asked then.
- BNE_BLDHZ=off: none.
hello.farHz == PROTO (the mod's roads.FAR_HZ): an older mod is sent none. Tiles are npz read with allow_pickle=False
and checked (offsets, column lengths) before use."""
import hashlib
import io
import json
import logging
import os
import threading

import numpy as np

import mapgeom

log = logging.getLogger("earthd.bldhz")

PROTO = 1
FORMAT = 1                  # tools/build_tall_index.py FORMAT read here
Z = 12
PAGES_NAME = "tallbld"      # the dataset's folder on the data site (build_tall_index.PAGES_NAME)
ON = os.environ.get("BNE_BLDHZ", "on") != "off"
DIR = os.environ.get("BNE_BLDHZ_DIR") or None
BASE = os.environ.get("BNE_BLDHZ_BASE") or None
# the reach by the building distance setting (settings.lua BUILDING_DISTANCE: near 0.6, normal 1, far 1.5, max 2): near
# none, normal 20 km (12 mi), far 35 km (22 mi), max to the horizon (HORIZON_M, the fixed view distance when shorter)
REACH_M = ((0.8, 0.0), (1.25, 20000.0), (1.75, 35000.0))
HORIZON_M = 120000.0
DROP_EXTRA_M = 4000.0
PER_POSE = 1
MAX_TILE_BYTES = 16 << 20
MIN_SIZE = (0.0, 0.0)       # (every indexed building: the index holds only tall ones; a part-tall one's outline is low)

_lock = threading.Lock()
_local = {}                 # dir -> LocalIndex or None
_pages = {}                 # (cache_dir, id(fetcher), BASE) -> PagesIndex


def reach(building_scale, view_m=None):
    """m: the horizon ring's reach for the building distance setting's scale (0: none); view_m: the fixed view
    distance (0 / None: the real horizon)."""
    s = float(building_scale or 1.0)
    for top, m in REACH_M:
        if s < top:
            return m
    return min(HORIZON_M, float(view_m)) if view_m else HORIZON_M


def decode(b):
    """A tile's npz bytes as {name: array} (no pickles)."""
    if len(b) > MAX_TILE_BYTES:
        raise ValueError(f"bldhz: a tile of {len(b)} bytes")
    with np.load(io.BytesIO(bytes(b)), allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def features(a):
    """(buildings, parts) Features of a decoded tile."""
    return _features("buildings", a, "b_"), _features("building_parts", a, "p_")


def _features(layer, a, pre):
    """mapgeom.Features of one tile's prefix (b_ / p_): polygons of one ring, the attribute columns as the map
    cells have them. Checked: offsets ascending within the coordinates, every column one row a feature."""
    ids = a.get(pre + "id")
    if ids is None:
        return mapgeom.build(layer, np.zeros(0, "<U1"), [])
    n = len(ids)
    off = np.asarray(a[pre + "ring_off"], np.int64)
    ll = np.asarray(a[pre + "ll"])
    if ll.ndim != 2 or ll.shape[1] != 2 or ll.dtype != np.int32 or len(off) != n + 1 or off[0] != 0 \
            or off[-1] != len(ll) or np.any(np.diff(off) < 3) or ids.dtype.kind != "U":
        raise ValueError(f"bldhz: a malformed tile ({pre})")
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
        if len(v) != n or v.dtype.kind not in "Ufb":
            raise ValueError(f"bldhz: a malformed tile column {k}")
        f.attrs[k[len(pre) + 2:]] = v
    return f


class LocalIndex:
    """BNE_BLDHZ_DIR: a local index folder, its manifest and tiles (memo of the last few decoded)."""

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

    def usable(self):
        return True

    def sig(self, gx, gy):
        """A tile's identity for cache keys: ("local", release, threshold, its SHA-1 or None)."""
        import geotiles
        t = self.tiles.get(geotiles.key(gx, gy, Z))
        return ("local", self.release, self.min_height, str(t.get("sha1")) if t else None)

    def load(self, gx, gy):
        """(buildings, parts) Features of tile (gx, gy), or None (none there); raises ValueError on a tile that fails
        its checks."""
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
            raise ValueError(f"bldhz: {path}: not the manifest's tile")
        got = features(decode(raw))
        with _lock:
            if len(self.memo) > 16:
                self.memo.clear()
            self.memo[key] = got
        return got


def _tall_shards_class():
    import osmtagshards
    import osmyears

    class TallShards(osmyears.YearShards):
        """The data site's index (osmyears' layout and checks: the build frozen per process, each part pinned to it by
        its first read's SHA-256 and its size)."""
        NAME, FORMAT, LABEL = PAGES_NAME, FORMAT, "tall building shards"
        MAX_TILE_BYTES = MAX_TILE_BYTES
        MAX_LEAF_BYTES = 4 << 20

        def _on(self):
            return ON and not DIR

        def _base(self):
            return BASE

        def _decode(self, b, m):
            return decode(b)
    return TallShards, osmtagshards


class PagesIndex:
    """The data site's index through TallShards (frozen build)."""

    def __init__(self, cache_dir, fetcher):
        cls, self._ots = _tall_shards_class()
        self.sh = cls(cache_dir, fetcher)

    def usable(self):
        got = self.sh.frozen()
        return got is not None and got[1].get("zoom") == Z

    def sig(self, gx, gy):
        got = self.sh.frozen()
        return ("pages", got[0], got[1].get("min_height")) if got is not None else ("pages", None)

    def load(self, gx, gy):
        """(buildings, parts) of tile (gx, gy), None (no tile there); raises (not had now: asked again later)."""
        import mapcells
        t = self.sh.tile(mapcells.CellKey(Z, int(gx), int(gy)))
        return features(t) if t is not None else None

    def version(self):
        return self.sh.version()


def source(md=None):
    """The index in use: LocalIndex (BNE_BLDHZ_DIR), PagesIndex (map data md's fetcher and cache folder), or None
    (off, unreadable, no fetcher)."""
    if not ON:
        return None
    if DIR:
        with _lock:
            if DIR in _local:
                return _local[DIR]
        try:
            ix = LocalIndex(DIR)
        except (OSError, ValueError) as e:
            log.warning("bldhz: index %s unusable: %s", DIR, e)
            ix = None
        with _lock:
            _local[DIR] = ix
        return ix
    fetcher = getattr(md, "fetcher", None)
    cache_dir = getattr(md, "cache_dir", None)
    if fetcher is None or not isinstance(cache_dir, str) or not cache_dir:
        return None
    k = (cache_dir, id(fetcher), BASE)
    with _lock:
        got = _pages.get(k)
    if got is None:
        try:
            got = PagesIndex(cache_dir, fetcher)
        except Exception as e:          # (a native module too old, a cache folder it cannot use)
            log.warning("bldhz: %s: %s", type(e).__name__, e)
            return None
        with _lock:
            got = _pages.setdefault(k, got)
    return got


def on(md=None):
    """Is there an index to read (the hello's farHz)?"""
    src = source(md)
    try:
        return src is not None and bool(src.usable())
    except Exception:
        return False


class _AllOwned(tuple):
    """A tile's box whose every point is its own (tile_buildings' own): the index's blobs hold each building once."""

    def __new__(cls, box):
        return super().__new__(cls, tuple(float(v) for v in box))

    def owns(self, x, y):
        return True if np.ndim(x) == 0 else np.ones(np.shape(x), bool)


def squares(tile, ground, md=None):
    """The horizon pieces of zoom-12 geotile `tile` (geotiles.GeoBox): {skyline square key: picked items} in the
    tile's frame, rings relative to its box's corner (x0, y0) (buildings.sky_items' rules: simplified, the tallest
    first up to the 16-bit vertex budget a square); {} when the index has none there, None when its tile is not had
    now (asked again later)."""
    import buildings as B
    import geotiles
    src = source(md)
    if src is None:
        return {}
    try:
        got = src.load(tile.gx, tile.gy)
    except ValueError:
        raise
    except Exception as e:              # (offline, the site failing: TallShards keeps it down a while)
        log.debug("bldhz tile %s: %s: %s", tile.key, type(e).__name__, e)
        return None
    if got is None or not len(got[0]):
        return {}
    bf, pf = got
    frame = tile.frame
    x0, y0, x1, y1 = tile.box
    # (every building of the tile's blob: the index put each in one tile, by its bbox centre)
    items = B.tile_buildings(frame, x0, y0, x1, y1, [bf], ground, min_size=MIN_SIZE, own=_AllOwned(tile.box),
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
