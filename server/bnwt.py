"""BNWT: our own prebaked z14 input tiles ("BeamNG.Earth world input tile"; research
.claude/work/expo-input-tiles-2026-11-12.md), read in place of the sources' own reads for the layers an archive holds
(ARCHIVES): the base theme (water, land use, infrastructure: ~10 MB parquet row groups, a rural place fetched 33.8 MB
of them for 37 KB of content), transportation (roads, connectors, route numbers), buildings (and their parts), places.

A tile holds, per layer, exactly what the runtime's own source gives for the cell, built by tools/build_bnwt.py through
that same code: overture.Reader.query for the parquet layers (base), pmtiles.Reader.query for the layers Overture's
tiles give (the others; their archive names the tiles' FORMAT it was built from). The codec (earthfast bnwt.rs) is
lossless, so a cell from a tile is the source's cell bit for bit and shares its cache file, its cache keys and every
plan and cell fact built from it; only the transport changes: one range read for a block of cells. (One decided
exception: places' src_record is kept empty: no code reads it, it was 15 % of the places' bytes.)

Off unless BNGE_BNWT_URL names where the archives are (an http(s) base URL, file:// or a folder): an archive of a release
is <base>/bnwt<FORMAT>/<release>/<name>.c<CONTENT>.index.json, its coverage and its parts (PMTiles v3 files of at most
~400 MB, so a CDN caches each whole, each a contiguous range of Hilbert tile ids: every quadtree square of cells is one
byte range of a part, or of two). A cell with no tile reads as its source's empty layer; tiles of the same content
(open sea: the ocean's pieces cut to the cell, nothing else) are kept once and decoded with the asked cell's own edges.
The source's own path stays the fallback for everything this cannot answer: no archive (404, network), another format,
content, release or source in its index, a cell outside its coverage, a layer it lacks, a damaged tile.

Cached under <raw>/bnwt/<release>/<name>/ (index.json; parts/<part>/head.bin, leaf/; tiles/14/<x>/<y>.bnwt: as stored,
b"" = no tile there). download_region() fetches a bbox's tiles as the few contiguous byte ranges they are (resumable:
tiles already on disk are not fetched again) and pins it (pins.json: never evicted by the cache cap);
`python3 bnwt.py region --bbox ...` from the command line.
"""
import argparse
import bisect
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import fast
import mapcells
import mapgeom as mg
import netfetch
import overture
import pmtiles

log = logging.getLogger("earthd.mapdata")

NATIVE = 273
FORMAT = 2              # the tile codec (earthfast bnwt.rs FORMAT): a reader reads only its own format's archives
ZOOM = 14
PMT = f"pmtiles{pmtiles.FORMAT}"
# name -> layers, the source they are the output of, the licence section (build_bnwt.SECTIONS), the columns kept empty
ARCHIVES = {
    "base": dict(layers=("water", "land_use", "infrastructure"), source="parquet", section="overture-odbl"),
    "transportation": dict(layers=("roads", "connectors", "road_routes"), source=PMT, section="overture-odbl"),
    "buildings": dict(layers=("buildings", "building_parts"), source=PMT, section="overture-odbl"),
    "places": dict(layers=("places",), source=PMT, section="overture-places", blank={"places": ("src_record",)}),
}
# What the tiles hold: the source's output for their layers. Bump an archive's with any change of that output (the
# columns, the query, the clipping, the tiles' decode: tests/test_bnwt.py ContentGuard): its archives are rebuilt, the
# old ones not read
CONTENT = {"base": 1, "transportation": 1, "buildings": 1, "places": 1}
LAYERS = tuple(ly for v in ARCHIVES.values() for ly in v["layers"])
ARCHIVE_OF = {ly: name for name, v in ARCHIVES.items() for ly in v["layers"]}
BASE_URL = (os.environ.get("BNGE_BNWT_URL") or "").strip().rstrip("/")
LEVEL = 19              # zstd level of the tiles
HEAD_BYTES = 16384
RETRY_S = 600.0         # an archive that could not be read (404, network, wrong version) is asked again after this
REGION_CHUNK = 8 << 20  # bytes a region download reads at once
BLOCK_ZOOM = 12         # a cell not on disk comes with its aligned 4 x 4 block: one contiguous range of the archive
                        # (a first visit plans a cell's rings; the base theme's block is ~0.1 MB in the Alps, ~2.5 in
                        # Midtown)
