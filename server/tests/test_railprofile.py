"""railprofile.py: the beds of the rail lines as one surface, the same for every tile and grading cell (plan 035)."""
import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import railprofile  # noqa: E402
import railways  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def hill_xy(xy):
    la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    return hill(la, lo)


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
        got = railprofile.beds(FRAME, "t", self.load, hill_xy, rails)
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

    def test_smoothing_levels_a_bump_but_pins_the_ends_and_limits_the_cut(self):
        xy = np.c_[np.arange(0.0, 400.0, 2.0), np.zeros(200)]
        zb = 10.0 + np.where(np.abs(xy[:, 0] - 200.0) < 6.0, 2.0, 0.0)          # a 12 m, 2 m high spike
        z = railprofile.smooth(xy, zb)
        self.assertLess(float(z.max()), 10.0 + 0.5, "the spike is levelled to a bump under half a metre")
        self.assertEqual(float(z[0]), float(zb[0]))
        self.assertEqual(float(z[-1]), float(zb[-1]))
        self.assertLessEqual(float(np.abs(z - zb).max()), railprofile.MAX_CUT_M + 1e-9)
        ramp = 10.0 + 0.01 * xy[:, 0]
        np.testing.assert_allclose(railprofile.smooth(xy, ramp), ramp, atol=1e-6, err_msg="a straight grade stays")

    def test_a_tram_is_left_to_the_window_profile(self):
        got = railprofile.beds(FRAME, "t", self.load, hill_xy, [("t", "tram", "ground", np.array([[0.0, 0.0], [2.0, 0.0]]))])
        self.assertTrue(np.isnan(got[0]).all())


class JoinBedsTest(unittest.TestCase):
    def line(self, p0, p1, step=2.0):
        n = int(np.hypot(p1[0] - p0[0], p1[1] - p0[1]) // step) + 1
        t = np.linspace(0.0, 1.0, n)
        return np.c_[p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t]

    def test_a_crossover_is_brought_to_the_beds_of_both_lines_it_joins(self):
        a = self.line((0.0, 0.0), (600.0, 0.0))
        b = self.line((0.0, 4.5), (600.0, 4.5))
        c = self.line((100.0, 0.0), (148.0, 4.5))           # its ends on a and b, 48 m long
        za, zb_ = np.full(len(a), 100.0), np.full(len(b), 100.0)
        zc = np.linspace(100.45, 99.42, len(c))             # laid from its own ground: 0.45 over, 0.58 under
        rails = [("a", "standard_gauge", "ground", a), ("b", "standard_gauge", "ground", b),
                 ("c", "standard_gauge", "ground", c)]
        out = railprofile.join_beds(rails, [za, zb_, zc])
        self.assertAlmostEqual(float(out[2][0]), 100.0, places=6)
        self.assertAlmostEqual(float(out[2][-1]), 100.0, places=6)
        self.assertLess(float(np.abs(out[2] - 100.0).max()), 0.05, "level between the two")
        np.testing.assert_array_equal(out[0], za)
        with unittest.mock.patch.object(railprofile, "JOIN_ON", False):
            self.assertEqual(float(railprofile.join_beds(rails, [za, zb_, zc])[2][0]), float(zc[0]))

    def test_a_siding_leaving_a_line_runs_the_difference_out_and_a_joint_is_left(self):
        a = self.line((0.0, 0.0), (600.0, 0.0))
        s = self.line((100.0, 0.0), (400.0, 40.0))
        za, zs = np.full(len(a), 100.0), np.full(len(s), 100.5)
        rails = [("a", "standard_gauge", "ground", a), ("s", "standard_gauge", "ground", s)]
        out = railprofile.join_beds(rails, [za, zs])
        self.assertAlmostEqual(float(out[1][0]), 100.0, places=6)
        self.assertAlmostEqual(float(out[1][-1]), 100.5, places=6, msg="its free end is as laid")
        self.assertGreater(float(out[1][len(s) // 2]), 100.49, "and far on, as laid")
        ahead = self.line((600.0, 0.0), (900.0, 0.0))       # a joint: this line starts where a ends
        out = railprofile.join_beds([rails[0], ("j", "standard_gauge", "ground", ahead)],
                                    [za, np.full(len(ahead), 103.0)])
        self.assertEqual(float(out[1][0]), 103.0)


if __name__ == "__main__":
    unittest.main()
