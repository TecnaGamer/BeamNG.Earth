"""New York's rooftop water tanks where the aerial photo shows them: tank points found by tools/build_nyc_tanks.py.

The procedural rule (buildings.water_tank: WATER_TANK_SHARE of the flat roofs of 6-30 storeys, by the seed) put tanks
where the city has none (Jackson Heights, Crown Heights: 5-6 storey blocks without a single tank on the photo) and too
few where it has many (Chelsea, Murray Hill: 20-30 in a 250 m square). The builder finds them on the NYS 2024
orthophoto (tools/tankdetect.py: cedar tops by colour and rim, metal tops by rim, on the city's footprints) and writes
a shard per z14 map cell (mapcells.py) to the data repo's Pages (<BASE>/14/<x>/<y>.npz): per tank its place on the
map (decimetres in the cell's ENU frame), its diameter, its kind (cedar / metal) and the roof's height.

A road tile in the coverage box asks for the shards under it as it starts (Fetch, on a thread); tank_rows waits WAIT_S
at most. A building whose roof holds a tank point carries a tank there (the topmost part under the point), sized from
the photo; in a cell the manifest lists, no other building has one (the photo is the answer, an empty roof included).
A cell the manifest does not list keeps the procedural tanks. A shard that could not be had: the procedural tanks for
now and the tile faulted (revisit.fault: built again later). Nothing is asked before the manifest is published (a
missing manifest: asked again after MANIFEST_RETRY_S, nothing kept). BNE_NYC_TANKS=off: the procedural tanks as before.

    BNE_NYC_TANKS_URL   the shards' base URL (default: the data repo's Pages, nyctanks/v1)
    BNE_NYC_TANKS_DIR   a local folder with <dir>/manifest.json and <dir>/14/<x>/<y>.npz (a build before publishing)

Data: NYS ITS Geospatial Services orthoimagery (2024); NYC Building Footprints (NYC Open Data terms of use)."""
import io
import json
import logging
import os
import threading
import time
import zipfile

import numpy as np

log = logging.getLogger("earthd.nyctanks")

ON = os.environ.get("BNE_NYC_TANKS", "on") != "off"
VERSION = "v1"
BOX = (40.47, -74.28, 40.94, -73.68)    # south, west, north, east: the five boroughs
ZOOM = 14
WAIT_S = 10.0
SHARD_RETRY_S = 600
MISS_TTL_S = 7 * 86400
MANIFEST_TTL_S = 86400
MANIFEST_RETRY_S = 3600
SHARD_KEYS = ("xy", "d", "kind", "h")
KINDS = ("cedar", "metal")


def base_url():
    got = os.environ.get("BNE_NYC_TANKS_URL")
    if got:
        return got.rstrip("/")
    import railtags
    return f"{railtags.PAGES}/nyctanks/{VERSION}"


def local_dir():
    return os.environ.get("BNE_NYC_TANKS_DIR") or None


def cells_over(lat0, lon0, lat1, lon1):
    import mapcells
    s, w, n, e = max(lat0, BOX[0]), max(lon0, BOX[1]), min(lat1, BOX[2]), min(lon1, BOX[3])
    if s > n or w > e:
        return []
    return list(mapcells.cells_for_bbox(s, w, n, e, z=ZOOM))


def encode(xy_dm, d_dm, kind, h_dm):
    """A shard's arrays (the builder's): xy (N, 2) int32 decimetres in the cell's ENU frame, d (N,) uint8 diameter in
    decimetres, kind (N,) uint8 (KINDS), h (N,) uint16 the roof's height in decimetres."""
    return {"xy": np.asarray(xy_dm, np.int32).reshape(-1, 2), "d": np.clip(np.asarray(d_dm), 0, 255).astype(np.uint8),
            "kind": np.asarray(kind, np.uint8), "h": np.clip(np.asarray(h_dm), 0, 65535).astype(np.uint16)}


def decode(body):
    """{xy (N, 2) m in the cell's frame, d (N,) m, kind (N,), h (N,) m}; {} for an empty or unreadable shard."""
    if not body:
        return {}
    try:
        with np.load(io.BytesIO(body), allow_pickle=False) as z:
            a = {k: z[k] for k in SHARD_KEYS}
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as e:
        log.warning("nyc tanks shard unreadable: %s", e)
        return {}
    n = len(a["d"])
    if a["xy"].shape != (n, 2) or len(a["kind"]) != n or len(a["h"]) != n:
        log.warning("nyc tanks shard malformed")
        return {}
    return {"xy": a["xy"].astype(np.float64) / 10.0, "d": a["d"].astype(np.float64) / 10.0,
            "kind": a["kind"].astype(np.int64), "h": a["h"].astype(np.float64) / 10.0}


