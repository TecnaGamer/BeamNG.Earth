"""Overture's own vector tiles (PMTiles v3 archives of z0-14 MVT) as the map cells' first source (mapdata.py,
BNGE_MAP_SOURCE=pmtiles, the default): one z14 tile is one of our cells, so a fresh cell's roads are one ~0.1-1 MB
range read and a native decode instead of DuckDB over ~10 MB parquet row groups (10-25 s cold, research
.claude/work/worldgen_speed_research.md §3.1).

Thin glue: the archive's bytes come through netfetch (the hosts' rate limits, slots, retries, circuit breakers,
offline mode, BNGE_NET_MBPS), kept on disk; everything that reads them is native (earthfast pmtiles.rs: header,
directories, MVT decode, the cell's features whole from the tiles they cross, the same columns and rules as
overture.Reader's SQL).

A cell's layer is one native call (pmt_layer): the cell's tile and its 8 neighbours first (points: the cell's tile
alone), then, through the fetch callback, the tiles the features crossing them still need (each tile one range read;
tiles near each other in the archive in one), at most MAX_ROUNDS rounds and CAP rings out.

  <raw>/overture-pmtiles/<release>/<theme>/head.bin        the archive's first 16 KB (header + root directory)
                                         /head.json        its ETag (every later read must match it)
                                         /leaf/<off>_<len>  leaf directories, as stored (gzip)
                                         /tiles/<z>/<x>/<y>.mvt  tiles, as stored (gzip; empty: none in the archive)
  <raw>/overture-pmtiles/mirrors.json                       the mirrors' order by their measured answer time

Mirrors (MIRRORS): Overture's tile CDN (tiles.overturemaps.org, the explorer's own: ~55 ms to the first byte from the
PC against ~170 ms from the bucket), the S3 bucket's regional and global names. Their order is measured once per
process (a 16 KB read of each, at once) and kept on disk for RANK_TTL_S; a mirror that fails goes last for
DEMOTE_S, its next request goes to the following one. A release's archives never change: a read whose ETag differs
from the header's drops the archive's cached directories and raises Changed (the cell then comes from parquet).

Mirrors measured from the PC 2026-11-11 (keep-alive, 200 KB reads at random offsets / 4 MB reads): tile CDN 55 ms to
the first byte, 17 MB/s; S3 regional 172 ms, 15-41 MB/s; S3 global name 162 ms, 16-25 MB/s. The GeoParquet copies
(rangeread, both used in turn): S3 137 ms, 16-39 MB/s; Azure 103 ms, 7-13 MB/s. Source Cooperative's Overture copy
(data.source.coop/fused/overture) stops at 2024 releases and the warehouse mirrors need accounts: not used.

Layers (LAYERS): those whose columns the tiles hold (overture.LAYERS: roads, connectors, road_routes from
transportation; buildings, building_parts; places; water, land_use, infrastructure from base, its z13 tiles, only with
BNGE_PMT_BASE=1). The divisions' layers read areas' whole bboxes and outlines a tile does not have: parquet.
"""
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import fast
import mapgeom as mg
import netfetch
import overture
import staging

log = logging.getLogger("earthd.mapdata")

FORMAT = 2          # the cells' content from the tiles: bump on any change of what pmt_layer gives (the cells' folder
                    # and MapData.versions() carry it: plans and cell facts keyed by it)
NATIVE = 206
MIRRORS = ("https://tiles.overturemaps.org/{release}/{theme}.pmtiles",
           "https://overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com/tiles/{release}/{theme}.pmtiles",
           "https://overturemaps-extras-us-west-2.s3.amazonaws.com/tiles/{release}/{theme}.pmtiles")
HOSTS = tuple(netfetch.host_of(m) for m in MIRRORS)
THEME_ZOOM = {"transportation": 14, "buildings": 14, "places": 14, "base": 13}
# The layers read from the tiles: every feature and attribute the parquet path gives (Fells Point, Midtown, ...:
# 2026-11-11 parity in the commit). The base theme's (water, land use, infrastructure) only with BNGE_PMT_BASE=1: its
# archive stops at z13, where Planetiler leaves out polygons under ~30 m and short lines (Fells Point: 55 of 191
# land-use areas, 120 of 1299 infrastructure features: storage tanks, piers, fences, walls), so they stay on parquet.
BASE_LAYERS = ("water", "land_use", "infrastructure")
BASE = os.environ.get("BNGE_PMT_BASE", "0") == "1"
LAYERS = ("roads", "connectors", "road_routes", "buildings", "building_parts", "places") + (BASE_LAYERS if BASE else ())
CAP = {14: 6, 13: 3}        # rings of tiles round the cell's a feature is followed into (beyond: as far as it got; a line
                            # is still owned by the cell holding its first vertex, so every cell agrees)
