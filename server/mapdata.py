"""The shared map-data pipeline: roads, buildings, water, land use, infrastructure, land cover and
canopy height per global cell, for every content feature to read (docs/ARCHITECTURE.md "Map data";
design: docs/research/2026-10-10-world-data-sources.md).

    md = mapdata.MapData(cache_dir, fetcher)            # the server's netfetch.Fetcher
    cell = md.cell(mapcells.cell_of(lat, lon))          # or md.cell_at(lat, lon)
    roads = cell.layers["roads"]                        # mapgeom.Features
    roads.attrs["class"], roads.rules["surface"], roads.parts(i, roads.en)
    lc = cell.layers["landcover"]                       # maprasters.Raster (WorldCover classes)
    cell.provenance                                     # sources + licences, for the credits

Cells (mapcells.py): Web-Mercator z14 tiles, keyed (z, x, y) independently of the anchor, the
player and the load order. A cell's content is a pure function of (cell key, source release ids,
VERSION): features sorted by their Overture GERS id, rules in their list order, rasters on the
sources' own pixel grids, nothing from timing, threads or fetch order. CellData.digest() hashes
it (the golden-hash tests and the deterministic generators can rely on it).

Layers (LAYERS):
  roads, connectors, buildings, water, land_use, infrastructure   Overture (overture.py)
  landcover                                                       ESA WorldCover 10 m (maprasters.py)
  canopy                                                          Meta/WRI CHMv2 1.2 m (maprasters.py)
Vector features carry lon/lat (float64) and `en`, metres in the cell's local ENU frame (the
tangent plane at the cell's centre, geodesy.EnuFrame(lat_c, lon_c, 0)), plus `clipped` (water
and land-use polygons are cut to the cell) and `own`: build it in this cell. A whole feature is
owned by the one cell that contains its bbox centre; a clipped polygon is this cell's piece of it,
owned here (every cell it touches owns its piece). So a generator that builds the owned features
of every cell builds each feature exactly once and every polygon whole.

Network: every request goes through the netfetch.Fetcher (host policies for stac.overturemaps.org,
the Overture S3 bucket and its Azure mirror, WorldCover and the canopy bucket), with the request
kind given to cell() ("demand", "prefetch", "background"). Nothing is fetched twice:

  <cache>/mapdata/v<VERSION>/<source>/<release>/<layer>/<z>/<x>/<y>.npz   one cell's layer
  <cache>/mapdata/raw/<source>/<release>/...                            range blocks of the source
                                                                         files, STAC JSON (immutable,
                                                                         shared by all VERSIONs)

A layer file is written once (atomically) and read back with allow_pickle=False. Writing a new
VERSION (or release) leaves the old directories for the cache cap's LRU to evict.

Not here (documented API for later): building meshes, road ribbons, ground layers, trees, sending
anything to the mod. Generators take CellData and key their choices on feature ids and global
positions (docs/ROADMAP.md "Principle: deterministic generation").

CLI (development):  python3 server/mapdata.py --cell 39.2834,-76.5934 --dump --png out.png
                    python3 server/mapdata.py --latest
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np

import geodesy
import gradesep
import mapcells
import mapgeom
import maprasters
import netfetch
import overture
import rangeread
import revisit
import staging
import xproc

log = logging.getLogger("earthd.mapdata")

LAYER_THREADS = 4               # layers of a cell built at once (each fetches through the shared limits)
OSM_THREADS = 8                 # cells whose OpenStreetMap tags are read at once (alongside their Overture roads)
# (cell, layer)s built ahead at once by warm() (a fresh place's first road tile asked for ~70 of them one after another:
# 56 s in Marseille, GitHub #241): half the CPUs, at most 8, and none on 8 or fewer: each is a DuckDB query and a
# decode of its own, and on a Steam Deck (4 cores, 8 threads) the work done ahead kept the CPUs from the road tiles
# (all 36 of a fresh place: 122 s -> 145-181 s over 8 places, 2026-10-25); on a 16-thread PC they came 2-10 s sooner.
_CPUS = os.cpu_count() or 4
WARM_THREADS = min(8, _CPUS // 2) if _CPUS > 8 else 0
BUILDING_MAX_M2 = 2.0e6     # m2: a building footprint larger than this is a mapping error (the largest real ones are
                            # ~1 km2; an OpenStreetMap one of 15.8 km2 roofed a city in Egypt, GitHub #413)
VERSION = 2                     # pipeline version: bump when what a cell's layers contain changes
# per layer, on top of VERSION (a layer's own change without re-fetching the others): roads 2 adds
# the "oneway" rule (Overture access_restrictions by heading)
# roads 3: OpenStreetMap's lanes, width, sidewalk, maxspeed... joined by way id (osmtags.py)
# roads 4: OpenStreetMap's raceways appended (osmtags.raceways, GitHub #143)
# roads 5: a segment from an OSM relation (no way id) takes the tags of the same-named way around it (osmtags.join)
# roads 6: rail segments' rail_flags (is_bridge / is_tunnel) in the "flags" rule (railways.py, mpsteppe's PR #287)
# (osmtags osm_highway, construction/proposed roads left out, GitHub #184: only cells fetched after it; the cached
# ones get it with the next Overture release bump, not a roads 7 here: every player's roads fetched again)
# roads 7: a way without lanes takes the same-named way's (osmtags.join); one-way ends at their own lane (oneway_joins)
LAYER_VERSIONS = {"roads": 7}
# (LidarBC's measured canopy heights, maprasters.bc_canopy, are opt-in: BNE_BC_CANOPY=1 fills cells built after it;
# no canopy version bump, which would fetch every player's canopy cells again)
                                # (2: clipped polygon pieces `own`ed by their cell, exact cell edges,
                                # the exact no-area test of clipped rings)
VECTOR_LAYERS = ("roads", "connectors", "buildings", "water", "land_use", "infrastructure", "building_parts", "places")
RASTER_LAYERS = ("landcover", "canopy")
OPTIONAL_LAYERS = ("building_parts", "places")   # built only when asked for by name (buildings.py, gasstations.py)
LAYERS = tuple(x for x in VECTOR_LAYERS if x not in OPTIONAL_LAYERS) + RASTER_LAYERS
ALL_LAYERS = VECTOR_LAYERS + RASTER_LAYERS
DEFAULT_CACHE = os.path.expanduser("~/.cache/beamng-earth")
# A request that the limiter or a policy refused for a non-demand kind (prefetch() returns False)
DEFERRED = (netfetch.Deferred, netfetch.PolicyDenied, netfetch.BudgetExceeded, netfetch.HostUnavailable,
            netfetch.RateLimited)

# Provenance per source (sources + licences for the credits overlay; exact wordings in CREDITS.md).
WORLDCOVER = {
    "id": "esa-worldcover", "name": "ESA WorldCover 10 m 2021 v200", "release": maprasters.WORLDCOVER_RELEASE,
    "attribution": "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) "
                   "processed by ESA WorldCover consortium",
    "licence": "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)", "url": "https://esa-worldcover.org",
}
CANOPY = {
    "id": "meta-wri-chm", "name": "Meta and WRI High Resolution Canopy Height Maps v2",
    "release": maprasters.CHM_RELEASE,
    "attribution": "Canopy height: Meta and World Resources Institute (WRI), High Resolution Canopy Height Maps",
    "licence": "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)",
    "url": "https://registry.opendata.aws/dataforgood-fb-forests/",
}
LIDAR_CANOPY = {
    "id": "lidarbc-chm", "name": "LidarBC surface and terrain models (canopy height = DSM - DEM), Meta / WRI elsewhere",
    "release": "lidarbc",
    "attribution": "Canopy height: contains information licensed under the Open Government Licence - British Columbia "
                   "(LidarBC); Meta and World Resources Institute (WRI)",
    "licence": "Open Government Licence - British Columbia; CC BY 4.0", "url": "https://lidar.gov.bc.ca",
}
# Short overlay wordings per provenance id (credits.py can take them when the map data is shown)
CREDIT_LINES = {
    "overture": overture.ATTRIBUTION,
    "esa-worldcover": "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) "
                      "processed by ESA WorldCover consortium",
    "meta-wri-chm": "Canopy height: Meta & WRI",
    "lidarbc-chm": "Canopy height: LidarBC (OGL-BC), Meta & WRI",
}
THEME_OF = {name: spec["theme"] for name, spec in overture.LAYERS.items()}


def _source(layer, release):
    if layer in VECTOR_LAYERS:
        return "overture", release
    if layer == "landcover":
        return "worldcover", maprasters.WORLDCOVER_RELEASE
    return "chm", maprasters.CHM_RELEASE


def _key(key):
    """A checked CellKey from a CellKey, a (z, x, y) tuple or 'z/x/y' (no cell outside the grid gets
    built or cached: cells_for_bbox() and the CLI only make valid ones, a caller's arithmetic might not)."""
    return mapcells.parse(key) if isinstance(key, str) else mapcells.check(mapcells.CellKey(*key))


def digest_arrays(arrays):
    """sha256 of named arrays (names, dtypes, shapes and bytes, in sorted name order)."""
    h = hashlib.sha256()
    for k in sorted(arrays):
        a = np.ascontiguousarray(arrays[k])
        h.update(k.encode())
        h.update(a.dtype.str.encode())
        h.update(repr(a.shape).encode())
        h.update(a.tobytes())
    return h.hexdigest()


# The layer files' arrays uncompressed (<cache>/mapdata-mirror/...: one .npy each and a marker), memory-mapped read-only:
# every server process decompressed and kept its own copy of the cells it read (each one's largest allocation after
# the walls, ~90-160 MB, 2026-10-20); a mirrored layer is decompressed once per machine and its pages shared, dropped
# instead of swapped under memory pressure. At most MIRROR_MB on disk (the least recently read go first); the vector
# layers' `en` (the cell frame's metres, Features.localize) is kept with them.
BUILD_WAIT_S = 180.0        # a cell layer another process is building is waited for this long (xproc.once)
MIRROR_MB = 2048
MIRROR_PRUNE_S = 120.0  # the mirror pruned at most this often per process (each prune walks it all)
MIRROR_SMALL = 65536    # bytes: an array this small is read into memory (no mapping)
MEM_MB = 192        # decoded cell layers kept in memory per process (a road tile reads its ~4 cells, each read by
                    # its neighbours too: ~8 ms of decompression and a coordinate transform each time, 2026-10-15).
                    # A road tile's layers of Baltimore's 4 cells are ~124 MB by _nbytes (the infrastructure 65):
                    # at 64 every tile decoded them again, 446 loads for 36 tiles (7.5 s of 87 on a Steam Deck)


def _nbytes(arrays):
    """Memory of a layer's arrays (its decoded form is about this, plus the local coordinates)."""
    n = 0
    for v in arrays.values():
        n += getattr(v, "nbytes", 0)
    return int(n * 1.5) + 1024


@dataclass
class CellData:
    key: mapcells.CellKey
    bounds: tuple                       # (lon0, lat0, lon1, lat1)
    center: tuple                       # (lat, lon): the local frame's origin
    frame: geodesy.EnuFrame
    versions: dict                      # {"pipeline": VERSION, "overture": release, ...}
    layers: dict = field(default_factory=dict)     # name -> Features | Raster | None (no data source)
    provenance: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)      # per layer: seconds, cache hit (not content)

    def digest(self):
        """A hash of the cell's content (every layer present, the versions): equal for equal content."""
        arrays = {"versions": np.array(json.dumps(self.versions, sort_keys=True))}
        for name in sorted(self.layers):
            arrays.update({f"{name}/{k}": v for k, v in _layer_arrays(self.layers[name]).items()})
        return digest_arrays(arrays)

    def credits(self):
        """The provenance ids of the cell's sources, in a fixed order (for credits.py)."""
        ids = []
        for p in self.provenance:
            i = "overture" if p["id"].startswith("overture:") else p["id"]
            if i not in ids:
                ids.append(i)
        return ids

    def credit_line(self):
        return " | ".join(CREDIT_LINES[i] for i in self.credits())


