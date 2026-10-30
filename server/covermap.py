"""The in-game map's cache view (the user, 2026-10-28: "in the map add a view that shows what's been cached (and at
what detail) in the map"): glue between the download cache (cache.Cache), the revisit cache's sidecar (revisit.py)
and the Rust index (native/earthfast cachemap.rs: CoverIndex), which keeps every cached tile by place and answers the
map's cache_map with a grid of levels.

The index is built once from the cache's in-memory keys after its start-up walk (no disk walk), on a background
thread; after that the cache's on_change hook queues only new and removed keys (never a repeat hit: the hot path
pays nothing), applied before the next query. The revisit items' places come from their sidecar files (read after
the start-up compaction, then only their new lines).

Modes (cache_map's mode): summary (none / some data / far-field data / ready to drive), elevation and imagery (the
detail zoom), map data (some layers / every layer a drive needs), world (what was built for some anchor: by this
version and settings, or an older one; not in the map's UI yet).
"""
import base64
import logging
import os
import threading

import fast
import revisit

log = logging.getLogger("earthd.covermap")

DEM, IMG = 1, 2                 # CoverIndex classes; map data layers from LAYER0 (ALL_LAYERS order)
LAYER0 = 16
WORLD0 = 1000                   # world rows: WORLD0 + 2 * group + near
MODES = {"summary": 0, "elevation": 1, "imagery": 2, "mapdata": 3, "world": 4}
GRID_MIN_Z, GRID_MAX_Z = 6, 16  # the map asks at clamp(round(view zoom) + 3, 6, 16)
MAX_CELLS = 16384
DRIVE_LAYERS = ("roads", "buildings", "water", "landcover")     # what a road tile and the ground need of a cell
# the summary's thresholds (zooms): far-field data = the far rings' DEM and photos; ready to drive = the near
# terrain's DEM zoom (dem.FINE_ZOOM where Mapterhorn has lidar, else what it has: dem.coverage_index) and photos
# at least this deep; some data = anything at about city scale
FAR_DEM_Z, SOME_DEM_Z = 11, 10
DRIVE_IMG_Z, FAR_IMG_Z, SOME_IMG_Z = 16, 13, 11
ADD_CHUNK = 50000               # keys per call while the index is built (the lock is let go between them)
SIDECAR_EVERY_S = 2.0           # the sidecar files are looked at for new lines at most this often
# The server's worker processes download DEM tiles and build map data cells themselves (blockworker: their own
# producers, without the cache's bookkeeping): those files reach the cache's index at its next start-up walk only. A
# view this close (grid zoom) has its tiles looked up on disk (the folders of its columns), at most every FRESHEN_S
# for the same view and FRESHEN_ANY_S for any, and the files the cache does not know handed to it (cache.wrote: they
# count against the cap from now on, and the hook brings them here).
FRESHEN_MIN_Z = 12
FRESHEN_S, FRESHEN_ANY_S = 15.0, 5.0     # (a city view: 25-70 ms of directory listings, 2026-10-28)
FRESHEN_ZOOMS_UP = 4            # tiles down to this many zooms coarser than the grid
FRESHEN_MAX_FILES = 200000


def rules(mapdata=None):
    """CoverIndex rules: (cache-key prefix, class, fixed zoom 0) for the DEM sources, the imagery chain's sources and,
    with map data, the current version of every layer (an older layer version or release is never read again)."""
    import dem as demmod
    import imagery as imagerymod
    out = [(f"dem/{demmod.MAPTERHORN['name']}/", DEM, 0), (f"dem/{demmod.AWS['name']}/", DEM, 0)]
    out += [(f"imagery/{name}/", IMG, 0) for name in sorted(imagerymod.SOURCES)]
    if mapdata is not None:
        import mapcells
        import mapdata as mapdatamod
        root = os.path.abspath(mapdata.cache_dir)
        for i, layer in enumerate(mapdatamod.ALL_LAYERS):
            p = os.path.relpath(mapdata.path(mapcells.CellKey(14, 0, 0), layer), root)
            parts = p.replace(os.sep, "/").split("/")[:-3]       # (.../<layer>/14/0/0.npz)
            out.append(("/".join(parts) + "/", LAYER0 + i, 0))
    return out


