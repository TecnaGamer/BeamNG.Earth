"""Polish LOD1 building footprints (GUGiK 2024) for the map-data buildings layer. Opt-in:

    BNE_PL_LOD1=1                  the index and the files a cell needs are fetched from GitHub
                                   (github.com/maalos/LOD1_2024_PL, branch master) when the cell is first built, and
                                   kept in <cache>/polandlod1/ (0.1 to 3.7 MB a county, 250 MB for all of Poland)

Where a map cell lies wholly in Poland and the files have tiles there, the cell's `buildings` come from the official
footprints (with a measured height) instead of Overture's, and its `building_parts` are empty (they would point at
buildings that are no longer there). Border cells and cells without tiles keep Overture: nothing changes without the
variable, nor outside Poland.

Which cells have tiles, and which file and block a tile sits in, is not read from the files but from the repository's
index.npz (tools/build_polandlod1_index.py), fetched once into the cache folder (and again after INDEX_MAX_AGE; a
refresh that fails keeps the old one) together with every file's size and SHA-256. So whether a cell is covered never
depends on what has been downloaded yet (the cell files' cache path depends on it), and a download is checked before
it is kept. Without an index (offline, first run) the feature is off and Overture serves, asked again after
INDEX_RETRY_S. Downloads go through netfetch's "lod1" lane of raw.githubusercontent.com; when one fails the cell's
build is deferred (netfetch's errors propagate), never completed from Overture, so a cell is not cached with the
wrong source.

File format (README of the dataset): header, tile table, block table, then DEFLATE blocks of six streams
[anchor_x, anchor_y, base_z, heights, rings, vertices]. Coordinates are EPSG:2180 metres (x east), converted to
lon/lat by pl1992 (pure numpy, no pyproj). An "ortho" ring is a rotated rectilinear outline: `k` is its angle in
units of 90 deg / 16384, `swap` says its first edge runs along the rotated y axis (checked on real tiles: the
corners of such rings coincide with the neighbours' ten times more often than for any other reading).
"""
import hashlib
import json
import logging
import math
import mmap
import os
import threading
import time
import zlib
from collections import OrderedDict

import numpy as np

import pl1992

log = logging.getLogger("polandlod1")

ENV = "BNE_PL_LOD1"
REPO, BRANCH = "maalos/LOD1_2024_PL", "master"
REMOTE_DIR = "bld"              # the files' folder in the repository
BASE_URL = f"https://raw.githubusercontent.com/{REPO}/refs/heads/{BRANCH}"     # (what github.com/<repo>/raw/... leads to)
INDEX_URL = f"{BASE_URL}/index.npz"
FILE_URL = f"{BASE_URL}/{REMOTE_DIR}/{{name}}"
INDEX_MAX_AGE = 7 * 86400       # s after which the cached index is fetched again
INDEX_RETRY_S = 60.0            # s before an index that could not be had is asked for again
LANE = "lod1"                   # netfetch lane: its own policy (netfetch.POLICIES["raw.githubusercontent.com#lod1"])
VERSION = 1                     # bump when what a cell's buildings contain changes
DATASET = "GUGiK LOD1 2024"     # buildings.py trusts it as a footprint source (not an ML one)
PROVENANCE_ID = "gugik-lod1"
ATTRIBUTION = "Building footprints and heights: GUGiK (Head Office of Geodesy and Cartography), LOD1 2024"
ANGLE_UNIT = math.pi / 2 / 16384
MARGIN_M = 60.0                 # tile extents are padded by this when picking tiles for a cell
BLOCKS_KEPT = 4                 # decoded blocks kept (a block is a few tens of thousands of buildings)
_SAMPLES = 9                    # points per side of a cell, to bound it in EPSG:2180


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


def _svarint(buf, o):
    v, o = _uvarint(buf, o)
    return (v >> 1) ^ -(v & 1), o


def _varints(buf):
    """All the varints of a stream, as int64 (vectorised: a byte below 128 ends a value)."""
    a = np.frombuffer(buf, np.uint8)
    if not len(a):
        return np.zeros(0, np.int64)
    ends = np.flatnonzero(a < 128)
    starts = np.empty_like(ends)
    starts[0] = 0
    starts[1:] = ends[:-1] + 1
    ln = ends - starts + 1
    out = np.zeros(len(ends), np.int64)
    for i in range(int(ln.max())):
        m = ln > i
        out[m] |= (a[starts[m] + i].astype(np.int64) & 127) << (7 * i)
    return out


def _zigzag(v):
    return (v >> 1) ^ -(v & 1)


