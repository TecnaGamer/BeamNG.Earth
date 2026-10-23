"""Raster map data per cell: ESA WorldCover 10 m land cover and Meta/WRI canopy height (CHMv2).

Both are Cloud-Optimised GeoTIFFs on S3, read with HTTP Range requests through netfetch
(rangeread.RemoteFile + cog.Cog: no GDAL); only the tiles under the cell are fetched.

WorldCover 2021 v200 (https://esa-worldcover.org, CC BY 4.0): EPSG:4326, 1/12000 deg pixels
(~9.3 m north-south), 3 x 3 deg files named by their south-west corner, 1024² Deflate tiles.
A cell's raster is the window of the *global* WorldCover pixel grid that covers the cell's lat/lon
box (whole pixels: floor of the west / north edge, ceil of the east / south edge), in native
pixels (no resampling), mosaicked over the 1-4 files it touches. Class 0 = no data (open ocean,
where no file exists, answers 404 and stays 0). Classes: CLASSES.

Canopy height CHMv2 (Meta and WRI, CC BY 4.0; s3://dataforgood-fb-data/forests/v2/global/
dinov3_global_chm_v2_ml3/chm/<quadkey10>.tif): EPSG:3857, 1.194 m pixels (the z17 Web-Mercator
pixel), uint8 metres, 32768² per file, 512² Deflate tiles. A z14 cell is exactly 2048 x 2048 of
its pixels (4 x 4 tiles of one file), so the cell's raster is that block. 255 is nodata (the
product README's caveat for the 10 m derivative; the 1.19 m files use it at their edges); a file
that does not exist (ocean, no coverage) gives no canopy raster for the cell.
"""
import math
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

import cog as cogmod
import lidarbc
import rangeread

WORLDCOVER_RELEASE = "v200-2021"
WORLDCOVER_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
                  "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
WORLDCOVER_PPD = 12000              # pixels per degree
WORLDCOVER_FILE_DEG = 3
CLASSES = {0: "no data", 10: "tree cover", 20: "shrubland", 30: "grassland", 40: "cropland",
           50: "built-up", 60: "bare / sparse vegetation", 70: "snow and ice",
           80: "permanent water bodies", 90: "herbaceous wetland", 95: "mangroves",
           100: "moss and lichen"}

CHM_RELEASE = "v2-ml3"
CHM_URL = ("https://dataforgood-fb-data.s3.amazonaws.com/forests/v2/global/dinov3_global_chm_v2_ml3/"
           "chm/{quadkey}.tif")
CHM_FILE_Z = 10                     # files are quadkey level 10 tiles
CHM_NODATA = 255
MERC_HALF = 20037508.342789244      # m, half the Web-Mercator world width
COG_CACHE = 16                      # parsed COG headers kept in memory
# British Columbia (Vancouver Island, 2026-10-23): where LidarBC flew both a surface (DSM) and a terrain
# (DEM) model, the canopy height is measured (DSM - DEM) instead of the Meta / WRI model's estimate (it saturates in
# tall coastal forest). Same grid, same uint8 metres; the Meta raster stays where lidar has nothing.
BC_CANOPY = os.environ.get("BNE_BC_CANOPY", "1") != "0"
BC_BAND = 512               # rows of the cell's canopy grid built at once


@dataclass
class Raster:
    """A north-up raster window: data[row, col]; pixel (col, row)'s top-left corner is at
    (x0 + col * dx, y0 - row * dy) in `crs` (EPSG:4326: x = lon, y = lat; EPSG:3857: metres)."""
    data: np.ndarray
    crs: str
    x0: float
    y0: float
    dx: float
    dy: float
    nodata: int | None = None
    files: list = field(default_factory=list)      # the source files it was read from (provenance)

    def sample(self, x, y):
        """Values at CRS points (nearest pixel; outside the window: nodata or 0)."""
        c = np.floor((np.asarray(x, np.float64) - self.x0) / self.dx).astype(np.int64)
        r = np.floor((self.y0 - np.asarray(y, np.float64)) / self.dy).astype(np.int64)
        h, w = self.data.shape
        ok = (c >= 0) & (c < w) & (r >= 0) & (r < h)
        out = np.full(np.shape(c), self.nodata if self.nodata is not None else 0, self.data.dtype)
        out[ok] = self.data[r[ok], c[ok]]
        return out


