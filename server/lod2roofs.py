"""Measured roofs: the shape and height of each building from open national and state LoD2 building models.

Overture and OSM rarely know a building's roof, so buildings.plan predicts it. Where a country publishes its buildings
as LoD2 models, tools/build_lod2_roofs.py reads them once and keeps one small record per building: where it stands, the
roof shape as an AdV roofType code (read from the model, or from the roof faces by tools/lod2_classify.py where the
model has none), the measured height and the height of the roof itself. The records are served as shards of a z10 tile
(~25 km), one folder per country, <PAGES>/<folder>/<x>/<y>.npz (SOURCES), and fetched like the other shards of
BeamNG.Earth-data (railtags.py). Nothing is parsed while playing.

A record joins the footprint it stands in, so the footprints stay the map's. What the map says itself wins: a mapped
roof:shape or height is kept. Outside the countries' boxes, where a shard is missing, or offline: no records, today's
predicted roofs. The sources and their licences are in CREDITS.md; each country's credit shows near its shards.

  BNE_LOD2_ROOFS=off   no measured roofs
  BNE_LOD2_DIR         a local folder with <dir>/<folder>/<x>/<y>.npz instead of the site (testing a build before it
                       is published); nothing is fetched then
"""
import io
import logging
import math
import os
import threading
import time
import zipfile
from collections import namedtuple

import numpy as np
import dmath

import netfetch
import railtags
from polygon import Path

log = logging.getLogger("earthd.lod2roofs")

ON = os.environ.get("BNE_LOD2_ROOFS", "on") != "off"
ZOOM = 10
RETRY_S = 3600                  # unreachable: asked again after this long
CELL_M = 32.0                   # the grid a tile's records are looked up in

# A country's shards: its folder on the site (and in the cache), the box nothing outside of asks for one (south, west,
# north, east), and its credit (credits.FEATURES). A folder per country: each is built and published on its own, and
# the z10 tiles on a border (Basel, Konstanz) hold each country's records in its own shard.
Country = namedtuple("Country", "key folder box credit")
SOURCES = (
    Country("de", "lod2", (47.2, 5.8, 55.1, 15.1), "roof:lod2-de"),            # the German states (AdV)
    Country("ch", "lod2-ch", (45.8, 5.9, 47.85, 10.55), "roof:lod2-ch"),      # swissBUILDINGS3D: CH and Liechtenstein
    Country("ee", "lod2-ee", (57.45, 21.6, 59.85, 28.25), "roof:lod2-ee"),    # Estonia
    Country("wien", "lod2-wien", (48.11, 16.17, 48.33, 16.58), "roof:lod2-wien"),   # Vienna (the city's roof model)
)
BOX = SOURCES[0].box            # (Germany's: kept for callers that ask for it)
# AdV roofType -> roof:shape. 5000 (mixed), 9999 (other) and an empty code say nothing about the shape: predicted.
SHAPES = {1000: "flat", 2100: "skillion", 2200: "skillion", 3100: "gabled", 3200: "hipped", 3300: "half-hipped",
          3400: "mansard", 3500: "pyramidal", 3600: "pyramidal", 3700: "dome", 3800: "skillion", 3900: "round",
          4000: "pyramidal"}


def tile_of(lat, lon, zoom=ZOOM):
    """(x, y) of the web-mercator tile at lat, lon."""
    n = 1 << zoom
    r = math.radians(lat)
    return int((lon + 180.0) / 360.0 * n), int((1.0 - dmath.asinh(dmath.tan(r)) / math.pi) / 2.0 * n)


def tiles_over(south, west, north, east, box=BOX):
    """[(x, y)] of the z10 tiles over a box, none outside box (Germany's by default)."""
    s, w, n, e = max(south, box[0]), max(west, box[1]), min(north, box[2]), min(east, box[3])
    if s > n or w > e:
        return []
    x0, y0 = tile_of(n, w)
    x1, y1 = tile_of(s, e)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def local_dir():
    return os.environ.get("BNE_LOD2_DIR") or None


