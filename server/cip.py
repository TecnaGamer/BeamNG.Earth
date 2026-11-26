"""CIP: architecture v2's compact per-cell inputs (~/bnge-tests/archv2/DESIGN.md, phase V2). One file per z14 map cell
holds what the server's sources give for that cell, prebuilt per input release by tools/build_cip.py and served from
the tile host, so a fresh place's map input is a range read or two (~0.1-2 MB a cell) instead of Overture's tiles and
parquet row groups, the OSM tag shards, Layercake, the 3DEP point clouds and the weekly Pages shards (a cold NYC visit
pulled ~900 MB of those).

    BNE_V2_CIP=on               read the cells (off, the default: today's sources, nothing here runs)
    BNE_V2_CIP_URL=<base>       where the builds are (http(s), file:// or a folder; default the tile host)
    BNE_V2_CIP_BUILD=<id>       which build (default: <base>/cip<FORMAT>/<release>/latest.json's "build")

What a cell holds (sections, native codec earthfast cip.rs: lossless, every array back with its dtype, shape and bytes):
  m:<layer>        a map layer's cell file as mapdata.MapData._build makes it (Overture through the source the build
                   names, `own`, the provenance record; the rasters' pixels). The roads hold no OpenStreetMap tags
                   (mapdata roads 10): the reader joins them at the decode from its own run's tag shard build, as for
                   every cell, so a build never brings another week's tags (CONTENT 2; a CONTENT 1 build, tags baked
                   in, is not read: its roads.9 sections are other code's, code_inputs)
  t:rail t:lanes   the weekly rail / lane tag shards' slice a join of the cell reads (railtags: the cell's ways' tags,
  t:stations       its nodes in the joins' pad; the stations whose point is in the cell), shard order kept
  z:<src> z:<src>.gaps   the lidar road heights' answer files (roadz: the cell's Lines arrays and its gaps JSON)
  f:osmfuel f:picnic     OpenStreetMap's fuel stations / picnic tables of the cell (the per-cell JSON the server keeps)
  q                the cell's data-quality record's data part (quality.py: made from the 3 x 3 cells' layers by the
                   server's own kernel; its first byte names its version, a reader of another makes it again)
and per build, beside the cells: airports.csv / runways.csv (OurAirports, the snapshot the build used).

Every one is what the server would have made itself from the same inputs, bit for bit (tools/build_cip.py makes them
with the server's own code), so nothing built from a CIP cell has a cache key of its own: the map layer's file, the
plans, facts and tiles are the ones the sources give. What the build read is its manifest's "inputs" (inputs()): the
code-made parts (formats, versions, the Overture release, the map source) must equal this server's own or the build
is not used; the weekly data builds it pinned (rail, lanes, stations) are the ones this server then reports
(MapData.versions()), for every cell, CIP or not: no cell keyed by one build mixes with another's. The OSM road tags'
build it pinned only names what its "q" records were counted with (quality.Source reads them while the run's build is
that one); the roads' tags are the run's own (above).

A cell the build has no section for (outside its coverage, a layer it lacks), a build that cannot be read (404,
network, another format) or a damaged cell: the source as before (the fallback is per section, logged once a cell).

Archive: PMTiles v3 parts of at most PART_MB (Cloudflare caches whole files; a cold MISS of a 400 MB part took ~9 s),
the BNWT container (bnwt.py: Hilbert-ordered, clustered, every quadtree square one byte range, identical cells kept
once), under <base>/cip<FORMAT>/<release>/<build>/cells.index.json; cached under <raw>/cip/<release>/<build>/.
"""
import gzip
import hashlib
import io
import json
import logging
import os
import threading
import time

import numpy as np

import bnwt
import fast
import mapcells
import netfetch
import pmtiles

log = logging.getLogger("earthd.cip")