def _layer_arrays(obj):
    """The content arrays of a layer (what the cache stores, minus derived `en`)."""
    if obj is None:
        return {"missing": np.array(True)}
    if isinstance(obj, maprasters.Raster):
        meta = {"crs": obj.crs, "x0": obj.x0, "y0": obj.y0, "dx": obj.dx, "dy": obj.dy,
                "nodata": obj.nodata, "files": obj.files}
        return {"data": obj.data, "meta": np.array(json.dumps(meta, sort_keys=True))}
    a = obj.to_arrays("")
    a.pop("en", None)
    return a


def _raster_from(arrays):
    meta = json.loads(str(arrays["meta"]))
    return maprasters.Raster(arrays["data"], meta["crs"], meta["x0"], meta["y0"], meta["dx"], meta["dy"],
                             meta["nodata"], list(meta["files"]))


class MapData:
    """See the module docstring. fetcher: the server's netfetch.Fetcher (None: one of its own);
    release: the Overture release (overture.RELEASE)."""

    def __init__(self, cache_dir=DEFAULT_CACHE, fetcher=None, release=overture.RELEASE,
                 threads=overture.DUCKDB_THREADS):
        self.cache_dir = cache_dir
        self.fetcher = fetcher if fetcher is not None else netfetch.Fetcher()
        self.release = release
        self.raw_dir = os.path.join(cache_dir, "mapdata", "raw")
        self.overture = overture.Reader(self.raw_dir, self.fetcher, release, threads)
        self.rasters = maprasters.Rasters(self.raw_dir, self.fetcher, os.path.join(cache_dir, "dem", "lidarbc"))
        import railtags                 # (OSM's railway tags joined to the roads layer's rail segments)
        self.rail_tags = railtags.RailTags(cache_dir, self.fetcher) if railtags.ON else None
        import roadz                    # (real road / deck / tunnel heights where a source has them: BNE_ROADZ)
        roadz.install(cache_dir, self.fetcher, self)
        self.lane_tags = railtags.LaneTags(cache_dir, self.fetcher) if railtags.LANES_ON else None   # (markings.py)
        import highwaysigns             # (exit, overhead and mile-marker signs: railtags.SignTags, signstream.py)
        self.highway_signs = highwaysigns.Source(self) if railtags.SIGNS_ON else None
        self.dem = None                 # (gradesep's ground: the server's dem.Dem, set by its owner, else its own)
        self._locks = {}
        self._lock = threading.Lock()
        self.pool = ThreadPoolExecutor(LAYER_THREADS, thread_name_prefix="mapdata-layer")
        self._mem = OrderedDict()           # (cell, layer) -> (decoded layer, provenance, bytes): cell()'s LRU
        self._mem_bytes = 0
        self._mem_lock = threading.Lock()
        self._warm_ex = None
        self._warming = set()               # (cell, layer)s queued or building in warm()

    def close(self):
        self.pool.shutdown(wait=False)
        if self._warm_ex is not None:
            self._warm_ex.shutdown(wait=False, cancel_futures=True)
        self.overture.close()

    def _rails(self, key, layer, feats):
        """The roads layer's rail segments with OSM's railway tags (railtags.py), once per build of them."""
        rt = getattr(self, "rail_tags", None)
        if rt is None or layer != "roads" or feats is None or not len(feats):
            return
        sub = feats.attrs.get("subtype")
        if sub is None or not (np.asarray(sub).astype(str) == "rail").any():
            return                          # (no rails: nothing to ask for)
        built = rt.version()
        if built is None or feats.__dict__.get("_rails_built") == built:
            return
        try:
            rt.join(feats, key)
            feats.__dict__["_rails_built"] = built
        except Exception as e:      # (Overture's classes alone)
            log.debug("railtags join %s: %s: %s", key, type(e).__name__, e)

    def _lanes(self, key, layer, feats):
        """The roads layer's road segments with OSM's lane tags (railtags.LaneTags), once per build of them."""
        lt = getattr(self, "lane_tags", None)
        if lt is None or layer != "roads" or feats is None or not len(feats):
            return
        built = lt.version()
        if built is None or feats.__dict__.get("_lanes_built") == built:
            return
        try:
            lt.join(feats, key)
            feats.__dict__["_lanes_built"] = built
        except Exception as e:      # (no lane data: the markings' guesses)
            log.debug("lanetags join %s: %s: %s", key, type(e).__name__, e)

    def versions(self):
        rails = getattr(self, "rail_tags", None)
        lanes = getattr(self, "lane_tags", None)
        return {"pipeline": VERSION, "overture": self.release, "worldcover": maprasters.WORLDCOVER_RELEASE,
                "canopy": maprasters.CHM_RELEASE, "rails": rails.known() if rails is not None else None,
                "lanes": lanes.known() if lanes is not None else None,
                "gradesep": (gradesep.VERSION, gradesep.MODE) if gradesep.TUNNELS else None}

    def path(self, key, layer):
        source, release = _source(layer, self.release)
        lv = LAYER_VERSIONS.get(layer)
        name = layer if lv is None else f"{layer}.{lv}"
        return os.path.join(self.cache_dir, "mapdata", f"v{VERSION}", source, release, name,
                            str(key.z), str(key.x), f"{key.y}.npz")

    @contextmanager
    def _key_lock(self, key, layer):
        """One builder per (cell, layer) at a time; the entry goes once nobody holds or waits for it
        (a long drive would otherwise keep a lock for every layer of every cell it passed)."""
        k = (key, layer)
        with self._lock:
            entry = self._locks.get(k)
            if entry is None:
                entry = self._locks[k] = [threading.Lock(), 0]
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._lock:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[k]

    # --- one layer -------------------------------------------------------------------------------
    def has(self, key, layer):
        """True when the layer's file exists (not read: cheap enough for the event loop)."""
        return os.path.exists(self.path(key, layer))

    def layer_provenance(self, key, layer):
        """The provenance dict of a cached layer file, None when it is not cached (or has none): only that entry of
        the file is read (np.load is lazy per array), and remembered per file (credits.feature_ids asks every second,
        from the event loop). Never fetches."""
        path = self.path(_key(key), layer)
        memo = self.__dict__.setdefault("_prov_memo", {})
        if path in memo:
            return memo[path]
        p = None
        try:
            with np.load(path, allow_pickle=False) as z:
                if "provenance" in z.files:
                    p = json.loads(str(z["provenance"]))
        except FileNotFoundError:
            return None                 # (not remembered: it may be built later)
        except Exception:
            p = None
        if len(memo) > 4096:
            memo.clear()
        memo[path] = p
        return p

    def _load(self, path):
        """A layer file's arrays, or None when it is missing or unreadable (truncated, a damaged
        deflate stream, another layout): a miss, rebuilt and written again."""
        def read():
            with np.load(path, allow_pickle=False) as z:
                return {k: z[k] for k in z.files}
        try:
            arrays = staging.read_held(read)
        except FileNotFoundError:
            return None
        except Exception as e:      # zipfile.BadZipFile, zlib.error, ValueError, EOFError, OSError...
            log.warning("mapdata: unreadable %s (%s: %s): rebuilding it", path, type(e).__name__, e)
            return None
        if "provenance" not in arrays:
            log.warning("mapdata: %s has no provenance record: rebuilding it", path)
            return None
        return arrays

    def _store(self, path, arrays):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp.npz"
        np.savez_compressed(tmp, **arrays)
        staging.replace_cached(tmp, path)

    def _build(self, key, layer, kind):
        """(content arrays, provenance record or None) of one layer, from the sources."""
        if layer in VECTOR_LAYERS:
            # the roads' OpenStreetMap tags are read alongside Overture's (each half a fresh cell's wait)
            osm = self._osm_pool().submit(self._osm_cell, key, kind) if layer == "roads" else None
            feats, sources = self.overture.query(layer, key, kind)
            if osm is not None:
                feats = self._osm(feats, osm.result())
            ox, oy = mapcells.owner(feats.attrs.get("cx", np.zeros(0)), feats.attrs.get("cy", np.zeros(0)), key.z)
            feats.own = (ox == key.x) & (oy == key.y)
            if feats.clipped is not None:
                # a polygon cut to the cell is this cell's piece of it: each piece is built where it
                # lies (the pieces of all cells make the whole polygon), not only in the bbox-centre cell
                feats.own |= feats.clipped
            arrays = _layer_arrays(feats)
            prov = overture.sources_provenance(THEME_OF[layer], self.release, sources) if len(feats) else None
        elif layer == "landcover":
            r = self.rasters.worldcover(key, kind)
            arrays = _layer_arrays(r)
            prov = dict(WORLDCOVER, files=r.files) if r.files else None
        else:
            r = self.rasters.canopy(key, kind)
            r, bc_files = self.rasters.bc_canopy(key, r)
            arrays = _layer_arrays(r)
            prov = dict(CANOPY, files=r.files) if r is not None else None
            if bc_files:
                prov = dict(LIDAR_CANOPY, files=r.files, also=CANOPY["id"])
        arrays["provenance"] = np.array(json.dumps(prov, sort_keys=True))
        return arrays

    def _osm_pool(self):
        with self._lock:
            if getattr(self, "_osm_ex", None) is None:
                self._osm_ex = ThreadPoolExecutor(OSM_THREADS, thread_name_prefix="osmtags")
            return self._osm_ex

    def _osm_cell(self, key, kind):
        """(tags, raceways) of the cell's OpenStreetMap highways (osmtags.py); none when the source fails (the roads
        keep their width guesses)."""
        import osmtags
        if self.fetcher is None:
            return {}, []
        try:
            with self._lock:
                if getattr(self, "osmtags", None) is None:
                    self.osmtags = osmtags.OsmTags(self.raw_dir, self.fetcher)
            return self.osmtags.cell(key.bounds(), kind)
        except DEFERRED:
            # a prefetch the host had no room for: the cell is not built now (MapData.prefetch: False) rather than cached
            # for good without its tags (a player's start cell in Baltimore kept the roads' guesses, 2026-10-23); on
            # demand the roads go on without them, as below
            if kind not in ("demand", "urgent"):
                raise
            log.warning("mapdata: OSM road tags of %s: the host is busy (%s); the roads go on without them", key, kind)
            return {}, []
        except Exception as e:          # optional: the roads go on with their guesses
            log.warning("mapdata: OSM road tags of %s: %s: %s", key, type(e).__name__, e)
            return {}, []

    @staticmethod
    def _osm(feats, got):
        """The roads with the cell's OpenStreetMap tags joined in and its raceways appended (Overture has none,
        GitHub #143)."""
        import mapgeom
        import osmtags
        tags, ways = got
        if ways:
            feats = mapgeom.concat(feats, osmtags.raceway_features(ways))
        osmtags.join(feats, tags)
        import oneway_joins
        oneway_joins.apply(feats)
        return feats

    def layer_arrays(self, key, layer, kind="demand"):
        """(arrays, hit) of one cell's layer: from the cache, or built and cached."""
        if layer not in ALL_LAYERS:
            raise ValueError(f"unknown map layer {layer!r}; choose from {', '.join(ALL_LAYERS)}")
        key = _key(key)
        path = self.path(key, layer)
        arrays = self._mirror_load(path)
        if arrays is not None:
            return arrays, True
        arrays = self._load(path)
        if arrays is not None:
            return self._mirrored(key, layer, path, arrays), True
        with self._key_lock(key, layer):
            # another thread, or another of the server's processes (xproc), built it meanwhile
            arrays, mine = xproc.once(path + ".lock", lambda: self._load(path), BUILD_WAIT_S, kind)
            if arrays is not None:
                return self._mirrored(key, layer, path, arrays), True
            try:
                arrays = self._build(key, layer, kind)
                self._store(path, arrays)
            finally:
                if mine:
                    xproc.release(path + ".lock")
            return self._mirrored(key, layer, path, arrays), False

    # --- the uncompressed mirror (MIRROR_MB) ---------------------------------------------------
    def _mirror_dir(self, path):
        rel = os.path.relpath(path, os.path.join(self.cache_dir, "mapdata"))
        return os.path.join(self.cache_dir, "mapdata-mirror", rel[:-4])

    @staticmethod
    def _stamp(path):
        st = os.stat(path)
        return f"{st.st_size} {st.st_mtime_ns}"

    def _mirror_load(self, path):
        """The mirrored arrays of a layer file (the large ones memory-mapped), or None (none, or not of this file)."""
        if MIRROR_MB <= 0:
            return None
        d = self._mirror_dir(path)
        try:
            with open(os.path.join(d, "ok")) as fh:
                stamp, names = fh.readline().rstrip("\n"), fh.read().split("\n")
            if stamp != self._stamp(path):
                return None
            out = {}
            for n in names:
                if not n:
                    continue
                f = os.path.join(d, n + ".npy")
                big = os.path.getsize(f) > MIRROR_SMALL
                a = np.load(f, mmap_mode="r" if big else None, allow_pickle=False)
                out[n] = np.asarray(a)          # (a plain view of the mapping: np.memmap's Python hooks ran on every slice)
            os.utime(os.path.join(d, "ok"))         # (read: the last to go)
            return out
        except (OSError, ValueError, EOFError):
            return None

    def _mirrored(self, key, layer, path, arrays):
        """arrays written to the mirror and read back from it (memory-mapped), or as they are when it cannot be."""
        if MIRROR_MB <= 0 or "provenance" not in arrays:
            return arrays
        d = self._mirror_dir(path)
        full = dict(arrays)
        try:
            if layer in VECTOR_LAYERS and "lonlat" in full and "en.local" not in full:
                f = mapgeom.Features.from_arrays(layer, {k: v for k, v in full.items() if k != "provenance"}, "")
                lat_c, lon_c = key.center()
                full["en.local"] = f.localize(geodesy.EnuFrame(lat_c, lon_c, 0.0)).en
            stamp = self._stamp(path)
            tmp = f"{d}.{os.getpid()}.{threading.get_ident()}.tmp"
            os.makedirs(tmp, exist_ok=True)
            size = 0
            for n, a in full.items():
                np.save(os.path.join(tmp, n + ".npy"), np.asarray(a), allow_pickle=False)
                size += np.asarray(a).nbytes
            with open(os.path.join(tmp, "ok"), "w") as fh:
                fh.write(stamp + "\n" + "\n".join(full) + "\n")
            try:
                if os.path.isdir(d):            # (stale: another file's; or another process's, the same)
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                os.rename(tmp, d)
            except OSError:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            self._mirror_note(size)
        except (OSError, ValueError) as e:
            log.debug("mapdata mirror %s: %s", d, e)
            return arrays
        got = self._mirror_load(path)
        return got if got is not None else arrays

    # (a process's first write prunes: the count of a session's own writes alone never reached the threshold when
    # each run wrote less, and the mirror grew past MIRROR_MB across sessions: 6.3 GB on a Steam Deck, 2026-10-23)
    _mirror_written = MIRROR_MB * 1e6
    _mirror_pruned = -1e9
    _mirror_lock = threading.Lock()

    def _mirror_note(self, nbytes):
        """Count what this process wrote; every MIRROR_MB / 8 of it, the mirror cut back to MIRROR_MB."""
        with MapData._mirror_lock:
            MapData._mirror_written += nbytes
            now = time.monotonic()
            # (at most every MIRROR_PRUNE_S: each prune walks the whole mirror, as dem.Dem's)
            if MapData._mirror_written < MIRROR_MB * 1e6 / 8 or now - MapData._mirror_pruned < MIRROR_PRUNE_S:
                return
            MapData._mirror_written, MapData._mirror_pruned = 0, now
        # (beside the write: the walk stat()s every file, 0.5-1 s on a desktop, in the block build that wrote)
        threading.Thread(target=self._mirror_prune, daemon=True, name="mapdata-mirror-prune").start()

    def _mirror_prune(self):
        prune_mirror(self.cache_dir)

    # --- cells -----------------------------------------------------------------------------------
    def cell(self, key, layers=LAYERS, kind="demand"):
        """CellData of one cell with the given layers (fetched / built / cached as needed)."""
        key = _key(key)
        lat_c, lon_c = key.center()
        frame = geodesy.EnuFrame(lat_c, lon_c, 0.0)
        cd = CellData(key, key.bounds(), (lat_c, lon_c), frame, self.versions())
        provs = {}

        def timed(layer):
            t0 = time.perf_counter()
            arrays, hit = self.layer_arrays(key, layer, kind)
            return arrays, hit, time.perf_counter() - t0
        mem = {}
        with self._mem_lock:
            for layer in layers:
                got = self._mem.get((str(key), layer))
                if got is not None:
                    self._mem.move_to_end((str(key), layer))
                    mem[layer] = got
        futs = [(layer, self.pool.submit(timed, layer)) for layer in layers if layer not in mem]
        results = {}
        err = None
        for layer, fut in futs:             # all finish before an error is raised (no stray fetches)
            try:
                results[layer] = fut.result()
            except Exception as e:
                err = err or e
        if err is not None:
            revisit.fault()                 # (a build that goes on without it is not kept)
            raise err
        for layer in layers:                # assembled in the caller's order, whatever finished first
            if layer in mem:                # decoded before: shared (cell layers are never changed after loading)
                cd.layers[layer], prov, _ = mem[layer]
                dt, hit = 0.0, True
                self._rails(key, layer, cd.layers[layer])
                self._lanes(key, layer, cd.layers[layer])
            else:
                arrays, hit, dt = results[layer]
                arrays = dict(arrays)
                prov = json.loads(str(arrays.pop("provenance")))
                en = arrays.pop("en.local", None)     # (the mirror's: this frame's, Features.localize)
                if layer in VECTOR_LAYERS:
                    f = mapgeom.Features.from_arrays(layer, arrays, "")
                    if en is not None and len(en) == len(f.lonlat):
                        f.en = en
                        cd.layers[layer] = f
                    else:
                        cd.layers[layer] = f.localize(frame)
                    # (which cell's layer: the per-cell facts the server's processes share, roads.cell_facts)
                    self._rails(key, layer, cd.layers[layer])
                    self._lanes(key, layer, cd.layers[layer])
                    if layer == "buildings":    # (a "building" of a whole city's size: left out, GitHub #413)
                        cd.layers[layer] = mapgeom.drop_huge(cd.layers[layer], BUILDING_MAX_M2)
                    if layer == "roads":        # (the bridges the map lacks, found when its ranges are first asked)
                        cd.layers[layer].__dict__["_bnge_gradesep"] = gradesep.hook(self, key)
                    cd.layers[layer].__dict__["_bnge_cell"] = (str(key), layer, json.dumps(self.versions(),
                                                                                           sort_keys=True))
                elif "missing" in arrays:
                    cd.layers[layer] = None
                else:
                    cd.layers[layer] = _raster_from(arrays)
                self._remember((str(key), layer), (cd.layers[layer], prov, _nbytes(arrays)))
            if prov:
                pid = prov["id"]
                if pid in provs:            # roads + connectors: one transportation record
                    merged = {(d["dataset"], d["license"]): d["features"] for d in provs[pid]["datasets"]}
                    for d in prov.get("datasets", ()):
                        k = (d["dataset"], d["license"])
                        merged[k] = merged.get(k, 0) + d["features"]
                    provs[pid]["datasets"] = [{"dataset": a, "license": b, "features": n}
                                              for (a, b), n in sorted(merged.items())]
                else:
                    provs[pid] = prov
            cd.stats[layer] = {"s": dt, "hit": hit}
        cd.provenance = [provs[k] for k in sorted(provs)]
        return cd

    def _remember(self, k, entry):
        """Keep a decoded layer for the next cell() of it (at most MEM_MB in all, least recently used out)."""
        with self._mem_lock:
            old = self._mem.pop(k, None)
            if old is not None:
                self._mem_bytes -= old[2]
            self._mem[k] = entry
            self._mem_bytes += entry[2]
            while self._mem_bytes > MEM_MB * 1e6 and len(self._mem) > 1:
                _, (_, _, n) = self._mem.popitem(last=False)
                self._mem_bytes -= n

    def warm(self, needs, kind="demand"):
        """Build the (cell, layer)s of needs into the cache ahead of the cell() calls that will ask for them, in their
        order, WARM_THREADS at once; returns at once with how many were queued. Those cached, in memory or already
        queued are skipped. A cell() of one meanwhile waits for its build (the cell's lock) or builds it itself."""
        if WARM_THREADS <= 0:
            return 0
        todo = []
        for key, layer in needs:
            key = _key(key)
            k = (str(key), layer)
            with self._mem_lock:
                if k in self._mem:
                    continue
            with self._lock:
                if k in self._warming:
                    continue
            if self.has(key, layer):
                continue
            with self._lock:
                if k in self._warming:
                    continue
                self._warming.add(k)
                if self._warm_ex is None:
                    self._warm_ex = ThreadPoolExecutor(WARM_THREADS, thread_name_prefix="mapdata-warm")
            todo.append((k, key, layer))

        def run(k, key, layer):
            try:
                self.layer_arrays(key, layer, kind)
            except Exception as e:      # (the caller's own cell() of it raises, or builds it again)
                log.debug("mapdata warm %s %s: %s: %s", key, layer, type(e).__name__, e)
            finally:
                with self._lock:
                    self._warming.discard(k)
        for k, key, layer in todo:
            self._warm_ex.submit(run, k, key, layer)
        return len(todo)

    def predownload(self, key, layer):
        """A saved area's cell layer (areas.py): built from its sources at netfetch "predownload" and stored, unless it
        is on disk; not loaded or mirrored (the session's cells keep the mirror). True when built now. Raises the
        sources' netfetch errors."""
        key = _key(key)
        path = self.path(key, layer)
        if os.path.exists(path):
            return False
        with self._key_lock(key, layer):
            got, mine = xproc.once(path + ".lock", lambda: path if os.path.exists(path) else None, BUILD_WAIT_S,
                                   "predownload")
            if got is not None:
                return False
            try:
                self._store(path, self._build(key, layer, "predownload"))
            finally:
                if mine:
                    xproc.release(path + ".lock")
        return True

    def cell_at(self, lat, lon, layers=LAYERS, kind="demand"):
        return self.cell(mapcells.cell_of(lat, lon), layers, kind)

    def prefetch(self, key, layers=LAYERS, kind="prefetch"):
        """Build and cache a cell ahead of need; False when the limiter or a policy refused (the
        layer is then built on demand later)."""
        try:
            for layer in layers:
                self.layer_arrays(key, layer, kind)
            return True
        except DEFERRED:
            return False



