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

Zoom 13 and deeper come from Mapterhorn's regional PMTiles archives by range reads (mtarchive.py: Source Cooperative's
mirror, then download.mapterhorn.com; the same bytes as the endpoint's), the tile endpoint only when neither can be
read (2026-11-15); zooms 0-12 from the project's tile host's packs (MT_CDN_*) when its switch says so.

Requests go through a netfetch.Fetcher (rate limit, retries, circuit breaker per host). When
Mapterhorn cannot be asked (circuit open, rate limited, failing) the same tile comes from AWS
(failover); if AWS fails too, DemUnavailable. 404s are remembered in the tile column's miss index (missindex.py)
and expire after the host's miss_ttl (Mapterhorn 90 days: it adds lidar areas; AWS never).
"""
from runtimeprofiler import step as profile_step
import contextlib
import io
import json
import logging
import math
import missindex
import mtarchive
import os
import sys
import threading
import time
from collections import Counter, OrderedDict
from runtimeprofiler import ThreadPoolExecutor

import numpy as np
from PIL import Image

import cache as cachemod
import dsmground
import fast
import dmath
import dnp
import hkdtm as hkdtmmod
import hklidar
import hrdem as hrdemmod
import lidarbc as lidarbcmod
import netfetch
import polardem
import revisit
import staging
import xproc

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
# (3 GB but on a small machine: one 30 km drive needs ~1.1 GB of decoded tiles, so at 1 GB the mirror cut back the
# tiles of the same drive and wrote them again, and none of a previous drive's was left: ~1.2 GB of SSD writes in a
# warm 8 min drive at 150 mph, 2026-11-04. BNGE_DEM_MIRROR_MB sets it.)
def _mirror_mb():
    v = os.environ.get("BNGE_DEM_MIRROR_MB")
    if v:
        return float(v)
    import budget
    return 1024 if budget.small() else 3072


MIRROR_MB = _mirror_mb()
# The mirror's tiles packed (earthfast f32z.rs: ~13 % of the .npy, 120-175 KB a tile; 1.7 ms to pack, 0.7 ms to unpack
# against ~4 ms to decode the WebP and fill its holes), each process's own array instead of shared mapped pages. On by
# default since 2026-11-13: the plain .npy were 1.1-3.1 GB of a cold visit's 4.8-7.9 GB of disk writes, from ~0.15 GB
# of WebP (expo baseline 2026-11-12). BNGE_DEM_MIRROR_ZSTD=0: the .npy (memory-mapped) as before.
MIRROR_ZSTD = os.environ.get("BNGE_DEM_MIRROR_ZSTD", "1") != "0"
# Decoded tiles kept per process with the packed mirror (budget.apply: a desktop's MEM_TILES at most this). Packed, a
# tile in memory is the process's own 1 MB, no longer pages every process shares, and one read back from the mirror
# costs ~0.5 ms (its 115 KB in the system's file cache, shared). A 24 GB machine's desktop size (429) filled in every
# road, block, late and far-tile process: 25 road tiles round the A7 or Berlin each touched 545+ tiles; at 256 the
# extra reads cost no measurable build time and every output stayed byte-identical (CT 115, 2026-11-16).
PACKED_MEM_TILES = 256
if MIRROR_ZSTD:
    fast.require(146, "the packed DEM mirror (f32_pack)")
CLAIM_WAIT_S, CLAIM_POLL_S, CLAIM_STALE_S = 0.5, 0.01, 30.0     # (Dem._load_tile: another process making a tile)
MIRROR_PRUNE_S = 120.0  # the mirror pruned at most this often per process (each prune walks it all)
MIRROR_MAX_AGE_S = 30 * 86400
SHORT_RETRY_S = 30.0        # s a stand-in tile (made while a download failed) is kept in memory before it is made again
DECODED_VERSION = 4          # 2: tiles past the lidar's depth upsampled from its deepest tile, not z12; its holes
                             # filled (fill_holes); 3: Canada's HRDEM lidar (hrdem.py) from z13 down; 4: LidarBC,
                             # and z12 / z13 shifted to its ground (BIAS_*)
# The mirror's own generation (its folder: decoded/v<DECODED_VERSION>m<MIRROR_GEN>), apart from DECODED_VERSION, which is
# in the format-keyed caches' identity (mapdata.dem_identity: raising it plans every player's cells again). 2
# (2026-11-18): before, a tile upsampled from a stand-in ancestor kept in memory (a Mapterhorn tile that failed, AWS's
# in its place) could be written to the mirror as Mapterhorn's and read back as complete for 30 days; the older folder
# is not read and goes first when the mirror is cut back (oldest written first). The cost: each tile is decoded again
# once when next read (~10 ms CPU against ~0.7 ms to read it back; the 8-place corpus's 12,103 mirrored tiles ~120
# CPU-s, ~15 s a place; a full 3 GB mirror ~20,000 tiles, ~200 CPU-s spread over the visits that read them) and written
# again (~150 KB a tile).
MIRROR_GEN = 2
# The elevation's identity in the format-keyed caches (mapdata.dem_identity: road plans, cell facts, the road tiles and
# the shared decisions' version key), pinned per app release (the user, 2026-11-16, the road revamp's decision D5: "Pin
# per app release"): the plans made on one release's elevation are kept until a release changes what the ground
# reads, and are never mixed with plans made on another. Raise it in the release whose elevation changes: a new source
# read (a lidar, a DEM), a source's pinned build moved (the Mapterhorn CDN packs' release, the regional archives'
# bindings), a filter or bias rule. The decoding (DECODED_VERSION) and the measured road heights (roadz.VERSION) are in
# the key on their own; the BNE_* elevation switches (BNE_DEM_ZOOM, BNE_HRDEM...) through revisit.switches_key. What
# the public Mapterhorn endpoint changes between releases is not followed (D5a): it reaches the plans with the next
# release that raises this.
ELEVATION_ID = 1
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
COVERAGE_RETRY_MAX_S = 86400.0      # a stale list's failed refreshes: asked again after COVERAGE_RETRY_S, doubling to this
_coverage = {"index": None, "at": -1e18, "lock": threading.Lock()}


def coverage_index(doc):
    """{(x, y) of a z6 tile: the deepest zoom Mapterhorn has under it} from its download_urls.json. Pure."""
    fast.require(565, "Mapterhorn coverage index")
    return fast._ef.dem_coverage(doc.get("items", ()), COVERAGE_ZOOM)


AWS = dict(name="aws-terrarium", ext="png", size=256,
           url="https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png")
# Raw tiles whose disk write failed (a full disk: OSError 28) kept in memory instead, up to this many bytes per process,
# and their 404s remembered here: the write's error failed the tile (and its far tile), whose next try downloaded the
# same bytes again, every few seconds while the disk stayed full (telemetry 2026-11-11: 862k far tiles failed with
# OSError 28 in 7 days, one line of earthd). MEM_MISSES: 404s kept so at most.
MEM_RAW_BYTES = 96 * 1024 * 1024
MEM_MISSES = 200000
# Mapterhorn's z0-12 from the project's own tile host (cdn.py; the data repo's dem workflow, tools/build_mt_packs.py):
# planet.pmtiles' tiles repacked as they are (the same bytes as tiles.mapterhorn.com: compared tile for tile, 2026-11-11)
# into packs of 8 x 8 (tilepack.py; zooms 0-2 one pack each), so one request is 64 tiles and the Mapterhorn endpoint
# (one maintainer, donations; players asked it 53 M times a day, 2026-11-11) is left the deeper zooms. index.bin: which
# packs exist (tilepack.index_*), the last file of a release written: a pack it does not list is open sea, its 64 tiles
# 404s without a request. Off unless the switch (cdn.json "dem" -> "bngmt") or BNGE_DEM_CDN (a release, or "off")
# names a release; a pack that cannot be had falls back to the endpoint.
MT_CDN_K = 3
MT_CDN_MAX_ZOOM = GLOBAL_ZOOM
MT_CDN_RETRY_S = 600.0              # the index not to be had: the endpoint alone, asked again after this
PACK_WAIT_S = 8.0                   # another process fetching a pack: its tile waited for at most this long
PACK_CLAIM_STALE_S = 30.0           # a pack claim older than this: its process ended mid-fetch
# One download of a raw tile on the machine at a time (_fetch_raw): the server's processes (main, terrain blocks, far
# tiles, road tiles) all start on a fresh place together and each met the same cold tiles, and the probes, the
# prefetch and the loads of one process did not share an in-flight registry either (telemetry 2026-11-15: ~60 M
# Mapterhorn requests a day). A process (or thread) that finds a tile being downloaded waits for it on disk (an
# xproc lock file, <dem>/locks/<src>-<z>-<x>-<y>.lock: dead holders' broken, Windows too) for up to DL_WAIT_S, on to
# DL_LIVE_S while its holder is alive and at it; then downloads it itself. The cache is looked at again once the lock
# is had.
SINGLE_FLIGHT = os.environ.get("BNGE_DEM_SINGLE_FLIGHT", "1") != "0"
# Zoom 13 and deeper from Mapterhorn's regional PMTiles archives by range reads (mtarchive.py: Source Cooperative's
# mirror, then download.mapterhorn.com), the tile endpoint only when neither can be read, and never for a prefetch or
# a pre-download (BNGE_DEM_ARCHIVES=0: the endpoint, as before).
ARCHIVES = mtarchive.ON
SPECULATIVE = ("prefetch", "predownload", "background")
DL_WAIT_S = 30.0
DL_LIVE_S = 120.0
DL_URGENT_WAIT_S = 10.0             # (the urgent lane's: no live wait)
_MISS = ("no tile",)                # (_fetch_raw's ready(): a 404 another process remembered)
# Callers of the raw reads (_note): the module that asked (frames of these skipped), for the telemetry's DEM detail.
_SKIP_MODULES = frozenset(("dem", "runtimeprofiler", "contextlib", "functools", "threading",
                           "concurrent.futures.thread", "concurrent.futures._base", "pools"))
_cdn_env = os.environ.get("BNGE_DEM_CDN", "").strip()
MT_CDN_RELEASE = None if _cdn_env.lower() in ("", "off", "0", "none") else _cdn_env


def mt_cdn_base(release):
    import imagery as imagerymod
    return f"{imagerymod.CDN_BASE}/dem/mapterhorn/{release}"


def _xkind(kind):
    """A request kind as xproc ranks its lock holders."""
    return kind if kind in xproc.KIND_RANK else "demand"


class DemUnavailable(IOError):
    """Neither Mapterhorn nor AWS could deliver a tile (network, offline)."""


def zoom_for(lat, pixel_m, max_zoom=MAX_ZOOM):
    """Smallest zoom whose 512 px tile pixels are <= pixel_m at this latitude."""
    c = 40075016.686 * max(dmath.cos(math.radians(min(abs(lat), MAX_LAT))), 1e-6)
    return int(min(max_zoom, max(0, math.ceil(dmath.log2(c / (TILE * pixel_m))))))


def decode_terrarium(data, size):
    """Decode an AWS Terrarium or Mapterhorn tile into float32 elevations."""
    fast.require(565, "DEM tile decode")
    return fast._ef.dem_decode(bytes(data), size, _read_image)


def _read_image(data, size):
    """Pillow codec for nonstandard image modes, malformed inputs and resampling."""
    im = Image.open(io.BytesIO(data))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    if im.size != (size, size):
        log.warning("unexpected DEM tile size %s (expected %d), resampling", im.size, size)
        im = im.resize((size, size), Image.BILINEAR)
    return np.ascontiguousarray(np.asarray(im))


HOLE_FILL_M = 600.0          # lidar holes up to ~2x this wide filled (a coarse tile's are whole city blocks: a
HOLE_FILL_MAX_PX = 24        # Rotterdam district read 67 % zeros at z9-z11, ~1 % after 24 px; 2026-10-21); wider
                             # zero areas are the sea


def hole_px(z, y):
    """fill_holes' rounds for tile row y at zoom z: HOLE_FILL_M in its pixels, 2 .. HOLE_FILL_MAX_PX."""
    fast.require(565, "DEM hole radius")
    return fast._ef.dem_hole_px(z, y, TILE, HOLE_FILL_M, HOLE_FILL_MAX_PX)


