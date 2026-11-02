import math
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy as g  # noqa: E402
import heightmap  # noqa: E402

LAT0, LON0 = 36.5785, -118.2923
MOD = Path(__file__).resolve().parents[2] / "mod" / "beamng_earth"


class FakeDem:
    """Smooth synthetic terrain; the zoom shifts it so seams can be checked."""

    def sample(self, lat, lon, zoom, progress=None):
        return 1500 + 300 * np.sin(lat * 90) * np.cos(lon * 70) + zoom * 7.0


class RoughDem:
    """Rough synthetic terrain with detail below the L0 vertex spacing; zoom independent."""

    def sample(self, lat, lon, zoom, progress=None):
        lat, lon = np.asarray(lat), np.asarray(lon)
        return (1500 + 300 * np.sin(lat * 90) * np.cos(lon * 70)
                + 4.0 * np.sin(lat * 61000) * np.cos(lon * 47000) + 2.0 * np.sin(lon * 151000))


class VValleyDem:
    """A sharp V-shaped valley (x = 100 m east of the anchor): its floor falls between L0
    vertices (and between the far tiles' 2 m lattice samples), so the 8 m tile bridges it above the 1 m terrain."""

    def __init__(self, frame):
        self.frame = frame

    def sample(self, lat, lon, zoom, progress=None):
        e, n, _ = self.frame.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.0)
        return 800 + 0.8 * np.abs(e - 101.0)