def prune_mirror(cache_dir):
    """The uncompressed mirror cut back to 80 % of MIRROR_MB when over it, the least recently written folders first
    (a process's writes, and earthd at start: a session that only reads it never pruned it)."""
    root = os.path.join(cache_dir, "mapdata-mirror")
    entries, total = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        if "ok" in filenames:
            try:
                n = sum(os.path.getsize(os.path.join(dirpath, f)) for f in filenames)
                entries.append((os.path.getmtime(os.path.join(dirpath, "ok")), n, dirpath))
                total += n
            except OSError:
                pass
            dirnames[:] = []
    if total <= MIRROR_MB * 1e6:
        return 0
    import shutil
    removed = 0
    for _, n, dpath in sorted(entries):
        shutil.rmtree(dpath, ignore_errors=True)
        total -= n
        removed += 1
        if total <= 0.8 * MIRROR_MB * 1e6:
            break
    return removed

# --- CLI ------------------------------------------------------------------------------------------
LANDCOVER_RGB = {0: (255, 255, 255), 10: (0, 100, 0), 20: (255, 187, 34), 30: (255, 255, 76), 40: (240, 150, 255),
                 50: (250, 0, 0), 60: (180, 180, 180), 70: (240, 240, 240), 80: (0, 100, 200), 90: (0, 150, 160),
                 95: (0, 207, 117), 100: (250, 230, 160)}   # the WorldCover legend's colours
