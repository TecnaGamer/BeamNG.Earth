"""Near-field TerrainBlock heightmap (docs/V1_2_DESIGN.md §1, §12 P1).

1024 x 1024 samples at exactly s = 2.0 m, on a world-locked lattice: the block centre (cx, cy)
is a multiple of 256 m in the anchor's ENU plane, the block position (SW corner) is
(cx - 1024, cy - 1024, zBase), and sample (gx, gy) sits at (cx - 1024 + 2 gx, cy - 1024 + 2 gy).
Coordinates are computed as integer lattice index x 2.0, so the same world point yields
bit-identical lat/lon (and DEM value) in every block; a 256 m terrain-follow move is exactly
128 samples. Row/column 1023 hold real DEM data.

The engine (probe P1) computes h = zBase + q * maxHeight / 65536. With a fixed per-anchor
height quantum Q (a power of two), maxHeight = Q * 65536 and zBase a multiple of Q, every
stored height is Q * round(u / Q) whatever the block, so overlapping samples of two blocks are
identical. Physics covers [x0, x0 + (N-1) s] = 2046 m per axis (P1); the 2 m strip up to
x0 + 2048 on the N/E edges is left to the far tiles.

The value of a sample is the ENU z of the DEM surface point under it. Stored as a 16-bit
grayscale PNG, row 0 = north.

SIZE, HALF, VALID and GRID describe the v1.3.1 block. The functions that depend on the block size
take it as a parameter (`size`, samples per edge; the server passes its Layout's `block`,
docs/V1_4_SPEC.md §3.1), defaulting to SIZE; nothing reads a module setting at call time.

M4 (docs/V1_4_SPEC.md §3.5, `earthd.py --block 512`): a 512² block, collidable [c-512, c+510],
centred on a 128 m grid (GRIDS). Its SW corner c - 512 is then a multiple of 128 m, an even lattice
index, so the block-local Torque diagonal parity is the world one, as with 1024² (farfield.py
torque_split45), and a 128 m move is exactly 64 samples.
"""
import math
import os
import struct
import threading
from collections import OrderedDict

import numpy as np
import staging
from PIL import Image

SIZE = 1024
SQUARE = 2.0                        # m; exactly representable, so the lattice is exact
HALF = 1024.0                       # block position = centre - HALF
VALID = (SIZE - 1) * SQUARE         # 2046 m: collidable extent from the SW corner (P1)
GRID = 256.0                        # block centres are multiples of this (= 128 samples)
GRIDS = {1024: GRID, 512: 128.0}    # the centre grid per block size (M4: 512² on 128 m = 64 samples)
CHUNK_ROWS = 256                    # bounds the geodesy temporaries
CHUNK = 64                          # samples: ChunkCache's chunk, 128 m (the 512² block's follow grid)
CHUNK_CACHE = 1024                  # chunks kept per frame (32 KB each: 32 MB)
# Height quanta (m), finest first: 65536 Q = 256 ... 4096 m of range per block.
Q_LADDER = (1 / 256, 1 / 128, 1 / 64, 1 / 32, 1 / 16)
Q_DEFAULT = 1 / 64
ZBASE_STEP = 64.0                   # zBase is a multiple of this (and so of every Q)
ZBASE_MARGIN = 8.0                  # m below the lowest sample


def snap(v, grid=GRID):
    """Nearest multiple of grid, halves rounded up (Lua: floor(v/g + 0.5)*g)."""
    return float(math.floor(v / grid + 0.5) * grid) + 0.0


def half_extent(size=SIZE):
    """Block position = centre - this (1024 m for the 1024² block)."""
    return size * SQUARE / 2


def grid(size=SIZE):
    """The centre grid of a `size`² block (256 m for 1024², 128 m for 512²)."""
    return GRIDS[size]


def valid(size=SIZE):
    """Collidable extent from the SW corner, (N-1) * s (P1): 2046 m for 1024², 1022 m for 512²."""
    return (size - 1) * SQUARE


def lattice_index(c, size=SIZE):
    """Lattice index of the first sample of a `size`² block centred at c (a multiple of 2 m)."""
    k = (c - half_extent(size)) / SQUARE
    if k != math.floor(k):
        raise ValueError(f"block centre {c} is not on the 2 m lattice")
    return int(k)


def lattice_coords(k0, n):
    """World coordinates of n lattice samples starting at index k0 (exact: index x 2.0)."""
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
    ny, nx = len(ys), len(xs)
    lat = np.empty((ny, nx))
    lon = np.empty((ny, nx))
    for r0 in range(0, ny, CHUNK_ROWS):
        e, n = np.meshgrid(xs, ys[r0:r0 + CHUNK_ROWS])
        lat[r0:r0 + CHUNK_ROWS], lon[r0:r0 + CHUNK_ROWS], _ = frame.enu_to_geodetic(e, n, 0.0)
    elev = elev_fn(lat, lon)
    if keep is not None:
        keep["lat"], keep["lon"] = lat, lon
    u = np.empty((ny, nx))
    for r0 in range(0, ny, CHUNK_ROWS):
        s = slice(r0, r0 + CHUNK_ROWS)
        # Spec step 5: z of the surface point; its tiny horizontal shift is ignored.
        u[s] = frame.geodetic_to_enu(lat[s], lon[s], elev[s])[2]
    return elev, u


