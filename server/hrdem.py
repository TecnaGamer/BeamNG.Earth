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
the decoded file tiles through a cog.TileCache. Rust projects the sample grid with the EPSG two-standard-parallel
Lambert formula on GRS80 and interpolates the valid mosaic pixels. tests/test_bclidar.py checks projected positions.

Horizontal datum: NAD83(CSRS) is taken as WGS84 (they differ by ~1-2 m on the west coast; below a 2 m terrain
lattice's own error). Vertical: CGVD2013 vs Mapterhorn's EGM2008 differ by decimetres; dem.py feathers the seams.
"""
import logging
import math
import os
import threading
from collections import OrderedDict

import numpy as np
import dmath
import fast
import bcproj

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
# ... less the US south of Canada: per 0.5-degree column of longitude from SOUTH_W0 east, the latitude below which no
# tile is asked: Canada's southernmost land in the column and 0.25 degrees either side, less 0.15 degrees (17 km),
# from Overture's borders (country_bands.bin, 0.01-degree samples; 2026-11-17). BOUNDS alone took in the northern US
# down to 41.6 N, and a tester's drive in Massachusetts asked the mosaic's files for every lidar tile. Tiles that
# reach within the margin are still asked, so a lidar project running past the border keeps its heights.
SOUTH_W0, SOUTH_STEP = -141.5, 0.5
SOUTH = (
    60.10, 60.05, 60.00, 60.00, 59.75, 59.40, 59.15, 58.75, 58.75, 58.85, 59.00, 59.35, 59.10, 58.70, 58.45,
    53.20, 52.70, 52.35, 51.95, 51.60, 51.35, 51.05, 50.80, 50.50, 50.40, 49.90, 49.65, 49.45, 49.05, 48.80,
    48.60, 48.35, 48.30, 48.20, 48.10, 48.05, 48.05, 48.10, 48.80, 48.80, 48.80, 48.85, 48.80, 48.80, 48.85,
    48.85, 48.85, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80,
    48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80,
    48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.80, 48.85, 48.85, 48.85, 48.85,
    48.80, 48.80, 48.80, 48.50, 48.35, 48.35, 48.35, 48.05, 48.00, 47.85, 47.85, 47.90, 47.80, 47.80, 47.80,
    47.85, 47.90, 47.70, 47.50, 47.30, 47.10, 46.90, 46.45, 46.30, 45.90, 45.50, 41.60, 41.60, 41.60, 41.60,
    41.80, 42.05, 42.10, 42.20, 42.35, 42.55, 43.45, 43.45, 43.45, 43.45, 43.55, 44.05, 44.35, 44.70, 44.80,
    44.80, 44.85, 44.85, 44.85, 44.85, 44.85, 45.05, 45.20, 45.75, 46.80, 47.00, 45.50, 44.95, 44.10, 43.10)
TILE = 512                      # dem.py's tile size
TILE_CACHE_BYTES = 256 * 1024 * 1024
LATTICE_M = 1.0                 # m: the terrain's lattice; finer mosaic pixels are not read (z16's 0.8 m: the 1 m overview)
MAX_LEVEL = 8                   # overviews asked down to 2^8 = 256 m pixels (dem.py asks z13 and deeper: <= 12 m)

def lcc(lat, lon):
    """EPSG:3979 coordinates on the NRCan mosaic."""
    return bcproj._conic(lat, lon, (49., 77.), (49., -95.), (0., 0.), hrdem=True)


def item_of(x, y):
    """The mosaic item ("i_j") holding EPSG:3979 point (x, y)."""
    return f"{int(math.floor(x / ITEM_M)) + 6}_{int(math.floor(y / ITEM_M)) + 3}"


def tile_lat_lon(z, x, y, size=TILE):
    """Pixel-centre lat / lon grids [size, size] of Web-Mercator tile (z, x, y) on dem.py's grid (row 0 north)."""
    fast.require(565, "regional DEM tile grid")
    lat, lon = fast._ef.dem_tile_axes(z, x, y, size)
    return np.broadcast_to(lat[:, None], (size, size)), np.broadcast_to(lon[None, :], (size, size))


def tile_bounds(z, x, y):
    """(south, west, north, east) of tile (z, x, y)."""
    n = 2 ** z
    lat = lambda yy: math.degrees(dmath.atan(dmath.sinh(math.pi * (1 - 2 * yy / n))))     # noqa: E731
    return lat(y + 1), x / n * 360.0 - 180.0, lat(y), (x + 1) / n * 360.0 - 180.0


def in_bounds(z, x, y):
    """Whether tile (z, x, y) touches the BOUNDS rectangle: dem.coarse_at's "the Canadian lidar reads here" (the road
    plans' coarse-elevation rules; kept as it was so no plan changes with covers' narrower ask, Codex review)."""
    s, w, n, e = tile_bounds(z, x, y)
    S, W, N, E = BOUNDS
    return s < N and n > S and w < E and e > W


def covers(z, x, y):
    """Whether tile (z, x, y) touches BOUNDS and reaches north of SOUTH (where the mosaic is asked at all)."""
    if not in_bounds(z, x, y):
        return False
    s, w, n, e = tile_bounds(z, x, y)
    a = math.floor((w - SOUTH_W0) / SOUTH_STEP)
    b = math.floor((e - SOUTH_W0) / SOUTH_STEP)
    if a < 0 or b >= len(SOUTH):
        return True                     # (past the table: west of Alaska's panhandle, east of Maine)
    return n > min(SOUTH[a:b + 1])


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

    def tile(self, z, x, y, size=TILE, min_px=LATTICE_M):
        """Heights and their validity on dem.py's tile (z, x, y), or None where the mosaic has nothing there.
        Network failures raise netfetch.FetchError (the caller keeps its own heights)."""
        if not covers(z, x, y):
            return None
        lat, lon = tile_lat_lon(z, x, y, size)
        ex, ny = lcc(lat, lon)
        fast.require(565, "HRDEM mosaic sampling")
        result = fast._ef.dem_hr_tile(ex, ny, lat[size // 2, 0], z, min_px, ITEM_M, MAX_LEVEL, NODATA_LIMIT, self._cog)
        with self._lock:
            self.stats["tiles"] += 1
            if result is None:
                self.stats["empty"] += 1
        return result


def feather(valid, px):
    """Blend weights [0, 1] for a partly valid tile: 1 deeper than px pixels inside the valid area, falling linearly
    to 0 at its edge (seams between lidar and the coarse model become slopes, not steps). The tile's border counts as
    valid beyond it (neighbouring tiles agree there). Pure."""
    fast.require(565, "regional DEM edge weights")
    return fast._ef.dem_feather(valid, max(1, int(px)))