PINS = "pins.json"
LANE = "bnwt"            # netfetch's lane of the archive host (its policy: a short connect timeout, 2 tries)


class Unavailable(IOError):
    """The archive cannot answer this cell (the caller reads the source itself)."""


_NOT_ITS_FAULT = (netfetch.Deferred, netfetch.PolicyDenied, netfetch.BudgetExceeded)


def enabled():
    return bool(BASE_URL)


def source_of(layer, pmt):
    """The source the runtime reads layer from: PMT when Overture's tiles give it (pmt: mapdata's _pmt), else parquet."""
    return PMT if pmt else "parquet"


def index_url(base, release, name):
    return f"{base}/bnwt{FORMAT}/{release}/{name}.c{CONTENT[name]}.index.json"


# --- codec glue ---------------------------------------------------------------------------------
def _col(a):
    a = np.asarray(a)
    if a.dtype.kind == "U":
        return ("T", a.tolist(), max(1, a.dtype.itemsize // 4))
    a = np.ascontiguousarray(a)
    return ("N", a.dtype.str, a.dtype.itemsize, a.tobytes())


def _uncol(c):
    if c[0] == "T":
        return np.frombuffer(c[2], f"<U{c[1]}").copy()
    return np.frombuffer(c[2], np.dtype(c[1])).copy()


def section(layer, feats, sources):
    """The codec's input for one layer of a cell (a source's (Features, sources))."""
    return {"name": layer, "ids": _col(feats.ids), "kind": np.asarray(feats.kind, np.int64),
            "part_off": np.asarray(feats.part_off, np.int64), "ring_off": np.asarray(feats.ring_off, np.int64),
            "coord_off": np.asarray(feats.coord_off, np.int64),
            "lonlat": np.ascontiguousarray(np.asarray(feats.lonlat, np.float64).reshape(-1, 2)),
            "clipped": None if feats.clipped is None else _col(feats.clipped),
            "attrs": [(k, _col(v)) for k, v in feats.attrs.items()],
            "rules": [(k, [_col(r.feature), _col(r.value), _col(r.start), _col(r.end)]) for k, r in feats.rules.items()],
            "sources": [(ds, lic, int(n)) for (ds, lic), n in sources.items()]}


def empty(name, layer):
    """What the archive's source gives a cell without features (a cell with no tile reads as this)."""
    if ARCHIVES[name]["source"] == "parquet":
        return overture.empty(layer), {}
    spec = overture.LAYERS[layer]
    tz = pmtiles.THEME_ZOOM[spec["theme"]]
    r = fast._ef.pmt_layer(layer, spec["type"], [], (ZOOM, 0, 0), tz, text=list(spec.get("text", ())),
                           float=list(spec.get("float", ())), boolean=list(spec.get("bool", ())),
                           clip=bool(spec["clip"]), road_rules=layer == "roads",
                           src_own=layer in ("roads", "road_routes"), cap=pmtiles.CAP[tz],
                           fetch=lambda need: [b"" for _ in need], max_rounds=pmtiles.MAX_ROUNDS, cache_key="bnwt-empty")
    return pmtiles.Reader._features(layer, spec, r)


def blanked(name, layer, got):
    """got (Features, sources) with the archive's blank columns emptied (places' src_record), as its tiles hold it."""
    cols = ARCHIVES[name].get("blank", {}).get(layer, ())
    f = got[0]
    for c in cols:
        if c in f.attrs:
            f.attrs[c] = mg.text_column([""] * len(f))
    return got


def encode_tile(key, layers, level=LEVEL):
    """(bytes, independent) of cell key's tile from {layer: (Features, sources)} (each layer whole; independent: the
    bytes hold nothing of the cell itself, the same in every cell with these features)."""
    fast.require(NATIVE, "bnwt")
    secs = [section(ly, f, s) for ly, (f, s) in layers.items()]
    return fast._ef.bnwt_encode_tile(secs, tuple(key.bounds()), level)


def decode_layer(tile, layer, key):
    """(Features, sources) of one layer of cell key's tile, or None when the tile has no such section."""
    fast.require(NATIVE, "bnwt")
    d = fast._ef.bnwt_decode_layer(tile, layer, tuple(key.bounds()))
    if d is None:
        return None
    f = mg.Features(layer, _uncol(d["ids"]), np.asarray(d["kind"]).astype(np.uint8), d["part_off"], d["ring_off"],
                    d["coord_off"], d["lonlat"])
    f.clipped = None if d["clipped"] is None else _uncol(d["clipped"])
    for k, c in d["attrs"]:
        f.attrs[k] = _uncol(c)
    for k, cols in d["rules"]:
        f.rules[k] = mg.RuleTable(*[_uncol(c) for c in cols])
    return f, {(ds, lic): int(n) for ds, lic, n in d["sources"]}


def differences(a, b):
    """What differs between two (Features, sources) (empty: identical arrays, dtypes and order): the builder's check
    of each tile it writes, and the tests'."""
    (fa, sa), (fb, sb) = a, b
    out = []

    def arr(name, x, y):
        if x is None or y is None:
            if (x is None) != (y is None):
                out.append(f"{name}: one is None")
            return
        x, y = np.asarray(x), np.asarray(y)
        if x.dtype != y.dtype or x.shape != y.shape or x.tobytes() != y.tobytes():
            out.append(f"{name}: {x.dtype}{x.shape} vs {y.dtype}{y.shape}")
    for k in ("ids", "kind", "part_off", "ring_off", "coord_off", "lonlat", "clipped"):
        arr(k, getattr(fa, k), getattr(fb, k))
    if list(fa.attrs) != list(fb.attrs):
        out.append(f"attrs {list(fa.attrs)} vs {list(fb.attrs)}")
    for k in fa.attrs:
        arr(f"attr {k}", fa.attrs[k], fb.attrs.get(k))
    if list(fa.rules) != list(fb.rules):
        out.append(f"rules {list(fa.rules)} vs {list(fb.rules)}")
    for k in fa.rules:
        if k in fb.rules:
            for c in ("feature", "value", "start", "end"):
                arr(f"rule {k}.{c}", getattr(fa.rules[k], c), getattr(fb.rules[k], c))
    if list(sa.items()) != list(sb.items()):
        out.append(f"sources {sa} vs {sb}")
    return out


# --- archives -----------------------------------------------------------------------------------
def _local_path(url):
    if url.startswith("file://"):
        return url[len("file://"):]
    return None if "://" in url else url


def _read_range(fetcher, url, start, end, kind, etag=None, short_ok=False):
    """(bytes [start, end) of url, ETag): a file's own bytes, or one range request."""
    path = _local_path(url)
    if path is not None:
        try:
            with open(path, "rb") as f:
                f.seek(start)
                body = f.read(end - start)
        except OSError as e:
            raise Unavailable(f"{path}: {e}") from e
        if len(body) != end - start and not (short_ok and len(body) >= 127):
            raise pmtiles.PmtilesError(f"{path}: {len(body)} bytes for {start}-{end - 1}")
        return body, ""
    try:
        res = fetcher.get(url, kind=kind, headers={"Range": f"bytes={start}-{end - 1}"}, lane=LANE)
    except netfetch.NotFound as e:
        raise Unavailable(f"{url}: not found") from e
    n = len(res.body)
    if res.status != 206 or not (n == end - start or (short_ok and 127 <= n < end - start)):
        raise pmtiles.PmtilesError(f"{url}: HTTP {res.status}, {n} bytes for {start}-{end - 1}")
    tag = (res.headers.get("etag") or "").strip()
    if etag is not None and tag and tag != etag:
        raise pmtiles.Changed(f"{url}: ETag {tag}, the cached directories are {etag}")
    return res.body, tag


class Part(pmtiles.Archive):
    """One part file of an archive (its header and directories cached on disk; pmtiles.Archive's directory walk)."""

    def __init__(self, arch, i, file):
        super().__init__(arch.r, file)
        self.arch, self.i = arch, i
        self.url = f"{arch.dir_url}/{file}"
        self.dir = os.path.join(arch.dir, "parts", file.rsplit(".", 1)[0])

    def _get(self, start, end, kind, etag=None, short_ok=False):
        try:
            return _read_range(self.r.fetcher, self.url, start, end, kind, etag, short_ok)
        except pmtiles.Changed:
            self._reset()
            raise

    def head(self, kind):
        with self._lock:
            if self._head is not None:
                return self._head
        b = pmtiles._read(os.path.join(self.dir, "head.bin"))
        tag = None
        if b is None:
            b, tag = self._get(0, HEAD_BYTES, kind, short_ok=True)
            pmtiles._write(os.path.join(self.dir, "head.bin"), b)
            pmtiles._write(os.path.join(self.dir, "head.json"), json.dumps({"etag": tag}).encode())
        else:
            try:
                with open(os.path.join(self.dir, "head.json"), encoding="utf-8") as f:
                    tag = json.load(f).get("etag")
            except (OSError, ValueError):
                tag = None
        h = fast._ef.pmt_header(b)
        if h["internal_compression"] not in (1, 2) or h["tile_compression"] != 1 or h["tile_type"] != 0:
            raise Unavailable(f"{self.url}: not a BNWT part")
        ro, rl = h["root_offset"], h["root_length"]
        root = b[ro:ro + rl] if ro + rl <= len(b) else self._get(ro, ro + rl, kind, tag or None)[0]
        got = (h, root, tag or None)
        with self._lock:
            self._head = got
        return got


class Archive:
    """One archive of a release: its index (coverage, parts by tile-id range), the parts, the tiles on disk."""

    def __init__(self, reader, name):
        self.r, self.name = reader, name
        self.dir_url = f"{reader.base}/bnwt{FORMAT}/{reader.release}"
        self.url = index_url(reader.base, reader.release, name)
        self.dir = os.path.join(reader.root, reader.release, name)
        self.meta = None
        self._cover = []
        self._parts = []                # [(first tile id, last tile id, Part)]
        self._firsts = []
        self._down = None               # (monotonic time, reason): could not be read
        self._lock = threading.Lock()

    def _tile_path(self, z, x, y):
        return os.path.join(self.dir, "tiles", str(z), str(x), f"{y}.bnwt")

    def open(self, kind):
        """The index read and checked (once; again RETRY_S after a failure); Unavailable when it cannot be used."""
        with self._lock:
            if self.meta is not None:
                return self
            down = self._down
            if down is not None and time.monotonic() - down[0] < RETRY_S:
                raise Unavailable(down[1])
            try:
                self._open(kind)
            except _NOT_ITS_FAULT:
                raise                       # (a prefetch the host had no room for, a budget: not the archive's fault)
            except (Unavailable, pmtiles.PmtilesError, netfetch.FetchError, ValueError, OSError, KeyError) as e:
                self._down = (time.monotonic(), f"{self.url}: {type(e).__name__}: {e}")
                log.info("bnwt: %s unavailable (%s: %s): the source meanwhile", self.url, type(e).__name__, e)
                raise Unavailable(str(e)) from e
            self._down = None
            return self

    def _open(self, kind):
        path = os.path.join(self.dir, "index.json")
        b = pmtiles._read(path)
        if b is None:
            lp = _local_path(self.url)
            if lp is not None:
                try:
                    with open(lp, "rb") as f:
                        b = f.read()
                except OSError as e:
                    raise Unavailable(f"{lp}: {e}") from e
            else:
                try:
                    b = self.r.fetcher.get(self.url, kind=kind, lane=LANE).body
                except netfetch.NotFound as e:
                    raise Unavailable(f"{self.url}: not found") from e
            m = json.loads(b)
            self._check(m)
            pmtiles._write(path, b)
        m = json.loads(b)
        self._check(m)
        self._cover = [tuple(int(v) for v in r) for r in m["coverage"]]
        self._parts = [(int(p["first"]), int(p["last"]), Part(self, i, p["file"])) for i, p in enumerate(m["parts"])]
        self._firsts = [p[0] for p in self._parts]
        self.meta = m

    def _check(self, m):
        want = {"format": "bnwt", "version": FORMAT, "content": CONTENT[self.name], "release": self.r.release,
                "archive": self.name, "source": ARCHIVES[self.name]["source"]}
        bad = {k: m.get(k) for k, v in want.items() if m.get(k) != v}
        if bad:
            raise Unavailable(f"{self.url}: {bad}, {want} wanted")

    def covers_xy(self, t):
        return any(x0 <= t[1] <= x1 and y0 <= t[2] <= y1 for x0, y0, x1, y1 in self._cover)

    def has_layer(self, layer):
        return self.meta is not None and layer in self.meta.get("layers", ())

    def parts_in(self, a, b):
        """The parts holding tile ids of [a, b)."""
        i = max(0, bisect.bisect_right(self._firsts, a) - 1)
        out = []
        for first, last, part in self._parts[i:]:
            if first >= b:
                break
            if last >= a:
                out.append(part)
        return out


class Reader(pmtiles.Reader):
    """The layers of ARCHIVES per cell from the BNWT archives under base (BASE_URL), as their sources give them;
    Unavailable when an archive cannot answer."""

    def __init__(self, raw_dir, fetcher, release=overture.RELEASE, base=None):
        fast.require(NATIVE, "bnwt")
        self.base = BASE_URL if base is None else base.rstrip("/")
        self.root = os.path.join(raw_dir, "bnwt")
        self.fetcher, self.release = fetcher, release
        self._arch = {}
        self._lock = threading.Lock()
        self._mem = pmtiles.OrderedDict()
        self._mem_bytes = 0
        self.pool = ThreadPoolExecutor(pmtiles.FETCH_THREADS, thread_name_prefix="bnwt-fetch")
        self.stats = dict(cells=0, fallbacks=0, decode_s=0.0)
        self._inflight = {}             # (archive, square) -> Event: being read (fetch_squares)

    def archive(self, name):
        with self._lock:
            a = self._arch.get(name)
            if a is None:
                a = self._arch[name] = Archive(self, name)
            return a

    def query(self, layer, cell, kind="demand", source="parquet"):
        """(Features, sources) of layer in cell as its source (source_of) gives it; Unavailable when the archive
        cannot say."""
        name = ARCHIVE_OF.get(layer)
        if name is None or not self.base:
            raise Unavailable(f"{layer}: not in a BNWT archive")
        if ARCHIVES[name]["source"] != source:
            raise Unavailable(f"{layer}: the archive holds {ARCHIVES[name]['source']}'s cells, {source} asked")
        if cell.z != ZOOM:
            raise Unavailable(f"cell {cell}: the tiles are z{ZOOM}")
        arch = self.archive(name).open(kind)
        k = (ZOOM, cell.x, cell.y)
        if not arch.covers_xy(k):
            raise Unavailable(f"{cell}: outside {arch.url}'s coverage")
        if not arch.has_layer(layer):
            raise Unavailable(f"{arch.url} has no {layer}")
        tile = self._tile(arch, k, kind)
        t0 = time.perf_counter()
        got = None
        if tile:
            try:
                got = decode_layer(tile, layer, cell)
            except ValueError as e:     # (a damaged tile file: dropped, the source this time)
                with self._lock:
                    old = self._mem.pop((name, k), None)
                    if old is not None:
                        self._mem_bytes -= len(old)
                try:
                    os.remove(arch._tile_path(*k))
                except OSError:
                    pass
                raise Unavailable(f"{cell} {layer}: {e}") from e
        if got is None:
            got = empty(name, layer)        # (a fresh one each time: callers may change what they get)
        self.stats["decode_s"] += time.perf_counter() - t0
        self.stats["cells"] += 1
        return got

    def _tile(self, arch, k, kind):
        """Tile k's bytes (b"": none there): memory, disk, else its block (BLOCK_ZOOM) from the archive."""
        b = self.mem_get((arch.name, k))
        if b is None:
            path = arch._tile_path(*k)
            b = pmtiles._read(path)
            for sq in (_block(k), k):       # (its block; the cell alone when another thread's read of it failed)
                if b is not None:
                    break
                self.fetch_squares(arch, [sq], kind)
                b = pmtiles._read(path)
            if b is None:
                raise Unavailable(f"{k}: not read")
            self.mem_put((arch.name, k), b)
        return b

    def prefetch(self, needs, kind="prefetch"):
        """The blocks of the (cell, layer)s in needs not on disk fetched at once (blocks next to each other in the
        archive in one range read). Returns the (cell, layer)s the archives cover (on disk now); none when they are not
        there."""
        want = {}
        for cell, layer in needs:
            name = ARCHIVE_OF.get(layer)
            if name is None or not self.base or cell.z != ZOOM:
                continue
            want.setdefault(name, []).append((cell, layer))
        done = set()
        for name, items in want.items():
            try:
                arch = self.archive(name).open(kind)
            except Unavailable:
                continue
            items = [(c, ly) for c, ly in items if arch.covers_xy((ZOOM, c.x, c.y)) and arch.has_layer(ly)]
            ks = {(ZOOM, c.x, c.y) for c, _ly in items}
            new = [k for k in ks if not os.path.exists(arch._tile_path(*k))]
            if new:
                self.fetch_squares(arch, sorted({_block(k) for k in new}), kind)
            done.update(items)
        return done

    def fetch_squares(self, arch, squares, kind, stats=None, progress=None):
        """Every tile of the quadtree squares [(z, x, y)] on disk: each square is one contiguous run of tile ids,
        runs next to each other are read together, REGION_CHUNK bytes at most a request (a tile kept once for many
        cells is read once); a cell of the coverage with no tile gets an empty file (no directory walk for it later).
        Squares another thread is reading are waited for. stats: {"tiles", "fetched", "bytes", "requests"}."""
        stats = stats if stats is not None else dict(tiles=0, fetched=0, bytes=0, requests=0)
        mine, theirs = [], []
        with self._lock:
            for sq in sorted(set(squares)):
                key = (arch.name, sq)
                ev = self._inflight.get(key)
                if ev is None:
                    self._inflight[key] = threading.Event()
                    mine.append(sq)
                else:
                    theirs.append(ev)
        try:
            if mine:
                self._read_squares(arch, mine, kind, stats, progress)
        finally:
            with self._lock:
                for sq in mine:
                    self._inflight.pop((arch.name, sq)).set()
        for ev in theirs:
            ev.wait(120.0)
        return stats

    def _read_squares(self, arch, squares, kind, stats, progress):
        runs = sorted(fast._ef.bnwt_square_range(z, x, y, ZOOM) for z, x, y in squares)
        merged = []
        for a, b in runs:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        by_content = {}                 # (part, offset, length) -> [tile ids]
        have = set()
        for a, b in merged:
            for part in arch.parts_in(a, b):
                h, root, etag = part.head(kind)
                for t, off, ln in self._entries(part, h, root, etag, a, b, kind, 0):
                    have.add(t)
                    if not os.path.exists(arch._tile_path(*_zxy(t))):
                        by_content.setdefault((part.i, off, ln), []).append(t)
        stats["tiles"] += len(have)
        parts = {p.i: p for _f, _l, p in arch._parts}
        todo = sorted(by_content)
        i = 0
        while i < len(todo):                # (chunks of tiles near each other in one part)
            j = i + 1
            while j < len(todo) and todo[j][0] == todo[i][0] \
                    and todo[j][1] - (todo[j - 1][1] + todo[j - 1][2]) <= pmtiles.MERGE_GAP \
                    and todo[j][1] + todo[j][2] - todo[i][1] <= REGION_CHUNK:
                j += 1
            part = parts[todo[i][0]]
            a, b = todo[i][1], max(o + n for _p, o, n in todo[i:j])
            body, _ = part._get(a, b, kind, part.head(kind)[2])
            stats["requests"] += 1
            stats["bytes"] += len(body)
            for c in todo[i:j]:
                data = body[c[1] - a:c[1] - a + c[2]]
                for t in by_content[c]:
                    pmtiles._write(arch._tile_path(*_zxy(t)), data)
                    stats["fetched"] += 1
            if progress is not None:
                progress(stats)
            i = j
        for z, x, y in squares:             # (the coverage's cells without a tile: empty, known so)
            sh = ZOOM - z
            for cx in range(x << sh, (x + 1) << sh):
                for cy in range(y << sh, (y + 1) << sh):
                    t = (ZOOM, cx, cy)
                    if arch.covers_xy(t) and fast._ef.pmt_tile_id(ZOOM, cx, cy) not in have \
                            and not os.path.exists(arch._tile_path(*t)):
                        pmtiles._write(arch._tile_path(*t), b"")

    def _entries(self, part, h, d, etag, a, b, kind, depth):
        """[(tile id, absolute offset, length)] of the part's tiles with ids in [a, b), from directory d and its
        leaves."""
        if depth > 4:
            raise pmtiles.PmtilesError("directories deeper than 4 levels")
        tiles, leaves = fast._ef.bnwt_dir_range(d, a, b)
        out = [(t, h["data_offset"] + off, ln) for t, off, ln in tiles if a <= t < b]
        for _t, off, ln in leaves:
            out += self._entries(part, h, part._leaf(off, ln, kind, etag), etag, a, b, kind, depth + 1)
        return sorted(out)

    # --- region packs -------------------------------------------------------------------------
    def download_region(self, bbox, kind="predownload", progress=None, names=None):
        """Every tile of the archives (names: all) in bbox (lon0, lat0, lon1, lat1) on disk, pinned first (never
        evicted by the cache cap): the quadtree squares covering it, each one contiguous byte range (resumable: tiles
        already there are skipped). Returns {"tiles", "fetched", "bytes", "requests"}."""
        stats = dict(tiles=0, fetched=0, bytes=0, requests=0)
        lon0, lat0, lon1, lat1 = bbox
        nw, se = mapcells.cell_of(lat1, lon0, ZOOM), mapcells.cell_of(lat0, lon1, ZOOM)
        pin(self.root, (nw.x, nw.y, se.x, se.y))
        for name in names or ARCHIVES:
            try:
                arch = self.archive(name).open(kind)
            except Unavailable as e:
                log.info("bnwt: region: %s", e)
                continue
            self.fetch_squares(arch, _squares(nw.x, nw.y, se.x, se.y), kind, stats, progress)
        return stats


# --- pinned regions (cache.Cache._unpinned asks pinned_keys) ------------------------------------
def pin(root, rect):
    """Pin the z14 cells of rect (x0, y0, x1, y1) of every archive under root (<raw>/bnwt)."""
    path = os.path.join(root, PINS)
    try:
        with open(path, encoding="utf-8") as f:
            rects = [tuple(r) for r in json.load(f)["rects"]]
    except (OSError, ValueError, KeyError, TypeError):
        rects = []
    if tuple(rect) not in rects:
        rects.append(tuple(int(v) for v in rect))
        pmtiles._write(path, json.dumps({"rects": rects}).encode())


_PIN_MEMO = {}


def pinned_keys(cache_root, keys):
    """[bool] per cache key ("mapdata/raw/bnwt/..."): a tile of a pinned region, or an archive's index, head or
    directory while any region is pinned. None when nothing is pinned (the caller then skips it)."""
    path = os.path.join(cache_root, "mapdata", "raw", "bnwt", PINS)
    try:
        st = os.stat(path)
    except OSError:
        return None
    memo = _PIN_MEMO.get(path)
    if memo is None or memo[0] != (st.st_mtime_ns, st.st_size):
        try:
            with open(path, encoding="utf-8") as f:
                rects = [tuple(int(v) for v in r) for r in json.load(f)["rects"]]
        except (OSError, ValueError, KeyError, TypeError):
            rects = []
        memo = _PIN_MEMO[path] = ((st.st_mtime_ns, st.st_size), rects)
    rects = memo[1]
    if not rects:
        return None
    out = []
    for k in keys:
        if not k.startswith("mapdata/raw/bnwt/"):
            out.append(False)
            continue
        bits = k.split("/")
        if len(bits) >= 9 and bits[5] == "tiles":
            try:
                x, y = int(bits[7]), int(bits[8].split(".", 1)[0])
            except ValueError:
                out.append(False)
                continue
            out.append(any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in rects))
        else:
            out.append(True)            # (index, heads, directories: the pinned tiles need them offline)
    return out


# --- Hilbert helpers ----------------------------------------------------------------------------
def _block(k):
    sh = ZOOM - BLOCK_ZOOM
    return BLOCK_ZOOM, k[1] >> sh, k[2] >> sh


def _zxy(tile_id):
    """(z, x, y) of a z14 tile id (BNWT archives hold z14 only)."""
    return (ZOOM,) + _hilbert_xy(tile_id - _ACC14, ZOOM)


_ACC14 = sum(1 << (2 * t) for t in range(ZOOM))


def _hilbert_xy(d, z):
    """x, y of Hilbert index d at zoom z (the inverse of pmtiles.rs tile_id's curve)."""
    x = y = 0
    s = 1
    n = 1 << z
    t = d
    while s < n:
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        t //= 4
        s *= 2
    return x, y


def _squares(x0, y0, x1, y1, z=0, x=0, y=0):
    """The aligned quadtree squares (z, x, y) exactly covering the z14 cells [x0, x1] x [y0, y1]."""
    sh = ZOOM - z
    ax0, ay0 = x << sh, y << sh
    ax1, ay1 = ax0 + (1 << sh) - 1, ay0 + (1 << sh) - 1
    if ax1 < x0 or ax0 > x1 or ay1 < y0 or ay0 > y1:
        return []
    if (x0 <= ax0 and ax1 <= x1 and y0 <= ay0 and ay1 <= y1) or z == ZOOM:
        return [(z, x, y)]
    return [sq for dx in (0, 1) for dy in (0, 1) for sq in _squares(x0, y0, x1, y1, z + 1, 2 * x + dx, 2 * y + dy)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="BNWT archives: region download (resumable, pinned), archive info")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("region", help="fetch every tile of a bbox into the cache (pinned)")
    r.add_argument("--bbox", required=True, help="lon0,lat0,lon1,lat1")
    r.add_argument("--archives", default="", help="comma-separated (default: all)")
    i = sub.add_parser("info", help="the archives' indexes")
    for p in (r, i):
        p.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/beamng-earth"))
        p.add_argument("--url", default=BASE_URL, help="base URL (default: BNGE_BNWT_URL)")
        p.add_argument("--release", default=overture.RELEASE)
    a = ap.parse_args(argv)
    if not a.url:
        ap.error("no base URL (--url or BNGE_BNWT_URL)")
    rd = Reader(os.path.join(a.cache_dir, "mapdata", "raw"), netfetch.Fetcher(), a.release, a.url)
    if a.cmd == "info":
        for name in ARCHIVES:
            try:
                print(json.dumps({"archive": name, "index": rd.archive(name).open("demand").meta}, indent=1))
            except Unavailable as e:
                print(json.dumps({"archive": name, "unavailable": str(e)}))
        return 0
    bbox = tuple(float(v) for v in a.bbox.split(","))
    t0 = time.perf_counter()
    s = rd.download_region(bbox, kind="predownload", names=[n for n in a.archives.split(",") if n] or None,
                           progress=lambda s: print(f"\r{s['fetched']} tiles, {s['bytes'] / 1e6:.1f} MB",
                                                    end="", file=sys.stderr))
    print(file=sys.stderr)
    print(json.dumps(dict(s, seconds=round(time.perf_counter() - t0, 2))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