ROAD_STYLE = {  # class -> (width px, rgb)
    "motorway": (6, (200, 60, 40)), "trunk": (5, (220, 110, 40)), "primary": (4, (240, 150, 40)),
    "secondary": (4, (240, 200, 60)), "tertiary": (3, (250, 240, 120)), "residential": (2, (255, 255, 255)),
    "unclassified": (2, (240, 240, 240)), "living_street": (2, (240, 240, 240)), "service": (1, (230, 230, 230)),
    "track": (2, (150, 100, 40)), "unknown": (1, (200, 200, 200))}
PATH_STYLE = (1, (120, 120, 120))
RAIL_STYLE = (2, (60, 60, 60))


def _hist(values, top=12):
    """'a 12, b 3, ...': the most common values (ties by value)."""
    vals, counts = np.unique(np.asarray(values), return_counts=True)
    order = np.lexsort((vals.astype(str), -counts))
    more = f", ... ({len(vals) - top} more)" if len(vals) > top else ""
    return ", ".join(f"{str(vals[i]) or '(none)'} {int(counts[i])}" for i in order[:top]) + more


def summarize(cd):
    """A JSON-able summary of a cell (the --dump output)."""
    out = {"key": str(cd.key), "quadkey": cd.key.quadkey(), "bounds": [round(v, 7) for v in cd.bounds],
           "size_m": [round(v) for v in cd.key.size_m()], "versions": cd.versions, "digest": cd.digest(),
           "layers": {}, "provenance": cd.provenance, "credits": cd.credit_line()}
    for name, obj in cd.layers.items():
        s = {"s": round(cd.stats[name]["s"], 3), "cached": cd.stats[name]["hit"]}
        if obj is None:
            s["missing"] = True
        elif isinstance(obj, maprasters.Raster):
            d = obj.data
            s["shape"] = list(d.shape)
            if name == "landcover":
                tot = d.size
                s["classes_pct"] = {f"{int(v)} {maprasters.CLASSES.get(int(v), '?')}": round(100.0 * c / tot, 1)
                                    for v, c in zip(*np.unique(d, return_counts=True))}
            else:
                valid = d[d != obj.nodata] if obj.nodata is not None else d.ravel()
                s["max_m"] = int(valid.max()) if valid.size else None
                s["pct_over_3m"] = round(100.0 * float((valid > 3).mean()), 1) if valid.size else None
                s["pct_over_10m"] = round(100.0 * float((valid > 10).mean()), 1) if valid.size else None
                s["nodata_px"] = int((d == obj.nodata).sum()) if obj.nodata is not None else 0
        else:
            s["features"] = len(obj)
            s["owned"] = int(obj.own.sum()) if obj.own is not None else None
            s["coords"] = obj.n_coords
            a = obj.attrs
            if name == "roads":
                s["subtype"] = _hist(a["subtype"])
                s["class"] = _hist(a["class"], 20)
                s["subclass"] = _hist(a["subclass"])
                for r in ("surface", "flags", "width", "level", "max_speed_kmh"):
                    rt = obj.rules[r]
                    s[f"rule_{r}"] = {"rows": len(rt), "features": int(len(np.unique(rt.feature)))}
                s["surface_values"] = _hist(obj.rules["surface"].value)
                s["flag_values"] = _hist(obj.rules["flags"].value)
            elif name == "buildings":
                h, nf = ~np.isnan(a["height"]), ~np.isnan(a["num_floors"])
                n = max(1, len(obj))
                s["height_pct"] = round(100.0 * h.sum() / n, 1)
                s["num_floors_pct"] = round(100.0 * nf.sum() / n, 1)
                s["height_or_floors_pct"] = round(100.0 * (h | nf).sum() / n, 1)
                s["roof_shape_pct"] = round(100.0 * (a["roof_shape"] != "").sum() / n, 1)
                s["height_m_median"] = round(float(np.median(a["height"][h])), 1) if h.any() else None
                s["height_m_max"] = round(float(a["height"][h].max()), 1) if h.any() else None
                s["class"] = _hist(a["class"])
                s["source"] = _hist(a["src_dataset"])
                s["height_source"] = _hist(a["height_src"][h]) if h.any() else ""
            elif name in ("water", "land_use", "infrastructure"):
                s["subtype_class"] = _hist(np.char.add(np.char.add(a["subtype"], "/"), a["class"]), 20)
                if obj.clipped is not None:
                    s["clipped"] = int(obj.clipped.sum())
        out["layers"][name] = s
    return out


