"""British Columbia's lidar-programme orthophotos (LidarBC), 10-15 cm, as an imagery source (imagery.py "bcortho").

Why (Vancouver Island, 2026-10-23): outside the regional services the ground had Sentinel-2's 10 m.
The Province publishes the orthophotos flown with its lidar (Open Government Licence - British Columbia) as GeoTIFFs
in https://nrs.objectstore.gov.bc.ca/gdwuts/<NTS>/<sheet>/<year>/orthophoto/. Only the streamable ones are indexed
(lidarbc_ortho_index.json.gz, tools/build_lidarbc_index.py --ortho): JPEG-compressed (2016: 10 cm, 2019 / 2020:
15 cm; the 2020 files tiled with overviews) and the 2020 50 cm mosaics with overviews. The uncompressed 2019 files
(~350 MB each, no overviews) would cost ~100 MB per square km of driving and are left out.

    bo = BCOrtho(cache_dir, fetcher)
    rgb, ok = bo.render(bbox, size)      # an EPSG:3857 box (minx, miny, maxx, maxy) as size x size RGB, and validity

Each output texel is the area mean of the photo's pixels under it (a box filter at the texel's size, then bilinear),
not a point sample: 0.78 m texels from 10 cm pixels would shimmer otherwise. Newest flight first; black fill (the
files' nodata) is skipped and the next file, then the next imagery source, fills in. Only the strips / tiles under
the box are read (cog.Cog over rangeread: cached on disk in blocks). The flights' colours vary (2020's 092K is
blue-green): imagery.render colour-matches lazy sources to Sentinel-2 at large scale.
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
import lidarbc
import rangeread

log = logging.getLogger("earthd.bcortho")

INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lidarbc_ortho_index.json.gz")
BASE_URL = lidarbc.BASE_URL
URL = BASE_URL + "#orthophoto"        # imagery's Source.url: the bucket's host (netfetch's policy), never fetched as is
ATTRIBUTION = "Contains information licensed under the Open Government Licence - British Columbia (LidarBC orthophotos)"
CHUNK = 256                 # output texels rendered per pass (bounded memory: a 200 m square of 10 cm pixels)
COGS = 64
TILE_CACHE_BYTES = 384 * 1024 * 1024
BLACK_MAX = 3               # a pixel this dark in every channel is the files' nodata fill (2012's: exact white)
MIN_VALID = 0.6             # a texel needs this share of its photo pixels valid
EARTH_R = 6378137.0


class BCOrtho:
    def __init__(self, cache_dir, fetcher, index_path=INDEX_PATH):
        self.cache_dir, self.fetcher = cache_dir, fetcher
        self._lock = threading.Lock()
        self._cogs = OrderedDict()
        self._tiles = cogmod.TileCache(TILE_CACHE_BYTES)
        self._index_path = index_path
        self._idx = None
        self.stats = {"renders": 0, "empty": 0}

    def _index(self):
        if self._idx is None:
            with self._lock:
                if self._idx is None:
                    try:
                        with gzip.open(self._index_path, "rt", encoding="utf-8") as f:
                            doc = json.load(f)
                        files, sheets = doc["files"], doc.get("sheets", [])
                    except (OSError, ValueError, KeyError) as e:
                        log.warning("LidarBC orthophoto index unavailable (%s): off", e)
                        files, sheets = [], []
                    self._idx = ([r[0] for r in files], np.array([r[1] for r in files], np.int64),
                                 np.array([r[2:6] for r in files], np.float64).reshape(-1, 4),
                                 np.array([r[6] for r in files], np.int64), np.array([r[7] for r in files], np.float64),
                                 lidarbc.LidarBC._latlon_box(sheets) if files else (0.0, 0.0, 0.0, 0.0))
        return self._idx

    def bounds(self):
        """(south, west, north, east) of the index's sheets."""
        return self._index()[5]

    def _cog(self, key, level):
        k = (key, level)
        with self._lock:
            if k in self._cogs:
                self._cogs.move_to_end(k)
                return self._cogs[k]
        remote = rangeread.RemoteFile(BASE_URL + key, None, os.path.join(self.cache_dir, key.replace("/", "_")),
                                      self.fetcher)
        try:
            c = cogmod.Cog(remote, "demand", level=level, tiles=self._tiles)
            if c.sx is None or c.bands < 3:
                c = None
        except rangeread.Missing:
            c = None
        except cogmod.CogError as e:
            if level > 0 and "no overview" in str(e):
                c = None
            else:
                raise
        with self._lock:
            self._cogs[k] = c
            while len(self._cogs) > COGS:
                self._cogs.popitem(last=False)
        return c

    def render(self, bbox, size):
        """bbox (EPSG:3857) as a size x size RGB image (row 0 north) and its validity; (None, None) where no file
        covers it. Network failures raise netfetch.FetchError."""
        keys, epsg, box, year, res, ll = self._index()
        if not keys:
            return None, None
        x0, y0, x1, y1 = bbox
        lat_n = math.degrees(math.atan(math.sinh(y1 / EARTH_R)))
        lat_s = math.degrees(math.atan(math.sinh(y0 / EARTH_R)))
        lon_w, lon_e = math.degrees(x0 / EARTH_R), math.degrees(x1 / EARTH_R)
        if lat_s > ll[2] or lat_n < ll[0] or lon_w > ll[3] or lon_e < ll[1]:
            return None, None
        out = np.zeros((size, size, 3), np.uint8)
        ok = np.zeros((size, size), bool)
        px = (x1 - x0) / size                                   # the texel, in Mercator metres
        ground = px * math.cos(math.radians((lat_n + lat_s) / 2))
        any_file = False
        for r0 in range(0, size, CHUNK):
            for c0 in range(0, size, CHUNK):
                n_r, n_c = min(CHUNK, size - r0), min(CHUNK, size - c0)
                mx = x0 + (c0 + np.arange(n_c) + 0.5) * px
                my = y1 - (r0 + np.arange(n_r) + 0.5) * px
                lon = np.broadcast_to(np.degrees(mx / EARTH_R)[None, :], (n_r, n_c))
                lat = np.broadcast_to(np.degrees(np.arctan(np.sinh(my / EARTH_R)))[:, None], (n_r, n_c))
                rgb, good, used = self._chunk(lat, lon, ground, keys, epsg, box, year, res)
                any_file |= used
                out[r0:r0 + n_r, c0:c0 + n_c] = rgb
                ok[r0:r0 + n_r, c0:c0 + n_c] = good
        with self._lock:
            self.stats["renders"] += 1
            if not ok.any():
                self.stats["empty"] += 1
        if not any_file:
            return None, None
        return out, ok

    def _chunk(self, lat, lon, ground, keys, epsg, box, year, res):
        shape = lat.shape
        rgb = np.zeros(shape + (3,), np.uint8)
        ok = np.zeros(shape, bool)
        cands, proj = [], {}
        for code in np.unique(epsg):
            code = int(code)
            if not bcproj.supported(code):
                continue
            px_, py_ = bcproj.forward(code, lat, lon)
            px_, py_ = np.broadcast_to(px_, shape), np.broadcast_to(py_, shape)
            sel = np.nonzero((epsg == code) & (box[:, 0] < px_.max()) & (box[:, 2] > px_.min())
                             & (box[:, 1] < py_.max()) & (box[:, 3] > py_.min()))[0]
            if len(sel):
                proj[code] = (px_, py_)
                cands.extend(int(j) for j in sel)
        if not cands:
            return rgb, ok, False
        cands.sort(key=lambda j: (-year[j], res[j], keys[j]))     # newest flight, then the finest
        for j in cands:
            px_, py_ = proj[int(epsg[j])]
            b = box[j]
            m = ~ok & (px_ >= b[0]) & (px_ <= b[2]) & (py_ >= b[1]) & (py_ <= b[3])
            if not m.any():
                continue
            # the overview whose pixels are at most half a texel (the 2020 files have them; others: level 0)
            level = max(0, int(math.floor(math.log2(max(ground / 2.0 / res[j], 1.0)))))
            c = None
            while level >= 0:
                c = self._cog(keys[j], level)
                if c is not None or level == 0:
                    break
                level -= 1
            if c is None:
                continue
            col, row = c.pixel_of(px_[m], py_[m])
            k = max(1, int(ground / c.sx))                      # photo pixels per texel: the box filter
            cmin, rmin = int(math.floor(col.min())) - k - 1, int(math.floor(row.min())) - k - 1
            cmax, rmax = int(math.ceil(col.max())) + k + 1, int(math.ceil(row.max())) + k + 1
            # whole boxes of k, aligned to the window
            w_, h_ = -(-(cmax - cmin) // k) * k, -(-(rmax - rmin) // k) * k
            win = c.read_window(cmin, rmin, w_, h_)
            if win.ndim != 3:
                continue
            valid = (win.max(axis=2) > BLACK_MAX) & (win.min(axis=2) < 255)    # black or exact-white fill: nodata
            if not valid.any():
                continue
            hb, wb = h_ // k, w_ // k
            v = valid.reshape(hb, k, wb, k).sum(axis=(1, 3)).astype(np.float32)
            s = (win.astype(np.float32) * valid[..., None]).reshape(hb, k, wb, k, 3).sum(axis=(1, 3))
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = s / v[..., None]
            frac = v / float(k * k)
            # bilinear in the box grid (box centres at integers)
            u = (col - cmin) / k - 0.5
            t = (row - rmin) / k - 0.5
            u0 = np.clip(np.floor(u).astype(np.int64), 0, wb - 2)
            t0 = np.clip(np.floor(t).astype(np.int64), 0, hb - 2)
            fu = np.clip(u - u0, 0, 1).astype(np.float32)[:, None]
            ft = np.clip(t - t0, 0, 1).astype(np.float32)[:, None]
            f00, f01, f10, f11 = frac[t0, u0], frac[t0, u0 + 1], frac[t0 + 1, u0], frac[t0 + 1, u0 + 1]
            good = np.minimum(np.minimum(f00, f01), np.minimum(f10, f11)) >= MIN_VALID
            if not good.any():
                continue
            val = ((mean[t0, u0] * (1 - fu) + mean[t0, u0 + 1] * fu) * (1 - ft)
                   + (mean[t0 + 1, u0] * (1 - fu) + mean[t0 + 1, u0 + 1] * fu) * ft)
            idx = np.nonzero(m)
            gi = (idx[0][good], idx[1][good])
            rgb[gi] = np.clip(np.rint(val[good]), 0, 255).astype(np.uint8)
            ok[gi] = True
            if ok.all():
                break
        return rgb, ok, True
