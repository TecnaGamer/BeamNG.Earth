"""Canada's lidar terrain model: NRCan's HRDEM mosaic (CanElevation series), 1 m bare earth.

Why: Mapterhorn answers z13-z15 tiles all over Vancouver Island, but outside the Capital Region they hold a coarse
model drawn out (Englishman River Falls, Strathcona: no feature under ~20 m, 2026-10-23), and dem.Dem takes any
tile deeper than z12 for national lidar, so those places got neither lidar nor the ground filter. The HRDEM mosaic
is airborne lidar with trees and buildings taken out (a DTM) at 1 m, for most of southern Canada.

Data (Open Government Licence - Canada; https://open.canada.ca/data/en/dataset/0fe65119-e96e-4a57-8bfe-9d9245fba06b):
Cloud-Optimised GeoTIFFs of 500 x 500 km on S3, in NAD83(CSRS) / Canada Atlas Lambert (EPSG:3979), heights in
metres above CGVD2013, float32, with overviews (2, 4, 8 ... m). Item "<i>_<j>" covers
x in [(i - 6) * 500 km, (i - 5) * 500 km), y in [(j - 3) * 500 km, (j - 2) * 500 km) (the STAC items' transforms,
datacube.services.geo.ca/stac/api, 2026-10-23); its DTM is ITEM_URL. North of the productive forest only a DSM
exists: the DTM reads nodata there and the tile keeps Mapterhorn's heights.

    h = Hrdem(cache_dir, fetcher)
    got = h.tile(z, x, y)       # None, or (heights float32 [512, 512], valid bool [512, 512]) on dem.py's grid

Reads go through netfetch (rangeread.RemoteFile: the file's blocks cached on disk, so a place is fetched once) and
the decoded file tiles through a cog.TileCache. Pure numpy otherwise: the Lambert projection is the EPSG formula
(conformal conic, two standard parallels, GRS80), checked against PROJ in tests/test_hrdem.py.

Horizontal datum: NAD83(CSRS) is taken as WGS84 (they differ by ~1-2 m on the west coast; below a 2 m terrain
lattice's own error). Vertical: CGVD2013 vs Mapterhorn's EGM2008 differ by decimetres; dem.py feathers the seams.
"""
import logging
import math
import os
import threading
from collections import OrderedDict

import numpy as np

import cog as cogmod
import rangeread

log = logging.getLogger("earthd.hrdem")

RELEASE = "mosaic-1m-2026"      # the cache folder's name: raise it when NRCan republishes the mosaic
ITEM_URL = "https://canelevation-dem.s3.ca-central-1.amazonaws.com/hrdem-mosaic-1m/{item}-mosaic-1m-dtm.tif"
HOST = "canelevation-dem.s3.ca-central-1.amazonaws.com"
ITEM_M = 500000.0
NODATA_LIMIT = -1000.0          # heights below this are nodata (the files' -32767, whatever the tag says)
# Where to ask at all: Canada's south (the DTM's extent), as lat / lon (south, west, north, east)
BOUNDS = (41.6, -141.1, 62.0, -52.5)
TILE = 512                      # dem.py's tile size
TILE_CACHE_BYTES = 256 * 1024 * 1024
MAX_LEVEL = 8                   # overviews asked down to 2^8 = 256 m pixels (dem.py asks z13 and deeper: <= 12 m)

# EPSG:3979: NAD83(CSRS) / Canada Atlas Lambert
_A = 6378137.0
_F = 1 / 298.257222101
_E = math.sqrt(_F * (2 - _F))
_LAT1, _LAT2, _LAT0, _LON0 = map(math.radians, (49.0, 77.0, 49.0, -95.0))


def _m(phi):
    return math.cos(phi) / math.sqrt(1 - (_E * math.sin(phi)) ** 2)


def _t(phi):
    s = np.sin(phi)
    return np.tan(np.pi / 4 - phi / 2) / ((1 - _E * s) / (1 + _E * s)) ** (_E / 2)


_N = (math.log(_m(_LAT1)) - math.log(_m(_LAT2))) / (math.log(float(_t(_LAT1))) - math.log(float(_t(_LAT2))))
_FF = _m(_LAT1) / (_N * float(_t(_LAT1)) ** _N)
_R0 = _A * _FF * float(_t(_LAT0)) ** _N


def lcc(lat, lon):
    """EPSG:3979 easting / northing (m) of WGS84 lat / lon (degrees; arrays)."""
    phi = np.radians(np.asarray(lat, np.float64))
    lam = np.radians(np.asarray(lon, np.float64))
    r = _A * _FF * _t(phi) ** _N
    th = _N * (lam - _LON0)
    return r * np.sin(th), _R0 - r * np.cos(th)


def item_of(x, y):
    """The mosaic item ("i_j") holding EPSG:3979 point (x, y)."""
    return f"{int(math.floor(x / ITEM_M)) + 6}_{int(math.floor(y / ITEM_M)) + 3}"


def tile_lat_lon(z, x, y, size=TILE):
    """Pixel-centre lat / lon grids [size, size] of Web-Mercator tile (z, x, y) on dem.py's grid (row 0 north)."""
    n = 2 ** z
    i = (np.arange(size) + 0.5) / size
    lon = (x + i) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + i) / n))))
    return np.broadcast_to(lat[:, None], (size, size)), np.broadcast_to(lon[None, :], (size, size))


def tile_bounds(z, x, y):
    """(south, west, north, east) of tile (z, x, y)."""
    n = 2 ** z
    lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))     # noqa: E731
    return lat(y + 1), x / n * 360.0 - 180.0, lat(y), (x + 1) / n * 360.0 - 180.0


