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
it (the golden-hash tests and the deterministic generators can rely on it). The roads layer as read
(cell()) also has OpenStreetMap's road tags joined, from the run's build of them (versions()
"osmtags"); its file is Overture's alone.

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
from runtimeprofiler import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np
import dnp

import geodesy
import gradesep
import mapcells
import mapgeom
import maprasters
import bnwt
import cip
import netfetch
import osmml
import overture
import pmtiles
import rangeread
import revisit
import levelways
import roadsmooth
import staging
import xproc

log = logging.getLogger("earthd.mapdata")

# Where the Overture layers that Overture's own vector tiles hold whole (pmtiles.LAYERS: roads, buildings, places...)
# come from: "pmtiles" (the default: a fresh cell is one ~0.1-1 MB range read, ~0.1-0.5 s, instead of DuckDB over its
# parquet row groups, 3-25 s) or "parquet" (as before; also every layer's fallback when the tiles fail). (Off for a
# few hours on 2026-11-11: with the tiles the game froze on cold teleports; the cause was the station boards' PNGs the
# engine cooked while the place loaded, which the tiles' early stations exposed: DDS now, imagery.write_picture_dds.)
MAP_SOURCE = (os.environ.get("BNGE_MAP_SOURCE") or "pmtiles").strip().lower()
LAYER_THREADS = 12             # layers built at once, every cell and caller of the process (mostly waiting on downloads: 4 were a fresh place's first graded block's ~25 s queue, Ipswich 2026-10-30)
OSM_THREADS = 8                 # cells whose OpenStreetMap tags are read at once (alongside their Overture roads)
OSM_FUEL = os.environ.get("BNGE_OSM_FUEL", "1") != "0"     # OpenStreetMap's fuel stations per cell (osmfuel.py; the
                                # user, 2026-10-28: "lets test OSM if it is too laggy we will revert")
OSM_POI_VERSION = 2             # the cells' rows: 2 = fuel and charging stations, with their amenity (EV chargers,
                                # 2026-10-29); a new version is a new folder, and part of versions()
# (cell, layer)s built ahead at once by warm() (a fresh place's first road tile asked for ~70 of them one after another:
# 56 s in Marseille, GitHub #241): half the CPUs, at most 8, and none on 8 or fewer: each is a DuckDB query and a
# decode of its own, and on a Steam Deck (4 cores, 8 threads) the work done ahead kept the CPUs from the road tiles
# (all 36 of a fresh place: 122 s -> 145-181 s over 8 places, 2026-10-25); on a 16-thread PC they came 2-10 s sooner.
_CPUS = os.cpu_count() or 4
WARM_THREADS = min(8, _CPUS // 2) if _CPUS > 8 else 0
BUILDING_MAX_M2 = 2.0e6     # m2: a building footprint larger than this is a mapping error (the largest real ones are
                            # ~1 km2; an OpenStreetMap one of 15.8 km2 roofed a city in Egypt, GitHub #413)
# The deflate level a cell's layer file is written at: np.savez_compressed's (zlib's default, 6) cost ~1.3 CPU-s of a
# cold place's ~300 files (Lawrence KS, 2026-11-11), level 3 half of it for files ~12 % larger (13.6 MB for 12.2). The
# same .npz (np.load reads any level), the same arrays.
STORE_LEVEL = 3
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
# (roads 7 kept for GitHub #683, a segment's own way, not the route relation Overture lists before it,
# overture._SRC_OWN: only cells fetched after it; the cached ones get it with the next Overture release bump, as #184)
# roads 8: a segment's src_record is its OpenStreetMap way, the one covering most of it, also on numbered roads (the
# route's relation came first in its sources: no way id, so none of OpenStreetMap's lanes, width, maxspeed or lane tags);
# the "src_ways" rule holds each way's range on a segment of several (overture.way_sources)
# (and PR #471's validated highway lane counts, current-way geometry evidence; merged before roads 8 was released)
# roads 9: OpenStreetMap's busways appended (osmtags.busway_features). Overture emits none, as with raceways.
# (osmtags.is_outline, a raceway that is a circuit site's outline left out, GitHub #1010: as #184, only cells fetched
# after it; the cached ones with the next Overture release bump)
# roads 10: the layer file is Overture's alone (no OpenStreetMap tags, raceways or busways baked in): the OSM road tags
# are joined when the layer is decoded, from the run's build of the weekly tag shards (MapData._osm_join; R7 of
# ~/bnge-tests/manifestaudit: cells fetched in different weeks carried different lanes / widths / surfaces of one way,
# keyed by the Overture release alone). That build is in versions() ("osmtags"), so in every key made of the joined
# roads (plans, cell facts, road tiles, the decoded roads' cache); the old files, with their own week's tags, are
# fetched again once.
LAYER_VERSIONS = {"roads": 10}


def dem_identity():
    """The elevation's identity in MapData.versions() (so in the road plans', cell facts', road tiles' and shared
    decisions' keys; the road revamp's R1, 2026-11-16: plans made on another elevation were found under the same key
    and kept): [dem.ELEVATION_ID (pinned per app release), dem.DECODED_VERSION, roadz.VERSION]."""
    import dem
    import roadz
    return [int(dem.ELEVATION_ID), int(dem.DECODED_VERSION), int(roadz.VERSION)]
LANE_CONTEXT_CELLS = 16       # neighbour cells' raw road layers kept for slip-road mouths (lanesections.split's context)
# (LidarBC's measured canopy heights, maprasters.bc_canopy, are opt-in: BNE_BC_CANOPY=1 fills cells built after it;
# no canopy version bump, which would fetch every player's canopy cells again)
                                # (2: clipped polygon pieces `own`ed by their cell, exact cell edges,
                                # the exact no-area test of clipped rings)
# OpenStreetMap's natural features and the depth polygons (natural.py): only built while a BNE_NATURAL_* switch asks
# for them (after the others in ALL_LAYERS: the cover map's layer rows of the others keep their numbers)
NATURAL_LAYERS = ("land", "bathymetry")
VECTOR_LAYERS = ("roads", "connectors", "buildings", "water", "land_use", "infrastructure", "building_parts", "places",
                 "road_routes", "town_areas", "town_points", "hoods") + NATURAL_LAYERS
RASTER_LAYERS = ("landcover", "canopy")
OPTIONAL_LAYERS = ("building_parts", "places", "road_routes", "town_areas", "town_points",
                   "hoods") + NATURAL_LAYERS   # built only when asked for by name (buildings.py,
                                                                    # gasstations.py, dirsigns.py, townentry.py)
LAYERS = tuple(x for x in VECTOR_LAYERS if x not in OPTIONAL_LAYERS) + RASTER_LAYERS
ALL_LAYERS = tuple(x for x in VECTOR_LAYERS if x not in NATURAL_LAYERS) + RASTER_LAYERS + NATURAL_LAYERS
DEFAULT_CACHE = os.path.expanduser("~/.cache/beamng-earth")
# A request that the limiter or a policy refused for a non-demand kind (prefetch() returns False)
DEFERRED = (netfetch.Deferred, netfetch.PolicyDenied, netfetch.BudgetExceeded, netfetch.HostUnavailable,
            netfetch.RateLimited)


def executor_closed(e):
    """Did a thread pool refuse e's work because it was shut down? At a process's exit concurrent.futures stops every
    pool's workers while the builds still running finish: their next submit fails with this (gate 293, a builder
    process ending with layer builds on its threads). Not a source's failure: an optional source's "go on without it"
    would store the cell without it for good."""
    return isinstance(e, RuntimeError) and str(e).startswith("cannot schedule new futures")


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
STAND_IN_S = 30.0           # s a parquet cell standing in for the tiles' is served from memory (cell()), then the
                            # tiles asked again (a memory hit does not extend it)
BUILD_WAIT_S = 180.0        # a cell layer another process is building is waited for this long (xproc.once)
MIRROR_MB = 2048
MIRROR_PRUNE_S = 120.0  # the mirror pruned at most this often per process (each prune walks it all)
_ASK = object()         # (MapData._rails / _lanes: the tag data's version not asked for yet)
MIRROR_SMALL = 65536    # bytes: an array this small is read into memory (no mapping)
MEM_MB = 256        # (192 before 2026-11-10: with places slimmed, a Manhattan 3x3 build still reads ~97 roads layers,
                    # ~341 MB: 256 cut cold road tiles' instructions 3.7 / 2.5 / 1.7 % at Manhattan / Munich / Fells Point
                    # for +45-57 MB peak RSS; budget.py's small machines keep SMALL_MAPDATA_MB)
                    # decoded cell layers kept in memory per process (a road tile reads its ~4 cells, each read by
                    # its neighbours too: ~8 ms of decompression and a coordinate transform each time, 2026-10-15).
                    # A road tile's layers of Baltimore's 4 cells are ~124 MB by _nbytes (the infrastructure 65):
                    # at 64 every tile decoded them again, 446 loads for 36 tiles (7.5 s of 87 on a Steam Deck)


# The road tiles read a cell's places (gas stations, EV chargers, the development context's pedestrian places) through
# places_slim: only the rows they use, each at its first point, with their readers' findings made on the whole layer
# (slim_places). The whole layer (Overture's places with every name, category and source as fixed-width strings: ~64
# MB a Manhattan cell, ~110 MB by _nbytes) is decoded for it and not kept: kept, two of them filled the decoded
# layers' MEM_MB and pushed out the roads layers every cell's facts read round it, decoded again and again (330
# decodes of 97 cells for a Manhattan 3 x 3 road tile build, ~12 % of its instructions, 2026-11-10).
PLACES_SLIM_MB = 32     # the slim places kept per process (a Manhattan cell's ~3 MB)


def slim_places(f):
    """places layer f cut down to its fuel stations' candidates (gasstations._candidates), EV chargers'
    (evchargers._ev_candidates) and pedestrian places (development._pois), in their order, one point each (its first:
    where their readers take it), those findings kept on it in its rows (its attrs left out: nothing else reads them).
    f itself when one of those rows has no point of its own."""
    if f is None or not len(f):
        return f
    import development
    import evchargers
    import gasstations
    fuel, ev, pois = gasstations._candidates(f), evchargers._ev_candidates(f), development._pois(f)
    rows = np.unique(np.concatenate([np.asarray([r[0] for r in fuel], np.int64),
                                     np.asarray([r[0] for r in ev], np.int64), np.asarray(pois, np.int64)]))
    part_off, ring_off = np.asarray(f.part_off, np.int64), np.asarray(f.ring_off, np.int64)
    coord_off = np.asarray(f.coord_off, np.int64)
    p0 = part_off[rows]
    if len(rows) and ((part_off[rows + 1] <= p0).any() or (coord_off[ring_off[p0] + 1] <= coord_off[ring_off[p0]]).any()):
        return f            # (a row without a point of its own: its readers' index arithmetic is not a slice's)
    c = coord_off[ring_off[p0]]
    m = len(rows)
    step = np.arange(m + 1, dtype=np.int64)
    out = mapgeom.Features(
        f.layer, f.ids[rows], np.asarray(f.kind)[rows], step, step.copy(), step.copy(),
        np.ascontiguousarray(np.asarray(f.lonlat)[c]), None if f.en is None else np.ascontiguousarray(f.en[c]),
        None if f.own is None else np.asarray(f.own)[rows], None if f.clipped is None else np.asarray(f.clipped)[rows],
        {}, {})
    pos = np.full(len(f), -1, np.int64)
    pos[rows] = np.arange(m, dtype=np.int64)
    out.__dict__["_fuel_candidates"] = [(int(pos[r[0]]),) + tuple(r[1:]) for r in fuel]
    out.__dict__["_ev_candidates"] = [(int(pos[r[0]]),) + tuple(r[1:]) for r in ev]
    out.__dict__["_development_pois"] = pos[np.asarray(pois, np.int64)]
    cid = f.__dict__.get("_bnge_cell")
    if cid is not None:     # (its own facts' key: the whole layer's findings are by its rows)
        out.__dict__["_bnge_cell"] = (cid[0], "places_slim", cid[2])
    return out


def _slim_nbytes(f):
    if f is None:
        return 1024
    return int(sum(getattr(v, "nbytes", 0) for v in (f.ids, f.kind, f.lonlat, f.en, f.own, f.clipped,
                                                    f.__dict__.get("_development_pois")))
               + sum(getattr(v, "nbytes", 0) for v in f.attrs.values())       # (the whole layer when kept as it is)
               + 200 * (len(f.__dict__.get("_fuel_candidates", ())) + len(f.__dict__.get("_ev_candidates", ())))) + 1024


def places_of(md, key, kind="demand"):
    """Cell key's places as the road tiles read them: md.places_slim, or (a map data without it) its places layer."""
    slim = getattr(md, "places_slim", None)
    if slim is not None:
        return slim(key, kind)
    return md.cell(key, layers=("places",), kind=kind).layers.get("places")


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



def _savez_deflated(path, arrays, level):
    """np.savez_compressed(path, **arrays) at deflate level `level` (numpy's own writer: each array an .npy member of a
    zip64 archive)."""
    import zipfile
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=level) as zf:
        for key, val in arrays.items():
            with zf.open(key + ".npy", "w", force_zip64=True) as fh:
                np.lib.format.write_array(fh, np.asanyarray(val), allow_pickle=True)

