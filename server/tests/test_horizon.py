"""Horizon mode (docs/V1_4_SPEC.md §3.6 "Radius: the real line-of-sight horizon", milestone M1h;
earthd --horizon, off by default): the horizon distances with refraction, the per-anchor zmax grid
(a coarse DEM pass, never below the DEM's pixels), RADIUS = clamp(sqrt(2R'h_eye) + sqrt(2R'zmax_region),
80 km, 1500 km), the disc selection with L10 roots and the per-tile horizon cull (the selection tiles
the disc minus the culled nodes exactly once), the scratchpad viewshed numbers (Whitney summit, eye
4417 m: San Gorgonio, 306 km away, stays; sea-level ground past the horizon goes), the number of
extra tiles, the near field (< 80 km) and its demand unchanged, and the server's eye, textures,
zooms and flags.

The synthetic terrain is sea level with cones at Mount Whitney (the anchor, 4415 m) and San
Gorgonio (3342 m, the scratchpad `viewshed.py` z7 value), or the real DEM's numbers checked
offline (2026-10-10, z6, Whitney summit: radius 503 km, zmax 4217 m, 83 tiles beyond 80 km; Central
Valley floor 289 km; open ocean 80 km; recorded in docs/V1_4_SPEC.md §3.6 implementation notes).
"""
import math
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dem as demmod  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy as g  # noqa: E402
import test_demand  # noqa: E402

WHITNEY = (36.5785, -118.2923)          # the anchor (the mod's default start)
SAN_GORGONIO = (34.0991, -116.8250)     # 3506 m summit; 3342 m in the z7 DEM the viewshed used
EYE_WHITNEY = 4417.0                    # summit + 1.7 m (spec §3.6)
FRAME = g.EnuFrame(*WHITNEY)
SG_XY = tuple(float(v) for v in FRAME.geodetic_to_enu(*SAN_GORGONIO, 0.0)[:2])
LAY64 = ff.Layout(64)


class SampledConeDem:
    """Sea level with cones ((x, y) in FRAME, height, base radius), sample() only (horizon_grid
    then samples it twice per block and axis, as for the other tests' synthetic DEMs)."""

    def __init__(self, cones):
        self.cones = cones

    def height(self, lat, lon):
        e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat, np.float64), np.asarray(lon, np.float64), 0.0)
        z = np.zeros(np.broadcast(e, n).shape)
        for (cx, cy), h, r in self.cones:
            z = np.maximum(z, h * np.clip(1.0 - np.hypot(e - cx, n - cy) / r, 0.0, 1.0))
        return z

    def sample(self, lat, lon, zoom, progress=None):
        return self.height(lat, lon)

    def pixel_height(self, zoom, fx, fy):
        w = 2 ** zoom * demmod.TILE
        lon = fx / w * 360.0 - 180.0
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * fy / w))))
        return self.height(lat, lon)


class ConeDem(SampledConeDem):
    """The same with block_max, as dem.Dem has it: the cones at every DEM pixel centre of the tile, so
    the grid sees what a real DEM's pixels would show."""

    def block_max(self, z, x, y, block):
        f = np.arange(demmod.TILE) + 0.5
        fx, fy = np.meshgrid(x * demmod.TILE + f, y * demmod.TILE + f)
        a = self.pixel_height(z, fx, fy)
        n = demmod.TILE // block
        return a.reshape(n, block, n, block).max(axis=(1, 3))


WHITNEY_CONE = ((0.0, 0.0), 4415.0, 20000.0)
SG_CONE = (SG_XY, 3342.0, 15000.0)
_GRIDS = {}


def grid_for(eye, cones=(WHITNEY_CONE, SG_CONE)):
    """The anchor's zmax grid for an eye (built once per test run and terrain; never mutated)."""
    key = (ff.horizon_extent(eye, earthd.ANCHOR_CENTRE_MAX), cones)
    if key not in _GRIDS:
        _GRIDS[key] = ff.horizon_grid(FRAME, ConeDem(cones), key[0])
    return _GRIDS[key]