class CoverMap:
    """One per server. query() from any thread (the event loop's executor)."""

    def __init__(self, cache, cache_dir, mapdata=None, signature=None):
        fast.require(101, "CoverIndex")
        import mapdata as mapdatamod
        self.cache = cache
        self.cache_dir = cache_dir
        self.signature = signature or (lambda: {})
        self.layers = [LAYER0 + i for i in range(len(mapdatamod.ALL_LAYERS))] if mapdata is not None else []
        self.drive_mask = sum(1 << mapdatamod.ALL_LAYERS.index(n) for n in DRIVE_LAYERS) if mapdata is not None else 0
        self.rules = rules(mapdata)
        self.idx = fast._ef.CoverIndex(self.rules)
        self.fresh = (None, -1e18)          # (the last freshened view, when)
        self.lock = threading.Lock()        # the index (a Rust object: one caller at a time)
        self.pending = []                   # (key, added) from the cache's hook, under the cache's lock
        self.ready = False
        self.gen = 0                        # bumped whenever the index changed (the map redraws)
        self.groups = {}                    # (code, sig) -> group id of the sidecar rows
        self.seen = set()                   # (kind, key) rows counted
        self.offsets = {}                   # sidecar path -> bytes read
        self.sidecar_at = -1e18
        self.caps_id = None
        self._thread = None

    # --- building ---------------------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="covermap", daemon=True)
        self._thread.start()
        return self

    def _changed(self, key, added):
        self.pending.append((key, added))   # (under the cache's lock: cache.used / forget / _remove)

    def _run(self):
        try:
            self.build()
        except Exception:
            log.exception("cache map: index not built (the map's cache view stays empty)")

    def build(self, wait=None):
        """The index from the cache's keys (after its start-up walk) and the sidecar lines (after the compaction)."""
        import time
        cache = self.cache
        if cache is not None:
            ev = getattr(cache, "_ready_ev", None)
            while ev is not None and not ev.wait(wait if wait is not None else 60.0):
                if wait is not None:
                    return False
            t0 = time.perf_counter()
            with cache._lock:                   # the hook and the snapshot at one moment: nothing missed or doubled
                cache.on_change = self._changed
                keys = list(cache._entries)
            n = 0
            for i in range(0, len(keys), ADD_CHUNK):
                with self.lock:
                    n += self.idx.add(keys[i:i + ADD_CHUNK])
            log.info("cache map: %d of %d cached files indexed by place in %.2f s", n, len(keys),
                     time.perf_counter() - t0)
        revisit.COMPACTED.wait(120.0 if wait is None else wait)
        self._sidecars(force=True)
        self.ready = True
        self.gen += 1
        return True

    def _drain(self):
        """The keys the cache added or removed since the last call, into the index (in order)."""
        cache = self.cache
        if cache is None:
            return
        with cache._lock:
            got, self.pending = self.pending, []
        if not got:
            return
        with self.lock:
            run, add = [], None
            for key, added in got + [(None, None)]:
                if added is not add and run:
                    (self.idx.add if add else self.idx.remove)(run)
                    run = []
                if key is not None:
                    run.append(key)
                    add = added
        self.gen += 1

    def _sidecars(self, force=False):
        """New lines of the revisit sidecar files into the index's world rows."""
        import time
        now = time.monotonic()
        if not force and now - self.sidecar_at < SIDECAR_EVERY_S:
            return
        self.sidecar_at = now
        d = os.path.join(self.cache_dir, revisit.SUBDIR, revisit.WHERE_DIR)
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".tsv"))
        except OSError:
            return
        rows = []
        for name in names:
            p = os.path.join(d, name)
            pos = self.offsets.get(p, 0)
            try:
                if os.path.getsize(p) <= pos:
                    continue
                with open(p, "rb") as f:
                    f.seek(pos)
                    data = f.read()
            except OSError:
                continue
            end = data.rfind(b"\n") + 1         # whole lines only (one being written stays for the next look)
            self.offsets[p] = pos + end
            for line in data[:end].decode("utf-8", "replace").splitlines():
                r = revisit.parse_where(line)
                if r is None or (r[2], r[3]) in self.seen:
                    continue
                self.seen.add((r[2], r[3]))
                gid = self.groups.setdefault((r[0], r[1]), len(self.groups))
                rows.append((WORLD0 + 2 * gid + (1 if r[2] in revisit.NEAR_KINDS else 0), r[6], r[7], r[8], r[9]))
        if rows:
            with self.lock:
                self.idx.add_rects(rows, 1)
            self.gen += 1

    def freshen(self, z, x0, y0, w, h):
        """The view's tiles written by other processes (FRESHEN_MIN_Z..): handed to the cache. Returns how many."""
        import time
        cache = self.cache
        if cache is None or z < FRESHEN_MIN_Z or not hasattr(cache, "wrote") or not getattr(cache, "ready", False):
            return 0
        now = time.monotonic()
        view, at = self.fresh
        if now - at < FRESHEN_ANY_S or (view == (z, x0, y0, w, h) and now - at < FRESHEN_S):
            return 0
        self.fresh = ((z, x0, y0, w, h), now)
        known = cache._entries          # (membership only: a dict lookup, no lock)
        seen, new = 0, []
        for prefix, _cls, _fz in self.rules:
            base = os.path.join(cache.root, *prefix.rstrip("/").split("/"))
            try:
                zdirs = os.listdir(base)
            except OSError:
                continue
            for zs in zdirs:
                if not zs.isdigit() or not z - FRESHEN_ZOOMS_UP <= int(zs) <= 20:
                    continue
                tz = int(zs)
                if tz >= z:
                    s = tz - z
                    tx0, tx1, ty0, ty1 = x0 << s, ((x0 + w) << s) - 1, y0 << s, ((y0 + h) << s) - 1
                else:
                    s = z - tz
                    tx0, tx1, ty0, ty1 = x0 >> s, (x0 + w - 1) >> s, y0 >> s, (y0 + h - 1) >> s
                zdir = os.path.join(base, zs)
                try:
                    xdirs = [xs for xs in os.listdir(zdir) if xs.isdigit() and tx0 <= int(xs) <= tx1]
                except OSError:
                    continue
                for xs in xdirs:
                    try:
                        with os.scandir(os.path.join(zdir, xs)) as it:
                            for de in it:
                                stem = de.name.split(".", 1)[0]
                                if not stem.isdigit() or not ty0 <= int(stem) <= ty1 or ".tmp" in de.name:
                                    continue
                                seen += 1
                                if f"{prefix}{zs}/{xs}/{de.name}" not in known:
                                    new.append(de.path)
                    except OSError:
                        continue
                    if seen > FRESHEN_MAX_FILES:
                        break
        for p in new:
            cache.wrote(p)
        return len(new)

    def _caps(self):
        """Mapterhorn's deepest zoom per z6 tile into the index (once per new coverage list)."""
        import dem as demmod
        cov = demmod._coverage.get("index")
        if cov is None or id(cov) == self.caps_id:
            return
        with self.lock:
            self.idx.set_dem_caps([(int(x), int(y), int(z)) for (x, y), z in cov.items()], demmod.GLOBAL_ZOOM)
        self.caps_id = id(cov)

    def world_classes(self):
        """[(class, near, current)] of the sidecar rows' groups."""
        sig = self.signature()
        code = revisit.code_key()
        out = []
        for (c, s), gid in list(self.groups.items()):
            cur = c == code and revisit.sig_current(s, sig)
            out += [(WORLD0 + 2 * gid, False, cur), (WORLD0 + 2 * gid + 1, True, cur)]
        return out

    # --- the map's question -------------------------------------------------------------------------
    def query(self, z, x0, y0, x1, y1, mode="summary"):
        """The cache_map_result fields for the zoom-z cells x0..x1, y0..y1 (inclusive; clipped to the world):
        {z, x0, y0, w, h, cells (base64 of w * h levels, north row first), gen, ready}. ValueError on a bad ask."""
        import math

        import dem as demmod
        if isinstance(z, bool) or not isinstance(z, int) or not GRID_MIN_Z <= z <= GRID_MAX_Z:
            raise ValueError(f"cache map zoom {z!r}: {GRID_MIN_Z}..{GRID_MAX_Z}")
        m = MODES.get(mode or "summary")
        if m is None:
            raise ValueError(f"cache map mode {mode!r}: one of {', '.join(MODES)}")
        n = 1 << z
        vals = []
        for v in (x0, y0, x1, y1):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError("cache map: x0, y0, x1, y1 must be numbers")
            vals.append(int(v))
        x0, y0, x1, y1 = (max(0, min(n - 1, v)) for v in vals)
        if x1 < x0 or y1 < y0:
            raise ValueError("cache map: an empty box")
        w, h = x1 - x0 + 1, y1 - y0 + 1
        if w * h > MAX_CELLS:
            raise ValueError(f"cache map: {w} x {h} cells (at most {MAX_CELLS})")
        out = {"z": z, "x0": x0, "y0": y0, "w": w, "h": h, "ready": self.ready}
        if not self.ready:
            out.update(cells="", gen=self.gen)
            return out
        try:
            self.freshen(z, x0, y0, w, h)
        except Exception as e:          # (the view as the cache knows it)
            log.debug("cache map: freshen: %s", e)
        self._drain()
        self._sidecars()
        self._caps()
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y0 + h / 2) / n))))
        hm = demmod.zoom_for(lat, 1.25, demmod.FINE_ZOOM)
        world = self.world_classes() if m == MODES["world"] else []
        with self.lock:
            cells = self.idx.grid(z, x0, y0, w, h, m, dem=DEM, img=IMG, layers=self.layers,
                                  drive_mask=self.drive_mask, hm_zoom=hm, far_dem=FAR_DEM_Z, drive_img=DRIVE_IMG_Z,
                                  far_img=FAR_IMG_Z, some_dem=SOME_DEM_Z, some_img=SOME_IMG_Z, world=world)
        out.update(cells=base64.b64encode(cells).decode("ascii"), gen=self.gen)
        return out

    def close(self):
        if self.cache is not None and self.cache.on_change == self._changed:
            with self.cache._lock:
                self.cache.on_change = None