class Source:
    """The shards: from the local folder (BNE_NYC_TANKS_DIR), else fetched once (the manifest's cells only) and kept on
    disk under <cache>/pagesdata/nyctanks/<VERSION>/, the last MEM held in memory."""

    MEM = 32

    def __init__(self, cache_dir, fetcher=None, local=None, base=None):
        self.dir = os.path.join(cache_dir, "pagesdata", "nyctanks", VERSION) if cache_dir else None
        self.local = local
        self.base = base or base_url()
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.mem = {}
        self.failed = {}
        self._cells = None
        self._cells_at = -1e18
        self._absent = None

    def _path(self, key):
        return os.path.join(self.dir, f"{key.z}_{key.x}_{key.y}.npz") if self.dir else None

    def cells(self):
        """The manifest's cells ("z/x/y"); False: none published (404); None: not had (offline). Asked again after
        MANIFEST_RETRY_S."""
        if self.local:
            if self._cells is None:
                try:
                    with open(os.path.join(self.local, "manifest.json"), "rb") as fh:
                        self._cells = set(json.loads(fh.read().decode("utf-8")).get("cells", {}).keys())
                except (OSError, ValueError, UnicodeDecodeError):
                    return False
            return self._cells
        import netfetch
        with self.lock:
            if self._cells is not None:
                return self._cells
            if time.time() - self._cells_at < MANIFEST_RETRY_S:
                return self._absent
            self._cells_at = time.time()
            if self.fetcher is None:
                self.fetcher = netfetch.Fetcher()
            fetcher = self.fetcher
        path = os.path.join(self.dir, "manifest.json") if self.dir else None
        body = None
        if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < MANIFEST_TTL_S:
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                body = None
        if body is None:
            try:
                body = fetcher.get(f"{self.base}/manifest.json", kind="demand").body
            except netfetch.NotFound:
                with self.lock:
                    self._absent = False
                return False
            except Exception as e:
                log.debug("nyc tanks manifest: %s: %s", type(e).__name__, e)
                with self.lock:
                    self._absent = None
                return None
            _keep(path, body, self.dir)
        try:
            j = json.loads(body.decode("utf-8"))
            cells = set(j.get("cells", {}).keys())
        except (ValueError, AttributeError, UnicodeDecodeError):
            return None
        import railtags
        with self.lock:
            self._cells = cells
            self.data = railtags.data_url(j)    # (the shards' host the manifest names, None: beside it)
        return cells

    def shard(self, key):
        """The arrays of cell key ({}: none there), or None: not had (offline, the site failing: asked again later)."""
        with self.lock:
            got = self.mem.get(key)
        if got is not None:
            return got
        if self.local:
            try:
                with open(os.path.join(self.local, str(key.z), str(key.x), f"{key.y}.npz"), "rb") as fh:
                    body = fh.read()
            except OSError:
                body = b""
        else:
            path = self._path(key)
            body = None
            if path and os.path.exists(path):
                try:
                    if not (os.path.getsize(path) == 0 and time.time() - os.path.getmtime(path) > MISS_TTL_S):
                        with open(path, "rb") as fh:
                            body = fh.read()
                except OSError:
                    body = None
            if body is None:
                body = self._fetch(key)
                if body is None:
                    return None
                _keep(path, body, self.dir)
        got = decode(body)
        with self.lock:
            self.mem[key] = got
            while len(self.mem) > self.MEM:
                self.mem.pop(next(iter(self.mem)))
        return got

    def _fetch(self, key):
        import netfetch
        with self.lock:
            if time.time() - self.failed.get(key, -1e18) < SHARD_RETRY_S:
                return None
            if self.fetcher is None:
                self.fetcher = netfetch.Fetcher()
            fetcher = self.fetcher
        import railtags
        try:
            return railtags.fetch_data(fetcher, getattr(self, "data", None), self.base,
                                       f"{key.z}/{key.x}/{key.y}.npz")
        except netfetch.NotFound:
            return b""
        except Exception as e:
            log.debug("nyc tanks shard %s: %s: %s", key, type(e).__name__, e)
            with self.lock:
                self.failed[key] = time.time()
            return None