def covers(z, x, y):
    """Whether tile (z, x, y) touches BOUNDS (where the mosaic is asked at all)."""
    s, w, n, e = tile_bounds(z, x, y)
    S, W, N, E = BOUNDS
    return s < N and n > S and w < E and e > W


class Hrdem:
    def __init__(self, cache_dir, fetcher):
        self.cache_dir, self.fetcher = cache_dir, fetcher
        self._cogs = OrderedDict()          # (item, level) -> Cog, or None: the file / overview does not exist
        self._lock = threading.Lock()
        self._tiles = cogmod.TileCache(TILE_CACHE_BYTES)
        self.stats = {"tiles": 0, "empty": 0, "errors": 0}

    def _cog(self, item, level):
        key = (item, level)
        with self._lock:
            if key in self._cogs:
                self._cogs.move_to_end(key)
                return self._cogs[key]
        url = ITEM_URL.format(item=item)
        remote = rangeread.RemoteFile(url, None, os.path.join(self.cache_dir, RELEASE, item), self.fetcher)
        try:
            c = cogmod.Cog(remote, "demand", level=level, tiles=self._tiles)
            if c.epsg not in (None, 3979) or c.sx is None:
                raise cogmod.CogError(f"HRDEM {item}: unexpected georeferencing (EPSG {c.epsg})")
        except rangeread.Missing:
            c = None                        # no such item (the sea, the far north)
        except cogmod.CogError as e:
            if level > 0 and "no overview" in str(e):
                c = None
            else:
                raise
        with self._lock:
            self._cogs[key] = c
            while len(self._cogs) > 64:
                self._cogs.popitem(last=False)
        return c

    def tile(self, z, x, y, size=TILE):
        """Heights and their validity on dem.py's tile (z, x, y), or None where the mosaic has nothing there.
        Network failures raise netfetch.FetchError (the caller keeps its own heights)."""
        if not covers(z, x, y):
            return None
        lat, lon = tile_lat_lon(z, x, y, size)
        ex, ny = lcc(lat, lon)
        # the tile's ground pixel (m) at its centre: the mosaic level that is as fine as that or one finer
        clat = math.radians(float(lat[size // 2, 0]))
        px = 40075016.686 * math.cos(clat) / 2 ** z / size
        want = max(0, min(MAX_LEVEL, int(math.floor(math.log2(max(px, 1.0))))))
        out = np.zeros((size, size), np.float32)
        ok = np.zeros((size, size), bool)
        ix = np.floor(ex / ITEM_M).astype(np.int64)
        iy = np.floor(ny / ITEM_M).astype(np.int64)
        code = (ix + 64) * 4096 + (iy + 64)             # (negative indices south / west of the origin)
        items = {f"{int(cd) // 4096 - 64 + 6}_{int(cd) % 4096 - 64 + 3}": code == cd for cd in np.unique(code)}
        for item, m in items.items():
            if not m.any():
                continue
            c = None
            level = want
            while level >= 0:
                c = self._cog(item, level)
                if c is not None or level == 0:
                    break
                level -= 1              # (fewer overviews than asked: the finest there is under it)
            if c is None:
                continue
            col, row = c.pixel_of(ex[m], ny[m])
            col, row = col - 0.5, row - 0.5             # pixel centres at integers
            c0, r0 = int(math.floor(col.min())) - 1, int(math.floor(row.min())) - 1
            c1, r1 = int(math.floor(col.max())) + 3, int(math.floor(row.max())) + 3
            win = c.read_window(c0, r0, c1 - c0, r1 - r0).astype(np.float32)
            bad = ~np.isfinite(win) | (win < NODATA_LIMIT)
            if c.nodata is not None:
                bad |= win == np.float32(c.nodata)
            if bad.all():
                continue
            u, v = col - c0, row - r0
            u0, v0 = np.floor(u).astype(np.int64), np.floor(v).astype(np.int64)
            fu, fv = (u - u0).astype(np.float32), (v - v0).astype(np.float32)
            a00, a01 = win[v0, u0], win[v0, u0 + 1]
            a10, a11 = win[v0 + 1, u0], win[v0 + 1, u0 + 1]
            good = ~(bad[v0, u0] | bad[v0, u0 + 1] | bad[v0 + 1, u0] | bad[v0 + 1, u0 + 1])
            h = (a00 * (1 - fu) + a01 * fu) * (1 - fv) + (a10 * (1 - fu) + a11 * fu) * fv
            out[m] = np.where(good, h, 0.0)
            ok[m] = good
        with self._lock:
            self.stats["tiles"] += 1
            if not ok.any():
                self.stats["empty"] += 1
        if not ok.any():
            return None
        return out, ok


def feather(valid, px):
    """Blend weights [0, 1] for a partly valid tile: 1 deeper than px pixels inside the valid area, falling linearly
    to 0 at its edge (seams between lidar and the coarse model become slopes, not steps). The tile's border counts as
    valid beyond it (neighbouring tiles agree there). Pure."""
    if valid.all():
        return np.ones(valid.shape, np.float32)
    px = max(1, int(px))
    w = valid.astype(np.float32)
    d = np.where(valid, float(px), 0.0).astype(np.float32)
    cur = valid.copy()
    for k in range(1, px + 1):          # distance (4-neighbour) to the invalid area, up to px
        p = np.pad(cur, 1, mode="edge")
        cur = cur & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
        ring = valid & ~cur & (d == px)
        d[ring] = k
        if not cur.any():
            break
    w = np.clip(d / px, 0.0, 1.0)
    return w.astype(np.float32)

