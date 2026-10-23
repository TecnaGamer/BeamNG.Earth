"""British Columbia's lidar terrain models (LidarBC): 1 m bare-earth DEMs, read where they cover a tile.

Why (2026-10-23): on Vancouver Island Mapterhorn's z13-z15 tiles are lidar only in the Capital Region and along some
coasts; elsewhere (the east coast lowlands, Port Alberni, the Cowichan valley, the north island) they are a coarse
model drawn out, and NRCan's HRDEM mosaic (hrdem.py) covers little more. The Province publishes its lidar programme's
DEMs openly (Open Government Licence - British Columbia; https://lidar.gov.bc.ca) as GeoTIFFs in a public bucket:
https://nrs.objectstore.gov.bc.ca/gdwuts/<NTS 1:250k>/<sheet>/<year>/dem/<file>.tif, about 1.5 x 1.5 km each
(newer: striped, LZW, float32, UTM 9 / 10 + CGVD2013; older: tiled, Deflate, UTM or BC Albers).

INDEX_PATH (lidarbc_index.json.gz, built by tools/build_lidarbc_index.py) lists the files of the sheets over and
around Vancouver Island with their CRS and bounds, so a tile finds its files without asking the bucket. Where files
of several flights overlap, the newest year comes first and older ones fill its gaps.

    lb = LidarBC(cache_dir, fetcher)
    got = lb.tile(z, x, y)      # None, or (heights float32 [512, 512], valid bool [512, 512]) on dem.py's grid

Only the file blocks under the tile's sample points are fetched (cog.Cog.sample_bilinear): a coarse tile over a
striped file reads only the strips of its rows. Blocks are cached on disk (rangeread), so a place is fetched once.
"""
import gzip
import json
import logging
import math
import os
import threading
from collections import OrderedDict

import numpy as np

import bcproj
import cog as cogmod
import rangeread

log = logging.getLogger("earthd.lidarbc")

INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lidarbc_index.json.gz")
DSM_INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lidarbc_dsm_index.json.gz")
SPARSE_TILE_MAX = 256 * 1024   # bytes (decoded) a sparse reader's point may cost in a tiled file
WIDE_M = 3000.0             # a file wider than this is a whole map sheet's (the small tiles are ~1.9 km)
SPIKE_M = 15.0              # m over the neighbourhood's median: a spike, not a tree crown
TREE_CAP_M = 95.0
CHM_MAX = 254               # uint8 metres (255: nodata, as the Meta / WRI canopy model)
BASE_URL = "https://nrs.objectstore.gov.bc.ca/gdwuts/"
HOST = "nrs.objectstore.gov.bc.ca"
TILE = 512
MIN_ZOOM = 14               # coarser tiles (far terrain) keep Mapterhorn: a z13 tile would touch ~40 files
NODATA_LIMIT = -1000.0
COGS = 256                  # parsed file headers kept
TILE_CACHE_BYTES = 128 * 1024 * 1024        # decoded strips kept (a server process each)


