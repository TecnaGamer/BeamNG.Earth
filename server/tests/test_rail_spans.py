"""A track over a road on the ground is drawn as a span past its carriageway and the bed's feet, wherever the map's
bridge ends (railways._spans_over_roads), its beds as made. Vienna's primary road under the five tracks by the A23:
the mapped bridges end 2.8 m from its centre line on a 4.3 m half width, the ground tracks' beds stood on the
carriageway and their walls rose 7 m out of it. (That road is the map's 126 m tunnel there, under the tracks and the
A23: with the map's own ranges no ground track stands on it, #118; the spans' beds are checked here.)"""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import railways  # noqa: E402

REPLAY = os.path.join(HERE, "fixtures", "drive_vienna_a23_1083.npz")
AT = (48.229468, 16.453193)     # (where they cross it)


@unittest.skipUnless(os.path.exists(REPLAY), "the Vienna A23 replay")
class TrackOverRoadSpansTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import interchanges
        rep = interchanges.load("vienna_a23_1083", prefix="drive")
        cls.frame, cls.cells = rep.frame, list(rep.cells.values())
        e, n, _ = cls.frame.geodetic_to_enu(np.array([AT[0]]), np.array([AT[1]]), 0.0)
        cls.at = np.array([float(e[0]), float(n[0])])
        box = (cls.at[0] - 150.0, cls.at[1] - 150.0, cls.at[0] + 150.0, cls.at[1] + 150.0)
        rails = railways.parts(cls.frame, cls.cells, box)
        cls.mapped = rails
        prof = [(np.full(len(xy), 1.0 + n), np.zeros(len(xy))) for n, (*_r, xy) in enumerate(rails)]
        ways = railways._roads_under_bridges(cls.frame, cls.cells, box, railways.REACH_M, rails)
        cls.parts, cls.prof = railways._spans_over_roads(rails, prof, ways)

    def test_the_beds_are_the_parts_own(self):
        # (drawn only: each piece keeps its part's beds, point for point; the ground runs off the road stay ground)
        self.assertEqual(len(self.parts), len(self.prof))
        for (_f, _c, _k, xy), (zb, zg) in zip(self.parts, self.prof):
            self.assertEqual(len(xy), len(zb))
            self.assertEqual(len(xy), len(zg))
        self.assertEqual(sum(len(xy) for *_r, xy in self.parts) - sum(len(xy) for *_r, xy in self.mapped),
                         sum(1 for (_f, _c, k, _x) in self.parts if k == "bridge")
                         - sum(1 for (_f, _c, k, _x) in self.mapped if k == "bridge"),
                         "each new span shares its end point with the ground run it was cut from")
        self.assertTrue(any(k == "ground" for (_f, _c, k, _x) in self.parts))


if __name__ == "__main__":
    unittest.main()