def _px(lonlat, x0, y0, scale, z):
    x, y = mapcells.tile_xy(lonlat[:, 1], lonlat[:, 0], z)
    return np.stack([(x - x0) * scale, (y - y0) * scale], axis=1)


def preview(cells, path, px_per_cell=1024):
    """A PNG of the cells (a square block, row-major): left land cover + water + land use +
    infrastructure + buildings + roads, right canopy height + building outlines."""
    from PIL import Image, ImageDraw
    xs = sorted({c.key.x for c in cells})
    ys = sorted({c.key.y for c in cells})
    z = cells[0].key.z
    nx, ny = len(xs), len(ys)
    W, H = nx * px_per_cell, ny * px_per_cell
    x0, y0 = xs[0], ys[0]
    scale = px_per_cell
    # pixel centres -> lat/lon / mercator metres
    jj, ii = np.mgrid[0:H, 0:W]
    tx = x0 + (ii + 0.5) / scale
    ty = y0 + (jj + 0.5) / scale
    n = 2.0 ** z
    lon = tx / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * ty / n))))
    mx = (tx / n - 0.5) * 2 * maprasters.MERC_HALF
    my = (0.5 - ty / n) * 2 * maprasters.MERC_HALF
    lc_img = np.full((H, W, 3), 255, np.uint8)
    chm_img = np.zeros((H, W), np.float32)
    lut = np.zeros((256, 3), np.uint8)
    for k, rgb in LANDCOVER_RGB.items():
        lut[k] = rgb
    for c in cells:
        cx, cy = (c.key.x - x0) * scale, (c.key.y - y0) * scale
        sl = (slice(cy, cy + scale), slice(cx, cx + scale))
        lc = c.layers.get("landcover")
        if lc is not None:
            lc_img[sl] = lut[lc.sample(lon[sl], lat[sl])]
        ch = c.layers.get("canopy")
        if ch is not None:
            v = ch.sample(mx[sl], my[sl]).astype(np.float32)
            v[v == ch.nodata] = 0
            chm_img[sl] = v
    base = (0.45 * lc_img + 0.55 * 255).astype(np.uint8)          # muted land cover
    left = Image.fromarray(base, "RGB")
    g = np.clip(chm_img / 30.0, 0, 1)
    right_arr = np.stack([(1 - g) * 245, 245 - g * 120, (1 - g) * 235], -1).astype(np.uint8)
    right = Image.fromarray(right_arr, "RGB")
    dl, dr = ImageDraw.Draw(left), ImageDraw.Draw(right)

    def rings_of(f):
        xy = _px(f.lonlat, x0, y0, scale, z)
        return lambda i: f.parts(i, xy)

    for c in cells:
        f = c.layers.get("land_use")
        if f is not None:
            rings = rings_of(f)
            for i in range(len(f)):
                for part in rings(i):
                    dl.line([tuple(p) for p in part[0]], fill=(130, 110, 60), width=1)
        f = c.layers.get("water")
        if f is not None:
            rings = rings_of(f)
            for i in range(len(f)):
                for part in rings(i):
                    if f.kind[i] == mapgeom.POLYGON:
                        dl.polygon([tuple(p) for p in part[0]], fill=(120, 170, 230))
                        for hole in part[1:]:
                            dl.polygon([tuple(p) for p in hole], fill=(240, 240, 240))
                    else:
                        dl.line([tuple(p) for p in part[0]], fill=(60, 120, 220), width=2)
        f = c.layers.get("infrastructure")
        if f is not None:
            rings = rings_of(f)
            for i in range(len(f)):
                for part in rings(i):
                    pts = [tuple(p) for p in part[0]]
                    if f.kind[i] == mapgeom.POINT:
                        x, y = pts[0]
                        dl.rectangle([x - 1, y - 1, x + 1, y + 1], fill=(120, 0, 160))
                    else:
                        dl.line(pts + ([pts[0]] if f.kind[i] == mapgeom.POLYGON else []), fill=(120, 0, 160), width=1)
        f = c.layers.get("buildings")
        if f is not None:
            rings = rings_of(f)
            hgt = f.attrs["height"]
            for i in range(len(f)):
                if not f.own[i]:
                    continue
                h = hgt[i]
                fill = (110, 110, 110) if np.isnan(h) else (int(min(255, 150 + 4 * h)), 90, 40)
                for part in rings(i):
                    pts = [tuple(p) for p in part[0]]
                    if len(pts) >= 3:
                        dl.polygon(pts, fill=fill, outline=(40, 40, 40))
                        dr.polygon(pts, outline=(90, 90, 90))
        f = c.layers.get("roads")
        if f is not None:
            rings = rings_of(f)
            cls, sub = f.attrs["class"], f.attrs["subtype"]
            order = sorted(range(len(f)), key=lambda i: ROAD_STYLE.get(cls[i], PATH_STYLE)[0])
            for i in order:
                if not f.own[i]:
                    continue
                w, rgb = RAIL_STYLE if sub[i] == "rail" else ROAD_STYLE.get(cls[i], PATH_STYLE)
                for part in rings(i):
                    pts = [tuple(p) for p in part[0]]
                    dl.line(pts, fill=(60, 60, 60), width=w + 2)
                    dl.line(pts, fill=rgb, width=w)
                    dr.line(pts, fill=(80, 80, 80), width=1)
    img = Image.new("RGB", (2 * W + 8, H), (0, 0, 0))
    img.paste(left, (0, 0))
    img.paste(right, (W + 8, 0))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path)
    return path


