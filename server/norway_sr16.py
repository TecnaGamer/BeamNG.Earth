"""NIBIO SR16 forest-type overlay for BeamNG.Earth's vegetation generator.

SR16R is a 16 x 16 metre forest inventory. Its authoritative numerical GeoTIFF
values (1 spruce, 2 pine, 3 broadleaf) are downloadable via Geonorge. This
lightweight live adapter uses NIBIO's public SRVTRESLAG WMS portrayal instead,
so the game does not need a nationwide GeoTIFF download or GDAL installed.

The WMS's colours describe *dominant forest classes*, not individual trees.
Decoding is therefore deliberately conservative: uncertain or unmapped pixels
are UNKNOWN and the existing canopy-height vegetation generator wins.

Source: https://wms.nibio.no/cgi-bin/sr16 (NIBIO, NLOD licence).
"""
import io
import logging
import math
import os
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image, UnidentifiedImageError
import norway_outline


log = logging.getLogger("earthd.norway_sr16")
URL = "https://wms.nibio.no/cgi-bin/sr16"
LAYER = "SRVTRESLAG"
WMS_PX = 256
CACHE_VERSION = "sr16-wms-v1"
CACHE_MAX_AGE_S = 30 * 86400
RETRY_S = 300
MAX_BYTES = 5_000_000
TIMEOUT_S = 6
MERCATOR_R = 6378137.0

UNKNOWN, SPRUCE, PINE, BROADLEAF, CONIFER_MIX, MIXED, NOT_FOREST = range(7)
NAMES = ("unknown", "spruce", "pine", "broadleaf", "conifer_mix", "mixed", "not_forest")


def candidate_norway(lat, lon):
    """Accurate Norway land gate; no Sweden/Finland by rough envelope."""
    return bool(norway_outline.contains(lat, lon))


def mercator(lon, lat):
    lon = np.asarray(lon, np.float64)
    lat = np.clip(np.asarray(lat, np.float64), -85.0511287798066, 85.0511287798066)
    return np.deg2rad(lon) * MERCATOR_R, np.log(np.tan(np.pi / 4 + np.deg2rad(lat) / 2)) * MERCATOR_R


def classify_wms_rgba(image):
    """Decode SRVTRESLAG's categorical map colours; unknown shades fail closed.

    NIBIO's published legend names 7 discrete classes: green spruce, brown
    pine, light green conifer mixture, orange mixture, yellow broadleaf, grey
    unstocked, pale grey uncomputed. Thresholds tolerate small RGB differences
    in WMS renderers; a future raw GeoTIFF adapter can use exact codes.
    """
    rgb = np.asarray(image.convert("RGBA"), np.uint8)
    r, g, b, a = (rgb[:, :, i].astype(np.int16) for i in range(4))
    valid = a >= 220
    out = np.zeros(a.shape, np.uint8)
    lo = np.minimum(np.minimum(r, g), b)
    hi = np.maximum(np.maximum(r, g), b)

    # Non-vegetation shading and grey stand classes never spawn trees.
    grey = valid & ((hi - lo) <= 22) & (r >= 70) & (r <= 245)
    out[grey] = NOT_FOREST

    green = valid & (g > r * 1.12) & (g > b * 1.10) & (g >= 75) & ~grey
    pale = green & (r > 105) & (g > 135)
    out[green] = SPRUCE
    out[pale] = CONIFER_MIX

    warm = valid & (r > g * 1.08) & (g > b * 1.08) & (r >= 95) & ~grey
    orange = warm & (r >= 175) & (g >= 105)
    out[warm] = PINE
    out[orange] = MIXED

    yellow = valid & (r >= 150) & (g >= 135) & (b < g * 0.84) & (abs(r - g) <= 35)
    out[yellow] = BROADLEAF
    # Antialiased boundary pixels are only accepted if an interior RGB class
    # is clear. Transparent pixels stay UNKNOWN even when their RGB is bright.
    out[~valid] = UNKNOWN
    return out


