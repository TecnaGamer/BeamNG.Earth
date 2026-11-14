"""The graded terrain blocks kept on disk (2026-11-12): a cached place's block comes back in a fraction of a second
instead of being built again (heights: DEM, sea floor, road grading, arch raise; then land cover and paint: a warm
teleport's median graded block took 6.7 s on the PC), and a moving block builds only its new strip of chunks, whichever
process built the others and in whichever session.

What is kept (earthfast blockstore.rs; files in <cache>/blocks):
- the float64 surface heights per 128 m lattice chunk (heightmap.ChunkCache's unit, CHUNK samples): pointwise
  (heightmap.surface_u), so a chunk serves every block of the anchor over it. Exact: the land cover's slope and the
  tunnel holes read the heights themselves, and a rounded height flips samples there.
- per block centre, the material index (after the layer fold, before the tunnel holes), the tunnel holes and the rest
  of the block's info. The paint is not pointwise (its affine grid is the block's, the slope's edge is the block's,
  the cells read are those under the block), so it is kept per block; blocks sit on the 128 m grid of a snapped
  anchor, so a place visited again or a road driven again asks for the same ones.

Keys are content addresses of everything the output depends on (a wrong hit is worse than a miss): FORMAT; the code
(revisit.code_key: the cached items' module closure, the native module and the BNE_* switches; and earthd.py, whose
own copy of the build runs a teleport's first block); the plans' and facts' formats; the map data's versions
(MapData.versions); the anchor (frame) and the DEM zoom; the lattice (heightmap.SQUARE, CHUNK); the settings that change
output (grading, road widths, surface, sea, airports and their OurAirports snapshot, tunnel lids, roads in the ground,
the road lift); every BNGE_* environment setting but those that change no output (_ENV_IGNORED); for a chunk the bus
station yards near it (they level the ground) and for a block its centre, size and height quantum, its chunks' keys and
the fuel, frontage and bus pads near it (surface.Surface.pad_digest); for both the photo's road surface verdicts this
run uses round them (roadsurf.area_tags: they change the plans and the paint).

Only complete builds are kept: no revisit.fault() during the build (a download that failed, a map cell or land cover
not there yet: the hooks every revisit item uses), every cell whose plans reach the block known complete
(grading.plans_complete), the surface complete, no chunk from the memory computed in a short build (ChunkCache.dirty),
and the keys' inputs (versions, pads) the same at the end as at the start. A short build's chunks stay in memory only,
marked dirty. Written in the build's thread once its own files are (~10-25 ms for
a whole block, a few ms for a moved block's new strip). The folder is cut back to MAX_MB (BNGE_BLOCKCACHE_MB, set by earthd from the cache
cap: budget_mb) at the server's start and by each process after it wrote a tenth of that (on a thread), least
recently used first.

An older generation's block (revisit.stale: the server shows a place's last version while the current one is built)
is found through the place's "latest" note: beside the files, <hex>.latest.json per block place (its key: every part
but the code, the plans' format and the pads, which arrive during a session: a block's first build of a run has none
of them, and its key is not the one the last run kept it under), naming the last block kept there, its generation
(code id, plans format) and its chunks' keys (Build.finish writes it, Build.stale_hit reads it). Its heights come
straight from the disk, never into the anchor's chunk memory (the current build computes its own). Notes untouched
for LATEST_DAYS go at the server's start (prune).

BNGE_BLOCKCACHE=off: nothing read or kept (A/B).
"""
import hashlib
import json
import logging
import os
import threading

import numpy as np

import fast
import heightmap
import revisit

log = logging.getLogger("earthd.blockcache")

FORMAT = 1                  # bump when what a file holds changes without a code change the code key sees
SUBDIR = "blocks"
NATIVE = 269                # earthfast's blockstore kernels
HEIGHTS_LEVEL = 1           # zstd: 4.90 bytes a sample at 1, 4.85 at 3 (the low mantissa is noise), half the time
BLOCK_LEVEL = 3
PAD_MARGIN_M = 250.0        # m round a chunk / block whose pads are in its key (a yard's levelling blends out within it)
LOW_WATER = 0.9
KEEP_S = 600.0              # files used this recently are never pruned
LATEST_EXT = ".latest.json"  # a block place's note of the last block kept there (Build.stale_hit)
LATEST_DAYS = 90.0          # ... removed when not written or read for this long (prune)
SHARE_OF_CAP = 0.1          # budget_mb: this share of the cache cap ...
MIN_MB, MAX_MB_CAP, DEFAULT_MB = 500.0, 4000.0, 2000.0     # ... within these (DEFAULT_MB: no cap given)


