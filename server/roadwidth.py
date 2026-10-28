"""Street widths from the buildings that line them (docs/ARCHITECTURE.md "Roads"; the user, 2026-10-11:
Thames St at Fells Point, a 25-30 m cobbled square, was drawn as a 10.6 m street - Overture has no
width for most streets, so every street of a class got the same default).

First tried from aerial imagery (NAIP, 0.6 m): asphalt, concrete sidewalks and many roofs are the
same light grey there (brightness ~0.6, saturation < 0.05), so cross-sections of one street read
4-35 m. Building footprints (Overture) are exact and need no imagery: in a built-up street the
buildings stand at the back of the sidewalks, so the distance between the building faces across the
street is its right-of-way.

measure_row(): every STEP m along the street (END_SKIP m kept clear of its ends: junctions), a ray
each way up to REACH m to the first building wall. The street is "walled" when both rays hit in at
least WALLED of the cross-sections and the face-to-face distances agree (IQR within AGREE of the
median); its right-of-way is then that median, split by split(): two sidewalks of WALK_SHARE of it
(clamped to WALK_MIN..WALK_MAX, down to WALK_NARROW_MIN on very narrow streets) and the carriageway between.
Elsewhere (front yards, open land): None,
and the class defaults stay. Pure; RoadWidths memoises per road for the life of the server, so the
physics block, grading, the road meshes and the surface masks always agree.
"""
import logging
import os
import threading
from collections import OrderedDict

import numpy as np

import geodesy
import mapcells
import staging

log = logging.getLogger("earthd.roadwidth")

STEP = 6.0              # m between cross-sections
END_SKIP = 10.0         # m kept clear of each end of a road (junctions)
REACH = 25.0            # m searched each side
NEAR_M = REACH + 10.0   # m round a road the cells whose buildings it is measured against
COMBOS = 32             # sets of cells' walls kept (roads near a cell's edge)
NEAR_STEP_M = 200.0     # m between a road's points its cells are found round
MIN_SAMPLES = 4
WALLED = 0.6            # share of cross-sections with a building on both sides
AGREE = 0.3             # IQR / median at most this
WALK_SHARE = 0.2        # each sidewalk this share of the right-of-way ...
WALK_MIN, WALK_MAX = 1.8, 4.5   # ... within these (m)
CARRIAGE_MIN = 3.0      # m: a measured single-lane street, including old town streets
WALK_NARROW_MIN = 0.6   # m: keep narrow streets within their measured building-to-building width
BUCKET = 16.0           # m, wall segment buckets


WALL_CELLS = 48          # cells of building walls kept per process (budget.py: fewer on a small machine)
# A cell's walls also on disk (store_dir), float32 in the cell's frame (within a few km of its centre: sub-mm): every
# server process built them from the buildings of the 9 cells around, ~0.6 s a cell on a Steam Deck, and the block
# process, the road processes and the server each did it again (7.4 s of a 15 s Baltimore block, 2026-10-20)
WALLS_VERSION = 2         # (2: a cell's own buildings only, 2026-10-27)

