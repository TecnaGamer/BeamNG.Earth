"""New York City's measured building massing: the setbacks of its 2014 3-D Building Model as building parts.

The city's model (NYC Office of Technology and Innovation, formerly DoITT, 2014) has every roof level of every
building that differs by more than 8 ft as a flat roof of its own (bulkheads and towers over 10 ft included; its
water tanks, where it has them at all, are boxes like the bulkheads: buildings.WATER_TANK_* places tanks). tools/build_nyc_massing.py turns each building with setbacks into stacked parts (a band per roof level,
its outline the union of the roofs at or above its top), checks it against the city's live Building Footprints
(gone, rebuilt since 2014, or a different height: left out) and writes a shard per z14 map cell (mapcells.py) to the
data repo's Pages (<BASE>/14/<x>/<y>.npz, ~10-170 kB a cell).

A road tile in the coverage box (BOX: the five boroughs) asks for the shards under it as it starts (Fetch, on a
thread), and the buildings wait for them WAIT_S at most. A map building with no parts of its own (OSM's / Overture's
building parts always win) whose outline holds a record's interior point and is within AREA_RATIO of its area takes
the record's parts (the join in Rust: earthfast.nyc_massing_match), through the same path the map's parts take
(buildings.tile_buildings). A mapped pitched roof, or a building off the ground (min_height), keeps the map's shape.
A shard that could not be had faults the tile (revisit.fault: built again later) and is asked again after
SHARD_RETRY_S; one the site does not have (404) is kept as an empty file for MISS_TTL_S. Outside the box, or with
BNE_NYC_MASSING=off: nothing is asked, the map's buildings as before.

    BNE_NYC_MASSING_URL   the shards' base URL (default: the data repo's Pages, nycmassing/v1)
    BNE_NYC_MASSING_DIR   a local folder with <dir>/14/<x>/<y>.npz instead (testing a build before it is published)

Data: NYC Office of Technology and Innovation, 3-D Building Model (2014); NYC Building Footprints (NYC Open Data
terms of use: free use, credit the source).
"""
import io
import logging
import os
import threading
import time
import zipfile

import numpy as np

import fast

log = logging.getLogger("earthd.nycmassing")

ON = os.environ.get("BNE_NYC_MASSING", "on") != "off"
VERSION = "v1"
BOX = (40.47, -74.28, 40.94, -73.68)    # south, west, north, east: the five boroughs (the model's own coverage)
ZOOM = 14
PAD_M = 200.0                   # records whose point is this far past a tile are its too (a long building's own)
AREA_RATIO = (0.8, 1.25)        # map footprint area / the record's
WAIT_S = 10.0                   # s the buildings wait for a tile's shards at most
SHARD_RETRY_S = 600             # a shard that could not be had: asked again after this long
MISS_TTL_S = 7 * 86400          # a shard the site did not have (404): asked again after this long
# The manifest (the builder's manifest.json: its cells) decides which shards exist: none is asked for a cell it does
# not list, and while the site has no manifest (not published yet, or the site failing) nothing is asked at all and
# nothing is kept: a 404 kept for a week per cell would have hidden the data for a week from anyone who drove there
# before it was published. Kept on disk MANIFEST_TTL_S; a missing one asked again after MANIFEST_RETRY_S.
MANIFEST_TTL_S = 86400
MANIFEST_RETRY_S = 3600
NATIVE = 186                    # earthfast.VERSION of nyc_massing_match
GROUND_M = 0.5                  # a map building whose bottom (min_height) is higher than this keeps its own shape
FLAT_ROOFS = ("", "flat")
SHARD_KEYS = ("bin", "pxy", "area", "top", "nparts", "pbot", "ptop", "pnv", "xy")


def base_url():
    got = os.environ.get("BNE_NYC_MASSING_URL")
    if got:
        return got.rstrip("/")
    import railtags
    return f"{railtags.PAGES}/nycmassing/{VERSION}"


def local_dir():
    return os.environ.get("BNE_NYC_MASSING_DIR") or None


def in_box(lat, lon):
    return BOX[0] <= float(lat) <= BOX[2] and BOX[1] <= float(lon) <= BOX[3]


def cells_over(lat0, lon0, lat1, lon1):
    """The z14 map cells (mapcells.CellKey) over a lat / lon box, none outside BOX."""
    import mapcells
    s, w, n, e = max(lat0, BOX[0]), max(lon0, BOX[1]), min(lat1, BOX[2]), min(lon1, BOX[3])
    if s > n or w > e:
        return []
    return list(mapcells.cells_for_bbox(s, w, n, e, z=ZOOM))