def _on():
    return os.environ.get("BNGE_BLOCKCACHE", "on").strip().lower() not in ("off", "0", "no", "false")


def max_mb():
    try:
        return float(os.environ.get("BNGE_BLOCKCACHE_MB", "") or DEFAULT_MB)
    except ValueError:
        return DEFAULT_MB


def budget_mb(cap_bytes):
    """The folder's limit (MB) for a cache cap of cap_bytes (None: no cap): SHARE_OF_CAP of it within MIN_MB-MAX_MB_CAP."""
    if not cap_bytes:
        return DEFAULT_MB
    return float(min(MAX_MB_CAP, max(MIN_MB, SHARE_OF_CAP * cap_bytes / 1e6)))


def configure(cap_bytes):
    """earthd, before its worker processes start: the folder's limit for them all (BNGE_BLOCKCACHE_MB)."""
    os.environ["BNGE_BLOCKCACHE_MB"] = f"{budget_mb(cap_bytes):.0f}"


def folder(cache_dir):
    return os.path.join(cache_dir, SUBDIR)


def prune(cache_dir, max_bytes=None, keep_s=KEEP_S):
    """The folder cut back (least recently used first). -> (files removed, bytes left)."""
    fast.require(NATIVE, "terrain block cache")
    mb = max_mb() if max_bytes is None else max_bytes / 1e6
    prune_latest(cache_dir)
    return fast._ef.blockstore_prune(folder(cache_dir), int(mb * 1e6), LOW_WATER, float(keep_s))


def prune_latest(cache_dir, days=LATEST_DAYS, now=None):
    """The block places' "latest" notes not written or read for days removed. -> how many."""
    import time
    now = time.time() if now is None else now
    n = 0
    try:
        subs = os.listdir(folder(cache_dir))
    except OSError:
        return 0
    for sub in subs:
        d = os.path.join(folder(cache_dir), sub)
        try:
            names = [x for x in os.listdir(d) if x.endswith(LATEST_EXT)]
        except OSError:
            continue
        for x in names:
            try:
                if now - os.path.getmtime(os.path.join(d, x)) > days * 86400.0:
                    os.remove(os.path.join(d, x))
                    n += 1
            except OSError:
                pass
    return n


