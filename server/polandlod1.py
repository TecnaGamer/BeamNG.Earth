"""Polish LOD1 building footprints (GUGiK 2024) for the map-data buildings layer. Opt-in:

    BNE_PL_LOD1=1                  the index and the files a cell needs are fetched from GitHub
                                   (github.com/maalos/LOD1_2024_PL, at the commit pinned below) when the cell is first
                                   built, and kept in <cache>/polandlod1/ (0.1 to 3.7 MB a county, 250 MB for all of Poland)

Where a map cell lies wholly in Poland and the files have tiles there, the cell's `buildings` come from the official
footprints (with a measured height) instead of Overture's, and its `building_parts` are empty (they would point at
buildings that are no longer there). Border cells and cells without tiles keep Overture: nothing changes without the
variable, nor outside Poland.

The source is pinned: COMMIT is a fixed commit of the dataset repository (no branch, nothing that can move) and
INDEX_SHA256 is the SHA-256 of its index.npz, so a data update is a reviewed change to these two constants (and to
VERSION, which is the cell files' identity). Which cells have tiles, and which file and block a tile sits in, is not
read from the files but from that index (tools/build_polandlod1_index.py), with every file's name, size and SHA-256.
The index is only accepted when it hashes to INDEX_SHA256, every file name in it must be of the form NNNN_X-NN.bld (a
name goes into a path and a URL), and a download is only kept when it is the size and SHA-256 the index lists. Files
are kept under `<name>.<sha[:16]>`, so a file is never read with another version's tile table.

Three states of a cell, decided once per build (mapdata passes the answer down, so the cache path and the source
cannot disagree): not ours (option off, or not wholly in Poland: Overture, always), ours (wholly in Poland and the
index lists tiles there: LOD1), and "not yet known" (wholly in Poland, the index not loaded yet): that cell's build
waits for the index (resolve() fetches it and raises netfetch's errors when it cannot), it is never built from
Overture meanwhile. Only `resolve` / `fetch_index` touch the network, never `index()`, `covers()` or `signature()`
(MapData.versions() runs those for every tile key, anywhere in the world), and no lock is held over a request.
Downloads go through netfetch's "lod1" lane of raw.githubusercontent.com; when one fails the cell's build is deferred
(netfetch's errors propagate), so a cell is not cached with the wrong source.

File format (README of the dataset): header, tile table, block table, then DEFLATE blocks of six streams
[anchor_x, anchor_y, base_z, heights, rings, vertices]. Coordinates are EPSG:2180 metres (x east), converted to
lon/lat by pl1992 (numpy and dnp, no pyproj). An "ortho" ring is a rotated rectilinear outline: `k` is its angle in
units of 90 deg / 16384, `swap` says its first edge runs along the rotated y axis (checked on real tiles: the
corners of such rings coincide with the neighbours' ten times more often than for any other reading). What is parsed
is bounded (MAX_* below): a malformed file is one clear error (netfetch.BadResponse) per file, not a traceback per cell.
"""
import hashlib
import io
import json
import logging
import math
import mmap
import os
import re
import threading
import time
import urllib.parse
import zlib
from collections import OrderedDict

import numpy as np

import dmath
import pl1992

log = logging.getLogger("polandlod1")

ENV = "BNE_PL_LOD1"
REPO = "maalos/LOD1_2024_PL"
COMMIT = "e4af9414590ff10a84003afcab71ad53a295e790"                  # the dataset repository's commit in use
INDEX_SHA256 = "bba60bc48ba85cb8fdc27e4deb13511838160003e24ec2813276263096e8b019"      # of index.npz at COMMIT
REMOTE_DIR = "bld"              # the files' folder in the repository
BASE_URL = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}"       # (what github.com/<repo>/raw/<commit>/... leads to)
INDEX_URL = f"{BASE_URL}/index.npz"
FILE_URL = f"{BASE_URL}/{REMOTE_DIR}/{{name}}"
INDEX_RETRY_S = 60.0            # s before an index that could not be had is asked for again
LANE = "lod1"                   # netfetch lane: its own policy (netfetch.POLICIES["raw.githubusercontent.com#lod1"])
VERSION = 2                     # bump when what a cell's buildings contain changes, and with every new COMMIT
DATASET = "GUGiK LOD1 2024"     # buildings.py trusts it as a footprint source (not an ML one)
PROVENANCE_ID = "gugik-lod1"
ATTRIBUTION = "Building footprints and heights: GUGiK (Head Office of Geodesy and Cartography), LOD1 2024"
ANGLE_UNIT = math.pi / 2 / 16384
MARGIN_M = 60.0                 # tile extents are padded by this when picking tiles for a cell
BLOCKS_KEPT = 4                 # decoded blocks kept (a block is a few tens of thousands of buildings)
_SAMPLES = 9                    # points per side of a cell, to bound it in EPSG:2180
BORDER_STEP_M = 10.0            # the border test samples a cell's edges this often (the border data is simplified to ~10 m)

