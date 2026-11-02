"""Overture Maps GeoParquet per cell, through the STAC per-file index and DuckDB (mapdata.py).

docs/research/2026-10-10-world-data-sources.md §2.1: a release is ~1,000 zstd GeoParquet files
on S3 (Azure mirror), spatially sorted. Querying the whole release glob makes DuckDB read every
file's footer (35 s for a 2.4 km box); the release's STAC collection lists each file's bbox, so
only the 1-4 files that intersect the cell are opened, and inside them DuckDB skips the row groups
whose bbox statistics miss the cell. Every byte comes through rangeread (netfetch's limits, the
block cache): DuckDB runs with no extensions loaded or installed and opens no socket itself; the
geometry arrives as WKB (DuckDB core's ST_AsWKB) and is decoded in mapgeom.

STAC files (stac.overturemaps.org/<release>/<theme>/<type>/collection.json and one item per file
for its URLs and size) are cached under <raw>/overture/<release>/stac/; parquet blocks under
<raw>/overture/<release>/<theme>/<type>/<file>/. A release is immutable, so both are kept forever.

Layers (LAYERS: name -> theme, type, columns): roads (transportation/segment: class, subclass,
surface / flags / width / level / speed / subclass rules along the segment, connector ids with
their position), connectors (transportation/connector), buildings (buildings/building: height,
min_height, floors, roof and facade fields, class), water and land_use (base: subtype, class,
tags), infrastructure (base: power lines and plants, towers, barriers, bridges...). Rows are
deduplicated by id (highest version) and sorted by id, so a cell's content does not depend on
which file or row group a feature came from, or in which order they were read. Water and land-use
polygons are clipped to the cell (an ocean or a national park would otherwise repeat its whole
outline in every cell); everything else is whole (a building or road segment that crosses the
cell edge is listed in every cell it touches, `own` marks the one cell that owns it).
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import mapgeom as mg
import netfetch
import rangeread
import xproc
import staging

log = logging.getLogger("earthd.mapdata")

# The pinned Overture release (update path: docs/ARCHITECTURE.md "Map data"; `mapdata.py --latest`
# prints the newest one). Overture removes old releases from the buckets after some months: a
# pinned release that is gone raises ReleaseGone for cells not cached yet.
RELEASE = "2026-09-23.1"
STAC_ROOT = "https://stac.overturemaps.org"
DUCKDB_THREADS = 2              # DuckDB worker threads per query (the server is niced; the game comes first)
REMOTE_FILES = 32               # open RemoteFiles kept (their blocks share rangeread's memory LRU)
# DuckDB's own prefetching reads whole row groups one after the other; Reader._plan fetches what the
# query needs in parallel first, so DuckDB reads lazily from the block cache
PARQUET_CONFIG = {"disable_parquet_prefetching": True}
RG_DIR = "rows"                 # the row groups' local copies (Reader._local_rows), beside a file's blocks
RG_ROWS = 2048                  # rows a row group of a local copy: a cell's bbox filter skips the others by their stats
RG_WAIT_S = 60.0                # a copy another process makes is waited for this long
META_FILE = "rowgroups.json"     # a file's parsed row groups, beside its cached blocks (_meta_load)
BOX_ROWS = 2_000_000           # row groups' bbox rows kept per Reader (_plan: ~16 B a row as float32)
FETCH_THREADS = 8               # parallel range requests per Reader (Overture's hosts allow 8 at once each)
FOOTER_GUESS = 256 * 1024       # bytes read from a file's end first for its footer (one block)
MERGE_GAP = 64 * 1024           # column chunks closer than this are fetched in one range
BASE_COLUMNS = {"id", "geometry", "bbox", "sources", "version"}

# Overture's attribution (https://docs.overturemaps.org/attribution/, read 2026-10-11): the line its
# own viewer shows, and each theme's licence with the sources that ask for credit, whole (the CC BY
# sources at the end need their credit as much as the first ones; re-read it when RELEASE changes).
ATTRIBUTION = "© OpenStreetMap contributors, Overture Maps Foundation"
THEME_LICENCES = {
    "transportation": "ODbL. © OpenStreetMap contributors. Available under the Open Database License. "
                      "Data from TomTom.",
    "buildings": "ODbL. © OpenStreetMap contributors. Available under the Open Database License. Esri "
                 "Community Maps contributors. Available under CC BY 4.0. Global ML Building Footprints. "
                 "Licensed by Microsoft under the Open Database License. Google Open Buildings. Available "
                 "under CC BY 4.0. USGS 3D Elevation Program Digital Elevation Program. Qian Shi, et al. A "
                 "First High-quality Vector Data of Buildings in East Asian Countries Based on a Comprehensive "
                 "Large-scale Mapping Framework. Zenodo, 22 July 2023, doi:10.5281/zenodo.8174931. Available "
                 "under CC BY 4.0. Work derived from BTN 2024 ign.es. Available under CC BY 4.0",
    "base": "ODbL. © OpenStreetMap contributors. Available under the Open Database License. Data from the "
            "Daylight Map Distribution. ESA WorldCover. © ESA WorldCover project 2020 / Contains modified "
            "Copernicus Sentinel data (2020) processed by ESA WorldCover consortium. Available under CC BY "
            "4.0 DEED. Data products from ETOPO1. Available under Open Data Commons Public Domain Dedication "
            "and License. Data from GLOBathy. Available under CC0 1.0 (assumed).",
    # (read 2026-10-23)
    "places": "Data from Meta. Available under CDLA Permissive 2.0. Data from Microsoft. Available under CDLA "
              "Permissive 2.0. Data from PinMeTo. Available under CDLA Permissive 2.0. Data from Krick. Available "
              "under CDLA Permissive 2.0. Data from RenderSEO. Available under CDLA Permissive 2.0. Data from DAC. "
              "Available under CDLA Permissive 2.0. Data from BrightQuery. Available under CDLA Permissive 2.0. Data "
              "from Birdeye. Available under CDLA Permissive 2.0. Data from Uberall. Available under CDLA Permissive "
              "2.0. Data from OpenTravel Alliance. Available under CDLA Permissive 2.0. Data from Foursquare. "
              "Copyright 2024 Foursquare Labs, Inc. All rights reserved. Available under Apache 2.0. Foursquare data "
              "was transformed to the Overture schema. Changed: 2026-03-18. NOTICE.txt. Data from AllThePlaces. "
              "Available under CC0 1.0.",
}

_SRC = "sources[1].dataset AS src_dataset, sources[1].record_id AS src_record"
_NAME = "names.\"primary\" AS name"
_TAGS = "CAST(to_json(source_tags) AS VARCHAR) AS tags"
LAYERS = {
    "roads": dict(theme="transportation", type="segment", clip=False,
                  cols=f"subtype, class, subclass, {_NAME}, {_SRC}",
                  text=("subtype", "class", "subclass", "name", "src_dataset", "src_record"),
                  extra="road_flags, rail_flags, speed_limits, connectors, road_surface, width_rules, level_rules, "
                        "subclass_rules, access_restrictions",
                  rules={"surface": ("road_surface", "value", "text"),
                         "width": ("width_rules", "value", "float"),
                         "level": ("level_rules", "value", "float"),
                         "subclass": ("subclass_rules", "value", "text")}),
    "connectors": dict(theme="transportation", type="connector", clip=False, cols=_SRC,
                       text=("src_dataset", "src_record")),
    "buildings": dict(theme="buildings", type="building", clip=False,
                      cols=f"subtype, class, {_NAME}, {_SRC}, height, min_height, num_floors, "
                           "num_floors_underground, min_floor, level, is_underground, has_parts, roof_shape, "
                           "roof_height, roof_direction, roof_orientation, roof_material, roof_color, "
                           "facade_material, facade_color, "
                           "COALESCE(list_filter(sources, lambda s: s.property = '/properties/height')[1].dataset, "
                           "CASE WHEN height IS NOT NULL THEN sources[1].dataset END) AS height_src",
                      text=("subtype", "class", "name", "src_dataset", "src_record", "roof_shape",
                            "roof_orientation", "roof_material", "roof_color", "facade_material",
                            "facade_color", "height_src"),
                      float=("height", "min_height", "num_floors", "num_floors_underground", "min_floor",
                             "level", "roof_height", "roof_direction"),
                      bool=("is_underground", "has_parts")),
    # parts of buildings (towers on podiums, setbacks): each its own height range; buildings.py draws a
    # building's parts in place of its footprint (has_parts)
    "building_parts": dict(theme="buildings", type="building_part", clip=False,
                           cols=f"building_id, {_SRC}, height, min_height, num_floors, min_floor, roof_shape, "
                                "roof_height, facade_color, roof_color",
                           text=("building_id", "src_dataset", "src_record", "roof_shape", "facade_color",
                                 "roof_color"),
                           float=("height", "min_height", "num_floors", "min_floor", "roof_height")),
    "water": dict(theme="base", type="water", clip=True,
                  cols=f"subtype, class, {_NAME}, {_SRC}, level, is_salt, is_intermittent, {_TAGS}",
                  text=("subtype", "class", "name", "src_dataset", "src_record", "tags"),
                  float=("level",), bool=("is_salt", "is_intermittent")),
    "land_use": dict(theme="base", type="land_use", clip=True,
                     cols=f"subtype, class, {_NAME}, {_SRC}, level, surface, elevation, {_TAGS}",
                     text=("subtype", "class", "name", "src_dataset", "src_record", "surface", "tags"),
                     float=("level", "elevation")),
    "infrastructure": dict(theme="base", type="infrastructure", clip=False,
                           cols=f"subtype, class, {_NAME}, {_SRC}, level, height, surface, {_TAGS}",
                           text=("subtype", "class", "name", "src_dataset", "src_record", "surface", "tags"),
                           float=("level", "height")),
    # points of interest; read for the gas stations (gasstations.py, the user 2026-10-23: "we can already refuel in
    # beam so I want to include it on this impressive map mod"). Schema of 2026-09-23.1: basic_category and
    # taxonomy.primary (the old categories column is gone), brand.wikidata / brand.names.primary
    "places": dict(theme="places", type="place", clip=False,
                   cols=f"basic_category, taxonomy.\"primary\" AS taxonomy, brand.wikidata AS brand_wikidata, "
                        f"brand.names.\"primary\" AS brand_name, {_NAME}, {_SRC}, operating_status, confidence",
                   text=("basic_category", "taxonomy", "brand_wikidata", "brand_name", "name", "src_dataset",
                         "src_record", "operating_status"),
                   float=("confidence",)),
}
# road_flags: one row per flag (is_bridge, is_tunnel, is_link, is_covered, ...)
# speed_limits: max speed in km/h (mph converted) per range
MPH = 1.609344


class OvertureError(IOError):
    pass


class ReleaseGone(OvertureError):
    """The pinned release's STAC index is no longer published (bump overture.RELEASE)."""