def nts_box(sheet):
    """(south, west, north, east) of an NTS 1:250,000 sheet of western Canada ("092F": series 82-114, letters A-P in
    the serpentine 4 x 4 order, A at the south-east, 1 deg x 2 deg each), or None."""
    m = __import__("re").fullmatch(r"(\d{2,3})([A-Pa-p])", sheet.strip())
    if not m:
        return None
    nnn, letter = int(m.group(1)), "ABCDEFGHIJKLMNOP".index(m.group(2).upper())
    east = -(8 * (nnn // 10) + 48)
    south = 48 + 4 * (nnn % 10 - 2)
    row, k = divmod(letter, 4)
    col = k if row % 2 == 0 else 3 - k              # columns counted westward from the east edge
    return (south + row, east - 2 * (col + 1), south + row + 1, east - 2 * col)


class LidarBC:
    def __init__(self, cache_dir, fetcher, index_path=INDEX_PATH, block=None, tile_cache_bytes=TILE_CACHE_BYTES):
        """block: the disk cache's block size (rangeread; None: its default, 256 kB). A sparse reader (a point every
        ~100 m) uses small blocks, so a sample row costs ~16 kB, not the 36 rows around it."""
        self.cache_dir, self.fetcher, self.block = cache_dir, fetcher, block
        self._lock = threading.Lock()
        self._cogs = OrderedDict()
        self._tiles = cogmod.TileCache(tile_cache_bytes)
        self._index_path = index_path
        self._idx = None
        self.stats = {"tiles": 0, "empty": 0, "files": 0}

    # --- the index ------------------------------------------------------------------------------------------------
    def _index(self):
        if self._idx is None:
            with self._lock:
                if self._idx is None:
                    sheets = []
                    try:
                        with gzip.open(self._index_path, "rt", encoding="utf-8") as f:
                            doc = json.load(f)
                        files, sheets = doc["files"], doc.get("sheets", [])
                    except (OSError, ValueError, KeyError) as e:
                        log.warning("LidarBC index unavailable (%s): BC lidar off", e)
                        files = []
                    keys = [r[0] for r in files]
                    epsg = np.array([r[1] for r in files], np.int64)
                    box = np.array([r[2:6] for r in files], np.float64).reshape(-1, 4)
                    year = np.array([r[6] for r in files], np.int64)
                    # the index's overall lat / lon box (a cheap test before projecting anything)
                    ll = self._latlon_box(sheets) if files else (0.0, 0.0, 0.0, 0.0)
                    self._idx = (keys, epsg, box, year, ll)
        return self._idx

    @staticmethod
    def _latlon_box(sheets):
        """(south, west, north, east) around the index's NTS sheets (0.1 deg margin; all of BC without sheets)."""
        boxes = [nts_box(sh) for sh in sheets]
        boxes = [b for b in boxes if b is not None]
        if not boxes:
            return (48.0, -139.1, 60.0, -114.0)
        return (min(b[0] for b in boxes) - 0.1, min(b[1] for b in boxes) - 0.1,
                max(b[2] for b in boxes) + 0.1, max(b[3] for b in boxes) + 0.1)

    def covers(self, z, x, y):
        if z < MIN_ZOOM:
            return False
        s, w, n, e = self._index()[4]
        n2 = 2 ** z
        lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n2))))     # noqa: E731
        ts, tn = lat(y + 1), lat(y)
        tw, te = x / n2 * 360.0 - 180.0, (x + 1) / n2 * 360.0 - 180.0
        return ts < n and tn > s and tw < e and te > w

    # --- files ----------------------------------------------------------------------------------------------------
    def _cog(self, key, level=0):
        ck = key if level == 0 else (key, level)
        with self._lock:
            if ck in self._cogs:
                self._cogs.move_to_end(ck)
                return self._cogs[ck]
        remote = rangeread.RemoteFile(BASE_URL + key, None, os.path.join(self.cache_dir, key.replace("/", "_")),
                                      self.fetcher, block=self.block)
        try:
            c = cogmod.Cog(remote, "demand", level=level, tiles=self._tiles)
            if c.sx is None:
                raise cogmod.CogError(f"LidarBC {key}: no georeferencing")
        except rangeread.Missing:
            c = None                                    # (gone from the bucket since the index was built)
        except cogmod.CogError as e:
            if level > 0 and "no overview" in str(e):
                c = None
            else:
                raise
        with self._lock:
            self._cogs[ck] = c
            self.stats["files"] += 1
            while len(self._cogs) > COGS:
                self._cogs.popitem(last=False)
        return c

    def tile(self, z, x, y, size=TILE):
        """Heights and their validity on dem.py's tile (z, x, y), or None where no LidarBC file has data there.
        Network failures raise netfetch.FetchError."""
        if not self.covers(z, x, y):
            return None
        n = 2 ** z
        i = (np.arange(size) + 0.5) / size
        lon1 = (x + i) / n * 360.0 - 180.0
        lat1 = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + i) / n))))
        lat = np.broadcast_to(lat1[:, None], (size, size))
        lon = np.broadcast_to(lon1[None, :], (size, size))
        got = self.sample(lat, lon)
        with self._lock:
            self.stats["tiles"] += 1
            if got is None:
                self.stats["empty"] += 1
        return got

    def sparse(self, box, step_m=64.0, coarse_m=None):
        """sparse(self, ...): ground points along the files' rows over a lat / lon box."""
        return sparse(self, box, step_m, coarse_m)

    def sample(self, lat, lon, files=None, max_strip_m=None, coarse_m=None):
        """Values (m) at WGS84 points (any shape) and their validity, newest flight first, older ones filling its
        gaps; None where no file has data. files: a list the keys of the files used are appended to. max_strip_m:
        striped files wider than this are skipped (a whole 1:20,000 sheet in full-width rows, ~11 km: every row
        read for a dense grid is ~35 kB; the canopy's 1.2 m grid over one would read ~100 MB a cell). coarse_m: a
        sparse grid's points may come from a tiled file's overview with pixels up to this size (the older flights
        have 2x / 4x ones: a point then reads a 2 or 4 m tile, not a 1 MB one of 1 m pixels)."""
        keys, epsg, box, year, _ = self._index()
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        shape = np.broadcast(lat, lon).shape
        out = np.zeros(shape, np.float32)
        ok = np.zeros(shape, bool)
        cands = []
        proj = {}
        # the points' lat / lon box's border (40 points), projected first: CRSs without a file near skip the full grid
        la0, la1 = float(lat.min()), float(lat.max())
        lo0, lo1 = float(lon.min()), float(lon.max())
        t = np.linspace(0.0, 1.0, 10)
        blat = np.concatenate([la0 + (la1 - la0) * t, la0 + (la1 - la0) * t, np.full(10, la0), np.full(10, la1)])
        blon = np.concatenate([np.full(10, lo0), np.full(10, lo1), lo0 + (lo1 - lo0) * t, lo0 + (lo1 - lo0) * t])
        for code in np.unique(epsg):
            code = int(code)
            if not bcproj.supported(code):
                continue
            bx, by = bcproj.forward(code, blat, blon)
            near = ((epsg == code) & (box[:, 0] < bx.max() + 50) & (box[:, 2] > bx.min() - 50)
                    & (box[:, 1] < by.max() + 50) & (box[:, 3] > by.min() - 50))
            if not near.any():
                continue
            px, py = bcproj.forward_grid(code, np.broadcast_to(lat, shape), np.broadcast_to(lon, shape))
            px, py = np.broadcast_to(px, shape), np.broadcast_to(py, shape)
            proj[code] = (px, py)
            x0, x1, y0, y1 = px.min(), px.max(), py.min(), py.max()
            sel = np.nonzero((epsg == code) & (box[:, 0] < x1) & (box[:, 2] > x0) & (box[:, 1] < y1)
                             & (box[:, 3] > y0))[0]
            cands.extend(int(j) for j in sel)
        if not cands:
            return None
        # newest flight first; within a flight the small tiles before a whole sheet's file (cheaper to read)
        cands.sort(key=lambda j: (-year[j], box[j, 2] - box[j, 0] > WIDE_M, keys[j]))
        for j in cands:
            px, py = proj[int(epsg[j])]
            b = box[j]
            m = ~ok & (px >= b[0]) & (px <= b[2]) & (py >= b[1]) & (py <= b[3])
            if not m.any():
                continue
            if max_strip_m is not None and b[2] - b[0] > max_strip_m:
                c = self._cog(keys[j])
                if c is None or c.stripped:
                    continue
            else:
                c = self._cog(keys[j])
            if c is None:
                continue
            if coarse_m is not None and not c.stripped and c.sx < coarse_m:
                co = None
                for lv in range(int(math.log2(coarse_m / c.sx)), 0, -1):
                    co = self._cog(keys[j], lv)
                    if co is not None:
                        break
                if co is None and c.tile_w * c.tile_h * 4 > SPARSE_TILE_MAX:
                    continue            # (no overview: a sparse point would read a whole ~1 MB tile of 1 m pixels)
                c = co or c
            col, row = c.pixel_of(px[m], py[m])
            v, good = c.sample_bilinear(col - 0.5, row - 0.5)
            good &= v > NODATA_LIMIT
            if not good.any():
                continue
            idx = np.nonzero(m)
            gi = tuple(a[good] for a in idx)
            out[gi] = v[good]
            ok[gi] = True
            if files is not None:
                files.append(keys[j])
            if ok.all():
                break
        if not ok.any():
            return None
        return out, ok


