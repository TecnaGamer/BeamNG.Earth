"""OpenStreetMap's road tags for a map cell from the weekly tag shards (tools/build_osm_tag_shards.py: a PMTiles
archive in parts of zoom-12 tiles, each the Layercake highways rows osmtags.py would read for its 16 cells), instead of the
29 GB Layercake parquet file itself.

Why (2026-11-11, the owner: "sub-second world gen, even with clear cache"): on a fresh cache every place's roads
waited 8-10 s for their tags (5 places' 9 cells, from the PC: 8.4-10.2 s, the first cell 7.9-10.1 s; a whole
MapData roads cell 8.5-13.4 s): the parquet file's 9.4 MB footer read and parsed by DuckDB in each of the 9 cells'
threads (~5 s), ~2.6 MB of tag columns per 123k-row group (1-5 a place), and a raceway's or busway's whole row-group
geometry (14-19 MB). From the shards the same 9 cells are 3-6 reads, 30-560 KB: 0.22-0.36 s with a CDN's 60 ms,
0.53-0.79 s at 150 ms and 20 Mbit/s (a local stand-in, 2026-11-11; the tags then wait on nothing else).

Thin glue: the bytes through netfetch, kept on disk per build; the tile's blob decoded natively (earthfast
osmtagshards.rs otg_decode, by the manifest's schema) into osmtags' row-group table, so the cell's tags, raceways and
busways, outlines and motorway geometry are picked by osmtags' own code (osmtags.from_tile / geometry_from_tile:
cell_rows, tags_and_ways, _lane_rows): the very values the parquet path gives for the same Layercake version (the
manifest's source_etag; 125 cells of 5 places compared equal, tags in the same order and types).

  <cache>/pagesdata/osmtags/manifest.json            the build in use (railtags.Shards: pinned first, refreshed daily)
  <cache>/pagesdata/osmtags/<build>/head.bin         the archive's first 16 KB (header + root directory), head.json
                                   /leaf/<off>_<len> leaf directories
                                   /p<i>/head.bin, /p<i>/leaf/...   the same of part i (a manifest with "parts")
                                   /tiles/<x>_<y>.otg  tiles as stored (raw deflate; empty: none in the archive)

The archive is in parts (the manifest's "parts": [{"archive", "first", "last", ...}], tile-id runs in that order, each
a whole PMTiles archive < 512 MB, so Cloudflare caches it: the one 2.4 GB archive answered every read from R2,
BYPASS, 2026-11-11); a tile is read from the part whose run holds its id. The first layout, one "archive", is read as a
single part. A part's URL is its "archive" (relative: beside the manifest). A part whose ETag changes under its cached
directories drops the build's cache (Changed) and the cell is asked of the Layercake file.

Off: BNE_OSMTAGS_SHARDS=off (or no manifest published, the archive unreachable, a tile that fails: DOWN_S): the cell's
tags come from the Layercake file as before (osmtags.OsmTags). BNE_OSMTAGS_BASE: the dataset's folder elsewhere (a
staged build).
"""
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict

import fast
import railtags

log = logging.getLogger("earthd.mapdata")

ON = os.environ.get("BNE_OSMTAGS_SHARDS", "on") != "off"
BASE = os.environ.get("BNE_OSMTAGS_BASE")   # the dataset's folder elsewhere than <BNE_DATA_PAGES>/osmtags (a staged build)
NAME = "osmtags"
NATIVE = 210
FORMAT = 1                  # the blob layout this reads (the manifest's "format")
HEAD_BYTES = 16384
DOWN_S = 600.0              # a failed read: the shards are not asked again for this long (the parquet path meanwhile)
MEM_TILES = 16              # decoded tiles kept per process (a place's 9 cells: 1-4 tiles)


class ShardError(IOError):
    pass


class NotInUse(ShardError):
    """No shards to read: off, none published (yet), or the manifest unreachable a moment ago."""


class Changed(ShardError):
    """The archive behind the URL is not the one whose head was cached."""


def schema_ok(s):
    """Whether a manifest's "schema" (the blob's columns) holds every column osmtags' table has, at its width."""
    import osmtags
    try:
        num = {str(c): int(w) for c, w in s["numeric"]}
        text, lines = [str(c) for c in s["text"]], [str(c) for c in s["lines"]]
    except (KeyError, TypeError, ValueError):
        return False
    want = {c: 4 if c in osmtags.EXACT32 else 8 for c in osmtags.COLUMNS
            if c not in osmtags.TEXT_COLUMNS and c not in ("bx0", "by0", "bx1", "by1")}
    return (all(num.get(c) == w for c, w in want.items()) and set(osmtags.TEXT_COLUMNS) <= set(text)
            and lines == list(osmtags.RACEWAY_COLUMNS[1:]))


