"""blockworker._NavBand: the AI graph's terrain-bump road band plans no cells past NAV_GRADED_M (Codex review of PR
#1407: with BNE_TERRAIN_BUMPS on, the band planned every cell of the graph's 2.2 km window)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import blockworker  # noqa: E402
import geodesy  # noqa: E402


class FakeGrader:
    def __init__(self):
        self.calls = []

    def road_band(self, lat, lon, reach, planned_only=False):
        self.calls.append((len(lat), planned_only))
        return np.full(len(lat), not planned_only)


class NavBandTest(unittest.TestCase):
    def test_far_points_ask_planned_cells_only(self):
        frame = geodesy.EnuFrame(50.0, 9.0)
        e = np.array([0.0, 500.0, blockworker.NAV_GRADED_M + 50.0, -2000.0])
        la, lo, _ = frame.enu_to_geodetic(e, np.zeros(4), 0.0)
        g = FakeGrader()
        got = blockworker._NavBand(g, frame, 0.0, 0.0).road_band(la, lo, 12.0)
        self.assertEqual(sorted(g.calls), [(2, False), (2, True)])
        np.testing.assert_array_equal(got, [True, True, False, False])

    def test_all_near(self):
        frame = geodesy.EnuFrame(50.0, 9.0)
        la, lo, _ = frame.enu_to_geodetic(np.array([10.0, 20.0]), np.zeros(2), 0.0)
        g = FakeGrader()
        blockworker._NavBand(g, frame, 0.0, 0.0).road_band(la, lo, 12.0)
        self.assertEqual(g.calls, [(2, False)])


if __name__ == "__main__":
    unittest.main()