# what is parsed is bounded (real data: a file has at most 422 tiles and 17 blocks, a block inflates to at most 0.33 MB
# and holds at most 15,000 buildings; the limits leave a wide margin)
NAME_RE = re.compile(r"[0-9]{4}_[A-Z]-[0-9]{2}\.bld")       # a file's name in the index (ASCII only)
MAX_INDEX_BYTES = 8 << 20       # index.npz (0.6 MB)
MAX_FILES = 4096
MAX_FILE_BYTES = 64 << 20       # a .bld file (3.7 MB at most)
MAX_TILES = 1 << 16             # tiles in one file
MAX_BLOCKS = 4096               # blocks in one file
MAX_BLOCK_BYTES = 8 << 20       # a block inflated (and a block's bytes as stored)
MAX_BLOCK_BUILDINGS = 250_000   # buildings in one block
MAX_TILE_BUILDINGS = MAX_BLOCK_BUILDINGS
_MAX_VARINT_BYTES = 10


class BadData(ValueError):
    """A .bld file or the index is not what the format allows."""


# --- varints -------------------------------------------------------------------------------------
def _uvarint(buf, o):
    r = s = 0
    while True:
        c = buf[o]
        o += 1
        r |= (c & 127) << s
        if c < 128:
            return r, o
        s += 7
        if s > 63:
            raise BadData("varint too long")


def _svarint(buf, o):
    v, o = _uvarint(buf, o)
    return (v >> 1) ^ -(v & 1), o


def _varints(buf):
    """All the varints of a stream, as int64 (vectorised: a byte below 128 ends a value)."""
    a = np.frombuffer(buf, np.uint8)
    if not len(a):
        return np.zeros(0, np.int64)
    if a[-1] >= 128:
        raise BadData("stream ends inside a varint")
    ends = np.flatnonzero(a < 128)
    starts = np.empty_like(ends)
    starts[0] = 0
    starts[1:] = ends[:-1] + 1
    ln = ends - starts + 1
    if int(ln.max()) > _MAX_VARINT_BYTES:
        raise BadData("varint too long")
    out = np.zeros(len(ends), np.int64)
    for i in range(int(ln.max())):
        m = ln > i
        out[m] |= (a[starts[m] + i].astype(np.int64) & 127) << (7 * i)
    return out


def _zigzag(v):
    return (v >> 1) ^ -(v & 1)


def _inflate(raw, limit=MAX_BLOCK_BYTES):
    """A raw DEFLATE stream inflated, to at most `limit` bytes (a larger or unfinished stream is BadData)."""
    d = zlib.decompressobj(-15)
    out = d.decompress(raw, limit)
    if d.unconsumed_tail or not d.eof:
        raise BadData(f"block inflates to more than {limit} bytes, or is cut short")
    return out


# --- file header ---------------------------------------------------------------------------------
class Header:
    """The tile and block tables of one .bld file."""

    def __init__(self, buf):
        if bytes(buf[:4]) != b"GBLD" or buf[4] != 2:
            raise BadData("not a GBLD v2 file")
        self.flags, self.xy_cm, self.z_cm = buf[5], buf[6], buf[7]
        if not self.xy_cm or not self.z_cm:
            raise BadData("zero grid step")
        n, o = _uvarint(buf, 8)
        if n > MAX_TILES:
            raise BadData(f"{n} tiles")
        tiles, px, py = [], 0, 0
        for _ in range(n):
            dx, o = _svarint(buf, o)
            dy, o = _svarint(buf, o)
            ex, o = _uvarint(buf, o)
            ey, o = _uvarint(buf, o)
            c, o = _uvarint(buf, o)
            if c > MAX_TILE_BUILDINGS:
                raise BadData(f"{c} buildings in a tile")
            px += dx
            py += dy
            tiles.append((px, py, ex, ey, c))
        nb, o = _uvarint(buf, o)
        if nb > MAX_BLOCKS:
            raise BadData(f"{nb} blocks")
        blocks = []
        for _ in range(nb):
            t, o = _uvarint(buf, o)
            m, o = _uvarint(buf, o)
            if (m >> 1) > MAX_BLOCK_BYTES:
                raise BadData(f"a block of {m >> 1} bytes")
            blocks.append((t, m >> 1, m & 1))
        if sum(t for t, _, _ in blocks) != n:
            raise BadData("the blocks do not cover the tile table")
        self.tiles, self.blocks = tiles, blocks
        offs, pos = [], o
        for _, ln, _ in blocks:
            offs.append(pos)
            pos += ln
        self.block_offsets = offs
        self.end = pos                      # where the last block ends: the file must reach it
        first, self.block_first_tile = 0, []
        for t, _, _ in blocks:
            self.block_first_tile.append(first)
            first += t