def _keep(path, body, d):
    if not path:
        return
    try:
        os.makedirs(d, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError as e:
        log.debug("nyc tanks: keep %s: %s", path, e)


_SOURCES = {}
_SRC_LOCK = threading.Lock()
_POOL = None


def source(cache_dir):
    k = (cache_dir, local_dir(), base_url())
    with _SRC_LOCK:
        got = _SOURCES.get(k)
        if got is None:
            got = _SOURCES[k] = Source(cache_dir, local=k[1], base=k[2])
        return got


class Fetch:
    """A tile's tank shards, asked for on a thread as the tile starts; result() waits WAIT_S at most."""

    def __init__(self, frame, x0, y0, x1, y1, cache_dir):
        global _POOL
        self.fut, self.frame, self.box = None, frame, (x0, y0, x1, y1)
        if not ON or not (cache_dir or local_dir()):
            return
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1], np.float64), np.array([y0, y0, y1, y1], np.float64),
                                          0.0)
        keys = cells_over(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        if not keys:
            return
        with _SRC_LOCK:
            if _POOL is None:
                from concurrent.futures import ThreadPoolExecutor
                _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nyctanks")
        src = source(cache_dir)

        def work():
            cells = src.cells()
            if not cells:               # (None: not had, asked again; False: not published)
                return cells
            return [(k, src.shard(k) if f"{k.z}/{k.x}/{k.y}" in cells else False) for k in keys]
        self.fut = _POOL.submit(work)

    def result(self):
        """Tanks, or None (no data here: the procedural tanks everywhere)."""
        if self.fut is None:
            return None
        try:
            got = self.fut.result(timeout=WAIT_S)
        except Exception:
            got = None
        if got is False:
            return None                 # (not published: nothing asked, nothing kept)
        if got is None or any(s is None for _k, s in got):
            import revisit
            revisit.fault()             # (a later visit builds it with the measured tanks)
        have = [(k, s) for k, s in (got or ()) if s is not None and s is not False]
        return Tanks(self.frame, have) if have else None


class Tanks:
    """The measured tanks of a tile's covered cells in its anchor frame: pts (N, 2) m, d (N,) m, kind (N,), h (N,) m;
    covered(lat, lon): is the place in a cell the photo answers for (an empty roof there has no tank)."""

    def __init__(self, frame, shards):
        import geodesy
        self.keys = {(k.z, k.x, k.y) for k, _s in shards}
        pts, d, kind, h = [np.zeros((0, 2))], [np.zeros(0)], [np.zeros(0, np.int64)], [np.zeros(0)]
        for key, s in shards:
            if not s or not len(s["d"]):
                continue
            cf = geodesy.EnuFrame(*key.center(), 0.0)
            la, lo, _ = cf.enu_to_geodetic(s["xy"][:, 0], s["xy"][:, 1], 0.0)
            e, n, _ = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            pts.append(np.stack([np.atleast_1d(e), np.atleast_1d(n)], 1))
            d.append(s["d"])
            kind.append(s["kind"])
            h.append(s["h"])
        self.pts, self.d = np.concatenate(pts), np.concatenate(d)
        self.kind, self.h = np.concatenate(kind), np.concatenate(h)

    def __len__(self):
        return len(self.d)

    def covered(self, lat, lon):
        """Per point (arrays): is it in a cell the photo answers for."""
        import mapcells
        lat, lon = np.atleast_1d(np.asarray(lat, np.float64)), np.atleast_1d(np.asarray(lon, np.float64))
        if not len(lat):
            return np.zeros(0, bool)
        return np.array([(k.z, k.x, k.y) in self.keys for k in mapcells.cells_of(lat, lon, z=ZOOM)], bool)


def covered_items(tanks, frame, items, x0, y0):
    """Per item (buildings' (spec, gmin, gmax), rings relative to x0, y0): is its centre in a covered cell."""
    if tanks is None or not items:
        return np.zeros(len(items), bool)
    c = np.array([np.asarray(s.ring, np.float64)[:, :2].mean(0) if len(s.ring) else (0.0, 0.0)
                  for s, _a, _b in items]) + np.array([x0, y0])
    la, lo, _ = frame.enu_to_geodetic(c[:, 0], c[:, 1], np.zeros(len(c)))
    return tanks.covered(la, lo)


def tile_rows(fetch, frame, items, x0, y0):
    """The tile's rooftank Forest rows: measured where the photo answers, procedural elsewhere (buildings)."""
    import buildings
    tanks = fetch.result() if fetch is not None else None
    if tanks is None:
        return buildings.tank_rows(items, x0, y0)
    return buildings.measured_tank_rows(items, x0, y0, tanks, covered_items(tanks, frame, items, x0, y0))
