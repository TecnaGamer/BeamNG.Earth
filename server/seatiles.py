"""The sea's water tiles near the player: square WaterBlocks that follow the Earth's curve (seacarve.py,
the mod's sea.lua; docs/research/2026-10-13-water-probe.md).

    tiles = plan(frame, x, y, sea_at)     # [Tile]: the anchor frame's sea tiles around (x, y)

A quadtree on a lattice of TILE_MAX squares in the anchor frame: a square is split while the player is
closer than SPLIT_K times its size (down to TILE_MIN), so the tiles are small near the player and large
far away (<= 34 over open sea; 83 tiles cost 3-9 % fps at Sandy Point, 2026-10-13). A leaf is kept when sea_at
says it holds sea. Each tile's surface is the sea level (seacarve.SEA_E) at its centre, its normal the
local vertical there, so its surface is at most (size/2)^2 / 2R off the true sea: 0.17 m at the corners
of 2048 m tiles, 1.3 m at the corners of 8192 m ones (which are >= 6 km away).
"""
import math
from dataclasses import dataclass

import numpy as np

TILE_MIN = 2048.0           # the sea's tiles: 6 inches off the curve at the corners of the smallest
PLAN_SQUARE = 512.0         # the server plans again when the player enters another square of this size
TILE_MAX = 8192.0
SPLIT_K = 0.75              # a square closer to the player than SPLIT_K * its size is split (<= 34 tiles)
RADIUS_M = 8192.0           # tiles within this of the player; beyond, the far tiles' photo
SAMPLES = 64                # sea_at is asked on a SAMPLES x SAMPLES grid per tile (32-128 m apart)
# Land below the sea (the Netherlands' polders, GitHub #8 / #53: a quarter of the country lies under sea level, and a
# tile's WaterBlock floods all of its square): a tile more than SEA_FLOOD_SHARE of which is land more than SEA_FLOOD_M
# under the sea is split, on down to SEA_TILE_FLOOR, where it is kept only when it is mostly sea
SEA_FLOOD_M = 0.3
SEA_FLOOD_FAR_M = 2.0           # ... for the tiles placed by the DEM band (> POLY_TILE_MAX, far): the polders are 2-6 m
                                # under the sea, a coarse DEM's marshes and shores a metre (Manhattan's 28 tiles: 62)
POLY_TILE_MAX = 2048.0          # (seacarve.POLY_TILE_MAX)
SEA_TILE_FLOOR = 64.0
SEA_FLOOR_K = 0.0625            # ... farther away, down to this share of the distance (a 16 km straight shore split to
                                # 128 m all along was 146 tiles; the far shore's strip of sea goes without)
SEA_FLOOD_SHARE = 0.05
# ... and near the player, any patch of it: a sunken road or a tunnel's ramp (Rotterdam's 's-Gravendijkwal, 30 m wide and
# 1-4 m under the sea; the Maastunnel's ramps) is a thousandth of a 2048 m tile and fell between its 32 m samples, so the
# tile's WaterBlock filled it (2026-10-21). A tile within SEA_NEAR_M is checked every SEA_NEAR_STEP_M as well and split
# when more than SEA_NEAR_LOW_M2 of it is land under the sea.
SEA_NEAR_M = 1024.0
SEA_NEAR_STEP_M = 8.0
SEA_NEAR_LOW_M2 = 300.0
SEA_SPLIT_SAMPLES = (16, 64)    # samples per edge of a tile checked for low land: size / 32, within these
SEA_MAX_TILES = 200             # nearest first
SEA_E = 0.0                     # m: the sea surface (seacarve.SEA_E)
EARTH_R = 6371000.0


@dataclass(frozen=True)
class Tile:
    key: str                # "<size>_<i>_<j>": the square's lattice index at its size
    x: float                # centre in the anchor frame (m)
    y: float
    z: float                # the sea level there
    size: float
    nx: float               # the local vertical's tilt (its x and y; z = sqrt(1 - nx^2 - ny^2))
    ny: float

    def to_msg(self):
        return [self.key, round(self.x, 2), round(self.y, 2), round(self.z, 3), self.size,
                round(self.nx, 6), round(self.ny, 6)]


def _dist(x, y, x0, y0, s):
    """Distance from (x, y) to the square [x0, x0 + s] x [y0, y0 + s]."""
    dx = max(x0 - x, 0.0, x - (x0 + s))
    dy = max(y0 - y, 0.0, y - (y0 + s))
    return math.hypot(dx, dy)


