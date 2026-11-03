"""The ground past Web Mercator's +-85.05 deg (the user, 2026-10-22: "add polar support"): the Copernicus DEM.

Mapterhorn and every other tiled source stop at Web Mercator's limit (mercator.MAX_LAT, 85.05 deg; Mapterhorn's
heights a little before 85.0, ~11 m short at its deeper zooms: a trench to sea level ran round Antarctica), so the
polar caps were flat at sea level: right for the Arctic Ocean round the North Pole (sea ice, no Copernicus files: 0 m),
wrong for Antarctica (the South Pole is on the ice sheet at 2835 m). dem.sample takes the ground from FROM_LAT on;
Mapterhorn's Antarctic heights are the Copernicus DEM's too (within a metre at 84.5-85 S), so the two meet without a
step. REMA (2 m, PGC) has no data within ~100 km of the South Pole (no stereo imagery that far south); the Copernicus
DEM (GLO-30, ESA / Airbus, free and open) covers it.

Files: one Cloud-Optimised GeoTIFF per 1 x 1 deg (s3://copernicus-dem-30m, named by their south-west corner),
float32 metres (EGM2008), Deflate with the floating-point predictor, PixelIsPoint: pixel (0, 0) is centred on the
file's north-west corner, rows every 1" of latitude; columns every 10" of longitude in files past 85 deg, every 5" in
those of 80-85 deg (BANDS). The files of a band join into one global grid of pixel centres, (90 - lat) * 3600 rows,
(lon + 180) * columns per degree; a point is sampled in the band of the file its row is in (85.0 N is the top row of
N84's, 85.0 S of S86's), its mosaic kept to that band's rows. Each file holds overviews 1-3 (block means of 2, 4 and
8 pixels each way: 60, 120 and 240 m north-south).

sample(lat, lon, pixel_m) reads the finest level not finer than pixel_m, or the coarsest when the points span more than
MAX_FILES files (round a pole every file of the last degree, 360 of them: its overview 3 and the header are the first
54 KB of each file, read in BLOCK-byte blocks), mosaics the windows of the files under the points (and the column
past the antimeridian), and interpolates bilinearly (earthfast.bilinear_grid). Files that do not exist (the ocean;
tileList.txt, fetched once) are 0 m.
"""
import logging
import math
import os
import threading
from collections import OrderedDict
from runtimeprofiler import ThreadPoolExecutor

import numpy as np

import cog as cogmod
import fast
import rangeread

log = logging.getLogger("earthd.polardem")

FROM_LAT = 84.9             # |lat| from which dem.sample reads this, inside Mapterhorn's last good rows
SOURCE = "copernicus-glo30"         # the data credits' id (credits.py)
BASE = "https://copernicus-dem-30m.s3.amazonaws.com/"
NAME = "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM"
TILE_LIST = BASE + "tileList.txt"
ROWS_PER_DEG = 3600                         # level 0, every band
LEVELS = 4                                  # 0: full resolution, 1-3: the overviews
LAT_PX_M = 6371008.8 * math.pi / 180 / ROWS_PER_DEG     # ~30.9 m north-south at level 0
MAX_FILES = 24              # points over more files than this: the coarsest level (round a pole: 360 files)
MAX_MOSAIC_PX = 16 << 20    # pixels of one mosaic (64 MB); larger spans are sampled in parts
BLOCK = 64 * 1024           # range-read blocks: the header and overview 3 of a file are its first ~54 KB
COG_CACHE = 1024            # open files (headers) kept per level
READERS = 16                # files read at once (the first visit round a pole reads 360)
TILE_BYTES = 192 << 20      # decoded tiles kept (all 360 overview-3 tiles of the ring round a pole: 29 MB)


def file_name(lat_sw, lon_sw):
    """The Copernicus file whose south-west corner is (lat_sw, lon_sw) (whole degrees)."""
    return NAME.format(ns="N" if lat_sw >= 0 else "S", lat=abs(lat_sw), ew="E" if lon_sw >= 0 else "W",
                       lon=abs(lon_sw))


def band_cols(lat_sw):
    """Columns per degree (level 0) of the files whose south-west corner is at latitude lat_sw: 10" past 85 deg, 5"
    in 80-85 deg (the Copernicus DEM's other bands are not read here)."""
    if lat_sw >= 85 or lat_sw <= -86:
        return 360
    if 80 <= lat_sw <= 84 or -85 <= lat_sw <= -81:
        return 720
    raise ValueError(f"latitude {lat_sw} is outside the polar bands")


def file_lat(lat):
    """The south-west latitude of the file whose rows hold lat (the file of row floor((90 - lat) * 3600); the South
    Pole's: the last row's)."""
    row = np.floor((90.0 - np.asarray(lat, np.float64)) * ROWS_PER_DEG).astype(np.int64)
    return 89 - np.clip(row, 0, 180 * ROWS_PER_DEG - 1) // ROWS_PER_DEG