# --- the shards ---------------------------------------------------------------------------------------------------
def decode(body):
    """The arrays of a shard's bytes, coordinates in metres in its cell's ENU frame: bin (B,), pt (B, 2) each
    building's interior point, area (B,) m2 (its footprint), top (B,) m, first (B + 1,) its parts' range, bot / ptop
    (P,) m above the building's lowest ground, vfirst (P + 1,) each part's corners' range in xy (V, 2) (open rings).
    {} for an empty or unreadable shard."""
    if not body:
        return {}
    try:
        with np.load(io.BytesIO(body), allow_pickle=False) as z:
            a = {k: z[k] for k in SHARD_KEYS}
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as e:
        log.warning("nyc massing shard unreadable: %s", e)
        return {}
    B, P = len(a["bin"]), len(a["pbot"])
    nparts = a["nparts"].astype(np.int64)
    pnv = a["pnv"].astype(np.int64)
    if (a["pxy"].shape != (B, 2) or len(a["area"]) != B or len(a["top"]) != B or len(nparts) != B
            or int(nparts.sum()) != P or len(a["ptop"]) != P or len(pnv) != P or a["xy"].ndim != 2
            or a["xy"].shape[1] != 2 or int(pnv.sum()) != len(a["xy"]) or (P and pnv.min() < 3)):
        log.warning("nyc massing shard malformed")
        return {}
    first = np.concatenate([[0], np.cumsum(nparts)])
    vfirst = np.concatenate([[0], np.cumsum(pnv)])
    # (each ring's first corner is relative to its building's point, the others to the corner before)
    d = a["xy"].astype(np.int64)
    owner = np.repeat(np.arange(B), nparts)
    if P:
        d[vfirst[:-1]] += a["pxy"].astype(np.int64)[owner]
    c = np.cumsum(d, axis=0)
    before = np.vstack([np.zeros((1, 2), np.int64), c])[vfirst[:-1]]
    xy = (c - np.repeat(before, pnv, axis=0)).astype(np.float64) / 10.0
    return {"bin": a["bin"].astype(np.int64), "pt": a["pxy"].astype(np.float64) / 10.0,
            "area": a["area"].astype(np.float64), "top": a["top"].astype(np.float64) / 10.0, "first": first,
            "bot": a["pbot"].astype(np.float64) / 10.0, "ptop": a["ptop"].astype(np.float64) / 10.0,
            "vfirst": vfirst, "xy": xy}