def coverage(tiles, px, py, radius):
    """Count how often each 256 m cell is covered over the radius square."""
    bbs = [t.bbox for t in tiles.values()]
    x0 = min(b[0] for b in bbs + [(px - radius, py - radius, 0, 0)])
    y0 = min(b[1] for b in bbs + [(px - radius, py - radius, 0, 0)])
    x1 = max(b[2] for b in bbs + [(0, 0, px + radius, py + radius)])
    y1 = max(b[3] for b in bbs + [(0, 0, px + radius, py + radius)])
    gx0, gy0 = math.floor(x0 / 256), math.floor(y0 / 256)
    cnt = np.zeros((math.ceil(y1 / 256) - gy0, math.ceil(x1 / 256) - gx0), np.int16)
    for b in bbs:
        cnt[int(b[1] // 256) - gy0:int(b[3] // 256) - gy0, int(b[0] // 256) - gx0:int(b[2] // 256) - gx0] += 1
    return cnt, gx0, gy0


def footprint(keys):
    """Set of 256 m cells covered by tiles."""
    out = set()
    for k in keys:
        t = ff.tile_of(k)
        n = 1 << t.level
        out |= {(t.i * n + a, t.j * n + b) for a in range(n) for b in range(n)}
    return out


class SelectionTest(unittest.TestCase):
    CASES = [  # (px, py, block centre or None, radius)
        (0.0, 0.0, (0.0, 0.0), ff.RADIUS),
        (300.0, -420.0, (256.0, -256.0), ff.RADIUS),
        (9876.5, -4321.0, (9984.0, -4352.0), ff.RADIUS),
        (-123.0, 77.0, None, 20000.0),
        (1000.0, 0.0, (768.0, 0.0), 5000.0),
    ]

    def check_tiling(self, px, py, forced, radius, prev=frozenset()):
        tiles, refined = ff.select(px, py, forced, prev, radius=radius)
        cnt, gx0, gy0 = coverage(tiles, px, py, radius)
        self.assertLessEqual(int(cnt.max()), 1, "overlapping tiles")
        # Every 256 m cell of the radius square is covered exactly once (the blocks included:
        # tiles under a block are kept).
        r0, r1 = math.floor((py - radius) / 256) - gy0, math.ceil((py + radius) / 256) - gy0
        c0, c1 = math.floor((px - radius) / 256) - gx0, math.ceil((px + radius) / 256) - gx0
        sq = cnt[r0:r1, c0:c1]
        self.assertTrue((sq == 1).all(), f"{int((sq == 0).sum())} uncovered cells")
        for t in tiles.values():
            x0, y0, x1, y1 = t.bbox
            self.assertTrue(x0 < px + radius and x1 > px - radius and y0 < py + radius and y1 > py - radius)
            self.assertTrue(0 <= t.level <= ff.MAX_LEVEL)
            self.assertEqual(x1 - x0, 256 * 2 ** t.level)
            self.assertEqual(x0 % t.size, 0)                # quadtree-aligned
            self.assertEqual(t.key, f"{t.level}_{t.i}_{t.j}")
            # LOD rule: a leaf above L0 is at least K(L)*size away (unless held by hysteresis).
            if t.level > 0 and not prev:
                self.assertGreaterEqual(t.distance(px, py), ff.k_default(t.level) * t.size)
                self.assertFalse(any(ff.overlaps(t.bbox, b) for b in forced))
            if t.level < ff.MAX_LEVEL:
                parent = ff.Tile(t.level + 1, t.i // 2, t.j // 2)
                self.assertIn(parent.node, refined)
        # Under every forced block: exactly its L0 tiles (4 x 4 for the 1 km block on the 256 m L0 grid).
        for b in forced:
            under = [t for t in tiles.values() if ff.overlaps(t.bbox, b)]
            self.assertEqual(len(under), int((b[2] - b[0]) // 256) * int((b[3] - b[1]) // 256))
            self.assertTrue(all(t.level == 0 for t in under))
        return tiles, refined

    def test_tiling_has_no_gaps_or_overlaps(self):
        for px, py, c, r in self.CASES:
            with self.subTest(px=px, py=py, block=c):
                self.check_tiling(px, py, [ff.Block(*c).bbox] if c else [], r)

    def test_keys_independent_of_block(self):
        """§2.8: keys are unchanged when only the block moves (the block's own area just stays L0)."""
        a, _ = ff.select(600.0, 0.0, [ff.Block(0.0, 0.0).bbox])
        b, _ = ff.select(600.0, 0.0, [ff.Block(512.0, 0.0).bbox])
        self.assertTrue(all("_a" not in k for k in a))
        both = [ff.Block(0.0, 0.0).bbox, ff.Block(512.0, 0.0).bbox]
        u, _ = ff.select(600.0, 0.0, both)
        # Moving the block only changes tiles in the union of both footprints, and only
        # between L0 and coarser levels there.
        adds, removes = ff.diff(a, b)
        for k in adds + removes:
            self.assertTrue(any(ff.overlaps(ff.tile_of(k).bbox, x) for x in both), k)
        self.assertLess(len(adds) + len(removes), 30)
        self.check_tiling(600.0, 0.0, both, ff.RADIUS)
        self.assertTrue(set(a) - set(adds) <= set(u) | set(removes))

    def test_forced_pending_and_predicted(self):
        forced = [ff.Block(0.0, 0.0).bbox, ff.Block(512.0, 0.0).bbox, ff.Block(768.0, 256.0).bbox]
        tiles, _ = self.check_tiling(400.0, 100.0, forced, ff.RADIUS)
        l0 = footprint(k for k, t in tiles.items() if t.level == 0)
        for b in forced:
            cells = {(i, j) for i in range(int(b[0] // 256), int(b[2] // 256))
                     for j in range(int(b[1] // 256), int(b[3] // 256))}
            self.assertTrue(cells <= l0)

    def test_k_per_level_and_counts(self):
        tiles, _ = ff.select(0.0, 0.0, [ff.Block(0.0, 0.0).bbox])
        by = {}
        for t in tiles.values():
            by[t.level] = by.get(t.level, 0) + 1
        # §3 table: about 666 (+64 under the block) for K = 3, 3, 2.5 and h = 0.5.
        self.assertTrue(600 < len(tiles) < 800, len(tiles))
        self.assertEqual(ff.k_default(1), 3.0)
        self.assertEqual(ff.k_default(2), 3.0)
        self.assertEqual(ff.k_default(5), 2.5)

    def test_hysteresis(self):
        """§2.8: an oscillating pose +-40 m around a threshold produces no change after the first."""
        forced = [ff.Block(0.0, 0.0).bbox]
        # The L1 node 1_3_0 (x 1536..2048) refines at d < 3 * 512 = 1536 m, i.e. px > 0.
        _, refined = ff.select(-500.0, 0.0, forced)
        self.assertNotIn((1, 3, 0), refined)
        prev = refined
        sets = []
        for n, px in enumerate([40.0, -40.0] * 6):
            tiles, prev = ff.select(px, 0.0, forced, prev)
            sets.append(set(tiles))
            if n == 0:
                self.assertIn((1, 3, 0), prev)
        self.assertTrue(all(s == sets[0] for s in sets[1:]))
        # Without hysteresis it flips every time.
        flips = [set(ff.select(px, 0.0, forced, h=0.0)[0]) for px in (40.0, -40.0)]
        self.assertNotEqual(flips[0], flips[1])
        # Within the band (d < (1 + h) * K * S = 1920 m) it stays refined, beyond it coarsens.
        tiles, prev = ff.select(-300.0, 0.0, forced, prev)
        self.assertIn((1, 3, 0), prev)
        tiles, prev = ff.select(-450.0, 0.0, forced, prev)
        self.assertNotIn((1, 3, 0), prev)
        self.assertGreaterEqual(ff.HYSTERESIS, 0.25)            # V1_3_SPEC §D: band >= 25 %

    def test_hold(self):
        forced = [ff.Block(0.0, 0.0).bbox]
        _, prev = ff.select(40.0, 0.0, forced)
        self.assertIn((1, 3, 0), prev)
        _, r = ff.select(-500.0, 0.0, forced, prev, hold={(1, 3, 0)})
        self.assertIn((1, 3, 0), r)                         # held refined
        _, r = ff.select(-500.0, 0.0, forced, prev)
        self.assertNotIn((1, 3, 0), r)

    def test_swap_groups(self):
        """§2.8: every group's add footprint equals its remove footprint; one-sided groups
        only at the radius edge; applying all groups gives the new set."""
        forced0 = [ff.Block(0.0, 0.0).bbox]
        a, ra = ff.select(0.0, 0.0, forced0)
        for px, py, c in ((600.0, 0.0, (0.0, 0.0)), (2500.0, 1300.0, (2304.0, 1280.0)), (-9000.0, 0.0, None)):
            with self.subTest(px=px, py=py):
                forced = [ff.Block(*c).bbox] if c else []
                b, _ = ff.select(px, py, forced, ra)
                groups = ff.swap_groups(set(a), b)
                cur = set(a)
                seen = set()
                for adds, removes in groups:
                    self.assertTrue(adds or removes)
                    self.assertFalse(seen & set(adds + removes))
                    seen |= set(adds + removes)
                    if adds and removes:
                        self.assertEqual(footprint(adds), footprint(removes))
                        # parent <-> descendants: one side is a single tile
                        self.assertTrue(len(adds) == 1 or len(removes) == 1 or
                                        len({ff.tile_of(k).level for k in adds + removes}) > 2)
                    else:
                        # entering / leaving the far radius: an add reaches outside the old
                        # radius square, a remove outside the new one
                        R = ff.RADIUS
                        for k, (cx, cy) in [(k, (0.0, 0.0)) for k in adds] + [(k, (px, py)) for k in removes]:
                            bb = ff.tile_of(k).bbox
                            self.assertFalse(bb[0] >= cx - R and bb[2] <= cx + R
                                             and bb[1] >= cy - R and bb[3] <= cy + R, k)
                    self.assertTrue(set(removes) <= cur)
                    cur = (cur - set(removes)) | set(adds)
                self.assertEqual(cur, set(b))

    def test_swap_groups_refine_and_coarsen(self):
        old = {"1_0_0"}
        new = {"0_0_0", "0_1_0", "0_0_1", "0_1_1"}
        self.assertEqual(ff.swap_groups(old, new), [(sorted(new), ["1_0_0"])])
        self.assertEqual(ff.swap_groups(new, old), [(["1_0_0"], sorted(new))])
        # Mixed depth: one child refined further.
        self.assertEqual(ff.swap_groups({"2_0_0"}, {"1_1_0", "1_0_1", "1_1_1", "0_0_0", "0_1_0", "0_0_1", "0_1_1"}),
                         [(["0_0_0", "0_0_1", "0_1_0", "0_1_1", "1_0_1", "1_1_0", "1_1_1"], ["2_0_0"])])
        # Two separate refinements are two groups.
        g = ff.swap_groups({"1_0_0", "1_5_5"}, {"0_0_0", "0_1_0", "0_0_1", "0_1_1", "1_5_5"})
        self.assertEqual(len(g), 1)
        g = ff.swap_groups({"1_0_0", "1_5_5"}, {"0_0_0", "0_1_0", "0_0_1", "0_1_1",
                                                "0_10_10", "0_11_10", "0_10_11", "0_11_11"})
        self.assertEqual(len(g), 2)

    def test_nearest_first(self):
        tiles = ff.nearest_first(ff.select(0.0, 0.0, [ff.Block(0.0, 0.0).bbox])[0].values(), 0.0, 0.0)
        d = [t.distance(0, 0) for t in tiles]
        self.assertEqual(d, sorted(d))
        self.assertEqual(tiles[0].level, 0)

    def test_transform_bbox(self):
        f1 = g.EnuFrame(LAT0, LON0)
        bb = (1000.0, -2000.0, 1256.0, -1744.0)
        np.testing.assert_allclose(ff.transform_bbox(f1, f1, bb), bb, atol=1e-6)
        # 10 km east: the box moves ~10 km west in the new frame.
        lat, lon, _ = f1.enu_to_geodetic(10000.0, 0.0, 0.0)
        f2 = g.EnuFrame(lat, lon)
        out = ff.transform_bbox(f1, f2, bb)
        self.assertAlmostEqual(out[0], 1000.0 - 10000.0, delta=5.0)   # + meridian convergence
        self.assertAlmostEqual(out[3], -1744.0, delta=15.0)  # rotated ~0.07 deg
        e, n, _ = f2.ecef_to_enu(*f1.enu_to_ecef(np.array([1000.0, 1256.0, 1000.0, 1256.0]),
                                                 np.array([-2000.0, -2000.0, -1744.0, -1744.0]), 0.0))
        np.testing.assert_allclose(out, (e.min(), n.min(), e.max(), n.max()), atol=1e-6)


class LayoutTest(unittest.TestCase):
    """docs/V1_4_SPEC.md §3.1 (M1): the layout object; nothing reads a module setting."""

    def test_layouts(self):
        v, m = ff.V131, ff.Layout(64)
        self.assertEqual((v.cells, v.block, v.min_level, v.exact_level, v.format, v.tris), (32, 1024, -2, -2, 3, 2304))
        self.assertEqual((m.cells, m.block, m.min_level, m.exact_level, m.format, m.tris), (64, 1024, -1, -1, 4, 8704))
        self.assertEqual(ff.Layout(32, mesh=False).min_level, 0)
        self.assertEqual(ff.Layout(32, mesh=False).near_radii, {})
        self.assertEqual(v.near_radii, {-2: 150.0, -1: 400.0})
        self.assertEqual(m.near_radii, {-1: 150.0, 0: 400.0})
        # K' = {1: 0.75, 2: 1.5, 3: 1.5, >=4: 1.25}; v1.3.1: K(1) = K(2) = 3, K(>=3) = 2.5
        self.assertEqual([m.k(lvl) for lvl in range(1, 8)], [0.75, 1.5, 1.5, 1.25, 1.25, 1.25, 1.25])
        self.assertEqual([v.k(lvl) for lvl in range(1, 6)], [3.0, 3.0, 2.5, 2.5, 2.5])
        self.assertEqual([ff.k_default(lvl) for lvl in range(1, 6)], [v.k(lvl) for lvl in range(1, 6)])
        # textures: 64 cells 256 px to L1, 512 from L2; v1.3.1 256 everywhere; --tex repeats its last value
        self.assertEqual([m.tex_size(lvl) for lvl in range(-1, 6)], [256, 256, 256, 512, 512, 512, 512])
        self.assertEqual({v.tex_size(lvl) for lvl in range(-2, 10)}, {256})
        self.assertEqual([ff.Layout(64, tex=(128, 256)).tex_size(lvl) for lvl in (-1, 0, 5)], [128, 256, 256])
        self.assertEqual([ff.Layout(32, mesh=False, tex=(512, 512, 256)).tex_size(lvl) for lvl in (0, 1, 2)],
                         [512, 512, 256])
        # The 1 km block (1024² at 1 m) on a 128 m grid; the 2 km one (2048²) on a 256 m grid, mesh mode only
        b = ff.Layout(64, 2048)
        self.assertEqual((b.block, b.grid, b.block_at(256.0, 0.0).bbox),
                         (2048, 256.0, (-768.0, -1024.0, 1280.0, 1024.0)))
        self.assertEqual((m.grid, m.block_at(128.0, 0.0)), (128.0, ff.Block(128.0, 0.0)))
        self.assertEqual(m.block_at(128.0, 0.0).bbox, (-384.0, -512.0, 640.0, 512.0))
        for bad in (dict(cells=48), dict(cells=64, mesh=False), dict(block=512), dict(block=1024, mesh=False),
                    dict(tex=())):
            with self.assertRaises(ValueError, msg=bad):
                ff.Layout(**bad)
        with self.assertRaises(Exception):
            v.cells = 64                                    # frozen
        # the exact level's tile is `cells` far-lattice squares wide (2 m: every second 1 m block sample)
        for lay in (v, m):
            self.assertEqual(ff.level_size(lay.exact_level), lay.cells * heightmap.FAR_SQUARE)

    def test_switch_distances_shift_one_level(self):
        """64 cells: level L splits where the v1.3.1 layout split level L - 1, so the vertex spacing
        at a distance is the same (the K' table and the shifted near radii)."""
        v, m = ff.V131, ff.Layout(64)
        for lvl in range(m.min_level, ff.MAX_LEVEL + 1):
            self.assertEqual(ff.switch_distance(lvl, min_level=m.min_level, layout=m),
                             ff.switch_distance(lvl - 1, min_level=v.min_level, layout=v), lvl)

    def test_no_module_globals(self):
        """No module-level CELLS (or other per-layout scalar) is left to set."""
        for name in ("CELLS", "K_NEAR", "K_FAR"):
            self.assertFalse(hasattr(ff, name), name)
        for table in (ff.K_TABLE, ff.NEAR_RADII, ff.MESH_MIN_LEVEL, ff.CONTENT_FORMAT, ff.TEX_DEFAULT):
            self.assertEqual(set(table), set(ff.CELL_COUNTS))


class TileGeometryTest(unittest.TestCase):
    """Tile shapes; parametrised over the layouts (subclasses set LAYOUT)."""
    LAYOUT = ff.V131

    @classmethod
    def setUpClass(cls):
        cls.frame = g.EnuFrame(LAT0, LON0)
        cls.dem = FakeDem()
        cls.n = cls.LAYOUT.cells

    def setUp(self):                    # the regular grid (AdaptiveTileTest: the adaptive meshes)
        self._eps, ff.ADAPT_EPS = ff.ADAPT_EPS, 0.0

    def tearDown(self):
        ff.ADAPT_EPS = self._eps

    def build(self, tile, dem=None, q=None, zoom=None):
        z = ff.zoom_for_level(self.LAYOUT, LAT0, max(tile.level, 1)) if zoom is None else zoom
        b = ff.build_tile(self.frame, dem or self.dem, tile, z, q, layout=self.LAYOUT)
        return b, np.array(b.pos), b.positions + b.pos

    def surface(self, x, y, level, dem=None):
        z = ff.zoom_for_level(self.LAYOUT, LAT0, max(level, 1))
        return self.frame.surface_enu(np.asarray(x, float), np.asarray(y, float),
                                      lambda la, lo: (dem or self.dem).sample(la, lo, z))[5]

    def test_tile_geometry(self):
        nc, nv = self.n, (self.n + 1) ** 2
        for tile in (ff.Tile(0, 4, 0), ff.Tile(3, -2, 1), ff.Tile(7, 1, -2)):
            b, pos, P = self.build(tile)
            nrm, uv, tris = b.normals, b.uvs, b.tris
            self.assertEqual(len(tris), 2 * nc * nc + 4 * 2 * nc)
            self.assertEqual(len(tris), self.LAYOUT.tris)
            self.assertEqual(tuple(pos[:2]), tile.bbox[:2])
            grid = P[:nv].reshape(nc + 1, nc + 1, 3)
            x0, y0, x1, y1 = tile.bbox
            np.testing.assert_allclose(grid[0, :, 0], np.linspace(x0, x1, nc + 1), atol=1e-6)
            np.testing.assert_allclose(grid[:, 0, 1], np.linspace(y0, y1, nc + 1), atol=1e-6)
            np.testing.assert_allclose(grid[..., 2], self.surface(grid[..., 0], grid[..., 1], tile.level), atol=1e-6)
            np.testing.assert_allclose(b.grid, grid[..., 2], atol=1e-3)
            self.assertEqual(b.grid.shape, (nc + 1, nc + 1))
            # Winding: every grid triangle faces up (CCW seen from +Z).
            main = tris[:2 * nc * nc]
            v0, v1, v2 = P[main[:, 0]], P[main[:, 1]], P[main[:, 2]]
            self.assertTrue((np.cross(v1 - v0, v2 - v0)[:, 2] > 0).all())
            # Skirts face outwards and hang skirt_depth(size, err) below the edge.
            sk = tris[2 * nc * nc:]
            v0, v1, v2 = P[sk[:, 0]], P[sk[:, 1]], P[sk[:, 2]]
            n = np.cross(v1 - v0, v2 - v0)
            c = (v0 + v1 + v2) / 3 - [(x0 + x1) / 2, (y0 + y1) / 2, 0]
            self.assertTrue((np.einsum("ij,ij->i", n[:, :2], c[:, :2]) > 0).all())
            self.assertTrue((np.abs(n[:, 2]) < 1e-6 * np.linalg.norm(n, axis=1)).all())
            self.assertEqual(b.skirt, ff.skirt_depth(tile.size, b.err))
            self.assertGreaterEqual(b.skirt, max(2.0, 0.02 * tile.size, 2.5 * b.err + 1))
            self.assertAlmostEqual(P[:, 2].min(), min(grid[0, :, 2].min(), grid[-1, :, 2].min(),
                                                      grid[:, 0, 2].min(), grid[:, -1, 2].min()) - b.skirt, places=6)
            self.assertTrue((nrm[:, 2] > 0.5).all())
            self.assertTrue(((uv > 0) & (uv < 1)).all())
            self.assertGreaterEqual(b.err, 0.0)
            self.assertEqual(b.sink is not None, tile.level == 0)

    def test_err_measures_detail(self):
        smooth, _, _ = self.build(ff.Tile(2, 3, 1))
        rough, _, _ = self.build(ff.Tile(2, 3, 1), RoughDem())
        self.assertLess(smooth.err, 0.5)
        self.assertGreater(rough.err, 2.0)
        self.assertLess(rough.err, 12.0)

    def test_neighbours_share_edges(self):
        m = self.n + 1
        _, _, a = self.build(ff.Tile(2, 0, 0))
        _, _, b = self.build(ff.Tile(2, 1, 0))
        ea = a[:m * m].reshape(m, m, 3)[:, -1]
        wb = b[:m * m].reshape(m, m, 3)[:, 0]
        np.testing.assert_allclose(ea, wb, atol=1e-6)

    def test_l0_on_terrain_lattice(self):
        """§1.4: L0 vertices are Q-rounded terrain samples, bit-identical to the heightmap's (every
        8th 1 m sample with 32 cells, every 4th with 64)."""
        q = 1 / 64
        zoom = 15
        tile = ff.Tile(0, 1, -2)        # x 256..512, y -512..-256: inside the 2 km block at (0, 0)
        b, pos, _ = self.build(tile, RoughDem(), q=q, zoom=zoom)
        u = heightmap.grid_u(self.frame, lambda la, lo: RoughDem().sample(la, lo, zoom), 0.0, 0.0, 2048)
        hq = heightmap.quantise(u, q) * q
        # Block sample gx = (x + 1024) / 1; the tile's vertices are every (256 / cells)-th sample.
        gx0, gy0 = (256 + 1024) // 1, (-512 + 1024) // 1
        st, m = 256 // self.n, self.n + 1
        expect = hq[gy0:gy0 + 257:st, gx0:gx0 + 257:st]
        self.assertEqual(expect.shape, (m, m))
        self.assertTrue((b.grid.astype(np.float64) == expect.astype(np.float32).astype(np.float64)).all())
        rel = b.positions[:m * m, 2].reshape(m, m) + pos[2]
        np.testing.assert_allclose(rel, expect, atol=1e-9)

    def test_sink_v_valley(self):
        """§2.8: L0 sink >= max(tile - lattice) on a synthetic V-valley, on the far lattice (FAR_SQUARE: every second 1 m
        block sample; build_tile does not measure the odd metres, a trade-off for the near tiles' build time)."""
        dem = VValleyDem(self.frame)
        q = 1 / 64
        tile = ff.Tile(0, 0, 0)             # x 0..256: the valley floor at x = 101 (between vertices 96 and 104)
        b, pos, _ = self.build(tile, dem, q=q, zoom=15)
        lat = np.arange(129) * heightmap.FAR_SQUARE  # the far lattice's samples over the tile (2 m)
        u = heightmap.surface_u(self.frame, lambda la, lo: dem.sample(la, lo, 15), lat, lat)
        terrain = heightmap.quantise(u, q) * q
        X, Y = np.meshgrid(lat, lat)
        tile_z = ff.surface_at(b.grid.astype(np.float64), 256.0 / self.n, X, Y)
        over = (tile_z - terrain).max()
        self.assertGreater(over, 0.5)           # the tile does bridge the valley
        self.assertGreaterEqual(b.sink, over + ff.SINK_MARGIN - 1e-6)
        self.assertLess(b.sink, over + ff.SINK_MARGIN + 0.01)
        # Flat-ish ground: just the margin plus the sub-vertex detail.
        b2, _, _ = self.build(ff.Tile(0, 0, 0), FakeDem(), q=q, zoom=15)
        self.assertLess(b2.sink, ff.SINK_MARGIN + 0.1)

    def test_group_deltas(self):
        dem = RoughDem()
        parent = ff.Tile(1, 0, 0)
        kids = [ff.Tile(0, i, j) for i in (0, 1) for j in (0, 1)]
        bp, _, _ = self.build(parent, dem)
        bk = [self.build(k, dem, zoom=15)[0] for k in kids]
        d_in, d_out = ff.group_deltas(([(k, b.grid) for k, b in zip(kids, bk)], [(parent, bp.grid)]))
        # Brute force on a 1 m grid.
        xs = np.arange(0, 513, 1.0)
        X, Y = np.meshgrid(xs, xs)
        old = ff.surface_at(bp.grid.astype(float), 512.0 / self.n, X, Y)
        new = np.zeros_like(old)
        for k, b in zip(kids, bk):
            x0, y0, x1, y1 = k.bbox
            m = (X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1)
            new[m] = ff.surface_at(b.grid.astype(float), 256.0 / self.n, X[m] - x0, Y[m] - y0)
        self.assertGreater(d_in, 0.5)
        self.assertGreater(d_out, 0.5)
        self.assertGreaterEqual(d_in + 0.1, (new - old).max())
        self.assertGreaterEqual(d_out + 0.1, (old - new).max())
        self.assertLessEqual(d_in, (new - old).max() + 1e-6)
        # Bound from the design: 1.2 (err_new + err_old) + 0.2.
        bound = 1.2 * (max(b.err for b in bk) + bp.err) + 0.2
        self.assertLessEqual(max(d_in, d_out), bound)

    # .cdae size bounds: palette tiles v1.3.1 §6.4 (90 KB); textured (UV1) 94 KB (V1_3 report) and, with
    # 64 cells, 348 KB (§3.1)
    MAX_KB = {32: 90, 64: 360}
    MAX_TEX_KB = {32: 100, 64: 360}

    def test_cdae_and_dae_written(self):
        tris, nv = self.LAYOUT.tris, (self.n + 1) ** 2 + 4 * (self.n + 1)
        with tempfile.TemporaryDirectory() as d:
            b = ff.write_tile(os.path.join(d, "t.cdae"), self.frame, self.dem, ff.Tile(5, 0, 2), 9, layout=self.LAYOUT)
            self.assertEqual(len(b.pos), 3)
            shape = cdae.read(os.path.join(d, "t.cdae"))
            self.assertEqual(shape["materials"][0]["name"], ff.MATERIAL)
            m = shape["meshes"][0]
            self.assertEqual(m["indices"][0], tris * 3)
            self.assertEqual(m["verts"][0], nv)
            self.assertLessEqual(os.path.getsize(os.path.join(d, "t.cdae")), self.MAX_KB[self.n] * 1024)
            ff.write_tile(os.path.join(d, "t.dae"), self.frame, self.dem, ff.Tile(5, 0, 2), 9, fmt="dae",
                          layout=self.LAYOUT)
            root = ET.parse(os.path.join(d, "t.dae")).getroot()
            ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
            tri = root.find(".//c:triangles", ns)
            self.assertEqual(int(tri.get("count")), tris)
            self.assertEqual(root.find(".//c:material", ns).get("name"), ff.MATERIAL)

    def test_textured_cdae_round_trip(self):
        """A textured tile (UV0 + UV1, its own material) round-trips through cdae.read: 2304 tris with
        32 cells, 8704 with 64 (§3.1), every triangle and vertex back as written."""
        b, _, _ = self.build(ff.Tile(-1, 3, -2), q=1 / 64, zoom=15)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.cdae")
            size = ff.write_built(path, b, "cdae", material="bngEarthSat_1_m1_3_m2", textured=True, tex_px=256)
            self.assertLessEqual(size, self.MAX_TEX_KB[self.n] * 1024)
            shape = cdae.read(path)
            self.assertEqual([x["name"] for x in shape["materials"]], ["bngEarthSat_1_m1_3_m2"])
            m = cdae.mesh_arrays(shape["meshes"][0])
            self.assertEqual(len(m["indices"]) // 3, self.LAYOUT.tris)
            self.assertEqual(len(m["verts"]), len(b.positions))
            self.assertEqual(len(m["tverts2"]), len(b.positions))
            # the same vertex set and the same triangles (by position), wound as the engine stores them
            got = {tuple(np.round(v, 3)) for v in m["verts"]}
            self.assertEqual(got, {tuple(np.round(v, 3)) for v in b.positions.astype(np.float32)})
            tri_pos = lambda P, T: {tuple(sorted(tuple(np.round(P[i], 3)) for i in t)) for t in T}  # noqa: E731
            self.assertEqual(tri_pos(m["verts"], m["indices"].reshape(-1, 3)),
                             tri_pos(b.positions.astype(np.float32), b.tris))
        self.assertLessEqual(self.LAYOUT.tris, ff.TRIS_MAX)

    DEM_ZOOMS = {32: [13, 12, 11, 10, 9, 8, 7, 6, 5],       # v1.3.1
                 64: [14, 13, 12, 11, 10, 9, 8, 7, 6]}      # half the spacing: one zoom finer (cap 14)

    def test_dem_zooms(self):
        self.assertEqual([ff.zoom_for_level(self.LAYOUT, LAT0, lvl) for lvl in range(1, 10)], self.DEM_ZOOMS[self.n])
        self.assertEqual([self.LAYOUT.dem_zoom(LAT0, lvl) for lvl in range(1, 10)], self.DEM_ZOOMS[self.n])
        # anchors made before and after another server's layout get the same zooms (no module state)
        self.assertEqual([ff.zoom_for_level(ff.V131, LAT0, lvl) for lvl in range(1, 10)], self.DEM_ZOOMS[32])

    def test_finer_than_exact_level_refused(self):
        with self.assertRaises(ValueError):
            ff.build_tile(self.frame, self.dem, ff.Tile(self.LAYOUT.exact_level - 1, 0, 0), 15, 1 / 64,
                          layout=self.LAYOUT)


class TileGeometry64Test(TileGeometryTest):
    """The same with 64 cells (M1). The L0 sink is terrain-mode only (frozen at 32 cells)."""
    LAYOUT = ff.Layout(64)
    test_sink_v_valley = None


class AssetsTest(unittest.TestCase):
    def test_palette_dirt_ramp(self):
        img = np.asarray(ff.palette_image()).astype(float) / 255
        self.assertEqual(img.shape, (ff.PALETTE_H, ff.PALETTE_W, 3))
        flat = img[ff.PALETTE_H // 2]
        steep = img[0]
        u = (np.arange(ff.PALETTE_W) + 0.5) / ff.PALETTE_W
        elev = ff.elev_from_u(u)
        low = (u >= 0.08) & (elev < ff.DIRT_UNTIL)
        # Below 1500 m: the terrain's dirt colour, flat or steep (the terrain has no rock tint).
        np.testing.assert_allclose(flat[low], np.broadcast_to(ff.DIRT, flat[low].shape), atol=1 / 255)
        np.testing.assert_allclose(steep[low], np.broadcast_to(ff.DIRT, steep[low].shape), atol=1 / 255)
        high = elev > ff.DIRT_BLEND_TO
        self.assertGreater(np.abs(flat[high] - ff.DIRT).max(), 0.1)
        self.assertGreater(np.abs(steep[high] - flat[high]).max(), 0.05)

    def test_mod_assets_match(self):
        """The palette and material shipped in the mod are what write_mod_assets() produces."""
        d = MOD.joinpath(*ff.MOD_SHAPES)
        with Image.open(d / ff.MOD_PALETTE) as im:
            np.testing.assert_array_equal(np.asarray(im.convert("RGB")), np.asarray(ff.palette_image()))
        import json
        mats = json.loads((d / "main.materials.json").read_text())
        vfs = "/" + "/".join(ff.MOD_SHAPES) + "/" + ff.MOD_PALETTE
        self.assertEqual(mats, ff.material_json(vfs))
        self.assertEqual(mats[ff.MATERIAL]["mapTo"], ff.MATERIAL)


class AdaptiveTileTest(unittest.TestCase):
    """Far tiles from ADAPT_MIN_LEVEL up as adaptive meshes (earthfast.rtin_mesh)."""
    LAYOUT = ff.Layout(64, 1024, True, (256, 256, 256, 512))

    def setUp(self):
        self._eps, ff.ADAPT_EPS = ff.ADAPT_EPS, 0.04
        self.frame = g.EnuFrame(LAT0, LON0)

    def tearDown(self):
        ff.ADAPT_EPS = self._eps

    def build(self, tile, dem):
        return ff.build_tile(self.frame, dem, tile, ff.zoom_for_level(self.LAYOUT, LAT0, tile.level), layout=self.LAYOUT)

    def test_flat_ground_is_few_triangles(self):
        b = self.build(ff.Tile(2, 0, 0), FakeDem())
        self.assertLess(len(b.tris), 8192 // 4)
        self.assertLess(len(b.positions), 65 * 65 // 4)

    def test_rough_ground_keeps_its_detail(self):
        b = self.build(ff.Tile(2, 0, 0), RoughDem())
        ff.ADAPT_EPS = 0.0
        full = self.build(ff.Tile(2, 0, 0), RoughDem())
        self.assertGreater(len(b.tris), len(full.tris) // 3)
        self.assertLessEqual(b.err, full.err + 0.15 * b.size / 64, "a few hundredths of a spacing more error")

    def test_mesh_is_whole_and_upright(self):
        b = self.build(ff.Tile(2, 0, 0), RoughDem())
        self.assertEqual(set(np.unique(b.tris)), set(range(len(b.positions))), "every vertex in a triangle")
        P = b.positions
        a, c, d = P[b.tris[:, 0]], P[b.tris[:, 1]], P[b.tris[:, 2]]
        up = (c[:, 0] - a[:, 0]) * (d[:, 1] - a[:, 1]) - (c[:, 1] - a[:, 1]) * (d[:, 0] - a[:, 0])
        flat = np.abs(up) > 1e-6        # (skirt quads stand vertical)
        self.assertTrue(np.all(up[flat] > 0), "counter-clockwise from +Z")

    def test_grid_is_the_mesh_surface(self):
        """Built.grid (swap deltas, rock faces) is the adaptive surface: surface_at agrees with the mesh's vertices."""
        b = self.build(ff.Tile(2, 0, 0), RoughDem())
        grid = np.asarray(b.grid, np.float64)
        top = b.positions[:, 2] + b.pos[2]
        xy = b.positions[:, :2]
        z = ff.surface_at(grid, b.size / 64, xy[:, 0], xy[:, 1])
        inside = top >= z - 1.0          # (the skirt vertices hang under it)
        np.testing.assert_allclose(top[inside], z[inside], atol=0.02)

    def test_near_levels_stay_the_lattice(self):
        self.assertEqual(ff.ADAPT_MIN_LEVEL, 1)


if __name__ == "__main__":
    unittest.main()
