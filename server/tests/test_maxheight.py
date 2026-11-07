import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import overture, roads  # noqa: E402


class MaxheightTest(unittest.TestCase):
    def test_units(self):
        self.assertAlmostEqual(overture._length_m(4.5, "m"), 4.5)
        self.assertAlmostEqual(overture._length_m(14, "ft"), 4.2672)
        self.assertIsNone(overture._length_m(None, "m"))
        self.assertIsNone(overture._length_m(3, "furlong"))

    def test_clear(self):
        d = roads.DECK_FULL_DEPTH
        self.assertAlmostEqual(roads.deck_road_clear(), roads.MIN_BRIDGE_HEADROOM_M + roads.BRIDGE_HEADROOM_MARGIN_M + d)
        self.assertAlmostEqual(roads.deck_road_clear(1.0, 4.65), 5.65)

    def test_point_line(self):
        import numpy as np
        ln = np.array([[0.0, 0.0], [10.0, 0.0]])
        self.assertAlmostEqual(roads._point_line_dist(np.array([5.0, 3.0]), ln), 3.0)
        self.assertAlmostEqual(roads._point_line_dist(np.array([13.0, 4.0]), ln), 5.0)


if __name__ == "__main__":
    unittest.main()