NATIVE = 604
FORMAT = 1                 # the cell codec and section names (earthfast cip.rs FORMAT)
# What the sections hold: bump with any change of what a section is made from or how (the builder's reads, a slice's
# rule): builds of another content are not read. (The map layers' own content is keyed by mapdata.VERSION,
# LAYER_VERSIONS and the map source, as their cache files are.)
CONTENT = 2                # (2: the roads' sections without OpenStreetMap's tags: joined by the reader, mapdata roads 10)
ZOOM = 14
ARCHIVE = "cells"
PART_MB = 32               # a part's data at most: a CDN edge caches it whole on the first read
LEVEL = 17                 # zstd: the size of 19 (within 1 %) at 2-3x its encode speed (a dense Manhattan cell: 0.24 s)
ON = os.environ.get("BNE_V2_CIP", "off").strip().lower() == "on"
BASE_URL = (os.environ.get("BNE_V2_CIP_URL") or "").strip().rstrip("/")
BUILD = (os.environ.get("BNE_V2_CIP_BUILD") or "").strip()
WEEKLY = ("osmtags", "rails", "lanes", "stations")   # data builds a build pins (inputs()["data"])
# section order in a cell: what most reads want first (a partial decode stops at the end of the section asked for; the
# quality record's ~360 bytes first); the connectors before the roads, whose connector references are coded as indexes into them (cip.rs 'X')
ORDER = ("q", "m:connectors", "m:roads", "m:road_routes", "m:water", "m:land_use", "m:infrastructure", "m:buildings",
         "m:building_parts", "m:town_areas", "m:town_points", "m:hoods", "t:rail", "t:lanes", "t:stations", "z:lidar",
         "z:lidar.gaps", "z:lidar-nogaps", "z:hk3", "z:ign", "z:nvdb", "f:osmfuel", "f:picnic", "m:landcover",
         "m:places", "m:canopy", "m:land", "m:bathymetry")   # (the natural layers last: natural.py)
ROADZ_SRCS = ("lidar", "lidar-nogaps", "hk3", "ign", "nvdb")
DECODED_MB = 96            # decoded sections kept per process (MapData keeps its own decoded layers: these are the
                           # side reads, slices, lidar answers, rows, and layers between their reads)
LANE = "cip"                # netfetch's lane of the build's host (its policy: the CDN's, given up quickly)


def enabled():
    return ON


def base_url():
    if BASE_URL:
        return BASE_URL
    import imagery
    return imagery.CDN_BASE


# --- what a build read ------------------------------------------------------------------------------------------------
def code_inputs(release):
    """The code-made part of a build's inputs: equal to the reader's own, or the build is not read (a section is only
    the server's own output when the server would make it so)."""
    import dem
    import lidarz
    import maprasters
    import mapdata
    import osmtags
    import roads
    return {"cip": [FORMAT, CONTENT], "pipeline": mapdata.VERSION, "layer_versions": dict(mapdata.LAYER_VERSIONS),
            "overture": release, "mapsrc": pmtiles.source_key() if mapdata.MAP_SOURCE == "pmtiles" else "parquet",
            "pmtiles": pmtiles.FORMAT, "worldcover": maprasters.WORLDCOVER_RELEASE, "canopy": maprasters.CHM_RELEASE,
            "osm_columns": list(osmtags.COLUMNS), "osm_poi": mapdata.OSM_POI_VERSION,
            "lidar": lidarz.lkey_doc(), "lidar_gaps": bool(roads.LIDAR_GAPS_ON), "dem": [dem.ELEVATION_ID]}


def build_id(inputs):
    """A build's id: the hash of its whole inputs document (code and data parts): a build never changes in place."""
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


# --- the codec ----------------------------------------------------------------------------------------------------------
def arrays_section(arrays):
    """An 'A' section's payload from {name: array} (in its order)."""
    out = []
    for k, v in arrays.items():
        a = np.asarray(v)
        if not a.flags.c_contiguous:        # (np.ascontiguousarray would make a 0-d array 1-d)
            a = a.copy(order="C")
        if a.dtype.hasobject:
            raise ValueError(f"{k}: object array")
        out.append((k, a.dtype.str, tuple(int(d) for d in a.shape), a.tobytes()))
    return out