def canopy(dem, dsm, lat, lon, files=None):
    """Lidar canopy height (m, the first-return surface minus the bare earth: trees, and roofs, as the Meta / WRI model
    counts them) at WGS84 points, and its validity; None where no surface model covers them. dem / dsm: LidarBC
    readers of the two products (the same flights mostly: their newest files first)."""
    s = dsm.sample(lat, lon, files, max_strip_m=WIDE_M)
    if s is None:
        return None
    hs, oks = s
    g = dem.sample(lat, lon, files, max_strip_m=WIDE_M)
    if g is None:
        return None
    hg, okg = g
    ok = oks & okg
    if not ok.any():
        return None
    h = np.clip(hs - hg, 0.0, float(CHM_MAX)).astype(np.float32)
    return despike(h, ok), ok


def despike(h, ok, spike_m=SPIKE_M, cap_m=TREE_CAP_M):
    """Canopy heights with lone spikes taken down to their 3 x 3 neighbourhood's median: a surface pixel over a cliff
    edge or a wire minus the ground beside it (Elk Falls' canyon read 105 m, 2026-10-23). Above cap_m (taller than any
    tree measured on the coast: the Carmanah Giant, ~95 m) also to the median. Grids only (2-D); pure."""
    if h.ndim != 2 or min(h.shape) < 3:
        return np.minimum(h, cap_m)
    p = np.pad(np.where(ok, h, np.nan), 1, mode="edge")
    n = np.stack([p[1 + dy:p.shape[0] - 1 + dy, 1 + dx:p.shape[1] - 1 + dx]
                  for dy in (-1, 0, 1) for dx in (-1, 0, 1)])
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # (all-nodata neighbourhoods)
        med = np.nanmedian(n, axis=0)
    med = np.where(np.isfinite(med), med, h)
    bad = ((h - med) > spike_m) | (h > cap_m)
    out = np.where(bad, np.minimum(med, cap_m), h)
    return out.astype(np.float32)


