"""Near-field TerrainBlock heightmap (docs/V1_2_DESIGN.md §1, §12 P1).

SIZE x SIZE samples at exactly s = SQUARE m (1024 x 1 m), on a world-locked lattice: the block centre (cx, cy)
is a multiple of grid() in the anchor's ENU plane, the block position (SW corner) is (cx - HALF, cy - HALF, zBase),
and sample (gx, gy) sits at (cx - HALF + s gx, cy - HALF + s gy). Coordinates are computed as integer lattice index x
s, so the same world point yields bit-identical lat/lon (and DEM value) in every block. The last row/column hold real
DEM data.

The engine (probe P1) computes h = zBase + q * maxHeight / 65536. With a fixed per-anchor
height quantum Q (a power of two), maxHeight = Q * 65536 and zBase a multiple of Q, every
stored height is Q * round(u / Q) whatever the block, so overlapping samples of two blocks are
identical. Physics covers [x0, x0 + (N-1) s] per axis (P1); the last square's strip on the N/E edges is left to
the far tiles.

The value of a sample is the ENU z of the DEM surface point under it. Stored as a 16-bit
grayscale PNG, row 0 = north.

The block: SIZE² samples SQUARE apart (1024² at 1 m since 2026-11-01, the stock maps' resolution; it was 512² at
2 m, and 1024² at 2 m in the v1.3.1 terrain mode), collidable [c-512, c+511], centred on a 128 m grid (grid(): by the
block's width in metres). Its SW corner c - 512 is then a multiple of 128 m, an even lattice index, so the block-local
Torque diagonal parity is the world one (farfield.py torque_split45), and a 128 m move is exactly 128 samples. The
functions that depend on the block size take it as a parameter (`size`, samples per edge; the server passes its
Layout's `block`), defaulting to SIZE; nothing reads a module setting at call time. The far field's near tiles keep a
2 m lattice (FAR_SQUARE): every second block sample, the same values.
"""
from runtimeprofiler import step as profile_step
import math
import os
import fast
import geodesy
import threading
from collections import OrderedDict

import numpy as np
import staging
from PIL import Image



def ground_square(environ=None):
    """The session's block square (m between samples): BNGE_GROUND_SQUARE, 1 (the default: the stock maps' resolution)
    or 2 (the lighter ground of the Low and Medium quality presets, GitHub #943: the same kilometre in 512² samples, a
    quarter of the .ter to parse at each swap; the launcher sets it from the settings page's groundGrid). Read once, at
    import: every block-size rule below and in the modules using them follows it."""
    v = (os.environ if environ is None else environ).get("BNGE_GROUND_SQUARE", "").strip()
    try:
        sq = float(v) if v else 1.0
    except ValueError:
        sq = 1.0
    return sq if sq in GROUND_SQUARES else 1.0


GROUND_SQUARES = (1.0, 2.0)
SQUARE = ground_square()            # m; exactly representable, so the lattice is exact
SIZE = int(1024 / SQUARE)           # samples per edge of the 1 km block (1024 at 1 m, 512 at 2 m)
FAR_SQUARE = 2.0                    # m: the far field's near tiles' lattice (every second block sample)
HALF = SIZE * SQUARE / 2            # block position = centre - HALF
VALID = (SIZE - 1) * SQUARE         # 1023 m: collidable extent from the SW corner (P1)
GRID_BY_M = {1024.0: 128.0, 2048.0: 256.0}  # the centre grid (m) by the block's width (m)
GRID = GRID_BY_M[SIZE * SQUARE]     # block centres are multiples of this
CHUNK = int(128 / SQUARE)           # samples: ChunkCache's chunk, 128 m (the block's follow grid)
CHUNK_CACHE = 512                   # chunks kept per frame (128 KB each: 64 MB, 8 blocks)
CHUNK_GEO_CACHE = 128               # ... and their lat / lon this many (256 KB each: 32 MB, 2 blocks; PR #786: a moved
                                    # block's own chunks are all it reads them for, and all 512 were +128 MB a process)
# Height quanta (m), finest first: 65536 Q = 64 ... 4096 m of range per block. On the 1 m ground the rounding is a
# curvature of ~Q per m² the tyres feel: a quarter car's roughness (IRI) of Phoenix's roads (bumpscan, 2026-11-12) was
# 3.2 m/km at 1/128, 1.8 at 1/256, 1.25 at 1/512, 0.99 at 1/1024 (0.82 unrounded): a flat place's blocks take 1/1024
# or 1/512 now (a block's relief within 48 / 112 m).
Q_LADDER = (1 / 1024, 1 / 512, 1 / 256, 1 / 128, 1 / 64, 1 / 32, 1 / 16)
Q_DEFAULT = 1 / 64
ZBASE_STEP = 8.0                    # zBase is a multiple of this (and so of every Q; 64 m took 1/1024's whole range)
ZBASE_MARGIN = 8.0                  # m below the lowest sample