def horizon_select(px, py, eye, grid, layout=LAY64, prev=frozenset(), hold=frozenset(), cull=True):
    radius, _ = grid.radius(px, py, eye)
    tiles, refined = ff.select(px, py, (), prev, layout.k, ff.HYSTERESIS, radius, hold=hold,
                               min_level=layout.min_level, radii=layout.near_radii, layout=layout,
                               max_level=ff.HORIZON_MAX_LEVEL, disc=True,
                               horizon=ff.Horizon(eye, grid) if cull else None)
    return tiles, refined, radius


def today_select(px, py, layout=LAY64, prev=frozenset(), hold=frozenset()):
    return ff.select(px, py, (), prev, layout.k, ff.HYSTERESIS, ff.RADIUS, hold=hold,
                     min_level=layout.min_level, radii=layout.near_radii, layout=layout)


def dist(node, px, py):
    s = ff.level_size(node[0])
    x0, y0 = node[1] * s, node[2] * s
    return math.hypot(max(x0 - px, 0.0, px - x0 - s), max(y0 - py, 0.0, py - y0 - s))


def check_partition(tc, tiles, refined, px, py, radius, horizon):
    """The tiles are an exact quadtree partition of the disc minus the culled nodes: no tile is an
    ancestor of another, every child of a refined node is a tile, refined, outside the disc or
    culled, every root in the disc is a tile or refined, and random points of the disc lie in
    exactly one tile unless a culled node holds them. (tc: the TestCase; also used by test_server.)"""
    nodes = {t.node for t in tiles.values()}

    def inside(n):
        d = dist(n, px, py)
        return d < radius and (horizon is None or horizon.keeps(*n, d))
    internal = set()
    for lvl, i, j in nodes:
        while lvl < ff.HORIZON_MAX_LEVEL:
            lvl, i, j = lvl + 1, i >> 1, j >> 1
            internal.add((lvl, i, j))
    tc.assertFalse(nodes & internal, "a tile overlaps another")
    tc.assertEqual(internal, set(refined) & internal)
    for lvl, i, j in internal:
        for c in ((lvl - 1, 2 * i, 2 * j), (lvl - 1, 2 * i + 1, 2 * j), (lvl - 1, 2 * i, 2 * j + 1),
                  (lvl - 1, 2 * i + 1, 2 * j + 1)):
            tc.assertTrue(c in nodes or c in internal or not inside(c), c)
    s = ff.level_size(ff.HORIZON_MAX_LEVEL)
    for i in range(math.floor((px - radius) / s), math.ceil((px + radius) / s)):
        for j in range(math.floor((py - radius) / s), math.ceil((py + radius) / s)):
            n = (ff.HORIZON_MAX_LEVEL, i, j)
            tc.assertTrue(n in nodes or n in internal or not inside(n), n)
    for n in nodes:
        tc.assertTrue(inside(n), n)
    rng = random.Random(3)
    for _ in range(3000):
        a, r = rng.uniform(0, 2 * math.pi), radius * math.sqrt(rng.random()) * 0.999
        x, y = px + r * math.cos(a), py + r * math.sin(a)
        hits = [t for t in tiles.values() if t.bbox[0] <= x < t.bbox[2] and t.bbox[1] <= y < t.bbox[3]]
        if len(hits) == 1:
            continue
        tc.assertEqual(hits, [], (x, y))
        lvl = ff.HORIZON_MAX_LEVEL                   # uncovered: some node on its path is culled
        while True:
            s = ff.level_size(lvl)
            n = (lvl, math.floor(x / s), math.floor(y / s))
            if not inside(n):
                break
            tc.assertIn(n, internal, (x, y))
            lvl -= 1