# BNGE_* settings that change no block (this cache's own, diagnostics, scheduling, networking, the per-run ids): every
# other one is in the keys
_ENV_IGNORED = frozenset({
    "BNGE_BLOCKCACHE", "BNGE_BLOCKCACHE_MB", "BNGE_PLAN_RUN", "BNGE_ROADZ_SESSION", "BNGE_TRACEMALLOC", "BNGE_SAMPLE",
    "BNGE_STACK_DUMP_S", "BNGE_NETLOG", "BNGE_NETSTATE", "BNGE_DEM_STATS", "BNGE_FACTS_LOG", "BNGE_FARTRACE",
    "BNGE_SERVER_CORES", "BNGE_BUDGET", "BNGE_SHED_MB", "BNGE_DUCKDB_THREADS", "BNGE_DEM_MEM_TILES",
    "BNGE_DEM_MIRROR_MB", "BNGE_DEM_MIRROR_ZSTD", "BNGE_PEERS", "BNGE_SHARE_PORT", "BNGE_SHARE_WAIT_S",
    "BNGE_SHARE_PREFETCH_M", "BNGE_SHARE_BPS", "BNGE_NET_MBPS", "BNGE_NET_LATENCY_MS", "BNGE_CDN_BASE",
    "BNGE_CDN_SWITCH", "BNGE_ROAD_TILE_TIMEOUT", "BNGE_SWITCH_MS", "BNGE_CATCHUP_LEND", "BNGE_VIEW_PENALTY",
    "BNGE_WARM_READ", "BNGE_RANGE_PAR_BLOCKS", "BNGE_HELPER_RADIUS_M", "BNGE_FIN_WORKER", "BNGE_PROVISIONAL_FOLLOW_S",
    "BNGE_TELEPORT_FULL", "BNGE_PROV_HERE", "BNGE_FAST_BASE_V", "BNGE_PHOTO_PNG", "BNGE_NO_BASE_PACK",
    "BNGE_CDAE_PLAIN", "BNGE_BAKE_FETCH", "BNGE_PRECACHE_LIDAR_WAIT", "BNGE_MCP_URL",
    # (a run's shared memory names and paths, set by earthd for its workers; memory budgets and process scheduling)
    "BNGE_MEMTABLE", "BNGE_CACHE_DIR", "BNGE_SERVER_LOG", "BNGE_REPORT_URL", "BNGE_MEMWATCH", "BNGE_MEMWATCH_S",
    "BNGE_MEMATTR_S", "BNGE_MAX_RAM_GB", "BNGE_TOTAL_GB", "BNGE_LOW_FREE_GB", "BNGE_TREE", "BNGE_TREE_MB",
    "BNGE_ROTATE_MB", "BNGE_CGROUP_MEM", "BNGE_EMERGENCY_TRIM", "BNGE_IDLE_SHED_S", "BNGE_LEAN", "BNGE_LEAN_POOLS",
    "BNGE_LEAN_PROCS", "BNGE_POOL_EXTRA_IDLE_S", "BNGE_TASKS_PER_CHILD", "BNGE_WIN_TRIM", "BNGE_HUNG_DUMP_S",
    "BNGE_NETLOG_STACK", "BNGE_IMG_HOST_POOLS", "BNGE_CACHE_WALK_DELAY_S", "BNGE_SMALL_COMPACT", "BNGE_AHEAD_BATCH",
    "BNGE_PREFETCH_SPARE", "BNGE_FULL_PIN_GAME", "BNGE_PHOTO_PROCESS", "BNGE_IGN_RATE", "BNGE_CATCHUP_LEND_NORMAL",
    "BNGE_LIDAR_THREADS", "BNGE_LIDAR_CLAIMS",
    "BNGE_ROADSURF_T0",     # (the run's start for the road surface verdicts: their digests are in the keys)
    "BNGE_SIM_BUMP",        # (revisit.SIM_BUMP: in the code id, and an older generation's blocks must still be found)
    "BNGE_BLOCK_NOTE_LOG",
    "BNGE_STALE_ANY_SWITCHES",
})

_code = []
_lock = threading.Lock()


def _code_id():
    """revisit.code_key with earthd.py's own bytes (its copy of the build: a teleport's first block), once a process.
    A packaged app without the source: its build's fingerprints (CODE_ID, REVISIT_ID) and VERSION instead."""
    with _lock:
        if not _code:
            here = os.path.dirname(os.path.abspath(__file__))
            h = hashlib.sha256(revisit.code_key().encode())
            found = False
            for p in (os.path.join(here, "earthd.py"),):
                try:
                    with open(p, "rb") as f:
                        h.update(b"earthd\0" + f.read())
                    found = True
                except OSError:
                    pass
            if not found:
                for p in (os.path.join(here, "CODE_ID"), os.path.join(here, "REVISIT_ID"),
                          os.path.join(os.path.dirname(here), "VERSION"), os.path.join(here, "VERSION")):
                    try:
                        with open(p, "rb") as f:
                            h.update(os.path.basename(p).encode() + b"\0" + f.read())
                    except OSError:
                        pass
            _code.append(h.hexdigest())
        return _code[0]


def warm():
    """The code id made on a thread now (a block process's start): the first block's key does not wait for it."""
    threading.Thread(target=_code_id, daemon=True, name="block-cache-code").start()


def env_key():
    return tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith("BNGE_") and k not in _ENV_IGNORED))


def settings(grading, md, widths, surface, sea, airports, lids):
    """The settings that change a block, as the key holds them (the caller's state: blockworker._State or earthd.Server)."""
    import grading as gradingmod
    import roads as roadsmod
    db = getattr(airports, "db", None)
    return (bool(grading), md is not None, widths is not None, surface is not None, sea is not None,
            None if airports is None else str(getattr(db, "snapshot", "?")), lids is not None,
            repr(getattr(roadsmod, "ROADS_IN_GROUND", None)), float(gradingmod.ROAD_LIFT),
            None if surface is None else (bool(getattr(surface, "gas_pads", False)),
                                          bool(getattr(surface, "bus_pads", False)),
                                          getattr(surface, "extra_pads", None) is not None))