def _net_delta(before, after):
    out = {}
    for host, a in after.items():
        b = before.get(host, {})
        req = a.get("requests", 0) - b.get("requests", 0)
        if req:
            out[host] = {"requests": req, "MB": round((a.get("bytes", 0) - b.get("bytes", 0)) / 1e6, 2)}
    return out


def _dir_size(path):
    total = files = 0
    for root, _, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
                files += 1
            except OSError:
                pass
    return total, files


def main(argv=None):
    ap = argparse.ArgumentParser(description="BeamNG.Earth map data per cell (development tool)")
    ap.add_argument("--cell", help="lat,lon: the z14 cell containing this point")
    ap.add_argument("--key", help="z/x/y: a cell key")
    ap.add_argument("--around", type=int, default=0, help="also the cells within this many cells (a square block)")
    ap.add_argument("--layers", default=",".join(LAYERS), help=f"comma-separated, from {', '.join(LAYERS)}")
    ap.add_argument("--dump", action="store_true", help="print counts, timings and sizes (JSON)")
    ap.add_argument("--png", help="write a preview PNG of the cell(s) here")
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--release", default=overture.RELEASE, help="Overture release (default: the pinned one)")
    ap.add_argument("--offline", action="store_true", help="disk cache only")
    ap.add_argument("--latest", action="store_true", help="print the newest Overture release and exit")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    fetcher = netfetch.Fetcher(offline=a.offline, stats_dir=os.path.join(a.cache_dir, "netstats"))
    md = None
    try:
        if a.latest:
            latest = overture.latest_release(fetcher)
            print(json.dumps({"pinned": overture.RELEASE, "latest": latest,
                              "update": "set overture.RELEASE (server/overture.py) to the latest id; cells "
                                        "are cached per release, so nothing else changes"}))
            return 0
        if a.key:
            center = mapcells.parse(a.key)
        elif a.cell:
            lat, lon = (float(v) for v in a.cell.split(","))
            center = mapcells.cell_of(lat, lon)
        else:
            ap.error("--cell or --key is needed")
        layers = tuple(s.strip() for s in a.layers.split(",") if s.strip())
        md = MapData(a.cache_dir, fetcher, a.release)
        r = a.around
        keys = [mapcells.CellKey(center.z, (center.x + dx) % center.n, center.y + dy)
                for dy in range(-r, r + 1) for dx in range(-r, r + 1) if 0 <= center.y + dy < center.n]
        cells, report = [], []
        for k in keys:
            before = fetcher.snapshot()
            t0 = time.perf_counter()
            cd = md.cell(k, layers)
            dt = time.perf_counter() - t0
            cells.append(cd)
            if a.dump:
                s = summarize(cd)
                s["seconds"] = round(dt, 2)
                s["net"] = _net_delta(before, fetcher.snapshot())
                s["cache_bytes"] = {layer: os.path.getsize(md.path(k, layer)) for layer in layers}
                report.append(s)
        if a.dump:
            raw, nraw = _dir_size(md.raw_dir)
            print(json.dumps({"cells": report, "raw_cache": {"MB": round(raw / 1e6, 1), "files": nraw}},
                             indent=1, ensure_ascii=False))
        if a.png:
            t0 = time.perf_counter()
            preview(cells, a.png)
            print(f"preview: {a.png} ({time.perf_counter() - t0:.1f} s)", file=sys.stderr)
        return 0
    except (overture.OvertureError, rangeread.RangeError, netfetch.FetchError) as e:
        print(f"mapdata: {e}", file=sys.stderr)
        return 1
    finally:
        if md is not None:
            md.close()
        fetcher.close()


if __name__ == "__main__":
    sys.exit(main())