class StandIn(dict):
    """A layer's arrays from the parquet cell standing in for the tiles' (MapData.layer_arrays): a dict, marked."""


class _NoTags(tuple):
    """MapData._osm_cell's answer when no source answered (no tags: the roads keep their guesses), marked."""


class _OsmTagsSource:
    """The run's OSM road tags as decisions' weekly slices read them (MapData.osm_tags_source)."""

    def __init__(self, md):
        self.md = md

    def known(self):
        return self.md.osm_tags_build(network=False)

    def cell_slice(self, key, fetch=True):
        return self.md.osm_tags_slice(key, fetch)


def _stand_in_stale(f):
    """Is decoded layer f a stand-in (_bnge_fallback: when it was decoded) older than STAND_IN_S?"""
    t = getattr(f, "__dict__", {}).get("_bnge_fallback")
    return t is not None and time.monotonic() - t >= STAND_IN_S   # (>=: a coarse clock (Windows: 15.6 ms ticks) can read 0.0 since)


class MapData:
    """See the module docstring. fetcher: the server's netfetch.Fetcher (None: one of its own);
    release: the Overture release (default: overture.effective(), the process's own)."""

    def __init__(self, cache_dir=DEFAULT_CACHE, fetcher=None, release=None,
                 threads=overture.DUCKDB_THREADS):
        release = release or overture.effective()      # (the process's release, frozen: overture.effective)
        self.cache_dir = cache_dir
        self.fetcher = fetcher if fetcher is not None else netfetch.Fetcher()
        self.release = release
        self.raw_dir = os.path.join(cache_dir, "mapdata", "raw")
        self.overture = overture.Reader(self.raw_dir, self.fetcher, release, threads)
        # Overture's own vector tiles for the layers they hold whole (pmtiles.LAYERS), the parquet files the fallback
        self.pmtiles = pmtiles.Reader(self.raw_dir, self.fetcher, release) if MAP_SOURCE == "pmtiles" else None
        # our prebaked input tiles (bnwt.py, BNGE_BNWT_URL) for the layers they hold: the cell its source gives, bit for
        # bit (same file, same keys), one range read for a block of cells
        self.bnwt = bnwt.Reader(self.raw_dir, self.fetcher, release) if bnwt.enabled() else None
        # architecture v2's compact per-cell inputs (cip.py, BNE_V2_CIP=on): a cell's layers as the sources give them
        # (the roads Overture's alone: their OSM tags joined at the decode, as every cell's), one range read a cell; the
        # sources below stay every section's fallback
        self.cip = cip.reader(cache_dir, self.fetcher, release)
        import picnicsource            # (OSM picnic tables: Overture's picnic_table class is unpopulated)
        self.picnic_osm = picnicsource.Source(self.overture, self.fetcher)
        import billboardsource         # (OSM billboards: Overture does not keep advertising=billboard)
        self.billboard_osm = billboardsource.Source()
        # the per-cell sources a road tile's props read without waiting (cells(keys, wait=False)): side_prefetch starts
        # their reads at the tile's build start, so they are mostly there when the props ask at its end
        self.side_sources = [self.picnic_osm]
        self.rasters = maprasters.Rasters(self.raw_dir, self.fetcher, os.path.join(cache_dir, "dem", "lidarbc"))
        import lod2roofs                # (Germany's measured roofs: shape and height of each building)
        self.lod2_roofs = lod2roofs.Source(cache_dir, self.fetcher) if lod2roofs.ON else None
        import bdtopo                   # (France's measured buildings: IGN BD TOPO, joined in cell(), BNE_BDTOPO)
        self.bdtopo = bdtopo.Source(cache_dir, self.fetcher) if bdtopo.ON else None
        import railtags                 # (OSM's railway tags joined to the roads layer's rail segments)
        self.rail_tags = railtags.RailTags(cache_dir, self.fetcher) if railtags.ON else None
        # the weekly station shards (railtags.StationTags: fuel and charging stations, capacity, charge points)
        self.station_tags = railtags.StationTags(cache_dir, self.fetcher) if railtags.STATIONS_ON else None
        import drivefeat                # (the mapped driving features' weekly shards: only with a switch that reads them)
        if drivefeat.SHARDS:
            import drivetags
            self.drive_tags = drivetags.DriveTags(cache_dir, self.fetcher)
            drivetags.DEFAULT[:] = [self.drive_tags]
        import regionaldata
        regionaldata.configure(cache_dir, self.fetcher)     # (PR #505's regional scenery data, fetched on demand)
        import nbiskew                  # (the US bridge inventory's skews: roadshape's deck ends, BNE_DECK_SKEW)
        nbiskew.configure(cache_dir, self.fetcher)
        import roadsys                  # (the waterways a deck end crosses: the road system's deck-end skew)
        roadsys.set_water_fn(lambda key: self.cell(key, layers=("water",), kind="demand").layers.get("water"))
        import germanroads              # (Germany's surveyed road widths, lanes and surfaces, fetched on demand)
        germanroads.configure(cache_dir, self.fetcher)
        import roadz                    # (real road / deck / tunnel heights where a source has them: BNE_ROADZ)
        roadz.install(cache_dir, self.fetcher, self)
        self.lane_tags = railtags.LaneTags(cache_dir, self.fetcher) if railtags.LANES_ON else None   # (markings.py)
        import decisions                # (the shared decision archives' weekly slices are made from these shards)
        decisions.register_weekly(self)
        import highwaysigns             # (exit, overhead and mile-marker signs: railtags.SignTags, signstream.py)
        self.highway_signs = highwaysigns.Source(self) if railtags.SIGNS_ON else None
        self.dem = None                 # (gradesep's ground: the server's dem.Dem, set by its owner, else its own)
        self._locks = {}
        self._lock = threading.Lock()
        self.pool = ThreadPoolExecutor(LAYER_THREADS, thread_name_prefix="mapdata-layer")
        self._mem = OrderedDict()           # (cell, layer) -> (decoded layer, provenance, bytes): cell()'s LRU
        self._mem_bytes = 0
        self._mem_lock = threading.Lock()
        self._loading = {}                 # (cell, layer) -> shared Future, including decode; guarded by _mem_lock
        self._cell_stats = dict(memory_hits=0, loads=0, coalesced=0, wait_s=0.0, load_s=0.0, decode_s=0.0)
        self._slim = OrderedDict()          # cell -> (slim places, bytes): places_slim's LRU (PLACES_SLIM_MB)
        self._slim_bytes = 0
        self._slim_locks = [threading.Lock() for _ in range(16)]    # (one thread makes a cell's)
        self._warm_ex = None
        self._warming = set()               # (cell, layer)s queued or building in warm()

    def close(self):
        self.pool.shutdown(wait=False)
        if self._warm_ex is not None:
            self._warm_ex.shutdown(wait=False, cancel_futures=True)
        self.overture.close()
        if getattr(self, "pmtiles", None) is not None:
            self.pmtiles.close()
        if getattr(self, "bnwt", None) is not None:
            self.bnwt.close()

    def _rails(self, key, layer, feats, built=_ASK):
        """The roads layer's rail segments with OSM's railway tags (railtags.py), once per build of them."""
        rt = getattr(self, "rail_tags", None)
        if rt is None or layer != "roads" or feats is None or not len(feats):
            return
        sub = feats.attrs.get("subtype")
        if sub is None or not (np.asarray(sub).astype(str) == "rail").any():
            return                          # (no rails: nothing to ask for)
        if built is _ASK:
            built = rt.version()
        if built is None or feats.__dict__.get("_rails_built") == built:
            return
        try:
            if rt.join(feats, key) is not False:    # (False: its shard unreachable, joined again next time)
                feats.__dict__["_rails_built"] = built
        except Exception as e:      # (Overture's classes alone)
            log.debug("railtags join %s: %s: %s", key, type(e).__name__, e)

    def _lanes(self, key, layer, feats, built=_ASK):
        """The roads layer's road segments with OSM's lane tags (railtags.LaneTags), once per build of them."""
        lt = getattr(self, "lane_tags", None)
        if lt is None or layer != "roads" or feats is None or not len(feats):
            return
        if built is _ASK:
            built = lt.version()
        if built is None or feats.__dict__.get("_lanes_built") == built:
            return
        try:
            if lt.join(feats, key) is not False:    # (False: its shard unreachable, joined again next time)
                feats.__dict__["_lanes_built"] = built
        except Exception as e:      # (no lane data: the markings' guesses)
            log.debug("lanetags join %s: %s: %s", key, type(e).__name__, e)

    def _joins_due(self, feats):
        """Is a tag join of the roads layer feats due (_rails / _lanes / _drive: its build's join not made yet, a shard
        that could not be had at its decode)?"""
        rt = getattr(self, "rail_tags", None)
        if rt is not None:
            sub = feats.attrs.get("subtype")
            if sub is not None and (np.asarray(sub).astype(str) == "rail").any():
                b = rt.version()
                if b is not None and feats.__dict__.get("_rails_built") != b:
                    return True
        for name, flag in (("lane_tags", "_lanes_built"), ("drive_tags", "_drive_built")):
            t = getattr(self, name, None)
            if t is not None:
                b = t.version()
                if b is not None and feats.__dict__.get(flag) != b:
                    return True
        return False

    def _joined(self, key, layer, feats):
        """A roads layer from memory with its due tag joins made (_joins_due): on a copy, which takes its place in
        memory, never on the object other threads may be reading (a join sets its columns one at a time: a reader
        took some of one join and some of another, R4 of ~/bnge-tests/manifestaudit). The layer itself when none is
        due (the usual case: one join a build, at the decode)."""
        if layer != "roads" or feats is None or not len(feats) or not self._joins_due(feats):
            return feats
        import copy
        c = copy.copy(feats)                # (its own __dict__, attrs and rules; the arrays shared, never changed)
        c.attrs, c.rules = dict(feats.attrs), dict(feats.rules)
        self._rails(key, layer, c)
        self._lanes(key, layer, c)
        self._drive(key, layer, c)
        if all(c.__dict__.get(f) == feats.__dict__.get(f) for f in ("_rails_built", "_lanes_built", "_drive_built")):
            return feats                    # (nothing joined: unreachable again, faulted by the join)
        k = (str(_key(key)), layer)
        with self._mem_lock:
            cur = self._mem.get(k)
            if cur is not None and cur[0] is feats:
                self._mem[k] = (c, cur[1], cur[2])
        return c

    def _drive(self, key, layer, feats):
        """The roads layer's road segments with the drive shards' way tags (drivetags.join: tracktype, ford, a way's
        traffic_calming), once per build of them; only with a switch that reads them (drivefeat.SHARDS). Joined after
        the decode is kept (roadlayercache holds the layer without them: no key of its own to keep)."""
        t = getattr(self, "drive_tags", None)
        if t is None or layer != "roads" or feats is None or not len(feats):
            return
        built = t.version()
        if built is None or feats.__dict__.get("_drive_built") == built:
            return
        import drivetags
        try:
            if drivetags.join(t, feats, key) is not False:
                feats.__dict__["_drive_built"] = built
        except Exception as e:      # (the roads as they are)
            log.debug("drivetags join %s: %s: %s", key, type(e).__name__, e)

    # --- the roads layer as decoded, on disk (roadlayercache.py) ---------------------------------
    def _dec_dir(self, key, layer):
        return self._mirror_dir(self.path(key, layer)) + ".dec"

    def _dec_key(self, key, layer, arrays):
        """roadlayercache's key of cell key's roads (its file's arrays) now, or None (off, no mirror, the layer's file
        not there: its parquet stand-in). The tag data's versions are asked for only where the decode asks (_rails: a
        layer with rail segments; _lanes: one with features): a layer without rails must not fetch the rail tags'
        manifest for its key."""
        import roadlayercache
        if not roadlayercache.ON or MIRROR_MB <= 0:
            return None, {}
        stamp = self._dec_stamp(layer, str(key))
        if stamp is None:
            return None, {}
        import lanesections
        import railtags
        ids, sub = arrays.get("ids"), arrays.get("attr.subtype")
        n = len(ids) if ids is not None else 0
        asks = {"rail_tags": bool(n) and sub is not None and bool((np.asarray(sub).astype(str) == "rail").any()),
                "lane_tags": bool(n)}
        vers = {}
        for name in ("rail_tags", "lane_tags"):
            t = getattr(self, name, None)
            if t is not None and asks[name]:
                vers[name] = t.version()        # (raises as in _rails / _lanes: the decode's own error)
        # (the OSM road tags' build the decode joins: every roads layer, an empty file's too, gets the join's columns)
        vers["osm_tags"] = self.osm_tags_build()
        switches = [levelways.ON, lanesections.ON, roadsmooth.ON, roadsmooth.G2_ON, roadsmooth.RAIL_ON,
                    roadsmooth.SWITCH_PIN_ON, railtags.LANES_ON]
        try:
            return roadlayercache.key(revisit.code_key(), stamp, vers.get("rail_tags", "-"), vers.get("lane_tags", "-"),
                                      switches, vers["osm_tags"]), vers
        except (TypeError, ValueError):
            return None, vers

    def _dec_stamp(self, layer, cell):
        """The stamp now of cell's (its string) layer file, or None. A layer read from its CIP cell (no file: cip.py)
        has its build's: "cip <build> <cell>" (a build never changes)."""
        try:
            z, x, y = (int(v) for v in cell.split("/"))
            key = mapcells.CellKey(z, x, y)
        except ValueError:
            return None
        try:
            return self._stamp(self.path(key, layer))
        except OSError:
            pass
        r = getattr(self, "cip", None)
        if r is not None and r.has_section(key, f"m:{self.layer_name(layer)}"):
            return f"cip {r.build} {key}"
        return None

    def _dec_load(self, key, layer, dec):
        import roadlayercache
        return roadlayercache.load(self._dec_dir(key, layer), dec, lambda c: self._dec_stamp(layer, c))

    # --- what a product made from decoded roads layers (roadsurf3's lane graphs) keys and checks its reads by ----------
    def roads_product_dir(self, key, name):
        """The folder of a product of cell key's roads layer (beside its mirror, pruned with it: prune_mirror), or None
        without a mirror."""
        if MIRROR_MB <= 0:
            return None
        return self._mirror_dir(self.path(_key(key), "roads")) + "." + name

    def roads_stamp(self, cell):
        """The stamp now of cell's (its string) roads layer file (its size and time), or None (not there)."""
        return self._dec_stamp("roads", cell)

    def roads_complete(self, key, feats):
        """Could cell key's decoded roads layer feats be kept as a product's input: its file there (not a parquet
        stand-in) and the tag data its decode asked for joined and read (as _dec_store asks before it keeps a
        decode)?"""
        key = _key(key)
        if feats is None or feats.__dict__.get("_bnge_fallback") or self._dec_stamp("roads", str(key)) is None:
            return False
        n = len(feats)
        sub = feats.attrs.get("subtype") if n else None
        asked = {"rail_tags": bool(n) and sub is not None and bool((np.asarray(sub).astype(str) == "rail").any()),
                 "lane_tags": bool(n)}
        for name, mark in (("rail_tags", "_rails_built"), ("lane_tags", "_lanes_built")):
            t = getattr(self, name, None)
            if t is None or not asked[name]:
                continue
            v = t.version()
            if v is not None and (feats.__dict__.get(mark) != v or not self._tags_read(name, key)):
                return False
        return feats.__dict__.get("_osm_built") == self.osm_tags_build()     # (the run's OSM road tags joined)

    def _tags_read(self, name, key):
        """Is the tag data's (rail_tags / lane_tags) shard of cell key read (in its memory: a shard that could not be
        had is passed over without an error for SHARD_RETRY_S, and the layer joined meanwhile lacks its tags)?"""
        import railtags
        t = getattr(self, name, None)
        shards = getattr(t, "shards", None)
        if shards is None:
            return False
        if getattr(t, "cip_pin", None) is not None and t.cip_pin()[1] and t.cip_read(key):   # (its CIP slice)
            return True
        shift = key.z - railtags.SHARD_ZOOM
        return (key.x >> shift, key.y >> shift) in shards

    def _dec_store(self, key, layer, dec, f, ctx, vers):
        """Keep the decoded roads f of cell key (ctx: [(neighbour cell, read)] of its lane sections; vers: the tag
        data's versions its key was made with) when complete: the tags that are there joined, every neighbour read
        and its file there."""
        import roadlayercache
        for name, mark in (("rail_tags", "_rails_built"), ("lane_tags", "_lanes_built")):
            if vers.get(name) is not None and (f.__dict__.get(mark) != vers[name] or not self._tags_read(name, key)):
                return              # (its join failed or its shard could not be had: tried again at the next decode)
        if "osm_tags" in vers and f.__dict__.get("_osm_built") != vers["osm_tags"]:
            return                  # (its OSM road tags from elsewhere than the run's build: a stand-in)
        stamps = []
        for cell, ok in ctx:
            stamp = self._dec_stamp(layer, cell) if ok else None
            if stamp is None:
                return
            if vers.get("lane_tags") is not None:       # (the neighbour's lanes joined too: its mouths' sections)
                z, x, y = (int(v) for v in cell.split("/"))
                if not self._tags_read("lane_tags", mapcells.CellKey(z, x, y)):
                    return
            stamps.append((cell, stamp))
        d = self._dec_dir(key, layer)
        if roadlayercache.has(d, dec) or roadlayercache.pending(d, dec):
            return              # (another process kept it while this one made it: a first visit's processes read the
            #                     same cells at once; or this one's write of it is queued)
        # (written on roadlayercache's writer thread: the tiles waiting for this decode get it before the pack and the
        # files are written, 2026-11-21)
        roadlayercache.store_later(d, dec, f, sorted(set(stamps)), self._mirror_note)

    def _lane_sections(self, key, layer, kind, frame, f, ctx=None, stand_in=None):
        """The roads layer with its merged highways' lane sections split out (lanesections.split, from PR #880 by
        DerEifrige-f; BNE_LANE_SECTIONS=off: as before). A slip road whose mouth lies in another cell reads that
        cell's own arrays (not cell(): two workers resolving the two ends of one link would wait on each other), so
        every copy of the link has one geometry. stand_in (a list): the neighbour cells whose OSM road tags were not
        the run's build's are appended to it (the layer is then a stand-in too)."""
        import lanesections
        import railtags
        if not lanesections.ON or railtags.LANE_PARTS not in f.attrs:
            return f
        context = {}
        lt = getattr(self, "lane_tags", None)
        built = lt.version() if lt is not None else None
        osm = self.osm_tags_build()
        with self._lock:                # (the neighbour cells' raw layers, kept for the next cells' decodes)
            kept = self.__dict__.setdefault("_lane_context", OrderedDict())

        def lane_context(lat, lon):
            owner = mapcells.cell_of(lat, lon)
            if owner == key:
                return f
            if owner not in context:
                ck = (str(owner), layer, built, osm)
                ok = True
                with self._lock:
                    other = kept.get(ck)
                    if other is not None:
                        kept.move_to_end(ck)
                if other is None:
                    try:
                        ar, _hit = self.layer_arrays(owner, layer, kind)
                        other = mapgeom.Features.from_arrays(layer, {k: v for k, v in ar.items()
                                                                     if k not in ("provenance", "en.local")}, "")
                        # (its OSM road tags, as its own decode joins them: the file is Overture's alone)
                        other, ok, _moved = self._osm_join(owner, other, kind, osm)
                        self._lanes(owner, layer, other)
                        if not ok and stand_in is not None:
                            stand_in.append(str(owner))
                        if ok:              # (tags of another build: used this once, not kept, not stored on)
                            with self._lock:
                                kept[ck] = other
                                while len(kept) > LANE_CONTEXT_CELLS:
                                    kept.popitem(last=False)
                    except Exception as e:
                        log.debug("lane mouth context %s: %s", owner, e)
                context[owner] = other
                if ctx is not None:         # (what this layer's lane sections read: roadlayercache's validity)
                    ctx.append((str(owner), other is not None and ok))
            return context[owner]
        return lanesections.split(f, frame, lane_context)

    def _bdtopo(self, key, layer, feats):
        """A French buildings layer with IGN BD TOPO's measured heights, storeys, materials and use (bdtopo.py), joined
        once its cell's records are here; never waits for them nor asks for them: the building tiles do, for their own
        cells (bdtopo_wait), not every reader of the outlines (the town coverage's 30-odd cells round a tile, the
        trees' masks). Returns the layer."""
        src = getattr(self, "bdtopo", None)
        if src is None or layer != "buildings" or feats is None or feats.__dict__.get("_bdtopo") is not None:
            return feats
        import bdtopo
        if not bdtopo.covers(_key(key).bounds()):
            feats.__dict__["_bdtopo"] = False
            return feats
        recs = src.cached(key)
        if recs is None:
            return feats
        try:
            got = bdtopo.join(feats, recs) if len(feats) else None
        except Exception as e:      # (the map's attributes alone)
            log.warning("bdtopo join %s: %s: %s", key, type(e).__name__, e)
            got = None
        if got is not None:
            feats.attrs = got       # (one swap: a reader holds either the old attrs or the new ones)
        feats.__dict__["_bdtopo"] = recs.n
        return feats

    def bdtopo_wait(self, keys, wait=True):
        """A building tile's wait for its French cells' measured buildings (bdtopo.Source.wait: together, bounded;
        wait=False: their downloads started, not waited for). True when they are all here (or none is French); else
        the tile is a revisit fault (built from the map's attributes alone: not kept, built again with them)."""
        src = getattr(self, "bdtopo", None)
        if src is None:
            return True
        ks = [_key(k) for k in keys]
        ok = src.wait(ks) if wait else (not src.prefetch(ks) and src.wait(ks))
        if not ok:
            revisit.fault()
        return ok

    def versions(self, buildings=False):
        """The map data's identity for cache keys; buildings: for a tile that draws buildings, with the OSM building
        years' and colours' builds too ("osmyears": osmyears.identity, "osmcolours": osmcolours.identity, each frozen
        per process; "off" / "none" without one)."""
        if buildings:
            import osmcolours
            import osmyears
            return dict(self.versions(), osmyears=osmyears.identity(self), osmcolours=osmcolours.identity(self))
        import europewalks

        rails = getattr(self, "rail_tags", None)
        lanes = getattr(self, "lane_tags", None)
        v = {"pipeline": VERSION, "overture": self.release, "worldcover": maprasters.WORLDCOVER_RELEASE,
             "canopy": maprasters.CHM_RELEASE, "rails": rails.known() if rails is not None else None,
             "lanes": lanes.known() if lanes is not None else None,
             # (the OSM road tags joined into the roads at their decode: the run's build of the tag shards,
             # "layercake" without one; roads 10, R7 of ~/bnge-tests/manifestaudit)
             "osmtags": self.osm_tags_build(network=False),
             "gradesep": (gradesep.VERSION, gradesep.MODE) if gradesep.TUNNELS else None,
             "osmfuel": OSM_POI_VERSION if OSM_FUEL else False}
        st = getattr(self, "station_tags", None)
        if st is not None:                      # (the chargers' capacity and charge points: the weekly build in use)
            v["stations"] = st.known()
        import drivefeat
        dv = drivefeat.identity(self)
        if dv is not None:                      # (the drive shards' build and the border data: keyed only while a
                                                # switch reads them)
            v["drive"] = dv
        if getattr(self, "pmtiles", None) is not None:
            # (the cells from Overture's tiles: their own key, so plans, cell facts and tiles built from them never mix
            # with the parquet cells'; BNGE_MAP_SOURCE=parquet keeps the keys as before)
            v["mapsrc"] = pmtiles.source_key()
        if roadsmooth.ON:                       # (a key only while on: what is built without it keeps its cache keys)
            v["roadsmooth"] = roadsmooth.VERSION
        import lanesections
        if lanesections.ON:                     # (likewise the merged highways' lane sections, PR #880)
            v["lanesections"] = lanesections.VERSION
        import roads
        if not roads.WIDTH_MODEL_ON or not roads.WINDING_ON:    # (the measured default widths' switches: off keyed)
            v["widths"] = (roads.WIDTH_MODEL_ON, roads.WINDING_ON)
        if europewalks.enabled():               # (PR #505's European street edges: the same, keyed only while on)
            v["street_edges"] = europewalks.VERSION
        if levelways.ON:
            v["levelways"] = levelways.VERSION
        if not osmml.ON:                        # (every machine-learned outline kept: keyed only while off)
            v["osm_over_ml"] = False
        v["dem"] = dem_identity()               # (the elevation the plans, facts and tiles are made on: pinned per release)
        import roadz
        if roadz.ON and roadz.DE_ON:            # (Germany's measured decks: the build the lidar answers there are made
            import dedom                        # with, dedom.identity; None while not decided, as the weekly ones)
            v["dedecks"] = dedom.identity(network=False)
        import osmareas
        if osmareas.FIT:                        # (BNE_ROADSYS_AREAS=on: the mapped road areas' build, keyed only while on)
            v["osmareas"] = osmareas.identity(self)
        import roadshape
        import roadsys
        if roadshape.SKEW_627 or roadsys.SKEW:  # (the deck ends' skew reads the bridge inventory's table: its build, keyed
            import nbiskew                      # only while a rule reads it; None: no table this run)
            v["nbiskew"] = nbiskew.identity()
        return v

    def freeze_manifests(self, wait_s=None):
        """The server's main process, before its workers start: the run's build of every weekly dataset decided now
        (pagesdata.Manifest's session: rails, lanes, stations, signs, drive, FRA crossings, the OSM tag / year / colour /
        area archives, the tall buildings, the regional scenery), each in a thread of its own: the pin on disk at once,
        a first run's fetched (pagesdata.FETCH_WAIT_S at most, else none for the run). Every process of the run reads
        those (the workers inherit the session's folder), so the tiles, plans and cell facts of one session are of one
        build each, and a first run's are keyed with the builds from its first tile on (versions). Returns a function
        that waits for them (wait_s at most; None: FETCH_WAIT_S and a little): called before the worker processes
        start. (The datasets first asked later, a roof folder or New York's shards, are decided by the first process
        that asks, the same way.)"""
        import pagesdata

        def srcs():
            out = [getattr(self, "rail_tags", None), getattr(self, "lane_tags", None),
                   getattr(self, "station_tags", None), getattr(self, "drive_tags", None)]
            hs = getattr(self, "highway_signs", None)
            out.append(getattr(hs, "tags", None))
            import railtags
            if railtags.ON and self.fetcher is not None:
                out.append(railtags.Shards(self.cache_dir, self.fetcher, "fra"))     # (blockworker's fra.Crossings)
            try:
                tg = self._osm_shards()
                out.append(tg.pages if tg is not None else None)
            except Exception:
                out.append(None)
            import osmareas
            import osmcolours
            import osmyears
            for mod in (osmyears, osmcolours, osmareas) if osmareas.ON else (osmyears, osmcolours):
                try:
                    out.append(mod.shards(self))
                except Exception:
                    out.append(None)
            import roadz
            if roadz.ON and roadz.DE_ON:        # (Germany's measured decks: their build is in versions() too)
                import dedom
                import types
                out.append(types.SimpleNamespace(name="dedecks", frozen=lambda: dedom.manifest().current()))
            try:
                import bldhz            # (the horizon ring's tall buildings, the same way)
                tall = bldhz.source(self)
                out.append(tall if isinstance(tall, bldhz.PagesIndex) else None)
            except Exception:
                out.append(None)
            return [x for x in out if x is not None]

        def one(src):
            try:
                if hasattr(src, "frozen"):
                    src.frozen()
                else:
                    src.version()
            except Exception as e:
                log.debug("manifest of %s: %s: %s", getattr(src, "name", src), type(e).__name__, e)

        def regional():
            try:
                import regionaldata
                regionaldata.manifest()
            except Exception as e:
                log.debug("regional manifest: %s: %s", type(e).__name__, e)
        ts = [threading.Thread(target=one, args=(src,), daemon=True, name="manifests") for src in srcs()]
        ts.append(threading.Thread(target=regional, daemon=True, name="manifests"))
        for t in ts:
            t.start()
        t0 = time.monotonic()
        limit = (pagesdata.FETCH_WAIT_S + 5.0) if wait_s is None else wait_s

        def wait():
            for t in ts:
                t.join(max(0.0, limit - (time.monotonic() - t0)))
            late = sum(t.is_alive() for t in ts)
            if late:
                log.warning("weekly data: %d manifest(s) not decided before the workers start", late)
            log.info("weekly data: %s (%.2f s)", " ".join(f"{k}={v}" for k, v in self.weekly_builds().items()),
                     time.monotonic() - t0)
        return wait

    def weekly_builds(self):
        """{dataset: the run's build or None} of the weekly datasets this map data holds (the log line at start)."""
        out = {}
        for k in ("rail_tags", "lane_tags", "station_tags", "drive_tags"):
            t = getattr(self, k, None)
            if t is not None:
                out[t.name] = t.manifest.built
        try:
            out["osmtags"] = self.osm_tags_build(network=False)    # (the roads' OSM tags: joined at the decode)
        except Exception:
            pass
        return out

    def warm_manifests(self):
        """freeze_manifests without waiting (an owner that starts no worker processes)."""
        self.freeze_manifests()

    def _pmt(self, layer):
        """Does this layer come from Overture's tiles (pmtiles.py; BNGE_MAP_SOURCE)?"""
        return getattr(self, "pmtiles", None) is not None and layer in pmtiles.LAYERS

    @staticmethod
    def layer_name(layer):
        """A layer's name with its own version (LAYER_VERSIONS: "roads.9"), as its cache folder and CIP section."""
        lv = LAYER_VERSIONS.get(layer)
        return layer if lv is None else f"{layer}.{lv}"

    def path(self, key, layer, parquet=False):
        """The cell layer's file. A layer from Overture's tiles has a folder of its own (overture-pmt<FORMAT>): the
        parquet cells (parquet=True: where the parquet path keeps them) are never read for it, nor written over."""
        source, release = _source(layer, self.release)
        if not parquet and MapData._pmt(self, layer):
            source = f"overture-pmt{pmtiles.FORMAT}"
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
        """True when the layer's file exists, or its CIP cell is on disk with it (not read: cheap enough for the event
        loop)."""
        if os.path.exists(self.path(key, layer)):
            return True
        r = getattr(self, "cip", None)
        return r is not None and r.has_section(_key(key), f"m:{self.layer_name(layer)}")

    def layer_provenance(self, key, layer, wait=False):
        """The provenance dict of a cached layer file, None when it is not cached (or has none, or, wait false, not
        read yet: read on a thread of its own, the next ask has it): only that entry of the file is read (np.load is
        lazy per array), and remembered per file (credits.feature_ids asks every second, from the event loop). Never
        fetches."""
        path = self.path(_key(key), layer)
        memo = self.__dict__.setdefault("_prov_memo", {})
        if path in memo:
            return memo[path]
        if getattr(self, "cip", None) is not None:      # (a cell file not built yet is read from its CIP cell)
            self.__dict__.setdefault("_prov_keys", {})[path] = (_key(key), layer)
        if not wait:
            # (a first read, or a file not built yet, off the event loop: on a busy hard disk opening the cell took
            # 5.2 s of the loop, GitHub #1054; the caller asks again a second later)
            self._prov_ask(path)
            return None
        return self._prov_read(path)

    PROV_MISS_S = 30.0      # s before a cell file that was not there is looked for again

    def _prov_read(self, path):
        memo = self.__dict__.setdefault("_prov_memo", {})
        p = None
        try:
            with np.load(path, allow_pickle=False) as z:
                if "provenance" in z.files:
                    p = json.loads(str(z["provenance"]))
        except FileNotFoundError:
            kl = self.__dict__.get("_prov_keys", {}).get(path)
            r = getattr(self, "cip", None)
            if kl is not None and r is not None and r.has_section(kl[0], f"m:{self.layer_name(kl[1])}"):
                got = cip.layer_arrays(r, kl[0], self.layer_name(kl[1]), "background")    # (on disk: no fetch)
                if got is not None and "provenance" in got:
                    p = json.loads(str(got["provenance"]))
                    memo[path] = p
                    return p
            self.__dict__.setdefault("_prov_miss", {})[path] = time.monotonic()
            return None                 # (not remembered: it may be built later)
        except Exception:
            p = None
        if len(memo) > 4096:
            memo.clear()
        memo[path] = p
        return p

    def _prov_ask(self, path):
        """Read path's provenance on the provenance thread (one at a time, newest asks first)."""
        miss = self.__dict__.setdefault("_prov_miss", {})
        if time.monotonic() - miss.get(path, -1e9) < self.PROV_MISS_S:
            return
        with self.__dict__.setdefault("_prov_lock", threading.Lock()):
            queue = self.__dict__.setdefault("_prov_queue", [])
            if path in queue:
                return
            queue.append(path)
            if self.__dict__.get("_prov_thread") is None:
                t = threading.Thread(target=self._prov_loop, name="mapdata provenance", daemon=True)
                self.__dict__["_prov_thread"] = t
                t.start()

    def _prov_loop(self):
        while True:
            with self.__dict__["_prov_lock"]:
                queue = self.__dict__["_prov_queue"]
                if not queue:
                    self.__dict__["_prov_thread"] = None
                    return
                path = queue.pop()
            try:
                self._prov_read(path)
            except Exception:
                pass

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
        _savez_deflated(tmp, arrays, STORE_LEVEL)
        staging.replace_cached(tmp, path)

    def _build(self, key, layer, kind):
        """(content arrays, provenance record or None) of one layer, from the sources (or their CIP cell: the same
        arrays)."""
        if getattr(self, "cip", None) is not None:
            got = cip.layer_arrays(self.cip, key, self.layer_name(layer), kind)
            if got is not None:
                return got
        if layer in VECTOR_LAYERS:
            # the roads' OpenStreetMap tag shard tile is fetched alongside Overture's (each half a fresh cell's wait),
            # not waited for: the file is Overture's alone, the tags are joined at its decode (_osm_join), which reads
            # the tile this started (or asks for it again)
            osm = self._osm_pool().submit(self._osm_warm, key, kind) if layer == "roads" else None
            feats = fell = None
            if self._pmt(layer) and getattr(self, "bnwt", None) is not None and layer in bnwt.LAYERS:
                feats, sources = self._bnwt_query(layer, key, kind, bnwt.PMT)
            if self._pmt(layer) and feats is None:
                try:
                    feats, sources = self.pmtiles.query(layer, key, kind)
                except Exception as e:      # (any: the parquet files have the same layer, slower)
                    if isinstance(e, DEFERRED) and kind not in ("demand", "urgent"):
                        if osm is not None:
                            osm.cancel()
                        raise               # (a prefetch the hosts had no room for: as the parquet path's)
                    fell = e
                    log.warning("mapdata %s %s: Overture's tiles failed (%s: %s): from its parquet files", key, layer,
                                type(e).__name__, e)
            if fell is not None:
                # what is built from the parquet cell is not kept (revisit) and the cell is stored with the parquet
                # ones (layer_arrays): the two sources never share a file or a cache key
                revisit.fault()
                got = self._load(self.path(key, layer, parquet=True))
                if got is not None:
                    if osm is not None:
                        osm.cancel()
                    got["_fallback"] = np.array(True)
                    return got
            if feats is None and fell is None and not self._pmt(layer) and getattr(self, "bnwt", None) is not None \
                    and layer in bnwt.LAYERS:
                feats, sources = self._bnwt_query(layer, key, kind, "parquet")
            if feats is None:
                feats, sources = self.overture.query(layer, key, kind)
            ox, oy = mapcells.owner(feats.attrs.get("cx", np.zeros(0)), feats.attrs.get("cy", np.zeros(0)), key.z)
            feats.own = (ox == key.x) & (oy == key.y)
            if feats.clipped is not None:
                # a polygon cut to the cell is this cell's piece of it: each piece is built where it
                # lies (the pieces of all cells make the whole polygon), not only in the bbox-centre cell
                feats.own |= feats.clipped
            arrays = _layer_arrays(feats)
            prov = overture.sources_provenance(THEME_OF[layer], self.release, sources) if len(feats) else None
            if prov is not None and self._pmt(layer):
                prov["format"] = "parquet" if fell is not None else "pmtiles"
            if fell is not None:
                arrays["_fallback"] = np.array(True)
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

    def _bnwt_query(self, layer, key, kind, source="parquet"):
        """(Features, sources) of the layer from the prebaked tiles (bnwt.py) as source gives them (Overture's tiles:
        bnwt.PMT, else "parquet"), (None, None) when they cannot say (the source itself then: the same content)."""
        try:
            return self.bnwt.query(layer, key, kind, source)
        except DEFERRED:
            if kind not in ("demand", "urgent"):
                raise                       # (a prefetch the hosts had no room for: as the parquet path's)
        except bnwt.Unavailable as e:
            log.debug("mapdata %s %s: no prebaked tile (%s): from the parquet files", key, layer, e)
        except Exception as e:              # (any: the parquet files have the same layer)
            log.warning("mapdata %s %s: prebaked tile failed (%s: %s): from the parquet files", key, layer,
                        type(e).__name__, e)
        self.bnwt.stats["fallbacks"] += 1
        return None, None

    def _osm_pool(self):
        with self._lock:
            if getattr(self, "_osm_ex", None) is None:
                self._osm_ex = ThreadPoolExecutor(OSM_THREADS, thread_name_prefix="osmtags")
            return self._osm_ex

    def _osm_shards(self):
        """The weekly OSM road tag shards (osmtagshards.py), None when off."""
        import osmtagshards
        if not osmtagshards.ON or self.fetcher is None:
            return None
        with self._lock:
            if getattr(self, "osm_shards", None) is None:
                self.osm_shards = osmtagshards.TagShards(self.cache_dir, self.fetcher)
            return self.osm_shards

    def _osm_tile(self, key, kind):
        """The decoded tag shard tile of cell key (osmtagshards.TagShards.tile; None: no tagged highway there), or
        False when the shards cannot answer (off, not published, unreachable, damaged: the Layercake file instead).
        A prefetch the host had no room for is raised (DEFERRED), as the Layercake path's; on demand the Layercake
        file is asked instead."""
        import osmtagshards
        shards = self._osm_shards()
        if shards is None or key.z != mapcells.ZOOM:
            return False
        try:
            return shards.tile(key, kind)
        except osmtagshards.NotInUse:
            return False                # (none published, or off)
        except Exception as e:
            if (isinstance(e, DEFERRED) and kind not in ("demand", "urgent")) or executor_closed(e):
                raise
            shards.down(e)
            return False

    def osm_tags_warm(self, kind="prefetch"):
        """Worker thread: the tag shards' manifest and archive head read once at the start (two small reads, kept on
        disk for every process), so a fresh place's first cell waits only for its leaf directory and tile."""
        shards = self._osm_shards()
        got = shards.manifest() if shards is not None else None
        if got is not None:
            for part in got[1].get("_parts", ()):      # (each archive part's head: head() takes a part, not the
                shards.head(got[0], part, kind)        # manifest, which failed with KeyError: 3 at every start)

    def _osm_geometry(self, key, kind, need):
        """{way id: lon/lat line} of the cell's tagged motorways and trunks (roadlanes.join's callback): from the tag
        shards when they answer, else the Layercake file."""
        import osmtags
        t = self._osm_tile(key, kind)
        if t is not False:
            return osmtags.geometry_from_tile(t, key.bounds(), need)
        with self._lock:
            if getattr(self, "osmtags", None) is None:
                self.osmtags = osmtags.OsmTags(self.raw_dir, self.fetcher)
        return self.osmtags.highway_geometry(key.bounds(), kind, need)

    def _osm_cell(self, key, kind):
        """(tags, extra ways) of the cell's OpenStreetMap highways (osmtags.py): from the weekly tag shards
        (osmtagshards.py, one small read), else the Layercake file; none when both fail (the roads keep their width
        guesses). Extra ways are the raceways and busways Overture does not emit."""
        import osmtags
        if self.fetcher is None:
            return {}, []
        try:
            t = self._osm_tile(key, kind)
            if t is not False:
                return osmtags.from_tile(t, key.bounds())
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
            return _NoTags(({}, []))
        except Exception as e:          # optional: the roads go on with their guesses
            if executor_closed(e):
                raise                   # (not a source's failure: the roads would be stored without their tags)
            log.warning("mapdata: OSM road tags of %s: %s: %s", key, type(e).__name__, e)
            return _NoTags(({}, []))

    # --- the OSM road tags joined at the decode (roads 10: the layer file is Overture's alone) ------------------------
    def _osm_warm(self, key, kind):
        """Pool thread (_build): cell key's tag shard tile read into memory and onto disk while Overture's cell is
        fetched (the decode's join then waits on nothing); nothing raised: the join asks again."""
        try:
            self._osm_tile(key, kind)
        except Exception as e:
            log.debug("osmtags warm %s: %s: %s", key, type(e).__name__, e)

    def osm_tags_build(self, network=True):
        """The OSM road tags' build this run joins into the roads (versions()' "osmtags", so in every key made of the
        joined roads): the weekly tag shards' build of the run (pagesdata's session: one build for every process of a
        server run), "layercake" when the run has none (BNE_OSMTAGS_SHARDS=off, none published, a first run that had
        no answer, a manifest this cannot read: the Layercake file then, as before), "none" without a fetcher. None
        while not known yet (network false: not decided, none pinned)."""
        if self.fetcher is None:
            return "none"
        sh = self._osm_shards()
        if sh is None:
            return "layercake"
        got = sh.build_in_use(network)
        if got is None:
            return None
        return got or "layercake"

    def _osm_read(self, key, kind):
        """(tags, extra ways, geometry(need), source) of cell key's OpenStreetMap highways for its join: from the run's
        tag shards (source: their build), else the Layercake file ("layercake"; _osm_cell), else none ("none": both
        failed). One tile for the tags and the geometry: never two builds in one join. Raises as _osm_tile (a busy
        prefetch: DEFERRED; a closed pool)."""
        import osmtags
        if self.fetcher is None:
            return {}, [], (lambda need: {}), "none"
        key = _key(key)
        bounds = key.bounds()
        t = self._osm_tile(key, kind)
        if t is not False:
            sh = self._osm_shards()
            built = sh.pages.built if sh is not None else None
            tags, ways = osmtags.from_tile(t, bounds)
            return tags, ways, (lambda need: osmtags.geometry_from_tile(t, bounds, need)), built
        got = self._osm_cell(key, kind)
        return got[0], got[1], (lambda need: self._osm_geometry(key, kind, need)), \
            "none" if isinstance(got, _NoTags) else "layercake"

    def _osm_join(self, key, f, kind, ident=_ASK):
        """(f with cell key's OpenStreetMap road tags joined as the cell's build joined them before roads 10: the
        raceways and busways appended, the tags joined, the one-way ends moved, `own` of the appended ways, the
        columns in the order a cell file's decode gives; ok; geometry moved). ok: the tags are the run's build's
        (ident: osm_tags_build(), asked here when not given); else (the shards unreachable a moment ago, the Layercake
        file standing in or failing too) the layer is a stand-in for the player's view: nothing kept is made of it
        (_decoded_layer marks it). f is changed in place and returned (or the appended copy)."""
        import mapgeom as mg
        import oneway_joins
        import osmtags
        if ident is _ASK:
            ident = self.osm_tags_build()
        try:
            tags, ways, geometry, src = self._osm_read(key, kind)
        except DEFERRED:
            # (a prefetch's / background read's tile the host had no room for: the layer without tags, a stand-in,
            # decoded again after STAND_IN_S; the file is there, the read is not refused for want of its tags)
            tags, ways, geometry, src = {}, [], (lambda need: {}), "none"
        n0 = len(f)
        # (as _build joined them before roads 10: busways first, "osm-busway-..." sorting before "osm-raceway-...", as
        # mapgeom.concat requires; a way with no highway tag a raceway; then the tags, then the one-way ends)
        for cls, build in (("busway", osmtags.busway_features), ("raceway", osmtags.raceway_features)):
            picked = [w for w in ways if str(w.get("highway") or "raceway") == cls]
            if picked:
                f = mg.concat(f, build(picked))
        osmtags.join(f, tags, geometry)
        orig = f.lonlat
        if len(f) == n0 and orig is not None:
            # (oneway_joins moves ends in place: a copy of the caller's lonlat, never its arrays, which a mirror's
            # read-only view may be, or arrays the caller keeps, a CIP section's, the builder's)
            f.lonlat = np.array(orig, np.float64)
        moved = oneway_joins.apply(f) > 0
        if not moved and len(f) == n0:
            f.lonlat = orig                 # (no end moved: the caller's own, a mirror's shared mapping, not a copy)
        moved = moved or len(f) != n0
        key = _key(key)
        if len(f) != n0:                # (raceways / busways appended: their owner as _build's own rule)
            ox, oy = mapcells.owner(f.attrs.get("cx", np.zeros(0)), f.attrs.get("cy", np.zeros(0)), key.z)
            own = (ox == key.x) & (oy == key.y)
            if f.clipped is not None:
                own |= f.clipped
            f.own = own
        f.attrs = {k: f.attrs[k] for k in sorted(f.attrs)}     # (from_arrays' order: a stored file's decode)
        f.rules = {k: f.rules[k] for k in sorted(f.rules)}
        f.__dict__["_osm_built"] = src
        return f, src == ident, moved

    def roads_joined_arrays(self, key, arrays, kind="demand"):
        """(cell key's roads layer file arrays with its OSM road tags joined as the decode joins them, as the file
        held them before roads 10: what the quality counts and the CIP builder's lane slices read; ok: of the run's
        build, _osm_join)."""
        key = _key(key)
        f = mapgeom.Features.from_arrays("roads", {k: v for k, v in arrays.items()
                                                   if k not in ("provenance", "en.local")}, "")
        f, ok, _moved = self._osm_join(key, f, kind)
        out = _layer_arrays(f)
        if "provenance" in arrays:
            out["provenance"] = arrays["provenance"]
        return out, ok

    def osm_tags_source(self):
        """decisions' view of the run's OSM road tags (weekly_sources' "osmtags"): known() and cell_slice()."""
        return _OsmTagsSource(self)

    def osm_tags_slice(self, key, fetch=True):
        """The digest (32 hex) of what a join of cell key reads of the run's OSM road tags (decisions' weekly slices:
        the cell's tags, raceways and busways, and the lane geometry of its motorways, as _osm_read gives them), or
        None: not to be had from the run's build (fetch false: not in this process's memory)."""
        import osmtags
        ident = self.osm_tags_build(network=fetch)
        if ident is None:
            return None
        key = _key(key)
        bounds = key.bounds()
        if fetch:
            try:
                tags, ways, geometry, src = self._osm_read(key, "demand")
            except Exception as e:
                log.debug("osmtags slice %s: %s: %s", key, type(e).__name__, e)
                return None
            if src != ident:
                return None
            geom = geometry(None)
        else:
            sh = self._osm_shards()
            if sh is None or ident != sh.pages.built:
                return None
            found, t = sh.cached_tile(key)
            if not found:
                return None
            tags, ways = osmtags.from_tile(t, bounds)
            geom = osmtags.geometry_from_tile(t, bounds, None)

        def plain(o):
            if isinstance(o, np.ndarray):
                return o.tolist()
            if isinstance(o, np.generic):
                return o.item()
            raise TypeError(type(o).__name__)
        doc = {"tags": sorted([int(w), sorted(t.items())] for w, t in tags.items()),
               "ways": [{k: w[k] for k in sorted(w)} for w in ways],
               "geom": sorted([int(w), c] for w, c in geom.items())}
        b = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=plain, allow_nan=True).encode("utf-8")
        return hashlib.sha256(b).hexdigest()[:32]

    def osm_fuel_path(self, key):
        return os.path.join(self.raw_dir, "osmfuel_cells", f"v{OSM_POI_VERSION}", f"{_key(key)}.json")

    def osm_fuel_cached(self, key):
        """The cell's OpenStreetMap fuel stations (osmfuel.OsmFuel.cell rows) as fetched by osm_fuel_fetch, or None when
        not fetched yet: the road tiles' workers only read them, never wait for them (the server fetches them in the
        background and sends the tiles again: earthd Server.osm_fuel_want). [] when off."""
        if not OSM_FUEL:
            return []
        try:
            with open(self.osm_fuel_path(key), encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, ValueError):
            rows = cip.side_json(getattr(self, "cip", None), "f:osmfuel", _key(key))   # (the build's rows: CIP on)
            if rows is None:
                return None
        for r in rows:
            r["bbox"] = tuple(r["bbox"])
        return rows

    def osm_fuel_fetch(self, key, kind="prefetch"):
        """Worker thread: the cell's OpenStreetMap fuel stations fetched (osmfuel.py) and kept on disk (none too: the
        cell is not asked again); [] when off or without a fetcher. The source's errors are raised (asked again)."""
        import osmfuel
        if not OSM_FUEL or self.fetcher is None:
            return []
        with self._lock:
            if getattr(self, "osmfuel", None) is None:
                self.osmfuel = osmfuel.OsmFuel(self.raw_dir, self.fetcher)
        if not osmfuel.budget_ok(self.fetcher):         # (the road tags first: the host's budget is shared)
            raise osmfuel.OverShare(f"OSM fuel: past {osmfuel.OSM_FUEL_SHARE:.0%} of {osmfuel.HOST}'s daily budget")
        rows = self.osmfuel.cell(_key(key).bounds(), kind)
        self._osm_fuel_store(key, rows)
        self._osm_fuel_prune(kind)
        return rows

    def osm_fuel_fetch_many(self, keys, kind="demand"):
        """osm_fuel_fetch of every cell of keys not on disk, in one scan (osmfuel.OsmFuel.cells_many: the same rows;
        tools/build_cip.py's z10 units). Returns the number of cells fetched."""
        import osmfuel
        if not OSM_FUEL or self.fetcher is None:
            return 0
        keys = [_key(k) for k in keys if not os.path.exists(self.osm_fuel_path(k))]
        if not keys:
            return 0
        with self._lock:
            if getattr(self, "osmfuel", None) is None:
                self.osmfuel = osmfuel.OsmFuel(self.raw_dir, self.fetcher)
        if not osmfuel.budget_ok(self.fetcher):
            raise osmfuel.OverShare(f"OSM fuel: past {osmfuel.OSM_FUEL_SHARE:.0%} of {osmfuel.HOST}'s daily budget")
        for k, rows in zip(keys, self.osmfuel.cells_many([k.bounds() for k in keys], kind)):
            self._osm_fuel_store(k, rows)
        self._osm_fuel_prune(kind)
        return len(keys)

    def _osm_fuel_store(self, key, rows):
        path = self.osm_fuel_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        os.replace(tmp, path)

    def _osm_fuel_prune(self, kind):
        import osmfuel
        now = time.monotonic()
        if now - getattr(self, "_osm_pruned", -1e9) >= osmfuel.PRUNE_EVERY_S and hasattr(self.osmfuel, "version"):
            self._osm_pruned = now                  # (its blocks under a cap)
            _, remote = self.osmfuel.version(kind)
            osmfuel.prune_blocks(remote.cache_dir, remote.size)

    def osm_fuel_warm(self, kind="prefetch"):
        """Worker thread: the OSM fuel file's version and row-group index read once (3 requests, 5 MB, about 1 s the
        first time a version: at the server's start, so no cell waits for it)."""
        import osmfuel
        if not OSM_FUEL or self.fetcher is None:
            return
        with self._lock:
            if getattr(self, "osmfuel", None) is None:
                self.osmfuel = osmfuel.OsmFuel(self.raw_dir, self.fetcher)
        self.osmfuel.warm(kind)

    def layer_arrays(self, key, layer, kind="demand"):
        """(arrays, hit) of one cell's layer: from the cache, or built and cached. A StandIn (a dict) when the tiles
        failed and these are the parquet cell's, stored or built now, with or without a provenance record
        (_decoded_layer marks the layer)."""
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
        if getattr(self, "cip", None) is not None:
            # (CIP on: the cell's arrays from its CIP cell as they are, ~ms; no cell file, no mirror: the CIP cell is
            # the copy on disk)
            got = cip.layer_arrays(self.cip, key, self.layer_name(layer), kind)
            if got is not None:
                return got, True
        with self._key_lock(key, layer):
            # another thread, or another of the server's processes (xproc), built it meanwhile
            arrays, mine = xproc.once(path + ".lock", lambda: self._load(path), BUILD_WAIT_S, kind)
            if arrays is not None:
                return self._mirrored(key, layer, path, arrays), True
            try:
                arrays = self._build(key, layer, kind)
                if arrays.pop("_fallback", None) is not None:
                    # (the tiles failed: the parquet cell, kept with the parquet ones; asked of the tiles again next
                    # time it is not in memory)
                    ppath = self.path(key, layer, parquet=True)
                    if not os.path.exists(ppath):
                        self._store(ppath, arrays)
                    return StandIn(self._mirrored(key, layer, ppath, arrays)), False
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
            if names and names[0] == PACK_MARK:
                out = _unpack(os.path.join(d, PACK_FILE), json.loads(names[1]))
            else:                   # (a mirror written before the pack: one .npy per array)
                out = {}
                for n in names:
                    if not n:
                        continue
                    f = os.path.join(d, n + ".npy")
                    big = os.path.getsize(f) > MIRROR_SMALL
                    a = np.load(f, mmap_mode="r" if big else None, allow_pickle=False)
                    out[n] = np.asarray(a)      # (a plain view of the mapping: np.memmap's Python hooks ran on every slice)
                if os.name != "nt":     # packed for the next read (Windows: files mapped by another process stay)
                    self._repack(d, stamp, out)
                    return out
            os.utime(os.path.join(d, "ok"))         # (read: the last to go)
            return out
        except (OSError, ValueError, EOFError, KeyError, IndexError, TypeError):
            return None

    def _repack(self, d, stamp, arrays):
        """An old mirror folder (one .npy per array) written again as a pack (PACK_FILE), in place."""
        tmp = f"{d}.{os.getpid()}.{threading.get_ident()}.tmp"
        import shutil
        try:
            os.makedirs(tmp, exist_ok=True)
            header, _ = _pack(os.path.join(tmp, PACK_FILE), arrays)
            with open(os.path.join(tmp, "ok"), "w") as fh:
                fh.write(stamp + "\n" + PACK_MARK + "\n" + json.dumps(header) + "\n")
            shutil.rmtree(d, ignore_errors=True)
            os.rename(tmp, d)
        except (OSError, ValueError) as e:
            log.debug("mapdata mirror %s: not packed: %s", d, e)
            shutil.rmtree(tmp, ignore_errors=True)

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
            header, size = _pack(os.path.join(tmp, PACK_FILE), full)
            with open(os.path.join(tmp, "ok"), "w") as fh:
                fh.write(stamp + "\n" + PACK_MARK + "\n" + json.dumps(header) + "\n")
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
        """Count what this process wrote; every MIRROR_MB / 8 of it, the mirror cut back to MIRROR_MB. Only in the
        server's own process: every worker process walked the whole mirror (~100k files) at its first write, several
        at once on a first visit, on the server's few CPUs while the world loaded (2026-10-30); earthd prunes it at
        its start too (prune_own_caches)."""
        import multiprocessing
        if multiprocessing.current_process().name != "MainProcess":
            return
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
    def cell_stats(self):
        """Process-wide counters. Durations aggregate overlapping threads, not critical-path/CPU time."""
        with self._mem_lock:
            return dict(self._cell_stats)

    def _decoded_layer(self, key, layer, kind, frame, token=None, remember=True):
        """Load and publish one decoded layer for all its callers. No neighbor discovery is run here:
        hooks are attached only, and no cache lock is held across I/O, decoding or tag joins."""
        k = (str(key), layer)
        try:
            t0 = time.perf_counter()
            arrays, hit = self.layer_arrays(key, layer, kind)
            t1 = time.perf_counter()
            fallback = isinstance(arrays, StandIn)
            arrays = dict(arrays)
            prov = json.loads(str(arrays.pop("provenance")))
            en = arrays.pop("en.local", None)
            if layer in VECTOR_LAYERS:
                # (the roads as they were left here before, by this or another process: roadlayercache.py)
                # (not for a stand-in: its parquet arrays are never stamped with, or stored under, the tiles' file)
                dec, vers = self._dec_key(key, layer, arrays) if layer == "roads" and not fallback else (None, {})
                f = self._dec_load(key, layer, dec) if dec is not None else None
                made = f is None
                osm_ok = True
                if made:
                    f = mapgeom.Features.from_arrays(layer, arrays, "")
                    moved = False
                    if layer == "roads":
                        # (OpenStreetMap's road tags of the run's build: the file is Overture's alone, roads 10)
                        n0 = len(f)
                        f, osm_ok, moved = self._osm_join(key, f, kind, vers.get("osm_tags", _ASK))
                        if not n0 and len(f) and "lane_tags" not in vers:
                            dec = None      # (ways appended to a file without roads: its key asked for no lane tags)
                    if en is not None and len(en) == len(f.lonlat) and not moved:
                        f.en = en
                    else:
                        f = f.localize(frame)
                    f0 = revisit.thread_faults()
                    # (the versions the key was made with, where it asked: each asked once a decode, as before)
                    self._rails(key, layer, f, **({"built": vers["rail_tags"]} if "rail_tags" in vers else {}))
                    self._lanes(key, layer, f, **({"built": vers["lane_tags"]} if "lane_tags" in vers else {}))
                if made and layer == "roads":
                    f = levelways.promote(f, frame)         # (a street mapped as a footway over a level crossing: a road)
                    # (a merged highway's own lane sections as their own pieces, before the bends are rounded:
                    # lanesections.py, from PR #880)
                    ctx, bad = [], []
                    f = self._lane_sections(key, layer, kind, frame, f, ctx, bad)
                    osm_ok = osm_ok and not bad
                    f = roadsmooth.smooth(f, frame)         # (bends as arcs, not corners: roadsmooth.py)
                    if dec is not None and osm_ok and revisit.thread_faults() == f0:
                        self._dec_store(key, layer, dec, f, ctx, vers)
                if layer == "roads":
                    self._drive(key, layer, f)             # (after the decode is kept: drivefeat's way tags)
                if layer == "buildings":
                    f = mapgeom.drop_huge(f, BUILDING_MAX_M2)
                    # (OpenStreetMap over machine-learned outlines where OSM maps the cell's buildings: osmml.py)
                    f = osmml.apply(f, _key(key))
                if fallback or not osm_ok:
                    # (the tiles failed and the parquet cell stood in, a stored one or one built now, empty or not; or
                    # its OSM road tags are not the run's build's, the shards unreachable a moment ago: what is kept
                    # under the tiles' keys is not made from it, roadwidth's walls, widths and clearances; and a build
                    # that takes the layer is not kept: _cell_collect; decoded again after STAND_IN_S)
                    f.__dict__["_bnge_fallback"] = time.monotonic()     # (when: STAND_IN_S)
                if layer == "roads":
                    f.__dict__["_bnge_gradesep"] = gradesep.hook(self, key)
                    f.__dict__["_bnge_neighbours"] = gradesep.neighbours_hook(self, key)
                f.__dict__["_bnge_cell"] = (str(key), layer, json.dumps(self.versions(), sort_keys=True))
            else:
                f = None if "missing" in arrays else _raster_from(arrays)
            nb = _nbytes(arrays)
            if layer == "roads" and f is not None:     # (the joined tags' columns: not in the file since roads 10)
                nb += sum(getattr(v, "nbytes", 0) for c, v in f.attrs.items() if f"attr.{c}" not in arrays)
            entry = (f, prov, nb)
            if remember:
                self._remember(k, entry)
            with self._mem_lock:
                self._cell_stats["load_s"] += t1 - t0
                self._cell_stats["decode_s"] += time.perf_counter() - t1
            return entry, hit, t1 - t0
        finally:
            # Errors are shared by current waiters; a later request can retry. An evicted result
            # remains alive in its Future until every admitted caller has consumed it.
            with self._mem_lock:
                cur = self._loading.get(k)
                if cur is not None and cur[2] is token:     # (not a more urgent load that took its place)
                    del self._loading[k]

    def cell(self, key, layers=LAYERS, kind="demand"):
        """CellData of one cell with the given layers (fetched / built / cached as needed)."""
        return self._cell_collect(self._cell_submit(key, layers, kind))

    def layer_unkept(self, key, layer, kind="background"):
        """One cell's decoded layer (as cell(key, (layer,)).layers[layer]) for a caller that reads many cells once:
        from memory when it is there, else decoded for this call alone and not kept in the MEM_MB cache. The modding
        API's cachedOnly places read up to places.CACHED_CELLS_MAX cells: through cell() they would push the roads
        layers every road tile reads out of MEM_MB (its note above) and be decoded again by the next build."""
        key = _key(key)
        with self._mem_lock:
            got = self._mem.get((str(key), layer))
        if got is None or _stand_in_stale(got[0]):
            lat_c, lon_c = key.center()
            got, _hit, _dt = self._decoded_layer(key, layer, kind, geodesy.EnuFrame(lat_c, lon_c, 0.0), remember=False)
        if getattr(got[0], "__dict__", {}).get("_bnge_fallback"):
            revisit.fault(here=True)            # (the parquet cell standing in for the tiles': as _cell_collect)
        return got[0]

    def cells(self, keys, layers=LAYERS, kind="demand"):
        """[cell(key, layers, kind) for key in keys], their layers loaded side by side: the loops that asked for a
        list of cells one after another waited for each cell's download and decode before asking for the next (a cold
        place's railway station group, 3 layers of its cells one at a time: ~5 s of a block's last stage at Lawrence
        KS, 2026-11-11). The same CellData in the same order; an error is raised once every cell has finished (the
        first in keys' order)."""
        pending = [self._cell_submit(k, layers, kind) for k in keys]
        out, err = [], None
        for st in pending:
            try:
                out.append(self._cell_collect(st))
            except Exception as e:
                err = err or e
        if err is not None:
            raise err
        return out

    def _cell_submit(self, key, layers, kind):
        """cell()'s first half: the layers not in memory submitted (or joined); (key, cd, mem, futs, layers)."""
        key = _key(key)
        lat_c, lon_c = key.center()
        frame = geodesy.EnuFrame(lat_c, lon_c, 0.0)
        cd = CellData(key, key.bounds(), (lat_c, lon_c), frame, self.versions())
        mem, futs = {}, []
        with self._mem_lock:
            for layer in layers:
                k = (str(key), layer)
                got = self._mem.get(k)
                if got is not None and _stand_in_stale(got[0]):
                    self._mem_bytes -= self._mem.pop(k)[2]      # (the tiles asked again)
                    got = None
                if got is not None:
                    self._mem.move_to_end(k)
                    mem[layer] = got
                    self._cell_stats["memory_hits"] += 1
                else:
                    # a load already under way is shared when it is at least as urgent; a more urgent request
                    # loads at its own priority (its downloads' lane: xproc.KIND_RANK, netfetch) and takes its place
                    # for the ones after it: a demand cell waited on a background prefetch's download, its slow
                    # lane, since the loads were shared (PR #676; players: dev.147 "streams very slowly", 2026-11-03)
                    rank = xproc.KIND_RANK.get(kind, 1)
                    cur = self._loading.get(k)
                    shared = cur is not None and cur[1] <= rank
                    if shared:
                        fut = cur[0]
                        self._cell_stats["coalesced"] += 1
                    else:
                        token = object()
                        fut = self.pool.submit(self._decoded_layer, key, layer, kind, frame, token)
                        self._loading[k] = (fut, rank, token)
                        self._cell_stats["loads"] += 1
                        if cur is not None:
                            self._cell_stats["urgent_over"] = self._cell_stats.get("urgent_over", 0) + 1
                    futs.append((layer, fut, shared))
        return key, cd, mem, futs, layers

    def _cell_collect(self, state):
        """cell()'s second half: the CellData once its layers are in (an error raised once all have finished)."""
        key, cd, mem, futs, layers = state
        provs = {}
        results = {}
        err = None
        for layer, fut, shared in futs:     # all finish before an error is raised (no stray fetches)
            start = time.perf_counter()
            try:
                results[layer] = fut.result()
            except Exception as e:
                err = err or e
            finally:
                waited = time.perf_counter() - start
                cd.stats[layer] = {"wait_s": waited, "shared": shared}
                with self._mem_lock:
                    self._cell_stats["wait_s"] += waited
        if err is not None:
            revisit.fault()                 # (a build that goes on without it is not kept)
            raise err
        for layer in layers:                # assembled in the caller's order, whatever finished first
            if layer in mem:                # decoded before: shared (cell layers are never changed after loading)
                cd.layers[layer], prov, _ = mem[layer]
                dt, hit = 0.0, True
                cd.layers[layer] = self._joined(key, layer, cd.layers[layer])
            else:
                entry, hit, dt = results[layer]
                cd.layers[layer], prov, _ = entry
            if getattr(cd.layers[layer], "__dict__", {}).get("_bnge_fallback"):
                # (the parquet cell standing in for the tiles': _build's fault was its loader's, in the pool; a build
                # that takes it later, from memory too, is not kept either)
                revisit.fault(here=True)
            if layer == "buildings":
                self._bdtopo(key, layer, cd.layers[layer])
            elif layer == "roads" and getattr(cd.layers[layer], "__dict__", {}).get("_lidar_rule"):
                # (its ranges made on the lidar rule for want of the lidar: what is made on them is kept on the layer,
                # so whatever takes the layer is not kept either: revisit.fault in the taker's thread)
                revisit.fault(lidar=True)
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
            cd.stats.setdefault(layer, {"wait_s": 0.0, "shared": False}).update(s=dt, hit=hit)
        cd.provenance = [provs[k] for k in sorted(provs)]
        return cd

    def places_slim(self, key, kind="demand"):
        """Cell key's places layer as the road tiles read it (slim_places), kept (PLACES_SLIM_MB); the whole layer
        decoded for it is not (MEM_MB keeps it only when another caller of cell() asked for it). None: no places.
        Raises as cell() does when the layer cannot be had."""
        key = _key(key)
        k = str(key)
        with self._mem_lock:
            got = self._slim.get(k)
            if got is not None:
                self._slim.move_to_end(k)
                return got[0]
        with self._slim_locks[hash(k) % len(self._slim_locks)]:
            with self._mem_lock:
                got = self._slim.get(k)
                if got is not None:
                    self._slim.move_to_end(k)
                    return got[0]
                full = self._mem.get((k, "places"))
            if full is not None:
                f = full[0]
            else:
                lat_c, lon_c = key.center()
                try:
                    entry, _hit, _dt = self._decoded_layer(key, "places", kind, geodesy.EnuFrame(lat_c, lon_c, 0.0),
                                                           remember=False)
                except Exception:
                    revisit.fault()             # (as cell(): a build that goes on without it is not kept)
                    raise
                f = entry[0]
            slim = slim_places(f)
            n = _slim_nbytes(slim)
            with self._mem_lock:
                self._slim[k] = (slim, n)
                self._slim_bytes += n
                while self._slim_bytes > PLACES_SLIM_MB * 1e6 and len(self._slim) > 1:
                    _, (_, m) = self._slim.popitem(last=False)
                    self._slim_bytes -= m
            return slim

    def clear_memory(self):
        """Drop the decoded layers kept in memory (budget.maybe_shed); the cells load again from disk when asked."""
        with self._mem_lock:
            self._mem.clear()
            self._mem_bytes = 0
            self._slim.clear()
            self._slim_bytes = 0
        # (the OSM tag shards: ~20 MB each; the station shards too, Codex on #1111, 2026-11-10)
        for tags in (self.rail_tags, self.lane_tags, getattr(self, "station_tags", None),
                     getattr(self, "osm_shards", None)):
            if tags is not None:
                tags.clear_memory()

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

    def tiles_prefetch(self, needs, kind="prefetch"):
        """The map tiles of the (cell, layer)s in needs not on disk yet fetched in batches (pmtiles.Reader.prefetch:
        neighbouring tiles in one range read) before they are built cell by cell. No-op without the tiles source.
        Returns the tiles asked for."""
        n, done = 0, set()
        if getattr(self, "cip", None) is not None:
            # (the ring's CIP cells as the few byte ranges they are; a layer its cell lacks is built from its source)
            try:
                got = self.cip.prefetch({_key(k) for k, ly in needs if not self.has(_key(k), ly)}, kind)
                n += len(got)
                done = {(_key(k), ly) for k, ly in needs if (14, _key(k).x, _key(k).y) in got}
            except Exception as e:
                log.debug("mapdata: CIP prefetch: %s: %s", type(e).__name__, e)
        if getattr(self, "bnwt", None) is not None:
            todo = [(_key(k), ly) for k, ly in needs if ly in bnwt.LAYERS and not self.has(_key(k), ly)
                    and bnwt.ARCHIVES[bnwt.ARCHIVE_OF[ly]]["source"] == bnwt.source_of(ly, MapData._pmt(self, ly))]
            todo = [t for t in todo if t not in done]
            try:
                done = done | (self.bnwt.prefetch(todo, kind) if todo else set())
                n += len(done)
            except Exception as e:          # (the cells' own builds read their sources then)
                log.debug("mapdata: prebaked tiles prefetch: %s: %s", type(e).__name__, e)
        if getattr(self, "pmtiles", None) is None:
            return n
        todo = [(_key(k), ly) for k, ly in needs if MapData._pmt(self, ly) and not self.has(_key(k), ly)
                and (_key(k), ly) not in done]      # (Overture's tiles not fetched where the prebaked tiles answer)
        return n + (self.pmtiles.prefetch(todo, kind) if todo else 0)

    def side_prefetch(self, keys):
        """The side_sources' background reads of these cells (a road tile's own) started now, without waiting; France's
        measured buildings with them (bdtopo: a tile's own cells only, not the two rings of outlines warm() asks for
        roadwidth's walls: 30 downloads for 4 Aix tiles built from 2 cells)."""
        if getattr(self, "bdtopo", None) is not None:
            self.bdtopo.prefetch(keys)
        for src in self.side_sources:
            try:
                src.prefetch(keys)
            except Exception as e:      # (the props' own read later says why)
                log.debug("mapdata side prefetch %s: %s: %s", type(src).__name__, type(e).__name__, e)

    def predownload(self, key, layer):
        """A saved area's cell layer (areas.py): built from its sources at netfetch "predownload" and stored, unless it
        is on disk; not loaded or mirrored (the session's cells keep the mirror). True when built now. Raises the
        sources' netfetch errors."""
        key = _key(key)
        if layer == "buildings" and getattr(self, "bdtopo", None) is not None and self.bdtopo.wanted(key):
            self.bdtopo.load(key, "predownload")    # (a saved area: France's measured buildings with it)
        path = self.path(key, layer)
        if os.path.exists(path):
            return False
        with self._key_lock(key, layer):
            got, mine = xproc.once(path + ".lock", lambda: path if os.path.exists(path) else None, BUILD_WAIT_S,
                                   "predownload")
            if got is not None:
                return False
            try:
                arrays = self._build(key, layer, "predownload")
                dest = path
                if arrays.pop("_fallback", None) is not None:
                    dest = self.path(key, layer, parquet=True)      # (as layer_arrays: with the parquet cells)
                    if os.path.exists(dest):
                        return False
                self._store(dest, arrays)
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



