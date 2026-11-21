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
import dmath
import dnp

import fast
import hrdem
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
MIN_ZOOM = 15               # coarser tiles (far terrain) keep Mapterhorn, shifted (dem.py BIAS_*): a z13 tile would touch ~40 files
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
        lat = lambda yy: math.degrees(dmath.atan(dmath.sinh(math.pi * (1 - 2 * yy / n2))))     # noqa: E731
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
        lat, lon = hrdem.tile_lat_lon(z, x, y, size)
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
        fast.require(565, "LidarBC sample")
        lat, lon = np.broadcast_arrays(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
        return fast._ef.bc_sample(lat, lon, keys, epsg, box, year, bcproj, self._read_points, files,
                                  WIDE_M, max_strip_m, coarse_m)

    def _overview(self, key, c, coarse_m):
        if coarse_m is None or c.stripped or c.sx >= coarse_m:
            return c
        for lv in range(int(dmath.log2(coarse_m / c.sx)), 0, -1):
            overview = self._cog(key, lv)
            if overview is not None:
                return overview
        return c if c.tile_w * c.tile_h * 4 <= SPARSE_TILE_MAX else None

    def _read_points(self, key, x, y, skip_strips, coarse_m):
        c = self._cog(key)
        if c is None or (skip_strips and c.stripped):
            return None
        c = self._overview(key, c, coarse_m)
        if c is None:
            return None
        col, row = c.pixel_of(x, y)
        v, good = c.sample_bilinear(col - 0.5, row - 0.5)
        return np.asarray(v, np.float32), good & (v > NODATA_LIMIT)


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
    fast.require(565, "LidarBC canopy filter")
    return fast._ef.dem_despike(h if h.dtype == np.float32 else np.asarray(h, np.float64), ok, spike_m, cap_m)


def sparse(lb, box, step_m=64.0, coarse_m=None):
    """Lidar ground points over a lat / lon box (south, west, north, east) a few km across, laid along the files' own
    rows (one row every step_m, a point every step_m / 2 along it) so a striped file costs one strip per row: a grid
    in Web Mercator falls on a different file row at every point (UTM's grid is turned ~1-3 degrees) and reads the
    whole file. Newest flight first; a point an older file gives where a newer one already gave its cell is kept too
    (the caller averages). Returns (lat, lon, height) arrays, possibly empty. The files' rows to lat / lon: an affine
    fit of the projection over the box (sub-metre over a few km)."""
    keys, epsg, bxs, year, _ = lb._index()
    fast.require(565, "sparse LidarBC survey rows")
    return fast._ef.bc_sparse(lb, box, step_m, coarse_m, keys, epsg, bxs, year, bcproj, dnp.lstsq, NODATA_LIMIT)