def read_header(path):
    """Header of a file; reads only as much of it as the tables take."""
    size = os.path.getsize(path)
    want = 1 << 16
    with open(path, "rb") as f:
        while True:
            buf = f.read(min(want, size))
            f.seek(0)
            try:
                h = Header(buf)
                break
            except IndexError:
                if want >= size:
                    raise
                want *= 4
    if h.end > size:
        raise BadData(f"the blocks end at {h.end}, the file has {size} bytes")
    return h


# --- one block -----------------------------------------------------------------------------------
class Block:
    """The buildings of one block: coords (N, 2) EPSG:2180 metres, ring_off (R + 1), ring_hole (R),
    bld_ring_off (B + 1), bbox (B, 4) x0 y0 x1 y1, anchor (B, 2) in decimetres, height (B) metres (NaN: none)."""
    __slots__ = ("coords", "ring_off", "ring_hole", "bld_ring_off", "bbox", "anchor", "height")


def _ortho(p0, k, swap, steps, n):
    th = k * ANGLE_UNIT
    u = np.array([dmath.cos(th), dmath.sin(th)])
    v = np.array([-u[1], u[0]])
    ax = (v, u) if swap else (u, v)
    m = len(steps)
    d = np.where((np.arange(m) & 1)[:, None] == 0, ax[0], ax[1]) * steps[:, None]
    pts = p0 + np.vstack([[0.0, 0.0], np.cumsum(d, axis=0)])
    side = (n - 2) & 1
    closing = -steps[side::2].sum() * ax[side]
    return np.vstack([pts, pts[-1] + closing])


def decode_block(raw, header, first_tile, tiles_in_block):
    """Decode the six streams of one (already inflated) block."""
    if len(raw) > MAX_BLOCK_BYTES:
        raise BadData(f"a block of {len(raw)} bytes")
    o, streams = 0, []
    for _ in range(6):
        n, o = _uvarint(raw, o)
        if o + n > len(raw):
            raise BadData("a stream runs past the block")
        streams.append(raw[o:o + n])
        o += n
    ax_s, ay_s, _bz, ht_s, rg_s, vx_s = streams
    spm = 100.0 / header.xy_cm
    counts = [header.tiles[first_tile + i][4] for i in range(tiles_in_block)]
    total = sum(counts)
    if total > MAX_BLOCK_BUILDINGS:
        raise BadData(f"{total} buildings in a block")       # (before anything is allocated for them)
    ax = _zigzag(_varints(ax_s))
    ay = _zigzag(_varints(ay_s))
    if len(ax) < total or len(ay) < total:
        raise BadData("fewer anchors than buildings")
    gx, gy = np.empty(total, np.int64), np.empty(total, np.int64)
    ox_b, oy_b = np.empty(total, np.float64), np.empty(total, np.float64)
    i = 0
    for t, c in enumerate(counts):                  # deltas restart at every tile
        ox, oy = header.tiles[first_tile + t][:2]
        gx[i:i + c] = np.cumsum(ax[i:i + c])
        gy[i:i + c] = np.cumsum(ay[i:i + c])
        ox_b[i:i + c], oy_b[i:i + c] = ox, oy
        i += c
    hv = _varints(ht_s)
    pal = int(hv[0])
    palette = hv[1:1 + pal].astype(np.float64) * header.z_cm / 100.0
    height = palette[hv[1 + pal:1 + pal + total]]
    height[height <= 0] = np.nan

    rg = _varints(rg_s).tolist()
    vu = _varints(vx_s)
    vs = _zigzag(vu)
    vu = vu.tolist()
    rings, holes, bld_off = [], [], [0]
    bbox = np.empty((total, 4), np.float64)
    rp = vp = 0
    for b in range(total):
        rc = rg[rp]
        rp += 1
        sx, sy = int(gx[b]), int(gy[b])
        first_ring = len(rings)
        for r in range(rc):
            v = rg[rp]
            rp += 1
            n, hole, ortho = v >> 2, (v >> 1) & 1, v & 1
            if n > len(vs) or (ortho and n < 3):
                raise BadData(f"a ring of {n} vertices")
            if r:
                sx += int(vs[vp])
                sy += int(vs[vp + 1])
                vp += 2
            p0 = np.array([sx, sy], np.float64)
            if not ortho:
                if vp + 2 * (n - 1) > len(vs):
                    raise BadData("a ring runs past the vertex stream")
                d = vs[vp:vp + 2 * (n - 1)].reshape(-1, 2).astype(np.float64)
                vp += 2 * (n - 1)
                pts = p0 + np.vstack([[0.0, 0.0], np.cumsum(d, axis=0)])
            else:
                if vp + n - 1 > len(vs):
                    raise BadData("a ring runs past the vertex stream")
                ks = vu[vp]
                steps = vs[vp + 1:vp + 1 + n - 2].astype(np.float64)
                vp += 1 + n - 2
                pts = _ortho(p0, ks >> 1, ks & 1, steps, n)
            rings.append(pts / spm + (ox_b[b], oy_b[b]))
            holes.append(bool(hole))
        bld_off.append(len(rings))
        outer = rings[first_ring] if rc else None
        if outer is not None:
            bbox[b] = (*outer.min(0), *outer.max(0))
        else:
            bbox[b] = np.nan
    blk = Block()
    sizes = np.fromiter((len(r) for r in rings), np.int64, len(rings))
    blk.ring_off = np.concatenate([[0], np.cumsum(sizes)])
    blk.coords = np.concatenate(rings) if rings else np.zeros((0, 2))
    blk.ring_hole = np.array(holes, bool)
    blk.bld_ring_off = np.array(bld_off, np.int64)
    blk.bbox = bbox
    blk.anchor = np.round(np.stack([ox_b + gx / spm, oy_b + gy / spm], 1) * 10).astype(np.int64)
    blk.height = height
    return blk


