"""Crop types per place from the EU crop map (JRC EUCROPMAP 2022, 10 m, CC BY 4.0).

Two Cloud-Optimised-style GeoTIFFs (tiled LZW BigTIFF, int32 codes, EPSG:3035 / ETRS89-LAEA) on the JRC open-data
FTP-over-HTTP server: the EU27 file (6.8 GB) and Ukraine's (1.0 GB). Neither is downloaded: the pixels under a
place are read with HTTP Range requests through netfetch (rangeread.RemoteFile + cog.Cog, as maprasters does for
WorldCover), kept on disk in blocks, and decoded in 512-px windows (5.1 km) held in a small memory LRU.

    CropMap(raw_dir, fetcher).sample(lat, lon, kind)   ->  uint8 crop group per point, 0 = none / not covered

The legend (JRC's own codes) is folded into the visual groups of GROUPS: what the plant looks like matters here,
not the botanical species (soft and durum wheat, barley, rye, oats all draw as one cereal).

Never waits on a refused request: a window the host policy defers (a prefetch with no room, an open breaker) reads
as "not covered" for now and is tried again next call; a hard failure is logged once and the place reads as none.
"""
import logging
import math
import os
import threading
from collections import OrderedDict

import numpy as np

import cog as cogmod
import netfetch
import rangeread

log = logging.getLogger("earthd.cropmap")

RELEASE = "eucropmap-2022-v1"
BASE_URL = "https://jeodpp.jrc.ec.europa.eu/ftp/public/JRC-OpenData/EUCROPMAP/2022/"
FILES = (("ua", "EU_CropMap_22_v1_stratum_UA-HR.tif"), ("eu27", "EU_CropMap_22_v1_stratum_EU27-HR.tif"))
WINDOW = 512                        # px: the unit of reading and of the memory cache (5.12 km at 10 m)
MEM_WINDOWS = 24                    # decoded windows kept (512² int32: 1 MB each as uint16 after folding: 0.5 MB)
# (lon0, lat0, lon1, lat1): where a file can have data; a cheap test before any request
COVERAGE = {"ua": (21.5, 44.0, 40.5, 52.6), "eu27": (-31.5, 27.0, 45.0, 72.0)}

# Visual groups (what the field looks like), 0 = not a field
(NONE, CEREAL, MAIZE, SUNFLOWER, RAPE, ROOT, FODDER, RICE, BARE, PULSE, GRASSLAND) = range(11)
GROUP_NAMES = ("none", "cereal", "maize", "sunflower", "rape", "root", "fodder", "rice", "bare", "pulse", "grass")
# GRASSLAND is not a crop: sample() hides it unless asked (grass=True), the Rust field filter knows the first ten groups only
# EUCROPMAP legend: 211 common wheat, 212 durum, 213 barley, 214 rye, 215 oats, 216 maize, 217 rice, 218 triticale,
# 219 other cereals, 221 potatoes, 222 sugar beet, 223 other root crops, 230 other industrial, 231 sunflower,
# 232 rape, 233 soya, 240 dry pulses, 250 fodder, 290 bare arable land. (100 artificial, 300 wood / shrub (with
# permanent crops), 500 grass (GRASSLAND), 600 bare, 700 water, 800 wetland: not arable.)
CODE_GROUP = {211: CEREAL, 212: CEREAL, 213: CEREAL, 214: CEREAL, 215: CEREAL, 218: CEREAL, 219: CEREAL,
              216: MAIZE, 217: RICE, 221: ROOT, 222: ROOT, 223: ROOT, 230: PULSE, 231: SUNFLOWER, 232: RAPE,
              233: PULSE, 240: PULSE, 250: FODDER, 290: BARE, 500: GRASSLAND}

ATTRIBUTION = ("Crop types: © European Union (JRC EUCROPMAP 2022), CC BY 4.0; changes: classes grouped, "
               "resampled to the ground")

_LUT = np.zeros(1024, np.uint8)
for _c, _g in CODE_GROUP.items():
    _LUT[_c] = _g


def fold(codes):
    """JRC class codes (any int array) -> visual group (uint8): CODE_GROUP, 0 for every other code."""
    a = np.asarray(codes)
    out = np.zeros(a.shape, np.uint8)
    ok = (a >= 0) & (a < len(_LUT))
    out[ok] = _LUT[a[ok].astype(np.int64)]
    return out


# ETRS89-LAEA (EPSG:3035): Lambert azimuthal equal-area on GRS80, centre 52 N 10 E, false origin 4321000, 3210000
_A = 6378137.0
_F = 1 / 298.257222101
_E2 = 2 * _F - _F * _F
_E = math.sqrt(_E2)
_LAT0, _LON0 = math.radians(52.0), math.radians(10.0)


def _q(s):
    return (1 - _E2) * (s / (1 - _E2 * s * s) - np.log((1 - _E * s) / (1 + _E * s)) / (2 * _E))