def snap(v, grid=GRID):
    """Nearest multiple of grid, halves rounded up (Lua: floor(v/g + 0.5)*g)."""
    return float(math.floor(v / grid + 0.5) * grid) + 0.0


def half_extent(size=SIZE):
    """Block position = centre - this (512 m for the 1024² block at 1 m)."""
    return size * SQUARE / 2


def grid(size=SIZE):
    """The centre grid of a `size`² block, by its width in metres (128 m for 1 km, 256 m for 2 km)."""
    return GRID_BY_M[size * SQUARE]


def valid(size=SIZE):
    """Collidable extent from the SW corner, (N-1) * s (P1): 1023 m for 1024² at 1 m."""
    return (size - 1) * SQUARE


def lattice_index(c, size=SIZE):
    """Lattice index of the first sample of a `size`² block centred at c (a multiple of SQUARE)."""
    k = (c - half_extent(size)) / SQUARE
    if k != math.floor(k):
        raise ValueError(f"block centre {c} is not on the {SQUARE:g} m lattice")
    return int(k)


def lattice_coords(k0, n):
    """World coordinates of n lattice samples starting at index k0 (exact: index x SQUARE)."""
    return (k0 + np.arange(n, dtype=np.int64)).astype(np.float64) * SQUARE


def sample_coords(cx, cy, size=SIZE):
    """World x of grid columns and y of grid rows (both length `size`)."""
    return lattice_coords(lattice_index(cx, size), size), lattice_coords(lattice_index(cy, size), size)


def surface_u(frame, elev_fn, xs, ys):
    """ENU z of the DEM surface under every plane point (xs[i], ys[j]), indexed [j, i].
    Elementwise, so a point gets the same value whichever grid it is part of."""
    return surface(frame, elev_fn, xs, ys)[1]


def surface(frame, elev_fn, xs, ys, keep=None):
    """(DEM elevation, ENU z of the surface point) under every plane point, indexed [j, i].
    keep (a dict, optional) receives the points' "lat" and "lon" (surface.py's land cover)."""
    fast.require(564, "terrain block coordinates")
    xs, ys = np.asarray(xs, np.float64), np.asarray(ys, np.float64)
    if type(frame) is geodesy.EnuFrame:
        lat, lon = fast._ef.block_coordinates(xs, ys, frame.origin, frame.rot)
    else:
        lat, lon, _ = frame.enu_to_geodetic(xs, ys[:, None], 0.0)
        lat, lon = (np.broadcast_to(np.asarray(a, np.float64), (len(ys), len(xs))) for a in (lat, lon))
    elev = elev_fn(lat, lon)
    if keep is not None:
        keep["lat"], keep["lon"] = lat, lon
    if type(frame) is geodesy.EnuFrame:
        u = fast._ef.block_up(lat, lon, np.broadcast_to(np.asarray(elev, np.float64), lat.shape), frame.origin, frame.rot)
    else:
        u = np.asarray(frame.geodetic_to_enu(lat, lon, elev)[2], np.float64)
    return elev, u


def grid_u(frame, elev_fn, cx=0.0, cy=0.0, size=SIZE, keep=None):
    """ENU z for every grid sample, indexed [gy, gx] (gy = north), all from real data. keep: as
    surface()."""
    xs, ys = sample_coords(cx, cy, size)
    return surface(frame, elev_fn, xs, ys, keep)[1]


class ChunkCache:
    """Surface heights (grid_u's values) and optional geodetic coordinates per CHUNK×CHUNK lattice chunk of one frame,
    so a block that moved by a grid step computes only its new samples (a 512² block moving 128 m shares 7/8 of its samples
    with the last; computing all of them every move took 2-10 s, more than the 2.2 s between moves at
    58 m/s, 2026-10-12). Exact: the heights and coordinates are elementwise (surface_u). LRU, thread-safe."""

    def __init__(self, cap=CHUNK_CACHE):
        self.cap = cap
        self.chunks = OrderedDict()             # (kx, ky) lattice chunk -> (CHUNK, CHUNK) float64 [j, i]
        self.geo = OrderedDict()                # (kx, ky) lattice chunk -> ((CHUNK, CHUNK) lat, (CHUNK, CHUNK) lon)
        self.dirty = set()                      # chunks computed in a build that read something short (blockcache.py:
                                                # never kept on disk, nor any block made with them)
        self.lock = threading.Lock()

    def get(self, k):
        with self.lock:
            got = self.chunks.get(k)
            if got is not None:
                self.chunks.move_to_end(k)
                if k in self.geo:
                    self.geo.move_to_end(k)
            return got

    def get_geo(self, k):
        with self.lock:
            if k not in self.chunks:
                self.geo.pop(k, None)
                return None
            got = self.geo.get(k)
            if got is not None:
                self.geo.move_to_end(k)
            return got

    def _trim_geo(self):
        while len(self.geo) > CHUNK_GEO_CACHE:
            self.geo.popitem(last=False)

    def put_geo(self, k, geo):
        with self.lock:
            if k in self.chunks and geo is not None:
                self.geo[k] = geo
                self.geo.move_to_end(k)
                self._trim_geo()

    def mark_dirty(self, ks):
        with self.lock:
            self.dirty.update(k for k in ks if k in self.chunks)

    def any_dirty(self, ks):
        with self.lock:
            return any(k in self.dirty for k in ks)

    def put(self, k, u, geo=None):
        with self.lock:
            self.dirty.discard(k)
            self.chunks[k] = u
            self.chunks.move_to_end(k)
            if geo is not None:
                self.geo[k] = geo
                self.geo.move_to_end(k)
            while len(self.chunks) > self.cap:
                evicted, _ = self.chunks.popitem(last=False)
                self.geo.pop(evicted, None)
                self.dirty.discard(evicted)
            self._trim_geo()

    def clear(self):
        with self.lock:
            self.chunks.clear()
            self.geo.clear()
            self.dirty.clear()


