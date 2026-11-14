"""Mesh near-visual mode (docs/V1_3_SPEC.md §B) and the textured tile shapes (§A), for both tile
layouts (docs/V1_4_SPEC.md §3.1 M1: 32 cells, exact level L-2; 64 cells, exact level L-1; every
class has a 64-cell subclass):
  - selection down to the exact level with the min-level radii and no block forcing: no gaps, no
    overlaps; with 64 cells the vertex spacing at a distance is v1.3.1's (never coarser);
  - hysteresis band >= 25 % of every switch distance;
  - exact-level vertices are exactly the terrain PNG's heights at every second sample (the far field's 2 m
    lattice, heightmap.FAR_SQUARE, on the 1 m block; same quantum), also as the engine computes them in float32;
    the next level's vertices are every 4th sample;
  - the triangle diagonals are the Torque TerrainBlock's on that 2 m lattice, checked against an independent port
    of TerrainBlock::getHeight + TerrainFile::_buildGridMap's level-0 split rule;
  - textured shapes: UV0 = footprint, UV1 = world-locked detail coordinates (continuous across
    tiles), in the engine's convention (verified against a stock two-UV-set .cdae if the game is
    installed).
"""
import math
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy as g  # noqa: E402
import heightmap  # noqa: E402

LAT0, LON0 = 36.5785, -118.2923
GAME = Path.home() / ".local/share/Steam/steamapps/common/BeamNG.drive/content/art_shapes.zip"


class RoughDem:
    """Rough synthetic terrain with detail below 2 m; zoom independent."""

    def sample(self, lat, lon, zoom, progress=None):
        lat, lon = np.asarray(lat), np.asarray(lon)
        return (1500 + 300 * np.sin(lat * 90) * np.cos(lon * 70)
                + 4.0 * np.sin(lat * 61000) * np.cos(lon * 47000) + 2.0 * np.sin(lon * 151000))


def torque_height(h, x, y):
    """Torque3D TerrainBlock::getHeight (Engine/source/terrain/terrData.cpp) on a block-local
    height grid h[gy, gx] at square coordinates (x, y), with the level-0 split rule of
    TerrainFile::_buildGridMap (terrFile.cpp): Split45 iff ((squareX ^ squareY) & 1) == 0.
    Written from the C++ independently of farfield.surface_at."""
    xi, yi = int(math.floor(x)), int(math.floor(y))
    xp, yp = x - xi, y - yi
    zbl, zbr = h[yi, xi], h[yi, xi + 1]
    ztl, ztr = h[yi + 1, xi], h[yi + 1, xi + 1]
    if ((xi ^ yi) & 1) == 0:
        if xp > yp:     # bottom half
            return zbl + xp * (zbr - zbl) + yp * (ztr - zbr)
        return zbl + xp * (ztr - ztl) + yp * (ztl - zbl)
    if 1.0 - xp > yp:   # bottom half
        return zbr + (1.0 - xp) * (zbl - zbr) + yp * (ztl - zbl)
    return zbr + (1.0 - xp) * (ztl - ztr) + yp * (ztr - zbr)