class DuckDbMissing(OvertureError):
    """duckdb / fsspec are not installed (pip install -r server/requirements.txt)."""


def _duckdb():
    try:
        import duckdb
    except ImportError as e:
        raise DuckDbMissing("map data needs duckdb (pip install -r server/requirements.txt)") from e
    if rangeread.NetfetchFS is None:
        raise DuckDbMissing("map data needs fsspec (pip install -r server/requirements.txt)")
    return duckdb


def intersects(b, bounds):
    """STAC bbox [xmin, ymin, xmax, ymax] against (lon0, lat0, lon1, lat1), edges inclusive."""
    lon0, lat0, lon1, lat1 = bounds
    return b[0] <= lon1 and b[2] >= lon0 and b[1] <= lat1 and b[3] >= lat0


# The pinned release's whole index shipped with the app (tools/build_stac_index.py): the server never asks
# stac.overturemaps.org for it. That host was down for about an hour on 2026-10-26 and no fresh place got roads or
# buildings (GitHub #357); it also saves a first visit an item request per file.
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overture_stac_{release}.json.gz")


def _bundle(release):
    """{"theme/type": {"license", "files": [[name, bbox, urls, size], ...]}} of the release's shipped index, or {}."""
    import gzip
    try:
        with gzip.open(BUNDLED.format(release=release), "rt", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data.get("collections", {}) if data.get("release") == release else {}


class StacIndex:
    """The release's per-file index: which parquet files of a theme/type intersect a box."""

    def __init__(self, release, raw_dir, fetcher):
        self.release, self.raw_dir, self.fetcher = release, raw_dir, fetcher
        self._coll = {}
        self._lock = threading.Lock()
        self._bundled = _bundle(release)

    def _json(self, url, path, kind):
        try:
            with open(path, "rb") as f:
                return json.loads(f.read())
        except (OSError, ValueError):
            pass
        try:
            body = self.fetcher.get(url, kind=kind).body
        except netfetch.NotFound as e:
            raise ReleaseGone(f"Overture release {self.release} is not published any more ({url}); "
                              "bump overture.RELEASE (mapdata.py --latest)") from e
        data = json.loads(body)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        staging.replace_cached(tmp, path)
        return data

    def _dir(self, theme, typ):
        return os.path.join(self.raw_dir, "overture", self.release, "stac", theme, typ)

    def collection(self, theme, typ, kind="demand"):
        key = (theme, typ)
        with self._lock:
            c = self._coll.get(key)
        if c is None and f"{theme}/{typ}" in self._bundled:
            b = self._bundled[f"{theme}/{typ}"]
            c = {"items": [r[0] for r in b["files"]], "bboxes": [r[1] for r in b["files"]], "license": b.get("license"),
                 "rows": b["files"]}
            with self._lock:
                self._coll[key] = c
        if c is None:
            raw = self._json(f"{STAC_ROOT}/{self.release}/{theme}/{typ}/collection.json",
                             os.path.join(self._dir(theme, typ), "collection.json"), kind)
            items = [link["href"] for link in raw["links"] if link.get("rel") == "item"]
            bboxes = raw["extent"]["spatial"]["bbox"][1:]
            if len(items) != len(bboxes):
                raise OvertureError(f"STAC {theme}/{typ}: {len(items)} items but {len(bboxes)} file boxes")
            c = {"items": items, "bboxes": bboxes, "license": raw.get("license")}
            with self._lock:
                self._coll[key] = c
        return c

    def files(self, theme, typ, bounds, kind="demand"):
        """[(name, [aws url, azure url], size)] of the files whose bbox intersects `bounds`, in
        index order."""
        c = self.collection(theme, typ, kind)
        if "rows" in c:                         # (the shipped index: no item requests)
            return [(name, list(urls), int(size)) for name, bb, urls, size in c["rows"] if intersects(bb, bounds)]
        out = []
        for i, (href, bb) in enumerate(zip(c["items"], c["bboxes"])):
            if not intersects(bb, bounds):
                continue
            name = href.rstrip("/").rsplit("/", 1)[-1]
            item = self._json(href, os.path.join(self._dir(theme, typ), "items", name), kind)
            a = item["assets"]
            urls = [a["aws"]["href"]] + ([a["azure"]["href"]] if "azure" in a else [])
            out.append((urls[0].rsplit("/", 1)[-1], urls, int(a["aws"]["file:size"])))
        return out


META_FILES = 2048        # files whose parsed Parquet metadata Reader keeps (a few hundred row groups each)


def _q(path):
    """A path in a DuckDB string literal."""
    return str(path).replace("'", "''")


class Reader:
    """Overture layers per cell (see the module docstring). threads: DuckDB threads per query."""

    def __init__(self, raw_dir, fetcher, release=RELEASE, threads=DUCKDB_THREADS):
        self.raw_dir, self.fetcher, self.release, self.threads = raw_dir, fetcher, release, threads
        self.index = StacIndex(release, raw_dir, fetcher)
        self._remotes = OrderedDict()
        self._lock = threading.Lock()
        self.pool = ThreadPoolExecutor(FETCH_THREADS, thread_name_prefix="mapdata-fetch")
        # path -> [(row group, {"first", "n", "bbox", "cols"})]: a file's parsed Parquet metadata (_plan; a release's
        # files never change). Re-reading it for every cell was most of a map cell's CPU (the Deck's mapdata-layer
        # thread, 36 CPU-s of a teleport, 2026-10-23); osmtags._groups keeps its file's the same way.
        self._meta = OrderedDict()
        self._boxes = OrderedDict()     # (path, row group) -> its rows' bbox (x0, x1, y0, y1) arrays (_plan)
        self._box_rows = 0

    def close(self):
        self.pool.shutdown(wait=False)

    def _remote(self, theme, typ, name, urls, size):
        key = (theme, typ, name)
        with self._lock:
            r = self._remotes.get(key)
            if r is None:
                r = rangeread.RemoteFile(urls, size, os.path.join(self.raw_dir, "overture", self.release, theme,
                                                                  typ, name), self.fetcher)
                self._remotes[key] = r
                while len(self._remotes) > REMOTE_FILES:
                    self._remotes.popitem(last=False)
            self._remotes.move_to_end(key)
            return r

    @staticmethod
    def _meta_load(remote):
        """A file's parsed row groups kept beside its blocks (_meta_save), or None. (0.15 s a file in each of the
        server's processes, ~10-20 files a new place's layers.)"""
        try:
            with open(os.path.join(remote.cache_dir, META_FILE), encoding="utf-8") as f:
                return {int(rg): {"first": g["first"], "n": g["n"],
                                  "bbox": {k: tuple(v) for k, v in g["bbox"].items()},
                                  "cols": {k: [tuple(r) for r in v] for k, v in g["cols"].items()}}
                        for rg, g in json.load(f).items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    @staticmethod
    def _meta_save(remote, pg):
        try:
            os.makedirs(remote.cache_dir, exist_ok=True)
            path = os.path.join(remote.cache_dir, META_FILE)
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({str(rg): g for rg, g in pg.items()}, f)
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError, AttributeError) as e:
            log.debug("overture: row groups not saved: %s", e)

    def _box_get(self, k):
        with self._lock:
            got = self._boxes.get(k)
            if got is not None:
                self._boxes.move_to_end(k)
            return got

    def _box_put(self, k, arrays):
        with self._lock:
            if k in self._boxes:
                return
            self._boxes[k] = arrays
            self._box_rows += len(arrays[0])
            while self._box_rows > BOX_ROWS and len(self._boxes) > 1:
                _, old = self._boxes.popitem(last=False)
                self._box_rows -= len(old[0])

    def _connect(self, files, kind):
        """A DuckDB connection whose filesystem serves `files` ({path: RemoteFile}) with `kind`."""
        duckdb = _duckdb()
        errors = []

        def resolve(path):
            return files.get(path)

        fs = rangeread.NetfetchFS(resolve, kind)
        orig = fs._open

        def _open(path, mode="rb", **kw):
            f = orig(path, mode, **kw)
            fetch = f._fetch_range

            def guarded(start, end):
                try:
                    return fetch(start, end)
                except Exception as e:      # DuckDB turns it into a generic error: keep the real one
                    errors.append(e)
                    raise
            f._fetch_range = guarded
            return f
        fs._open = _open
        con = duckdb.connect(config={"threads": self.threads, "autoinstall_known_extensions": False,
                                     "autoload_known_extensions": False})
        for k, v in PARQUET_CONFIG.items():
            con.execute(f"SET {k} = {str(v).lower()}")
        con.register_filesystem(fs)
        return con, fs.protocol, errors

    def query(self, layer, cell, kind="demand"):
        """The layer's Features of one cell (lon/lat only; mapdata localises and marks `own`), and
        {(dataset, license): count} of the sources of its features."""
        spec = LAYERS[layer]
        bounds = cell.bounds()
        t0 = time.perf_counter()
        hits = self.index.files(spec["theme"], spec["type"], bounds, kind)
        t1 = time.perf_counter()
        if not hits:
            return empty(layer), {}
        files = {f"{spec['theme']}/{spec['type']}/{name}": self._remote(spec["theme"], spec["type"], name, urls, size)
                 for name, urls, size in hits}
        con, proto, errors = self._connect(files, kind)
        try:
            paths = sorted(files)
            rows = self._plan(con, proto, paths, files, spec, bounds, kind)
            if not rows:
                return empty(layer), {}         # no row of these files lies in the cell
            t2 = time.perf_counter()
            out = self._run(con, proto, paths, layer, spec, bounds, rows, files, kind)
            log.debug("mapdata %s %s: index %.2f s, plan %.2f s, query %.2f s", layer, cell, t1 - t0, t2 - t1,
                      time.perf_counter() - t2)
            return out
        except Exception as e:
            real = next((x for x in errors if isinstance(x, (netfetch.FetchError, rangeread.RangeError))), None)
            if real is None or isinstance(e, (netfetch.FetchError, rangeread.RangeError)):
                real = e
            if isinstance(real, rangeread.Missing):
                # a file the (cached) STAC index lists answers 404: Overture removed the release
                raise ReleaseGone(f"Overture release {self.release}: {real}; bump overture.RELEASE "
                                  "(mapdata.py --latest)") from real
            if real is not e:
                raise real from e
            raise
        finally:
            con.close()

    # --- fetch planning ------------------------------------------------------------------------
    def _parallel(self, tasks):
        """Run callables on the fetch pool; the first error is raised once all have finished."""
        futs = [self.pool.submit(t) for t in tasks]
        err = None
        for f in futs:
            try:
                f.result()
            except Exception as e:      # re-raised below, after the others finished
                err = err or e
        if err is not None:
            raise err

    @staticmethod
    def _ranges(chunks):
        """Byte ranges [start, end) merged where they touch or lie within MERGE_GAP."""
        out = []
        for a, b in sorted(chunks):
            if out and a - out[-1][1] <= MERGE_GAP:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out

    def _fetch(self, files, wanted, kind):
        """Fetch {path: [(start, end)]} in parallel (one task per merged range) into the block cache."""
        tasks = []
        for path, chunks in sorted(wanted.items()):
            r = files[path]
            for a, b in self._ranges(chunks):
                tasks.append(lambda r=r, a=a, b=b: r.blocks(a // rangeread.BLOCK, (b - 1) // rangeread.BLOCK + 1,
                                                            kind))
        self._parallel(tasks)

    def _plan(self, con, proto, paths, files, spec, bounds, kind):
        """Fetch, in parallel and before the real query, what it will read: the footers, then the
        bbox columns of the row groups whose statistics touch the cell, then every column the query
        uses of the row groups that hold a matching row; False when no row matches. DuckDB (its own sequential prefetching
        off) then reads from the block cache. A row group whose bbox statistics touch the cell but
        that holds no matching row costs only its bbox columns: in sparse areas row groups span
        hundreds of km (a desert cell's roads and water: 16 MB instead of 27 MB, 4 s instead of
        13 s with DuckDB reading lazily on its own)."""
        lon0, lat0, lon1, lat1 = bounds
        t0 = time.perf_counter()

        def footer(r):                  # the last 8 bytes: footer length + "PAR1"
            tail = r.read(max(0, r.size - FOOTER_GUESS), r.size, kind)
            n = int.from_bytes(tail[-8:-4], "little")
            if n + 8 > len(tail):
                r.read(r.size - n - 8, r.size, kind)
        self._parallel([lambda r=files[p]: footer(r) for p in paths])
        uris = ", ".join(f"'{proto}://{p}'" for p in paths)
        with self._lock:
            new = [p for p in paths if p not in self._meta]
        for p in list(new):             # parsed by another process or an earlier run (release files never change)
            pg = self._meta_load(files[p])
            if pg is not None:
                with self._lock:
                    self._meta[p] = pg
                new.remove(p)
        if new:
            meta = con.execute(f"""SELECT file_name, row_group_id, row_group_num_rows, path_in_schema, stats_min,
                stats_max, dictionary_page_offset, data_page_offset, total_compressed_size
                FROM parquet_metadata([{", ".join(f"'{proto}://{p}'" for p in new)}])
                ORDER BY file_name, row_group_id, column_id""").fetchall()
            parsed = {}                 # path -> {rg: {"first": row, "n", "bbox": {leaf: (min, max)}, "cols": {top: [range]}}}
            next_row = {}
            for fname, rg, nrows, col, smin, smax, dict_off, data_off, size in meta:
                path = fname.split("://", 1)[-1]
                pg = parsed.setdefault(path, {})
                g = pg.get(rg)
                if g is None:
                    first = next_row.get(path, 0)
                    next_row[path] = first + nrows
                    g = pg[rg] = {"first": first, "n": nrows, "bbox": {}, "cols": {}}
                offs = [o for o in (dict_off, data_off) if o]
                if not offs:
                    continue
                top, leaf = col.split(", ")[0], col.split(", ")[-1]
                g["cols"].setdefault(top, []).append((min(offs), min(offs) + size))
                if top == "bbox":
                    g["bbox"][leaf] = (smin, smax)
            for p in new:
                self._meta_save(files[p], parsed.get(p, {}))
            with self._lock:
                for p in new:
                    self._meta[p] = parsed.get(p, {})
                while len(self._meta) > META_FILES:
                    self._meta.popitem(last=False)
        with self._lock:
            groups = {}                 # (path, rg) -> {"first": row, "bbox": {leaf: (min, max)}, "cols": {top: [range]}}
            for p in paths:
                pg = self._meta.get(p)
                if pg is None:          # (evicted meanwhile by another thread's files: read again next time)
                    continue
                self._meta.move_to_end(p)
                for rg, g in pg.items():
                    groups[(p, rg)] = g

        def stat(g, leaf, i, default):
            try:
                return float(g["bbox"][leaf][i])
            except (KeyError, TypeError, ValueError):
                return default
        cand = [k for k, g in groups.items()
                if stat(g, "xmin", 0, -1e9) <= lon1 and stat(g, "xmax", 1, 1e9) >= lon0
                and stat(g, "ymin", 0, -1e9) <= lat1 and stat(g, "ymax", 1, 1e9) >= lat0]
        t1 = time.perf_counter()
        if not cand:
            return []
        # each row group's boxes decoded once (BOX_ROWS kept): a place's cells share their row groups (a fresh
        # place's first block: 69 queries over 210 row groups of 19, 2026-10-30), and the bbox pass was ~13 s of its
        # layer threads; compared in the column's own type, as DuckDB compares it
        need = [k for k in cand if self._box_get(k) is None]
        if need:
            wanted = {}
            for path, rg in need:
                wanted.setdefault(path, []).extend(groups[(path, rg)]["cols"].get("bbox", []))
            self._fetch(files, wanted, kind)
            by_path = {}
            for path, rg in need:
                by_path.setdefault(path, []).append(rg)
            for path, rgs in by_path.items():   # (a file at a time, by row number only: DuckDB skips the other row
                # groups then; with the files' names in the filter it read every row group's boxes, block by block)
                only = " OR ".join(f"file_row_number BETWEEN {groups[(path, rg)]['first']} AND "
                                   f"{groups[(path, rg)]['first'] + groups[(path, rg)]['n'] - 1}" for rg in rgs)
                got = con.execute(f"""SELECT file_row_number, bbox.xmin AS x0, bbox.xmax AS x1, bbox.ymin AS y0,
                    bbox.ymax AS y1 FROM read_parquet('{proto}://{path}', file_row_number = true)
                    WHERE {only}""").fetchnumpy()
                fr = np.asarray(got["file_row_number"], np.int64)
                cols = [np.ma.filled(np.ma.asarray(got[c]).astype(np.float64), np.nan).astype(np.asarray(got[c]).dtype)
                        for c in ("x0", "x1", "y0", "y1")]
                for rg in rgs:
                    g = groups[(path, rg)]
                    sel = (fr >= g["first"]) & (fr < g["first"] + g["n"])
                    self._box_put((path, rg), tuple(c[sel] for c in cols))
        matched = set()
        for k in cand:
            x0, x1, y0, y1 = self._box_get(k)
            t = x0.dtype.type
            if np.any((x0 <= t(lon1)) & (x1 >= t(lon0)) & (y0 <= t(lat1)) & (y1 >= t(lat0))):
                matched.add(k)
        used = set(re.findall(r"[A-Za-z_]+", spec["cols"] + " " + spec.get("extra", ""))) | BASE_COLUMNS
        wanted = {}
        for path, rg in sorted(matched):
            for top, chunks in groups[(path, rg)]["cols"].items():
                if top in used:
                    wanted.setdefault(path, []).extend(chunks)
        t2 = time.perf_counter()
        self._fetch(files, wanted, kind)
        log.debug("mapdata plan %s/%s: %d files, %d row groups, %d candidates, %d matched; footers %.2f s, "
                  "bbox %.2f s, data %.2f s (%.1f MB)", spec["theme"], spec["type"], len(paths), len(groups),
                  len(cand), len(matched), t1 - t0, t2 - t1, time.perf_counter() - t2,
                  sum(b - a for c in wanted.values() for a, b in self._ranges(c)) / 1e6)
        rows = []
        for path, rg in sorted(matched):
            g = groups[(path, rg)]
            rows.append((f"{proto}://{path}", g["first"], g["first"] + g["n"] - 1))
        return rows

    def _local_rows(self, con, layer, spec, rows, files, kind):
        """Local copies of the row groups rows ([(uri, first row, last row)]): each one's columns the layer's query
        reads, as one Parquet file beside the file's blocks (RG_DIR), made once (xproc: one process) and read by DuckDB
        itself. A place's cells share their row groups (a fresh place's first block: 55 queries decoded 117 row groups
        of 9 through the Python filesystem, 2026-10-30). Returns the files."""
        extra = f", {spec['extra']}" if spec.get("extra") else ""
        proj = (f"id, version, {spec['cols']}, ST_AsWKB(geometry) AS wkb, (bbox.xmin + bbox.xmax) / 2 AS cx, "
                f"(bbox.ymin + bbox.ymax) / 2 AS cy, sources{extra}, bbox")
        tag = hashlib.sha1(proj.encode()).hexdigest()[:12]
        out = []
        for u, a, b in rows:
            path = u.split("://", 1)[-1]
            d = os.path.join(files[path].cache_dir, RG_DIR)
            f = os.path.join(d, f"{layer}_{tag}_{int(a)}_{int(b)}.parquet")
            if not os.path.exists(f):
                got, mine = xproc.once(f + ".lock", lambda f=f: f if os.path.exists(f) else None, RG_WAIT_S, kind)
                if got is None:
                    try:
                        os.makedirs(d, exist_ok=True)
                        tmp = f"{f}.{os.getpid()}.{threading.get_ident()}.tmp"
                        con.execute(f"""COPY (SELECT {proj} FROM read_parquet('{_q(u)}', file_row_number = true)
                            WHERE file_row_number BETWEEN {int(a)} AND {int(b)}) TO '{_q(tmp)}' (FORMAT parquet, ROW_GROUP_SIZE {RG_ROWS})""")
                        os.replace(tmp, f)
                    finally:
                        if mine:
                            xproc.release(f + ".lock")
            out.append(f)
        return out

    def _run(self, con, proto, paths, layer, spec, bounds, rows, files=None, kind="demand"):
        lon0, lat0, lon1, lat1 = bounds
        src = ("read_parquet([" + ", ".join(f"'{proto}://{p}'" for p in paths) + "], filename = true, "
               "file_row_number = true)")
        # only the row groups the plan fetched (DuckDB would otherwise read every row group whose
        # statistics it cannot rule out, lazily, one request each)
        only = " OR ".join(f"(filename = '{u}' AND file_row_number BETWEEN {a} AND {b})" for u, a, b in rows)
        bbox = (f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND "
                f"bbox.ymin <= {lat1!r} AND bbox.ymax >= {lat0!r}")
        extra = f", {spec['extra']}" if spec.get("extra") else ""
        # The scan first, the dedup (by id, highest version) and numbering from the copy: with the
        # window in the same statement DuckDB 1.5 stopped skipping row groups by their statistics and
        # read every row group of the file between the matches (12 MB more, 9 s, for a city cell).
        local = None
        if files is not None:
            try:
                local = self._local_rows(con, layer, spec, rows, files, kind)
            except Exception as e:      # (a full disk, DuckDB's own errors: read from the files as before)
                log.debug("mapdata %s: local row groups: %s: %s", layer, type(e).__name__, e)
                local = None
        if local:
            con.execute(f"""CREATE TEMP TABLE s AS SELECT * EXCLUDE (bbox)
                  FROM read_parquet([{", ".join(f"'{_q(f)}'" for f in local)}]) WHERE {bbox}""")
        else:
            con.execute(f"""CREATE TEMP TABLE s AS
                  SELECT id, version, {spec['cols']}, ST_AsWKB(geometry) AS wkb,
                         (bbox.xmin + bbox.xmax) / 2 AS cx, (bbox.ymin + bbox.ymax) / 2 AS cy,
                         sources{extra}
                  FROM {src} WHERE ({only}) AND {bbox}""")
        con.execute("""CREATE TEMP TABLE t AS
            SELECT row_number() OVER (ORDER BY id) - 1 AS fi, * FROM (
              SELECT * EXCLUDE (version) FROM s
              QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC, wkb) = 1)""")
        names = ["id", "wkb", "cx", "cy"] + [c for k in ("text", "float", "bool") for c in spec.get(k, ())]
        rows = con.execute(f"SELECT {', '.join(names)} FROM t ORDER BY fi").fetchall()
        at = {c: i for i, c in enumerate(names)}
        ids = mg.text_column([r[at["id"]] for r in rows])
        geoms = [mg.parse_wkb(r[at["wkb"]]) for r in rows]
        clipped = np.zeros(len(rows), bool)
        if spec["clip"]:
            keep = []
            for i, (kind, parts) in enumerate(geoms):
                if kind == mg.POLYGON:
                    parts, changed = mg.clip_polygon_parts(parts, bounds)
                    geoms[i] = (kind, parts)
                    clipped[i] = changed
                keep.append(bool(parts))
            keep = np.array(keep, bool)
        else:
            keep = np.ones(len(rows), bool)
        feats = mg.build(layer, ids[keep], [g for g, k in zip(geoms, keep) if k])
        feats.clipped = clipped[keep]
        fmap = np.cumsum(keep) - 1          # old feature index -> new (rows dropped by clipping: unused)
        sel = [r for r, k in zip(rows, keep) if k]
        feats.attrs["cx"] = np.array([r[at["cx"]] for r in sel], np.float64)
        feats.attrs["cy"] = np.array([r[at["cy"]] for r in sel], np.float64)
        for c in spec.get("text", ()):
            feats.attrs[c] = mg.text_column([r[at[c]] for r in sel])
        for c in spec.get("float", ()):
            feats.attrs[c] = mg.float_column([r[at[c]] for r in sel])
        for c in spec.get("bool", ()):
            feats.attrs[c] = mg.bool_column([r[at[c]] for r in sel])
        if layer == "roads":
            self._road_rules(con, feats, keep, fmap)
        sources = {}
        for ds, lic, n in con.execute("""SELECT s.dataset, s.license, count(*) FROM
                (SELECT unnest(sources) AS s FROM t) GROUP BY ALL ORDER BY 1, 2""").fetchall():
            sources[(ds or "", lic or "")] = int(n)
        return feats, sources

    @staticmethod
    def _road_rules(con, feats, keep, fmap):
        def ranged(column, field_, kind):
            rows = con.execute(f"""SELECT fi, r.{field_}, r."between" FROM
                (SELECT fi, unnest({column}) AS r, generate_subscripts({column}, 1) AS k FROM t)
                ORDER BY fi, k""").fetchall()
            return mg.rule_table([(int(fmap[fi]), v, b) for fi, v, b in rows if keep[fi]], kind)
        for name, (column, field_, kind) in LAYERS["roads"]["rules"].items():
            feats.rules[name] = ranged(column, field_, kind)
        # (a rail segment's is_bridge / is_tunnel are in rail_flags: railways.py's bridges)
        rows = con.execute("""SELECT fi, f, r."between" FROM
            (SELECT fi, r, unnest(r."values") AS f, generate_subscripts(r."values", 1) AS j, k FROM
              (SELECT fi, unnest(road_flags) AS r, generate_subscripts(road_flags, 1) AS k FROM t
               UNION ALL
               SELECT fi, unnest(rail_flags) AS r, 1000 + generate_subscripts(rail_flags, 1) AS k FROM t))
            ORDER BY fi, k, j""").fetchall()
        feats.rules["flags"] = mg.rule_table([(int(fmap[fi]), f, b) for fi, f, b in rows if keep[fi]], "text")
        rows = con.execute("""SELECT fi, r.max_speed."value", r.max_speed.unit, r."between", r."when" IS NULL FROM
            (SELECT fi, unnest(speed_limits) AS r, generate_subscripts(speed_limits, 1) AS k FROM t)
            ORDER BY fi, k""").fetchall()
        feats.rules["max_speed_kmh"] = mg.rule_table(
            [(int(fmap[fi]), v * (MPH if (u or "").lower() == "mph" else 1.0), b)
             for fi, v, u, b, uncond in rows if keep[fi] and v is not None and uncond], "float")
        # one-way: motor traffic denied in one heading (no time, mode or vehicle conditions):
        # +1 = one-way along the geometry (backward denied), -1 = against it
        rows = con.execute("""SELECT fi, r.access_type, r."when".heading, r."when"."mode", r."when".during,
                r."when"."using", r."when".recognized, r."when".vehicle, r."between" FROM
            (SELECT fi, unnest(access_restrictions) AS r, generate_subscripts(access_restrictions, 1) AS k FROM t)
            ORDER BY fi, k""").fetchall()
        ow = []
        for fi, typ, heading, mode, during, using, recognized, vehicle, b in rows:
            if not keep[fi] or typ != "denied" or heading not in ("forward", "backward"):
                continue
            if during or using or recognized or vehicle:
                continue
            if mode and not any(m in ("motor_vehicle", "vehicle", "car") for m in mode):
                continue
            ow.append((int(fmap[fi]), 1.0 if heading == "backward" else -1.0, b))
        feats.rules["oneway"] = mg.rule_table(ow, "float")
        rows = con.execute("""SELECT fi, c.connector_id, c."at" FROM
            (SELECT fi, unnest(connectors) AS c, generate_subscripts(connectors, 1) AS k FROM t)
            ORDER BY fi, k""").fetchall()
        feats.rules["connectors"] = mg.rule_table(
            [(int(fmap[fi]), cid, [a, a]) for fi, cid, a in rows if keep[fi]], "text")


def empty(layer):
    """A layer without features, with the layer's columns and rule tables (same schema as a full one)."""
    spec = LAYERS[layer]
    f = mg.empty(layer)
    f.clipped = np.zeros(0, bool)
    f.attrs["cx"] = np.zeros(0, np.float64)
    f.attrs["cy"] = np.zeros(0, np.float64)
    for c in spec.get("text", ()):
        f.attrs[c] = mg.text_column([])
    for c in spec.get("float", ()):
        f.attrs[c] = mg.float_column([])
    for c in spec.get("bool", ()):
        f.attrs[c] = mg.bool_column([])
    if layer == "roads":
        for name, (_, _, kind) in spec["rules"].items():
            f.rules[name] = mg.rule_table([], kind)
        f.rules["flags"] = mg.rule_table([], "text")
        f.rules["max_speed_kmh"] = mg.rule_table([], "float")
        f.rules["connectors"] = mg.rule_table([], "text")
        f.rules["oneway"] = mg.rule_table([], "float")
    return f


def latest_release(fetcher, kind="demand"):
    """The newest release id in the STAC catalog (for the update path)."""
    return json.loads(fetcher.get(f"{STAC_ROOT}/catalog.json", kind=kind).body)["latest"]


def sources_provenance(theme, release, sources):
    """The provenance record of one theme's features in a cell."""
    return {"id": f"overture:{theme}", "name": "Overture Maps Foundation", "release": release,
            "attribution": ATTRIBUTION, "licence": THEME_LICENCES.get(theme, "ODbL"),
            "url": "https://docs.overturemaps.org/attribution/",
            "datasets": [{"dataset": d, "license": l, "features": n} for (d, l), n in sorted(sources.items())]}