def encode_cell(key, sections, level=LEVEL):
    """Cell key's file from {name: {array name: array} | bytes} (sections in ORDER, then the rest by name)."""
    fast.require(NATIVE, "cip")
    rank = {n: i for i, n in enumerate(ORDER)}

    def order(n):           # (a map layer's section by its layer: "m:roads.9" is "m:roads")
        base = n.split(".", 1)[0] if n.startswith("m:") else n
        return rank.get(n, rank.get(base, len(rank))), n
    secs = []
    for name in sorted(sections, key=order):
        v = sections[name]
        if isinstance(v, (bytes, bytearray)):
            secs.append((name, "B", bytes(v)))
        else:
            secs.append((name, "A", arrays_section(v)))
    return fast._ef.cip_encode_cell(secs, tuple(mapcells.check(key).bounds()), level)


def decode_cell(key, cell, names=None):
    """{name: {array name: array (read-only, a view of one buffer)} | bytes} of cell key's file (names: those only)."""
    fast.require(NATIVE, "cip")
    out = {}
    for item in fast._ef.cip_decode(cell, tuple(mapcells.check(key).bounds()),
                                    None if names is None else list(names)):
        if item[1] == "B":
            out[item[0]] = item[2]
            continue
        data, arrs = item[2], {}
        for name, dt, shape, off, nb in item[3]:
            dt = np.dtype(dt)
            n = nb // dt.itemsize if dt.itemsize else 0
            shape = tuple(int(d) for d in shape)
            arrs[name] = np.frombuffer(data, dt, n, off).reshape(shape) if n else np.zeros(shape, dt)
        out[item[0]] = arrs
    return out


def cell_sections(cell):
    """[(name, codec, raw bytes)] of a cell file."""
    return fast._ef.cip_cell_info(cell)[1]


# --- the archive ---------------------------------------------------------------------------------------------------------
class Archive(bnwt.Archive):
    """A build's cell archive (bnwt.Archive's index, parts and tile cache; its own names, lane and checks)."""

    LANE = LANE

    def __init__(self, reader, name=ARCHIVE):
        self.r, self.name = reader, name
        self.dir_url = f"{reader.base}/cip{FORMAT}/{reader.release}/{reader.build}"
        self.url = f"{self.dir_url}/{name}.index.json"
        self.dir = os.path.join(reader.root, reader.release, reader.build, name)
        self.meta = None
        self._cover = []
        self._parts = []
        self._firsts = []
        self._down = None
        self._lock = threading.Lock()

    def _tile_path(self, z, x, y):
        return os.path.join(self.dir, "tiles", str(z), str(x), f"{y}.cip")

    def _check(self, m):
        want = {"format": "cip", "version": FORMAT, "release": self.r.release, "build": self.r.build,
                "archive": self.name}
        bad = {k: m.get(k) for k, v in want.items() if m.get(k) != v}
        if bad:
            raise bnwt.Unavailable(f"{self.url}: {bad}, {want} wanted")
        mine = code_inputs(self.r.release)
        theirs = (m.get("inputs") or {}).get("code") or {}
        diff = sorted(k for k in set(mine) | set(theirs) if mine.get(k) != theirs.get(k))
        if diff:
            raise bnwt.Unavailable(f"{self.url}: built by other code ({', '.join(diff)} differ)")

    def has_layer(self, layer):
        return True


