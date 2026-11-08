"""RDW garages: a polygon keeps its capacity and its charge points; a point outside has none."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import rdwparking as rdw  # noqa: E402

RING = [(4.880, 52.356), (4.882, 52.356), (4.882, 52.358), (4.880, 52.358), (4.880, 52.356)]


class ParkingTest(unittest.TestCase):
    def test_a_polygon_keeps_capacity_and_chargers(self):
        row = rdw.row_of("9251", "453",
                         "POLYGON ((4.880 52.356, 4.882 52.356, 4.882 52.358, 4.880 52.358, 4.880 52.356))")
        row["counts"] = rdw.counts_of({"capacity": "335", "chargingpointcapacity": "12"})
        info = rdw.lookup(52.357, 4.881, rows=[row])
        self.assertEqual(info["capacity"], 335)
        self.assertEqual(info["chargers"], 12)
        self.assertGreaterEqual(rdw.floors_for(335, 1200), 1)

    def test_a_point_outside_the_polygon_has_none(self):
        row = rdw.row_of("9251", "453",
                         "POLYGON ((4.880 52.356, 4.882 52.356, 4.882 52.358, 4.880 52.358, 4.880 52.356))")
        row["counts"] = {"capacity": 10, "chargers": 0}
        self.assertIsNone(rdw.lookup(52.370, 4.900, rows=[row]))
        self.assertIsNone(rdw.lookup(50.938, 6.960, rows=[row]))

    def test_an_ended_polygon_is_dropped(self):
        self.assertIsNone(rdw.row_of("1", "1", "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))", "2019-01-01T00:00:00.000"))