def grid_u(frame, elev_fn, cx=0.0, cy=0.0, size=SIZE, keep=None):
    """ENU z for every grid sample, indexed [gy, gx] (gy = north), all from real data. keep: as
    surface()."""
    xs, ys = sample_coords(cx, cy, size)
    return surface(frame, elev_fn, xs, ys, keep)[1]


class ChunkCache:
    """Surface heights (grid_u's values) per CHUNK×CHUNK lattice chunk of one frame, so a block that moved
    by a grid step computes only its new samples (a 512² block moving 128 m shares 7/8 of its samples
    with the last; computing all of them every move took 2-10 s, more than the 2.2 s between moves at
    58 m/s, 2026-10-12). Exact: the heights are elementwise (surface_u). LRU, thread-safe."""

    def __init__(self, cap=CHUNK_CACHE):
        self.cap = cap
        self.chunks = OrderedDict()             # (kx, ky) lattice chunk -> (CHUNK, CHUNK) float64 [j, i]
        self.lock = threading.Lock()

    def get(self, k):
        with self.lock:
            got = self.chunks.get(k)
            if got is not None:
                self.chunks.move_to_end(k)
            return got

    def put(self, k, u):
        with self.lock:
            self.chunks[k] = u
            self.chunks.move_to_end(k)
            while len(self.chunks) > self.cap:
                self.chunks.popitem(last=False)


