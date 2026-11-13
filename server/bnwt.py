"""BNWT: our own prebaked z14 input tiles ("BeamNG.Earth world input tile"; research
.claude/work/expo-input-tiles-2026-11-12.md), read in place of the Overture parquet files for the layers an archive
holds (ARCHIVES: the base theme first: water, land use, infrastructure, which came from ~10 MB parquet row groups: a
rural place fetched 33.8 MB of them for 37 KB of content).

A tile holds, per layer, exactly what overture.Reader.query gives for the cell (built by tools/build_bnwt.py through
that same code): the codec (earthfast bnwt.rs) is lossless, so a cell from a tile is the parquet cell bit for bit and
shares its cache file, its cache keys and every plan and cell fact built from it. Only the transport changes: one
range read for a whole region of cells instead of row groups.

Off unless BNGE_BNWT_URL names where the archives are (an http(s) base URL, file:// or a folder): the archive of a
release is <base>/bnwt<FORMAT>/<release>/<name>.c<CONTENT>.pmtiles, a PMTiles v3 container (Hilbert order, clustered:
every quadtree square of cells is one contiguous byte range). The parquet path stays the fallback for everything this
cannot answer: no archive (404, network), another format, release or content version in its metadata, a cell outside
the archive's coverage, a layer it lacks, a damaged tile.

Cached under <raw>/bnwt/<release>/<name>/ as pmtiles.py does it (head.bin, leaf/, tiles/<z>/<x>/<y>.bnwt: as stored,
b"" = no tile there). download_region() fetches a bbox's tiles as the few contiguous byte ranges they are (resumable:
tiles already on disk are not fetched again); `python3 bnwt.py region --bbox ...` from the command line.
"""
import argparse
import gzip
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

NATIVE = 268
FORMAT = 1              # the tile codec (earthfast bnwt.rs FORMAT): a reader reads only its own format's archives
CONTENT = 1             # what the tiles hold: overture.Reader.query's output for their layers. Bump with any change of
                        # that output (columns, clipping, ordering): the archives are rebuilt, the old ones not read
ZOOM = 14
ARCHIVES = {"base": ("water", "land_use", "infrastructure")}
LAYERS = tuple(ly for v in ARCHIVES.values() for ly in v)
ARCHIVE_OF = {ly: name for name, v in ARCHIVES.items() for ly in v}
BASE_URL = (os.environ.get("BNGE_BNWT_URL") or "").strip().rstrip("/")
LEVEL = 19              # zstd level of the tiles
HEAD_BYTES = 16384
RETRY_S = 600.0         # an archive that could not be read (404, network, wrong version) is asked again after this
REGION_CHUNK = 8 << 20  # bytes a region download reads at once
BLOCK_ZOOM = 12         # a cell not on disk comes with its aligned 4 x 4 block: one contiguous range of the archive
                        # (a first visit plans a cell's rings; the base theme's block is ~0.1 MB in the Alps, ~2.5 in
                        # Midtown)


class Unavailable(IOError):
    """The archive cannot answer this cell (the caller reads the parquet files)."""


_NOT_ITS_FAULT = (netfetch.Deferred, netfetch.PolicyDenied, netfetch.BudgetExceeded)


def enabled():
    return bool(BASE_URL)


def archive_url(base, release, name):
    return f"{base}/bnwt{FORMAT}/{release}/{name}.c{CONTENT}.pmtiles"


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
    """The codec's input for one layer of a cell (overture.Reader.query's (Features, sources))."""
    return {"name": layer, "ids": _col(feats.ids), "kind": np.asarray(feats.kind, np.int64),
            "part_off": np.asarray(feats.part_off, np.int64), "ring_off": np.asarray(feats.ring_off, np.int64),
            "coord_off": np.asarray(feats.coord_off, np.int64),
            "lonlat": np.ascontiguousarray(np.asarray(feats.lonlat, np.float64).reshape(-1, 2)),
            "clipped": None if feats.clipped is None else _col(feats.clipped),
            "attrs": [(k, _col(v)) for k, v in feats.attrs.items()],
            "rules": [(k, [_col(r.feature), _col(r.value), _col(r.start), _col(r.end)]) for k, r in feats.rules.items()],
            "sources": [(ds, lic, int(n)) for (ds, lic), n in sources.items()]}


def encode_tile(key, layers, level=LEVEL):
    """A tile's bytes from {layer: (Features, sources)} of cell key (layers without features or sources left out:
    a missing section reads as overture.empty)."""
    fast.require(NATIVE, "bnwt")
    secs = [section(ly, f, s) for ly, (f, s) in layers.items() if len(f) or s]
    return fast._ef.bnwt_encode_tile(key.z, key.x, key.y, secs, level)