def square_of(x, y):
    """The PLAN_SQUARE the player is in: the plans are the same anywhere inside it."""
    return math.floor(x / PLAN_SQUARE), math.floor(y / PLAN_SQUARE)


def squares(x, y, radius=None):
    """The quadtree leaves around (x, y): [(x0, y0, size)], every point within `radius` (default
    RADIUS_M) of the centre of (x, y)'s TILE_MIN square in exactly one of them."""
    r = RADIUS_M if radius is None else radius
    # planned from the centre of the player's TILE_MIN square: the set only changes when the player
    # crosses into another square (no tiles made and deleted every few metres)
    x = (math.floor(x / TILE_MIN) + 0.5) * TILE_MIN
    y = (math.floor(y / TILE_MIN) + 0.5) * TILE_MIN
    out = []
    i0, i1 = math.floor((x - r) / TILE_MAX), math.floor((x + r) / TILE_MAX)
    j0, j1 = math.floor((y - r) / TILE_MAX), math.floor((y + r) / TILE_MAX)

    def visit(x0, y0, s):
        d = _dist(x, y, x0, y0, s)
        if d > r:
            return
        if s > TILE_MIN and d < SPLIT_K * s:
            h = s / 2
            for a in (0, 1):
                for b in (0, 1):
                    visit(x0 + a * h, y0 + b * h, h)
        else:
            out.append((x0, y0, s))
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            visit(i * TILE_MAX, j * TILE_MAX, TILE_MAX)
    return out


def _grid(frame, corners, s, n):
    """The (lat, lon) of an n x n grid of sample points in each square (x0, y0) of size s, square by square."""
    f = (np.arange(n) + 0.5) / n
    c = np.asarray(corners, np.float64)
    E = np.repeat((c[:, 0:1] + f[None, :] * s)[:, None, :], n, axis=1)     # [k, j, i]
    N = np.repeat((c[:, 1:2] + f[None, :] * s)[:, :, None], n, axis=2)
    lat, lon, _ = frame.enu_to_geodetic(E.ravel(), N.ravel(), 0.0)
    return lat, lon


def plan(frame, x, y, sea_at, radius=None, ground_at=None):
    """The sea tiles around (x, y) in `frame` (a geodesy.EnuFrame). sea_at(lat, lon, size) -> bool array:
    which sample points of a tile of that size are sea. ground_at(lat, lon, spacing) -> the ground's heights there,
    at about the samples' spacing in m (None: not checked): a tile with land under the sea in it is split
    (SEA_FLOOD_M; near the player SEA_NEAR_LOW_M2)."""

    def near_low(x0, y0, s, flood):
        """Is more than SEA_NEAR_LOW_M2 of the tile land more than `flood` under the sea (SEA_NEAR_STEP_M samples)?"""
        n = int(s / SEA_NEAR_STEP_M)
        lat, lon = _grid(frame, [(x0, y0)], s, n)
        g = np.asarray(ground_at(lat, lon, SEA_NEAR_STEP_M), np.float64)
        k = np.flatnonzero(g < SEA_E - flood)
        if len(k) * SEA_NEAR_STEP_M ** 2 <= SEA_NEAR_LOW_M2:
            return False
        dry = ~np.asarray(sea_at(lat[k], lon[k], s), bool)
        return int(dry.sum()) * SEA_NEAR_STEP_M ** 2 > SEA_NEAR_LOW_M2

    leaves = squares(x, y, radius)
    if not leaves:
        return []
    keep = []
    todo = list(leaves)
    while todo:
        by_size = {}
        for x0, y0, s in todo:
            by_size.setdefault(s, []).append((x0, y0))
        todo = []
        for s, corners in sorted(by_size.items()):
            lat, lon = _grid(frame, corners, s, SAMPLES)
            sea = np.asarray(sea_at(lat, lon, s), bool).reshape(len(corners), -1)
            has = sea.any(axis=1)
            corners = [c for c, h in zip(corners, has) if h]
            if not corners:
                continue
            if ground_at is None:
                keep += [(x0, y0, s) for x0, y0 in corners]
                continue
            n = int(min(max(s / 32.0, SEA_SPLIT_SAMPLES[0]), SEA_SPLIT_SAMPLES[1]))
            lat, lon = _grid(frame, corners, s, n)
            wet = np.asarray(sea_at(lat, lon, s), bool).reshape(len(corners), -1)
            g = np.asarray(ground_at(lat, lon, s / n), np.float64).reshape(len(corners), -1)
            flood = SEA_FLOOD_M if s <= POLY_TILE_MAX else SEA_FLOOD_FAR_M
            low = ~wet & (g < SEA_E - flood)        # land under the sea
            for (x0, y0), w, lo in zip(corners, wet, low):
                # (a share, not any: the DEM's water under the piers the map has as land, Manhattan's waterfront,
                # split every tile on to 200; near the player only)
                if lo.mean() <= SEA_FLOOD_SHARE and not (_dist(x, y, x0, y0, s) < SEA_NEAR_M
                                                         and near_low(x0, y0, s, flood)):
                    keep.append((x0, y0, s))
                elif s / 2 >= max(SEA_TILE_FLOOR, SEA_FLOOR_K * _dist(x, y, x0, y0, s)):
                    h = s / 2
                    todo += [(x0 + a * h, y0 + b * h, h) for a in (0, 1) for b in (0, 1)]
                elif w.mean() >= 0.5:
                    keep.append((x0, y0, s))
    keep.sort(key=lambda t: _dist(x, y, t[0], t[1], t[2]))
    out = []
    for x0, y0, s in keep[:SEA_MAX_TILES]:
        cx, cy = x0 + s / 2, y0 + s / 2
        la, lo, _ = frame.enu_to_geodetic(cx, cy, 0.0)
        z = float(np.asarray(frame.geodetic_to_enu(la, lo, 0.0)[2]))
        # the local vertical at (cx, cy): the sphere's normal, the frame's up tilted by the offset
        nx, ny = cx / EARTH_R, cy / EARTH_R
        out.append(Tile(f"{int(s)}_{int(x0 // s)}_{int(y0 // s)}", cx, cy, z, s, nx, ny))
    return out


