"""The download cache's size cap: last use per file, LRU eviction, "clear cache" (docs/ROADMAP.md
"Caching" 1, the cheap parts of 3 and 5).

Layout of the cache root (~/.cache/beamng-earth by default):
  VERSION             the layout version (LAYOUT_VERSION). Missing: this layout, written now. Any other
                      value (a newer server's layout): the cache is left alone - counted, never evicted,
                      cleared or indexed.
  index.tsv           the last use of every tracked file ("<unix s>\t<bytes>\t<path>"), saved by the
                      background thread every SAVE_S when something changed, and on close()
  dem/ imagery/ colormatch/ deshadow/     MANAGED: tracked, evicted, cleared
  mapdata/            MANAGED too (mapdata.py: layer files and the sources' range blocks; a missing
                      one is a miss, built or fetched again). Not used by earthd yet, so nothing
                      there is protected as this session's: the walk's mtime is its last use
  gravity/            counted, never evicted or cleared (the 145 MB EGM2008 model, downloaded once)
  airports/           the same (airports.py: OurAirports' CSV files, ~17 MB, the last good copy kept for
                      offline use; airports.py replaces it weekly and deletes the old generation itself)
  seacarve/ (its raw rasters) peercache/ roadwidth/   their own limits (seacarve.RAW_MB, peerwork.CACHE_BYTES,
                      roadwidth.STORE_MB), cut back at server start (prune_dir; their readers touch_once what they use)
  mapdata-mirror/ dem's mirror            their own limits, pruned by their writers (a process's first write too)
  netstats/ (and anything else)           not tracked: the daily request budgets must survive a clear

Last use = the server's own record of every disk hit and write (dem.py, imagery.py, the de-shadow and
colour-seam caches call used() / wrote() / read()), not the file system's atime (relatime) and no stat
per request: one dict update under a lock. At start a background thread walks the tree once
(st_size, and st_mtime = the download time for files the index does not know, e.g. a cache from before
this module) and merges it with the index; until then ready is False and nothing is evicted.

Eviction (a background thread): when the tracked bytes exceed the cap, least recently used files go
until LOW_WATER (90 %) of the cap is left, at most every EVICT_S. Never evicted: gravity/, files this
server process used within PROTECT_S (an hour: the area around the player), and empty files (404 and
"no seams" markers: they save requests; clear() removes them). Directories stay (a writer may be about
to put a file there). clear() removes every MANAGED file but the ones this server process used at all
(the current session's, however long ago).

Per-source lifetimes (ROADMAP "Caching" 3): LIFETIMES maps a path prefix to the longest a file may be
kept after its download (s); the start-up walk deletes older ones. Empty today: none of the current
sources (USGS public domain, EOX CC BY / CC BY-NC-SA, Mapterhorn, AWS Terrain Tiles) limits caching
time; the 404 markers' own expiry is netfetch's miss_ttl. Per-producer versions live in the paths
(colormatch/v1/, imagery/usgs-ds1-<mode>/): a generator update writes a new directory and the old one
is never read again, so it ages out through the LRU instead of being deleted at once (ROADMAP 5).

Memory: about 200 bytes per tracked file (a 20 GB cache of 30 KB imagery tiles: ~130 MB).
"""
import logging
import math
import os
import threading
import time

log = logging.getLogger("earthd.cache")

LAYOUT_VERSION = 1
VERSION_FILE = "VERSION"
INDEX_FILE = "index.tsv"
INDEX_HEADER = "# beamng-earth cache index 1"
MANAGED = ("dem", "imagery", "colormatch", "deshadow", "mapdata")     # evicted and cleared
KEPT = ("gravity", "airports")                              # counted only
DEFAULT_CAP_GB = 20.0
MAX_CAP_GB = 1e6            # 1 PB: anything above is a mistake (and would overflow)
GB = 1e9                    # the page's GB (decimal, as disk sizes are sold)
LOW_WATER = 0.9             # evict down to this share of the cap
PROTECT_S = 3600.0          # used by this server within an hour: never evicted (clear: any use this session)
EVICT_S = 180.0             # at most one eviction pass per 3 minutes
SAVE_S = 600.0              # index saved at most every 10 minutes (and on close)
TICK_S = 30.0               # the background thread's check interval
LIFETIMES = {}              # path prefix ("imagery/xyz/") -> max age after download (s); none today
MSG_VERSION = 1
READ_WAITS = (0.02, 0.05, 0.1, 0.25)   # s between the tries of a read held up on Windows (NoCache.read)


