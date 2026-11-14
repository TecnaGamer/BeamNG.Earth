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

    def test_a_road_cutting_under_the_sea_is_not_flooded(self):
        # GitHub #1059 / #1060: a tunnel's approach cut 2 m under the sea, 300 m from a harbour, was under its tile
        f = geodesy.EnuFrame(40.7, -74.0)
        edge = -74.0 + 0.004                                        # the shore ~340 m east of the player
        east = lambda la, lo, s: np.asarray(lo) > edge             # noqa: E731
        ground = lambda la, lo, s: np.where(np.asarray(lo) > edge, -10.0, 2.0)     # noqa: E731
        ramp = [np.c_[np.linspace(-74.0005, -73.9995, 20), np.full(20, 40.7)]]   # a road, lon / lat, ~85 m long
        cut = lambda la, lo: np.full(np.shape(la), -2.0)           # noqa: E731
        low = seatiles.low_roads(f, 0.0, 0.0, ramp, cut, lambda la, lo: east(la, lo, 64.0))
        self.assertEqual(len(low), 11)                              # (every 8 m)
        before = seatiles.plan(f, 0.0, 0.0, east, ground_at=ground)
        self.assertTrue(any(abs(t.x) <= t.size / 2 and abs(t.y) <= t.size / 2 for t in before), "flooded before")
        tiles = seatiles.plan(f, 0.0, 0.0, east, ground_at=ground, low_roads_xy=low)
        for x, y in low:
            self.assertFalse([t for t in tiles if abs(x - t.x) <= t.size / 2 and abs(y - t.y) <= t.size / 2], (x, y))
        self.assertTrue([t for t in tiles if t.x - t.size / 2 > 400.0], "the harbour keeps its water")
        self.assertEqual(sorted(t.key for t in seatiles.plan(f, 0.0, 0.0, east, ground_at=ground,
                                                            low_roads_xy=np.zeros((0, 2)))),
                         sorted(t.key for t in before), "no low road: as before")

    def test_low_roads_leave_out_the_dry_far_and_shore(self):
        f = geodesy.EnuFrame(40.7, -74.0)
        lon = lambda x: -74.0 + x / (111320.0 * math.cos(math.radians(40.7)))   # noqa: E731
        line = lambda x0, x1: np.c_[[lon(x0), lon(x1)], [40.7, 40.7]]           # noqa: E731
        sea_east = lambda la, lo: np.asarray(lo) > lon(500.0)                    # noqa: E731
        low = lambda la, lo: np.full(np.shape(la), -2.0)                         # noqa: E731
        dry = lambda la, lo: np.full(np.shape(la), 1.0)                          # noqa: E731
        self.assertEqual(len(seatiles.low_roads(f, 0.0, 0.0, [line(0, 80)], dry, sea_east)), 0, "above the sea")
        self.assertEqual(len(seatiles.low_roads(f, 0.0, 0.0, [line(470, 495)], low, sea_east)), 0, "by the shore")
        self.assertEqual(len(seatiles.low_roads(f, 0.0, 0.0, [line(3000, 3080)], low, sea_east)), 0, "far away")
        got = seatiles.low_roads(f, 0.0, 0.0, [line(0, 80)], lambda la, lo: 1 / 0, sea_east,
                                  raw_at=lambda la, lo: np.full(np.shape(la), 10.0))
        self.assertEqual(len(got), 0, "the raw DEM high above the sea: not graded")

    def test_low_roads_asked_lazily_once(self):
        f = geodesy.EnuFrame(40.7, -74.0)
        edge = -74.0 + 0.004
        east = lambda la, lo, s: np.asarray(lo) > edge             # noqa: E731
        ground = lambda la, lo, s: np.where(np.asarray(lo) > edge, -10.0, 2.0)     # noqa: E731
        calls = []

        def low():
            calls.append(1)
            return np.array([[0.0, 0.0]])
        tiles = seatiles.plan(f, 0.0, 0.0, east, ground_at=ground, low_roads_xy=low)
        self.assertEqual(len(calls), 1, "asked once for the whole plan")
        self.assertEqual(sorted(t.key for t in tiles),
                         sorted(t.key for t in seatiles.plan(f, 0.0, 0.0, east, ground_at=ground,
                                                             low_roads_xy=low())), "as the points themselves")
        calls.clear()
        dry = lambda la, lo, s: np.zeros(np.shape(la), bool)       # noqa: E731
        self.assertEqual(seatiles.plan(f, 0.0, 0.0, dry, ground_at=ground, low_roads_xy=low), [])
        self.assertEqual(calls, [], "inland: never asked")

    def test_road_points_every_step_along_each_line(self):
        f = geodesy.EnuFrame(40.7, -74.0)
        lon = lambda x: -74.0 + x / (111320.0 * math.cos(math.radians(40.7)))   # noqa: E731
        lines = [np.c_[[lon(0), lon(20), lon(20)], [40.7, 40.7, 40.7]], np.c_[[lon(100), lon(103)], [40.7, 40.7]],
                 np.array([[lon(0), 40.7]])]
        pts = seatiles.road_points(f, lines, 8.0)
        np.testing.assert_allclose(pts[:, 0], [0.0, 8.0, 16.0, 100.0], atol=0.3)     # (a spherical lon)
        np.testing.assert_allclose(pts[:, 1], 0.0, atol=0.05)

    def test_no_sea_no_tiles(self):
        f = geodesy.EnuFrame(36.9, -110.1)
        self.assertEqual(seatiles.plan(f, 0.0, 0.0, lambda la, lo, s: np.zeros(np.shape(la), bool)), [])


if __name__ == "__main__":
    unittest.main()