def credited_near(cache_dir, lat, lon, near_m, local=None):
    """The credits (credits.FEATURES ids) of the countries whose measured roof shards with records are on this
    computer for the z10 tiles within near_m of lat, lon (the LoD2 licences ask for attribution); an empty set: none.
    On-disk shards only, never a download (the road tiles are built in the workers, which share the cache)."""
    local = local or local_dir()
    if not ON or not (cache_dir or local):
        return set()
    dlat = near_m / 110540.0
    dlon = near_m / (111320.0 * max(0.1, dmath.cos(math.radians(lat))))
    got = set()
    for c in SOURCES:
        for x, y in tiles_over(lat - dlat, lon - dlon, lat + dlat, lon + dlon, c.box):
            path = (os.path.join(local, c.folder, str(x), f"{y}.npz") if local else
                    os.path.join(cache_dir, "pagesdata", c.folder, f"{x}_{y}.npz"))
            try:
                if os.path.getsize(path) > 0:
                    got.add(c.credit)
                    break
            except OSError:
                pass
    return got


MISS_TTL_S = 7 * 86400     # s: a shard the site did not have (404) asked again after this


class Source:
    """The shards of every country: each fetched once, kept on disk, and held in memory for the tiles around it (or
    read from the local folder, BNE_LOD2_DIR)."""

    def __init__(self, cache_dir, fetcher, local=None):
        self.cache_dir = cache_dir
        self.local = local or local_dir()
        self.dir = os.path.join(cache_dir, "pagesdata", "lod2") if cache_dir else None     # (Germany's folder)
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.shards = {}            # (folder, x, y) -> {lat, lon, code, height, roof} arrays, or {} none there
        self.down_at = 0.0
        self.sites = {}             # folder -> railtags.Shards: its manifest (pinned, refreshed daily), its "data" host

    def _site(self, folder):
        with self.lock:
            s = self.sites.get(folder)
            if s is None:
                s = self.sites[folder] = railtags.Shards(self.cache_dir, self.fetcher, folder)
            return s

    def shard(self, x, y, folder="lod2"):
        with self.lock:
            got = self.shards.get((folder, x, y))
        if got is not None:
            return got
        if self.local:
            try:
                with open(os.path.join(self.local, folder, str(x), f"{y}.npz"), "rb") as fh:
                    body = fh.read()
            except OSError:
                body = b""
        else:
            body = self._fetched(x, y, folder)
            if body is None:
                return {}
        got = decode(body)
        with self.lock:
            self.shards[(folder, x, y)] = got
        return got

    def _fetched(self, x, y, folder):
        """The shard's bytes from the disk cache or the site (b"": the site has none), None: not had now."""
        d = os.path.join(self.cache_dir, "pagesdata", folder) if self.cache_dir else None
        path = os.path.join(d, f"{x}_{y}.npz") if d else None
        if path and os.path.exists(path):
            try:
                # (a shard the site didn't have, kept as an empty file, is asked again after MISS_TTL_S: before the
                # shards were published every tile answered 404, and kept for good the roofs never came)
                stale_miss = os.path.getsize(path) == 0 and time.time() - os.path.getmtime(path) > MISS_TTL_S
                if not stale_miss:
                    with open(path, "rb") as fh:
                        return fh.read()
            except OSError:
                pass
        if self.fetcher is None or time.time() - self.down_at < RETRY_S:
            return None
        try:
            # (the folder's manifest names the host of its shards, data_url: R2 behind the CDN; none had (offline,
            # an old manifest): Pages, as before)
            site = self._site(folder)
            site.version()
            body = site.get(f"{x}/{y}.npz")
        except netfetch.NotFound:
            body = b""
        except Exception as e:      # (offline, or not published: the predicted roofs)
            log.debug("lod2 shard %s %s %s: %s: %s", folder, x, y, type(e).__name__, e)
            self.down_at = time.time()
            return None
        if path:
            os.makedirs(d, exist_ok=True)
            with open(path + ".part", "wb") as fh:
                fh.write(body)
            os.replace(path + ".part", path)
        return body

    def tile(self, frame, x0, y0, x1, y1):
        """The Records of an anchor-frame box, or None when there are none: a tile outside every country's box costs
        nothing."""
        if not ON:
            return None
        m = 100.0
        lats, lons, _ = frame.enu_to_geodetic(np.array([x0 - m, x1 + m, x0 - m, x1 + m]),
                                              np.array([y0 - m, y0 - m, y1 + m, y1 + m]), 0.0)
        s, w, n, e = float(np.min(lats)), float(np.min(lons)), float(np.max(lats)), float(np.max(lons))
        shards = [self.shard(x, y, c.folder) for c in SOURCES for x, y in tiles_over(s, w, n, e, c.box)]
        shards = [sh for sh in shards if sh]
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