def coverage(tiles, px, py, radius, cell=64.0):
    """Count how often each `cell` square of the radius square is covered."""
    gx0, gy0 = math.floor((px - radius) / cell), math.floor((py - radius) / cell)
    nx, ny = math.ceil((px + radius) / cell) - gx0, math.ceil((py + radius) / cell) - gy0
    cnt = np.zeros((ny, nx), np.int16)
    for t in tiles.values():
        x0, y0, x1, y1 = t.bbox
        i0, i1 = max(0, int(x0 // cell) - gx0), min(nx, int(x1 // cell) - gx0)
        j0, j1 = max(0, int(y0 // cell) - gy0), min(ny, int(y1 // cell) - gy0)
        cnt[j0:j1, i0:i1] += 1
    return cnt


class MeshSelectionTest(unittest.TestCase):
    """Parametrised over the layouts: LAYOUT (subclasses: 64 cells)."""
    LAYOUT = ff.V131

    def mesh_select(self, px, py, prev=frozenset(), **kw):
        lay = self.LAYOUT
        return ff.select(px, py, [], prev, min_level=lay.min_level, layout=lay, **kw)

    def test_tiling_no_gaps_or_overlaps(self):
        lay = self.LAYOUT
        ex, radii = lay.exact_level, lay.near_radii
        for px, py, r in ((0.0, 0.0, ff.RADIUS), (1000.5, -333.3, 20000.0), (-12345.0, 6789.0, 20000.0),
                          (31.9, 32.1, 5000.0)):
            with self.subTest(px=px, py=py):
                tiles, refined = self.mesh_select(px, py, radius=r)
                cnt = coverage(tiles, px, py, r)
                self.assertTrue((cnt == 1).all(), f"{int((cnt == 0).sum())} holes, {int((cnt > 1).sum())} overlaps")
                for t in tiles.values():
                    self.assertTrue(ex <= t.level <= ff.MAX_LEVEL)
                    self.assertEqual(t.key, f"{t.level}_{t.i}_{t.j}")
                    self.assertEqual(ff.tile_of(t.key), t)
                    self.assertEqual(t.bbox[0] % t.size, 0)
                    d = t.distance(px, py)
                    # min-level radii (V1_3_SPEC §B): the exact level within 150 m, at most the next
                    # level within 400 m (32 cells: L-2, L-1; 64 cells: L-1, L0)
                    if d < radii[ex]:
                        self.assertEqual(t.level, ex, t)
                    if d < radii[ex + 1]:
                        self.assertLessEqual(t.level, ex + 1, t)
                    # a leaf is never closer than its own switch distance (no hysteresis here)
                    self.assertGreaterEqual(d, ff.switch_distance(t.level, min_level=ex, layout=lay), t)
                    if t.level < ff.MAX_LEVEL:
                        self.assertIn((t.level + 1, t.i // 2, t.j // 2), refined)

    # tiles at a pose (§2.1: v1.3.1 811 with hysteresis along a drive; 64 cells: 300), and of the exact
    # and the next level (the same areas: 150 m and 400 m around the player)
    COUNTS = {32: (700, 900), 64: (280, 320)}
    NEAR_COUNTS = {32: ((30, 50), (30, 60)), 64: ((12, 20), (8, 20))}

    def test_counts(self):
        lay = self.LAYOUT
        tiles, _ = self.mesh_select(0.0, 0.0)
        by = {}
        for t in tiles.values():
            by[t.level] = by.get(t.level, 0) + 1
        print(f"\n  {lay.cells}-cell mesh initial set at (0, 0): {len(tiles)} tiles, by level "
              f"{dict(sorted(by.items()))}", file=sys.stderr)
        ex = lay.exact_level
        (a0, a1), (b0, b1) = self.NEAR_COUNTS[lay.cells]
        self.assertTrue(a0 <= by[ex] <= a1, by)
        self.assertTrue(b0 <= by[ex + 1] <= b1, by)
        lo, hi = self.COUNTS[lay.cells]
        self.assertTrue(lo <= len(tiles) <= hi, len(tiles))
        # the terrain mode's L0 floor is unchanged
        t0, _ = ff.select(0.0, 0.0, [ff.Block(0.0, 0.0).bbox])
        self.assertEqual(min(t.level for t in t0.values()), 0)

    def test_terrain_mode_ignores_radii(self):
        if self.LAYOUT.cells != 32:
            # 64 cells (mesh only: terrain mode is frozen at 32): L0 within 150 m of the player is
            # refined to L-1, L1 within 400 m to L0 (K'(1) * 512 = 384 m < 400), L2 at K' * S
            lay = self.LAYOUT
            sw = [ff.switch_distance(lvl, min_level=-1, layout=lay) for lvl in range(-1, 5)]
            self.assertEqual(sw, [0.0, 150.0, 400.0, 1.5 * 1024, 1.5 * 2048, 1.25 * 4096])
            with self.assertRaises(ValueError):
                ff.Layout(64, mesh=False)
            return
        self.assertEqual(ff.switch_distance(0), 0.0)
        self.assertEqual(ff.switch_distance(1), 3 * 512)
        self.assertEqual(ff.switch_distance(0, min_level=-2), 400.0)
        self.assertEqual(ff.switch_distance(-1, min_level=-2), 150.0)
        self.assertEqual(ff.switch_distance(-2, min_level=-2), 0.0)
        self.assertEqual(ff.switch_distance(1, min_level=-2), 3 * 512)
        self.assertEqual(ff.switch_distance(0, radii={-2: 100.0, -1: 300.0}, min_level=-2), 300.0)

    def test_hysteresis_near_levels(self):
        """An oscillation of +-20 m around the 150 m (next level -> exact) and the 400 m thresholds
        changes nothing after the first refinement; past (1 + h) x the distance the node coarsens.
        (32 cells: L-1 -> L-2 and L0 -> L-1; 64 cells: L0 -> L-1 and L1 -> L0.)"""
        ex = self.LAYOUT.exact_level
        for node, thr in (((ex + 1, 3, 0), 150.0), ((ex + 2, 2, 0), 400.0)):
            with self.subTest(node=node):
                x0 = ff.level_size(node[0]) * node[1]       # west edge of the node; player west of it
                _, prev = self.mesh_select(x0 - thr - 20.0, 30.0)
                self.assertNotIn(node, prev)
                sets = []
                for n, dx in enumerate([20.0, -20.0] * 5):
                    tiles, prev = self.mesh_select(x0 - thr + dx, 30.0, prev)
                    sets.append(set(tiles))
                    self.assertIn(node, prev)
                self.assertTrue(all(s == sets[0] for s in sets[1:]))
                _, prev = self.mesh_select(x0 - thr * (1 + ff.HYSTERESIS) + 1.0, 30.0, prev)
                self.assertIn(node, prev)
                _, prev = self.mesh_select(x0 - thr * (1 + ff.HYSTERESIS) - 1.0, 30.0, prev)
                self.assertNotIn(node, prev)

    def test_swap_groups_with_near_levels(self):
        a, ra = self.mesh_select(0.0, 0.0)
        b, _ = self.mesh_select(100.0 * self.LAYOUT.cells / 32, 30.0, ra)     # (bigger tiles: a longer move)
        groups = ff.swap_groups(set(a), b)
        self.assertTrue(groups)
        cur = set(a)
        for adds, removes in groups:
            self.assertTrue(adds and removes)
            ca = coverage({k: ff.tile_of(k) for k in adds}, 0, 0, 2000)
            cr = coverage({k: ff.tile_of(k) for k in removes}, 0, 0, 2000)
            np.testing.assert_array_equal(ca, cr)
            cur = (cur - set(removes)) | set(adds)
        self.assertEqual(cur, set(b))


class LatticeTest(unittest.TestCase):
    """Exact-level tiles are the TerrainBlock's surface at their vertices, with its diagonals (§B;
    L-2 with 32 cells, L-1 with 64)."""
    LAYOUT = ff.V131
    # exact-level tiles (i, j) inside the 1 km block centred at (256, -512): one inside, the SW-most, the NE-most
    EXACT_TILES = {32: ((3, -5), (-4, -16), (10, -2)), 64: ((1, -3), (-2, -8), (4, -2))}
    NEXT_TILE = {32: (1, -3), 64: (0, -2)}

    @classmethod
    def setUpClass(cls):
        cls.frame = g.EnuFrame(LAT0, LON0)
        cls.dem = RoughDem()
        cls.q = 1 / 64
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cx, cls.cy = 256.0, -512.0
        path = os.path.join(cls.tmp.name, "hm.png")
        info = heightmap.generate(cls.frame, lambda la, lo: cls.dem.sample(la, lo, 15), path, cls.cx, cls.cy, q=cls.q,
                                  size=cls.LAYOUT.block)
        with Image.open(path) as im:
            cls.q16 = np.asarray(im).astype(np.int64)[::-1]           # [gy, gx], gy = north
        cls.info = info
        cls.h = heightmap.heights(cls.q16, info["zBase"], info["maxHeight"])
        half = heightmap.half_extent(cls.LAYOUT.block)
        cls.x0, cls.y0 = cls.cx - half, cls.cy - half
        cls.c, cls.ex = cls.LAYOUT.cells, cls.LAYOUT.exact_level
        cls.st = int(heightmap.FAR_SQUARE / heightmap.SQUARE)      # block samples per far-lattice square (2)
        cls.h2 = cls.h[::cls.st, ::cls.st]                           # the far field's 2 m lattice, block-local

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, tile):
        return ff.build_tile(self.frame, self.dem, tile, 15, self.q, with_sink=False, layout=self.LAYOUT)

    def test_block_lattice_parity(self):
        """Every block starts at an even lattice index, so block-local square parity is the
        world lattice parity (torque_split45's premise)."""
        for c in (0.0, 256.0, -256.0, 768.0, -9984.0, 24832.0):
            self.assertEqual(heightmap.lattice_index(c, self.LAYOUT.block) % 2, 0, c)
        for lvl in range(self.ex, 1):
            self.assertEqual(ff.level_size(lvl) / heightmap.SQUARE % 2, 0)

    def test_exact_vertices_equal_terrain_png(self):
        m, st = self.c + 1, self.st
        for i, j in self.EXACT_TILES[self.c]:
            tile = ff.Tile(self.ex, i, j)
            with self.subTest(tile=tile.key):
                b = self.build(tile)
                gx0 = int((tile.bbox[0] - self.x0) / heightmap.SQUARE)
                gy0 = int((tile.bbox[1] - self.y0) / heightmap.SQUARE)
                want = self.h[gy0:gy0 + st * (m - 1) + 1:st, gx0:gx0 + st * (m - 1) + 1:st]
                self.assertEqual(want.shape, (m, m))
                rel = b.positions[:m * m, 2].reshape(m, m)
                got = rel.astype(np.float32).astype(np.float64) + b.pos[2]      # as written to the .cdae
                self.assertTrue((got == want).all(), np.abs(got - want).max())
                self.assertEqual(b.pos[2], round(b.pos[2]))                     # integral origin
                # as the engine computes both in float32: zBase + q16 * (maxHeight / 65536) for the
                # terrain, object position + vertex for the TSStatic
                f32 = np.float32
                q16 = self.q16[gy0:gy0 + st * (m - 1) + 1:st, gx0:gx0 + st * (m - 1) + 1:st]
                eng = f32(self.info["zBase"]) + q16.astype(f32) * f32(
                    self.info["maxHeight"] / 65536)
                mesh = f32(b.pos[2]) + rel.astype(f32)
                self.assertTrue((eng == mesh).all())
                # and the x, y of the vertices are the lattice points
                xy = b.positions[:m * m, :2] + b.pos[:2]
                self.assertTrue((xy[:, 0] % 2 == 0).all() and (xy[:, 1] % 2 == 0).all())

    def test_next_level_vertices_are_every_second_sample(self):
        m, st = self.c + 1, 2 * self.st                         # every 2nd far-lattice sample: every 4th block one
        tile = ff.Tile(self.ex + 1, *self.NEXT_TILE[self.c])
        b = self.build(tile)
        gx0 = int((tile.bbox[0] - self.x0) / heightmap.SQUARE)
        gy0 = int((tile.bbox[1] - self.y0) / heightmap.SQUARE)
        want = self.h[gy0:gy0 + st * self.c + 1:st, gx0:gx0 + st * self.c + 1:st]
        self.assertEqual(want.shape, (m, m))
        got = b.positions[:m * m, 2].reshape(m, m).astype(np.float32).astype(np.float64) + b.pos[2]
        self.assertTrue((got == want).all())

    def test_diagonals_match_torque(self):
        """The exact-level surface equals Torque's TerrainBlock::getHeight on the far field's 2 m lattice
        everywhere (not only at the vertices), and every cell of the built mesh uses that block's diagonal (the
        1 m physics block is finer than the far tiles since 2026-11-01)."""
        c, m = self.c, self.c + 1
        tile = ff.Tile(self.ex, *self.EXACT_TILES[c][0])
        b = self.build(tile)
        grid = b.grid.astype(np.float64)
        rng = np.random.default_rng(3)
        pts = rng.random((4000, 2)) * tile.size
        pts[:20] = [[2 * k + 0.5, 2 * k + 1.5] for k in range(20)]        # upper triangles near the diagonal
        ours = ff.surface_at(grid, 2.0, pts[:, 0], pts[:, 1])
        bx = (tile.bbox[0] - self.x0 + pts[:, 0]) / heightmap.FAR_SQUARE
        by = (tile.bbox[1] - self.y0 + pts[:, 1]) / heightmap.FAR_SQUARE
        theirs = np.array([torque_height(self.h2, x, y) for x, y in zip(bx, by)])
        np.testing.assert_allclose(ours, theirs, atol=1e-9)
        # v1.2's uniform SE-NW split would differ on this rough ground (the test has teeth).
        i, j = np.minimum((pts // 2.0).astype(int), c - 1).T
        a, cc = pts[:, 0] / 2.0 - i, pts[:, 1] / 2.0 - j
        z00, z10, z01, z11 = grid[j, i], grid[j, i + 1], grid[j + 1, i], grid[j + 1, i + 1]
        uniform = np.where(a + cc <= 1, z00 + a * (z10 - z00) + cc * (z01 - z00),
                           z11 + (1 - a) * (z01 - z11) + (1 - cc) * (z10 - z11))
        self.assertGreater(np.abs(uniform - theirs).max(), 0.05)
        # Mesh triangles: cell (i, j) is triangles 2k, 2k+1 (k = j * c + i); the shared edge is
        # its diagonal.
        tris = b.tris[:2 * c * c].reshape(c * c, 2, 3)
        gx0 = int((tile.bbox[0] - self.x0) / 2)
        gy0 = int((tile.bbox[1] - self.y0) / 2)
        for k, (t1, t2) in enumerate(tris):
            j, i = divmod(k, c)
            shared = sorted(set(t1.tolist()) & set(t2.tolist()))
            a = j * m + i
            sw_ne = shared == sorted([a, a + m + 1])
            se_nw = shared == sorted([a + 1, a + m])
            self.assertTrue(sw_ne or se_nw)
            self.assertEqual(sw_ne, bool(ff.torque_split45(gx0 + i, gy0 + j)), (i, j))
        # winding: every grid triangle faces up
        P = b.positions
        main = b.tris[:2 * c * c]
        n = np.cross(P[main[:, 1]] - P[main[:, 0]], P[main[:, 2]] - P[main[:, 0]])
        self.assertTrue((n[:, 2] > 0).all())

    def test_bilinear_tolerance(self):
        """The exact surface against the engine's collision rule, bilinear per square (probe p_diag), on the
        far field's 2 m lattice (the 1 m block's every second sample): between vertices they differ by at most
        |twist|/4 (the Targets' tolerance), and by exactly that at a square's centre."""
        c = self.c
        tile = ff.Tile(self.ex, *self.EXACT_TILES[c][0])
        grid = self.build(tile).grid.astype(np.float64)
        rng = np.random.default_rng(11)
        pts = rng.random((3000, 2)) * tile.size
        mesh = ff.surface_at(grid, 2.0, pts[:, 0], pts[:, 1])
        args = (self.q16[::self.st, ::self.st], self.info["zBase"], self.info["maxHeight"], self.cx, self.cy)
        sq = heightmap.FAR_SQUARE
        bil = np.array([heightmap.height_at(*args, tile.bbox[0] + x, tile.bbox[1] + y, square=sq) for x, y in pts])
        i, j = np.minimum((pts // 2.0).astype(int), c - 1).T
        twist = np.abs(grid[j, i] - grid[j, i + 1] - grid[j + 1, i] + grid[j + 1, i + 1])
        self.assertTrue((np.abs(mesh - bil) <= twist / 4 + 1e-9).all())
        centre = ff.surface_at(grid, 2.0, 2.0 * i + 1.0, 2.0 * j + 1.0)
        bil_c = np.array([heightmap.height_at(*args, tile.bbox[0] + 2.0 * a + 1.0, tile.bbox[1] + 2.0 * b + 1.0,
                                              square=sq) for a, b in zip(i, j)])
        np.testing.assert_allclose(np.abs(centre - bil_c), twist / 4, atol=1e-9)

    def test_err_and_no_sink(self):
        b = self.build(ff.Tile(self.ex, *self.EXACT_TILES[self.c][0]))
        self.assertIsNone(b.sink)
        self.assertGreater(b.err, 0.0)          # 1 m reference: the DEM detail between lattice points
        self.assertLess(b.err, 8.0)
        self.assertEqual(b.skirt, ff.skirt_depth(ff.level_size(self.ex), b.err))
        b0 = ff.build_tile(self.frame, self.dem, ff.Tile(0, 0, -2), 15, self.q, with_sink=True, layout=self.LAYOUT)
        self.assertIsNotNone(b0.sink)


class TexturedShapeTest(unittest.TestCase):
    LAYOUT = ff.V131

    def test_uv0_footprint_uv1_detail(self):
        frame = g.EnuFrame(LAT0, LON0)
        ex = self.LAYOUT.exact_level
        se = ff.level_size(ex)
        with tempfile.TemporaryDirectory() as d:
            uv1 = {}
            for tile in (ff.Tile(ex, 0, 0), ff.Tile(ex, 1, 0), ff.Tile(3, -1, 2)):
                b = ff.build_tile(frame, RoughDem(), tile, 15 if tile.level <= 0 else 10,
                                  1 / 64 if tile.level <= 0 else None, with_sink=False, layout=self.LAYOUT)
                path = os.path.join(d, f"{tile.key}.cdae")
                ff.write_built(path, b, "cdae", material="bngEarthSat_1_m2_0_0", textured=True)
                shape = cdae.read(path)
                self.assertEqual(shape["materials"][0]["name"], "bngEarthSat_1_m2_0_0")
                m = cdae.mesh_arrays(shape["meshes"][0])
                V = m["verts"].astype(np.float64)
                local = V[:, :2]                # verts are relative to the tile origin (SW corner)
                S = tile.size
                # UV0 = footprint (engine stores v flipped), UV1 = metres / DETAIL_M (flipped too)
                np.testing.assert_allclose(m["tverts"][:, 0], local[:, 0] / S, atol=1e-6)
                np.testing.assert_allclose(m["tverts"][:, 1], 1 - local[:, 1] / S, atol=1e-6)
                np.testing.assert_allclose(m["tverts2"][:, 0], local[:, 0] / ff.DETAIL_M, atol=1e-4)
                np.testing.assert_allclose(m["tverts2"][:, 1], 1 - local[:, 1] / ff.DETAIL_M, atol=1e-4)
                self.assertTrue((m["tverts"] >= -1e-6).all() and (m["tverts"] <= 1 + 1e-6).all())
                uv1[tile.key] = (V[:, :2] + b.pos[:2], m["tverts2"].astype(np.float64))
            # world-locked: on the shared edge x = se the two exact tiles' UV1 differ by whole repeats
            (pa, ua), (pb, ub) = uv1[ff.Tile(ex, 0, 0).key], uv1[ff.Tile(ex, 1, 0).key]
            ea = {round(p[1], 3): u for p, u in zip(pa, ua) if abs(p[0] - se) < 1e-6}
            eb = {round(p[1], 3): u for p, u in zip(pb, ub) if abs(p[0] - se) < 1e-6}
            self.assertTrue(ea and ea.keys() == eb.keys())
            for y in ea:
                diff = ea[y] - eb[y]
                np.testing.assert_allclose(diff, np.round(diff), atol=1e-4)

    def test_palette_tiles_have_no_uv1(self):
        frame = g.EnuFrame(LAT0, LON0)
        b = ff.build_tile(frame, RoughDem(), ff.Tile(2, 0, 0), 12, layout=self.LAYOUT)
        with tempfile.TemporaryDirectory() as d:
            ff.write_built(os.path.join(d, "t.cdae"), b)
            m = cdae.read(os.path.join(d, "t.cdae"))["meshes"][0]
            self.assertEqual(m["tverts2"][0], 0)

    @unittest.skipUnless(GAME.is_file(), "BeamNG.drive not installed")
    def test_uv2_convention_matches_engine(self):
        """A stock shape compiled by the engine from a Collada file with two UV sets stores
        tverts2 = (u1, 1 - v1), exactly as tverts = (u0, 1 - v0), which is what cdae.encode
        writes for uvs2 (read-only check against the game's art_shapes.zip; the first of these the
        installed game has: 0.40 dropped the string course module)."""
        with zipfile.ZipFile(GAME) as z:
            names = set(z.namelist())
            base = next((b for b in ("art/shapes/common/building_architect_modules/string_course/G_C_SC_01_300x20",
                                     "art/shapes/interface/raceLineAssist/s_rla_marker_arrow",
                                     "art/shapes/race/dragstrip/WinLight_Timeboard")
                         if b + ".dae" in names and b + ".cdae" in names), None)
            if base is None:
                self.skipTest("no stock two-UV shape in this game version")
            txt = z.read(base + ".dae").decode()
            shape = cdae.decode(z.read(base + ".cdae"))

        def arr(key):
            return np.array(re.search(rf'id="[^"]*{key}-array" count="\d+">([^<]*)<', txt).group(1).split(), float)
        pos, uv0, uv1 = arr("mesh-positions").reshape(-1, 3), arr("mesh-map-0").reshape(-1, 2), arr("mesh-map-1").reshape(-1, 2)
        p = np.array(re.search(r"<p>([^<]*)</p>", txt).group(1).split(), int).reshape(-1, 4)
        m = cdae.mesh_arrays(shape["meshes"][0])
        for row in p:
            k = np.where((np.abs(m["verts"] - pos[row[0]]).max(1) < 1e-4)
                         & (np.abs(m["tverts"] - [uv0[row[2], 0], 1 - uv0[row[2], 1]]).max(1) < 1e-4))[0]
            self.assertTrue(len(k))
            self.assertLess(np.abs(m["tverts2"][k] - [uv1[row[2], 0], 1 - uv1[row[2], 1]]).max(1).min(), 1e-6)
        self.assertEqual(shape["meshes"][0]["flags"], 0)
        # ours, for comparison
        ours = cdae.mesh_arrays(cdae.decode(cdae.encode(pos[:3], [[0, 1, 2]], uvs=uv0[:3], uvs2=uv1[:3]))["meshes"][0])
        for i in range(3):
            j = np.where(np.abs(ours["verts"] - pos[i]).max(1) < 1e-6)[0][0]
            np.testing.assert_allclose(ours["tverts2"][j], [uv1[i, 0], 1 - uv1[i, 1]], atol=1e-6)



# --- 64 cells (docs/V1_4_SPEC.md §3.1, M1) ---------------------------------------------------
L64 = ff.Layout(64)


class MeshSelection64Test(MeshSelectionTest):
    LAYOUT = L64

    def test_spacing_matches_v131(self):
        """Each level keeps its size and splits where v1.3.1 split the level below it, so the
        vertex spacing at a distance is v1.3.1's: for the LOD rule at 25 sample distances exactly,
        and in the real selections never coarser and at most one level finer (a 64-cell node is
        refined when it is nearer than the switch distance even where one of its four 32-cell
        children is not)."""
        def rule_spacing(lay, d):
            lvl = next(lv for lv in range(lay.min_level, ff.MAX_LEVEL + 1)
                       if d >= ff.switch_distance(lv, min_level=lay.min_level, layout=lay) or lv == ff.MAX_LEVEL)
            return ff.level_size(lvl) / lay.cells

        dists = np.geomspace(5.0, 70000.0, 25)
        for d in dists:
            self.assertEqual(rule_spacing(L64, d), rule_spacing(ff.V131, d), d)

        def tile_at(tiles, x, y):
            for t in tiles.values():
                x0, y0, x1, y1 = t.bbox
                if x0 <= x < x1 and y0 <= y < y1:
                    return t
        same = 0
        for px, py in ((1234.0, -777.0), (0.0, 0.0), (-12345.0, 6789.0)):
            a, _ = ff.select(px, py, min_level=ff.V131.min_level)
            b, _ = self.mesh_select(px, py)
            for d in dists:
                x, y = px + d * math.sin(math.radians(37.0)), py + d * math.cos(math.radians(37.0))
                sa, sb = tile_at(a, x, y).size / 32, tile_at(b, x, y).size / 64
                self.assertIn(sb, (sa, sa / 2), (px, py, d))
                same += sb == sa
        self.assertGreaterEqual(same, 0.8 * 3 * len(dists))


class Lattice64Test(LatticeTest):
    LAYOUT = L64


class TexturedShape64Test(TexturedShapeTest):
    LAYOUT = L64
    test_uv2_convention_matches_engine = None       # layout independent


if __name__ == "__main__":
    unittest.main()