class Walls:
    """Building wall segments (metres, a local frame) with a bucket index for ray casts."""

    def __init__(self, segs, index=None):
        """index: (buckets (3, B) int64 rows key, first, count, seg_of (P,) int64) as index_arrays() gives (the
        store's, memory-mapped), else made here."""
        # x0 y0 x1 y1: float32 kept as given (the store's file, memory-mapped: one copy in the page cache for every
        # server process; their float64 copies were ~100-160 MB a process on a small machine, 2026-10-20), widened to
        # float64 wherever they are computed with (exact: the same numbers as a float64 copy)
        if isinstance(segs, np.ndarray) and segs.dtype == np.float32 and segs.ndim == 2:
            self.segs = segs
        else:
            self.segs = np.asarray(segs, np.float64).reshape(-1, 4)
        if index is not None:
            bk, self.seg_of = index
            self.keys, self.first, self.counts = bk[0], bk[1], bk[2]     # (rows: contiguous, as the Rust loop takes)
            return
        # bucket index, vectorised (a Python loop per segment took ~18 s for a city's 9 cells, 2026-10-12):
        # one (segment, bucket) pair per bucket a segment's box touches, sorted by bucket key
        s = np.asarray(self.segs, np.float64)
        bx0 = np.floor(np.minimum(s[:, 0], s[:, 2]) / BUCKET).astype(np.int64)
        bx1 = np.floor(np.maximum(s[:, 0], s[:, 2]) / BUCKET).astype(np.int64)
        by0 = np.floor(np.minimum(s[:, 1], s[:, 3]) / BUCKET).astype(np.int64)
        by1 = np.floor(np.maximum(s[:, 1], s[:, 3]) / BUCKET).astype(np.int64)
        nx, ny = bx1 - bx0 + 1, by1 - by0 + 1
        cnt = nx * ny
        seg = np.repeat(np.arange(len(s)), cnt)
        k = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        bx = np.repeat(bx0, cnt) + k % np.repeat(nx, cnt)
        by = np.repeat(by0, cnt) + k // np.repeat(nx, cnt)
        key = self._key(bx, by)
        # (any order within a bucket: a ray keeps its nearest hit; np.unique would sort the sorted keys again)
        order = np.argsort(key)
        ks = key[order]
        first = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]]) if len(ks) else np.zeros(0, np.int64)
        self.keys = ks[first]
        self.first, self.counts, self.seg_of = first, np.diff(np.r_[first, len(ks)]), seg[order]

    def index_arrays(self):
        """(buckets (3, B) int64 rows key, first, count, seg_of (P,) int64): the bucket index, for the store."""
        return (np.stack([self.keys, self.first, self.counts]).astype(np.int64),
                np.asarray(self.seg_of, np.int64))

    @staticmethod
    def _key(bx, by):
        return (np.asarray(bx, np.int64) + (1 << 30)) * (1 << 31) + (np.asarray(by, np.int64) + (1 << 30))

    def ray(self, p, d, reach=REACH):
        """Distance from p (2,) along unit d (2,) to the first wall, or None within reach."""
        t = self.rays(np.asarray(p, np.float64)[None], np.asarray(d, np.float64)[None], reach)[0]
        return None if np.isnan(t) else float(t)

    def rays(self, p, d, reach=REACH):
        """Distances from the points p (m, 2) along the unit directions d (m, 2) to the first wall, NaN
        where none is within reach. All rays at once (one Python call per ray took ~3 s a block,
        2026-10-12): every (ray, bucket) pair of the rays' boxes, then every (ray, segment) pair of those
        buckets, intersected together."""
        p = np.asarray(p, np.float64).reshape(-1, 2)
        d = np.asarray(d, np.float64).reshape(-1, 2)
        out = np.full(len(p), np.nan)
        if not len(p) or not len(self.keys):
            return out
        q = p + d * reach
        bx0 = np.floor(np.minimum(p[:, 0], q[:, 0]) / BUCKET).astype(np.int64)
        bx1 = np.floor(np.maximum(p[:, 0], q[:, 0]) / BUCKET).astype(np.int64)
        by0 = np.floor(np.minimum(p[:, 1], q[:, 1]) / BUCKET).astype(np.int64)
        by1 = np.floor(np.maximum(p[:, 1], q[:, 1]) / BUCKET).astype(np.int64)
        nx, ny = bx1 - bx0 + 1, by1 - by0 + 1
        cnt = nx * ny
        ray = np.repeat(np.arange(len(p)), cnt)
        k = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        bx = np.repeat(bx0, cnt) + k % np.repeat(nx, cnt)
        by = np.repeat(by0, cnt) + k // np.repeat(nx, cnt)
        key = self._key(bx, by)
        i = np.minimum(np.searchsorted(self.keys, key), len(self.keys) - 1)
        found = self.keys[i] == key
        ray, i = ray[found], i[found]
        c = self.counts[i]
        ray = np.repeat(ray, c)
        j = np.arange(c.sum()) - np.repeat(np.cumsum(c) - c, c) + np.repeat(self.first[i], c)
        s = np.asarray(self.segs[self.seg_of[j]], np.float64)
        pr, dr = p[ray], d[ray]
        a, e = s[:, :2], s[:, 2:] - s[:, :2]
        den = dr[:, 0] * e[:, 1] - dr[:, 1] * e[:, 0]
        ok = np.abs(den) > 1e-12
        sden = np.where(ok, den, 1.0)
        w = a - pr
        t = np.where(ok, (w[:, 0] * e[:, 1] - w[:, 1] * e[:, 0]) / sden, -1)
        u = np.where(ok, (w[:, 0] * dr[:, 1] - w[:, 1] * dr[:, 0]) / sden, -1)
        hit = ok & (t > 0) & (t <= reach) & (u >= 0) & (u <= 1)
        best = np.full(len(p), np.inf)
        np.minimum.at(best, ray[hit], t[hit])
        out[np.isfinite(best)] = best[np.isfinite(best)]
        return out