def ground_rules(grading, md, widths, surface, sea, airports, lids):
    """The GROUND RULES fingerprint: every server setting that changes the ground or the road geometry built on it
    (--grading, the map data, --road-widths, --surface, --sea, --airports and its snapshot, the tunnel lids, roads in
    the ground, the road lift): settings() without the surface's pad switches, which come on with a session's first
    pads. ONE fingerprint for everything that stands on that ground: the terrain blocks' keys (settings), the road
    geotiles' content and skins, the trees, the sign tiles, the block photos (blockworker._ground_rules, earthd
    Server.ground_rules). None of these is a BNE_* switch, so the revisit code key does not hold them: a skin kept
    with --grading on was a hit with it off, on terrain that had changed (review of 2026-11-13)."""
    return settings(grading, md, widths, surface, sea, airports, lids)[:9]


def _versions(md):
    if md is None or not hasattr(md, "versions"):
        return None
    try:
        return json.dumps(md.versions(), sort_keys=True, default=repr)
    except Exception as e:      # (no versions: no key, nothing read or kept)
        log.debug("block cache: map data versions: %s: %s", type(e).__name__, e)
        raise


def _h(*parts):
    return hashlib.sha256(repr(parts).encode()).digest()


class _Writer:
    """This process's writes (in the build's thread, after its files: ~10-25 ms for a whole block's 64 chunks on 8
    threads, ~3 ms for a moved block's 8; a thread of their own raced the test folders' removal and held the heights
    in memory) and its prunes (on a thread of their own, one at a time)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.written = 0            # bytes since the last prune
        self.pruning = False
        self.stats = {"chunksWritten": 0, "blocksWritten": 0, "bytesWritten": 0}

    def write(self, cache_dir, chunk_keys, u, block):
        d = folder(cache_dir)
        if not os.path.isdir(cache_dir):       # (the cache folder removed under us: nothing made again)
            return False
        n = files = 0
        if chunk_keys:
            files, n = fast._ef.blockstore_put_heights(d, chunk_keys, u, HEIGHTS_LEVEL)
        b = 0
        if block is not None:
            key, size, idx, holes, meta = block
            b = fast._ef.blockstore_put_block(d, key, size, idx, holes, meta, BLOCK_LEVEL)
        lim = max_mb() * 1e6
        with self.lock:
            self.stats["chunksWritten"] += files
            self.stats["blocksWritten"] += int(b > 0)
            self.stats["bytesWritten"] += n + b
            self.written += n + b
            prune_now = self.written > lim / 10 and not self.pruning
            if prune_now:
                self.written, self.pruning = 0, True
        if prune_now:
            threading.Thread(target=self._prune, args=(d, lim), daemon=True, name="block-cache-prune").start()
        return True

    def _prune(self, d, lim):
        try:
            removed, left = fast._ef.blockstore_prune(d, int(lim), LOW_WATER, KEEP_S)
            if removed:
                log.info("block cache: %d files removed, %.2f GB left", removed, left / 1e9)
        except Exception as e:
            log.warning("block cache: not cut back: %s: %s", type(e).__name__, e)
        finally:
            self.pruning = False


_writer = _Writer()


class ChunkDisk:
    """heightmap.grid_u_chunked's view of the disk for one block: load(chunk ids) -> {id: (CHUNK, CHUNK) heights} of
    those on disk; computed: [(id, heights)] the build made (kept by Build.finish when the block is complete)."""

    def __init__(self, build):
        self.build = build
        self.computed = []
        self.read = 0

    def load(self, ks):
        keys = [self.build.chunk_keys[k] for k in ks]
        if not keys:
            return {}
        u, found = fast._ef.blockstore_get_heights(self.build.dir, keys, heightmap.CHUNK)
        self.read += sum(found)
        return {k: u[i] for i, k in enumerate(ks) if found[i]}


class _AltDisk:
    """ChunkDisk's load for an older generation's chunk keys (Build.stale_hit)."""

    def __init__(self, d, chunk_keys):
        self.dir, self.chunk_keys = d, chunk_keys

    def load(self, ks):
        keys = [self.chunk_keys[k] for k in ks]
        if not keys:
            return {}
        u, found = fast._ef.blockstore_get_heights(self.dir, keys, heightmap.CHUNK)
        return {k: u[i] for i, k in enumerate(ks) if found[i]}


class Build:
    """One graded block's cache handle: begin() makes it before the block is built."""

    def __init__(self, cache_dir, frame, hm_zoom, cx, cy, size, q, setting, md, surface, chunks):
        self.cache_dir, self.dir = cache_dir, folder(cache_dir)
        self.frame, self.hm_zoom, self.cx, self.cy, self.size, self.q = frame, hm_zoom, cx, cy, size, q
        self.setting, self.md, self.surface, self.chunks = setting, md, surface, chunks
        self.cells = heightmap.block_chunks(cx, cy, size)
        self.scope = revisit.begin()        # (this block's own faults: revisit.scope; ended by finish)
        self.base, self.pads = self._keys()
        self.disk = ChunkDisk(self)
        self.hit_info = None

    def _keys(self):
        """(base key, (chunk pad digests, block pad digest)); sets chunk_keys and block_key."""
        f = self.frame
        parts = {"format": FORMAT, "code": _code_id(), "plans": _plans_format(), "versions": _versions(self.md),
                 "settings": self.setting, "env": env_key(),
                 "frame": (repr(float(f.lat0)), repr(float(f.lon0)), repr(float(getattr(f, "h0", 0.0)))),
                 "dem": int(self.hm_zoom), "lattice": (float(heightmap.SQUARE), int(heightmap.CHUNK))}
        if hasattr(self, "parts"):      # (the end of a build: what changed, for the log)
            self.why = [n if n != "env" else f"env {sorted(set(self.parts[n]) ^ set(v))}"
                        for n, v in parts.items() if self.parts.get(n) != v]
        self.parts = parts
        base = _h("bnge-block", *sorted(parts.items()))
        s = heightmap.SQUARE
        ks = [k for _, _, k in self.cells]
        x0 = np.array([kx * heightmap.CHUNK * s for kx, _ in ks], np.float64)
        y0 = np.array([ky * heightmap.CHUNK * s for _, ky in ks], np.float64)
        w = heightmap.CHUNK * s
        boxes = self._boxes(np.concatenate([x0, [self.cx - self.size * s / 2]]),
                            np.concatenate([y0, [self.cy - self.size * s / 2]]),
                            np.concatenate([np.full(len(ks), w), [self.size * s]]))
        surf = self.surface
        import roadsurf     # (the photo's road surface verdicts this run uses round each: the plans' and paint's)
        tags = [roadsurf.area_tags(b[0], b[2], b[1], b[3]) for b in boxes]
        cpads = [(surf.pad_digest(*b, bus_only=True) if surf is not None else b"", tags[i])
                 for i, b in enumerate(boxes[:-1])]
        bpad = (surf.pad_digest(*boxes[-1]) if surf is not None else b"", tags[-1])
        self.chunk_keys = {k: _h(base, "heights", int(k[0]), int(k[1]), cpads[i]) for i, k in enumerate(ks)}
        self.block_key = _h(base, "block", repr(float(self.cx)), repr(float(self.cy)), int(self.size), repr(float(self.q)),
                            bpad, tuple(self.chunk_keys[k] for k in ks))
        return base, (tuple(cpads), bpad)

    def _boxes(self, x0, y0, w):
        """(lat0, lat1, lon0, lon1) of each square (x0, y0, side w) of the anchor frame, PAD_MARGIN_M wider (more than the
        grading's reach, grading.T_MAX + 40 m)."""
        m = PAD_MARGIN_M
        xs = np.stack([x0 - m, x0 + w + m, x0 - m, x0 + w + m], 1)
        ys = np.stack([y0 - m, y0 - m, y0 + w + m, y0 + w + m], 1)
        la, lo, _ = self.frame.enu_to_geodetic(xs.ravel(), ys.ravel(), np.zeros(xs.size))
        la, lo = np.asarray(la, np.float64).reshape(xs.shape), np.asarray(lo, np.float64).reshape(xs.shape)
        return [(float(a.min()), float(a.max()), float(b.min()), float(b.max())) for a, b in zip(la, lo)]

    # ---- a hit ----

    def hit(self, out_dir, name, ter_name, ground_at, with_surface):
        """The block written from the cache (heightmap PNG name, masks, .ter ter_name in out_dir) when its entry and
        every chunk are there: its info (as the build's, without stages), or None (build it). In a revisit.stale
        scope a miss falls back to an older generation's block (stale_hit: its info has "stale", the generation's
        id); with only the older generation alone is read and a miss raises revisit.StaleMiss."""
        got = None if revisit.stale_only() else self._read(self.block_key, self.chunks, self.disk, with_surface)
        gid = None
        if got is None:
            for gen in revisit.stale_gens():
                got = self.stale_hit(gen, with_surface)
                if got is not None:
                    gid = gen["id"]
                    break
            if got is None:
                if revisit.stale_only():
                    revisit.end(self.scope)
                    raise revisit.StaleMiss("block")
                return None
        info = self._write(got, out_dir, name, ter_name, ground_at)
        if gid is not None:
            info["stale"] = gid
            revisit.note_stale_hit(gid)
        self.hit_info = info
        return info

    def _latest_path(self):
        """The file of this block place's "latest" note: keyed by every part of the block's key but the generation's
        (the code, the plans' format) and the pads."""
        # (nor the map data's versions: a run learns its shards' releases as it reads them, the first block's key is
        # made before; a block of other map data is still the place's last version, and the road tiles, keyed by the
        # versions, then miss and end the stale mode)
        parts = {n: v for n, v in self.parts.items() if n not in ("code", "plans", "versions")}
        # (the surface's pad switches come on with a session's first pads, as the pads do: not the place's either)
        parts["settings"] = tuple(self.setting)[:9] if isinstance(self.setting, tuple) else self.setting
        h = _h("bnge-block-latest", *sorted(parts.items()), repr(float(self.cx)), repr(float(self.cy)), int(self.size),
               repr(float(self.q))).hex()
        if os.environ.get("BNGE_BLOCK_NOTE_LOG"):       # (debugging: what a place's note is keyed by)
            log.info("block note %s: %r", h[:12], (sorted(parts.items()), self.cx, self.cy, self.size, self.q))
        return os.path.join(self.dir, h[:2], h + LATEST_EXT)

    def note_latest(self):
        """This block (just kept) is the last one kept at its place: its generation and keys in the place's note."""
        ks = [k for _, _, k in self.cells]
        rec = {"code": self.parts["code"], "plans": self.parts["plans"], "block": self.block_key.hex(),
               "chunks": [self.chunk_keys[k].hex() for k in ks]}
        path = self._latest_path()
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def latest(self):
        """The place's note ({"code", "plans", "block", "chunks"}), or None."""
        try:
            with open(self._latest_path(), encoding="utf-8") as f:
                rec = json.load(f)
            ks = [k for _, _, k in self.cells]
            if not (isinstance(rec["code"], str) and isinstance(rec["plans"], str) and len(rec["chunks"]) == len(ks)):
                return None
            bytes.fromhex(rec["block"])
            return rec
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def latest_is_current(self):
        """Is the last block kept at this place this generation's (whatever its pads)?"""
        rec = self.latest()
        return rec is not None and rec["code"] == self.parts["code"] and rec["plans"] == self.parts["plans"]

    def stale_hit(self, gen, with_surface=True):
        """(idx, holes, meta, heights) of the last block kept at this place when it is generation gen's, or None: none
        kept, another generation's, its entry or one of its chunks gone, or this code cannot read that generation's
        blocks. Nothing goes into the anchor's chunk memory."""
        if not revisit.readable(gen, "block"):
            return None
        rec = self.latest()
        if rec is None or rec["code"] != gen["block"] or rec["plans"] != gen["plans"]:
            return None
        try:
            ks = [k for _, _, k in self.cells]
            chunk_keys = {k: bytes.fromhex(h) for k, h in zip(ks, rec["chunks"])}
            got = self._read(bytes.fromhex(rec["block"]), None, _AltDisk(self.dir, chunk_keys), with_surface)
        except (ValueError, TypeError):
            return None
        if got is not None:
            try:
                os.utime(self._latest_path(), None)     # (used: prune_latest)
            except OSError:
                pass
        return got

    def _read(self, block_key, chunks, disk, with_surface):
        got = fast._ef.blockstore_get_block(self.dir, block_key, int(self.size))
        if got is None:
            return None
        idx, holes, meta = got
        meta = json.loads(bytes(meta).decode("utf-8"))
        if bool(meta.get("surface")) != bool(with_surface):
            return None
        u = heightmap.chunk_grid(self.cx, self.cy, self.size, chunks, disk)
        if u is None:
            return None
        return idx, holes, meta, u

    def _write(self, got, out_dir, name, ter_name, ground_at):
        idx, holes, meta, u = got
        import surface as surfacemod
        q, z_base, max_height, qgrid = heightmap.write_png(os.path.join(out_dir, name), u, self.q)
        gz = heightmap.height_at(qgrid, z_base, max_height, self.cx, self.cy, *ground_at) if ground_at else None
        info = {"cx": self.cx, "cy": self.cy, "zBase": z_base, "maxHeight": max_height, "q": q,
                "groundZ": None if gz is None else round(gz, 3), "minU": float(u.min()), "maxU": float(u.max())}
        if idx is not None and meta.get("surface"):
            info["layers"] = surfacemod.write_masks(idx[::-1], out_dir, name[:-4])
            info["surfaceComplete"] = meta["surfaceComplete"]
            info["roadsComplete"] = meta["roadsComplete"]
        ter = idx
        if holes is not None:
            ter = np.zeros(holes.shape, np.uint8) if idx is None else idx.copy()
            ter[holes] = heightmap.TER_HOLE
            info["tunnelHoles"] = int(holes.sum())
        info["ter"], mats = surfacemod.write_ter(out_dir, ter_name, qgrid, ter)
        if mats is not None:
            info["mats"] = mats
        return info

    # ---- a build ----

    def finish(self, info, keep, idx_pre, idx_ter, g):
        """After the block was built and written: its chunks and entry written to the disk when the build was complete,
        else its new chunks marked dirty in the memory. idx_pre: the material index before the tunnel holes (None: no
        surface); idx_ter: the .ter's (holes in; None: none). Returns whether it was kept."""
        revisit.end(self.scope)
        computed = self.disk.computed
        ks = [k for _, _, k in self.cells]
        why = self._short(info, keep, g, ks)
        if why is not None:
            if computed and self.chunks is not None:
                self.chunks.mark_dirty([k for k, _ in computed])
            log.debug("block cache: block (%s, %s) not kept: %s", self.cx, self.cy, why)
            return False
        holes = None
        if idx_ter is not None:
            h = idx_ter == heightmap.TER_HOLE
            if idx_pre is not None:
                h &= idx_pre != heightmap.TER_HOLE
            holes = h if h.any() else None
        meta = {"surface": idx_pre is not None, "surfaceComplete": info.get("surfaceComplete"),
                "roadsComplete": info.get("roadsComplete")}
        block = (self.block_key, int(self.size),
                 None if idx_pre is None else np.ascontiguousarray(idx_pre, np.uint8),
                 None if holes is None else np.ascontiguousarray(holes), json.dumps(meta, sort_keys=True).encode())
        keys = [self.chunk_keys[k] for k, _ in computed]
        u = (np.ascontiguousarray(np.stack([c for _, c in computed]), np.float64) if computed
             else np.zeros((0, heightmap.CHUNK, heightmap.CHUNK)))
        kept = _writer.write(self.cache_dir, keys, u, block)
        if kept:
            self.note_latest()
        return kept

    def _short(self, info, keep, g, ks):
        """Why the build may not be kept (None: it may)."""
        if not self.scope.complete:
            return "something it read failed or was missing" + (f" ({self.scope.why_not()})" if revisit.SITES else "")
        if info.get("surfaceComplete") is False or info.get("roadsComplete") is False:
            return "surface incomplete"
        if self.chunks is not None and self.chunks.any_dirty(ks):
            return "chunks of a short build in it"
        if g is not None:
            import grading as gradingmod
            lat, lon = keep.get("lat"), keep.get("lon")
            if lat is None:
                return "no positions"
            la = np.array([np.min(lat), np.max(lat)])
            lo = np.array([np.min(lon), np.max(lon)])
            if not gradingmod.plans_complete(g._keys(la, lo)):
                return "plans short or not known complete"
        try:
            if self._keys() != (self.base, self.pads):
                return f"its inputs changed during the build ({', '.join(self.why) or 'the pads'})"
        except Exception as e:
            return f"keys: {type(e).__name__}"
        return None


def _plans_format():
    import plancache
    return plancache.plans_format()


def begin(cache_dir, frame, hm_zoom, cx, cy, size, q, setting, md, surface, chunks):
    """A Build for a graded block (cx, cy) of the anchor frame, or None (the cache off, the block off the chunk grid, no
    native kernels, its keys not to be had)."""
    if not _on() or cache_dir is None or chunks is None or heightmap.block_chunks(cx, cy, size) is None:
        return None
    try:
        fast.require(NATIVE, "terrain block cache")
        return Build(cache_dir, frame, hm_zoom, cx, cy, size, q, setting, md, surface, chunks)
    except Exception as e:
        log.debug("block cache: block (%s, %s) uncached: %s: %s", cx, cy, type(e).__name__, e)
        return None


def stats():
    with _writer.lock:
        return dict(_writer.stats)