def cap_bytes(gb):
    """A cap in GB (float, 0 = unlimited) as bytes; None / negative / not finite -> ValueError."""
    gb = float(gb)
    if not 0 <= gb <= MAX_CAP_GB:
        raise ValueError(f"cache cap {gb!r} GB: a number from 0 to {MAX_CAP_GB:g} (0 = unlimited)")
    return int(round(gb * GB))


class NoCache:
    """The default of every producer's `disk` attribute: reads and writes without bookkeeping (Server()
    without a Cache, the tools, the tests)."""

    def read(self, path):
        """The file's bytes, or None if it is not there (also when it was evicted a moment ago). On Windows a file
        another process is replacing or deleting cannot be opened for a moment (PermissionError: a far tile of a
        first visit to Chicago failed on a Windows test PC, 2026-10-24): tried again READ_WAITS, then a miss (the
        caller fetches it again and staging.replace_cached keeps whichever copy goes in)."""
        for wait in READ_WAITS + (None,):
            try:
                with open(path, "rb") as f:
                    return f.read()
            except FileNotFoundError:
                return None
            except PermissionError:
                if wait is None:
                    log.debug("cache: %s held by another process: a miss", path)
                    return None
                time.sleep(wait)

    def used(self, path, size=None):
        pass

    def wrote(self, path, size=None):
        pass

    def forget(self, path):
        pass


NONE = NoCache()