class Reader(bnwt.Reader):
    """The CIP cells of one build (BASE_URL / BUILD, or latest.json's), with the BNWT reader's fetching: a cell not on
    disk is read alone (its own byte range: a fresh place's first cell comes first), prefetch() reads the ring's cells
    as the few contiguous runs they are."""

    BLOCK = ZOOM            # (bnwt's 4 x 4 block of a cold cell: ~24 MB in Midtown with every section)

    def __init__(self, raw_dir, fetcher, release, base=None, build=None):
        fast.require(NATIVE, "cip")
        self.base = (base or base_url()).rstrip("/")
        self.root = os.path.join(raw_dir, "cip")
        self.fetcher, self.release = fetcher, release
        self.build = build or BUILD or None
        self._arch = {}
        self._lock = threading.Lock()
        self._mem = pmtiles.OrderedDict()
        self._mem_bytes = 0
        from concurrent.futures import ThreadPoolExecutor
        self.pool = ThreadPoolExecutor(pmtiles.FETCH_THREADS, thread_name_prefix="cip-fetch")
        self.stats = dict(cells=0, fallbacks=0, decode_s=0.0, sections=0)
        self._inflight = {}
        self._down = None
        self._dec = pmtiles.OrderedDict()   # (cell, section) -> decoded section (DECODED_MB)
        self._dec_sizes = {}
        self._dec_bytes = 0
        self._decoding = {}                 # (cell, section) -> Event of the decode under way
        self._dec_lock = threading.Lock()
        self._noted = set()
        self._heads = {}                    # cell -> its tile's section names (has_section)

    def _resolve(self, kind):
        """The build in use (BNE_V2_CIP_BUILD, else latest.json of the release), or Unavailable."""
        if self.build:
            return self.build
        down = self._down
        if down is not None and time.monotonic() - down < bnwt.RETRY_S:
            raise bnwt.Unavailable("no CIP build known")
        rel = f"{self.base}/cip{FORMAT}/{self.release}/latest.json"
        try:
            lp = bnwt._local_path(rel)
            if lp is not None:
                with open(lp, "rb") as f:
                    b = f.read()
            else:
                b = self.fetcher.get(rel, kind=kind, lane=LANE).body
            got = json.loads(b)
            build = str(got["build"])
        except Exception as e:      # (any: the sources as before)
            self._down = time.monotonic()
            raise bnwt.Unavailable(f"{rel}: {type(e).__name__}: {e}") from e
        self.build = build
        return build

    def archive(self, name=ARCHIVE):
        with self._lock:
            a = self._arch.get(name)
        if a is None:
            self._resolve("demand")
            with self._lock:
                a = self._arch.get(name)
                if a is None:
                    a = self._arch[name] = Archive(self, name)
        return a

    def manifest(self, kind="demand"):
        """The build's index (its "inputs": code, data builds), or None when it cannot be read."""
        try:
            return self.archive().open(kind).meta
        except bnwt.Unavailable:
            return None

    def data_builds(self):
        """{name: build} of the weekly data the build pinned (WEEKLY), {} when no build can be read."""
        m = self.manifest()
        return dict(((m or {}).get("inputs") or {}).get("data") or {})

    def _cell_bytes(self, key, kind):
        arch = self.archive().open(kind)
        k = (ZOOM, key.x, key.y)
        if key.z != ZOOM or not arch.covers_xy(k):
            raise bnwt.Unavailable(f"{key}: outside the build's coverage")
        b = self.mem_get((arch.name, k))
        if b is None:
            path = arch._tile_path(*k)
            b = pmtiles._read(path)
            if b is None:
                self.fetch_squares(arch, [k], kind)
                b = pmtiles._read(path)
            if b is None:
                raise bnwt.Unavailable(f"{k}: not read")
            self.mem_put((arch.name, k), b)
        return b

    def names(self, key, kind="demand"):
        """The section names of cell key's tile (fetched if need be), or None: not in the build."""
        key = mapcells.check(key)
        ck = str(key)
        got = self._heads.get(ck)
        if got is not None:
            return got
        try:
            b = self._cell_bytes(key, kind)
            got = frozenset(n for n, _c, _l in fast._ef.cip_cell_info(b)[1]) if b else frozenset()
        except (bnwt.Unavailable, netfetch.FetchError, ValueError) as e:
            self._note(key, f"{type(e).__name__}: {e}")
            return None
        self._heads[ck] = got
        return got

    def one(self, key, name, kind="demand"):
        """Section name of cell key alone, decoded (the frame only as far as its end; DECODED_MB of sections kept, one
        decode of each at a time), or None: not in the build."""
        key = mapcells.check(key)
        k = (str(key), name)
        with self._dec_lock:
            got = self._dec.get(k)
            if got is not None:
                self._dec.move_to_end(k)
                return got
            ev = self._decoding.get(k)
            mine = ev is None
            if mine:
                ev = self._decoding[k] = threading.Event()
        if not mine:
            ev.wait(60.0)
            with self._dec_lock:
                got = self._dec.get(k)
            if got is not None:
                return got
        try:
            names = self.names(key, kind)
            if names is None or name not in names:
                return None
            b = self._cell_bytes(key, kind)
            t0 = time.perf_counter()
            got = decode_cell(key, b, [name]).get(name)
            self.stats["decode_s"] += time.perf_counter() - t0
            self.stats["cells"] += 1
            if got is not None:
                n = len(got) if isinstance(got, bytes) else sum(a.nbytes for a in got.values())
                with self._dec_lock:
                    self._dec[k] = got
                    self._dec_bytes += n
                    self._dec_sizes[k] = n
                    while self._dec_bytes > DECODED_MB * 1e6 and len(self._dec) > 1:
                        old, _ = self._dec.popitem(last=False)
                        self._dec_bytes -= self._dec_sizes.pop(old, 0)
            return got
        except ValueError as e:         # (a damaged cell file: dropped, the sources this time)
            log.warning("cip %s: damaged (%s): from the sources", key, e)
            self._heads.pop(str(key), None)
            try:
                os.remove(self.archive()._tile_path(ZOOM, key.x, key.y))
            except (OSError, bnwt.Unavailable):
                pass
            return None
        except (bnwt.Unavailable, netfetch.FetchError) as e:
            self._note(key, f"{type(e).__name__}: {e}")
            return None
        finally:
            if mine:
                with self._dec_lock:
                    self._decoding.pop(k, None)
                ev.set()

    def sections(self, key, kind="demand"):
        """{name: section} of cell key (every section decoded, not kept), or None: not in the build (the sources)."""
        key = mapcells.check(key)
        try:
            b = self._cell_bytes(key, kind)
            t0 = time.perf_counter()
            got = decode_cell(key, b) if b else {}
            self.stats["decode_s"] += time.perf_counter() - t0
        except bnwt.Unavailable as e:
            self._note(key, str(e))
            return None
        except netfetch.FetchError as e:
            self._note(key, f"{type(e).__name__}: {e}")
            return None
        except ValueError as e:         # (a damaged cell file: dropped, the sources this time)
            log.warning("cip %s: damaged (%s): from the sources", key, e)
            try:
                os.remove(self.archive()._tile_path(ZOOM, key.x, key.y))
            except (OSError, bnwt.Unavailable):
                pass
            return None
        self.stats["cells"] += 1
        return got

    def has_section(self, key, name):
        """Is section name of cell key's tile on disk already (no network, no decode: its head only; for MapData.has,
        which the event loop asks)? False when the build is not open yet, the cell not fetched or not in it."""
        with self._lock:
            arch = self._arch.get(ARCHIVE)
        if arch is None or arch.meta is None or key.z != ZOOM or not arch.covers_xy((ZOOM, key.x, key.y)):
            return False
        if name.startswith("m:"):
            lays = self.built_layers()
            if lays is not None and name[2:].split(".", 1)[0] not in lays:
                return False
        ck = str(key)
        got = self._heads.get(ck)
        if got is None:
            try:
                with open(arch._tile_path(ZOOM, key.x, key.y), "rb") as fh:
                    b = fh.read(4096)
            except OSError:
                return False
            try:
                got = frozenset(n for n, _c, _l in fast._ef.cip_cell_info(b)[1]) if b else frozenset()
            except ValueError:
                return False
            if len(self._heads) > 65536:
                self._heads.clear()
            self._heads[ck] = got
        return name in got

    def built_layers(self):
        """The map layers the build holds (its index's "layers"; the others are read from their sources by design,
        no fallback: the planet build leaves the rasters out), or None (every layer)."""
        m = self.manifest()
        got = (m or {}).get("layers")
        return None if got is None else set(got)

    def section(self, key, name, kind="demand"):
        """One section of cell key, or None (the cell or the section not in the build)."""
        if name.startswith("m:"):
            lays = self.built_layers()
            if lays is not None and name[2:].split(".", 1)[0] not in lays:
                return None
        got = self.one(key, name, kind)
        if got is None:
            self.stats["fallbacks"] += 1
            self._note(key, f"no {name}")
        else:
            self.stats["sections"] += 1
        return got

    def _note(self, key, why):
        k = (str(key), why)
        if k not in self._noted:
            self._noted.add(k)
            log.info("cip %s: %s: from the sources", key, why)

    def prefetch(self, keys, kind="prefetch"):
        """The cells of keys not on disk read at once (cells next to each other in the archive in one range read)."""
        try:
            arch = self.archive().open(kind)
        except bnwt.Unavailable:
            return set()
        ks = sorted({(ZOOM, k.x, k.y) for k in keys if k.z == ZOOM and arch.covers_xy((ZOOM, k.x, k.y))})
        new = [k for k in ks if not os.path.exists(arch._tile_path(*k))]
        if new:
            self.fetch_squares(arch, new, kind)
        return set(ks)

    def airports(self, kind="demand"):
        """(airports.csv, runways.csv) bytes of the build's OurAirports snapshot, or None."""
        m = self.manifest(kind)
        if not m or not m.get("airports"):
            return None
        arch = self.archive()
        out = []
        for name in ("airports.csv.gz", "runways.csv.gz"):
            path = os.path.join(arch.dir, "..", name)
            b = pmtiles._read(path)
            if b is None:
                url = f"{arch.dir_url}/{name}"
                lp = bnwt._local_path(url)
                try:
                    if lp is not None:
                        with open(lp, "rb") as f:
                            b = f.read()
                    else:
                        b = self.fetcher.get(url, kind=kind, lane=LANE).body
                except (OSError, netfetch.FetchError):
                    return None
                if hashlib.sha256(b).hexdigest() != m["airports"].get(name):
                    log.warning("cip: %s does not match the build's record", url)
                    return None
                pmtiles._write(path, b)
            out.append(gzip.decompress(b))
        return tuple(out)


