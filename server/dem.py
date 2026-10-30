"""Elevation tiles (Terrarium encoding, Web Mercator) with a disk cache.

Primary: Mapterhorn, 512 px WebP tiles. Measured 2026-10-09: z<=12 everywhere
on land, deeper only where national lidar exists (z16 around Lone Pine CA,
z17 in the Swiss Alps); everything else returns 404. Open ocean is 404 even at
z11, and in coastal tiles the sea reads exactly 0.
Fallback: AWS Terrain Tiles, 256 px PNG, z<=15, global, includes bathymetry.

All sampling happens on the Mapterhorn grid (512 px tiles). A fallback tile is
assembled from AWS tiles of the same footprint and resampled to 512 px.

Outside the lidar areas (no Mapterhorn tile deeper than z12) Mapterhorn is Copernicus GLO-30, a surface model:
its z12 tiles get their buildings and small tree stands taken out (dsmground.ground, with the 8 neighbouring
tiles as context so neighbours agree), and deeper zooms there are that ground upsampled, not AWS (SRTM there:
25 m building bumps in Mexico City, a 28 m void fill over Botafogo's bay; 2026-10-15). Dem.ground_filter = False
keeps the raw tiles and AWS (tests, comparisons).

Requests go through a netfetch.Fetcher (rate limit, retries, circuit breaker per host). When
Mapterhorn cannot be asked (circuit open, rate limited, failing) the same tile comes from AWS
(failover); if AWS fails too, DemUnavailable. 404s are remembered as <y>.404 markers that expire
after the host's miss_ttl (Mapterhorn 90 days: it adds lidar areas; AWS never).
"""
import contextlib
import io
import json
import logging
import math
import os
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

import cache as cachemod
import dsmground
import fast
import hrdem as hrdemmod
import lidarbc as lidarbcmod
import netfetch
import polardem
import staging

log = logging.getLogger("earthd.dem")

TILE = 512                  # sampling grid tile size (Mapterhorn)
MEM_TILES = 512             # decoded tiles kept per process (Dem's default; budget.py: fewer on a small machine)
PROBE_WORKERS = 16          # Dem._ahead(): raw downloads started ahead of the walk down the zooms
URGENT_WORKERS = 16         # Dem.urgent(): its own fetch threads (with the 16 shared: Mapterhorn's 32 at once)
# Decoded tiles mirrored on disk (<cache>/dem/decoded/...: float32 .npy), memory-mapped read-only: every server process
# decoded the same tiles (WebP + Terrarium, ~3 ms a tile here, the z12 ground filter far more) and kept its own copy
# (up to MEM_TILES MB a process, outside Python's traced memory); a mirrored tile is decoded once per machine and its
# pages shared, dropped instead of swapped under memory pressure. At most MIRROR_MB on disk (the oldest go first);
# older than MIRROR_MAX_AGE_S it is made again (Mapterhorn adds lidar areas). DECODED_VERSION: raise it when what a
# tile holds changes (the decoding, the ground filter).
MIRROR_MB = 1024
MIRROR_PRUNE_S = 120.0  # the mirror pruned at most this often per process (each prune walks it all)
MIRROR_MAX_AGE_S = 30 * 86400
DECODED_VERSION = 4          # 2: tiles past the lidar's depth upsampled from its deepest tile, not z12; its holes
                             # filled (fill_holes); 3: Canada's HRDEM lidar (hrdem.py) from z13 down; 4: LidarBC,
                             # and z12 / z13 shifted to its ground (BIAS_*)
MAX_ZOOM = 15               # AWS stops at z15 (the far field's zooms: zoom_for's default cap)
# The near terrain and the road profiles read Mapterhorn's lidar one zoom deeper where it has it (the user, 2026-10-21:
# "the terrain data gets smoothed ... sharper terrain features get lost": z16 is ~0.8 m a pixel at 48 N, z15 ~1.6 m, the
# terrain's lattice 2 m); a z16 tile it lacks is its z15 parent upsampled (AWS has no z16). BNE_DEM_ZOOM=15: as before.
FINE_ZOOM = int(os.environ.get("BNE_DEM_ZOOM", "16"))
GLOBAL_ZOOM = 12            # Mapterhorn is complete on land up to here
HRDEM = os.environ.get("BNE_HRDEM", "1") != "0"
# British Columbia's own lidar (lidarbc.py): opt-in. Its 1 m files have no overviews and are read in full-width rows:
# ~15 MB a square km of near terrain, and a first visit on Vancouver Island took ~60 s longer to its roads (four
# fresh towns, 2026-10-28: all 36 road tiles at 85-88 s against 23-26 s). HRDEM (its overviews) stays on.
LIDARBC = HRDEM and os.environ.get("BNE_LIDARBC", "0") == "1"
# HRDEM read whole for the near terrain (z15 and deeper, 2 m): opt-in too (2026-10-28, four fresh Vancouver Island
# towns: all road tiles at 33-53 s against 20-22 s without, 61-72 MB a place). Its coarse overview still lines the
# far tiles up with the lidar ground (_hrdem_bias: a few MB).
HRDEM_NEAR = HRDEM and os.environ.get("BNE_HRDEM_NEAR", "0") == "1"
HRDEM_FEATHER_M = 60.0      # m over which HRDEM's edge blends into Mapterhorn's heights
HRDEM_FEATHER_MAX_PX = 48
# The distant terrain (z12 / z13: the far field's L2 / L3 rings, 1-4 km out): reading BC's 1 m lidar whole for it would
# cost ~40 MB a tile, but where Mapterhorn has no lidar its forests stand ~5-18 m above the ground (a quarter of the
# LidarBC areas sampled, 2026-10-23: Carmanah +15 m), a step against the lidar of the rings inside. So a sparse grid
# of lidar points (every BIAS_STEP_M along the files' own rows, read through small cache blocks), binned on a
# BIAS_GRID x BIAS_GRID grid
# measures Mapterhorn's offset there, smoothed over BIAS_SMOOTH grid cells, and the tile is shifted by it: Mapterhorn's
# shape at the lidar's level, fading out where the lidar ends.
BIAS_ZOOMS = (12, 13, 14)
# The lidar itself (HRDEM, LidarBC) only from this zoom on: the near terrain (z15: ~1.6 m pixels at 49 N, the
# terrain's 2 m lattice). Read whole, the far rings' z13 / z14 tiles cost ~10 MB each (2026-10-28, Port Alberni: 440 MB
# of lidar for one first visit, against Overture's 84 MB); there HRDEM's coarse overview shifts Mapterhorn instead.
FINE_MIN_ZOOM = 15
HRDEM_BIAS_PX = 64          # the far tile's grid the HRDEM overview is read on ...
HRDEM_BIAS_M = 32.0         # ... from the overview with pixels this size (a 16 km file tile serves a whole region)
BIAS_GRID = 32
BIAS_SMOOTH = 2.0           # grid cells (Gaussian sigma)
BIAS_BLOCK = 16 * 1024
BIAS_STEP_M = 64.0          # m between the lidar rows sampled (a point every half of it along them)
BIAS_COARSE_M = 4.0         # m: the older tiled flights' overviews are good enough for a level
GROUND_MARGIN = 96          # px of the neighbouring z12 tiles the ground filter sees around a tile
MAX_LAT = 85.0511287798     # Web Mercator limit
MAX_TILES_PER_SAMPLE = 1024
SPARSE_MIN_TILES = 16       # a sample's box of more tiles than this with points in fewer than 1 / SPARSE_RATIO of them
SPARSE_RATIO = 4            # is sampled in groups of SPARSE_GROUP x SPARSE_GROUP tiles (only the tiles near points)
SPARSE_GROUP = 2