_QP = float(_q(1.0))
_Q0 = float(_q(math.sin(_LAT0)))
_RQ = _A * math.sqrt(_QP / 2)
_B0 = math.asin(_Q0 / _QP)
_D = _A * (math.cos(_LAT0) / math.sqrt(1 - _E2 * math.sin(_LAT0) ** 2)) / (_RQ * math.cos(_B0))


def laea(lat, lon):
    """(x, y) metres in EPSG:3035 of WGS84 degrees (numpy arrays or scalars; sub-millimetre against PROJ)."""
    p = np.radians(np.asarray(lat, np.float64))
    dl = np.radians(np.asarray(lon, np.float64)) - _LON0
    b = np.arcsin(np.clip(_q(np.sin(p)) / _QP, -1.0, 1.0))
    den = 1 + math.sin(_B0) * np.sin(b) + math.cos(_B0) * np.cos(b) * np.cos(dl)
    bb = _RQ * np.sqrt(2 / den)
    x = 4321000.0 + bb * _D * np.cos(b) * np.sin(dl)
    y = 3210000.0 + (bb / _D) * (math.cos(_B0) * np.sin(b) - math.sin(_B0) * np.cos(b) * np.cos(dl))
    return x, y


def covers(lat, lon):
    """Names of the files that can have data at the points (a quick bounding-box test)."""
    la0, la1 = float(np.min(lat)), float(np.max(lat))
    lo0, lo1 = float(np.min(lon)), float(np.max(lon))
    return [n for n, (a, b, c, d) in COVERAGE.items() if lo1 >= a and lo0 <= c and la1 >= b and la0 <= d]


class CropMap:
    """The crop map reader. raw_dir: the block cache root (<cache>/mapdata/raw); fetcher: netfetch.Fetcher."""

    def __init__(self, raw_dir, fetcher):
        self.raw_dir, self.fetcher = raw_dir, fetcher
        self._cogs = {}
        self._mem = OrderedDict()           # (file, wr, wc) -> uint8 groups (WINDOW², row 0 north)
        self._lock = threading.Lock()
        self._dead = set()                  # files that failed hard (logged once)

    def _cog(self, name, kind):
        with self._lock:
            c = self._cogs.get(name)
            if c is not None:
                return c
            if name in self._dead:
                return None
        fname = dict(FILES)[name]
        remote = rangeread.RemoteFile(BASE_URL + fname, None, os.path.join(self.raw_dir, "eucropmap", RELEASE, fname),
                                      self.fetcher)
        c = cogmod.Cog(remote, kind)
        if c.epsg != 3035:
            raise cogmod.CogError(f"EUCROPMAP {fname}: unexpected layout")
        with self._lock:
            self._cogs[name] = c
        return c

    def _window(self, name, c, wr, wc, kind):
        key = (name, wr, wc)
        with self._lock:
            w = self._mem.get(key)
            if w is not None:
                self._mem.move_to_end(key)
                return w
        w = fold(c.read_window(wc * WINDOW, wr * WINDOW, WINDOW, WINDOW))
        with self._lock:
            self._mem[key] = w
            while len(self._mem) > MEM_WINDOWS:
                self._mem.popitem(last=False)
        return w

    def sample(self, lat, lon, kind="background", grass=False):
        """uint8 groups at the points (arrays of equal shape; 0 where no file covers them or a read was refused). The crop
        groups only; with grass=True a 0 / 1 uint8: 1 where the map says grassland."""
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        out = np.zeros(lat.shape, np.uint8)
        names = covers(lat, lon)
        if not names:
            return out
        x, y = laea(lat, lon)
        for name in names:
            try:
                c = self._cog(name, kind)
                if c is None:
                    continue
                col, row = c.pixel_of(x, y)
                col, row = np.floor(col).astype(np.int64), np.floor(row).astype(np.int64)
                ok = (col >= 0) & (col < c.width) & (row >= 0) & (row < c.height) & (out == 0)
                if not ok.any():
                    continue
                wr, wc = row // WINDOW, col // WINDOW
                for key in np.unique(np.stack([wr[ok], wc[ok]], axis=1), axis=0):
                    sel = ok & (wr == key[0]) & (wc == key[1])
                    win = self._window(name, c, int(key[0]), int(key[1]), kind)
                    out[sel] = win[row[sel] - key[0] * WINDOW, col[sel] - key[1] * WINDOW]
            except (netfetch.Deferred, netfetch.PolicyDenied, netfetch.BudgetExceeded, netfetch.HostUnavailable,
                    netfetch.RateLimited):
                continue                    # not now: the photo is painted without it and refreshed later
            except Exception as e:          # noqa: BLE001 - a broken source must not break a block
                with self._lock:
                    first = name not in self._dead
                    self._dead.add(name)
                if first:
                    log.warning("crop map %s unavailable: %s", name, e)
        if grass:
            return (out == GRASSLAND).astype(np.uint8)
        out[out == GRASSLAND] = 0
        return out