# --- the index: what is known without the files --------------------------------------------------
def make_index(paths):
    """The arrays of the repository's index.npz for these files (tools/build_polandlod1_index.py saves them with
    np.savez): each file's name, size, SHA-256 and building count, and every non-empty tile's EPSG:2180 rectangle
    with the file (its number in `paths`' order) and the block it sits in."""
    files, tf, tb, tx, ty, te, tn = [], [], [], [], [], [], []
    for fi, path in enumerate(paths):
        with open(path, "rb") as f:
            data = f.read()
        h = read_header(path)
        n = 0
        for bi, (t, _, _) in enumerate(h.blocks):
            f0 = h.block_first_tile[bi]
            for ox, oy, ex, ey, c in h.tiles[f0:f0 + t]:
                if c:
                    tf.append(fi), tb.append(bi), tx.append(ox), ty.append(oy), te.append(ex), tn.append(ey)
                    n += c
        files.append([os.path.basename(path), len(data), hashlib.sha256(data).hexdigest(), n])
    cols = {"tile_file": (tf, np.uint16), "tile_blk": (tb, np.uint16), "tile_x0": (tx, np.int32),
            "tile_y0": (ty, np.int32), "tile_ex": (te, np.uint16), "tile_ey": (tn, np.uint16)}
    out = {}
    for k, (src, dt) in cols.items():
        out[k] = np.array(src, dt)
        if not (out[k].astype(np.int64) == np.array(src, np.int64)).all():
            raise ValueError(f"{k} does not fit {np.dtype(dt)}")
    out["meta"] = np.frombuffer(json.dumps({"files": files}, separators=(",", ":")).encode(), np.uint8)
    return out