class Tile:
    def __init__(self, classes, bbox_3857):
        self.classes = np.asarray(classes, np.uint8)
        self.xmin, self.ymin, self.xmax, self.ymax = bbox_3857
        self.h, self.w = self.classes.shape

    def sample(self, lon, lat):
        xx, yy = mercator(lon, lat)
        xx, yy = np.broadcast_arrays(xx, yy)
        out = np.zeros(xx.shape, np.uint8)
        ii = np.floor((xx - self.xmin) / (self.xmax - self.xmin) * self.w).astype(np.int64)
        jj = np.floor((self.ymax - yy) / (self.ymax - self.ymin) * self.h).astype(np.int64)
        inside = (ii >= 0) & (ii < self.w) & (jj >= 0) & (jj < self.h)
        out[inside] = self.classes[jj[inside], ii[inside]]
        return out


class SR16:
    def __init__(self, cache_dir, offline=False, fetch=None):
        self.enabled = os.environ.get("BNE_SR16", "1") != "0"
        self.offline = offline
        self.dir = Path(cache_dir) / "mapdata" / CACHE_VERSION if cache_dir else None
        self._fetch = fetch or self._http_fetch
        self._fail_until = {}
        self._lock = threading.Lock()  # one public WMS request at a time per server
        self.last_error = ""
        self.last_source = ""

    @staticmethod
    def _http_fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": "BeamNG.Earth-Norway-Foliage/0.2 (NIBIO SR16 WMS)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as response:
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("SR16 WMS response exceeded size limit")
        return data

    @staticmethod
    def url(bbox):
        params = {
            "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": LAYER,
            "STYLES": "", "FORMAT": "image/png", "TRANSPARENT": "TRUE", "CRS": "EPSG:3857",
            "BBOX": ",".join(f"{p:.2f}" for p in bbox), "WIDTH": WMS_PX, "HEIGHT": WMS_PX,
        }
        return URL + "?" + urllib.parse.urlencode(params)

    @staticmethod
    def decode(data):
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("SR16 WMS did not return a readable PNG image") from exc
        if im.format != "PNG" or im.size != (WMS_PX, WMS_PX):
            raise ValueError("unexpected SR16 image size or format")
        return classify_wms_rgba(im)

    def cell(self, x, y, bounds):
        """SR16 map for a z14 cell (bounds: west,south,east,north), or None on failure.

        The original CHM/WorldCover code remains the fallback if the service or
        its palette cannot be read. Network misses are not saved permanently.
        """
        if not self.enabled:
            return None
        west, south, east, north = bounds
        if not norway_outline.intersects_bbox(bounds):
            return None
        xmin, ymin = mercator(west, south)
        xmax, ymax = mercator(east, north)
        bbox = (float(xmin), float(ymin), float(xmax), float(ymax))
        path = self.dir / str(x) / f"{y}.png" if self.dir else None

        def cached():
            if not (path and path.is_file()):
                return None
            try:
                data = path.read_bytes()
                values = self.decode(data)
                fresh = time.time() - path.stat().st_mtime < CACHE_MAX_AGE_S
                if fresh or self.offline:
                    self.last_source = "cached"
                    return Tile(values, bbox)
            except (OSError, ValueError):
                log.warning("SR16: unusable cached tile %s; will try WMS", path)
            return None

        result = cached()
        if result is not None:
            return result
        if self.offline:
            self.last_error = "offline and no cached SR16 tile"
            return None
        key = (x, y)
        with self._lock:
            # Avoid duplicate downloads when two generation workers request a cell.
            result = cached()
            if result is not None:
                return result
            if self._fail_until.get(key, 0) > time.monotonic():
                return None
            try:
                data = self._fetch(self.url(bbox))
                classes = self.decode(data)
                # An all-transparent map is a valid blank tile (not forest), not
                # evidence of spruce; the forest-type overlay is optional there.
                if path:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                    try:
                        tmp.write_bytes(data)
                        os.replace(tmp, path)
                    finally:
                        tmp.unlink(missing_ok=True)
                self.last_source = "NIBIO SR16 WMS"
                self.last_error = ""
                return Tile(classes, bbox)
            except (OSError, ValueError, TimeoutError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._fail_until[key] = time.monotonic() + RETRY_S
                log.warning("SR16 WMS unavailable for %s: %s (canopy fallback)", key, self.last_error)
                return None