def parts_of(m):
    """The archive's parts of manifest m, [(first tile id, last tile id or None, archive URL as given, cache subfolder)]
    in tile-id order, or None (no archive, or parts this cannot read). The first layout (one "archive") is one part
    of every tile id, cached where it was."""
    ps = m.get("parts")
    if ps is None:
        a = m.get("archive")
        return [(0, None, str(a), "")] if isinstance(a, str) and a else None
    if not isinstance(ps, list) or not ps:
        return None
    out = []
    for i, p in enumerate(ps):
        try:
            a, first, last = p["archive"], int(p["first"]), int(p["last"])
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(a, str) or not a or last < first or (out and first <= out[-1][1]):
            return None
        out.append((first, last, a, f"p{i}"))
    return out


def decode(b, m):
    """A tile's bytes as stored decoded by the manifest m's schema (earthfast otg_decode)."""
    s = m["schema"]
    return fast._ef.otg_decode(b, [(str(c), int(w)) for c, w in s["numeric"]], [str(c) for c in s["text"]],
                               [str(c) for c in s["lines"]])


def _write(path, b):
    import staging
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


class TagShards:
    def __init__(self, cache_dir, fetcher):
        fast.require(NATIVE, "osmtagshards")
        self.fetcher = fetcher
        self.pages = railtags.Shards(cache_dir, fetcher, NAME)
        if BASE:
            self.pages.base = BASE.rstrip("/")
        self._lock = threading.Lock()
        self._man = None            # (build, manifest dict; its "_parts": parts_of)
        self._heads = {}            # (build, part subfolder) -> (header dict, root bytes, etag)
        self._leaves = OrderedDict()
        self._mem = OrderedDict()   # (build, x, y) -> decoded tile
        self._inflight = {}
        self._keylocks = {}         # ("head" | "leaf", ...) -> Lock: one read of each, the other threads wait for it
        self._down = -1e9

    # --- the build in use ---------------------------------------------------------------------------------------
    def manifest(self):
        """(build, manifest) of the shards in use, or None (off, not published, unreachable a moment ago)."""
        if not ON or self.fetcher is None or time.monotonic() - self._down < DOWN_S:
            return None
        built = self.pages.version()
        if built is None:
            return None
        with self._lock:
            if self._man is not None and self._man[0] == built:
                return self._man
        try:
            with open(os.path.join(self.pages.dir, "manifest.json"), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError, TypeError):
            return None
        parts = parts_of(m) if isinstance(m, dict) else None
        if parts is None or m.get("built") != built or m.get("format") != FORMAT \
                or not isinstance(m.get("zoom"), int) or not 0 < m["zoom"] <= 14 or not schema_ok(m.get("schema")):
            return None
        m["_parts"] = parts
        m["_firsts"] = [p[0] for p in parts]
        got = (built, m)
        with self._lock:
            self._man = got
        return got

    def _dir(self, built):
        return os.path.join(self.pages.dir, re.sub(r"[^0-9A-Za-z]+", "_", built))

    def _url(self, part):
        a = part[2]
        return a if a.startswith(("http://", "https://")) else f"{self.pages.base}/{a.lstrip('/')}"

    def _pdir(self, built, part):
        return os.path.join(self._dir(built), part[3]) if part[3] else self._dir(built)

    @staticmethod
    def part_of(m, t):
        """The part of manifest m (manifest()'s) whose run holds tile id t, or None (in none: no such tile)."""
        import bisect
        i = bisect.bisect_right(m["_firsts"], t) - 1
        if i < 0:
            return None
        p = m["_parts"][i]
        return p if p[1] is None or t <= p[1] else None

    def down(self, e):
        log.warning("osmtags shards: %s: %s; the parquet file for %.0f min", type(e).__name__, e, DOWN_S / 60)
        self._down = time.monotonic()

    # --- the archive --------------------------------------------------------------------------------------------
    def _get(self, url, start, end, kind, etag=None, short_ok=False):
        r = self.fetcher.get(url, kind=kind, headers={"Range": f"bytes={start}-{end - 1}"})
        n = len(r.body)
        if r.status != 206 or not (n == end - start or (short_ok and 127 <= n < end - start)):
            raise ShardError(f"{url}: HTTP {r.status}, {n} bytes for {start}-{end - 1}")
        tag = (r.headers.get("etag") or "").strip()
        if etag and tag and tag != etag:
            raise Changed(f"{url}: ETag {tag}, its directories cached at {etag}")
        return r.body, tag

    def _forget(self, built):
        with self._lock:
            self._heads = {k: v for k, v in self._heads.items() if k[0] != built}
            self._leaves.clear()
            self._mem.clear()
        import shutil
        shutil.rmtree(self._dir(built), ignore_errors=True)

    def _once(self, key):
        with self._lock:
            lk = self._keylocks.get(key)
            if lk is None:
                lk = self._keylocks[key] = threading.Lock()
            return lk

    def head(self, built, part, kind):
        """(header, root directory, etag) of an archive part (parts_of's), read once and kept on disk."""
        k = (built, part[3])
        with self._lock:
            got = self._heads.get(k)
        if got is not None:
            return got
        with self._once(("head",) + k):
            with self._lock:
                got = self._heads.get(k)
            return got if got is not None else self._head_once(built, part, kind)

    def _head_once(self, built, part, kind):
        d = self._pdir(built, part)
        url = self._url(part)
        b = _read(os.path.join(d, "head.bin"))
        try:
            with open(os.path.join(d, "head.json"), encoding="utf-8") as f:
                etag = json.load(f).get("etag")
        except (OSError, ValueError):
            b = None
        if b is None:
            b, etag = self._get(url, 0, HEAD_BYTES, kind, short_ok=True)
            _write(os.path.join(d, "head.bin"), b)
            _write(os.path.join(d, "head.json"), json.dumps({"etag": etag}).encode())
        h = fast._ef.pmt_header(b)
        if h["internal_compression"] not in (1, 2) or h["tile_compression"] not in (1, 2):
            raise ShardError(f"unsupported archive (compression {h['internal_compression']}/{h['tile_compression']})")
        ro, rl = h["root_offset"], h["root_length"]
        root = b[ro:ro + rl] if ro + rl <= len(b) else self._get(url, ro, ro + rl, kind, etag)[0]
        got = (h, root, etag or None)
        with self._lock:
            self._heads[(built, part[3])] = got
        return got

    def _leaf(self, built, part, h, off, ln, kind, etag):
        k = (built, part[3], off, ln)
        with self._lock:
            b = self._leaves.get(k)
            if b is not None:
                self._leaves.move_to_end(k)
                return b
        with self._once(("leaf",) + k):
            with self._lock:
                b = self._leaves.get(k)
            if b is not None:
                return b
            path = os.path.join(self._pdir(built, part), "leaf", f"{off}_{ln}")
            b = _read(path)
            if b is None or len(b) != ln:
                b, _ = self._get(self._url(part), h["leaf_offset"] + off, h["leaf_offset"] + off + ln, kind, etag)
                _write(path, b)
            with self._lock:
                self._leaves[k] = b
                while len(self._leaves) > 64:
                    self._leaves.popitem(last=False)
            return b

    def _fetch(self, built, m, z, x, y, kind):
        """Tile z/x/y's bytes as stored (b"": none in the archive), read and kept on disk."""
        t = fast._ef.pmt_tile_id(z, x, y)
        part = self.part_of(m, t)
        body = b""
        if part is not None:
            h, root, etag = self.head(built, part, kind)
            d = root
            for _ in range(4):
                e = fast._ef.pmt_find(d, t)
                if e is None:
                    break
                off, ln, leaf = e
                if not leaf:
                    a = h["data_offset"] + off
                    body, _ = self._get(self._url(part), a, a + ln, kind, etag)
                    break
                d = self._leaf(built, part, h, off, ln, kind, etag)
            else:
                raise ShardError("directories deeper than 4 levels")
        _write(os.path.join(self._dir(built), "tiles", f"{x}_{y}.otg"), body)
        return body

    def tile(self, key, kind="demand"):
        """The decoded tile (otg_decode's dict, None: no tile there, the cell has no tagged highway) holding map
        cell key, or raises (ShardError, netfetch's errors); NotInUse when the shards are not in use."""
        got = self.manifest()
        if got is None:
            raise NotInUse("osmtags shards: none in use")
        built, m = got
        z = m["zoom"]
        if key.z < z:
            raise ShardError(f"cell zoom {key.z} above the shards' {z}")
        x, y = key.x >> (key.z - z), key.y >> (key.z - z)
        k = (built, x, y)
        with self._lock:
            if k in self._mem:
                self._mem.move_to_end(k)
                return self._mem[k]
            ev = self._inflight.get(k)
            mine = ev is None
            if mine:
                ev = self._inflight[k] = threading.Event()
        if not mine:
            ev.wait(120.0)
            with self._lock:
                if k in self._mem:
                    return self._mem[k]
        try:
            path = os.path.join(self._dir(built), "tiles", f"{x}_{y}.otg")
            b = _read(path)
            t = None
            if b:
                try:
                    t = decode(b, m)
                except ValueError as e:         # (damaged on disk: read again)
                    log.warning("osmtags shards: %s: %s; reading it again", path, e)
                    b = None
            if b is None:
                try:
                    b = self._fetch(built, m, z, x, y, kind)
                except Changed:
                    self._forget(built)
                    raise
                t = decode(b, m) if b else None
            with self._lock:
                self._mem[k] = t
                while len(self._mem) > MEM_TILES:
                    self._mem.popitem(last=False)
            return t
        finally:
            if mine:
                with self._lock:
                    self._inflight.pop(k, None)
                ev.set()

    def clear_memory(self):
        with self._lock:
            self._mem.clear()
            self._leaves.clear()
