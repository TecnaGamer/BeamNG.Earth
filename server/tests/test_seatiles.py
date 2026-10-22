"""seatiles: the quadtree of water tiles around the player (small near, large far, every nearby point in
one tile), kept where there is sea, each at the sea level of its centre and tilted to the local
vertical."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import seatiles  # noqa: E402


class SquaresTest(unittest.TestCase):
    def test_cover_once_small_near_large_far(self):
        x, y = 1234.0, -567.0
        sq = seatiles.squares(x, y)
        self.assertLessEqual(len(sq), 80)
        rng = np.random.default_rng(1)
        for px, py in rng.uniform(-6000, 6000, (300, 2)) + (x, y):
            cx = (math.floor(x / seatiles.TILE_MIN) + 0.5) * seatiles.TILE_MIN
            cy = (math.floor(y / seatiles.TILE_MIN) + 0.5) * seatiles.TILE_MIN
            if math.hypot(px - cx, py - cy) > seatiles.RADIUS_M:
                continue
            n = sum(1 for x0, y0, s in sq if x0 <= px < x0 + s and y0 <= py < y0 + s)
            self.assertEqual(n, 1, (px, py))
        near = [s for x0, y0, s in sq if x0 <= x < x0 + s and y0 <= y < y0 + s]
        self.assertEqual(near, [seatiles.TILE_MIN], "the player's own tile is the smallest")
        cx = (math.floor(x / seatiles.TILE_MIN) + 0.5) * seatiles.TILE_MIN
        cy = (math.floor(y / seatiles.TILE_MIN) + 0.5) * seatiles.TILE_MIN
        for x0, y0, s in sq:            # no tile is larger than its distance allows
            if s > seatiles.TILE_MIN:
                self.assertGreaterEqual(seatiles._dist(cx, cy, x0, y0, s), seatiles.SPLIT_K * s - 1e-6)

    def test_same_square_same_set(self):
        """Anywhere in the player's TILE_MIN square the set is the same (no tiles made every few metres);
        the next square changes only some of them."""
        a = set(seatiles.squares(10.0, 10.0))
        self.assertEqual(a, set(seatiles.squares(500.0, 300.0)))
        b = set(seatiles.squares(600.0, 10.0))
        self.assertGreater(len(a & b), len(a) * 0.6)


class PlanTest(unittest.TestCase):
    def test_sea_only_level_and_tilt(self):
        f = geodesy.EnuFrame(39.0, -76.4)
        east = lambda la, lo, s: np.asarray(lo) > -76.4 + 0.02     # noqa: E731  sea east of ~1.7 km
        tiles = seatiles.plan(f, 0.0, 0.0, east)
        self.assertTrue(tiles)
        for t in tiles:
            self.assertGreater(t.x + t.size / 2, 1000.0, t)            # no tile wholly west of the sea
            la, lo, _ = f.enu_to_geodetic(t.x, t.y, 0.0)
            self.assertAlmostEqual(t.z, float(f.geodetic_to_enu(la, lo, 0.0)[2]), places=6)
            self.assertLess(t.z, 0.0)                                  # below the anchor's tangent plane
            self.assertAlmostEqual(t.nx, t.x / seatiles.EARTH_R)
        far = max(tiles, key=lambda t: math.hypot(t.x, t.y))
        self.assertAlmostEqual(far.z, -(far.x ** 2 + far.y ** 2) / (2 * seatiles.EARTH_R), delta=0.05)
        self.assertEqual(len(tiles[0].to_msg()), 7)

    def test_land_under_the_sea_is_not_flooded(self):
        # GitHub #8: the polders by the coast (land 4 m under the sea) were under the sea's tiles
        f = geodesy.EnuFrame(52.3, 4.6)
        edge = 4.6 + 0.02                                           # the shore ~1.4 km east of the player
        east = lambda la, lo, s: np.asarray(lo) > edge             # noqa: E731
        polder = lambda la, lo, s: np.where(np.asarray(lo) > edge, -10.0, -4.0)    # noqa: E731
        tiles = seatiles.plan(f, 0.0, 0.0, east, ground_at=polder)
        self.assertTrue(tiles)
        for t in tiles:
            if math.hypot(t.x, t.y) > 2000.0:
                continue                                           # (far away a mostly-sea tile is kept whole)
            la, lo, _ = f.enu_to_geodetic(t.x - t.size / 2 + 1.0, t.y, 0.0)    # its west edge
            self.assertGreater(float(lo), edge - 0.0015, t)        # no tile reaches more than ~100 m over the polder
        dry = lambda la, lo, s: np.where(np.asarray(lo) > edge, -10.0, 2.0)    # noqa: E731
        self.assertEqual(sorted(t.key for t in seatiles.plan(f, 0.0, 0.0, east, ground_at=dry)),
                         sorted(t.key for t in seatiles.plan(f, 0.0, 0.0, east)), "land above the sea: as before")

    def test_sunken_road_near_the_player_is_not_flooded(self):
        # 2026-10-21: Rotterdam's 's-Gravendijkwal, a 30 m wide road 1 m under the sea, filled by a 2048 m tile
        f = geodesy.EnuFrame(51.9, 4.47)
        edge = 4.47 + 0.02                                          # the river ~1.4 km east of the player
        east = lambda la, lo, s: np.asarray(lo) > edge              # noqa: E731
        trench_e = f.geodetic_to_enu(51.9, 4.47 + 0.002, 0.0)[0]    # a N-S trench 140 m east, 30 m wide

        def ground(la, lo, spacing):
            e = np.asarray(f.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)[0])
            return np.where(np.asarray(lo) > edge, -10.0,
                            np.where((np.abs(e - trench_e) < 15.0) & (np.abs(np.asarray(la) - 51.9) < 0.002),
                                     -1.0, 3.0))
        tiles = seatiles.plan(f, 0.0, 0.0, east, ground_at=ground)
        self.assertTrue(tiles)
        for t in tiles:
            inside = abs(t.x - trench_e) < t.size / 2 + 15.0 and abs(t.y) < t.size / 2 + 150.0
            self.assertFalse(inside, t)
        dry = lambda la, lo, s: np.where(np.asarray(lo) > edge, -10.0, 3.0)    # noqa: E731
        self.assertLess(len(tiles), 2 * len(seatiles.plan(f, 0.0, 0.0, east, ground_at=dry)) + 20)

    def test_no_sea_no_tiles(self):
        f = geodesy.EnuFrame(36.9, -110.1)
        self.assertEqual(seatiles.plan(f, 0.0, 0.0, lambda la, lo, s: np.zeros(np.shape(la), bool)), [])


if __name__ == "__main__":
    unittest.main()