def decode_layer(tile, layer):
    """(Features, sources) of one layer of a tile, or None when the tile has no such section."""
    fast.require(NATIVE, "bnwt")
    d = fast._ef.bnwt_decode_layer(tile, layer)
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


# --- archive ------------------------------------------------------------------------------------
def _local_path(url):
    if url.startswith("file://"):
        return url[len("file://"):]
    return None if "://" in url else url


class Archive(pmtiles.Archive):
    """One archive of a release (header, directories and tiles cached on disk; pmtiles.Archive's directory walk and
    range-merged tile reads, our own source and checks)."""

    def __init__(self, reader, name):
        super().__init__(reader, name)
        self.name = name
        self.url = archive_url(reader.base, reader.release, name)
        self.dir = os.path.join(reader.root, reader.release, name)
        self.meta = None
        self._cover = []
        self._down = None               # (monotonic time, reason): could not be read
        self._open_lock = threading.Lock()

    def _get(self, start, end, kind, etag=None, short_ok=False):
        path = _local_path(self.url)
        if path is not None:
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    body = f.read(end - start)
            except OSError as e:
                raise pmtiles.PmtilesError(f"{path}: {e}") from e
            if len(body) != end - start and not (short_ok and len(body) >= 127):
                raise pmtiles.PmtilesError(f"{path}: {len(body)} bytes for {start}-{end - 1}")
            return body, ""
        try:
            res = self.r.fetcher.get(self.url, kind=kind, headers={"Range": f"bytes={start}-{end - 1}"})
        except netfetch.NotFound as e:
            raise Unavailable(f"{self.url}: not found") from e
        n = len(res.body)
        if res.status != 206 or not (n == end - start or (short_ok and 127 <= n < end - start)):
            raise pmtiles.PmtilesError(f"{self.url}: HTTP {res.status}, {n} bytes for {start}-{end - 1}")
        tag = (res.headers.get("etag") or "").strip()
        if etag is not None and tag and tag != etag:
            self._reset()
            raise pmtiles.Changed(f"{self.url}: ETag {tag}, the cached directories are {etag}")
        return res.body, tag

    def _tile_path(self, z, x, y):
        return os.path.join(self.dir, "tiles", str(z), str(x), f"{y}.bnwt")

    def head(self, kind):
        with self._lock:
            if self._head is not None:
                return self._head
        with self._open_lock:               # (one read of the head: the cell's layers ask at once)
            with self._lock:
                if self._head is not None:
                    return self._head
                down = self._down
            if down is not None and time.monotonic() - down[0] < RETRY_S:
                raise Unavailable(down[1])
            return self._head_once(kind)

    def _head_once(self, kind):
        try:
            got = self._open(kind)
        except _NOT_ITS_FAULT:
            raise                           # (a prefetch the host had no room for, a budget: not the archive's fault)
        except (Unavailable, pmtiles.PmtilesError, netfetch.FetchError, ValueError, OSError) as e:
            with self._lock:
                self._down = (time.monotonic(), f"{self.url}: {type(e).__name__}: {e}")
            log.info("bnwt: %s unavailable (%s: %s): the parquet files meanwhile", self.url, type(e).__name__, e)
            raise Unavailable(str(e)) from e
        with self._lock:
            self._head = got
            self._down = None
        return got

    def _open(self, kind):
        b = pmtiles._read(os.path.join(self.dir, "head.bin"))
        meta = None
        if b is not None:
            try:
                with open(os.path.join(self.dir, "head.json"), encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                b = None
        if b is None:
            b, tag = self._get(0, HEAD_BYTES, kind, short_ok=True)
            h = fast._ef.pmt_header(b)
            mo, ml = h["metadata_offset"], h["metadata_length"]
            if mo + ml > len(b):                # (a long coverage list: read it with the head)
                b = b + self._get(len(b), mo + ml, kind, tag or None)[0]
            meta = {"etag": tag}
            pmtiles._write(os.path.join(self.dir, "head.bin"), b)
            pmtiles._write(os.path.join(self.dir, "head.json"), json.dumps(meta).encode())
        h = fast._ef.pmt_header(b)
        if h["internal_compression"] not in (1, 2) or h["tile_compression"] != 1 or h["tile_type"] != 0:
            raise Unavailable(f"{self.url}: not a BNWT archive (compression {h['internal_compression']}/"
                              f"{h['tile_compression']}, type {h['tile_type']})")
        mo, ml = h["metadata_offset"], h["metadata_length"]
        m = json.loads(gzip.decompress(b[mo:mo + ml]) if b[mo:mo + 2] == b"\x1f\x8b" else b[mo:mo + ml])
        want = {"format": "bnwt", "version": FORMAT, "content": CONTENT, "release": self.r.release}
        bad = {k: m.get(k) for k, v in want.items() if m.get(k) != v}
        if bad:
            self._reset()
            raise Unavailable(f"{self.url}: {bad}, {want} wanted")
        self.meta = m
        self._cover = [tuple(int(v) for v in r) for r in m.get("coverage", ())]
        ro, rl = h["root_offset"], h["root_length"]
        root = b[ro:ro + rl] if ro + rl <= len(b) else self._get(ro, ro + rl, kind, meta.get("etag"))[0]
        return h, root, meta.get("etag") or None

    def covers(self, key, kind):
        self.head(kind)
        sh = key.z - ZOOM
        return self.covers_xy((ZOOM, key.x >> sh, key.y >> sh))

    def covers_xy(self, t):
        return any(x0 <= t[1] <= x1 and y0 <= t[2] <= y1 for x0, y0, x1, y1 in self._cover)

    def has_layer(self, layer):
        return self.meta is not None and layer in self.meta.get("layers", ())


class Reader(pmtiles.Reader):
    """The layers of ARCHIVES per cell from the BNWT archives under base (BASE_URL), as overture.Reader.query gives
    them; Unavailable when an archive cannot answer."""

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

    def _tile_key(self, cell):
        sh = cell.z - ZOOM
        if sh < 0:
            raise Unavailable(f"cell {cell} is coarser than the tiles")
        return ZOOM, cell.x >> sh, cell.y >> sh

    def query(self, layer, cell, kind="demand"):
        """(Features, sources) of layer in cell, as overture.Reader.query; Unavailable when the archive cannot say."""
        name = ARCHIVE_OF.get(layer)
        if name is None or not self.base:
            raise Unavailable(f"{layer}: not in a BNWT archive")
        arch = self.archive(name)
        if not arch.covers(cell, kind):
            raise Unavailable(f"{cell}: outside {arch.url}'s coverage")
        if not arch.has_layer(layer):
            raise Unavailable(f"{arch.url} has no {layer}")
        if cell.z != ZOOM:
            raise Unavailable(f"cell {cell}: the tiles are z{ZOOM}")
        k = self._tile_key(cell)
        tile = self._tile(arch, k, kind)
        t0 = time.perf_counter()
        if not tile:
            got = None
        else:
            try:
                got = decode_layer(tile, layer)
            except ValueError as e:     # (a damaged tile file: dropped, the parquet files this time)
                with self._lock:
                    old = self._mem.pop((arch.theme, k), None)
                    if old is not None:
                        self._mem_bytes -= len(old)
                try:
                    os.remove(arch._tile_path(*k))
                except OSError:
                    pass
                raise Unavailable(f"{cell} {layer}: {e}") from e
        self.stats["decode_s"] += time.perf_counter() - t0
        self.stats["cells"] += 1
        return got if got is not None else (overture.empty(layer), {})

    def _tile(self, arch, k, kind):
        """Tile k's bytes (b"": none there): memory, disk, else its block (BLOCK_ZOOM) from the archive."""
        b = self.mem_get((arch.theme, k))
        if b is None:
            b = pmtiles._read(arch._tile_path(*k))
            if b is None:
                self.fetch_squares(arch, [_block(k)], kind)
                b = pmtiles._read(arch._tile_path(*k))
                if b is None:               # (its block's read failed in another thread: this one alone)
                    b = arch.tiles([k], kind)[k]
            self.mem_put((arch.theme, k), b)
        return b

    def prefetch(self, needs, kind="prefetch"):
        """The blocks of the (cell, layer)s in needs not on disk fetched at once (blocks next to each other in the
        archive in one range read). Returns the cells asked for; nothing when the archive is not there."""
        want = {}
        for cell, layer in needs:
            name = ARCHIVE_OF.get(layer)
            if name is None or not self.base or cell.z != ZOOM:
                continue
            want.setdefault(name, set()).add(self._tile_key(cell))
        n = 0
        for name, ks in want.items():
            arch = self.archive(name)
            try:
                arch.head(kind)
            except Unavailable:
                continue
            ks = [k for k in ks if arch.covers_xy(k) and not os.path.exists(arch._tile_path(*k))]
            if ks:
                self.fetch_squares(arch, sorted({_block(k) for k in ks}), kind)
                n += len(ks)
        return n

    def fetch_squares(self, arch, squares, kind, stats=None, progress=None):
        """Every tile of the quadtree squares [(z, x, y)] on disk: each square is one contiguous run of the archive,
        runs next to each other are read together, REGION_CHUNK bytes at most a request; a cell of the coverage with
        no tile gets an empty file (no directory walk for it later). Squares another thread is reading are waited
        for. stats: {"tiles", "fetched", "bytes", "requests"} counted up."""
        stats = stats if stats is not None else dict(tiles=0, fetched=0, bytes=0, requests=0)
        mine, theirs = [], []
        with self._lock:
            for sq in sorted(set(squares)):
                k = (arch.name, sq)
                ev = self._inflight.get(k)
                if ev is None:
                    self._inflight[k] = threading.Event()
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
        h, root, etag = arch.head(kind)
        runs = sorted(fast._ef.bnwt_square_range(z, x, y, ZOOM) for z, x, y in squares)
        merged = []
        for a, b in runs:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        ents = [e for a, b in merged for e in self._entries(arch, h, root, etag, a, b, kind, 0)]
        have = {e[0] for e in ents}
        stats["tiles"] += len(ents)
        todo = [e for e in ents if not os.path.exists(arch._tile_path(*_zxy(e[0])))]
        i = 0
        while i < len(todo):                # (chunks of tiles back to back in the archive)
            j = i + 1
            while j < len(todo) and todo[j][1] - (todo[j - 1][1] + todo[j - 1][2]) <= pmtiles.MERGE_GAP \
                    and todo[j][1] + todo[j][2] - todo[i][1] <= REGION_CHUNK:
                j += 1
            a, b = todo[i][1], todo[j - 1][1] + todo[j - 1][2]
            body, _ = arch._get(a, b, kind, etag)
            stats["requests"] += 1
            stats["bytes"] += len(body)
            for t, off, ln in todo[i:j]:
                pmtiles._write(arch._tile_path(*_zxy(t)), body[off - a:off - a + ln])
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

    # --- region packs -------------------------------------------------------------------------
    def _entries(self, arch, h, d, etag, a, b, kind, depth):
        """[(tile id, absolute offset, length)] of the tiles with ids in [a, b), from directory d and its leaves."""
        if depth > 4:
            raise pmtiles.PmtilesError("directories deeper than 4 levels")
        tiles, leaves = fast._ef.bnwt_dir_range(d, a, b)
        out = [(t, h["data_offset"] + off, ln) for t, off, ln in tiles if a <= t < b]
        for _t, off, ln in leaves:
            out += self._entries(arch, h, arch._leaf(off, ln, kind, etag), etag, a, b, kind, depth + 1)
        return sorted(out)

    def download_region(self, bbox, kind="predownload", progress=None):
        """Every tile of every archive in bbox (lon0, lat0, lon1, lat1) on disk: the quadtree squares covering it,
        each one contiguous byte range (resumable: tiles already there are skipped). Returns {"tiles", "fetched",
        "bytes", "requests"}."""
        stats = dict(tiles=0, fetched=0, bytes=0, requests=0)
        lon0, lat0, lon1, lat1 = bbox
        nw, se = mapcells.cell_of(lat1, lon0, ZOOM), mapcells.cell_of(lat0, lon1, ZOOM)
        for name in ARCHIVES:
            self.fetch_squares(self.archive(name), _squares(nw.x, nw.y, se.x, se.y), kind, stats, progress)
        return stats


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
    ap = argparse.ArgumentParser(description="BNWT archives: region download (resumable), archive info")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("region", help="fetch every tile of a bbox into the cache")
    r.add_argument("--bbox", required=True, help="lon0,lat0,lon1,lat1")
    i = sub.add_parser("info", help="the archive's header and metadata")
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
            h, _root, _etag = rd.archive(name).head("demand")
            print(json.dumps({"archive": name, "header": h, "metadata": rd.archive(name).meta}, indent=1,
                             default=str))
        return 0
    bbox = tuple(float(v) for v in a.bbox.split(","))
    t0 = time.perf_counter()
    s = rd.download_region(bbox, kind="predownload",
                           progress=lambda s: print(f"\r{s['fetched']} tiles, {s['bytes'] / 1e6:.1f} MB",
                                                    end="", file=sys.stderr))
    print(file=sys.stderr)
    print(json.dumps(dict(s, seconds=round(time.perf_counter() - t0, 2))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