MAPTERHORN = dict(name="mapterhorn", ext="webp", size=512,
                  url="https://tiles.mapterhorn.com/{z}/{x}/{y}.webp")
# Mapterhorn's list of its files: the planet to z12, then one file per z6 tile with lidar under it, with the deepest
# zoom it has there. A deeper tile under a z6 tile it does not list (or deeper than that file goes) is a 404 without
# asking: a fresh place's first block asked ~240 such tiles (z16 -> z15 -> z14 -> z13 per tile), on its critical path
# (Belgrade, 2026-10-25).
COVERAGE_URL = "https://download.mapterhorn.com/download_urls.json"
COVERAGE_ZOOM = 6
COVERAGE_MAX_AGE_S = 7 * 86400.0    # the copy on disk read again from the net after this (it adds areas)
COVERAGE_RETRY_S = 600.0            # a failed read asked again after this (until then: every tile asked, as before)
_coverage = {"index": None, "at": -1e18, "lock": threading.Lock()}


def coverage_index(doc):
    """{(x, y) of a z6 tile: the deepest zoom Mapterhorn has under it} from its download_urls.json. Pure."""
    out = {}
    for it in doc.get("items", ()):
        name = str(it.get("name", ""))
        parts = name.split(".")[0].split("-")
        if len(parts) == 3 and parts[0] == str(COVERAGE_ZOOM):
            try:
                out[(int(parts[1]), int(parts[2]))] = int(it["max_zoom"])
            except (KeyError, TypeError, ValueError):
                continue
    return out
AWS = dict(name="aws-terrarium", ext="png", size=256,
           url="https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png")


class DemUnavailable(IOError):
    """Neither Mapterhorn nor AWS could deliver a tile (network, offline)."""


def zoom_for(lat, pixel_m, max_zoom=MAX_ZOOM):
    """Smallest zoom whose 512 px tile pixels are <= pixel_m at this latitude."""
    c = 40075016.686 * max(math.cos(math.radians(min(abs(lat), MAX_LAT))), 1e-6)
    return int(min(max_zoom, max(0, math.ceil(math.log2(c / (TILE * pixel_m))))))


def decode_terrarium(data, size):
    """float32 heights of a Terrarium tile (Rust: no RGB copy, no float temporaries; ~2/3 of a tile's decode, and
    the worker processes each decode the DEM tiles they read)."""
    im = Image.open(io.BytesIO(data))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    if im.size != (size, size):
        log.warning("unexpected DEM tile size %s (expected %d), resampling", im.size, size)
        im = im.resize((size, size), Image.BILINEAR)
    fast.require(1, "decode_terrarium")
    return fast._ef.terrarium_heights(np.ascontiguousarray(np.asarray(im)))


HOLE_FILL_M = 600.0          # lidar holes up to ~2x this wide filled (a coarse tile's are whole city blocks: a
HOLE_FILL_MAX_PX = 24        # Rotterdam district read 67 % zeros at z9-z11, ~1 % after 24 px; 2026-10-21); wider
                             # zero areas are the sea


def hole_px(z, y):
    """fill_holes' rounds for tile row y at zoom z: HOLE_FILL_M in its pixels, 2 .. HOLE_FILL_MAX_PX."""
    n = 2 ** z
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
    px = 40075016.686 * math.cos(math.radians(lat)) / n / TILE
    return int(min(HOLE_FILL_MAX_PX, max(2, round(HOLE_FILL_M / px))))


def fill_holes(a, k=HOLE_FILL_MAX_PX):
    """Lidar tile a with its small exact-0 holes filled from their surroundings. National lidar terrain models have
    no ground under buildings, and Mapterhorn writes those cells as 0 (Rotterdam's AHN: 74 % of the zeros in
    Spangen on land, 2026-10-21): a polder at -1.5 m got 0 m pits, and seacarve read them as the sea. Zeros take
    the mean of their non-zero 4-neighbours, k rounds; zero areas wider than that (the sea, which reads exactly
    0) keep 0, the band filled into their edge too. Pure."""
    fast.require(65, "dem.fill_holes")     # (earthfast fill_holes, 2026-10-23: the same values as the numpy rounds)
    return fast._ef.fill_holes(np.ascontiguousarray(a, np.float32), int(k))


def _resize(a, size):
    if a.shape[0] == size:
        return a
    return np.asarray(Image.fromarray(a, mode="F").resize((size, size), Image.BILINEAR))