def measure_row(centreline, walls):
    """The right-of-way (m) of a street (centreline (k, 2), metres) between the building walls, or None
    where it is not walled on both sides consistently. Pure."""
    import fast
    if not len(walls.keys):         # (no walls at all: not walled)
        return None
    fast.require(34, "measure_row")     # (the Rust loop; ~3000 streets a 9-tile set)
    k = walls.__dict__.get("_rs")
    f32 = walls.segs.dtype == np.float32
    if k is None:
        k = walls._rs = tuple(np.ascontiguousarray(a, t) for a, t in
                              ((walls.keys, np.int64), (walls.first, np.int64), (walls.counts, np.int64),
                               (walls.seg_of, np.int64), (walls.segs, np.float32 if f32 else np.float64)))
    fn = fast._ef.measure_row32 if f32 else fast._ef.measure_row
    return fn(np.ascontiguousarray(np.asarray(centreline, np.float64)[:, :2]), *k, BUCKET,
              STEP, END_SKIP, REACH, MIN_SAMPLES, WALLED, AGREE)

def split(row):
    """(carriageway m, sidewalk m) of a right-of-way, or None when the carriageway would be implausible."""
    walk = min(WALK_MAX, max(WALK_MIN, WALK_SHARE * row))
    walk = min(walk, max(WALK_NARROW_MIN, (row - CARRIAGE_MIN) / 2))
    cw = row - 2 * walk
    if cw < CARRIAGE_MIN:
        return None
    return cw, walk


def polygon_walls(feats, frame):
    """Wall segments of building polygons (Features) in frame's metres: consecutive vertices of every ring
    (rings of fewer than 3 points skipped). One coordinate transform for the whole set (one per ring took
    ~22 s for a city's 9 cells, 2026-10-12)."""
    ll = np.asarray(feats.lonlat, np.float64)
    off = np.asarray(feats.coord_off, np.int64)
    if len(ll) < 2 or len(off) < 2:
        return np.zeros((0, 4))
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    p = np.stack([e, n], 1)
    lengths = np.diff(off)
    ring = np.repeat(np.arange(len(lengths)), lengths)
    keep_ring = lengths >= 3
    i = np.arange(len(ll) - 1)
    ok = (ring[:-1] == ring[1:]) & keep_ring[ring[:-1]]
    i = i[ok]
    return np.concatenate([p[i], p[i + 1]], 1)


def _read_segs(path):
    """A cell's walls from the store, memory-mapped read-only (shared by the server's processes), or None."""
    try:
        a = np.asarray(np.load(path, mmap_mode="r", allow_pickle=False))     # (a plain view of the mapping)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError) as e:        # damaged: built again
        log.debug("road widths: %s: %s", path, e)
        return None
    import cache as cachemod
    cachemod.touch_once(path)           # (recent for the start-up prune: cache.prune_dir, STORE_MB)
    return a if a.dtype == np.float32 and a.ndim == 2 and a.shape[1] == 4 else None