def grid_u_chunked(frame, elev_fn, cx, cy, size, cache, keep=None):
    """grid_u through a ChunkCache: elev_fn runs once, on the samples of the chunks the cache lacks.
    Returns (u, the share of samples computed)."""
    kx0, ky0 = lattice_index(cx, size), lattice_index(cy, size)
    if kx0 % CHUNK or ky0 % CHUNK or size % CHUNK:
        return grid_u(frame, elev_fn, cx, cy, size, keep), 1.0
    xs, ys = sample_coords(cx, cy, size)
    lat = np.empty((size, size))
    lon = np.empty((size, size))
    for r0 in range(0, size, CHUNK_ROWS):
        e, n = np.meshgrid(xs, ys[r0:r0 + CHUNK_ROWS])
        lat[r0:r0 + CHUNK_ROWS], lon[r0:r0 + CHUNK_ROWS], _ = frame.enu_to_geodetic(e, n, 0.0)
    if keep is not None:
        keep["lat"], keep["lon"] = lat, lon
    nc = size // CHUNK
    u = np.empty((size, size))
    missing = []
    for cj in range(nc):
        for ci in range(nc):
            k = ((kx0 // CHUNK) + ci, (ky0 // CHUNK) + cj)
            got = cache.get(k)
            if got is None:
                missing.append((ci, cj, k))
            else:
                u[cj * CHUNK:(cj + 1) * CHUNK, ci * CHUNK:(ci + 1) * CHUNK] = got
    if missing:
        sl = [(slice(cj * CHUNK, (cj + 1) * CHUNK), slice(ci * CHUNK, (ci + 1) * CHUNK)) for ci, cj, _ in missing]
        mla = np.concatenate([lat[a, b].ravel() for a, b in sl])
        mlo = np.concatenate([lon[a, b].ravel() for a, b in sl])
        elev = np.asarray(elev_fn(mla, mlo), np.float64)
        mu = np.empty(len(mla))
        for r0 in range(0, len(mla), CHUNK_ROWS * size):
            r = slice(r0, r0 + CHUNK_ROWS * size)
            mu[r] = frame.geodetic_to_enu(mla[r], mlo[r], elev[r])[2]
        for n_, ((a, b), (_, _, k)) in enumerate(zip(sl, missing)):
            c = mu[n_ * CHUNK * CHUNK:(n_ + 1) * CHUNK * CHUNK].reshape(CHUNK, CHUNK)
            u[a, b] = c
            cache.put(k, c.copy())
    return u, len(missing) / (nc * nc)


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


def block_params(u_min, u_max, q):
    """(Q, zBase, maxHeight) for a block with this height range. zBase is a multiple of
    ZBASE_STEP (so of Q); if the block does not fit the anchor's Q, the next coarser quantum
    is used for this block only (a one-time difference of <= Q/2 at its seams)."""
    z_base = math.floor((u_min - ZBASE_MARGIN) / ZBASE_STEP) * ZBASE_STEP
    ladder = [x for x in Q_LADDER if x >= q] or [Q_LADDER[-1]]
    for qq in ladder:
        if u_max - z_base <= 65534 * qq:        # one quantum of headroom for the rounding
            q = qq
            break
    else:
        q = ladder[-1]
    return q, float(z_base), q * 65536


def write_png(path, u, q=Q_DEFAULT):
    """Quantise on the global Q lattice and write the PNG atomically (when path is not None).
    Returns (Q, zBase, maxHeight, qgrid)."""
    q, z_base, max_height = block_params(float(u.min()), float(u.max()), q)
    g = quantise(u, q) - round(z_base / q)          # integer arithmetic on exact floats
    qgrid = np.clip(g, 0, 65535).astype(np.uint16)
    if path is not None:
        img = Image.fromarray(np.ascontiguousarray(qgrid[::-1]))   # flip: row 0 = north (max gy)
        tmp = staging.tmp_for(path)
        # Uncompressed: the game decodes it on the main thread during the terrain-follow commit,
        # and level 0 imports ~7 ms faster than level 1 (43 vs 50 ms for 1024², measured in-game).
        img.save(tmp, format="PNG", compress_level=0)
        staging.replace(tmp, path)
    return q, z_base, max_height, qgrid


TER_VERSION = 9
TER_HOLE = 255          # a .ter layer index: no ground there (drawn or colliding)


def write_ter(path, qgrid, idx, materials):
    """The block as the engine's own terrain file (.ter v9: version u8, size u32, size² u16 heights,
    size² u8 material indices, material count u32, names as u8 length + bytes), written atomically.
    Rows as qgrid's: row 0 = south (the engine's sample (x, y) is at block-local (x, y) * squareSize).
    idx: the material index per sample (None: material 0 everywhere); materials: their names. The mod
    swaps it in with terrainFile + postApply: ~11 ms for 512² and collision follows without a
    reloadCollision (docs/research/2026-10-12-engine-limits-and-worldgen.md §7), against ~30 ms of
    importMaps + reloadCollision and a ~20 ms physics frame."""
    n = qgrid.shape[0]
    lay = np.zeros((n, n), np.uint8) if idx is None else np.array(idx, np.uint8)
    # the outermost samples are holes (TER_HOLE): the engine lights a block's edge cells as if it repeated
    # (their normals take the opposite edge's heights), a dark 2 m strip along an edge that is lower than
    # the opposite one (in-game 2026-10-13); the edge ring under them (0.3 m lower) shows instead. No
    # collision there: the edge guard keeps the car tens of metres inside.
    lay[0, :] = lay[-1, :] = lay[:, 0] = lay[:, -1] = TER_HOLE
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(struct.pack("<BI", TER_VERSION, n))
        f.write(np.ascontiguousarray(qgrid, "<u2").tobytes())
        f.write(np.ascontiguousarray(lay).tobytes())
        f.write(struct.pack("<I", len(materials)))
        for name in materials:
            b = name.encode()
            f.write(struct.pack("<B", len(b)) + b)
    staging.replace(tmp, path)


def heights(qgrid, z_base, max_height):
    """Stored heights of a quantised grid, as the engine computes them (float64 here)."""
    return z_base + qgrid.astype(np.float64) * (max_height / 65536)


def height_at(qgrid, z_base, max_height, cx, cy, x, y, square=SQUARE):
    """Bilinear height of the quantised grid (a block of qgrid.shape[0]² samples) at world (x, y),
    or None outside the valid (collidable) area. This is what the game's terrain reports there."""
    half = qgrid.shape[0] * square / 2
    x0, y0 = cx - half, cy - half
    n = qgrid.shape[0] - 1
    fx, fy = (x - x0) / square, (y - y0) / square
    if not (0 <= fx <= n and 0 <= fy <= n):
        return None
    ix, iy = min(int(fx), n - 1), min(int(fy), n - 1)
    wx, wy = fx - ix, fy - iy
    h = heights(qgrid[iy:iy + 2, ix:ix + 2], z_base, max_height)
    return float((h[0, 0] * (1 - wx) + h[0, 1] * wx) * (1 - wy) + (h[1, 0] * (1 - wx) + h[1, 1] * wx) * wy)


def generate(frame, elev_fn, path, cx=0.0, cy=0.0, ground_at=(0.0, 0.0), q=Q_DEFAULT, size=SIZE, keep=None,
             chunks=None):
    """Build the size² block centred at (cx, cy) with height quantum q and write it to path.
    groundZ is the terrain height at ground_at (None if that point is outside the block). keep (a
    dict, optional) receives "lat", "lon" and "u" of the grid ([gy, gx], gy = north) and write_png's
    "qgrid". chunks: a ChunkCache of this frame (the heights it holds are reused, the new ones added)."""
    if chunks is None:
        u = grid_u(frame, elev_fn, cx, cy, size, keep)
    else:
        u, _ = grid_u_chunked(frame, elev_fn, cx, cy, size, chunks, keep)
    q, z_base, max_height, qgrid = write_png(path, u, q)
    if keep is not None:
        keep["u"] = u
        keep["qgrid"] = qgrid
    gz = height_at(qgrid, z_base, max_height, cx, cy, *ground_at) if ground_at else None
    return {"cx": cx, "cy": cy, "zBase": z_base, "maxHeight": max_height, "q": q,
            "groundZ": None if gz is None else round(gz, 3),
            "minU": float(u.min()), "maxU": float(u.max())}