class HorizonMathTest(unittest.TestCase):
    def test_distances(self):
        self.assertAlmostEqual(ff.R_REFRACTED, 6371000.0 / 0.87, delta=1e-6)
        self.assertAlmostEqual(ff.R_REFRACTED / 1000, 7323, delta=1)
        self.assertAlmostEqual(ff.horizon_distance(EYE_WHITNEY) / 1000, 254.3, delta=0.1)
        self.assertAlmostEqual(ff.horizon_distance(10000.0) / 1000, 382.7, delta=0.1)      # 10 km: 357 km without refraction
        self.assertAlmostEqual(ff.horizon_distance(10000.0, ff.R_EARTH) / 1000, 357.0, delta=0.1)
        self.assertEqual((ff.horizon_distance(0.0), ff.horizon_distance(-86.0)), (0.0, 0.0))  # Death Valley
        # spec §3.6 examples: Whitney summit ~480-510 km bound; the valley floor with the Sierra ~290 km;
        # the cap 1500 km covers eyes up to ~40 km over the highest terrain
        self.assertAlmostEqual((ff.horizon_distance(EYE_WHITNEY) + ff.horizon_distance(4421)) / 1000, 509, delta=1)
        self.assertAlmostEqual((ff.horizon_distance(110) + ff.horizon_distance(4421)) / 1000, 295, delta=1)
        self.assertLess(ff.horizon_distance(40000) + ff.horizon_distance(4000), ff.HORIZON_MAX)
        self.assertEqual((ff.HORIZON_MIN, ff.HORIZON_MAX, ff.HORIZON_MAX_LEVEL), (80000.0, 2600000.0, 10))
        # (an eye at 400 km, the ISS's height, sees 2290 km: inside the cap; the zmax grid stays 1500 km)
        self.assertLess(ff.horizon_distance(400000), ff.HORIZON_MAX)
        self.assertEqual(ff.HORIZON_GRID_MAX, 1500000.0)

    def test_extent(self):
        s10 = ff.level_size(10)
        for eye in (0.0, 2.0, EYE_WHITNEY, 10000.0, 40000.0, 1e6):
            e = ff.horizon_extent(eye, earthd.ANCHOR_CENTRE_MAX)
            self.assertEqual(e % s10, 0)
            self.assertGreaterEqual(e, min(ff.HORIZON_GRID_MAX, ff.horizon_distance(eye) + ff.horizon_distance(ff.Z_TOP))
                                    + earthd.ANCHOR_CENTRE_MAX)
            self.assertLess(e, ff.HORIZON_GRID_MAX + earthd.ANCHOR_CENTRE_MAX + s10)
        self.assertEqual(ff.horizon_extent(EYE_WHITNEY, earthd.ANCHOR_CENTRE_MAX), 3 * s10)