# --- file header ---------------------------------------------------------------------------------
class Header:
    """The tile and block tables of one .bld file."""

    def __init__(self, buf):
        if bytes(buf[:4]) != b"GBLD" or buf[4] != 2:
            raise ValueError("not a GBLD v2 file")
        self.flags, self.xy_cm, self.z_cm = buf[5], buf[6], buf[7]
        n, o = _uvarint(buf, 8)
        tiles, px, py = [], 0, 0
        for _ in range(n):
            dx, o = _svarint(buf, o)
            dy, o = _svarint(buf, o)
            ex, o = _uvarint(buf, o)
            ey, o = _uvarint(buf, o)
            c, o = _uvarint(buf, o)
            px += dx
            py += dy
            tiles.append((px, py, ex, ey, c))
        nb, o = _uvarint(buf, o)
        blocks = []
        for _ in range(nb):
            t, o = _uvarint(buf, o)
            m, o = _uvarint(buf, o)
            blocks.append((t, m >> 1, m & 1))
        self.tiles, self.blocks = tiles, blocks
        offs, pos = [], o
        for _, ln, _ in blocks:
            offs.append(pos)
            pos += ln
        self.block_offsets = offs
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
                return Header(buf)
            except IndexError:
                if want >= size:
                    raise
                want *= 4


# --- one block -----------------------------------------------------------------------------------
class Block:
    """The buildings of one block: coords (N, 2) EPSG:2180 metres, ring_off (R + 1), ring_hole (R),
    bld_ring_off (B + 1), bbox (B, 4) x0 y0 x1 y1, anchor (B, 2) in decimetres, height (B) metres (NaN: none)."""
    __slots__ = ("coords", "ring_off", "ring_hole", "bld_ring_off", "bbox", "anchor", "height")


