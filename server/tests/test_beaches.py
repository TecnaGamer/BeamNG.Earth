"""Sandy shores in WorldCover (beaches.py, GitHub #578)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import beaches  # noqa: E402
import surface  # noqa: E402


def coast(x):
    """Cottesloe's transect by x (m east): the sea, bare sand, the berm's grassland, bare, then town (built-up)."""
    x = np.asarray(x, np.float64)
    return np.select([x < 0, x < 8, x < 23, x < 60], [80, 60, 30, 60], 50).astype(np.uint8)


def creek(x):
    """A 10 m stream with grassy, partly bare banks: no beach."""
    x = np.asarray(x, np.float64)
    return np.select([np.abs(x) < 5, np.abs(x) < 12], [80, 60], 30).astype(np.uint8)


def points(land, xs):
    cls = land(xs)
    return beaches.sand_points(cls, lambda idx, dx, dy: land(xs[idx] + dx))


class PointTest(unittest.TestCase):
    def test_a_beach_strip_is_sand_its_berm_too(self):
        xs = np.array([4.0, 15.0, 40.0, 58.0, 90.0, 200.0])
        np.testing.assert_array_equal(points(coast, xs), [True, True, True, True, False, False])

    def test_grassland_far_from_bare_on_a_shore_keeps_its_grass(self):
        land = lambda x: np.where(np.asarray(x) < 0, 80, 30).astype(np.uint8)     # noqa: E731 (a lawn to the quay)
        self.assertFalse(points(land, np.array([10.0, 30.0])).any())

    def test_a_stream_bank_is_not_a_beach(self):
        self.assertFalse(points(creek, np.array([8.0, 15.0, 30.0])).any())


class GridTest(unittest.TestCase):
    SQUARE = 2.0

    def grid(self, land):
        x = (np.arange(-100, 200) * self.SQUARE)[None, :].repeat(20, 0)
        return land(x)

    def test_the_grid_agrees_with_the_points(self):
        g = self.grid(coast)
        sand = beaches.sand_grid(g, self.SQUARE)
        x = np.arange(-100, 200) * self.SQUARE
        row = sand[10]
        self.assertTrue(row[(x >= 0) & (x < 58)].all())
        self.assertFalse(row[x >= 60].any())
        self.assertFalse(row[x < 0].any(), "the sea is not sand")
        self.assertFalse(beaches.sand_grid(self.grid(creek), self.SQUARE).any())

    def test_no_grass_layer_on_the_berm(self):
        g = self.grid(coast)
        idx = surface.classify(g, np.zeros(g.shape), self.SQUARE)
        x = np.arange(-100, 200) * self.SQUARE
        berm = (x >= 8) & (x < 23)
        self.assertTrue((idx[10][berm] == surface.LOOSE).all())
        inland = self.grid(lambda x: np.full(np.shape(x), 30, np.uint8))
        self.assertTrue((surface.classify(inland, np.zeros(inland.shape), self.SQUARE) == surface.GRASS).all())


if __name__ == "__main__":
    unittest.main()