class Cache(NoCache):
    """Bookkeeping, cap and eviction for one cache root. used() / wrote() / read() are thread-safe and
    cheap (any producer thread); evict(), clear() and save() take a while (the background thread, or
    an executor for clear)."""

    def __init__(self, root, cap_gb=None, now=time.time, start=True):
        """cap_gb: the cap (GB, 0 = unlimited); None = the cap saved in the index (the last one the mod
        set), else DEFAULT_CAP_GB. start: run the walk and the eviction thread (False: the caller calls
        scan() / tick() itself, the tests)."""
        self.root = os.path.abspath(root)
        self._prefix = self.root + os.sep
        self.now = now
        self.started = now()
        self._lock = threading.Lock()
        self._entries = {}          # "dem/mapterhorn/12/1/2.webp" -> [bytes or None (not known yet), last use s]
        self._used = 0              # bytes of the entries with a known size
        self._cap = None            # bytes, 0 = unlimited (set below)
        self._dirty = False
        self.ready = False          # the start-up walk is done
        self._ready_ev = threading.Event()
        self._last_evict = None
        self._last_save = self.started
        self._wake = threading.Event()
        self._closed = False
        self.stats = {"evicted": 0, "evicted_bytes": 0, "cleared": 0, "cleared_bytes": 0, "expired": 0}
        self.managed = self._check_version()
        saved_cap = self._load_index()
        self._cap = cap_bytes(cap_gb if cap_gb is not None else
                              saved_cap / GB if saved_cap is not None else DEFAULT_CAP_GB)
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="cache", daemon=True)
            self._thread.start()

    # --- layout ----------------------------------------------------------------------------------
    def _check_version(self):
        """True when this layout is ours (VERSION missing: written now); False leaves the cache alone."""
        path = os.path.join(self.root, VERSION_FILE)
        try:
            with open(path) as f:
                text = f.read().strip()
        except FileNotFoundError:
            text = ""
        except OSError as e:
            log.warning("cache: cannot read %s (%s): not managed", path, e)
            return False
        if not text:        # missing, or empty (a write cut short): no newer layout, ours from now on
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                os.makedirs(self.root, exist_ok=True)
                with open(tmp, "w") as f:
                    f.write(f"{LAYOUT_VERSION}\n")
                os.replace(tmp, path)       # atomic: a second server never reads a half-written file
            except OSError as e:
                log.warning("cache: cannot write %s (%s): not managed", path, e)
                return False
            return True
        if text == str(LAYOUT_VERSION):
            return True
        log.warning("cache: %s says layout %r (this server: %d): left alone (no eviction, no clear)",
                    path, text[:40], LAYOUT_VERSION)
        return False

    def _key(self, path):
        """The tracked key ("dem/..." with '/') of a path under the root, or None (outside, not tracked)."""
        if not path.startswith(self._prefix):
            path = os.path.abspath(path)
            if not path.startswith(self._prefix):
                return None
        key = path[len(self._prefix):]
        if os.sep != "/":
            key = key.replace(os.sep, "/")
        top = key.split("/", 1)[0]
        # a file being written: "<path>.<pid>.<thread>.tmp", or mapdata's "....tmp.npz" (numpy appends .npz)
        if top not in MANAGED and top not in KEPT or key.endswith((".tmp", ".tmp.npz")):
            return None
        return key

    def _path(self, key):
        return os.path.join(self.root, *key.split("/"))

    @staticmethod
    def _evictable(key, size):
        return bool(size) and key.split("/", 1)[0] in MANAGED

    def _floor(self):
        """Files used at or after this time are protected (this process, within PROTECT_S)."""
        return max(self.started, self.now() - PROTECT_S)

    # --- the producers' hooks (any thread) --------------------------------------------------------
    def used(self, path, size=None):
        """A disk hit: last use = now (size if the caller knows it)."""
        key = self._key(path)
        if key is None:
            return
        t = self.now()
        with self._lock:
            e = self._entries.get(key)
            if e is None:
                self._entries[key] = [size, t]
                if size is not None:
                    self._used += size
            else:
                e[1] = t
                if size is not None and e[0] != size:
                    self._used += size - (e[0] or 0)
                    e[0] = size
            self._dirty = True

    def wrote(self, path, size=None):
        """A file written (or replaced): its size (stat'ed when not given) and last use = now."""
        if size is None:
            try:
                size = os.path.getsize(path)
            except OSError:
                return
        self.used(path, size)
        if self._cap and self._used > self._cap:
            self._wake.set()

    def read(self, path):
        """A disk hit: marked used first (a pass running now keeps it), then read; a miss leaves no entry."""
        self.used(path)
        data = super().read(path)
        if data is None:
            self.forget(path)
        else:
            self.used(path, len(data))
        return data

    def forget(self, path):
        """A file the producer removed itself (an undecodable tile)."""
        key = self._key(path)
        if key is None:
            return
        with self._lock:
            e = self._entries.pop(key, None)
            if e is not None:
                self._used -= e[0] or 0
                self._dirty = True

    # --- index -----------------------------------------------------------------------------------
    def _load_index(self):
        """Last uses from index.tsv (a damaged line is skipped, a damaged header drops the file: the
        walk then takes the files' mtimes). Returns the saved cap (bytes) or None."""
        path = os.path.join(self.root, INDEX_FILE)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except FileNotFoundError:
            return None
        except OSError as e:
            log.warning("cache: index unreadable (%s): last uses from the files' times", e)
            return None
        head = lines[0].split("\t")
        if head[0] != INDEX_HEADER:
            log.warning("cache: index %s damaged (header %r): last uses from the files' times", path, lines[0][:60])
            return None
        cap = None
        for item in head[1:]:
            if item.startswith("cap="):
                try:
                    cap = cap_bytes(int(item[4:]) / GB)
                except (ValueError, OverflowError):     # (a digit run too long for a float)
                    cap = None
        bad = 0
        for line in lines[1:]:
            if not line:
                continue
            parts = line.split("\t", 2)
            try:
                last, size, key = float(parts[0]), int(parts[1]), parts[2]
            except (ValueError, IndexError):
                bad += 1
                continue
            # a non-finite time ("nan", "-inf") would break the sort and every later save (int(nan))
            if (size < 0 or not math.isfinite(last) or set(key.split("/")) & {"", ".", ".."}
                    or self._key(self._path(key)) != key):
                bad += 1
                continue
            # sizes are checked by the walk; a time from a clock that ran ahead must not protect a file
            self._entries[key] = [size, min(last, self.started - 1)]
        if bad:
            log.warning("cache: index: %d damaged lines skipped", bad)
        return cap

    def save(self):
        """Write index.tsv (atomically) if anything changed. Never for a cache left alone."""
        if not self.managed:
            return False
        with self._lock:
            if not self._dirty:
                return False
            rows = [f"{int(e[1])}\t{e[0]}\t{k}\n" for k, e in self._entries.items() if e[0] is not None]
            cap = self._cap
            self._dirty = False
        path = os.path.join(self.root, INDEX_FILE)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(f"{INDEX_HEADER}\tcap={cap}\n")
                f.writelines(rows)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("cache: index not saved: %s", e)
            with self._lock:
                self._dirty = True
            return False
        self._last_save = self.now()
        return True

    # --- the start-up walk -----------------------------------------------------------------------
    def scan(self):
        """Walk the tracked directories once: sizes, and the mtime as the last use of files the index
        does not know; index entries without a file are dropped; files past their LIFETIMES go."""
        found = {}
        for top in MANAGED + KEPT:
            stack = [os.path.join(self.root, top)]
            while stack:
                d = stack.pop()
                try:
                    it = os.scandir(d)
                except OSError:
                    continue
                with it:
                    for de in it:
                        try:
                            if de.is_dir(follow_symlinks=False):
                                stack.append(de.path)
                                continue
                            if not de.is_file(follow_symlinks=False):
                                continue
                            key = self._key(de.path)
                            if key is None:
                                continue
                            st = de.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if self.managed and self._expired(key, st.st_mtime):
                            try:
                                os.remove(de.path)
                                self.stats["expired"] += 1
                            except OSError:
                                pass
                            continue
                        found[key] = (st.st_size, st.st_mtime)
        with self._lock:
            entries = {}
            for key, (size, mtime) in found.items():
                e = self._entries.get(key)
                if e is None:
                    entries[key] = [size, min(mtime, self.started - 1)]
                else:       # the index's last use; a newer download (a run without the index) counts too
                    fresh = e[1] >= self.started and e[0] is not None     # written while the walk ran
                    entries[key] = [e[0] if fresh else size, max(e[1], min(mtime, self.started - 1))]
            # used / written while the walk ran (a file the walk passed before it existed)
            for key, e in self._entries.items():
                if key not in found and e[1] >= self.started and e[0] is not None:
                    entries[key] = e
            self._entries = entries
            self._used = sum(e[0] for e in entries.values())
            self._dirty = True
            self.ready = True
        self._ready_ev.set()
        log.info("cache: %s in %d files under %s (cap %s)%s", _gb(self._used), len(entries), self.root,
                 _gb(self._cap) if self._cap else "unlimited", "" if self.managed else "; left alone (VERSION)")

    def _expired(self, key, mtime):
        for prefix, age in LIFETIMES.items():
            if key.startswith(prefix) and self.now() - mtime > age:
                return True
        return False

    # --- cap, eviction, clear --------------------------------------------------------------------
    @property
    def cap(self):
        return self._cap

    def set_cap(self, gb):
        """A new cap (GB, 0 = unlimited): kept in the index, applied by the next eviction pass (at
        once if that is due). True if it changed."""
        b = cap_bytes(gb)
        with self._lock:
            if b == self._cap:
                return False
            self._cap = b
            self._dirty = True
        self._last_save = -1e18         # the cap is saved on the next tick
        self._wake.set()
        return True

    def evict(self, target=None):
        """Remove least recently used files until the tracked bytes are at most target (default:
        LOW_WATER of the cap; nothing while under the cap). Returns (files, bytes) removed."""
        if not self.managed or not self.ready:
            return 0, 0
        with self._lock:
            cap = self._cap
            if target is None:
                if not cap or self._used <= cap:
                    return 0, 0
                target = int(cap * LOW_WATER)
            floor = self._floor()
            order = sorted((e[1], k) for k, e in self._entries.items()
                           if self._evictable(k, e[0]) and e[1] < floor)
        self._last_evict = self.now()
        n = b = 0
        for _, key in order:
            if self._used <= target:
                break
            got = self._remove(key, floor, empty_too=False)
            if got is not None:
                n, b = n + 1, b + got
        if n:
            self.stats["evicted"] += n
            self.stats["evicted_bytes"] += b
            log.info("cache: evicted %d least recently used files (%s); %s of %s in use", n, _gb(b),
                     _gb(self._used), _gb(cap) if cap else "unlimited")
        return n, b

    def _remove(self, key, floor, empty_too):
        """Delete one tracked file unless it was used since `floor` (checked under the lock, so a
        producer's used() a moment earlier wins). Bytes removed, or None."""
        with self._lock:
            e = self._entries.get(key)
            if e is None or e[1] >= floor or not (self._evictable(key, e[0]) or
                                                  empty_too and e[0] == 0 and key.split("/", 1)[0] in MANAGED):
                return None
            try:
                os.remove(self._path(key))
            except FileNotFoundError:
                pass
            except OSError as err:
                log.debug("cache: cannot remove %s: %s", key, err)
                return None
            del self._entries[key]
            self._used -= e[0] or 0
            self._dirty = True
            return e[0] or 0

    def clear(self, wait=60.0):
        """"Clear cache": every MANAGED file but the ones this server process used (the current
        session's, however long ago: tiles read at its start may still be in the producers' memory
        caches; gravity/ and netstats/ stay). Waits up to `wait` s for the start-up walk. Returns
        (files, bytes)."""
        if not self.managed or not self._ready_ev.wait(wait):
            return 0, 0
        floor = self.started
        with self._lock:
            keys = [k for k, e in self._entries.items() if e[1] < floor and k.split("/", 1)[0] in MANAGED]
        n = b = 0
        for key in keys:
            got = self._remove(key, floor, empty_too=True)
            if got is not None:
                n, b = n + 1, b + got
        self.stats["cleared"] += n
        self.stats["cleared_bytes"] += b
        log.info("cache: cleared %d files (%s); %s left (this session's and the gravity model)", n, _gb(b),
                 _gb(self._used))
        self.save()
        return n, b

    # --- status ----------------------------------------------------------------------------------
    def usage(self):
        """(bytes, files) tracked now. No lock: the event loop asks (the 'cache' message), and an
        eviction pass or an index save holds the lock for a few tenths of a second on a full cache;
        two reads of a moment apart are good enough for the page."""
        return self._used, len(self._entries)

    def message(self, cleared=None):
        """The `cache` message for the mod's settings page (cleared: bytes freed by a clear)."""
        used, files = self.usage()
        m = {"type": "cache", "v": MSG_VERSION, "usedBytes": int(used), "capBytes": int(self._cap),
             "files": files, "ready": self.ready, "managed": self.managed}
        if cleared is not None:
            m["clearedBytes"] = int(cleared)
        return m

    # --- the background thread -------------------------------------------------------------------
    def tick(self):
        """One check: evict when over the cap (at most every EVICT_S), save the index (every SAVE_S)."""
        if not self.ready:
            return
        now = self.now()
        cap = self._cap
        if cap and self._used > cap and (self._last_evict is None or now - self._last_evict >= EVICT_S):
            self.evict()
        if now - self._last_save >= SAVE_S:
            self.save()

    def _run(self):
        try:
            self.scan()
        except Exception:
            log.exception("cache: start-up walk failed; no eviction this session")
            return
        while not self._closed:
            try:
                self.tick()
            except Exception:           # never take the server down; try again next tick
                log.exception("cache: eviction / index save failed")
            self._wake.wait(TICK_S)
            self._wake.clear()

    def close(self):
        """Stop the thread and save the index (the server's shutdown)."""
        self._closed = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(5.0)
        if self.ready:
            self.save()


def prune_dir(root, max_bytes, keep_s=600.0, match=None, now=None):
    """The caches that keep their own files outside MANAGED (sea carving's raw rasters, compute sharing's replies,
    the road widths' walls): the oldest files (mtime; their readers touch the ones they use) under root removed until
    at most max_bytes are left; files changed within keep_s stay; match(file name) selects files (None: all). A file
    that cannot be removed (mapped by a running process on Windows) is skipped. -> (files removed, bytes left)"""
    now = time.time() if now is None else now
    files, total = [], 0
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if match is not None and not match(n):
                continue
            p = os.path.join(dirpath, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
    removed = 0
    if total > max_bytes:
        for mtime, size, p in sorted(files):
            if total <= max_bytes * LOW_WATER or now - mtime < keep_s:
                break
            try:
                os.remove(p)
            except OSError:
                continue
            total -= size
            removed += 1
    return removed, total


_touched = set()
_touched_lock = threading.Lock()


def touch_once(path):
    """Mark a self-managed cache file used (its mtime now, prune_dir's order), once per process."""
    with _touched_lock:
        if path in _touched:
            return
        _touched.add(path)
    try:
        os.utime(path, None)
    except OSError:
        pass


def _gb(b):
    return f"{b / GB:.2f} GB"