def fill_holes(a, k=HOLE_FILL_MAX_PX):
    """Lidar tile a with its small exact-0 holes filled from their surroundings. National lidar terrain models have
    no ground under buildings, and Mapterhorn writes those cells as 0 (Rotterdam's AHN: 74 % of the zeros in
    Spangen on land, 2026-10-21): a polder at -1.5 m got 0 m pits, and seacarve read them as the sea. Zeros take
    the mean of their non-zero 4-neighbours, k rounds; zero areas wider than that (the sea, which reads exactly
    0) keep 0, the band filled into their edge too. Pure."""
    fast.require(65, "dem.fill_holes")     # (earthfast fill_holes, 2026-10-23: the same values as the numpy rounds)
    return fast._ef.fill_holes(np.ascontiguousarray(a, np.float32), int(k))


def _tile_xy(lat, lon, z):
    """Web-Mercator tile (x, y) holding (lat, lon) at zoom z."""
    fast.require(565, "DEM tile coordinates")
    return fast._ef.dem_xy(lat, lon, z, MAX_LAT)


def _stale(marks):
    """A kept stand-in tile's time to be made again (Dem._lru_short's marks, SHORT_RETRY_S)."""
    return marks is not None and time.monotonic() - marks[2] > SHORT_RETRY_S