class Manifest:
    """index.npz: every file's name / size / SHA-256 / building count, and every tile's rectangle (x0 y0 x1 y1,
    EPSG:2180 metres) with the file and block it sits in. `digest` identifies this index. `source` is a path or a
    file object. Every file name is checked (BadData otherwise: the whole index is refused), because a name goes into
    a local path and a URL."""

    def __init__(self, source):
        with np.load(source, allow_pickle=False) as z:
            raw = bytes(z["meta"])
            meta = json.loads(raw.decode())
            self.tile_file = z["tile_file"].astype(np.int64)
            self.tile_blk = z["tile_blk"].astype(np.int64)
            self.x0 = z["tile_x0"].astype(np.float64)
            self.y0 = z["tile_y0"].astype(np.float64)
            self.x1 = self.x0 + z["tile_ex"]
            self.y1 = self.y0 + z["tile_ey"]
        self.digest = hashlib.sha256(raw).hexdigest()
        files = meta["files"]
        if not isinstance(files, list) or not 0 < len(files) <= MAX_FILES:
            raise BadData("the index lists no files, or too many")
        names, sizes, sha, buildings = [], [], [], []
        for f in files:
            if not isinstance(f, list) or len(f) != 4:
                raise BadData("a malformed file entry")
            name, size, digest, n = f
            if not isinstance(name, str) or not NAME_RE.fullmatch(name) or os.path.basename(name) != name:
                raise BadData(f"unsafe file name {name!r:.60}")
            if (type(size) is not int or not 0 < size <= MAX_FILE_BYTES or type(n) is not int or n < 0
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise BadData(f"a malformed entry for {name}")
            names.append(name), sizes.append(size), sha.append(digest), buildings.append(n)
        if len(set(names)) != len(names):
            raise BadData("a file is listed twice")
        self.names, self.sizes, self.sha, self.buildings = names, sizes, sha, buildings
        nt = len(self.tile_file)
        if not (len(self.tile_blk) == len(self.x0) == len(self.y0) == len(self.x1) == len(self.y1) == nt):
            raise BadData("the tile columns differ in length")
        if nt and (int(self.tile_file.max()) >= len(names) or int(self.tile_blk.max()) >= MAX_BLOCKS):
            raise BadData("a tile names a file or block that does not exist")

    @staticmethod
    def url(name):
        return FILE_URL.format(name=urllib.parse.quote(name, safe=""))


def _get(url, kind):
    import netfetch
    return (_state["fetcher"] or netfetch.default_fetcher()).get(url, kind=kind, lane=LANE).body


def _save(path, body):
    """Write `body` to `path` whole or not at all."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
    try:
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _index_path(d):
    return os.path.join(d, f"index.{INDEX_SHA256[:16]}.npz")


def _read_index(d):
    """The Manifest of the pinned index kept in folder `d`; None when it is not there, is not the pinned bytes or does
    not load (a bad file is removed). Reads the disk only: no network."""
    path = _index_path(d)
    try:
        with open(path, "rb") as f:
            body = f.read(MAX_INDEX_BYTES + 1)
    except OSError:
        return None
    try:
        if len(body) > MAX_INDEX_BYTES or hashlib.sha256(body).hexdigest() != INDEX_SHA256:
            raise BadData("not the pinned index")
        return Manifest(io.BytesIO(body))
    except Exception as e:
        log.warning("polandlod1: %s unusable (%s: %s); removed", path, type(e).__name__, e)
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def _fetch_manifest(d, kind):
    """The pinned index from the repository, checked against INDEX_SHA256 before it is parsed or kept. Raises
    netfetch's errors."""
    import netfetch
    body = _get(INDEX_URL, kind)
    if len(body) > MAX_INDEX_BYTES or hashlib.sha256(body).hexdigest() != INDEX_SHA256:
        raise netfetch.BadResponse(f"{INDEX_URL}: not the pinned index ({len(body)} bytes)", INDEX_URL,
                                   netfetch.host_of(INDEX_URL))
    try:
        m = Manifest(io.BytesIO(body))
    except Exception as e:                  # (the pinned bytes do not load: a bug in the pin, not in the network)
        raise netfetch.BadResponse(f"{INDEX_URL}: {type(e).__name__}: {e}", INDEX_URL,
                                   netfetch.host_of(INDEX_URL)) from None
    try:
        _save(_index_path(d), body)
        _prune(d, "index.", keep=_index_path(d), legacy="index.npz")
    except OSError as e:
        raise netfetch.FetchFailed(f"{_index_path(d)}: {e}", INDEX_URL, netfetch.host_of(INDEX_URL)) from None
    return m


def _prune(d, prefix, keep, legacy=None):
    """Remove what an earlier pin left beside the file `keep` (and a file of the old, unhashed name)."""
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        p = os.path.join(d, n)
        if p != keep and not n.endswith((".part", ".check")) and (n == legacy or n.startswith(prefix)):
            try:
                os.remove(p)
            except OSError:
                pass


# --- the files ------------------------------------------------------------------------------------
_DECODE_ERRORS = (BadData, IndexError, ValueError, OverflowError, zlib.error)


class Index:
    """The Manifest's tiles, and the files behind them: each is fetched into `folder` when first needed (ensure)."""

    def __init__(self, folder, manifest):
        self.folder, self.manifest = folder, manifest
        m = manifest
        self.names, self.sizes = m.names, m.sizes
        self.n_buildings = sum(m.buildings)
        self.file, self.blk = m.tile_file, m.tile_blk
        self.x0, self.y0, self.x1, self.y1 = m.x0, m.y0, m.x1, m.y1
        self._headers = {}
        self._blocks = OrderedDict()
        self._lock = threading.Lock()
        self._file_locks = {}
        self._bad = {}

    def path(self, fi):
        """Where file `fi` is kept: named by its hash, so an index that changed a file is never read with the old one."""
        return os.path.join(self.folder, f"{self.names[fi]}.{self.manifest.sha[fi][:16]}")

    def blocks_for(self, x0, y0, x1, y1):
        """Sorted unique (file, block) pairs that have a tile touching the box."""
        m = (self.x0 <= x1 + MARGIN_M) & (self.x1 >= x0 - MARGIN_M) & (self.y0 <= y1 + MARGIN_M) & (self.y1 >= y0 - MARGIN_M)
        return sorted({(int(f), int(b)) for f, b in zip(self.file[m], self.blk[m])})

    def ensure(self, fi, kind="demand"):
        """The path of file `fi`, fetched first when it is not on disk (or not the size the index says). A failure
        raises netfetch's errors: the cell is then not built (nor cached) from another source."""
        path, want = self.path(fi), self.sizes[fi]
        if _size(path) == want:
            return path
        with self._lock:
            lock = self._file_locks.setdefault(fi, threading.Lock())
        with lock:                          # (one fetch of a file, however many cells wait on it)
            if _size(path) != want:
                self._fetch(fi, path, kind)
        return path

    def _fetch(self, fi, path, kind):
        import netfetch
        url = self.manifest.url(self.names[fi])
        body = _get(url, kind)
        if len(body) != self.sizes[fi] or hashlib.sha256(body).hexdigest() != self.manifest.sha[fi]:
            raise netfetch.BadResponse(f"{url}: not the file the index lists ({len(body)} bytes)", url,
                                       netfetch.host_of(url))
        _save(path, body)
        _prune(self.folder, self.names[fi] + ".", keep=path, legacy=self.names[fi])
        log.info("polandlod1: fetched %s (%d KB)", self.names[fi], len(body) // 1024)

    def _bad_file(self, fi, err=None):
        """netfetch.BadResponse for a file that does not parse: logged once, then raised afresh for every cell that
        reaches it (without decoding it again)."""
        import netfetch
        msg = self._bad.get(fi)
        if msg is None:
            msg = self._bad[fi] = f"{self.names[fi]}: malformed data ({type(err).__name__}: {err})"
            log.warning("polandlod1: %s; the cells it serves are not built", msg)
        url = self.manifest.url(self.names[fi])
        return netfetch.BadResponse(msg, url, netfetch.host_of(url))

    def header(self, fi, kind="demand"):
        if fi in self._bad:
            raise self._bad_file(fi)
        with self._lock:
            h = self._headers.get(fi)
        if h is None:
            path = self.ensure(fi, kind)
            try:
                h = read_header(path)
            except _DECODE_ERRORS as e:
                raise self._bad_file(fi, e) from None
            with self._lock:
                self._headers[fi] = h
        return h

    def block(self, fi, bi, kind="demand"):
        with self._lock:
            got = self._blocks.get((fi, bi))
            if got is not None:
                self._blocks.move_to_end((fi, bi))
                return got
        h = self.header(fi, kind)
        try:
            if not 0 <= bi < len(h.blocks):
                raise BadData(f"the index names block {bi} of {len(h.blocks)}")
            with open(self.path(fi), "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                _, ln, defl = h.blocks[bi]
                off = h.block_offsets[bi]
                raw = bytes(mm[off:off + ln])
            if defl:
                raw = _inflate(raw)
            blk = decode_block(raw, h, h.block_first_tile[bi], h.blocks[bi][0])
        except _DECODE_ERRORS as e:
            raise self._bad_file(fi, e) from None
        with self._lock:
            self._blocks[(fi, bi)] = blk
            while len(self._blocks) > BLOCKS_KEPT:
                self._blocks.popitem(last=False)
        return blk


_index = None
_state = {"cache": None, "fetcher": None, "retry_at": 0.0}
_index_lock = threading.Lock()      # the module's state below; held for dictionary work only, never over I/O
_fetch_lock = threading.Lock()      # one fetch of the index at a time: only the cells that need it wait here
_covers = {}
_wanted = {}


def configure(cache_dir, fetcher):
    """MapData's cache folder (everything goes to <cache>/polandlod1) and its netfetch.Fetcher (all downloads go
    through it)."""
    with _index_lock:
        _state.update(cache=cache_dir, fetcher=fetcher, retry_at=0.0)


def on():
    """True when the player asked for it: BNE_PL_LOD1=1."""
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes", "on")


def enabled():
    """Same as on(): whether the feature is asked for, not whether the index has arrived (that is covers() / resolve())."""
    return on()


def folder():
    """Where the index and the files are kept ('' when the cache folder is not known yet)."""
    return os.path.join(_state["cache"], "polandlod1") if _state["cache"] else ""


def index():
    """The Index when it is loaded or on disk, else None. Reads the disk at most (the pinned index is 0.6 MB, hashed
    once); never the network and never a lock held over I/O: MapData.versions() and path() call this for every tile
    key. fetch_index() gets it from the repository."""
    global _index
    if not on():
        return None
    d = folder()
    if not d:
        return None
    with _index_lock:
        if _index is not None and _index.folder == d:
            return _index
    m = _read_index(d)
    if m is None:
        return None
    with _index_lock:
        if _index is None or _index.folder != d:
            _index = Index(d, m)
            _covers.clear()
            log.info("polandlod1: %d files, %d buildings, kept in %s", len(m.names), _index.n_buildings, d)
        return _index


def fetch_index(kind="demand"):
    """The Index, fetched from the repository when it is not on disk. None when the feature is off; netfetch's errors
    when it cannot be had (a failure is remembered for INDEX_RETRY_S: the cells that ask meanwhile get an error at
    once instead of each waiting out a request). The request holds only this module's fetch lock, which nothing but
    the Polish cells' builds ever wait for."""
    import netfetch
    global _index
    idx = index()
    if idx is not None or not on() or not folder():
        return idx
    d = folder()
    with _fetch_lock:
        idx = index()
        if idx is not None:
            return idx
        wait = _state["retry_at"] - time.monotonic()
        if wait > 0:
            raise netfetch.HostUnavailable(f"{INDEX_URL}: not fetched a moment ago, asked again in {wait:.0f} s",
                                           INDEX_URL, netfetch.host_of(INDEX_URL), retry_after=wait)
        try:
            m = _fetch_manifest(d, kind)
        except (netfetch.FetchError, OSError) as e:
            _state["retry_at"] = time.monotonic() + INDEX_RETRY_S
            log.warning("polandlod1: index %s not fetched (%s: %s)", INDEX_URL, type(e).__name__, e)
            raise
        with _index_lock:
            _index = Index(d, m)
            _covers.clear()
            log.info("polandlod1: %d files, %d buildings, kept in %s", len(m.names), _index.n_buildings, d)
            return _index


def warm(kind="prefetch"):
    """Fetch the index now when the feature is on and it is not on disk (MapData.warm_manifests, on its own thread:
    a first Polish cell then finds it). Never raises."""
    try:
        fetch_index(kind)
    except Exception as e:
        log.info("polandlod1: index not fetched ahead (%s: %s)", type(e).__name__, e)


def signature():
    """What the cell files' identity needs on top of VERSION (None when off): the pin, a constant. It does not depend on
    whether the index has arrived (versions() asks for it for every tile key, and a key that changed once the index
    loaded would split the caches)."""
    if not on():
        return None
    return {"version": VERSION, "commit": COMMIT[:12], "index": INDEX_SHA256[:16]}


def _in_poland_only(lats, lons):
    """True when every point is in Poland (the bundled borders), False when one is not; None when the borders are not
    known (not downloaded yet)."""
    try:
        import countrybounds
        borders = countrybounds.bundled()
        if isinstance(borders, countrybounds.Absent):
            return None
        codes = borders.codes_at([(float(a), float(o)) for a, o in zip(lats, lons)])
    except Exception:
        return None
    return len(codes) > 0 and set(codes) == {"PL"}


def _cell_box(bounds):
    """(lons, lats of a grid over the cell, EPSG:2180 box of it)."""
    lon0, lat0, lon1, lat1 = bounds
    lo, la = np.meshgrid(np.linspace(lon0, lon1, _SAMPLES), np.linspace(lat0, lat1, _SAMPLES))
    x, y = pl1992.from_lonlat(lo.ravel(), la.ravel())
    return lo.ravel(), la.ravel(), (x.min(), y.min(), x.max(), y.max())


def _border_points(bounds, grid, box):
    """(lons, lats) for the border test: the cell's four edges every BORDER_STEP_M (the border data is simplified to
    about 10 m, so a border running through the cell cannot slip between points that are as close as its own detail),
    and the grid inside. Poland has no foreign enclave, so a cell whose whole outline and inside grid are in Poland is."""
    lon0, lat0, lon1, lat1 = bounds
    n = int(max(box[2] - box[0], box[3] - box[1]) // BORDER_STEP_M) + 2
    t = np.linspace(0.0, 1.0, n)
    lo, la = lon0 + (lon1 - lon0) * t, lat0 + (lat1 - lat0) * t
    return (np.concatenate([lo, lo, np.full(n, lon0), np.full(n, lon1), grid[0]]),
            np.concatenate([np.full(n, lat0), np.full(n, lat1), la, la, grid[1]]))


def wanted(key):
    """True when the player asked for LOD1 and the cell lies wholly in Poland (the bundled borders). Independent of
    the index and of the network; unknown borders count as not Poland (and are not remembered)."""
    if not on():
        return False
    k = str(key)
    with _index_lock:
        got = _wanted.get(k)
    if got is None:
        bounds = key.bounds()
        lo, la, box = _cell_box(bounds)
        lons, lats = _border_points(bounds, (lo, la), box)
        r = _in_poland_only(lats, lons)
        if r is None:
            return False
        got = bool(r)
        with _index_lock:
            if len(_wanted) > 8192:
                _wanted.clear()
            _wanted[k] = got
    return got


def covers(key):
    """True when the cell's buildings come from LOD1: wholly in Poland, and the index (loaded) has tiles there. False
    while the index is not loaded: use resolve() where the answer decides what is built or stored. Never fetches."""
    if not wanted(key):
        return False
    idx = index()
    if idx is None:
        return False
    k = str(key)
    with _index_lock:
        got = _covers.get(k)
    if got is None:
        _, _, box = _cell_box(key.bounds())
        got = bool(idx.blocks_for(*box))
        with _index_lock:
            if len(_covers) > 8192:
                _covers.clear()
            _covers[k] = got
    return got


def routes_to_lod1(key):
    """Which cache path to look at for the cell, without the network: LOD1's when it is LOD1's, and also when it is
    not yet known (wholly in Poland, the index not loaded: the cell's build waits for the index, so Overture's path
    is not where it will be). Readers use this; whoever builds and stores a cell uses resolve()."""
    return wanted(key) and (index() is None or covers(key))


def resolve(key, kind="demand"):
    """Is the cell LOD1's? Decided here, once, for whoever builds a cell: the cache path and the source are both
    taken from this answer. Fetches the index when the cell is wholly in Poland and it is not on disk (kind:
    netfetch's request kind) and raises netfetch's errors when it cannot be had: that cell then waits, it is not
    built from Overture."""
    if not wanted(key):
        return False
    fetch_index(kind)
    return covers(key)


# --- a cell's Features ---------------------------------------------------------------------------
def query(key, kind="demand", covered=None):
    """(mapgeom.Features, {(dataset, licence): count}) of the cell's buildings, Overture's columns; None when the
    cell is not LOD1's (the caller uses Overture). `covered` is the answer of resolve() the caller already acted on
    (the cache path was chosen by it): not asked again, so that the source cannot differ from the path. The index and
    the files it needs are fetched first (kind: netfetch's request kind, so a prefetch only takes what the host has
    room for); their errors are raised."""
    import mapgeom as mg
    import overture
    if covered is None:
        covered = resolve(key, kind)
    if not covered:
        return None
    idx = index() or fetch_index(kind)
    lon0, lat0, lon1, lat1 = key.bounds()
    _, _, (x0, y0, x1, y1) = _cell_box(key.bounds())
    x0, y0, x1, y1 = x0 - MARGIN_M, y0 - MARGIN_M, x1 + MARGIN_M, y1 + MARGIN_M
    picked = []                              # (block, building row)
    for fi, bi in idx.blocks_for(x0, y0, x1, y1):
        blk = idx.block(fi, bi, kind)
        bb = blk.bbox
        rows = np.flatnonzero((bb[:, 0] <= x1) & (bb[:, 2] >= x0) & (bb[:, 1] <= y1) & (bb[:, 3] >= y0))
        picked.extend((blk, int(r)) for r in rows)
    spec = overture.LAYERS["buildings"]
    seen, items = set(), []                  # one per anchor: a building listed by two tiles is one building
    for blk, r in picked:
        r0, r1 = blk.bld_ring_off[r], blk.bld_ring_off[r + 1]
        if r1 == r0 or blk.ring_hole[r0]:
            continue
        ax, ay = blk.anchor[r]
        fid = f"pl1:{int(ax):08d}{int(ay):08d}"
        n = 0
        while fid in seen:                   # two buildings starting at the same decimetre
            n += 1
            fid = f"pl1:{int(ax):08d}{int(ay):08d}-{n}"
        seen.add(fid)
        items.append((fid, blk, int(r0), int(r1), float(blk.height[r])))
    items.sort(key=lambda t: t[0])
    parts_xy, spans = [], []                 # all coordinates, converted once
    for fid, blk, r0, r1, h in items:
        parts, cur = [], None
        for ring in range(r0, r1):
            pts = blk.coords[blk.ring_off[ring]:blk.ring_off[ring + 1]]
            if len(pts) < 3:
                continue
            if not blk.ring_hole[ring] or cur is None:
                if blk.ring_hole[ring]:
                    continue                  # a hole before any outer ring: damaged
                cur = []
                parts.append(cur)
            cur.append(pts)
        spans.append(parts)
        for p in parts:
            parts_xy.extend(p)
    sizes = [len(r) + 1 for r in parts_xy]   # + the closing vertex
    lon, lat = (pl1992.to_lonlat(*np.concatenate([np.vstack([r, r[:1]]) for r in parts_xy]).T)
                if parts_xy else (np.zeros(0), np.zeros(0)))
    ll = np.stack([lon, lat], 1)
    off = np.concatenate([[0], np.cumsum(sizes)])
    ids, geoms, cx, cy, heights = [], [], [], [], []
    ri = 0
    for (fid, blk, r0, r1, h), parts in zip(items, spans):
        gparts = []
        for p in parts:
            gparts.append([ll[off[ri + j]:off[ri + j + 1]] for j in range(len(p))])
            ri += len(p)
        if not gparts:
            continue
        outer = np.concatenate([g[0] for g in gparts])
        bx0, by0 = outer.min(0)
        bx1, by1 = outer.max(0)
        if bx1 < lon0 or bx0 > lon1 or by1 < lat0 or by0 > lat1:
            continue                          # the margin's neighbours
        ids.append(fid)
        geoms.append((mg.POLYGON, gparts))
        cx.append((bx0 + bx1) / 2)
        cy.append((by0 + by1) / 2)
        heights.append(h)
    if not ids:
        return overture.empty("buildings"), {}
    feats = mg.build("buildings", mg.text_column(ids), geoms)
    feats.clipped = np.zeros(len(ids), bool)
    feats.attrs["cx"] = np.array(cx, np.float64)
    feats.attrs["cy"] = np.array(cy, np.float64)
    n = len(ids)
    for c in spec.get("text", ()):
        feats.attrs[c] = mg.text_column(
            [DATASET if c in ("src_dataset", "height_src") else (i if c == "src_record" else "") for i in ids])
    for c in spec.get("float", ()):
        feats.attrs[c] = mg.float_column(heights if c == "height" else [np.nan] * n)
    for c in spec.get("bool", ()):
        feats.attrs[c] = mg.bool_column([False] * n)
    return feats, {(DATASET, "open data (GUGiK)"): n}


def provenance(sources):
    return {"id": PROVENANCE_ID, "name": "GUGiK (Główny Urząd Geodezji i Kartografii)", "release": "2024",
            "attribution": ATTRIBUTION, "licence": "Polish open geodata (GUGiK)",
            "url": "https://www.geoportal.gov.pl/",
            "datasets": [{"dataset": d, "license": l, "features": n} for (d, l), n in sorted(sources.items())]}