# --- the readers' hooks ----------------------------------------------------------------------------------------------------
_READERS = {}
_RLOCK = threading.Lock()


def reader(cache_dir, fetcher, release):
    """The process's CIP reader of cache_dir (one per cache and release), or None when off."""
    if not ON:
        return None
    k = (os.path.realpath(cache_dir), release)
    with _RLOCK:
        r = _READERS.get(k)
        if r is None:
            r = _READERS[k] = Reader(os.path.join(cache_dir, "mapdata", "raw"), fetcher, release)
        return r


def any_reader():
    with _RLOCK:
        return next(iter(_READERS.values()), None)


def layer_arrays(r, key, layer, kind="demand"):
    """A map layer's cell file arrays (a dict, its order) from the build, or None (the sources)."""
    if r is None:
        return None
    got = r.section(key, f"m:{layer}", kind)
    return None if got is None else dict(got)


def roadz_files(r, src, key):
    """(Lines file bytes (an .npz), gaps JSON bytes or None) of cell key's lidar answer of source src (roadz's file
    name), or None (not in the build)."""
    if r is None:
        return None
    arrs = r.one(key, f"z:{src}")
    if arrs is None:
        return None
    buf = io.BytesIO()
    np.savez(buf, **arrs)
    return buf.getvalue(), r.one(key, f"z:{src}.gaps")


def side_json(r, name, key):
    """The decoded JSON of section f:<name> / t:<name> of cell key, or None (not in the build)."""
    if r is None:
        return None
    got = r.one(key, name)
    return None if got is None else json.loads(got.decode("utf-8"))