def block_chunks(cx, cy, size=SIZE):
    """[(ci, cj, (kx, ky))] of the CHUNK x CHUNK lattice chunks of the size² block centred at (cx, cy), row by row from
    the south-west, or None when the block is not on the chunk grid."""
    kx0, ky0 = lattice_index(cx, size), lattice_index(cy, size)
    if kx0 % CHUNK or ky0 % CHUNK or size % CHUNK:
        return None
    nc = size // CHUNK
    return [(ci, cj, (kx0 // CHUNK + ci, ky0 // CHUNK + cj)) for cj in range(nc) for ci in range(nc)]


def chunk_grid(cx, cy, size, cache, disk):
    """The block's heights from its chunks alone: the memory's (a ChunkCache, its dirty ones aside) and the disk's
    (disk: blockcache.ChunkDisk; those it has are put in the memory too). None when one is in neither: nothing is
    computed here."""
    kx, ky = lattice_index(cx, size), lattice_index(cy, size)
    if kx % CHUNK or ky % CHUNK or size % CHUNK:
        return None
    fast.require(564, "cached terrain chunks")
    return fast._ef.block_cached_grid(size, CHUNK, (kx // CHUNK, ky // CHUNK), cache, disk)


def grid_u_chunked(frame, elev_fn, cx, cy, size, cache, keep=None, disk=None):
    """grid_u through a ChunkCache: elev_fn runs once, on the samples of the chunks the cache lacks.
    disk (blockcache.ChunkDisk, optional): the chunks the memory lacks read from the disk first; the ones computed
    here are handed to it (disk.computed) for the caller to keep once the block is known complete.
    Returns (u, the share of samples computed)."""
    kx0, ky0 = lattice_index(cx, size), lattice_index(cy, size)
    if kx0 % CHUNK or ky0 % CHUNK or size % CHUNK:
        return grid_u(frame, elev_fn, cx, cy, size, keep), 1.0

    fast.require(564, "terrain block chunk grid")
    xs, ys = sample_coords(cx, cy, size)
    args = xs, ys, (kx0 // CHUNK, ky0 // CHUNK), CHUNK, cache, disk, keep, elev_fn
    if type(frame) is geodesy.EnuFrame:
        return fast._ef.block_chunk_grid(frame.origin, frame.rot, *args)
    return fast._ef.block_chunk_grid(None, None, *args, frame)


def quantise(u, q):
    """Global quantum index round(u / Q) (exact: Q is a power of two)."""
    return np.rint(np.asarray(u, np.float64) / q)


def choose_q(relief, ladder=Q_LADDER):
    """Smallest quantum whose block range (65535 Q) covers the relief plus 10 % and the zBase
    rounding (ZBASE_STEP + ZBASE_MARGIN)."""
    need = relief * 1.1 + ZBASE_STEP + ZBASE_MARGIN
    for q in ladder:
        if 65535 * q >= need:
            return q
    return ladder[-1]


def block_q(relief, ladder=Q_LADDER):
    """Smallest quantum of the ladder a block with this relief fits (block_params' rule: its range above the rounded
    down zBase, ZBASE_STEP + ZBASE_MARGIN at most, within 65534 Q)."""
    need = relief + ZBASE_STEP + ZBASE_MARGIN
    for q in ladder:
        if need <= 65534 * q:
            return q
    return ladder[-1]


def block_params(u_min, u_max, q):
    """(Q, zBase, maxHeight) for a block with this height range. zBase is a multiple of
    ZBASE_STEP (so of Q); if the block does not fit the anchor's Q, the next coarser quantum
    is used for this block only (a one-time difference of <= Q/2 at its seams)."""
    fast.require(564, "terrain height range")
    return fast._ef.block_params(u_min, u_max, q, Q_LADDER, ZBASE_STEP, ZBASE_MARGIN)


def write_png(path, u, q=Q_DEFAULT):
    """Quantise on the global Q lattice and write the PNG atomically.
    Returns (Q, zBase, maxHeight, qgrid)."""
    q, z_base, max_height = block_params(float(u.min()), float(u.max()), q)
    fast.require(564, "terrain height quantization")
    qgrid = fast._ef.block_quantize(np.asarray(u, np.float64), q, z_base)
    img = Image.fromarray(np.ascontiguousarray(qgrid[::-1]))   # flip: row 0 = north (max gy)
    # Deflate level 1: 1.2 MB in 29 ms here, level 0 2.1 MB in 55 ms (1024², 2026-11-04). The game decodes it only
    # when it imports a block (an anchor's first, or when the .ter swap cannot be used: terrain.lua swap); level 0
    # imported ~7 ms faster (43 vs 50 ms) when every commit imported, now a few per session. The rest of the time it
    # was ~0.3 GB of SSD writes in a warm 8 min drive at 150 mph.
    with staging.staged_write(path) as tmp:
        img.save(tmp, format="PNG", compress_level=1)
    return q, z_base, max_height, qgrid


TER_VERSION = 9
TER_HOLE = 255          # a .ter layer index: no ground there (drawn or colliding)


def write_ter(path, qgrid, idx, materials, source_ids=None):
    """The block as the engine's own terrain file (.ter v9: version u8, size u32, size² u16 heights,
    size² u8 material indices, material count u32, names as u8 length + bytes), written atomically.
    Rows as qgrid's: row 0 = south (the engine's sample (x, y) is at block-local (x, y) * squareSize).
    idx: the material index per sample (None: material 0 everywhere); materials: their names. The mod
    swaps it in with terrainFile + postApply: ~11 ms for 512² and collision follows without a
    reloadCollision (docs/research/2026-10-12-engine-limits-and-worldgen.md §7), against ~30 ms of
    importMaps + reloadCollision and a ~20 ms physics frame."""
    fast.require(564, "terrain file")
    with staging.staged_write(path) as tmp:
        fast._ef.block_write_ter(tmp, np.asarray(qgrid, np.uint16), None if idx is None else np.asarray(idx, np.uint8),
                                 materials, source_ids)


def heights(qgrid, z_base, max_height):
    """Stored heights of a quantised grid, as the engine computes them (float64 here)."""
    return z_base + qgrid.astype(np.float64) * (max_height / 65536)


def height_at(qgrid, z_base, max_height, cx, cy, x, y, square=SQUARE):
    """Bilinear height of the quantised grid (a block of qgrid.shape[0]² samples) at world (x, y),
    or None outside the valid (collidable) area. This is what the game's terrain reports there."""
    fast.require(564, "quantized terrain height")
    return fast._ef.block_height_at(np.asarray(qgrid), z_base, max_height, (cx, cy), (x, y), square)


@profile_step('Heightmap generation')
def generate(frame, elev_fn, path, cx=0.0, cy=0.0, ground_at=(0.0, 0.0), q=Q_DEFAULT, size=SIZE, keep=None,
             chunks=None, post=None, disk=None):
    """Build the size² block centred at (cx, cy) with height quantum q and write it to path.
    groundZ is the terrain height at ground_at (None if that point is outside the block). keep (a
    dict, optional) receives "lat", "lon" and "u" of the grid ([gy, gx], gy = north) and write_png's
    "qgrid". chunks: a ChunkCache of this frame (the heights it holds are reused, the new ones added).
    post(u, keep): the grid's heights changed before they are written (a provisional block's L0 roads, l0roads.py;
    needs keep, never with chunks: the chunks keep the heights as computed). disk: grid_u_chunked's (with chunks)."""
    if chunks is None:
        u = grid_u(frame, elev_fn, cx, cy, size, keep)
    else:
        u, share = grid_u_chunked(frame, elev_fn, cx, cy, size, chunks, keep, disk)
        if keep is not None:
            keep["new_share"] = share       # (the share of samples computed: the rest from earlier blocks' chunks)
    if post is not None and keep is not None:
        u = post(u if chunks is None else u.copy(), keep)
    q, z_base, max_height, qgrid = write_png(path, u, q)
    if keep is not None:
        keep["u"] = u
        keep["qgrid"] = qgrid
    gz = height_at(qgrid, z_base, max_height, cx, cy, *ground_at) if ground_at else None
    return {"heightmap": os.path.basename(path), "cx": cx, "cy": cy, "zBase": z_base, "maxHeight": max_height, "q": q,
            "groundZ": None if gz is None else round(gz, 3),
            "minU": float(u.min()), "maxU": float(u.max())}