class Source:
    """The shards: from the local folder (BNE_NYC_MASSING_DIR), else fetched once and kept on disk under
    <cache>/pagesdata/nycmassing/<VERSION>/, and held in memory (LRU) for the tiles around them."""

    MEM = 32

    def __init__(self, cache_dir, fetcher=None, local=None, base=None):
        self.dir = os.path.join(cache_dir, "pagesdata", "nycmassing", VERSION) if cache_dir else None
        self.local = local
        self.base = base or base_url()
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.mem = {}
        self.failed = {}                # key -> when a fetch failed (asked again after SHARD_RETRY_S)
        self._cells = None              # the manifest's cells ("z/x/y" set), None: not had
        self._cells_at = -1e18          # when the manifest was last asked for (MANIFEST_RETRY_S)
        self._absent = None             # cells()'s answer meanwhile: False (not published) or None (not had)
        self.data = None                # the manifest's "data" host of the shards (railtags.data_url), None: beside it

    def _path(self, key):
        return os.path.join(self.dir, f"{key.z}_{key.x}_{key.y}.npz") if self.dir else None

    def shard(self, key):
        """The arrays of cell key ({} none there), or None: not had (offline, the site failing: asked again later)."""
        with self.lock:
            got = self.mem.get(key)
        if got is not None:
            return got
        body = None
        if self.local:
            try:
                with open(os.path.join(self.local, str(key.z), str(key.x), f"{key.y}.npz"), "rb") as fh:
                    body = fh.read()
            except OSError:
                body = b""
        else:
            cells = self.cells()
            if cells is None:
                return None             # (the manifest could not be had: offline, the site failing; asked again)
            if cells is False:
                return {}               # (no manifest published: nothing asked or kept)
            if f"{key.z}/{key.x}/{key.y}" not in cells:
                return {}               # (a cell the manifest does not list: none there)
            path = self._path(key)
            if path and os.path.exists(path):
                try:
                    stale_miss = os.path.getsize(path) == 0 and time.time() - os.path.getmtime(path) > MISS_TTL_S
                    if not stale_miss:
                        with open(path, "rb") as fh:
                            body = fh.read()
                except OSError:
                    body = None
            if body is None:
                body = self._fetch(key)
                if body is None:
                    return None
                if path:
                    try:
                        os.makedirs(self.dir, exist_ok=True)
                        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
                        with open(tmp, "wb") as fh:
                            fh.write(body)
                        os.replace(tmp, path)
                    except OSError as e:
                        log.debug("nyc massing: keep %s: %s", key, e)
        got = decode(body)
        with self.lock:
            self.mem[key] = got
            while len(self.mem) > self.MEM:
                self.mem.pop(next(iter(self.mem)))
        return got

    def cells(self):
        """The manifest's cells ("z/x/y" strings; kept MANIFEST_TTL_S on disk); False: the site has no manifest (404: not
        published); None: it could not be had (offline, the site failing). Both asked again after MANIFEST_RETRY_S."""
        import json
        import netfetch
        with self.lock:
            if self._cells is not None:
                return self._cells
            if time.time() - self._cells_at < MANIFEST_RETRY_S:
                return self._absent
            self._cells_at = time.time()
            fetcher = self.fetcher
            if fetcher is None:
                fetcher = self.fetcher = netfetch.Fetcher()
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
            except netfetch.NotFound:   # (not published yet)
                with self.lock:
                    self._absent = False
                return False
            except Exception as e:      # (offline, the site failing)
                log.debug("nyc massing manifest: %s: %s", type(e).__name__, e)
                with self.lock:
                    self._absent = None
                return None
            if path:
                try:
                    os.makedirs(self.dir, exist_ok=True)
                    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
                    with open(tmp, "wb") as fh:
                        fh.write(body)
                    os.replace(tmp, path)
                except OSError as e:
                    log.debug("nyc massing: keep manifest: %s", e)
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

    def _fetch(self, key):
        """The shard's bytes (b"" the site has none), or None (failed: asked again after SHARD_RETRY_S)."""
        import netfetch
        with self.lock:
            if time.time() - self.failed.get(key, -1e18) < SHARD_RETRY_S:
                return None
            fetcher = self.fetcher
            if fetcher is None:
                fetcher = self.fetcher = netfetch.Fetcher()
        import railtags
        try:
            return railtags.fetch_data(fetcher, self.data, self.base, f"{key.z}/{key.x}/{key.y}.npz")
        except netfetch.NotFound:
            return b""
        except Exception as e:          # (offline, limits, the site failing: the map's buildings, asked again later)
            log.debug("nyc massing shard %s: %s: %s", key, type(e).__name__, e)
            with self.lock:
                self.failed[key] = time.time()
            return None


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
    """A tile's shards asked for on a thread as the tile starts; index() waits WAIT_S at most."""

    def __init__(self, frame, x0, y0, x1, y1, cache_dir):
        global _POOL
        self.fut = None
        self.frame, self.box = frame, (x0 - PAD_M, y0 - PAD_M, x1 + PAD_M, y1 + PAD_M)
        if not ON or not (cache_dir or local_dir()):
            return
        bx = self.box
        la, lo, _ = frame.enu_to_geodetic(np.array([bx[0], bx[2], bx[0], bx[2]], np.float64),
                                         np.array([bx[1], bx[1], bx[3], bx[3]], np.float64), 0.0)
        keys = cells_over(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        if not keys:
            return
        with _SRC_LOCK:
            if _POOL is None:
                from concurrent.futures import ThreadPoolExecutor
                _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nycmassing")
        src = source(cache_dir)
        self.fut = _POOL.submit(lambda: [(k, src.shard(k)) for k in keys])

    def index(self):
        """Records of the tile's shards, or None (none here; a shard not had faults the tile)."""
        if self.fut is None:
            return None
        try:
            got = self.fut.result(timeout=WAIT_S)
        except Exception:
            got = None
        if got is None or any(s is None for _k, s in got):
            import revisit
            revisit.fault()             # (a later visit builds it again with the measured massing)
        had = [(k, s) for k, s in (got or ()) if s]
        return Records(self.frame, had, self.box) if had else None


class Records:
    """A tile's records in its anchor frame: each building's point and area for the join, its parts' rings."""

    def __init__(self, frame, shards, box):
        import geodesy
        x0, y0, x1, y1 = box
        pts, areas, parts = [], [], []
        for key, s in shards:
            if not len(s["bin"]):
                continue
            clat, clon = key.center()
            cf = geodesy.EnuFrame(clat, clon, 0.0)
            la, lo, _ = cf.enu_to_geodetic(s["pt"][:, 0], s["pt"][:, 1], 0.0)
            e, n, _ = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            e, n = np.atleast_1d(np.asarray(e, np.float64)), np.atleast_1d(np.asarray(n, np.float64))
            keep = np.flatnonzero((e >= x0) & (e <= x1) & (n >= y0) & (n <= y1))
            if not len(keep):
                continue
            # (the kept buildings' corners, all at once: cell frame -> lat / lon -> anchor frame)
            first, vfirst = s["first"], s["vfirst"]
            prs = [np.arange(first[b], first[b + 1]) for b in keep.tolist()]
            pidx = np.concatenate(prs)
            lens = vfirst[pidx + 1] - vfirst[pidx]
            vidx = np.arange(int(lens.sum())) + np.repeat(vfirst[pidx] - (np.cumsum(lens) - lens), lens)
            vla, vlo, _ = cf.enu_to_geodetic(s["xy"][vidx, 0], s["xy"][vidx, 1], 0.0)
            ve, vn, _ = frame.geodetic_to_enu(np.asarray(vla, np.float64), np.asarray(vlo, np.float64), 0.0)
            vxy = np.stack([np.atleast_1d(ve), np.atleast_1d(vn)], 1)
            at = 0
            for b, pr in zip(keep.tolist(), prs):
                bp = []
                for p in pr.tolist():
                    k = int(vfirst[p + 1] - vfirst[p])
                    ring = vxy[at:at + k]
                    at += k
                    bot, top = float(s["bot"][p]), float(s["ptop"][p])
                    if top - bot <= 0.0:
                        continue
                    # (closed, as the map's rings; (own height, bottom) as buildings._part_heights)
                    bp.append((np.vstack([ring, ring[:1]]), (top - bot, bot), {}))
                pts.append((e[b], n[b]))
                areas.append(float(s["area"][b]))
                parts.append(bp)
        self.pt = np.ascontiguousarray(np.array(pts, np.float64).reshape(-1, 2))
        self.area = np.ascontiguousarray(np.array(areas, np.float64))
        self._parts = parts
        self.claimed = {}               # record -> the map building's id that took it (one building a record)

    def __len__(self):
        return len(self._parts)

    def match_layer(self, f, frame, rows):
        """{row: record} of the rows of a buildings layer (mapgeom.Features) whose outer ring takes a record."""
        if not len(self._parts) or f is None or not len(rows):
            return {}
        fast.require(NATIVE, "nyc_massing_match")
        rows = np.asarray(rows, np.int64)
        po, ro, co = (np.asarray(v, np.int64) for v in (f.part_off, f.ring_off, f.coord_off))
        has = po[rows + 1] > po[rows]
        r0 = ro[np.minimum(po[rows], len(ro) - 2)]
        has &= ro[np.minimum(po[rows] + 1, len(ro) - 1)] > r0
        r0 = np.minimum(r0, len(co) - 2)
        start = np.where(has, co[r0], 0)
        end = np.where(has, co[r0 + 1], 0)
        xy = np.ascontiguousarray(f.xy_in(frame), np.float64)
        idx = np.asarray(fast._ef.nyc_massing_match(xy, np.ascontiguousarray(start), np.ascontiguousarray(end),
                                                    self.pt, self.area, float(AREA_RATIO[0]),
                                                    float(AREA_RATIO[1])), np.int64)
        ids = getattr(f, "ids", None)
        out = {}
        for r, j in zip(rows[idx >= 0].tolist(), idx[idx >= 0].tolist()):
            bid = str(ids[r]) if ids is not None else (id(f), r)
            if self.claimed.setdefault(j, bid) == bid:       # (a building in two cells' layers: the same id)
                out[r] = j
        return out

    def parts(self, j):
        """[(closed ring (k, 2) in the anchor frame, (own height, bottom), {})] of record j, bottom to top."""
        return self._parts[j]


def takes(a, i):
    """May map building row i (attrs a) take a record's parts: on the ground, no mapped pitched roof."""
    mh = a["min_height"][i] if a.get("min_height") is not None else None
    if mh is not None and np.isfinite(mh) and mh > GROUND_M:
        return False
    rs = a["roof_shape"][i] if a.get("roof_shape") is not None else None
    return rs is None or str(rs).strip().lower() in FLAT_ROOFS or str(rs) in ("None", "nan")


def credited_near(cache_dir, lat, lon, near_m):
    """Is a shard with records on this computer within near_m of lat, lon (credits.feature_ids)? Disk only."""
    if not ON or not in_box(lat, lon):
        return False
    dlat = near_m / 110540.0
    dlon = near_m / (111320.0 * max(0.05, float(np.cos(np.radians(lat)))))
    local = local_dir()
    for key in cells_over(lat - dlat, lon - dlon, lat + dlat, lon + dlon):
        p = os.path.join(local, str(key.z), str(key.x), f"{key.y}.npz") if local else \
            os.path.join(cache_dir, "pagesdata", "nycmassing", VERSION, f"{key.z}_{key.x}_{key.y}.npz") \
            if cache_dir else None
        try:
            if p and os.path.getsize(p) > 0:
                return True
        except OSError:
            continue
    return False