# --- lakes and rivers ----------------------------------------------------------------------------------------
LAKE_RADIUS_M = 2000.0      # lake tiles within this of the player (the far tiles carved: L <= 2)
LAKE_TILE_MAX = 512.0
LAKE_TILE_MIN = 64.0
LAKE_TILE_FLOOR = 16.0      # ... split on to this where land under the water's plane would flood (LAKE_FLOOD_SHARE)
LAKE_FLOOD_SHARE = 0.05     # a tile's share of samples that may be land under the water's plane
LAKE_SAMPLES = 24           # samples per tile edge when checking a lake tile
LAKE_FLOOD_M = 0.3          # land inside a lake tile lower than the lake by more than this splits it
                            # (a valley below a dam; the banks themselves are ~ at the lake's level). At 1 m the
                            # tiles of a flat Dutch town's canals flooded its streets 3x the canals' own area
                            # (Petten: 17 ha of land under 6 ha of water tiles, 1.5 ha at 0.3 m; GitHub #8 / #53)
LAKE_MAX_TILES = 120        # nearest first (lakes and wide rivers)
LAKE_DEM_ZOOM = 15
LEVEL_TOL_M = 0.4            # a tile's water is at most this far from its plane (else it is split)


def _plane(u, v, z):
    """Least-squares plane z = a + b u + c v through the points (a level plane when they are too few or in
    a line)."""
    if len(z) < 3:
        return float(np.median(z)), 0.0, 0.0
    A = np.stack([np.ones_like(u), u, v], -1)
    sol, _, rank, _ = np.linalg.lstsq(A, z, rcond=None)
    if rank < 3:
        return float(np.median(z)), 0.0, 0.0
    return float(sol[0]), float(sol[1]), float(sol[2])


