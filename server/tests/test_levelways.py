"""levelways.py: a street mapped as a pedestrian way over a vehicle level crossing becomes a road (plan 035)."""
import math
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import levelways  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def node(x, y, kind="level_crossing"):
    lon = FRAME.lon0 + x / (111320.0 * math.cos(math.radians(FRAME.lat0)))
    lat = FRAME.lat0 + y / 111320.0
    return (lon, lat, {"railway": kind})


class LevelWaysTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(levelways, "ON", True)
        p.start()
        self.addCleanup(p.stop)

    def kladska(self, kind="level_crossing", walk_len=50.0):
        # a residential street ends 14 m short of the tracks (y = 100); a short pedestrian piece links it to the long
        # pedestrian way over the tracks, which ends on a primary road
        f = features([[(0.0, 40.0), (0.0, 80.0)], [(0.0, 80.0), (0.0, 94.0)], [(0.0, 94.0), (0.0, 94.0 + walk_len)],
                      [(-60.0, 94.0 + walk_len), (60.0, 94.0 + walk_len)], [(-100.0, 100.0), (100.0, 100.0)]],
                     ["residential", "pedestrian", "pedestrian", "primary", "standard_gauge"],
                     subtypes=["road", "road", "road", "road", "rail"])
        f = f.localize(FRAME)
        f.__dict__["_rail_nodes"] = [node(0.0, 100.0, kind)]
        return levelways.promote(f, FRAME)

    def test_a_pedestrian_way_over_a_vehicle_level_crossing_takes_the_street_s_class(self):
        cls = list(self.kladska().attrs["class"])
        self.assertEqual(cls[2], "residential", "the street that goes on over the tracks, not the primary at its end")
        self.assertEqual(cls[1], "residential", "and the short piece linking it to the street")
        self.assertEqual(cls[3], "primary")

    def test_a_pedestrian_crossing_node_changes_nothing(self):
        cls = list(self.kladska("crossing").attrs["class"])
        self.assertEqual(cls[1:3], ["pedestrian", "pedestrian"])

    def test_a_long_promenade_stays_a_walk(self):
        cls = list(self.kladska(walk_len=400.0).attrs["class"])
        self.assertEqual(cls[2], "pedestrian")

    def test_off(self):
        with mock.patch.object(levelways, "ON", False):
            self.assertEqual(list(self.kladska().attrs["class"])[2], "pedestrian")


if __name__ == "__main__":
    unittest.main()