def cell_center(cell):
    """WGS84 lat / lon of a Web-Mercator cell's centre."""
    nz = 1 << cell.z
    lon = (cell.x + 0.5) / nz * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (cell.y + 0.5) / nz))))
    return lat, lon


def worldcover_tile_name(lat_sw, lon_sw):
    return f"{'N' if lat_sw >= 0 else 'S'}{abs(lat_sw):02d}{'E' if lon_sw >= 0 else 'W'}{abs(lon_sw):03d}"


def worldcover_window(bounds):
    """Global WorldCover pixel window (col0, row0, col1, row1) covering a lat/lon box: columns from
    -180 deg, rows from +90 deg."""
    lon0, lat0, lon1, lat1 = bounds
    p = WORLDCOVER_PPD
    return (int(math.floor((lon0 + 180.0) * p + 1e-9)), int(math.floor((90.0 - lat1) * p + 1e-9)),
            int(math.ceil((lon1 + 180.0) * p - 1e-9)), int(math.ceil((90.0 - lat0) * p - 1e-9)))


class Rasters:
    """Reads the per-cell rasters. raw_dir: the block cache root (<cache>/mapdata/raw); fetcher:
    the server's netfetch.Fetcher."""

    def __init__(self, raw_dir, fetcher, lidar_dir=None):
        """lidar_dir: the LidarBC block cache (the terrain's: <cache>/dem/lidarbc, so both read the same files once);
        None: no BC lidar canopy."""
        self.raw_dir, self.fetcher = raw_dir, fetcher
        self._bc = None
        if lidar_dir is not None and BC_CANOPY:
            mb = 48 * 1024 * 1024           # (a cell's band of strips: ~15 MB a product)
            self._bc = (lidarbc.LidarBC(lidar_dir, fetcher, tile_cache_bytes=mb),
                        lidarbc.LidarBC(lidar_dir, fetcher, index_path=lidarbc.DSM_INDEX_PATH, tile_cache_bytes=mb))
        self._cogs = OrderedDict()
        self._lock = threading.Lock()

    def _cog(self, source, release, name, url, kind):
        """The parsed COG of one file, or None if the file does not exist."""
        key = (source, name)
        with self._lock:
            c = self._cogs.get(key)
            if c is not None:
                self._cogs.move_to_end(key)
                return c
        remote = rangeread.RemoteFile(url, None, os.path.join(self.raw_dir, source, release, name), self.fetcher)
        try:
            c = cogmod.Cog(remote, kind)
        except rangeread.Missing:
            return None
        with self._lock:
            self._cogs[key] = c
            while len(self._cogs) > COG_CACHE:
                self._cogs.popitem(last=False)
        return c

    def worldcover(self, cell, kind="demand"):
        """The cell's land-cover Raster (uint8 classes, EPSG:4326 native pixels)."""
        col0, row0, col1, row1 = worldcover_window(cell.bounds())
        p, f = WORLDCOVER_PPD, WORLDCOVER_FILE_DEG * WORLDCOVER_PPD
        out = np.zeros((row1 - row0, col1 - col0), np.uint8)
        files = []
        for fr in range(row0 // f, (row1 - 1) // f + 1):
            for fc in range(col0 // f, (col1 - 1) // f + 1):
                lat_sw = 90 - (fr + 1) * WORLDCOVER_FILE_DEG
                lon_sw = fc * WORLDCOVER_FILE_DEG - 180
                name = worldcover_tile_name(lat_sw, lon_sw)
                c = self._cog("worldcover", WORLDCOVER_RELEASE, name, WORLDCOVER_URL.format(tile=name), kind)
                if c is None:
                    continue
                if c.dtype != np.uint8 or c.width != f or abs(c.sx - 1.0 / p) > 1e-12:
                    raise cogmod.CogError(f"WorldCover {name}: unexpected layout")
                files.append(name)
                a0, b0 = max(col0, fc * f), max(row0, fr * f)
                a1, b1 = min(col1, (fc + 1) * f), min(row1, (fr + 1) * f)
                win = c.read_window(a0 - fc * f, b0 - fr * f, a1 - a0, b1 - b0)
                out[b0 - row0:b1 - row0, a0 - col0:a1 - col0] = win
        return Raster(out, "EPSG:4326", col0 / p - 180.0, 90.0 - row0 / p, 1.0 / p, 1.0 / p, 0, files)

    def canopy(self, cell, kind="demand"):
        """The cell's canopy-height Raster (uint8 m, EPSG:3857, 2048² for a z14 cell), or None
        where no CHM file exists."""
        if cell.z < CHM_FILE_Z:
            raise ValueError("canopy height is read per cell of zoom >= 10")
        parent = cell.parent(CHM_FILE_Z)
        qk = parent.quadkey()
        c = self._cog("chm", CHM_RELEASE, qk, CHM_URL.format(quadkey=qk), kind)
        if c is None:
            return None
        n = 1 << cell.z
        size = 2 * MERC_HALF / n
        mx0, my0 = cell.x * size - MERC_HALF, MERC_HALF - cell.y * size
        fc, fr = c.pixel_of(mx0, my0)
        col0, row0 = int(round(float(fc))), int(round(float(fr)))
        w = int(round(size / c.sx))
        if abs(fc - col0) > 1e-3 or abs(fr - row0) > 1e-3 or c.dtype != np.uint8:
            raise cogmod.CogError(f"CHM {qk}: cell not aligned to the file's pixels")
        data = c.read_window(col0, row0, w, w)
        return Raster(data, "EPSG:3857", mx0, my0, c.sx, c.sy, CHM_NODATA, [qk])

    def bc_canopy(self, cell, base):
        """base (the Meta / WRI canopy Raster of the cell, or None) with LidarBC's measured canopy heights put in
        where lidar covers it; (raster, lidar files used). Network failures raise netfetch.FetchError."""
        if self._bc is None:
            return base, []
        dem, dsm = self._bc
        s, w, n, e = dsm._index()[4]
        clat, clon = cell_center(cell)
        if not (s <= clat <= n and w <= clon <= e):
            return base, []
        nz = 1 << cell.z
        size = 2 * MERC_HALF / nz
        px = 2 * MERC_HALF / (1 << (CHM_FILE_Z + 15))       # the CHM files' pixel (z17 Web Mercator, 1.194 m)
        npx = int(round(size / px))
        mx0, my0 = cell.x * size - MERC_HALF, MERC_HALF - cell.y * size
        i = (np.arange(npx) + 0.5) * px
        lon1 = np.degrees((mx0 + i) / 6378137.0)
        lat1 = np.degrees(np.arctan(np.sinh((my0 - i) / 6378137.0)))
        files = []
        data = None
        for r0 in range(0, npx, BC_BAND):            # in bands: a whole cell's 4 million points peaked at ~1 GB
            rows = slice(r0, min(npx, r0 + BC_BAND))
            n_r = rows.stop - rows.start
            lat = np.broadcast_to(lat1[rows, None], (n_r, npx))
            lon = np.broadcast_to(lon1[None, :], (n_r, npx))
            got = lidarbc.canopy(dem, dsm, lat, lon, files)
            if got is None:
                continue
            h, ok = got
            if data is None:
                data = (base.data.copy() if base is not None and base.data.shape == (npx, npx)
                        else np.full((npx, npx), CHM_NODATA, np.uint8))
            band = data[rows]
            band[ok] = np.rint(h[ok]).astype(np.uint8)
        if data is None:
            return base, []
        prev = list(base.files) if base is not None and base.data.shape == (npx, npx) else []
        files = sorted(set(files))
        return Raster(data, "EPSG:3857", mx0, my0, px, px, CHM_NODATA, prev + ["lidarbc:" + f for f in files]), files