POINT_LAYERS = ("connectors", "places")    # (their cell's tile alone; the others ask its 8 neighbours with it: most
                                           # features that cross its edges end in them, one round instead of two)
MAX_ROUNDS = 8              # fetch rounds per layer (each the tiles the features still need)
HEAD_BYTES = 16384          # the first read of an archive: header (127 B) + root directory
MERGE_GAP = 64 * 1024       # tiles closer than this in the archive are read in one range
RANK_TTL_S = 6 * 3600.0
DEMOTE_S = 300.0
MEM_TILES_MB = 96           # tiles (as stored) kept in memory per process
FETCH_THREADS = 8


def source_key():
    """MapData.versions()' "mapsrc": the tiles' format and which layers come from them."""
    return f"pmtiles{FORMAT}" + ("+base" if BASE else "")


class PmtilesError(IOError):
    pass


class Changed(PmtilesError):
    """The archive on the server is not the version its cached directories are from."""


_FAILOVER = (netfetch.HostUnavailable, netfetch.RateLimited, netfetch.FetchFailed, PmtilesError)


def _write(path, b):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(b)
    staging.replace_cached(tmp, path)


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


class Mirrors:
    """The mirrors in their measured order (see the module docstring)."""

    def __init__(self, root, fetcher):
        self.root, self.fetcher = root, fetcher
        self._order = None
        self._lock = threading.Lock()
        self._lock2 = threading.Lock()
        self._down = {}                 # mirror index -> monotonic time it failed
        self.heads = {}                 # (release, theme) -> [(elapsed, first 16 KB, ETag)] read while measuring

    def order(self, release, kind):
        with self._lock:
            if self._order is None:
                self._order = self._load() or self._measure(release, kind)
            now = time.monotonic()
            up = [i for i in self._order if now - self._down.get(i, -1e9) >= DEMOTE_S]
            return up + [i for i in self._order if i not in up]

    def failed(self, i):
        with self._lock:
            self._down[i] = time.monotonic()

    def _path(self):
        return os.path.join(self.root, "mirrors.json")

    def _load(self):
        try:
            with open(self._path(), encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - float(d["t"]) < RANK_TTL_S and sorted(d["order"]) == list(range(len(MIRRORS))) \
                    and d.get("mirrors") == list(MIRRORS):
                return [int(i) for i in d["order"]]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def _measure(self, release, kind):
        """Each mirror's time for a 16 KB read of the transportation archive, at once; the order (failures last)."""
        def one(i):
            url = MIRRORS[i].format(release=release, theme="transportation")
            t0 = time.perf_counter()
            try:
                r = self.fetcher.get(url, kind=kind, headers={"Range": f"bytes=0-{HEAD_BYTES - 1}"})
                if r.status == 206 and r.body[:7] == b"PMTiles":
                    with self._lock2:       # (the fastest mirror's read serves as the archive's head: one round less)
                        self.heads.setdefault((release, "transportation"), []).append(
                            (r.elapsed, r.body, (r.headers.get("etag") or "").strip()))
                return i, (r.elapsed or time.perf_counter() - t0) if r.status == 206 else None
            except netfetch.FetchError as e:
                log.info("pmtiles: mirror %s: %s: %s", HOSTS[i], type(e).__name__, e)
                return i, None
        with ThreadPoolExecutor(len(MIRRORS)) as ex:
            got = list(ex.map(one, range(len(MIRRORS))))
        ms = {HOSTS[i]: (None if s is None else round(s * 1000.0, 1)) for i, s in got}
        order = [i for i, s in sorted((g for g in got if g[1] is not None), key=lambda g: (g[1], g[0]))]
        order += [i for i, s in got if s is None]
        log.info("pmtiles: mirrors by answer time %s", ms)
        try:
            _write(self._path(), json.dumps({"t": time.time(), "order": order, "mirrors": list(MIRRORS),
                                             "ms": ms}).encode())
        except OSError:
            pass
        return order


class Archive:
    """One theme's archive of a release (header, directories and tiles cached on disk)."""

    def __init__(self, reader, theme):
        self.r, self.theme = reader, theme
        self.dir = os.path.join(reader.root, reader.release, theme)
        self._lock = threading.Lock()
        self._head = None               # (header dict, root directory bytes, etag)
        self._leaves = OrderedDict()
        self._inflight = {}

    # --- bytes ------------------------------------------------------------------------------------
    def _get(self, start, end, kind, etag=None, short_ok=False):
        """Bytes [start, end) through the mirrors in turn: (body, etag). short_ok: fewer when the file ends sooner
        (the head read of an archive smaller than HEAD_BYTES)."""
        err = None
        for i in self.r.mirrors.order(self.r.release, kind):
            url = MIRRORS[i].format(release=self.r.release, theme=self.theme)
            try:
                res = self.r.fetcher.get(url, kind=kind, headers={"Range": f"bytes={start}-{end - 1}"})
                n = len(res.body)
                if res.status != 206 or not (n == end - start or (short_ok and 127 <= n < end - start)):
                    raise PmtilesError(f"{url}: HTTP {res.status}, {len(res.body)} bytes for {start}-{end - 1}")
                tag = (res.headers.get("etag") or "").strip()
                if etag is not None and tag and tag != etag:
                    self._reset()
                    raise Changed(f"{url}: ETag {tag}, the cached directories are {etag}")
                return res.body, tag
            except Changed:
                raise
            except netfetch.NotFound as e:
                raise PmtilesError(f"{url}: not found (release {self.r.release} gone?)") from e
            except _FAILOVER as e:
                self.r.mirrors.failed(i)
                err = e
        raise err if err is not None else PmtilesError("no mirror")

    def _reset(self):
        with self._lock:
            self._head = None
            self._leaves.clear()
        for name in ("head.bin", "head.json"):
            try:
                os.remove(os.path.join(self.dir, name))
            except OSError:
                pass

    def head(self, kind):
        with self._lock:
            if self._head is not None:
                return self._head
        b = _read(os.path.join(self.dir, "head.bin"))
        meta = None
        try:
            with open(os.path.join(self.dir, "head.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            b = None
        if b is None:
            self.r.mirrors.order(self.r.release, kind)          # (measured first: its reads may hold the head)
            with self.r.mirrors._lock2:
                seen = sorted(self.r.mirrors.heads.pop((self.r.release, self.theme), []), key=lambda h: h[0])
            if seen:
                _, b, tag = seen[0]
            else:
                b, tag = self._get(0, HEAD_BYTES, kind, short_ok=True)
            meta = {"etag": tag}
            _write(os.path.join(self.dir, "head.bin"), b)
            _write(os.path.join(self.dir, "head.json"), json.dumps(meta).encode())
        h = fast._ef.pmt_header(b)
        if h["internal_compression"] not in (1, 2) or h["tile_compression"] not in (1, 2) or h["tile_type"] != 1:
            raise PmtilesError(f"{self.theme}: unsupported archive (compression {h['internal_compression']}/"
                               f"{h['tile_compression']}, type {h['tile_type']})")
        ro, rl = h["root_offset"], h["root_length"]
        root = b[ro:ro + rl] if ro + rl <= len(b) else self._get(ro, ro + rl, kind, meta.get("etag"))[0]
        got = (h, root, meta.get("etag") or None)
        with self._lock:
            self._head = got
        return got

    def _leaf(self, off, ln, kind, etag):
        k = (off, ln)
        with self._lock:
            b = self._leaves.get(k)
            if b is not None:
                self._leaves.move_to_end(k)
                return b
        path = os.path.join(self.dir, "leaf", f"{off}_{ln}")
        b = _read(path)
        if b is None or len(b) != ln:
            h = self._head[0]
            b, _ = self._get(h["leaf_offset"] + off, h["leaf_offset"] + off + ln, kind, etag)
            _write(path, b)
        with self._lock:
            self._leaves[k] = b
            while len(self._leaves) > 256:
                self._leaves.popitem(last=False)
        return b

    def locate(self, z, x, y, kind):
        """(absolute offset, length) of tile z/x/y's bytes, or None when the archive has no such tile."""
        h, root, etag = self.head(kind)
        t = fast._ef.pmt_tile_id(z, x, y)
        d = root
        for _ in range(4):
            e = fast._ef.pmt_find(d, t)
            if e is None:
                return None
            off, ln, leaf = e
            if not leaf:
                return h["data_offset"] + off, ln
            d = self._leaf(off, ln, kind, etag)
        raise PmtilesError(f"{self.theme}: directories deeper than 4 levels")

    def _tile_path(self, z, x, y):
        return os.path.join(self.dir, "tiles", str(z), str(x), f"{y}.mvt")

    def tiles(self, zxys, kind):
        """{(z, x, y): tile bytes as stored (b"" when the archive has none)}: memory, disk, else the network, tiles
        near each other in the archive in one range."""
        out, todo = {}, []
        for k in sorted(set(zxys)):
            b = self.r.mem_get((self.theme, k))
            if b is None:
                b = _read(self._tile_path(*k))
                if b is not None:
                    self.r.mem_put((self.theme, k), b)
            if b is None:
                todo.append(k)
            else:
                out[k] = b
        if not todo:
            return out
        mine, theirs = [], []
        with self._lock:
            for k in todo:
                ev = self._inflight.get(k)
                if ev is None:
                    self._inflight[k] = threading.Event()
                    mine.append(k)
                else:
                    theirs.append((k, ev))
        try:
            if mine:
                out.update(self._fetch(mine, kind))
        finally:
            with self._lock:
                for k in mine:
                    self._inflight.pop(k).set()
        for k, ev in theirs:
            ev.wait(120.0)
            b = self.r.mem_get((self.theme, k))
            if b is None:
                b = _read(self._tile_path(*k))
            if b is None:
                b = self._fetch([k], kind)[k]
            out[k] = b
        return out

    def _fetch(self, keys, kind):
        h, _root, etag = self.head(kind)
        quiet = getattr(netfetch._quiet, "on", False)

        def pooled(fn, items):          # (on the fetch pool, with the caller's netfetch "quiet" state)
            def run(it):
                prev = getattr(netfetch._quiet, "on", False)
                netfetch._quiet.on = quiet
                try:
                    return fn(it)
                finally:
                    netfetch._quiet.on = prev
            return list(self.r.pool.map(run, items)) if len(items) > 1 else [fn(it) for it in items]
        # (their leaf directories at once: neighbours may sit in different leaves)
        where = dict(zip(keys, pooled(lambda k: self.locate(*k, kind), keys)))
        out = {k: b"" for k, w in where.items() if w is None}
        runs = []                       # [start, end, [(key, offset, length)]]
        for k, (off, ln) in sorted(((k, w) for k, w in where.items() if w is not None), key=lambda kv: kv[1]):
            if runs and off - runs[-1][1] <= MERGE_GAP:
                runs[-1][1] = max(runs[-1][1], off + ln)
                runs[-1][2].append((k, off, ln))
            else:
                runs.append([off, off + ln, [(k, off, ln)]])

        def one(run):
            a, b, items = run
            body, _ = self._get(a, b, kind, etag)
            return {k: body[off - a:off - a + ln] for k, off, ln in items}
        for got in pooled(one, runs):
            out.update(got)
        for k in keys:
            b = out[k]
            _write(self._tile_path(*k), b)
            self.r.mem_put((self.theme, k), b)
        return out


class Reader:
    """Overture layers per cell from the PMTiles archives (overture.Reader's query: the same Features and sources)."""

    def __init__(self, raw_dir, fetcher, release=overture.RELEASE):
        fast.require(NATIVE, "pmtiles")
        self.root = os.path.join(raw_dir, "overture-pmtiles")
        self.fetcher, self.release = fetcher, release
        self.mirrors = Mirrors(self.root, fetcher)
        self._arch = {}
        self._lock = threading.Lock()
        self._mem = OrderedDict()
        self._mem_bytes = 0
        self.pool = ThreadPoolExecutor(FETCH_THREADS, thread_name_prefix="pmtiles-fetch")

    def close(self):
        self.pool.shutdown(wait=False)

    def mem_get(self, k):
        with self._lock:
            b = self._mem.get(k)
            if b is not None:
                self._mem.move_to_end(k)
            return b

    def mem_put(self, k, b):
        with self._lock:
            old = self._mem.pop(k, None)
            if old is not None:
                self._mem_bytes -= len(old)
            self._mem[k] = b
            self._mem_bytes += len(b)
            while self._mem_bytes > MEM_TILES_MB * 1e6 and len(self._mem) > 1:
                _, o = self._mem.popitem(last=False)
                self._mem_bytes -= len(o)

    def archive(self, theme):
        with self._lock:
            a = self._arch.get(theme)
            if a is None:
                a = self._arch[theme] = Archive(self, theme)
            return a

    def tiles_for(self, layer, cell):
        """(theme, {(z, x, y)}) of the tiles query() reads first for the layer of cell (its tile, and its 8 neighbours
        but for POINT_LAYERS)."""
        spec = overture.LAYERS[layer]
        theme = spec["theme"]
        tz = THEME_ZOOM[theme]
        sh = cell.z - tz
        cx, cy = cell.x >> sh, cell.y >> sh
        n = 1 << tz
        ring = (0,) if layer in POINT_LAYERS else (-1, 0, 1)
        return theme, {(tz, cx + dx, cy + dy) for dx in ring for dy in ring if 0 <= cx + dx < n and 0 <= cy + dy < n}

    def prefetch(self, needs, kind="prefetch"):
        """The first tiles of every (cell, layer) in needs fetched at once, one archive at a time (Archive.tiles: those
        near each other in the archive in one range read), before the cells are decoded one by one: a round of cells
        ahead (earthd download_ahead) asked each cell's 3 x 3 tiles on its own, several range reads where one or two
        would do (the retro optimization study's section N: a round trip costs as much as ~1 MB). Returns the tiles
        asked for."""
        want = {}
        for cell, layer in needs:
            if layer not in LAYERS:
                continue
            theme, ts = self.tiles_for(layer, cell)
            want.setdefault(theme, set()).update(ts)
        n = 0
        for theme, ts in want.items():
            self.archive(theme).tiles(ts, kind)
            n += len(ts)
        return n

    def query(self, layer, cell, kind="demand"):
        """The layer's Features of one cell (lon/lat; mapdata localises and marks `own`) and {(dataset, license):
        count} of its features' sources, as overture.Reader.query."""
        spec = overture.LAYERS[layer]
        theme = spec["theme"]
        tz = THEME_ZOOM[theme]
        arch = self.archive(theme)
        sh = cell.z - tz
        cx, cy = cell.x >> sh, cell.y >> sh
        n = 1 << tz
        ring = (0,) if layer in POINT_LAYERS else (-1, 0, 1)
        want = {(tz, cx + dx, cy + dy) for dx in ring for dy in ring if 0 <= cx + dx < n and 0 <= cy + dy < n}
        t0 = time.perf_counter()
        first = arch.tiles(want, kind)

        def fetch(need):            # (the native loop's next round: the tiles its features still cross into)
            got = arch.tiles([tuple(t) for t in need], kind)
            return [got[tuple(t)] for t in need]
        r = fast._ef.pmt_layer(layer, spec["type"], [(z, x, y, b) for (z, x, y), b in sorted(first.items())],
                               (cell.z, cell.x, cell.y), tz, text=list(spec.get("text", ())),
                               float=list(spec.get("float", ())), boolean=list(spec.get("bool", ())),
                               clip=bool(spec["clip"]), road_rules=layer == "roads",
                               src_own=layer in ("roads", "road_routes"), cap=CAP[tz], fetch=fetch,
                               max_rounds=MAX_ROUNDS, cache_key=f"{self.release}/{theme}/{arch.head(kind)[2] or ''}")
        log.debug("pmtiles %s %s: %d tiles in %d rounds, %.2f s, %d partial", layer, cell, len(r["tiles"]),
                  r["rounds"], time.perf_counter() - t0, len(r["partial_ids"]))
        return self._features(layer, spec, r)

    @staticmethod
    def _features(layer, spec, r):
        f = mg.Features(layer, mg.text_column(r["ids"]), np.asarray(r["kind"], np.uint8),
                        np.asarray(r["part_off"], np.int64), np.asarray(r["ring_off"], np.int64),
                        np.asarray(r["coord_off"], np.int64), np.ascontiguousarray(r["lonlat"], np.float64))
        f.clipped = np.asarray(r["clipped"], bool)
        f.attrs["cx"] = np.asarray(r["cx"], np.float64)
        f.attrs["cy"] = np.asarray(r["cy"], np.float64)
        for c in spec.get("text", ()):
            f.attrs[c] = mg.text_column(r["text"][c])
        for c in spec.get("float", ()):
            f.attrs[c] = np.asarray(r["float"][c], np.float32)
        for c in spec.get("bool", ()):
            f.attrs[c] = np.asarray(r["bool"][c], bool)
        for name, (feat, val, start, end) in r["rules"].items():
            v = mg.text_column(val) if isinstance(val, list) else np.asarray(val, np.float32)
            f.rules[name] = mg.RuleTable(np.asarray(feat, np.int32), v, np.asarray(start, np.float32),
                                         np.asarray(end, np.float32))
        return f, {(ds, lic): int(n) for ds, lic, n in r["sources"]}