# A mirrored layer's arrays in one file (PACK_FILE, each at a PACK_ALIGN offset; the "ok" marker's header: name, dtype,
# shape, offset), mapped once: one .npy per array (~30 a layer) was an open, a stat and a mapping each, ~11 s of a road
# process's 4 Manhattan geotiles (2026-11-02), and ~30x the files for prune_mirror's walk.
PACK_FILE = "pack.bin"
PACK_MARK = "#pack1"
PACK_ALIGN = 64


def _pack(path, arrays):
    """arrays written to path (PACK_*): (header, bytes of the arrays)."""
    header, off = [], 0
    with open(path, "wb") as fh:
        for n, a in arrays.items():
            a = np.asarray(a)
            if not a.flags.c_contiguous:    # (not ascontiguousarray: it makes a 0-d array 1-d)
                a = a.copy(order="C")
            if a.dtype.hasobject:
                raise ValueError(f"mapdata mirror: {n} holds objects")
            pad = (-off) % PACK_ALIGN
            if pad:
                fh.write(b"\0" * pad)
                off += pad
            header.append([n, np.lib.format.dtype_to_descr(a.dtype), list(a.shape), off])
            fh.write(a.tobytes())
            off += a.nbytes
    return header, off


def _unpack(path, header):
    """{name: array} of a PACK_FILE (its header): views of one read-only mapping, the small ones (MIRROR_SMALL)
    copied into memory as a plain load gave them (writable)."""
    import mmap
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) if size else None
    out = {}
    for n, descr, shape, off in header:
        dt = np.lib.format.descr_to_dtype(descr if isinstance(descr, str) else [tuple(x) for x in descr])
        count = int(np.prod(shape)) if shape else 1
        nbytes = count * dt.itemsize
        if off + nbytes > size:
            raise ValueError(f"mapdata mirror: {n} past the end of {path}")
        if nbytes == 0 or mm is None:
            a = np.empty(shape, dt)
        else:
            a = np.frombuffer(mm, dt, count, off).reshape(shape)
            if nbytes <= MIRROR_SMALL:
                a = a.copy()
        out[n] = a
    return out


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
    lat = np.degrees(dnp.arctan(dnp.sinh(np.pi * (1 - 2 * ty / n))))
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
    ap.add_argument("--release", default=None,
                    help="Overture release (default: overture.effective(): BNGE_OVERTURE_RELEASE, else the pinned one)")
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
                              "schemas": list(overture.SUPPORTED_SCHEMAS),
                              "update": "the data repo's cdn.json {\"overture\": {\"release\": ...}} once the "
                                        "release's products are built (or overture.RELEASE, the fallback); cells "
                                        "are cached per release"}))
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