def plan_lakes(frame, x, y, lake_at, ground_at, radius=None):
    """Water tiles for the lakes and rivers around (x, y): lake_at(lat, lon) -> the inland water's level at
    the points (NaN away from it, seacarve.SeaCarver.lake_at), ground_at(lat, lon) -> the DEM there. A square of the
    LAKE_TILE_MAX lattice holding lake water becomes one tile at that level when every other point in it
    is land no lower than the water's plane by more than LAKE_FLOOD_M and the water is within LEVEL_TOL_M
    of that plane (a least-squares fit: a river falls across a tile, which is tilted to match), else it
    is split, down to LAKE_TILE_MIN (kept there when at least half of
    it is water; at the median level). So a reservoir's tile never floods the valley below its dam. Tilted to
    the local vertical like the sea's tiles."""
    r = LAKE_RADIUS_M if radius is None else radius
    x = (math.floor(x / PLAN_SQUARE) + 0.5) * PLAN_SQUARE
    y = (math.floor(y / PLAN_SQUARE) + 0.5) * PLAN_SQUARE
    s0 = LAKE_TILE_MAX
    i0, i1 = math.floor((x - r) / s0), math.floor((x + r) / s0)
    j0, j1 = math.floor((y - r) / s0), math.floor((y + r) / s0)
    roots = [(i * s0, j * s0) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)
             if _dist(x, y, i * s0, j * s0, s0) <= r]
    if not roots:
        return []
    f = (np.arange(LAKE_SAMPLES) + 0.5) / LAKE_SAMPLES
    out = []

    def samples(sq, s):
        c = np.asarray(sq, np.float64)
        E = np.repeat((c[:, 0:1] + f[None, :] * s)[:, None, :], LAKE_SAMPLES, axis=1)
        N = np.repeat((c[:, 1:2] + f[None, :] * s)[:, :, None], LAKE_SAMPLES, axis=2)
        lat, lon, _ = frame.enu_to_geodetic(E.ravel(), N.ravel(), 0.0)
        return lat, lon

    todo, s = roots, s0
    while todo and s >= LAKE_TILE_FLOOR:
        lat, lon = samples(todo, s)
        lv = np.asarray(lake_at(lat, lon), np.float64).reshape(len(todo), -1)
        has = np.isfinite(lv).any(axis=1)
        todo = [sq for sq, h in zip(todo, has) if h]
        lv = lv[has]
        if not todo:
            break
        lat, lon = samples(todo, s)
        g = np.asarray(ground_at(lat, lon), np.float64).reshape(len(todo), -1)
        nxt = []
        u = (f - 0.5) * s                               # sample offsets from the tile's centre
        U = np.tile(u, LAKE_SAMPLES)                    # [j, i] raveled: i fastest
        V = np.repeat(u, LAKE_SAMPLES)
        for (x0, y0), l, gr in zip(todo, lv, g):
            wet = np.isfinite(l)
            # the water as a plane over the tile (a river falls across it): level at the centre, slope
            a, bx, by = _plane(U[wet], V[wet], l[wet])
            fit = np.abs(l[wet] - (a + bx * U[wet] + by * V[wet])).max() <= LEVEL_TOL_M
            plane = a + bx * U + by * V
            low = gr[~wet] < plane[~wet] - LAKE_FLOOD_M  # land under the water's plane: it would flood
            clear = not low.any()
            # a tile at least half water may hold a little low land (a shore's noise), but not a trench or a
            # valley: those split on down to LAKE_TILE_FLOOR (a reservoir's 64 m tile flooded Central Park's
            # sunken 86th Street Transverse beside it, the user's report 2026-10-17)
            flooded = low.sum() / low.size if low.size else 0.0
            if (fit and clear) or (s <= LAKE_TILE_MIN and wet.mean() >= 0.5 and fit
                                   and (flooded <= LAKE_FLOOD_SHARE or s <= LAKE_TILE_FLOOR)):
                out.append((x0, y0, s, a, bx, by))
            elif s > LAKE_TILE_FLOOR:
                h = s / 2
                nxt += [(x0 + a * h, y0 + b * h) for a in (0, 1) for b in (0, 1)]
        todo, s = nxt, s / 2
    out.sort(key=lambda t: _dist(x, y, t[0], t[1], t[2]))
    tiles = []
    for x0, y0, s, level, bx, by in out[:LAKE_MAX_TILES]:
        cx, cy = x0 + s / 2, y0 + s / 2
        la, lo, _ = frame.enu_to_geodetic(cx, cy, 0.0)
        z = float(np.asarray(frame.geodetic_to_enu(la, lo, level)[2]))
        # the tilt: the local vertical, then the water's own slope (a surface rising by b per metre in x
        # leans its normal by -b in x)
        tiles.append(Tile(f"L{int(s)}_{int(x0 // s)}_{int(y0 // s)}_{int(round(level * 10))}", cx, cy, z, s,
                          cx / EARTH_R - bx, cy / EARTH_R - by))
    return tiles