def level_for(pixel_m):
    """The finest level whose pixels (north-south) are not finer than pixel_m needs."""
    if not pixel_m or pixel_m <= LAT_PX_M:
        return 0
    return int(min(LEVELS - 1, math.floor(math.log2(pixel_m / LAT_PX_M))))


def global_px(lat, lon, level, cpd=360):
    """Fractional global pixel coordinates (column, row) of lat / lon in a band's grid (cpd columns per degree) at
    level: level 0 has pixel centres on whole arc seconds (PixelIsPoint), an overview pixel j is the mean of level-0
    pixels j * f .. j * f + f - 1, centred at j * f + (f - 1) / 2."""
    f = 1 << level
    gx = (np.asarray(lon, np.float64) + 180.0) * cpd
    gy = (90.0 - np.asarray(lat, np.float64)) * ROWS_PER_DEG
    return (gx - (f - 1) / 2.0) / f, (gy - (f - 1) / 2.0) / f


class PolarDem:
    """raw_dir: the range-read block cache root; fetcher: the server's netfetch.Fetcher."""

    def __init__(self, raw_dir, fetcher):
        self.dir, self.fetcher = os.path.join(raw_dir, "copernicus-dem-30m"), fetcher
        self._cogs = OrderedDict()          # (name, level) -> cog.Cog
        self._lock = threading.Lock()
        self._files = None                  # names of the files that exist from 84 deg on (tileList.txt)
        self._pool = ThreadPoolExecutor(READERS, thread_name_prefix="polardem")
        self._tiles = cogmod.TileCache(TILE_BYTES)     # decoded file tiles (a level-0 tile: 1.4 MB)

    # --- files ------------------------------------------------------------------------------------
    def _existing(self):
        """The names of the cap files that exist (the list is fetched once and kept on disk)."""
        if self._files is not None:
            return self._files
        path = os.path.join(self.dir, "tileList.txt")
        try:
            with open(path, encoding="ascii") as f:
                text = f.read()
        except OSError:
            text = self.fetcher.get(TILE_LIST, kind="demand").body.decode("ascii")
            os.makedirs(self.dir, exist_ok=True)
            with open(path + ".tmp", "w", encoding="ascii") as f:
                f.write(text)
            os.replace(path + ".tmp", path)
        names = set()
        for line in text.split():
            parts = line.split("_")         # Copernicus_DSM_COG_10_S90_00_E000_00_DEM
            if len(parts) > 4 and parts[4][1:].isdigit():
                lat = int(parts[4][1:]) * (1 if parts[4][0] == "N" else -1)
                if lat >= 84 or lat <= -85:      # (S85: 85 to 84 S)
                    names.add(line.strip())
        self._files = names
        return names

    def _cog(self, name, level, cpd):
        key = (name, level)
        with self._lock:
            c = self._cogs.get(key)
            if c is not None:
                self._cogs.move_to_end(key)
                return c
        remote = rangeread.RemoteFile(BASE + f"{name}/{name}.tif", None, os.path.join(self.dir, name), self.fetcher,
                                      block=BLOCK)
        c = cogmod.Cog(remote, "demand", level=level, tiles=self._tiles)
        if c.width * (1 << level) != cpd or c.height * (1 << level) != ROWS_PER_DEG:
            raise cogmod.CogError(f"Copernicus {name}: unexpected layout {c.width} x {c.height} at level {level}")
        with self._lock:
            self._cogs[key] = c
            while len(self._cogs) > COG_CACHE:
                self._cogs.popitem(last=False)
        return c

    def covers(self, lat, lon):
        """Whether any of the points lies on a file that exists (the data credits)."""
        lat = np.atleast_1d(np.asarray(lat, np.float64))
        lon = (np.atleast_1d(np.asarray(lon, np.float64)) + 180.0) % 360.0 - 180.0
        existing = self._existing()
        return any(file_name(int(a), int(o)) in existing
                   for a, o in set(zip(np.floor(lat).astype(int).tolist(), np.floor(lon).astype(int).tolist())))

    # --- sampling ---------------------------------------------------------------------------------
    def sample(self, lat, lon, pixel_m=None):
        """Heights (m, float64) at lat / lon arrays at or past FROM_LAT; 0 where no file exists."""
        lat = np.atleast_1d(np.asarray(lat, np.float64))
        lon = np.atleast_1d(np.asarray(lon, np.float64))
        out = np.zeros(lat.shape, np.float64)
        if lat.size == 0:
            return out
        lon = (lon + 180.0) % 360.0 - 180.0
        files = np.unique(np.floor(lat).astype(np.int64) * 1000 + np.floor(lon).astype(np.int64))
        level = level_for(pixel_m) if len(files) <= MAX_FILES else LEVELS - 1
        fl = file_lat(lat)
        for v in np.unique(fl).tolist():
            band_cols(v)                    # (points outside the polar bands: ValueError)
        cpd = np.where((fl >= 85) | (fl <= -86), 360, 720)
        for c in np.unique(cpd).tolist():   # one band and hemisphere at a time
            for north in (True, False):
                m = (cpd == c) & ((fl >= 0) == north)
                if m.any():
                    sub = np.zeros(int(m.sum()))
                    self._sample_into(sub, lat[m], lon[m], level, int(c), north)
                    out[m] = sub
        return out

    @staticmethod
    def _band_rows(cpd, north, level):
        """The band's global rows (first, last) at level."""
        f = 1 << level
        sw = [a for a in range(-90, 90) if (a >= 0) == north and band_rows_ok(a, cpd)]
        top, bottom = max(sw) + 1, min(sw)          # its files' north edge, south-west corner
        r0 = int(round((90 - top) * ROWS_PER_DEG))
        r1 = int(round((90 - bottom) * ROWS_PER_DEG)) - 1
        return r0 // f, (r1 + 1) // f - 1

    def _sample_into(self, out, lat, lon, level, cpd, north):
        f = 1 << level
        cols = 360 * cpd // f                       # the level's global grid
        b0, b1 = self._band_rows(cpd, north, level)
        gx, gy = global_px(lat, lon, level, cpd)
        gx = gx % cols                      # (west of the first centre: wraps to the last columns)
        # one mosaic over the points' rows (within the band); its columns: the points' span, or the whole circle (plus
        # a wrapped column) when they surround a pole or straddle the antimeridian
        r0 = max(b0, min(b1 - 1, int(math.floor(gy.min()))))
        r1 = max(r0 + 1, min(b1, int(math.floor(gy.max())) + 1))     # (the pole's row: the last two rows)
        c0, c1 = int(math.floor(gx.min())), int(math.floor(gx.max())) + 1
        if c1 - c0 + 1 > cols // 2:
            c0, c1 = 0, cols                # all columns; column `cols` repeats column 0
        if (r1 - r0 + 1) * (c1 - c0 + 1) > MAX_MOSAIC_PX:
            low = gy <= np.median(gy)
            if low.any() and not low.all():         # in two parts, north and south
                for m in (low, ~low):
                    sub = np.zeros(int(m.sum()))
                    self._sample_into(sub, lat[m], lon[m], level, cpd, north)
                    out[m] = sub
                return
        mosaic = self._mosaic(level, cpd, r0, r1, c0, max(c1, c0 + 1), cols)
        if mosaic is None:
            return
        fast.require(55, "bilinear_grid")
        out[:] = fast._ef.bilinear_grid(mosaic, np.ascontiguousarray(gx - c0), np.ascontiguousarray(gy - r0))

    def _mosaic(self, level, cpd, r0, r1, c0, c1, cols):
        """Rows r0..r1 x columns c0..c1 (inclusive; columns modulo cols) of a band's global grid at level, float32,
        from the files under them (0 m where none exists); None when no file exists there at all."""
        f = 1 << level
        rpf, cpf = ROWS_PER_DEG // f, cpd // f      # pixels per file at this level
        existing = self._existing()
        out = np.zeros((r1 - r0 + 1, c1 - c0 + 1), np.float32)
        jobs = []
        for fr in range(r0 // rpf, r1 // rpf + 1):
            lat_sw = 90 - (fr + 1)
            for fcu in range(c0 // cpf, c1 // cpf + 1):
                name = file_name(lat_sw, fcu % (cols // cpf) - 180)
                if name in existing:
                    jobs.append((name, max(r0, fr * rpf), min(r1, (fr + 1) * rpf - 1), fr * rpf,
                                 max(c0, fcu * cpf), min(c1, (fcu + 1) * cpf - 1), fcu * cpf))

        def read(job):
            name, a0, a1, ra, b0, b1, cb = job
            try:
                win = self._cog(name, level, cpd).read_window(b0 - cb, a0 - ra, b1 - b0 + 1, a1 - a0 + 1)
            except (rangeread.RangeError, cogmod.CogError, OSError) as e:
                log.warning("polar DEM: %s: %s", name, e)
                return False
            out[a0 - r0:a1 - r0 + 1, b0 - c0:b1 - c0 + 1] = win
            return True

        if len(jobs) > 1:
            got = list(self._pool.map(read, jobs))
        else:
            got = [read(j) for j in jobs]
        return out if any(got) else None


def band_rows_ok(lat_sw, cpd):
    """Whether the files at lat_sw are in the band of cpd columns per degree."""
    try:
        return band_cols(lat_sw) == cpd
    except ValueError:
        return False
