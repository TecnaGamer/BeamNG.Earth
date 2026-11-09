"""Measured roofs for Germany: the shape and height of each building from the states' open LoD2 models (AdV CityGML).

Overture and OSM rarely know a German building's roof, so buildings.plan predicted it. tools/build_lod2_roofs.py reads
the states' tiles once and keeps one small record per building: where it stands, the AdV roofType, measuredHeight and
the height of the roof itself. The records are served as shards of a z10 tile (~25 km), <PAGES>/lod2/<x>/<y>.npz, and
fetched like the other shards of BeamNG.Earth-data (railtags.py). Nothing is parsed while playing.

A record joins the footprint it stands in, so the footprints stay the map's. What the map says itself wins: a mapped
roof:shape or height is kept. Outside Germany, where a shard is missing, or offline: no records, today's predicted
roofs. The sources and their licences are in CREDITS.md.
"""
import io
import logging
import math
import os
import threading
import time
import zipfile

import numpy as np

import netfetch
import railtags
from polygon import Path

log = logging.getLogger("earthd.lod2roofs")

ON = os.environ.get("BNE_LOD2_ROOFS", "on") != "off"
ZOOM = 10
RETRY_S = 3600                  # unreachable: asked again after this long
BOX = (47.2, 5.8, 55.1, 15.1)   # south, west, north, east: Germany, so nowhere else asks for a shard
CELL_M = 32.0                   # the grid a tile's records are looked up in
# AdV roofType -> roof:shape. 5000 (mixed), 9999 (other) and an empty code say nothing about the shape: predicted.
SHAPES = {1000: "flat", 2100: "skillion", 2200: "skillion", 3100: "gabled", 3200: "hipped", 3300: "half-hipped",
          3400: "mansard", 3500: "pyramidal", 3600: "pyramidal", 3700: "dome", 3800: "skillion", 3900: "round",
          4000: "pyramidal"}


def tile_of(lat, lon, zoom=ZOOM):
    """(x, y) of the web-mercator tile at lat, lon."""
    n = 1 << zoom
    r = math.radians(lat)
    return int((lon + 180.0) / 360.0 * n), int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)


def tiles_over(south, west, north, east):
    """[(x, y)] of the z10 tiles over a box, none outside Germany."""
    s, w, n, e = max(south, BOX[0]), max(west, BOX[1]), min(north, BOX[2]), min(east, BOX[3])
    if s > n or w > e:
        return []
    x0, y0 = tile_of(n, w)
    x1, y1 = tile_of(s, e)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


MISS_TTL_S = 7 * 86400     # s: a shard the site did not have (404) asked again after this


class Source:
    """The shards: each fetched once, kept on disk, and held in memory for the tiles around it."""

    def __init__(self, cache_dir, fetcher):
        self.base = f"{railtags.PAGES}/lod2"
        self.dir = os.path.join(cache_dir, "pagesdata", "lod2") if cache_dir else None
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.shards = {}            # (x, y) -> {lat, lon, code, height, roof} arrays, or {} none there
        self.down_at = 0.0

    def shard(self, x, y):
        with self.lock:
            got = self.shards.get((x, y))
        if got is not None:
            return got
        path = os.path.join(self.dir, f"{x}_{y}.npz") if self.dir else None
        body = None
        if path and os.path.exists(path):
            try:
                # (a shard the site didn't have, kept as an empty file, is asked again after MISS_TTL_S: before the
                # shards were published every German tile answered 404, and kept for good the roofs never came)
                stale_miss = os.path.getsize(path) == 0 and time.time() - os.path.getmtime(path) > MISS_TTL_S
                if not stale_miss:
                    with open(path, "rb") as fh:
                        body = fh.read()
            except OSError:
                body = None
        if body is None:
            if self.fetcher is None or time.time() - self.down_at < RETRY_S:
                return {}
            try:
                body = self.fetcher.get(f"{self.base}/{x}/{y}.npz", kind="demand").body
            except netfetch.NotFound:
                body = b""
            except Exception as e:      # (offline, or not published: the predicted roofs)
                log.debug("lod2 shard %s %s: %s: %s", x, y, type(e).__name__, e)
                self.down_at = time.time()
                return {}
            if path:
                os.makedirs(self.dir, exist_ok=True)
                with open(path + ".part", "wb") as fh:
                    fh.write(body)
                os.replace(path + ".part", path)
        got = decode(body)
        with self.lock:
            self.shards[(x, y)] = got
        return got

    def tile(self, frame, x0, y0, x1, y1):
        """The Records of an anchor-frame box, or None when there are none: a tile outside Germany costs nothing."""
        if not ON:
            return None
        m = 100.0
        lats, lons, _ = frame.enu_to_geodetic(np.array([x0 - m, x1 + m, x0 - m, x1 + m]),
                                              np.array([y0 - m, y0 - m, y1 + m, y1 + m]), 0.0)
        shards = [self.shard(x, y) for x, y in tiles_over(float(np.min(lats)), float(np.min(lons)),
                                                          float(np.max(lats)), float(np.max(lons)))]
        shards = [s for s in shards if s]
        if not shards:
            return None
        return Records(frame, shards, x0 - m, y0 - m, x1 + m, y1 + m)