def _affine(src, dst):
    """Least-squares affine map from src points (n, 2) to dst (n, 2): a function of (m, 2) arrays."""
    A = np.column_stack([src, np.ones(len(src))])
    coef, *_ = np.linalg.lstsq(A, dst, rcond=None)
    return lambda p: np.column_stack([p, np.ones(len(p))]) @ coef


def sparse(lb, box, step_m=64.0, coarse_m=None):
    """Lidar ground points over a lat / lon box (south, west, north, east) a few km across, laid along the files' own
    rows (one row every step_m, a point every step_m / 2 along it) so a striped file costs one strip per row: a grid
    in Web Mercator falls on a different file row at every point (UTM's grid is turned ~1-3 degrees) and reads the
    whole file. Newest flight first; a point an older file gives where a newer one already gave its cell is kept too
    (the caller averages). Returns (lat, lon, height) arrays, possibly empty. The files' rows to lat / lon: an affine
    fit of the projection over the box (sub-metre over a few km)."""
    keys, epsg, bxs, year, _ = lb._index()
    s, w, n, e = box
    g = np.linspace(0.0, 1.0, 9)
    LA, LO = np.meshgrid(s + (n - s) * g, w + (e - w) * g, indexing="ij")
    lats, lons, hs = [], [], []
    order = sorted(range(len(keys)), key=lambda j: (-year[j], keys[j]))
    fits = {}
    for code in np.unique(epsg):
        code = int(code)
        if not bcproj.supported(code):
            continue
        X, Y = bcproj.forward(code, LA, LO)
        fits[code] = (float(X.min()), float(Y.min()), float(X.max()), float(Y.max()),
                      _affine(np.column_stack([X.ravel(), Y.ravel()]), np.column_stack([LA.ravel(), LO.ravel()])),
                      _affine(np.column_stack([LA.ravel(), LO.ravel()]), np.column_stack([X.ravel(), Y.ravel()])))
    for j in order:
        code = int(epsg[j])
        if code not in fits:
            continue
        x0, y0, x1, y1, to_ll, _ = fits[code]
        b = bxs[j]
        if b[0] > x1 or b[2] < x0 or b[1] > y1 or b[3] < y0:
            continue
        c = lb._cog(keys[j])
        if c is None:
            continue
        if not c.stripped and coarse_m is not None and c.sx < coarse_m:
            co = None
            for lv in range(int(math.log2(coarse_m / c.sx)), 0, -1):
                co = lb._cog(keys[j], lv)
                if co is not None:
                    break
            if co is None and c.tile_w * c.tile_h * 4 > SPARSE_TILE_MAX:
                continue
            c = co or c
        # the file's rows and columns inside the box (pixel corners at integers)
        cx0, ry1 = c.pixel_of(max(x0, b[0]), max(y0, b[1]))
        cx1, ry0 = c.pixel_of(min(x1, b[2]), min(y1, b[3]))
        rs = np.arange(math.ceil(float(ry0) / (step_m / c.sy)) * (step_m / c.sy), float(ry1), step_m / c.sy)
        cs = np.arange(float(cx0), float(cx1), step_m / 2 / c.sx)
        if len(rs) == 0 or len(cs) == 0:
            continue
        R, C = np.meshgrid(np.floor(rs) + 0.5, np.floor(cs) + 0.5, indexing="ij")
        v, ok = c.sample_nearest(C.ravel(), R.ravel())
        ok &= v > NODATA_LIMIT
        if not ok.any():
            continue
        X = c.x0 + C.ravel()[ok] * c.sx
        Y = c.y0 - R.ravel()[ok] * c.sy
        ll = to_ll(np.column_stack([X, Y]))
        inb = (ll[:, 0] >= s) & (ll[:, 0] <= n) & (ll[:, 1] >= w) & (ll[:, 1] <= e)
        lats.append(ll[inb, 0])
        lons.append(ll[inb, 1])
        hs.append(v[ok][inb])
    if not lats:
        return np.zeros(0), np.zeros(0), np.zeros(0, np.float32)
    return np.concatenate(lats), np.concatenate(lons), np.concatenate(hs)