class HorizonGridTest(unittest.TestCase):
    def test_pyramid_lookup(self):
        n = 32                                          # one L10 node per side: half = 262 km / 2 ... 2 nodes
        half = n * ff.level_size(ff.HORIZON_GRID_LEVEL) / 2
        base = np.zeros((n, n))
        base[3, 20] = 1234.0                            # L6 node (4, -13)
        base[30, 1] = 500.0                             # L6 node (-15, 14)
        grid = ff.HorizonGrid(half, base)
        self.assertEqual(grid.zmax(6, 4, -13), 1234.0)
        self.assertEqual(grid.zmax(7, 2, -7), 1234.0)
        self.assertEqual(grid.zmax(9, 0, -2), 1234.0)
        self.assertEqual(grid.zmax(10, 0, -1), 1234.0)
        self.assertEqual(grid.zmax(10, -1, 0), 500.0)
        self.assertEqual(grid.zmax(10, 0, 0), 0.0)
        self.assertEqual(grid.zmax(-1, 4 * 128 + 5, -13 * 128 + 127), 1234.0)       # a finer node: its L6 ancestor
        self.assertEqual(grid.zmax(0, 4 * 64, -13 * 64), 1234.0)
        self.assertEqual(grid.zmax(5, 9, -26), 1234.0)
        self.assertEqual(grid.zmax(5, -30, 29), 500.0)
        self.assertEqual(grid.zmax(5, -31, 28), 0.0)                                # its neighbour's parent
        self.assertEqual(grid.zmax(10, 1, 0), math.inf)                             # outside: never culled
        self.assertEqual(grid.zmax(6, -17, 0), math.inf)
        with self.assertRaises(ValueError):
            ff.HorizonGrid(half, np.zeros((24, 24)))

    def test_radius(self):
        n = 96                                          # 3 L10 nodes per side: 786 km half-size
        half = n * ff.level_size(6) / 2
        s = ff.level_size(6)
        base = np.zeros((n, n))
        base[n // 2, n // 2] = 4400.0                   # the eye's own cell (0, 0)
        grid = ff.HorizonGrid(half, base)
        r, zr = grid.radius(100.0, 100.0, 4417.0)
        self.assertEqual(zr, 4400.0)                    # the eye's own cell is always in sight
        self.assertAlmostEqual(r, ff.horizon_distance(4417.0) + ff.horizon_distance(4400.0))
        # a 3000 m cell 300 km away is in sight (254 + 210 km); a 6000 m one 650 km away is not (254 + 296)
        base[n // 2, n // 2] = 0.0
        base[n // 2, n // 2 + int(300000 // s)] = 3000.0
        base[n // 2, n // 2 + int(650000 // s)] = 6000.0
        grid = ff.HorizonGrid(half, base)
        r, zr = grid.radius(0.0, 0.0, 4417.0)
        self.assertEqual(zr, 3000.0)
        self.assertAlmostEqual(r, ff.horizon_distance(4417.0) + ff.horizon_distance(3000.0))
        self.assertEqual(grid.radius(0.0, 0.0, 10000.0)[1], 6000.0)    # 383 + 296 km: from 10 km it is
        # sea level everywhere: the 80 km floor; very high (600 km): the cap
        flat = ff.HorizonGrid(half, np.zeros((n, n)))
        self.assertEqual(flat.radius(0.0, 0.0, 2.0), (ff.HORIZON_MIN, 0.0))
        self.assertEqual(flat.radius(0.0, 0.0, 600000.0)[0], ff.HORIZON_MAX)

    def test_grid_is_never_below_the_dem_pixels(self):
        """Every L6 cell's zmax is at least every DEM pixel (at the grid's zoom) whose centre lies in
        the cell: the cull can only keep too much."""
        dem = ConeDem((WHITNEY_CONE, SG_CONE, ((-60000.0, 90000.0), 2500.0, 9000.0)))
        half = ff.level_size(10)
        grid = ff.horizon_grid(FRAME, dem, half)
        self.assertEqual(grid.base.shape, (32, 32))
        self.assertEqual(grid.zoom, ff.HORIZON_DEM_ZOOM)
        rng = np.random.default_rng(5)
        xy = rng.uniform(-half, half, (20000, 2))
        xy[:200] = rng.normal(0.0, 3000.0, (200, 2))                    # the summits
        xy[200:400] = np.array(SG_XY) + rng.normal(0.0, 3000.0, (200, 2))
        lat, lon, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        fx, fy = ff._merc(lat, lon, grid.zoom)
        pz = dem.pixel_height(grid.zoom, np.floor(fx) + 0.5, np.floor(fy) + 0.5)   # the pixel the point lies in
        s = ff.level_size(6)
        for (x, y), z in zip(xy, pz):
            self.assertGreaterEqual(grid.zmax(6, int(x // s), int(y // s)), z - 1e-6, (x, y))
        # and not grossly above it: the summit cell is the summit, sea-level cells far off stay 0
        self.assertAlmostEqual(grid.zmax(6, 0, 0), 4415.0, delta=4415.0 * 1000 / 20000)
        self.assertEqual(grid.zmax(6, 15, 15), 0.0)

    def test_sampled_dem_and_cache(self):
        """A DEM without block_max is sampled (the tests' synthetic DEMs); the block cache is
        filled once and reused by the next anchor."""
        cache = {}
        half = ff.level_size(10)
        a = ff.horizon_grid(FRAME, SampledConeDem((WHITNEY_CONE,)), half, cache=cache)
        self.assertTrue(cache and all(k[0] == ff.HORIZON_DEM_ZOOM and k[3] == ff.HORIZON_BLOCK for k in cache))
        self.assertGreater(a.zmax(6, 0, 0), 3500.0)
        n = len(cache)

        class NoDem:
            def sample(self, *a, **kw):
                raise AssertionError("cached blocks must not be sampled again")
        b = ff.horizon_grid(FRAME, NoDem(), half, cache=cache)
        self.assertEqual(len(cache), n)
        self.assertTrue(np.array_equal(a.base, b.base))

    def test_dem_block_max(self):
        """dem.Dem.block_max: the max of each block x block pixel square of a tile (row 0 north)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = demmod.Dem(tmp)
            try:
                tile = np.arange(512 * 512, dtype=np.float32).reshape(512, 512)
                d._lru[(6, 10, 20)] = tile              # in memory: no download
                bm = d.block_max(6, 10, 20, 8)
                self.assertEqual(bm.shape, (64, 64))
                self.assertEqual(bm[0, 0], tile[7, 7])
                self.assertEqual(bm[63, 63], tile[511, 511])
                self.assertEqual(bm[2, 5], tile[23, 47])
            finally:
                d.pool.shutdown()

    def test_zoom_drops_for_a_large_grid(self):
        """A grid that would need more than HORIZON_DEM_TILES tiles at z6 (the cap's 1500 km half-size
        at 65° N, where Mercator tiles are small) is built from a coarser zoom."""
        half = ff.horizon_extent(1e6, earthd.ANCHOR_CENTRE_MAX)
        self.assertEqual(half, 6 * ff.level_size(10))
        north = g.EnuFrame(65.0, 20.0)
        grid = ff.horizon_grid(north, SampledConeDem(()), half)
        self.assertLess(grid.zoom, ff.HORIZON_DEM_ZOOM)
        self.assertEqual(grid.base.shape[0] * ff.level_size(6), 2 * half)
        self.assertEqual(float(grid.base.max()), 0.0)
        near = ff.horizon_grid(FRAME, SampledConeDem(()), ff.level_size(10))
        self.assertEqual(near.zoom, ff.HORIZON_DEM_ZOOM)


class DiscSelectionTest(unittest.TestCase):
    def test_disc_without_cull_covers_exactly_once(self):
        for layout in (LAY64, ff.V131):
            for px, py, radius in ((0.0, 0.0, 300000.0), (12345.0, -6789.0, ff.HORIZON_MIN),
                                   (-262144.0, 131072.0, 700000.0), (5000.0, 5000.0, 1500000.0)):
                with self.subTest(cells=layout.cells, p=(px, py), r=radius):
                    tiles, refined = ff.select(px, py, (), frozenset(), radius=radius, layout=layout,
                                               min_level=layout.min_level, max_level=ff.HORIZON_MAX_LEVEL, disc=True)
                    check_partition(self, tiles, refined, px, py, radius, None)

    def test_disc_with_cull_covers_exactly_once(self):
        for eye, (px, py) in ((EYE_WHITNEY, (0.0, 0.0)), (10000.0, (3000.0, -2000.0)), (2.0, (9000.0, 0.0))):
            grid = grid_for(eye)
            with self.subTest(eye=eye):
                tiles, refined, radius = horizon_select(px, py, eye, grid)
                check_partition(self, tiles, refined, px, py, radius, ff.Horizon(eye, grid))

    def test_square_default_is_unchanged(self):
        """disc / horizon off: the v1.3.1 square (the arguments' defaults change nothing)."""
        a = ff.select(1234.0, -777.0, (), frozenset(), min_level=-2)
        b = ff.select(1234.0, -777.0, (), frozenset(), min_level=-2, disc=False, horizon=None)
        self.assertEqual(a, b)
        corner = [t for t in a[0].values() if t.distance(1234.0, -777.0) > ff.RADIUS]
        self.assertTrue(corner)                         # the square's corners (a disc has none)
        self.assertTrue(all(t.level <= ff.MAX_LEVEL for t in a[0].values()))


class HorizonCullTest(unittest.TestCase):
    def test_whitney_summit_keeps_san_gorgonio(self):
        """Spec §3.6 / the scratchpad viewshed: from Whitney summit (eye 4417 m) San Gorgonio (3342 m,
        306 km) is in sight, so its tile stays; with that mountain gone the same ground (sea level,
        306 km > sqrt(2R' 4417) = 254 km) is behind the bulge and its tile goes."""
        self.assertAlmostEqual(math.hypot(*SG_XY) / 1000, 305.6, delta=0.5)
        grid = grid_for(EYE_WHITNEY)
        tiles, _, radius = horizon_select(0.0, 0.0, EYE_WHITNEY, grid)
        self.assertGreater(radius, 450000.0)
        sg = [t for t in tiles.values() if t.bbox[0] <= SG_XY[0] < t.bbox[2] and t.bbox[1] <= SG_XY[1] < t.bbox[3]]
        self.assertEqual(len(sg), 1)
        self.assertGreaterEqual(sg[0].level, 8)
        flat = grid_for(EYE_WHITNEY, (WHITNEY_CONE,))
        tiles2, _, _ = horizon_select(0.0, 0.0, EYE_WHITNEY, flat)
        self.assertFalse([t for t in tiles2.values()
                          if t.bbox[0] <= SG_XY[0] < t.bbox[2] and t.bbox[1] <= SG_XY[1] < t.bbox[3]])
        # every kept tile past 80 km rises above the eye's horizon; nothing past reach + d(Z_TOP)
        hz = ff.Horizon(EYE_WHITNEY, grid)
        for t in tiles.values():
            d = t.distance(0.0, 0.0)
            if d > ff.HORIZON_MIN:
                self.assertLessEqual(d, hz.reach + ff.horizon_distance(grid.zmax(*t.node)), t.key)
            self.assertLess(d, radius)

    def test_beyond_the_horizon_is_dropped(self):
        """Sea-level ground past sqrt(2R'h_eye): gone, at every eye height; the near field never."""
        for eye in (2.0, 300.0, EYE_WHITNEY, 10000.0):
            grid = grid_for(eye, ())                    # the whole world at sea level
            tiles, _, radius = horizon_select(0.0, 0.0, eye, grid)
            reach = max(ff.horizon_distance(eye), ff.HORIZON_MIN)
            self.assertEqual(radius, max(ff.HORIZON_MIN, ff.horizon_distance(eye)))
            self.assertTrue(all(t.distance(0.0, 0.0) <= reach for t in tiles.values()), eye)
            near = {k for k, t in today_select(0.0, 0.0)[0].items() if t.distance(0.0, 0.0) < ff.HORIZON_MIN}
            self.assertLessEqual(near, set(tiles), eye)

    def test_extra_tile_counts(self):
        """+40-90 coarse tiles beyond 80 km on Whitney (spec §3.6; the real DEM gave 83), more at 10 km,
        none at sea level on open water (only the disc: a few square corners fewer)."""
        today = len(today_select(0.0, 0.0)[0])

        def counts(eye, cones):
            tiles, _, radius = horizon_select(0.0, 0.0, eye, grid_for(eye, cones))
            far = [t for t in tiles.values() if t.distance(0.0, 0.0) > ff.HORIZON_MIN]
            return len(tiles), len(far), {t.level for t in far}, radius
        n, far, levels, radius = counts(EYE_WHITNEY, (WHITNEY_CONE, SG_CONE))
        print(f"\n  Whitney summit: radius {radius / 1000:.0f} km, {n} tiles ({today} today), {far} beyond 80 km, "
              f"levels {sorted(levels)}", file=sys.stderr)
        self.assertTrue(40 <= far <= 90, far)
        self.assertLessEqual(levels, {7, 8, 9, 10})
        self.assertLess(n - far, today)                 # the near field is today's (minus the square's corners)
        n10, far10, _, radius10 = counts(10000.0, (WHITNEY_CONE, SG_CONE))
        print(f"  10 km above Whitney: radius {radius10 / 1000:.0f} km, {n10} tiles, {far10} beyond 80 km", file=sys.stderr)
        self.assertGreater(radius10, radius)
        self.assertGreater(far10, far)
        self.assertLess(far10, 150)
        n0, far0, _, radius0 = counts(2.0, ())
        self.assertEqual((far0, radius0), (0, ff.HORIZON_MIN))
        self.assertLess(n0, today)
        # without the cull the disc would keep far more
        tiles, _, _ = horizon_select(0.0, 0.0, EYE_WHITNEY, grid_for(EYE_WHITNEY), cull=False)
        self.assertGreater(sum(1 for t in tiles.values() if t.distance(0.0, 0.0) > ff.HORIZON_MIN), far + 20)

    def test_near_field_and_demand_unchanged(self):
        """Within 80 km the horizon selection is today's, tile for tile, along a 134 m/s drive with the
        server's hysteresis and HOLD_S (both layouts); the demand (adds/s) differs only by the rare
        coarse tiles at the horizon."""
        grid = grid_for(EYE_WHITNEY)
        path = test_demand.straight(134.0)
        for layout in (LAY64, ff.V131):
            with self.subTest(cells=layout.cells):
                prev_h = prev_t = frozenset()
                changed_h, changed_t = {}, {}
                for k in range(int(30 * test_demand.HZ)):
                    t = k / test_demand.HZ
                    px, py, _, _ = path(t)
                    hold_h = {n for n, tc in changed_h.items() if t - tc < ff.HOLD_S}
                    hold_t = {n for n, tc in changed_t.items() if t - tc < ff.HOLD_S}
                    th, rh, _ = horizon_select(px, py, EYE_WHITNEY, grid, layout, prev_h, hold_h)
                    tt, rt = today_select(px, py, layout, prev_t, hold_t)
                    if k:
                        for n in rh ^ prev_h:
                            changed_h[n] = t
                        for n in rt ^ prev_t:
                            changed_t[n] = t
                    prev_h, prev_t = frozenset(rh), frozenset(rt)
                    near_h = {key for key, x in th.items() if x.distance(px, py) < ff.HORIZON_MIN}
                    near_t = {key for key, x in tt.items() if x.distance(px, py) < ff.HORIZON_MIN}
                    self.assertEqual(near_h, near_t, (layout.cells, t))

        def sel(px, py, vx, vy, prev, hold):
            tiles, refined, _ = horizon_select(px, py, EYE_WHITNEY, grid, LAY64, prev, hold)
            return tiles, refined
        _, rate_h, _ = test_demand.demand(sel, path)
        _, rate_t, _ = test_demand.demand(test_demand.select_m1, path)
        print(f"\n  134 m/s: {rate_h:.2f} adds/s with the horizon, {rate_t:.2f} today", file=sys.stderr)
        self.assertLessEqual(abs(rate_h - rate_t), 0.5)

    def test_swap_groups_reach_level_10(self):
        """An L10 tile and its four L9 children form one swap group (the union walks up to L10)."""
        kids = [f"9_{i}_{j}" for i in (2, 3) for j in (-2, -1)]
        groups = ff.swap_groups({"10_1_-1"}, set(kids))
        self.assertEqual(groups, [(sorted(kids), ["10_1_-1"])])


class HorizonServerTest(unittest.TestCase):
    """earthd.Server(horizon=True): mesh mode only, L10 zooms, coarse textures from L8, the quadtree
    root; the default is off and changes nothing."""

    def make(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, **kw)
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(s.tile_pool.shutdown)
        if s.horizon_pool is not None:
            self.addCleanup(s.horizon_pool.shutdown)
        return s

    def test_flag(self):
        import inspect
        self.assertIs(inspect.signature(earthd.Server).parameters["horizon"].default, False)
        s = self.make(cells=64)
        self.assertEqual((s.horizon, s.horizon_pool, s.max_level), (False, None, ff.MAX_LEVEL))
        self.assertEqual([s.tex_size(lvl) for lvl in range(7, 11)], [512] * 4)
        h = self.make(cells=64, horizon=True)
        self.assertEqual((h.horizon, h.max_level), (True, ff.HORIZON_MAX_LEVEL))
        self.assertEqual([h.tex_size(lvl) for lvl in range(-1, 11)], [256, 256, 256] + [512] * 6 + [256, 256, 128])
        a = h.new_anchor(*WHITNEY)
        self.assertEqual([a.zooms[lvl] for lvl in (8, 9, 10)], [7, 6, 5])
        self.assertEqual(a.zooms[10], ff.zoom_for_level(h.layout, WHITNEY[0], 10))
        with self.assertRaises(ValueError):
            self.make(horizon=True, near_visual="terrain")
        import subprocess
        r = subprocess.run([sys.executable, str(Path(earthd.__file__)), "--horizon", "--near-visual", "terrain"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--horizon needs --near-visual mesh", r.stderr)

    def test_eye_from_the_pose(self):
        """The eye: the camera's z (else the player's + EYE_ABOVE_PLAYER) as a height above sea level in
        the pose's anchor frame (the plane lies d²/2R above the ellipsoid away from the anchor),
        quantised by EYE_STEP / EYE_STEP_REL."""
        s = self.make(cells=64, horizon=True)
        c = earthd.Connection.__new__(earthd.Connection)
        c.s, c.eye = s, None
        c.update_eye(FRAME, 0.0, 0.0, {"z": 4415.0, "cam": {"z": 4417.0}})
        self.assertAlmostEqual(c.eye, 4417.0, delta=1e-6)
        c.update_eye(FRAME, 0.0, 0.0, {"z": 4415.0, "cam": {"z": 4417.0 + 0.04 * 4417}})
        self.assertAlmostEqual(c.eye, 4417.0, delta=1e-6)                     # within 5 %: unchanged
        c.update_eye(FRAME, 0.0, 0.0, {"z": 3000.0})                          # no camera: player + 1.5 m
        self.assertAlmostEqual(c.eye, 3000.0 + earthd.EYE_ABOVE_PLAYER, delta=1e-6)
        c.update_eye(FRAME, 15000.0, 0.0, {"z": 0.0, "cam": {"z": 0.0}})      # 15 km out on the plane: 17.7 m up
        self.assertAlmostEqual(c.eye, 15000.0 ** 2 / (2 * 6371000.0), delta=0.2)
        c.update_eye(FRAME, 0.0, 0.0, {"z": 5.0, "cam": {"z": 9.0}})
        self.assertAlmostEqual(c.eye, 17.7, delta=0.2)                        # within EYE_STEP (10 m): unchanged
        c.eye = None
        c.update_eye(FRAME, 0.0, 0.0, {"z": 5.0, "cam": {"z": float("nan")}})  # a bad camera z: the player's
        self.assertAlmostEqual(c.eye, 5.0 + earthd.EYE_ABOVE_PLAYER, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
