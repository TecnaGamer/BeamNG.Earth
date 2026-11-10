"""roads.strip_runs: a dirt or gravel strip is not drawn over a paved carriageway it crosses (GitHub #771)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roads  # noqa: E402


def line(x0, y0, x1, y1, n=11):
    return np.c_[np.linspace(x0, x1, n), np.linspace(y0, y1, n), np.zeros(n)]


class StripRunsTest(unittest.TestCase):
    def setUp(self):
        self.paved = roads._built_segments([("p", roads.Spec(8.0, "paved", False, "tertiary"), line(0, -40, 0, 40, 3),
                                             0.0)])

    def test_a_track_across_an_asphalt_road_stops_at_its_edges(self):
        runs = roads.strip_runs(line(-50, 0, 50, 0), self.paved)
        self.assertEqual(len(runs), 2)
        (a, ca, sa, la), (b, cb, sb, lb) = runs
        self.assertEqual((ca, cb), ((False, True), (True, False)), "cut at the asphalt only: the far ends as they were")
        half = 4.0 + roads.STRIP_PAVED_MARGIN_M
        self.assertLessEqual(float(a[-1, 0]), -half + 1e-9)
        self.assertGreaterEqual(float(b[0, 0]), half - 1e-9)
        self.assertGreater(float(a[-1, 0]), -half - roads.STRIP_PAVED_STEP_M - 1e-9, "within a step of the edge")
        self.assertEqual((sa, la), (0.0, 100.0))
        self.assertAlmostEqual(sb, float(b[0, 0]) + 50.0, places=6, msg="the ruts' distance runs on past the cut")

    def test_no_paved_road_or_none_near_leaves_the_track_whole(self):
        whole = line(-50, 60, 50, 60)
        for paved in (None, self.paved):
            runs = roads.strip_runs(whole, paved)
            self.assertEqual(len(runs), 1)
            self.assertIs(runs[0][0], whole)
            self.assertEqual(runs[0][1:], ((False, False), 0.0, None))

    def test_dirt_roads_are_not_carriageways(self):
        self.assertIsNone(roads._built_segments([("d", roads.Spec(4.0, "dirt", False, "track"), line(0, -9, 0, 9), 0.0)]))


if __name__ == "__main__":
    unittest.main()