def decode(body):
    """The arrays of a shard's bytes ({} empty or unreadable)."""
    if not body:
        return {}
    try:
        with np.load(io.BytesIO(body), allow_pickle=False) as z:
            return {"lat": z["lat"].astype(np.float64) / 1e6, "lon": z["lon"].astype(np.float64) / 1e6,
                    "code": z["code"].astype(np.int32), "height": z["height"].astype(np.float32) / 10.0,
                    "roof": z["roof"].astype(np.float32) / 10.0}
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as e:   # (EOFError: empty, BadZipFile: cut short)
        log.warning("lod2 shard unreadable: %s", e)
        return {}


class Records:
    """A tile's records in its frame, found by the footprint they stand in."""

    def __init__(self, frame, shards, x0, y0, x1, y1):
        e, n, code, height, roof = [], [], [], [], []
        for s in shards:
            k = (s["lat"] > -90.0)
            ee, nn, _ = frame.geodetic_to_enu(s["lat"][k], s["lon"][k], 0.0)
            keep = (ee >= x0) & (ee < x1) & (nn >= y0) & (nn < y1)
            e.append(ee[keep])
            n.append(nn[keep])
            code.append(s["code"][k][keep])
            height.append(s["height"][k][keep])
            roof.append(s["roof"][k][keep])
        self.e, self.n = np.concatenate(e), np.concatenate(n)
        self.code, self.height, self.roof = np.concatenate(code), np.concatenate(height), np.concatenate(roof)
        self.grid = {}
        for i, key in enumerate(zip((self.e // CELL_M).astype(np.int64).tolist(),
                                    (self.n // CELL_M).astype(np.int64).tolist())):
            self.grid.setdefault(key, []).append(i)

    def near(self, e, n):
        """Indices of the records in the cells of the box of a ring."""
        out = []
        for gx in range(int(e.min() // CELL_M), int(e.max() // CELL_M) + 1):
            for gy in range(int(n.min() // CELL_M), int(n.max() // CELL_M) + 1):
                out += self.grid.get((gx, gy), ())
        return out

    def find(self, e, n):
        """The index of the largest-roofed record standing in the ring (e, n), or None. Parts of one building are
        records of their own: the tallest says what the building is."""
        idx = self.near(e, n)
        if not idx:
            return None
        ii = np.array(idx)
        inside = Path(np.stack([e, n], 1)).contains_points(np.stack([self.e[ii], self.n[ii]], 1))
        if not inside.any():
            return None
        ii = ii[inside]
        return int(ii[np.argmax(self.height[ii])])

    def fill(self, at, e, n):
        """at (a building's attributes) with what the measured record adds; the map's own values stay."""
        i = self.find(e, n)
        if i is None:
            return at
        shape = SHAPES.get(int(self.code[i]))
        if shape and not at.get("roof_shape"):
            at["roof_shape"] = shape
            if self.roof[i] > 0 and not _known(at.get("roof_height")):
                at["roof_height"] = float(self.roof[i])
        if self.height[i] > 0 and not _known(at.get("height")):
            at["height"] = float(self.height[i])
        return at


def _known(v):
    return v is not None and np.isfinite(v)
