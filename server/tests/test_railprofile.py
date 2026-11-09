"""railprofile.py: the beds of the rail lines as one surface, the same for every tile and grading cell (plan 035)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import railprofile  # noqa: E402
import railways  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def hill(la, lo):
    """A DEM: a gentle slope with a ridge (the line follows it)."""
    x, y, _ = FRAME.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
    return 10.0 + 0.01 * x + 3.0 * np.exp(-((x - 300.0) / 40.0) ** 2)


class RailProfileTest(unittest.TestCase):
    def setUp(self):
        railprofile._NODES.clear()
        # a long line, with a bridge 330 m east of the seam at x = 256: the deck's ramp climbs past a tile's reach
        self.f = features([[(-600.0, 100.0), (586.0, 100.0)], [(586.0, 100.0), (610.0, 100.0)],
                           [(610.0, 100.0), (1500.0, 100.0)]],
                          ["standard_gauge"] * 3, subtypes=["rail"] * 3, flags=[(1, "is_bridge")])
        self.load = lambda key: self.f
        self.keys = lambda frame, box, reach: [0]
        self._cells = railways.cells_near
        railways.cells_near = self.keys

    def tearDown(self):
        railways.cells_near = self._cells

    def beds(self, tx):
        rails = railways.parts(FRAME, [self.f], roads.tile_bbox(tx, 0), clip=True)
        got = railprofile.beds(FRAME, "t", self.load, hill, rails)
        return {(fid, kind, round(float(x), 2), round(float(y), 2)): float(z)
                for (fid, _c, kind, xy), zs in zip(rails, got) for (x, y), z in zip(xy, zs)}

    def test_the_bed_is_the_same_whichever_tile_asks(self):
        a, b = self.beds(0), self.beds(1)
        common = set(a) & set(b)
        self.assertGreater(len(common), 100, "the tiles share their overlap's points")
        for k in common:
            self.assertAlmostEqual(a[k], b[k], places=6, msg=str(k))

    def test_no_point_is_left_without_a_bed(self):
        for tx in (0, 1, 2):
            self.assertTrue(np.isfinite(list(self.beds(tx).values())).all())

    def test_the_nodes_beds_are_blended_continuously(self):
        # points across a lattice line (x = 256) a few cm apart differ by the bed's grade only, not by a node's step
        bed = self.beds(0) | self.beds(1)
        pts = sorted((k[2], z) for k, z in bed.items() if k[1] == "ground")
        xs = np.array([p[0] for p in pts])
        zs = np.array([p[1] for p in pts])
        order = np.argsort(xs)
        step = np.abs(np.diff(zs[order]) / np.maximum(np.diff(xs[order]), 1e-9))
        self.assertLess(float(step.max()), 0.1, "no step along the line: grade stays small (0.1 = 10 %)")

    def test_a_tram_is_left_to_the_window_profile(self):
        got = railprofile.beds(FRAME, "t", self.load, hill, [("t", "tram", "ground", np.array([[0.0, 0.0], [2.0, 0.0]]))])
        self.assertTrue(np.isnan(got[0]).all())


if __name__ == "__main__":
    unittest.main()