def _replay(marks):
    """A kept tile's marks (Dem._load_marked) said again in the reading thread: its builds are short too."""
    if marks:
        if marks[0]:
            revisit.fault(here=True)
        if marks[1]:
            revisit.fault(lidar=True)


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
        # (z, x, y) -> (faults, lidar rule faults, when made) of a tile in _lru made while something it read failed (a
        # stand-in for a download that failed): whoever reads the kept copy is short of it too (_replay; only its first
        # readers were told: a cell planned later from the kept stand-in counted as complete), and it is made again
        # after SHORT_RETRY_S (kept, it stood in for the session: no read of it was ever complete again)
        self._lru_short = {}
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
        # (the LiDAR sheets the road heights read too: <cache>/hklidar)
        self.hkdtm = (hkdtmmod.HkDtm(hklidar.at(os.path.dirname(cache_dir), self.fetcher)) if hkdtmmod.ON
                      else None)
        self.hkdtm_served = set()       # tile keys with Hong Kong's LiDAR heights in them
        self._mem_raw = OrderedDict()   # (src name, z, x, y) -> bytes not written to disk (MEM_RAW_BYTES)
        self._mem_raw_bytes = 0
        self._mem_miss = OrderedDict()  # (src name, z, x, y) -> 404s not written to disk (MEM_MISSES)
        self._nowrite_logged = False
        self.cdn_release = MT_CDN_RELEASE   # the packs' release (MT_CDN_*), None: the endpoint alone
        self._cdn_idx = None            # (release, index bytes) once read
        self._cdn_failed_at = -1e18
        self._pack_locks = {}           # pack key -> lock: one fetch of a pack at a time in this process
        # zoom 13 and deeper from Mapterhorn's regional archives (mtarchive.py), the endpoint only when they cannot
        # be read
        self.archives = (mtarchive.Archives(cache_dir, self.fetcher, self._archive_items)
                         if ARCHIVES and fast.AVAILABLE and fast._ef is not None
                         and getattr(fast._ef, "VERSION", 0) >= mtarchive.NATIVE else None)
        import multiprocessing
        if SINGLE_FLIGHT and multiprocessing.current_process().name == "MainProcess":
            self._prune_locks()

    def _prune_locks(self):
        """Download locks (_fetch_raw) left by processes that ended mid-download, older than a day: removed (a lock
        is broken when its tile is asked again; one never asked again stayed)."""
        d = os.path.join(self.cache_dir, "locks")
        try:
            names = os.listdir(d)
        except OSError:
            return
        now = time.time()
        for n in names:
            p = os.path.join(d, n)
            try:
                if n.endswith(".lock") and now - os.path.getmtime(p) > 86400:
                    os.remove(p)
            except OSError:
                continue


    def clear_memory(self):
        """Drop the decoded tiles kept in memory (budget.maybe_shed)."""
        with self._lock:
            self._lru.clear()
            self._lru_short.clear()
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
        lat_of = lambda yy: math.degrees(dmath.atan(dmath.sinh(math.pi * (1 - 2 * yy / n))))     # noqa: E731
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
        fast.require(565, "regional DEM bias field")
        out = fast._ef.dem_bias(key, a, plat, plon, np.asarray(ph, np.float64), BIAS_GRID, BIAS_SMOOTH)
        return a if out is None else out

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
        out = a if h is None else self._blend(key, a, h, ok)
        if out is a:
            return a
        for name in used:
            self._count(name + ":tile")
        with self._lock:
            for name in used:
                (self.lidarbc_served if name == "lidarbc" else self.hrdem_served).add(key)
        return out

    def _hongkong(self, key, a):
        """Tile key's heights a with Hong Kong's lidar terrain (hkdtm.py) put in where this run reads its sheets (the
        near terrain). Where it goes without one it has, the tile is not mirrored and nothing built on it kept."""
        if a is None or self.hkdtm is None or not hkdtmmod.covers(*key):
            return a
        h, ok, short = self.hkdtm.tile(*key)
        if short:
            self._count("hkdtm:short")
            self._partial()
            revisit.fault()
        out = self._blend(key, a, h, ok)
        if out is not a:
            self._count("hkdtm:tile")
            with self._lock:
                self.hkdtm_served.add(key)
        return out

    def _blend(self, key, a, h, ok):
        """Tile key's heights a with lidar heights h put in where ok, their edges feathered into a over
        HRDEM_FEATHER_M (a new array; a itself where there are none off its sea)."""
        fast.require(565, "regional DEM blending")
        out = fast._ef.dem_blend(key, a, h, ok, HRDEM_FEATHER_M, HRDEM_FEATHER_MAX_PX)
        return a if out is None else out

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
        it cannot be had: every tile is asked then. One per process. An old list's refresh failing keeps the old list
        (stale-if-error): that failure faults no build (netfetch.stale_ok), is logged once, and is asked again after
        COVERAGE_RETRY_S, doubling per failure in a row up to COVERAGE_RETRY_MAX_S (netfetch.Backoff)."""
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
            stale = None        # (an older list on disk: kept when the refresh below fails, so plans don't change
            try:                # with whether one download worked: offline, a host down, a frozen test cache)
                age = now - os.path.getmtime(path)
                with open(path, "rb") as f:
                    doc = json.loads(f.read())
                if age < COVERAGE_MAX_AGE_S:
                    c["index"], c["at"], c["failed"], c["doc"] = coverage_index(doc), now - age, False, doc
                    c["archives"] = mtarchive.archives_of(doc)
                    return c["index"]
                stale = doc
            except (OSError, ValueError):
                pass
            if stale is None and c["index"] is not None:
                stale = c.get("doc")      # (the list this process read before, its file gone meanwhile)
            if stale is not None and not (isinstance(stale, dict) and coverage_index(stale)):
                stale = None            # (an old file that lists nothing usable is no copy)
            backoff = c.get("backoff")
            if backoff is None:
                backoff = c["backoff"] = netfetch.Backoff(COVERAGE_RETRY_S, COVERAGE_RETRY_MAX_S)
            try:
                with netfetch.stale_ok(stale is not None):      # (an old list in hand: its refresh is no build's)
                    body = self.fetcher.get(COVERAGE_URL, kind="demand").body
                doc = json.loads(body)
                idx = coverage_index(doc)
                if not idx:
                    raise ValueError("no z6 files listed")
                c["archives"] = mtarchive.archives_of(doc)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(tmp, "wb") as f:
                    f.write(body)
                os.replace(tmp, path)
                c["index"], c["at"], c["failed"], c["doc"] = idx, now, False, doc
                backoff.ok()
            except Exception as e:      # (offline, a host down, another format): the older list if there is one,
                idx = coverage_index(stale) if stale is not None else None      # else ask every tile
                if idx:     # (retried after the backoff, not the full max age)
                    netfetch.refresh_failed(log, backoff, now, "dem: Mapterhorn's file list", e)
                    c["index"], c["at"], c["failed"] = idx, backoff.until - COVERAGE_MAX_AGE_S, False
                    c["archives"], c["doc"] = mtarchive.archives_of(stale), stale
                else:
                    log.debug("dem: Mapterhorn coverage: %s: %s", type(e).__name__, e)
                    c["at"], c["failed"] = now, True
            return c["index"]

    def coarse_at(self, lat, lon):
        """Is the elevation at (lat, lon) only the coarse global model (Mapterhorn's file list has nothing deeper than
        GLOBAL_ZOOM under its z6 tile: no lidar, ~30 m Copernicus and the like)? False when unknown (no list yet) or
        where the opt-in Canadian lidar reads. grading.ease_coarse (GitHub #414). True over Hong Kong's DTM (hkdtm.py)
        too: the grading's lidar-terrain underpass rules dug Sha Tin's roads 4-6 m under its unmeasured viaducts there
        (ground roads p90 4.8 m off the DTM, 0.2 m with the coarse rules)."""
        idx = self._coverage()
        if idx is None:
            return False
        if idx.get(_tile_xy(lat, lon, COVERAGE_ZOOM), GLOBAL_ZOOM) > GLOBAL_ZOOM:
            return False
        if (self.lidarbc is not None or HRDEM_NEAR) and hrdemmod.in_bounds(GLOBAL_ZOOM, *_tile_xy(lat, lon, GLOBAL_ZOOM)):
            return False
        return True

    def _archive_items(self):
        """The file list's regional archives (mtarchive.archives_of), or None while there is no list."""
        if self._coverage() is None:
            return None
        return _coverage.get("archives")

    def _listed(self, z, x, y):
        """False when Mapterhorn's file list says it has no tile (z, x, y) (z > GLOBAL_ZOOM); True otherwise."""
        fast.require(565, "Mapterhorn file depth")
        return fast._ef.dem_listed(self._coverage(), z, x, y, COVERAGE_ZOOM, GLOBAL_ZOOM)

    def _download(self, src, z, x, y, kind="demand"):
        """Raw tile bytes from disk cache or network; None for a (cached) 404. Raises
        netfetch.FetchError when the host cannot deliver (netfetch.Deferred for a refused prefetch)."""
        if src is MAPTERHORN and z > GLOBAL_ZOOM and not self._listed(z, x, y):
            self._count(src["name"] + ":unlisted")
            self._note(src, z, "unlisted")
            return None
        path = self._path(src, z, x, y)
        miss = os.path.join(self.cache_dir, src["name"], str(z), str(x), f"{y}.404")
        data = self.disk.read(path)         # None: not cached (or evicted a moment ago)
        if data is not None:
            self._count(src["name"] + ":disk")
            self._note(src, z, "disk")
            return data
        mk = (src["name"], z, x, y)
        if self._mem_raw or self._mem_miss:
            with self._lock:
                data = self._mem_raw.get(mk)
                if data is not None:
                    self._mem_raw.move_to_end(mk)
                elif mk in self._mem_miss:
                    self._note(src, z, "miss")
                    return None
            if data is not None:
                self._count(src["name"] + ":memory")
                self._note(src, z, "memory")
                return data
        url = src["url"].format(z=z, x=x, y=y)
        if self.fetcher.miss_alive(miss, url):
            self._note(src, z, "miss")
            return None
        if src is MAPTERHORN and z <= MT_CDN_MAX_ZOOM and self.cdn_release:
            got = self._download_pack(z, x, y, kind)
            if got is not False:
                self._note(src, z, "cdn")
                return got
        return self._fetch_raw(src, z, x, y, url, path, miss, mk, kind)

    def _fetch_raw(self, src, z, x, y, url, path, miss, mk, kind):
        """Raw tile (z, x, y) from src's host (_download's network part): one download on the machine at a time
        (SINGLE_FLIGHT); bytes, or None for a 404 (remembered)."""
        # (one folder of their own: not among the tiles, whose folders the map's cache view counts by name)
        lock = os.path.join(self.cache_dir, "locks", f"{src['name']}-{z}-{x}-{y}.lock")
        mine = False
        if SINGLE_FLIGHT:
            def ready():
                if os.path.exists(path):
                    got = self.disk.read(path)
                    if got is not None:
                        return got
                return _MISS if self.fetcher.miss_alive(miss, url) else None
            # (the urgent lane's loads, the block under the car: waited for DL_URGENT_WAIT_S at most)
            urgent = self.is_urgent() or threading.current_thread().name.startswith("dem-urgent")
            got, mine = xproc.once(lock, ready, DL_URGENT_WAIT_S if urgent else DL_WAIT_S,
                                   kind=_xkind(kind),
                                   live_s=None if urgent else DL_LIVE_S, patient=True)
            if got is not None:             # (another process's, or another thread's, download)
                self._count(src["name"] + ":waited")
                self._note(src, z, "waited")
                return None if got is _MISS else got
        try:
            if self.archives is not None and src is MAPTERHORN and z >= mtarchive.MIN_ZOOM:
                got = self._from_archives([(z, x, y)], kind).get((z, x, y), False)
                if got is not False:
                    return got
                if kind in SPECULATIVE and self.archives.region(z, x, y) is not None:
                    # (an archive has it but cannot be read now: the endpoint is the demand's last resort, never a
                    # guess's; with no file list at all every tile is asked as before)
                    self._note(src, z, "deferred")
                    raise netfetch.Deferred(f"mapterhorn {z}/{x}/{y}: the archives cannot be read now", url,
                                            netfetch.host_of(url))
            try:
                data = self.fetcher.get(url, kind=kind).body
            except netfetch.NotFound:
                self._remember_miss([(mk, miss)])   # (again: its time starts the miss TTL)
                self._count(src["name"] + ":404")
                self._note(src, z, "404")
                return None
            except netfetch.Deferred:
                self._count(src["name"] + ":error")
                self._note(src, z, "deferred")
                raise
            except netfetch.FetchError:
                self._count(src["name"] + ":error")
                self._note(src, z, "error")
                raise
            self._keep(mk, path, data)
            try:
                missindex.clear(miss, self.disk)    # an expired miss: the host has data there now
            except OSError:
                pass
            self._count(src["name"] + ":net")
            self._note(src, z, "net")
            return data
        finally:
            if mine:
                xproc.release(lock)

    def _from_archives(self, keys, kind):
        """Mapterhorn tiles keys from the regional archives (mtarchive.py), written to the raw cache as the endpoint's
        would be (a tile an archive lacks: its 404 remembered): {key: bytes or None} for those an archive answered;
        the others left out (the endpoint's to ask). Raises netfetch.Deferred (a prefetch refused)."""
        name = MAPTERHORN["name"]
        got = self.archives.fetch(keys, kind)
        misses = []
        for (z, x, y), b in got.items():
            mk = (name, z, x, y)
            marker = os.path.join(self.cache_dir, name, str(z), str(x), f"{y}.404")
            if b is None:
                misses.append((mk, marker))
                self._count(name + ":archive404")
                self._note(MAPTERHORN, z, "archive404")
                continue
            self._keep(mk, self._path(MAPTERHORN, z, x, y), b)
            try:
                missindex.clear(marker, self.disk)
            except OSError:
                pass
            self._count(name + ":archive")
            self._note(MAPTERHORN, z, "archive")
        if misses:
            self._remember_miss(misses)
        for k in keys:
            if k not in got:
                self._note(MAPTERHORN, k[0], "archive_err")
        return got

    def _archive_batch(self, keys, kind):
        """Warm the raw cache for keys (Mapterhorn, zoom 13+) from the archives in as few range reads as they allow:
        the keys with neither a decoded nor a raw tile nor a remembered 404, each this process's to fetch (its download
        lock had at once; one another process holds is left to it), and where a z16 tile proves missing the tile its
        heights will come from (the deepest of its z15 / z14 / z13 ancestors there; no request to find it: the
        directories say). A cache warm only: what the loads then make is decided by them as before."""
        if self.archives is None:
            return
        name = MAPTERHORN["name"]
        todo = []
        for k in dict.fromkeys(keys):
            z, x, y = k
            if z < mtarchive.MIN_ZOOM or not self._listed(z, x, y):
                continue
            if os.path.exists(self._path(MAPTERHORN, z, x, y)) or os.path.exists(self._mirror_path(k)):
                continue
            if (name,) + k in self._mem_raw or self._known_miss(*k):
                continue
            todo.append(k)
        rounds = 0
        while todo and rounds < 4:
            rounds += 1
            held = []
            for k in todo:
                lock = os.path.join(self.cache_dir, "locks", f"{name}-{k[0]}-{k[1]}-{k[2]}.lock")
                if not SINGLE_FLIGHT or xproc.take(lock, _xkind(kind)):
                    held.append((k, lock))
            try:
                got = self._from_archives([k for k, _ in held], kind) if held else {}
            except netfetch.Deferred:
                got = {}
            finally:
                if SINGLE_FLIGHT:
                    for _, lock in held:
                        xproc.release(lock)
            # (a missing tile's heights come from its parent: z16 always, z15 / z14 with the ground filter)
            nxt = []
            for (z, x, y), b in got.items():
                if b is None and z > mtarchive.MIN_ZOOM and (z > MAX_ZOOM or self.ground_filter):
                    p = (z - 1, x >> 1, y >> 1)
                    if (self._listed(*p) and not os.path.exists(self._path(MAPTERHORN, *p))
                            and not os.path.exists(self._mirror_path(p)) and not self._known_miss(*p)):
                        nxt.append(p)
            todo = list(dict.fromkeys(nxt))

    def _caller(self):
        """The module that asked for the tiles this thread reads now (a pool task: the one that queued it)."""
        c = getattr(self._local, "caller", None)
        if c is not None:
            return c
        f = sys._getframe(1)
        while f is not None and f.f_globals.get("__name__") in _SKIP_MODULES:
            f = f.f_back
        name = f.f_globals.get("__name__") if f is not None else None
        return (name or "pool").rsplit(".", 1)[-1][:20]

    def _as(self, caller, fn, *args):
        """fn(*args) with this thread's reads counted as caller's (a pool task queued by caller)."""
        prev = getattr(self._local, "caller", None)
        self._local.caller = caller
        try:
            return fn(*args)
        finally:
            self._local.caller = prev

    def _note(self, src, z, outcome):
        """A raw tile read's telemetry detail (netfetch.note_detail): the source, the caller, the zoom, how it was
        answered. No position."""
        netfetch.note_detail(f"{src['name']}:{self._caller()}:{z}:{outcome}")

    def _keep(self, mk, path, data):
        """Downloaded tile bytes written to the disk cache; when that fails (a full disk, a folder that cannot be
        written) kept in this process's memory instead (MEM_RAW_BYTES, the oldest dropped), so the tile is not failed
        and downloaded again at its next try."""
        tmp = f"{path}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            staging.replace_cached(tmp, path)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            with self._lock:
                old = self._mem_raw.pop(mk, None)
                self._mem_raw_bytes -= len(old) if old is not None else 0
                self._mem_raw[mk] = data
                self._mem_raw_bytes += len(data)
                while self._mem_raw_bytes > MEM_RAW_BYTES and len(self._mem_raw) > 1:
                    self._mem_raw_bytes -= len(self._mem_raw.popitem(last=False)[1])
                first, self._nowrite_logged = not self._nowrite_logged, True
            self._count("dem:nowrite")
            if first:
                log.warning("dem: tiles cannot be written to the cache (%s: %s): kept in memory for this session",
                            type(e).__name__, e)
            return False
        self.disk.wrote(path, len(data))
        return True

    def _remember_miss(self, items):
        """404s [(memory key, marker path)] recorded in their columns' miss indexes (missindex.py); kept in memory
        when that cannot be written."""
        try:
            missindex.record([m for _, m in items], self.disk)
        except OSError:
            with self._lock:
                for mk, _ in items:
                    self._mem_miss[mk] = True
                while len(self._mem_miss) > MEM_MISSES:
                    self._mem_miss.popitem(last=False)
            self._count("dem:nowrite_miss")

    # --- Mapterhorn's z0-12 from the project's tile host (MT_CDN_*) --------------------------------
    def _cdn_index(self):
        """The release's index.bin (bytes: tilepack.index_has), from disk or the tile host once; None when it cannot
        be had (the endpoint alone for MT_CDN_RETRY_S)."""
        rel = self.cdn_release
        got = self._cdn_idx
        if got is not None and got[0] == rel:
            return got[1]
        if time.monotonic() - self._cdn_failed_at < MT_CDN_RETRY_S:
            return None
        import tilepack
        path = os.path.join(self.cache_dir, "bngmt", rel, "index.bin")
        data = None
        try:
            with open(path, "rb") as f:
                data = f.read()
            tilepack.index_check(data)
        except (OSError, tilepack.PackError):
            data = None
        if data is None:
            try:
                data = self.fetcher.get(mt_cdn_base(rel) + "/index.bin", kind="demand").body
                tilepack.index_check(data)
            except (netfetch.FetchError, tilepack.PackError) as e:
                log.info("dem: the tile host's Mapterhorn packs (%s): no index (%s: %s): the endpoint meanwhile",
                         rel, type(e).__name__, e)
                self._cdn_failed_at = time.monotonic()
                return None
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except OSError:
                pass
        self._cdn_idx = (rel, data)
        return data

    def _download_pack(self, z, x, y, kind):
        """Mapterhorn tile (z, x, y) (z <= MT_CDN_MAX_ZOOM) from its pack on the tile host: the pack fetched once and
        every tile of it written to the cache as Mapterhorn's own (a tile it lacks: a 404 remembered, as the endpoint
        would answer). The bytes, None (no tile there), or False: ask the endpoint (no index, the host failing, a
        damaged pack)."""
        import tilepack
        idx = self._cdn_index()
        if idx is None:
            return False
        k = min(MT_CDN_K, z)
        (pz, px, py), _ = tilepack.pack_of(z, x, y, k)
        name = MAPTERHORN["name"]

        def miss(tz, tx, ty):
            return (name, tz, tx, ty), os.path.join(self.cache_dir, name, str(tz), str(tx), f"{ty}.404")
        if not tilepack.index_has(idx, pz, px, py):     # open sea: none of its tiles exists
            self._remember_miss([miss(*tilepack.tile_at(pz, px, py, k, i)) for i in range(dnp.sq(1 << k))])
            self._count("bngmt:unlisted")
            return None
        with self._lock:
            lock = self._pack_locks.setdefault((pz, px, py), threading.Lock())
        with lock:
            path = self._path(MAPTERHORN, z, x, y)
            data = self.disk.read(path)             # (another tile of the pack fetched it meanwhile)
            if data is not None:
                self._count(name + ":disk")
                return data
            with self._lock:
                data = self._mem_raw.get((name, z, x, y))
                if data is None and (name, z, x, y) in self._mem_miss:
                    return None
            if data is not None:
                return data
            # (one process fetches a pack, the others wait for its tiles on disk: a cold place's 25 packs were asked
            # 48 times by the server's processes at once, 2026-11-12)
            claim = os.path.join(self.cache_dir, "bngmt", self.cdn_release, "claims", f"{pz}_{px}_{py}")
            if not self._pack_claim(claim):
                got = self._pack_wait(path, miss(z, x, y)[1])
                if got is not False:
                    self._count("bngmt:waited")
                    return got
            try:
                return self._fetch_pack(pz, px, py, k, x, y, kind, miss)
            finally:
                try:
                    os.remove(claim)
                except OSError:
                    pass

    def _pack_claim(self, claim):
        """True: this process fetches the pack (its claim file made, or claims cannot be made here); False: another
        process has fetched it within PACK_CLAIM_STALE_S."""
        for _ in range(2):
            try:
                os.makedirs(os.path.dirname(claim), exist_ok=True)
                os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return True
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(claim) < PACK_CLAIM_STALE_S:
                        return False
                    os.remove(claim)        # (a process that ended mid-pack)
                except OSError:
                    pass
            except OSError:
                return True
        return True

    def _pack_wait(self, path, marker):
        """The tile another process's pack fetch writes at path: its bytes, None (the pack lacks it: its 404
        recorded), or False when it did not come within PACK_WAIT_S (fetched here then)."""
        end = time.monotonic() + PACK_WAIT_S
        while time.monotonic() < end:
            time.sleep(CLAIM_POLL_S * 2)
            data = self.disk.read(path)
            if data is not None:
                return data
            if missindex.known(marker):
                return None
        return False

    def _fetch_pack(self, pz, px, py, k, x, y, kind, miss):
        import tilepack
        name = MAPTERHORN["name"]
        url = f"{mt_cdn_base(self.cdn_release)}/{tilepack.path(pz, px, py)}"
        try:
            body = self.fetcher.get(url, kind=kind).body
            tz_, tpx, tpy, tk, tiles = tilepack.parse(body)
            if (tz_, tpx, tpy, tk) != (pz, px, py, k):
                raise tilepack.PackError(f"pack {tz_}/{tpx}/{tpy} k{tk} where {pz}/{px}/{py} k{k} was asked")
        except netfetch.Deferred:
            raise                                   # (a prefetch refused: the caller waits, as for the endpoint)
        except (netfetch.FetchError, tilepack.PackError) as e:
            self._count("bngmt:fallback")
            log.debug("dem: pack %s: %s: %s: the endpoint", url, type(e).__name__, e)
            return False
        out = None
        misses = []
        for i in range(dnp.sq(1 << k)):
            tz, tx, ty = tilepack.tile_at(pz, px, py, k, i)
            b = tiles.get(i)
            if b is None:
                misses.append(miss(tz, tx, ty))
                continue
            self._keep((name, tz, tx, ty), self._path(MAPTERHORN, tz, tx, ty), b)
            if (tx, ty) == (x, y):
                out = b
        if misses:
            self._remember_miss(misses)
        self._count("bngmt:net")
        return out

    def _ahead(self, keys):
        """Start the raw Mapterhorn downloads of keys at once (on disk afterwards, or a remembered 404); one request
        per tile however many ask. Returns their futures (result: the bytes or None; errors are left for the caller's
        own _download to meet)."""
        futs = []
        caller = self._caller()
        with self._lock:
            for k in keys:
                f = self._probing.get(k)
                if f is None:
                    f = self._probing[k] = self.probe_pool.submit(self._as, caller, self._probe, k)
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
        keys = list(keys)
        if self.archives is not None:       # (the zoom 13+ tiles first, in as few archive reads as they allow)
            self._archive_batch(keys, "prefetch")
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
        if missindex.known(os.path.join(self.cache_dir, MAPTERHORN["name"], str(z), str(x), f"{y}.404")):
            return True
        return z > GLOBAL_ZOOM and not self._listed(z, x, y)

    def keys_around(self, lat, lon, radius_m, zoom):
        """The tile keys (z, x, y) of zoom covering radius_m around lat/lon, nearest first; with the ground
        filter also their z12 parents (a z15 tile without lidar is made from z12 ground)."""
        import mapcells
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * max(0.05, dmath.cos(math.radians(lat))))
        x0, y0 = mapcells.tile_xy(lat + dlat, lon - dlon, zoom)
        x1, y1 = mapcells.tile_xy(lat - dlat, lon + dlon, zoom)
        cx, cy = mapcells.tile_xy(lat, lon, zoom)
        n = 2 ** zoom
        keys = [(zoom, x % n, y) for y in range(max(0, int(y0)), min(n - 1, int(y1)) + 1)
                for x in range(int(math.floor(x0)), int(math.floor(x1)) + 1)]
        keys.sort(key=lambda k: dnp.sq(k[1] + 0.5 - cx) + dnp.sq(k[2] + 0.5 - cy))
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
        fast.require(565, "AWS Terrarium mosaic")
        return fast._ef.dem_aws(self, AWS, (z, x, y), MAX_ZOOM, TILE)

    # --- the decoded mirror (MIRROR_MB) ----------------------------------------------------------
    def _mirror_path(self, key):
        z, x, y = key
        hk = "h" if self.hkdtm is not None and hkdtmmod.covers(z, x, y) else ""     # (made with Hong Kong's DTM)
        # (made with the opt-in Canadian lidar, the settings page's Experimental switch: BNE_LIDARBC's own files and
        # its bias, BNE_HRDEM_NEAR's whole HRDEM. Their tiles apart: off, the mirror's tiles as before)
        hk += ("b" if getattr(self, "lidarbc", None) is not None else "") + \
            ("n" if getattr(self, "hrdem_near", HRDEM_NEAR) else "")
        return os.path.join(self.cache_dir, "decoded", f"v{DECODED_VERSION}m{MIRROR_GEN}",
                            f"g{int(self.ground_filter)}{hk}", str(z), str(x),
                            f"{y}{'.f32z' if MIRROR_ZSTD else '.npy'}")

    def _mirror_get(self, key, readback=False):
        """A tile from the decoded mirror (a read-only view of its memory map), or None. readback: _mirror_put's
        own read (not counted as a hit: dem:mirror counts the tiles another decode was saved for)."""
        if MIRROR_MB <= 0:
            return None
        path = self._mirror_path(key)
        try:
            if time.time() - os.path.getmtime(path) > MIRROR_MAX_AGE_S:
                return None
            if MIRROR_ZSTD:             # (packed: this process's own array, not a shared mapping)
                with open(path, "rb") as fh:
                    a = fast._ef.f32_unpack(fh.read()).reshape(TILE, TILE)
            else:
                a = np.asarray(np.load(path, mmap_mode="r", allow_pickle=False))
        except (OSError, ValueError, EOFError):
            return None
        if a.shape != (TILE, TILE) or a.dtype != np.float32:
            return None
        if not readback:
            self._count("dem:mirror")
        return a

    def _mirror_put(self, key, a):
        """a written to the mirror and read back from it (shared by the processes), or None when it cannot be."""
        path = self._mirror_path(key)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "wb") as f:
                if MIRROR_ZSTD:
                    f.write(fast._ef.f32_pack(np.ascontiguousarray(a, np.float32).ravel()))
                else:
                    np.save(f, np.ascontiguousarray(a, np.float32), allow_pickle=False)
            staging.replace_cached(tmp, path)
        except OSError as e:
            log.debug("dem mirror %s: %s", path, e)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None
        self._count("dem:mirror_put")
        with Dem._mirror_lock:
            Dem._mirror_written += TILE * TILE * 4
            # (and at most every MIRROR_PRUNE_S: each prune walks the whole mirror, ~28k folders; a fresh place's
            # DEM reads in a worker triggered it again and again, seconds of a far field's geometry, 2026-10-23)
            now = time.monotonic()
            prune = (Dem._mirror_written >= MIRROR_MB * 1e6 / 8
                     and now - Dem._mirror_pruned >= MIRROR_PRUNE_S)
            if prune:
                Dem._mirror_written, Dem._mirror_pruned = 0, now
        # (only in the server's own process: every worker walked the mirror at its first write, several at once on a
        # first visit, on the server's few CPUs while the world loaded, 2026-10-30; earthd prunes it at its start too)
        import multiprocessing
        if prune and multiprocessing.current_process().name == "MainProcess":
            threading.Thread(target=self._mirror_prune, daemon=True, name="dem-mirror-prune").start()
        if MIRROR_ZSTD:                 # (its own array either way: not read back)
            return np.ascontiguousarray(a, np.float32)
        return self._mirror_get(key, readback=True)

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
                if n.endswith((".npy", ".f32z")):
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
        """This load used a stand-in (a failed fetch): its tile is not mirrored, and the builds reading it are short
        (revisit.fault: a source whose failure is no failed download, a lidar sheet that raised OSError, said nothing
        before, and the stand-in went into the LRU unmarked)."""
        self._local.partial = True
        revisit.fault(here=True)

    def _load_tile(self, key):
        """A decoded tile: from the mirror, else made (_make_tile) and, when it is all Mapterhorn's data as it will
        stay (no failover, no fetch failed on the way), written to the mirror. One process makes and writes a tile
        (its claim, _mirror_claim); another one asking meanwhile waits for it at most CLAIM_WAIT_S, then makes it
        without writing: the server's processes decoded and wrote the same tiles side by side, 3083 writes of 1 MB
        for ~1144 tiles in a warm 8 min drive (2026-11-04: the mirror's SSD writes)."""
        got = self._mirror_get(key)
        if got is not None:
            self.served.setdefault(key, MAPTERHORN["name"])
            return got
        claim = self._mirror_claim(key) if MIRROR_MB > 0 else False
        if claim is None:               # another process is making it
            end = time.monotonic() + CLAIM_WAIT_S
            while time.monotonic() < end:
                time.sleep(CLAIM_POLL_S)
                got = self._mirror_get(key)
                if got is not None:
                    self._count("dem:mirror_waited")
                    self.served.setdefault(key, MAPTERHORN["name"])
                    return got
            claim = False
        prev = getattr(self._local, "partial", False)
        self._local.partial = False
        partial = True
        try:
            a = self._make_tile(key)
            partial = self._local.partial
            if claim and not partial and a is not None and self.served.get(key) == MAPTERHORN["name"]:
                got = self._mirror_put(key, a)
                if got is not None:
                    return got
            return a
        finally:
            self._local.partial = prev or partial
            if claim:
                try:
                    os.remove(self._mirror_path(key) + ".claim")
                except OSError:
                    pass

    def _mirror_claim(self, key):
        """True: this process makes key's tile (its claim file made); None: another one is (a claim younger than
        CLAIM_STALE_S); False: no claims here (the folder cannot be written: made and not written, as before)."""
        path = self._mirror_path(key) + ".claim"
        for _ in range(2):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return True
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(path) < CLAIM_STALE_S:
                        return None
                    os.remove(path)         # (a process that ended mid-tile)
                except OSError:
                    pass
            except OSError:
                return False
        return None

    def _make_tile(self, key):
        return self._hongkong(key, self._canada(key, self._make_tile0(key)))

    def _make_tile0(self, key):
        fast.require(565, "DEM source selection")
        return fast._ef.dem_source(self, sys.modules[__name__], key)

    # --- the ground under the surface model ------------------------------
    def _lidar(self, x, y):
        """Has Mapterhorn deeper tiles (national lidar: a terrain model already) under z12 tile (x, y)? Two of
        its z13 children (diagonal): yes when either is there (404s are remembered). A child already on disk answers
        without a request; otherwise they are asked one after the other, the second only when the first is missing
        (both were downloaded at once before, the second wasted wherever the first was there: the same answer)."""
        fast.require(565, "Mapterhorn lidar probes")
        return fast._ef.dem_lidar(self, sys.modules[__name__], x, y)

    def _archive_lidar(self, kids):
        # (the children the cache knows nothing of answered by the regional archive's directories, no tile read:
        # the endpoint's answer; a 404 remembered for a child it lacks, as the endpoint's would be)
        name = MAPTERHORN["name"]
        ask = [k for k in kids if self._listed(*k) and not self._known_miss(*k)]
        try:
            there = self.archives.exists(ask) if ask else {}
        except netfetch.Deferred:
            there = None
        if there is not None and len(there) == len(ask):
            gone = [((name,) + k, os.path.join(self.cache_dir, name, str(k[0]), str(k[1]), f"{k[2]}.404"))
                    for k in ask if not there[k]]
            if gone:
                self._remember_miss(gone)
            for k in ask:
                self._note(MAPTERHORN, k[0], "index")
            return any(there.values())
        return None

    def _known_miss(self, z, x, y):
        """Is Mapterhorn tile (z, x, y) a 404 the cache remembers (as _download reads it)?"""
        name = MAPTERHORN["name"]
        if (name, z, x, y) in self._mem_miss:
            return True
        return self.fetcher.miss_alive(os.path.join(self.cache_dir, name, str(z), str(x), f"{y}.404"),
                                       MAPTERHORN["url"].format(z=z, x=x, y=y))

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
        lat = math.degrees(dmath.atan(dmath.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
        px = 40075016.686 * dmath.cos(math.radians(lat)) / n / TILE
        g = dsmground.ground(big, px)
        self._count("dem:ground12")
        return np.ascontiguousarray(g[m:m + TILE, m:m + TILE])

    def _deepest_lidar(self, z, x, y):
        """The deepest zoom between GLOBAL_ZOOM + 1 and z - 1 at which Mapterhorn has the ancestor of tile (z, x, y)
        (national lidar), else None (404s are remembered, so a miss is asked once per tile)."""
        fast.require(565, "Mapterhorn ancestor probes")
        return fast._ef.dem_deepest(self, MAPTERHORN, netfetch.FetchError, z, x, y, GLOBAL_ZOOM)

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
            marks = self._lru_short.get(key)
            if anc is not None and _stale(marks):
                anc = None
        if anc is not None:
            _replay(marks)
            if marks:           # (an ancestor kept short: the tile made from it is a stand-in too, never mirrored)
                self._partial()
        else:
            # (in this thread: a pool worker waiting on the pool could deadlock). It owns no future of the key, so it
            # publishes a complete tile only: a stand-in stays this descendant's (its faults are this reader's, and the
            # descendant is partial: _load_tile), not put in the LRU, where tiles() would hand it out ahead of the
            # owner's healthy load still pending
            anc, marks = self._load_marked(key)
            if marks is None:
                with self._lock:
                    self._lru_put(key, anc, marks)
            else:
                self._partial()     # (the descendant made from it is a stand-in too: never mirrored)
        fast.require(565, "DEM ancestor resampling")
        return fast._ef.dem_ancestor(anc, d, x, y)

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
        if self.archives is not None:
            # (the cold zoom 13+ tiles' raw bytes first, in as few archive range reads as they allow: each load then
            # finds its tile on disk)
            with self._lock:
                cold = [k for k in keys if k[0] >= mtarchive.MIN_ZOOM and k not in self._lru and k not in self._inflight]
            if cold:
                self._archive_batch(cold, "demand")
        out, futs, new, short = {}, {}, [], []
        urgent = getattr(self._local, "urgent", False)
        pool = self.urgent_pool if urgent else self.pool
        caller = self._caller()
        if urgent and len(keys) >= 16 and log.isEnabledFor(logging.DEBUG):
            zs = Counter(k[0] for k in keys)
            log.debug("dem urgent: %d tiles %s", len(keys), dict(zs))
        with self._lock:
            for k in keys:
                if k in self._lru and not _stale(self._lru_short.get(k)):
                    self._lru.move_to_end(k)
                    out[k] = self._lru[k]
                    short.append(self._lru_short.get(k))
                elif k in self._inflight and not (urgent and not self._inflight[k].running()
                                                  and not self._inflight[k].done()):
                    futs[k] = self._inflight[k]
                else:
                    futs[k] = self._inflight[k] = pool.submit(self._as, caller, self._load_marked, k)
                    new.append(k)
        for marks in short:     # (kept copies made short of something: this reader is too)
            _replay(marks)
        # Outside the lock: the callback runs at once if the future is already done.
        for k in new:
            futs[k].add_done_callback(lambda f, k=k: self._finish(k, f))
        done = len(out)
        if progress:
            progress(done, len(keys))
        for k, fut in futs.items():
            out[k] = fut.result()[0]        # (its faults are this reader's: revisit.bind)
            done += 1
            if progress:
                progress(done, len(keys))
        return out

    def _finish(self, key, fut):
        """Move a finished load from _inflight into the LRU (failures are dropped,
        so the next request retries instead of reusing a stale exception)."""
        with self._lock:
            # (the load that owns the key publishes whatever it made; one an urgent reload took the key over from
            # (superseded) publishes only a complete tile: its stand-in, finishing later, went over the newer answer,
            # while its complete tile still replaces a stand-in the urgent one put in, _lru_put never the reverse)
            owner = self._inflight.get(key) is fut
            if owner:
                del self._inflight[key]
            if not fut.cancelled() and fut.exception() is None:
                a, marks = fut.result()
                if owner or marks is None:
                    self._lru_put(key, a, marks)

    def _lru_put(self, key, a, marks):
        """With self._lock held: tile key's heights a into the LRU, with marks (_load_marked's) kept beside it. A
        stand-in (marks) never replaces a complete entry already there (a retry that failed again while another
        thread's load of the same tile succeeded, _from_ancestor's inline load): the complete one stays."""
        if marks is not None and key in self._lru and key not in self._lru_short:
            self._lru.move_to_end(key)
            return
        self._lru[key] = a
        self._lru.move_to_end(key)
        if marks is None:
            self._lru_short.pop(key, None)
        else:
            self._lru_short[key] = marks
        while len(self._lru) > self.mem_tiles:
            self._lru_short.pop(self._lru.popitem(last=False)[0], None)

    def _load_marked(self, key):
        """(_load_tile(key), its marks): (faults, lidar rule faults) when making it read something short (a stand-in
        for a failed download), else None. Its faults reach this thread's builds as they happen (its own scope is
        inside theirs)."""
        with revisit.scope() as made:
            a = self._load_tile(key)
        return a, ((made.n, made.lidar, time.monotonic()) if made.n or made.lidar else None)

    def block_max(self, z, x, y, block):
        """Highest elevation in every block x block pixel square of tile (z, x, y): a (TILE / block)²
        array, row 0 north (the horizon mode's coarse pass, farfield.horizon_grid)."""
        a = self.tiles([(z, x, y)])[(z, x, y)]
        n = TILE // block
        return a.reshape(n, block, n, block).max(axis=(1, 3))

    # --- sampling --------------------------------------------------------
    @profile_step('Elevation sampling')
    def sample(self, lat, lon, zoom, progress=None):
        """Bilinear elevation (m) at arbitrary lat/lon arrays from zoom-level tiles. Points from polardem.FROM_LAT
        (84.9 deg) on (Mapterhorn's heights stop short of 85, Web Mercator at 85.05) come from the Copernicus DEM
        (polardem.py; 0 m on the Arctic Ocean). earthfast.dem_sample does the work (pixel coordinates, the box of
        tiles or its groups, the tiles the bilinear reads: only those are asked, the bilinear on them in place); this
        keeps the tile store and the polar DEM."""
        if zoom > max(MAX_ZOOM, FINE_ZOOM):
            raise ValueError(f"zoom {zoom} > {max(MAX_ZOOM, FINE_ZOOM)}")
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        if lat.shape == lon.shape:
            shape = lat.shape
            lat, lon = lat.ravel(), lon.ravel()
        else:
            shape = np.broadcast(lat, lon).shape
            lat, lon = np.broadcast_to(lat, shape).ravel(), np.broadcast_to(lon, shape).ravel()
        fast.require(484, "dem_sample")
        return fast._ef.dem_sample(lat, lon, zoom, TILE, polardem.FROM_LAT, MAX_TILES_PER_SAMPLE, SPARSE_MIN_TILES,
                                   SPARSE_RATIO, SPARSE_GROUP, lambda keys: self._sample_tiles(keys, progress),
                                   lambda la, lo: self._polar_heights(la, lo, zoom)).reshape(shape)

    def _sample_tiles(self, keys, progress):
        """(dem_sample's fetch) the float32 heights of tiles keys (a list of (z, x, y)), in their order."""
        got = self.tiles(keys, progress)
        return [np.asarray(got[k], np.float32) for k in keys]

    def _polar_heights(self, lat, lon, zoom):
        """(dem_sample's polar) heights at points past polardem.FROM_LAT, None when the polar DEM can't be had."""
        px = 40075016.686 * dmath.cos(math.radians(MAX_LAT)) / (TILE * 2 ** zoom)
        try:
            out = np.zeros(lat.shape, np.float64)
            out[np.ones(lat.shape, bool)] = self.polar().sample(lat, lon, px)     # (numpy's assignment rules, as before)
            return out
        except (netfetch.FetchError, OSError, ValueError) as e:
            log.warning("polar DEM unavailable (%s): sea level there", e)
            return None