def _index_paths(path):
    return path[:-4] + ".bk.npy", path[:-4] + ".so.npy"


def complete_store(path):
    """The walls' own file is in the store (their index may be kept beside it)."""
    return os.path.exists(path)


def _read_index(path, nsegs):
    """The bucket index stored beside a cell's walls (memory-mapped), or None (none, or not of these walls)."""
    pb, ps = _index_paths(path)
    try:
        bk = np.asarray(np.load(pb, mmap_mode="r", allow_pickle=False))
        so = np.asarray(np.load(ps, mmap_mode="r", allow_pickle=False))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError) as e:
        log.debug("road widths: %s: %s", pb, e)
        return None
    ok = (bk.dtype == np.int64 and bk.ndim == 2 and bk.shape[0] == 3 and so.dtype == np.int64 and so.ndim == 1
          and (bk.shape[1] == 0 or int(bk[1, -1]) + int(bk[2, -1]) == len(so)) and (nsegs > 0 or len(so) == 0))
    return (bk, so) if ok else None


def _write_index(path, index):
    for p, a in zip(_index_paths(path), index):
        _write_segs(p, a)


def _write_segs(path, segs):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as fh:
            np.save(fh, segs)
        staging.replace_cached(tmp, path)
    except OSError as e:
        log.debug("road widths: %s: %s", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass


STORE_MB = 1024             # the walls' files kept at most (earthd's start-up prune; 3.5 GB on a Steam Deck, 2026-10-23)


def store_dir(cache_dir, md):
    """The walls' folder for map data md (its release) under cache_dir, or None."""
    if not cache_dir or md is None:
        return None
    return os.path.join(cache_dir, "roadwidth", f"v{WALLS_VERSION}", str(getattr(md, "release", "x")))


class RoadWidths:
    """Measured street widths per road id, decided once per server run (thread-safe). cell_fn(key) ->
    the buildings Features of a map cell (None / raises when unavailable: the road keeps its default)."""

    def __init__(self, cell_fn, store_dir=None):
        """store_dir: the walls' files (the map data release's own folder), or None (none kept on disk)."""
        self.cell_fn = cell_fn
        self.store_dir = store_dir
        self.memo = {}                          # fid -> (carriageway, walk) or None
        self.walls = OrderedDict()              # cell key -> (frame, Walls), the WALL_CELLS last used
        self.combos = OrderedDict()             # cell keys -> (frame, Walls) of a road near cells' edges
        self.lock = threading.Lock()
        self.key_locks = {}                     # cell key -> Lock: one thread builds a cell's walls

    def _walls(self, key):
        """The walls of the buildings in cell key, in a frame at the cell's centre."""
        with self.lock:
            got = self.walls.get(str(key))
            kl = self.key_locks.setdefault(str(key), threading.Lock())
        if got is not None:
            return got
        with kl:
            return self._build_walls(key)

    def _build_walls(self, key):
        with self.lock:
            got = self.walls.get(str(key))
        if got is not None:
            return got
        lon0, lat0, lon1, lat1 = key.bounds()
        frame = geodesy.EnuFrame(0.5 * (lat0 + lat1), 0.5 * (lon0 + lon1))
        path = (os.path.join(self.store_dir, str(key.z), str(key.x), f"{key.y}.npy")
                if self.store_dir and hasattr(key, "z") else None)
        segs = _read_segs(path) if path else None
        if segs is None:
            parts, complete = [], True
            for k2 in (key,):
                try:
                    feats = self.cell_fn(k2)
                except Exception:
                    feats, complete = None, False       # (not kept: built again once the cell is at hand)
                if feats is not None and len(feats):
                    parts.append(polygon_walls(feats, frame))
            # float32 as the file keeps them, so a process that built them and one that read them agree
            segs = (np.concatenate(parts) if parts else np.zeros((0, 4))).astype(np.float32)
            if path and complete:
                _write_segs(path, segs)
                segs = _read_segs(path)         # (the file's mapping: shared with the other processes)
                if segs is None:
                    segs = (np.concatenate(parts) if parts else np.zeros((0, 4))).astype(np.float32)
        walls = None
        if path:            # the bucket index beside them (memory-mapped too), made once
            index = _read_index(path, len(segs))
            if index is not None:
                walls = Walls(segs, index)
            else:
                walls = Walls(segs)
                if complete_store(path):
                    _write_index(path, walls.index_arrays())
                    index = _read_index(path, len(segs))
                    if index is not None:
                        walls = Walls(segs, index)
        if walls is None:
            walls = Walls(segs)
        with self.lock:
            got = self.walls.setdefault(str(key), (frame, walls))
            self.walls.move_to_end(str(key))
            while len(self.walls) > WALL_CELLS:     # (a road's width is decided once, memo: the walls can go)
                self.walls.popitem(last=False)
            return got

    def _near_walls(self, ll):
        """(frame, Walls) of the buildings in the cells within NEAR_M of the road (lon/lat (k, 2)): its own cell's,
        or, near a cell's edge, those of the cells there in the first one's frame (kept for the COMBOS last used).
        Each road read the buildings of the 8 cells around its middle's too: ~75 cells of buildings for one road
        tile's planning on a first visit to a Pennsylvania town, 2026-10-27; a width needs the walls within REACH."""
        # (the cells along it: every NEAR_STEP_M of it; its box's cells were a grid of them for a long rural road)
        seg = np.hypot(np.diff(ll[:, 1]) * 111320.0, np.diff(ll[:, 0]) * 111320.0 * np.cos(np.radians(ll[:-1, 1])))
        s = np.r_[0.0, np.cumsum(seg)]
        t = np.r_[np.arange(0.0, s[-1], NEAR_STEP_M), s[-1]] if s[-1] > 0 else np.zeros(1)
        lat, lon = np.interp(t, s, ll[:, 1]), np.interp(t, s, ll[:, 0])
        dlat = (NEAR_M + NEAR_STEP_M / 2) / 111320.0
        dlon = dlat / max(0.05, float(np.cos(np.radians(np.mean(lat)))))
        keys = sorted(mapcells.cells_near_points(lat, lon, dlat, dlon), key=str)
        if len(keys) == 1:
            return self._walls(keys[0])
        ck = tuple(str(k) for k in keys)
        with self.lock:
            got = self.combos.get(ck)
            if got is not None:
                self.combos.move_to_end(ck)
                return got
        frame, segs = None, []
        for k in keys:
            f, w = self._walls(k)
            sg = np.asarray(w.segs, np.float64)
            if frame is None:
                frame = f
            elif len(sg):
                pts = sg.reshape(-1, 2)
                la, lo, _ = f.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
                e, n, _ = frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
                sg = np.stack([np.asarray(e), np.asarray(n)], -1).reshape(-1, 4)
            segs.append(sg)
        got = (frame, Walls(np.concatenate(segs) if segs else np.zeros((0, 4))))
        with self.lock:
            got = self.combos.setdefault(ck, got)
            while len(self.combos) > COMBOS:
                self.combos.popitem(last=False)
        return got

    def get(self, fid, lonlat):
        """(carriageway m, sidewalk m) of road fid (geometry lonlat (k, 2)), or None (defaults)."""
        with self.lock:
            if fid in self.memo:
                return self.memo[fid]
        ll = np.asarray(lonlat, np.float64)
        frame, walls = self._near_walls(ll)
        res = None
        if len(walls.segs):
            e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
            row = measure_row(np.c_[e, n], walls)
            res = split(row) if row is not None else None
        with self.lock:
            self.memo.setdefault(fid, res)
            return self.memo[fid]