def _tile_xy(lat, lon, z):
    """Web-Mercator tile (x, y) holding (lat, lon) at zoom z."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n) % n
    y = int((1.0 - math.asinh(math.tan(math.radians(max(-MAX_LAT, min(MAX_LAT, lat))))) / math.pi) / 2.0 * n)
    return x, max(0, min(n - 1, y))


def _gauss2(a, sigma):
    """A separable Gaussian blur of a small 2-D grid (edges held), pure numpy."""
    r = max(1, int(round(3 * sigma)))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    p = np.pad(a, ((r, r), (0, 0)), mode="edge")
    a = sum(k[i] * p[i:i + a.shape[0]] for i in range(2 * r + 1))
    p = np.pad(a, ((0, 0), (r, r)), mode="edge")
    return sum(k[i] * p[:, i:i + a.shape[1]] for i in range(2 * r + 1))


class Dem:
    disk = cachemod.NONE    # the server's cache.Cache (last use per file, the size cap); set by earthd
    ground_filter = True    # surface-model areas: buildings out, deeper zooms from that ground (see above)

    # mem_tiles: decoded float32 512² tiles kept in memory (1 MB each; 512 per docs/V1_2_DESIGN.md §4.3).
    # fetcher: the server's netfetch.Fetcher (shared rate limits); None = a Fetcher of its own.
    def __init__(self, cache_dir, fetch_workers=16, mem_tiles=None, fetcher=None):
        self.cache_dir = cache_dir
        self.fetcher = fetcher if fetcher is not None else netfetch.Fetcher()
        self.fetcher.register_chain("dem", (MAPTERHORN["url"], AWS["url"]))
        self.pool = ThreadPoolExecutor(fetch_workers, thread_name_prefix="dem-fetch")
        # The urgent lane (urgent()): the anchor's first block after a teleport needs ~10 tiles and waited ~33 s
        # behind hundreds queued by the far field, the road plans and the ground ahead (2026-10-16; alone 0.9 s).
        self.urgent_pool = ThreadPoolExecutor(URGENT_WORKERS, thread_name_prefix="dem-urgent")
        # raw Mapterhorn downloads started ahead of need (_ahead): a fresh place's tiles walked z16 -> z15 -> z14 ->
        # z13 -> z12 and its 8 neighbours one request at a time, ~1 s each before the CDN has them (2026-10-22)
        self.probe_pool = ThreadPoolExecutor(PROBE_WORKERS, thread_name_prefix="dem-probe")
        self._probing = {}              # (z, x, y) -> Future of its raw download
        self._local = threading.local()
        self.mem_tiles = MEM_TILES if mem_tiles is None else mem_tiles
        self._lru = OrderedDict()       # (z, x, y) -> float32 [TILE, TILE]
        self._inflight = {}             # (z, x, y) -> Future
        self._lock = threading.Lock()
        self.stats = Counter()          # updated from fetch threads; read via stats_snapshot()
        # (z, x, y) -> the provider that served the tile ("mapterhorn" / "aws-terrarium"; None: no
        # data, zeros), for the data credits (credits.py). A few bytes per tile ever loaded.
        self.served = {}
        self.hrdem = hrdemmod.Hrdem(os.path.join(cache_dir, "hrdem"), self.fetcher) if HRDEM else None
        self.lidarbc = lidarbcmod.LidarBC(os.path.join(cache_dir, "lidarbc"), self.fetcher) if LIDARBC else None
        self.lidarbc_served = set()     # tile keys with LidarBC heights in them
        self.lidarbc_sparse = (lidarbcmod.LidarBC(os.path.join(cache_dir, "lidarbc-sparse"), self.fetcher,
                                                  block=BIAS_BLOCK) if LIDARBC else None)
        self.hrdem_served = set()       # tile keys with HRDEM heights in them (the data credits)

    def _canada(self, key, a):
        """Tile key's heights a with Canada's lidar terrain (hrdem.py) put in where it has data (z13 and deeper)."""
        z, x, y = key
        if a is None or z < GLOBAL_ZOOM:
            return a
        if z < FINE_MIN_ZOOM:
            return self._bc_bias(key, self._hrdem_bias(key, a))
        return self._canada_fine(key, a)

    def _hrdem_bias(self, key, a):
        """Distant tile key's heights a shifted to HRDEM's ground (read from a coarse overview) where Mapterhorn's sit
        off it: its forests and drawn-out coarse model, at the lidar's level, for a few hundred kB a region."""
        z, x, y = key
        if self.hrdem is None or z not in BIAS_ZOOMS or not hrdemmod.covers(z, x, y):
            return a
        try:
            got = self.hrdem.tile(z, x, y, size=HRDEM_BIAS_PX, min_px=HRDEM_BIAS_M)
        except (netfetch.FetchError, OSError, ValueError) as e:
            self._count("hrdem-bias:error")
            self._partial()
            log.debug("hrdem bias %s: %s", key, e)
            return a
        if got is None:
            return a
        h, ok = got
        lat, lon = hrdemmod.tile_lat_lon(z, x, y, HRDEM_BIAS_PX)
        out = self._shift_to(key, a, lat[ok], lon[ok], h[ok])
        if out is not a:
            self._count("hrdem-bias:tile")
            with self._lock:
                self.hrdem_served.add(key)
        return out

    def _bc_bias(self, key, a):
        """Distant tile key's heights a shifted to BC's lidar ground where Mapterhorn's sit off it (BIAS_* above)."""
        z, x, y = key
        lb = self.lidarbc_sparse
        if lb is None or z not in BIAS_ZOOMS:
            return a
        n = 2 ** z
        lat_of = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))     # noqa: E731
        box = (lat_of(y + 1), x / n * 360.0 - 180.0, lat_of(y), (x + 1) / n * 360.0 - 180.0)
        s_, w_, n_, e_ = lb._index()[4]
        if box[0] > n_ or box[2] < s_ or box[1] > e_ or box[3] < w_:
            return a
        try:
            plat, plon, ph = lb.sparse(box, BIAS_STEP_M, coarse_m=BIAS_COARSE_M)
        except (netfetch.FetchError, OSError, ValueError) as e:
            self._count("lidarbc-bias:error")
            self._partial()
            log.debug("lidarbc bias %s: %s", key, e)
            return a
        out = self._shift_to(key, a, plat, plon, ph)
        if out is not a:
            self._count("lidarbc-bias:tile")
        return out

    def _shift_to(self, key, a, plat, plon, ph):
        """Tile key's heights a shifted by the smoothed offset between them and ground points (plat, plon, ph): a
        new array, or a itself where there is too little to go on or the shift is under a quarter metre."""
        z, x, y = key
        n = 2 ** z
        if len(ph) < 16:
            return a
        # the tile's own heights at the lidar points (bilinear on its pixel grid)
        fx = ((plon + 180.0) / 360.0 * n - x) * TILE - 0.5
        fy = ((1 - np.arcsinh(np.tan(np.radians(plat))) / math.pi) / 2 * n - y) * TILE - 0.5
        x0_ = np.clip(np.floor(fx).astype(np.int64), 0, TILE - 2)
        y0_ = np.clip(np.floor(fy).astype(np.int64), 0, TILE - 2)
        u = np.clip(fx - x0_, 0, 1)
        v = np.clip(fy - y0_, 0, 1)
        base = ((a[y0_, x0_] * (1 - u) + a[y0_, x0_ + 1] * u) * (1 - v)
                + (a[y0_ + 1, x0_] * (1 - u) + a[y0_ + 1, x0_ + 1] * u) * v)
        keep = (a[y0_, x0_] != 0.0) & (a[y0_ + 1, x0_ + 1] != 0.0)          # not the sea
        # binned on the BIAS_GRID grid: the mean offset per cell, then smoothed
        g = BIAS_GRID
        gi = np.clip(((fy + 0.5) / TILE * g).astype(np.int64), 0, g - 1)
        gj = np.clip(((fx + 0.5) / TILE * g).astype(np.int64), 0, g - 1)
        d = np.zeros((g, g))
        wgt = np.zeros((g, g))
        np.add.at(d, (gi[keep], gj[keep]), (base - ph)[keep])
        np.add.at(wgt, (gi[keep], gj[keep]), 1.0)
        d = np.where(wgt > 0, d / np.maximum(wgt, 1), 0.0)
        wgt = (wgt > 0).astype(np.float64)
        if wgt.sum() < 4:
            return a
        sd, sw = _gauss2(d * wgt, BIAS_SMOOTH), _gauss2(wgt, BIAS_SMOOTH)
        with np.errstate(invalid="ignore", divide="ignore"):
            off = np.where(sw > 1e-6, sd / sw, 0.0) * np.clip(sw / max(sw.max(), 1e-6) * 2.0, 0.0, 1.0)
        # the offset field on the tile's pixels (bilinear between the grid cells' centres)
        pix = (np.arange(TILE) + 0.5) / TILE * g - 0.5
        i0 = np.clip(np.floor(pix).astype(np.int64), 0, g - 2)
        f = np.clip(pix - i0, 0.0, 1.0)
        o_r = off[i0] * (1 - f)[:, None] + off[i0 + 1] * f[:, None]
        field = o_r[:, i0] * (1 - f)[None, :] + o_r[:, i0 + 1] * f[None, :]
        if np.abs(field).max() < 0.25:
            return a
        out = (a - field).astype(np.float32)
        out[a == 0.0] = 0.0                          # the sea stays the sea
        return out

    def _canada_fine(self, key, a):
        z, x, y = key
        h = ok = None
        used = []
        near_hrdem = self.hrdem if getattr(self, "hrdem_near", HRDEM_NEAR) else None
        for name, src in (("lidarbc", self.lidarbc), ("hrdem", near_hrdem)):
            if src is None or (ok is not None and ok.all()):
                continue
            if name == "lidarbc" and not src.covers(z, x, y):
                continue
            if name == "hrdem" and not hrdemmod.covers(z, x, y):
                continue
            try:
                got = src.tile(z, x, y)
            except (netfetch.FetchError, OSError, ValueError) as e:
                self._count(name + ":error")
                self._partial()                 # (not mirrored: asked again next time)
                log.debug("%s %s: %s", name, key, e)
                continue
            if got is None:
                continue
            gh, gok = got
            if h is None:
                h, ok = gh.copy(), gok.copy()
            else:
                fill = gok & ~ok
                h[fill] = gh[fill]
                ok |= fill
            used.append(name)
        if h is None:
            return a
        sea = a == 0.0                      # Mapterhorn's sea reads exactly 0: it stays the sea
        ok = ok & ~sea
        if not ok.any():
            return a
        n = 2 ** z
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
        px = 40075016.686 * math.cos(math.radians(lat)) / n / TILE
        w = hrdemmod.feather(ok, min(HRDEM_FEATHER_MAX_PX, max(2, round(HRDEM_FEATHER_M / px))))
        for name in used:
            self._count(name + ":tile")
        with self._lock:
            for name in used:
                (self.lidarbc_served if name == "lidarbc" else self.hrdem_served).add(key)
        return (a * (1.0 - w) + h * w).astype(np.float32)

    def polar(self):
        """The polar caps' DEM (made on first use)."""
        p = getattr(self, "_polar", None)
        if p is None:
            with self._lock:
                p = getattr(self, "_polar", None)
                if p is None:
                    p = self._polar = polardem.PolarDem(self.cache_dir, self.fetcher)
        return p

    def _count(self, what):
        with self._lock:
            self.stats[what] += 1

    def stats_snapshot(self):
        with self._lock:
            return dict(self.stats)

    # --- raw tiles -------------------------------------------------------
    def _path(self, src, z, x, y):
        return os.path.join(self.cache_dir, src["name"], str(z), str(x), f"{y}.{src['ext']}")

    def _coverage(self):
        """coverage_index of Mapterhorn's file list (the copy on disk, read again from the net when old), or None when
        it cannot be had: every tile is asked then. One per process."""
        c = _coverage
        now = time.time()
        if c["index"] is not None and now - c["at"] < COVERAGE_MAX_AGE_S:
            return c["index"]
        with c["lock"]:
            if c["index"] is not None and now - c["at"] < COVERAGE_MAX_AGE_S:
                return c["index"]
            if c["index"] is None and now - c["at"] < COVERAGE_RETRY_S and c.get("failed"):
                return None
            path = os.path.join(self.cache_dir, MAPTERHORN["name"], "download_urls.json")
            try:
                age = now - os.path.getmtime(path)
                if age < COVERAGE_MAX_AGE_S:
                    with open(path, "rb") as f:
                        c["index"], c["at"], c["failed"] = coverage_index(json.loads(f.read())), now - age, False
                        return c["index"]
            except (OSError, ValueError):
                pass
            try:
                body = self.fetcher.get(COVERAGE_URL, kind="demand").body
                idx = coverage_index(json.loads(body))
                if not idx:
                    raise ValueError("no z6 files listed")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(tmp, "wb") as f:
                    f.write(body)
                os.replace(tmp, path)
                c["index"], c["at"], c["failed"] = idx, now, False
            except Exception as e:      # (offline, a host down, another format): ask every tile, as before
                log.debug("dem: Mapterhorn coverage: %s: %s", type(e).__name__, e)
                c["at"], c["failed"] = now, True
            return c["index"]

    def coarse_at(self, lat, lon):
        """Is the elevation at (lat, lon) only the coarse global model (Mapterhorn's file list has nothing deeper than
        GLOBAL_ZOOM under its z6 tile: no lidar, ~30 m Copernicus and the like)? False when unknown (no list yet) or
        where the opt-in Canadian lidar reads. grading.ease_coarse (GitHub #414)."""
        idx = self._coverage()
        if idx is None:
            return False
        n = 2 ** COVERAGE_ZOOM
        lat = max(-MAX_LAT, min(MAX_LAT, float(lat)))
        x = int((float(lon) + 180.0) / 360.0 * n) % n
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        if idx.get((x, max(0, min(n - 1, y))), GLOBAL_ZOOM) > GLOBAL_ZOOM:
            return False
        if (self.lidarbc is not None or HRDEM_NEAR) and hrdemmod.covers(GLOBAL_ZOOM, *_tile_xy(lat, lon, GLOBAL_ZOOM)):
            return False
        return True

    def _listed(self, z, x, y):
        """False when Mapterhorn's file list says it has no tile (z, x, y) (z > GLOBAL_ZOOM); True otherwise."""
        idx = self._coverage()
        if idx is None:
            return True
        d = z - COVERAGE_ZOOM
        return z <= idx.get((x >> d, y >> d), GLOBAL_ZOOM)

    def _download(self, src, z, x, y, kind="demand"):
        """Raw tile bytes from disk cache or network; None for a (cached) 404. Raises
        netfetch.FetchError when the host cannot deliver (netfetch.Deferred for a refused prefetch)."""
        if src is MAPTERHORN and z > GLOBAL_ZOOM and not self._listed(z, x, y):
            self._count(src["name"] + ":unlisted")
            return None
        path = self._path(src, z, x, y)
        miss = os.path.join(self.cache_dir, src["name"], str(z), str(x), f"{y}.404")
        data = self.disk.read(path)         # None: not cached (or evicted a moment ago)
        if data is not None:
            self._count(src["name"] + ":disk")
            return data
        url = src["url"].format(z=z, x=x, y=y)
        if self.fetcher.miss_alive(miss, url):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            data = self.fetcher.get(url, kind=kind).body
        except netfetch.NotFound:
            open(miss, "wb").close()        # (re)written: its mtime starts the marker's TTL
            self.disk.wrote(miss, 0)
            self._count(src["name"] + ":404")
            return None
        except netfetch.FetchError:
            self._count(src["name"] + ":error")
            raise
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        staging.replace_cached(tmp, path)
        self.disk.wrote(path, len(data))
        if os.path.exists(miss):            # an expired marker: the host has data there now
            os.remove(miss)
            self.disk.forget(miss)
        self._count(src["name"] + ":net")
        return data

    def _ahead(self, keys):
        """Start the raw Mapterhorn downloads of keys at once (on disk afterwards, or a .404 marker); one request
        per tile however many ask. Returns their futures (result: the bytes or None; errors are left for the caller's
        own _download to meet)."""
        futs = []
        with self._lock:
            for k in keys:
                f = self._probing.get(k)
                if f is None:
                    f = self._probing[k] = self.probe_pool.submit(self._probe, k)
                futs.append(f)
        return futs

    def _probe(self, k):
        try:
            return self._download(MAPTERHORN, *k)
        except netfetch.FetchError:
            return None
        finally:
            with self._lock:
                self._probing.pop(k, None)

    @staticmethod
    def _wait(futs):
        for f in futs:
            try:
                f.result()
            except Exception:
                pass

    def prefetch(self, keys):
        """Download the Mapterhorn tiles (z, x, y) that are not on disk yet into the disk cache, at netfetch
        "prefetch" priority (only spare tokens of the host: the blocks being built keep theirs), without
        decoding them. Returns (downloaded, deferred): deferred when the host refused one (no spare tokens
        now): the rest waits for the next call. Errors skip the tile (asked again on demand)."""
        got = 0
        for z, x, y in keys:
            if os.path.exists(self._path(MAPTERHORN, z, x, y)):
                continue
            try:
                if self._download(MAPTERHORN, z, x, y, kind="prefetch") is not None:
                    got += 1
            except netfetch.Deferred:
                return got, True
            except netfetch.FetchError:
                continue
        return got, False

    def predownload(self, z, x, y):
        """A saved area's tile (areas.py) at netfetch "predownload" (hosts that allow it, within its share of the daily
        budget), unless has() says it needs nothing: True when fetched now (or a 404 remembered). Raises
        netfetch.FetchError (Deferred, BudgetExceeded, ...) for the caller to wait or skip."""
        if self.has(z, x, y):
            return False
        self._download(MAPTERHORN, z, x, y, kind="predownload")
        return True

    def has(self, z, x, y):
        """True when Mapterhorn tile (z, x, y) needs no download: on disk, a 404 remembered, or not in Mapterhorn's
        file list (_listed; those never land on disk, and routeahead.py's bar stopped short of its total). For
        routeahead.py's progress count: no tile fetch (the file list is read once per session)."""
        if os.path.exists(self._path(MAPTERHORN, z, x, y)):
            return True
        if os.path.exists(os.path.join(self.cache_dir, MAPTERHORN["name"], str(z), str(x), f"{y}.404")):
            return True
        return z > GLOBAL_ZOOM and not self._listed(z, x, y)

    def keys_around(self, lat, lon, radius_m, zoom):
        """The tile keys (z, x, y) of zoom covering radius_m around lat/lon, nearest first; with the ground
        filter also their z12 parents (a z15 tile without lidar is made from z12 ground)."""
        import mapcells
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * max(0.05, math.cos(math.radians(lat))))
        x0, y0 = mapcells.tile_xy(lat + dlat, lon - dlon, zoom)
        x1, y1 = mapcells.tile_xy(lat - dlat, lon + dlon, zoom)
        cx, cy = mapcells.tile_xy(lat, lon, zoom)
        n = 2 ** zoom
        keys = [(zoom, x % n, y) for y in range(max(0, int(y0)), min(n - 1, int(y1)) + 1)
                for x in range(int(math.floor(x0)), int(math.floor(x1)) + 1)]
        keys.sort(key=lambda k: (k[1] + 0.5 - cx) ** 2 + (k[2] + 0.5 - cy) ** 2)
        if self.ground_filter and zoom > GLOBAL_ZOOM:
            sh = zoom - GLOBAL_ZOOM
            parents = list(dict.fromkeys((GLOBAL_ZOOM, x >> sh, y >> sh) for _, x, y in keys))
            keys = parents + keys
        return keys

    def _decode(self, src, z, x, y, data):
        """decode_terrarium of a source tile; a body that is no image (empty, or cut short where the
        server sent no length) leaves the disk cache and raises netfetch.BadResponse, so the chain
        fails over now and the tile is asked again later instead of failing every session."""
        try:
            return decode_terrarium(data, src["size"])
        except (OSError, ValueError) as e:
            try:
                os.remove(self._path(src, z, x, y))
            except OSError:
                pass
            self.disk.forget(self._path(src, z, x, y))
            self._count(src["name"] + ":undecodable")
            raise netfetch.BadResponse(f"undecodable {src['name']} tile {z}/{x}/{y}: {e}") from None

    def _aws_tile(self, z, x, y):
        """Fallback for Mapterhorn tile (z, x, y), built from AWS z+1 (max 15)."""
        za = min(MAX_ZOOM, z + 1)
        n = 2 ** (za - z)
        s = AWS["size"]
        mosaic = np.zeros((n * s, n * s), np.float32)
        found = False
        for j in range(n):
            for i in range(n):
                data = self._download(AWS, za, x * n + i, y * n + j)
                if data is not None:
                    mosaic[j * s:(j + 1) * s, i * s:(i + 1) * s] = self._decode(AWS, za, x * n + i, y * n + j, data)
                    found = True
        return _resize(mosaic, TILE) if found else None

    # --- the decoded mirror (MIRROR_MB) ----------------------------------------------------------
    def _mirror_path(self, key):
        z, x, y = key
        return os.path.join(self.cache_dir, "decoded", f"v{DECODED_VERSION}", f"g{int(self.ground_filter)}", str(z),
                            str(x), f"{y}.npy")

    def _mirror_get(self, key):
        """A tile from the decoded mirror (a read-only view of its memory map), or None."""
        if MIRROR_MB <= 0:
            return None
        path = self._mirror_path(key)
        try:
            if time.time() - os.path.getmtime(path) > MIRROR_MAX_AGE_S:
                return None
            a = np.asarray(np.load(path, mmap_mode="r", allow_pickle=False))
        except (OSError, ValueError, EOFError):
            return None
        if a.shape != (TILE, TILE) or a.dtype != np.float32:
            return None
        self._count("dem:mirror")
        return a

    def _mirror_put(self, key, a):
        """a written to the mirror and read back from it (shared by the processes), or None when it cannot be."""
        path = self._mirror_path(key)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "wb") as f:
                np.save(f, np.ascontiguousarray(a, np.float32), allow_pickle=False)
            staging.replace_cached(tmp, path)
        except OSError as e:
            log.debug("dem mirror %s: %s", path, e)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None
        with Dem._mirror_lock:
            Dem._mirror_written += TILE * TILE * 4
            # (and at most every MIRROR_PRUNE_S: each prune walks the whole mirror, ~28k folders; a fresh place's
            # DEM reads in a worker triggered it again and again, seconds of a far field's geometry, 2026-10-23)
            now = time.monotonic()
            prune = (Dem._mirror_written >= MIRROR_MB * 1e6 / 8
                     and now - Dem._mirror_pruned >= MIRROR_PRUNE_S)
            if prune:
                Dem._mirror_written, Dem._mirror_pruned = 0, now
        if prune:       # (beside the read that wrote it: the walk is seconds on a Steam Deck)
            threading.Thread(target=self._mirror_prune, daemon=True, name="dem-mirror-prune").start()
        return self._mirror_get(key)

    # (a process's first write prunes: the count of a session's own writes alone never reached the threshold when
    # each run wrote less, and the mirror grew past MIRROR_MB across sessions: 6.3 GB on a Steam Deck, 2026-10-23)
    _mirror_written = MIRROR_MB * 1e6
    _mirror_pruned = -1e9
    _mirror_lock = threading.Lock()

    def _mirror_prune(self):
        """The mirror cut back to 80 % of MIRROR_MB, the least recently written first."""
        files = []
        for dirpath, _, names in os.walk(os.path.join(self.cache_dir, "decoded")):
            for n in names:
                if n.endswith(".npy"):
                    p = os.path.join(dirpath, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        if total <= MIRROR_MB * 1e6:
            return
        for _, size, p in sorted(files):
            try:
                os.remove(p)
            except OSError:
                continue
            total -= size
            if total <= 0.8 * MIRROR_MB * 1e6:
                break

    def _partial(self):
        """This load used a stand-in (a failed fetch): its tile is not mirrored."""
        self._local.partial = True

    def _load_tile(self, key):
        """A decoded tile: from the mirror, else made (_make_tile) and, when it is all Mapterhorn's data as it will
        stay (no failover, no fetch failed on the way), written to the mirror."""
        got = self._mirror_get(key)
        if got is not None:
            self.served.setdefault(key, MAPTERHORN["name"])
            return got
        prev = getattr(self._local, "partial", False)
        self._local.partial = False
        partial = True
        try:
            a = self._make_tile(key)
            partial = self._local.partial
        finally:
            self._local.partial = prev or partial
        if not partial and a is not None and self.served.get(key) == MAPTERHORN["name"] and MIRROR_MB > 0:
            got = self._mirror_put(key, a)
            if got is not None:
                return got
        return a

    def _make_tile(self, key):
        return self._canada(key, self._make_tile0(key))

    def _make_tile0(self, key):
        z, x, y = key
        if z > MAX_ZOOM:                # (most places have no z16: its z15 parent asked alongside)
            self._ahead([(MAX_ZOOM, x >> (z - MAX_ZOOM), y >> (z - MAX_ZOOM))])
        try:
            data = self._download(MAPTERHORN, z, x, y)
            a = None if data is None else self._decode(MAPTERHORN, z, x, y, data)
        except netfetch.FetchError as e:
            # Mapterhorn cannot be asked (circuit open, rate limited, failing): the same tile from AWS,
            # as it is (with AWS bathymetry: a failure says nothing about where the sea is).
            self._count("mapterhorn:failover")
            self._partial()
            if z > MAX_ZOOM:            # (AWS stops at z15: its tile upsampled)
                a = self._from_ancestor(z, x, y, MAX_ZOOM)
                self.served[key] = MAPTERHORN["name"]
                return a
            try:
                a = self._aws_tile(z, x, y)
            except netfetch.FetchError as e2:
                raise DemUnavailable(f"DEM tile {z}/{x}/{y}: Mapterhorn: {e}; AWS: {e2}") from None
            self.served[key] = None if a is None else AWS["name"]
            return np.zeros((TILE, TILE), np.float32) if a is None else a
        if a is not None:
            self.served[key] = MAPTERHORN["name"]
            if self.ground_filter:
                if z == GLOBAL_ZOOM:
                    lidar = self._lidar(x, y)
                    a = fill_holes(a, hole_px(z, y)) if lidar else self._ground12(x, y, a)
                elif z > GLOBAL_ZOOM:
                    a = fill_holes(a, hole_px(z, y))        # lidar: no ground under buildings
                else:
                    d = GLOBAL_ZOOM - z                     # a coarse tile over lidar: its holes too
                    if self._lidar((x << d) + (1 << d) // 2, (y << d) + (1 << d) // 2):
                        a = fill_holes(a, hole_px(z, y))
            return a
        if z > MAX_ZOOM:                # (no lidar this deep here: the z15 tile upsampled)
            a = self._from_ancestor(z, x, y, MAX_ZOOM)
            self.served[key] = MAPTERHORN["name"]
            return a
        if self.ground_filter and z > GLOBAL_ZOOM:
            # deeper than the lidar goes here (Rotterdam: AHN to z14, z15 404): its deepest tile upsampled; no
            # lidar at all: the surface model's ground, upsampled. z12 under lidar gave 19 m steps and 0.5 m stairs
            # on city roads (Pleinweg's grade 1.5 % from z12, 0.4 % from z14; 2026-10-21)
            az = self._deepest_lidar(z, x, y)
            a = self._from_ancestor(z, x, y, az) if az else self._from_ground12(z, x, y)
            self.served[key] = MAPTERHORN["name"]
            return a
        try:
            a = self._aws_tile(z, x, y)
        except netfetch.FetchError as e:
            raise DemUnavailable(f"DEM tile {z}/{x}/{y}: AWS: {e}") from None
        self.served[key] = None if a is None else AWS["name"]
        if a is None:
            return np.zeros((TILE, TILE), np.float32)
        if z <= GLOBAL_ZOOM:
            # Mapterhorn is complete on land at these zooms, so a 404 here is
            # open ocean. Drop AWS bathymetry: the sea surface is what we draw.
            a = np.maximum(a, 0.0)
        return a

    # --- the ground under the surface model ------------------------------
    def _lidar(self, x, y):
        """Has Mapterhorn deeper tiles (national lidar: a terrain model already) under z12 tile (x, y)? Two of
        its z13 children asked, at once (404s are remembered)."""
        self._wait(self._ahead([(GLOBAL_ZOOM + 1, 2 * x, 2 * y), (GLOBAL_ZOOM + 1, 2 * x + 1, 2 * y + 1)]))
        for cx, cy in ((2 * x, 2 * y), (2 * x + 1, 2 * y + 1)):
            try:
                if self._download(MAPTERHORN, GLOBAL_ZOOM + 1, cx, cy) is not None:
                    return True
            except netfetch.FetchError:
                self._partial()
        return False

    def _raw12(self, x, y):
        """A z12 Mapterhorn tile as it is (zeros where there is none: the open sea)."""
        n = 2 ** GLOBAL_ZOOM
        if not 0 <= y < n:
            return None
        x %= n
        try:
            data = self._download(MAPTERHORN, GLOBAL_ZOOM, x, y)
        except netfetch.FetchError:
            self._partial()
            return None
        if data is None:
            return np.zeros((TILE, TILE), np.float32)
        return self._decode(MAPTERHORN, GLOBAL_ZOOM, x, y, data)

    def _ground12(self, x, y, a):
        """z12 tile (x, y) (elevations a) with the surface objects taken out, its neighbours as context."""
        m = GROUND_MARGIN
        n12 = 2 ** GLOBAL_ZOOM
        self._wait(self._ahead([(GLOBAL_ZOOM, (x + dx) % n12, y + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                                if (dx or dy) and 0 <= y + dy < n12]))
        big = np.empty((TILE + 2 * m, TILE + 2 * m), np.float32)
        big[m:m + TILE, m:m + TILE] = a
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nb = self._raw12(x + dx, y + dy)
                r0, r1 = (0, m) if dy < 0 else (m + TILE, 2 * m + TILE) if dy > 0 else (m, m + TILE)
                c0, c1 = (0, m) if dx < 0 else (m + TILE, 2 * m + TILE) if dx > 0 else (m, m + TILE)
                if nb is None:      # beyond the map's top / bottom, or not to be had: the edge repeated
                    rr = np.clip(np.arange(r0, r1) - m, 0, TILE - 1)
                    cc = np.clip(np.arange(c0, c1) - m, 0, TILE - 1)
                    big[r0:r1, c0:c1] = a[np.ix_(rr, cc)]
                else:
                    sr = slice(TILE - m, TILE) if dy < 0 else slice(0, m) if dy > 0 else slice(0, TILE)
                    sc = slice(TILE - m, TILE) if dx < 0 else slice(0, m) if dx > 0 else slice(0, TILE)
                    big[r0:r1, c0:c1] = nb[sr, sc]
        n = 2 ** GLOBAL_ZOOM
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
        px = 40075016.686 * math.cos(math.radians(lat)) / n / TILE
        g = dsmground.ground(big, px)
        self._count("dem:ground12")
        return np.ascontiguousarray(g[m:m + TILE, m:m + TILE])

    def _deepest_lidar(self, z, x, y):
        """The deepest zoom between GLOBAL_ZOOM + 1 and z - 1 at which Mapterhorn has the ancestor of tile (z, x, y)
        (national lidar), else None (404s are remembered, so a miss is asked once per tile)."""
        self._wait(self._ahead([(az, x >> (z - az), y >> (z - az)) for az in range(z - 1, GLOBAL_ZOOM, -1)]))
        for az in range(z - 1, GLOBAL_ZOOM, -1):
            d = z - az
            try:
                if self._download(MAPTERHORN, az, x >> d, y >> d) is not None:
                    return az
            except netfetch.FetchError:
                self._partial()
                return None
        return None

    def _from_ground12(self, z, x, y):
        """Tile (z, x, y), z > 12, bilinear from its z12 ancestor's ground (edges held at the ancestor's)."""
        return self._from_ancestor(z, x, y, GLOBAL_ZOOM)

    def _from_ancestor(self, z, x, y, z0):
        """Tile (z, x, y) bilinear from its ancestor at zoom z0 (as loaded: z12's ground; edges held at the ancestor's)."""
        d = z - z0
        ax, ay = x >> d, y >> d
        key = (z0, ax, ay)
        with self._lock:
            anc = self._lru.get(key)
        if anc is None:
            anc = self._load_tile(key)      # (in this thread: a pool worker waiting on the pool could deadlock)
            with self._lock:
                self._lru[key] = anc
                while len(self._lru) > self.mem_tiles:
                    self._lru.popitem(last=False)
        k = 2 ** d
        i = (np.arange(TILE) + 0.5) / k - 0.5
        u = np.clip((x - ax * k) * TILE / k + i, 0, TILE - 1)
        v = np.clip((y - ay * k) * TILE / k + i, 0, TILE - 1)
        u0 = np.minimum(np.floor(u).astype(np.int64), TILE - 2)
        v0 = np.minimum(np.floor(v).astype(np.int64), TILE - 2)
        fu, fv = (u - u0)[None, :], (v - v0)[:, None]
        a00 = anc[np.ix_(v0, u0)]
        a01 = anc[np.ix_(v0, u0 + 1)]
        a10 = anc[np.ix_(v0 + 1, u0)]
        a11 = anc[np.ix_(v0 + 1, u0 + 1)]
        return ((a00 * (1 - fu) + a01 * fu) * (1 - fv) + (a10 * (1 - fu) + a11 * fu) * fv).astype(np.float32)

    @contextlib.contextmanager
    def urgent(self):
        """Tiles this thread asks for meanwhile skip the queue: their own small pool, and a tile already queued (not
        started) is loaded again there instead of waited for."""
        prev = getattr(self._local, "urgent", False)
        self._local.urgent = True
        try:
            yield
        finally:
            self._local.urgent = prev

    def is_urgent(self):
        """Whether this thread is in urgent() (a helper it starts carries the lane over: grading._plan_missing)."""
        return getattr(self._local, "urgent", False)

    def tiles(self, keys, progress=None):
        """Fetch many tiles concurrently (deduplicated across callers)."""
        keys = list(dict.fromkeys(keys))
        out, futs, new = {}, {}, []
        urgent = getattr(self._local, "urgent", False)
        pool = self.urgent_pool if urgent else self.pool
        if urgent and len(keys) >= 16 and log.isEnabledFor(logging.DEBUG):
            zs = Counter(k[0] for k in keys)
            log.debug("dem urgent: %d tiles %s", len(keys), dict(zs))
        with self._lock:
            for k in keys:
                if k in self._lru:
                    self._lru.move_to_end(k)
                    out[k] = self._lru[k]
                elif k in self._inflight and not (urgent and not self._inflight[k].running()
                                                  and not self._inflight[k].done()):
                    futs[k] = self._inflight[k]
                else:
                    futs[k] = self._inflight[k] = pool.submit(self._load_tile, k)
                    new.append(k)
        # Outside the lock: the callback runs at once if the future is already done.
        for k in new:
            futs[k].add_done_callback(lambda f, k=k: self._finish(k, f))
        done = len(out)
        if progress:
            progress(done, len(keys))
        for k, fut in futs.items():
            out[k] = fut.result()
            done += 1
            if progress:
                progress(done, len(keys))
        return out

    def _finish(self, key, fut):
        """Move a finished load from _inflight into the LRU (failures are dropped,
        so the next request retries instead of reusing a stale exception)."""
        with self._lock:
            if self._inflight.get(key) is fut:
                del self._inflight[key]
            if not fut.cancelled() and fut.exception() is None:
                self._lru[key] = fut.result()
                while len(self._lru) > self.mem_tiles:
                    self._lru.popitem(last=False)

    def block_max(self, z, x, y, block):
        """Highest elevation in every block x block pixel square of tile (z, x, y): a (TILE / block)²
        array, row 0 north (the horizon mode's coarse pass, farfield.horizon_grid)."""
        a = self.tiles([(z, x, y)])[(z, x, y)]
        n = TILE // block
        return a.reshape(n, block, n, block).max(axis=(1, 3))

    # --- sampling --------------------------------------------------------
    def sample(self, lat, lon, zoom, progress=None):
        """Bilinear elevation (m) at arbitrary lat/lon arrays from zoom-level tiles. Points from polardem.FROM_LAT
        (84.9 deg) on (Mapterhorn's heights stop short of 85, Web Mercator at 85.05) come from the Copernicus DEM
        (polardem.py; 0 m on the Arctic Ocean)."""
        if zoom > max(MAX_ZOOM, FINE_ZOOM):
            raise ValueError(f"zoom {zoom} > {max(MAX_ZOOM, FINE_ZOOM)}")
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        shape = np.broadcast(lat, lon).shape
        lat, lon = np.broadcast_to(lat, shape).ravel(), np.broadcast_to(lon, shape).ravel()
        out = np.zeros(lat.shape, np.float64)
        valid = np.abs(lat) < polardem.FROM_LAT
        if not valid.all():                 # the polar caps: the Copernicus DEM (polardem.py)
            px = 40075016.686 * math.cos(math.radians(MAX_LAT)) / (TILE * 2 ** zoom)
            try:
                out[~valid] = self.polar().sample(lat[~valid], lon[~valid], px)
            except (netfetch.FetchError, OSError, ValueError) as e:
                log.warning("polar DEM unavailable (%s): sea level there", e)
        if not valid.any():
            return out.reshape(shape)
        lat, lon = lat[valid], lon[valid]
        n = 2 ** zoom
        w = n * TILE
        # Global pixel coordinates; pixel centres sit at +0.5.
        fx = (lon / 360.0 + 0.5) * w
        fx -= w * np.round((fx - fx[0]) / w)    # unwrap across the antimeridian
        fx -= 0.5
        fy = (0.5 - np.arcsinh(np.tan(np.radians(lat))) / (2 * np.pi)) * w - 0.5
        tx0, tx1 = int(math.floor(fx.min())) // TILE, int(math.floor(fx.max()) + 1) // TILE
        ty0 = max(0, int(math.floor(fy.min())) // TILE)
        ty1 = min(n - 1, int(math.floor(fy.max()) + 1) // TILE)
        nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
        sparse = False
        if SPARSE_MIN_TILES < nx * ny <= MAX_TILES_PER_SAMPLE:
            # points along a line (a road's profile) in a large box: the whole box's tiles were fetched, decoded and
            # copied into one mosaic (I-80 over the salt flats: a road tile's grading took minutes, 2026-10-17)
            tid = (np.floor(fx).astype(np.int64) // TILE - tx0) * ny + (np.floor(fy).astype(np.int64) // TILE - ty0)
            sparse = nx * ny > SPARSE_RATIO * len(np.unique(tid))
        if nx * ny > MAX_TILES_PER_SAMPLE or sparse:
            # spread-out points (all the roads of a cell with a long highway, 2026-10-12: every road tile of
            # a session failed on one 53 x 82 box): sample groups of nearby tiles separately (the same values)
            g = SPARSE_GROUP if sparse else max(1, int(math.isqrt(MAX_TILES_PER_SAMPLE)) - 1)
            gx = (np.floor(fx).astype(np.int64) // TILE) // g
            gy = (np.floor(fy).astype(np.int64) // TILE) // g
            sub = np.zeros(len(lat), np.float64)
            for key in np.unique(np.stack([gx, gy], 1), axis=0):
                m = (gx == key[0]) & (gy == key[1])
                sub[m] = self.sample(lat[m], lon[m], zoom, progress)
            out[valid] = sub
            return out.reshape(shape)
        keys = [(zoom, tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]     # (row-major)
        got = self.tiles(keys, progress)
        fx -= tx0 * TILE
        fy -= ty0 * TILE
        # (sampled from the tiles in place: a mosaic of them copied on every call cost ~2 s per 16 city road tiles)
        fast.require(76, "bilinear_tiles")
        out[valid] = fast._ef.bilinear_tiles([np.asarray(got[k], np.float32) for k in keys], nx, TILE, fx, fy)
        return out.reshape(shape)