def _ortho(p0, k, swap, steps, n):
    th = k * ANGLE_UNIT
    u = np.array([math.cos(th), math.sin(th)])
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
    o, streams = 0, []
    for _ in range(6):
        n, o = _uvarint(raw, o)
        streams.append(raw[o:o + n])
        o += n
    ax_s, ay_s, _bz, ht_s, rg_s, vx_s = streams
    spm = 100.0 / header.xy_cm
    counts = [header.tiles[first_tile + i][4] for i in range(tiles_in_block)]
    total = sum(counts)
    ax = _zigzag(_varints(ax_s))
    ay = _zigzag(_varints(ay_s))
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
            if r:
                sx += int(vs[vp])
                sy += int(vs[vp + 1])
                vp += 2
            p0 = np.array([sx, sy], np.float64)
            if not ortho:
                d = vs[vp:vp + 2 * (n - 1)].reshape(-1, 2).astype(np.float64)
                vp += 2 * (n - 1)
                pts = p0 + np.vstack([[0.0, 0.0], np.cumsum(d, axis=0)])
            else:
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
    EPSG:2180 metres) with the file and block it sits in. `digest` identifies this index (the cache identity)."""

    def __init__(self, path):
        with np.load(path) as z:
            raw = bytes(z["meta"])
            meta = json.loads(raw.decode())
            self.tile_file = z["tile_file"].astype(np.int64)
            self.tile_blk = z["tile_blk"].astype(np.int64)
            self.x0 = z["tile_x0"].astype(np.float64)
            self.y0 = z["tile_y0"].astype(np.float64)
            self.x1 = self.x0 + z["tile_ex"]
            self.y1 = self.y0 + z["tile_ey"]
        self.digest = hashlib.sha256(raw).hexdigest()
        self.names = [f[0] for f in meta["files"]]
        self.sizes = [int(f[1]) for f in meta["files"]]
        self.sha = [f[2] for f in meta["files"]]
        self.buildings = [int(f[3]) for f in meta["files"]]

    @staticmethod
    def url(name):
        return FILE_URL.format(name=name)


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


def _load_manifest(d, kind="demand"):
    """The index of the local cache folder `d`: <d>/index.npz, fetched from the repository when it is not there yet
    and again when it is older than INDEX_MAX_AGE (a refresh that fails keeps the old one). None without any."""
    import netfetch
    path = os.path.join(d, "index.npz")
    age = time.time() - os.path.getmtime(path) if os.path.exists(path) else None
    if age is None or age > INDEX_MAX_AGE:
        try:
            body = _get(INDEX_URL, kind)
            tmp = os.path.join(d, f"index.{os.getpid()}.check")         # (it must load before it replaces the old)
            _save(tmp, body)
            try:
                Manifest(tmp)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except (netfetch.FetchError, OSError, ValueError, KeyError) as e:
            log.warning("polandlod1: index %s not fetched (%s: %s)%s", INDEX_URL, type(e).__name__, e,
                        "; the one on disk stays" if age is not None else "")
    try:
        return Manifest(path)
    except Exception as e:
        if os.path.exists(path):
            log.warning("polandlod1: %s unreadable (%s: %s)", path, type(e).__name__, e)
        return None


# --- the files ------------------------------------------------------------------------------------
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

    def path(self, fi):
        return os.path.join(self.folder, self.names[fi])

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
        log.info("polandlod1: fetched %s (%d KB)", self.names[fi], len(body) // 1024)

    def header(self, fi, kind="demand"):
        with self._lock:
            h = self._headers.get(fi)
        if h is None:
            h = read_header(self.ensure(fi, kind))
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
        with open(self.path(fi), "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            _, ln, defl = h.blocks[bi]
            off = h.block_offsets[bi]
            raw = bytes(mm[off:off + ln])
        if defl:
            raw = zlib.decompress(raw, -15)
        blk = decode_block(raw, h, h.block_first_tile[bi], h.blocks[bi][0])
        with self._lock:
            self._blocks[(fi, bi)] = blk
            while len(self._blocks) > BLOCKS_KEPT:
                self._blocks.popitem(last=False)
        return blk


_index = None
_state = {"cache": None, "fetcher": None, "retry_at": 0.0}
_index_lock = threading.Lock()
_covers = {}


def configure(cache_dir, fetcher):
    """MapData's cache folder (everything goes to <cache>/polandlod1) and its netfetch.Fetcher (all downloads go
    through it)."""
    with _index_lock:
        _state.update(cache=cache_dir, fetcher=fetcher, retry_at=0.0)


def on():
    """True when the player asked for it: BNE_PL_LOD1=1."""
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes", "on")


def folder():
    """Where the index and the files are kept ('' when the cache folder is not known yet)."""
    return os.path.join(_state["cache"], "polandlod1") if _state["cache"] else ""


def index():
    """The Index of the local cache folder, None when off or while the index cannot be had (then Overture serves;
    asked again after INDEX_RETRY_S)."""
    global _index
    if not on():
        return None
    d = folder()
    if not d:
        return None
    with _index_lock:
        if _index is not None and _index.folder == d:
            return _index
        if time.monotonic() < _state["retry_at"]:
            return None
        m = _load_manifest(d)
        if m is None:
            _state["retry_at"] = time.monotonic() + INDEX_RETRY_S
            return None
        _covers.clear()
        _index = Index(d, m)
        log.info("polandlod1: %d files, %d buildings, kept in %s", len(m.names), _index.n_buildings, d)
        return _index


def enabled():
    return index() is not None


def signature():
    """What the cell files' identity needs on top of VERSION (None when off): the index in use, not what happens
    to be on disk."""
    i = index()
    if i is None:
        return None
    m = i.manifest
    return {"version": VERSION, "index": m.digest[:16], "files": len(m.names), "buildings": sum(m.buildings)}


def _in_poland_only(lats, lons):
    """True when every point is in Poland (the bundled borders); False when unknown or not."""
    try:
        import countrybounds
        codes = countrybounds.bundled().codes_at([(float(a), float(o)) for a, o in zip(lats, lons)])
    except Exception:
        return False
    return len(codes) > 0 and set(codes) == {"PL"}


def _cell_box(bounds):
    """(lons, lats of a grid over the cell, EPSG:2180 box of it)."""
    lon0, lat0, lon1, lat1 = bounds
    lo, la = np.meshgrid(np.linspace(lon0, lon1, _SAMPLES), np.linspace(lat0, lat1, _SAMPLES))
    x, y = pl1992.from_lonlat(lo.ravel(), la.ravel())
    return lo.ravel(), la.ravel(), (x.min(), y.min(), x.max(), y.max())


def covers(key):
    """True when the cell's buildings come from LOD1: wholly in Poland, and the files have tiles there."""
    idx = index()
    if idx is None:
        return False
    k = str(key)
    got = _covers.get(k)
    if got is None:
        lo, la, box = _cell_box(key.bounds())
        got = bool(idx.blocks_for(*box)) and _in_poland_only(la, lo)
        if len(_covers) > 8192:
            _covers.clear()
        _covers[k] = got
    return got


# --- a cell's Features ---------------------------------------------------------------------------
def query(key, kind="demand"):
    """(mapgeom.Features, {(dataset, licence): count}) of the cell's buildings, Overture's columns; None when the
    cell is not covered (the caller uses Overture). The files it needs are fetched first (kind: netfetch's request
    kind, so a prefetch only takes what the host has room for); their errors are raised."""
    import mapgeom as mg
    import overture
    if not covers(key):
        return None
    idx = index()
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
