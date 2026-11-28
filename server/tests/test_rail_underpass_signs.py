"""A short rail bridge on a long Overture rail segment is kept, and a road under a bridge with a height limit (a maxheight
sign) is read as one: the limit's polyline, the clearance it asks (plan 045; II/301 under the line at Zdar nad Metuji)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grading
import overture
import railways
import roads
from test_roads import FRAME, features, rules


class RailPiecesTest(unittest.TestCase):
    def setUp(self):
        railways._GROUND_MEMO.clear()

    def test_a_short_bridge_of_a_long_segment_is_kept(self):
        # a 20 km rail segment with an 11 m bridge in its middle: 0.055 % of it, under roads.MIN_PART (0.1 %) of it
        a, b = 0.49972, 0.50028
        f = features([[(-10000, 0), (10000, 0)]], ["standard_gauge"], subtypes=["rail"],
                     flags=[(0, "is_bridge", a, b)], levels=[(0, 1.0, a, b)])
        def metres(r):
            r = np.asarray(r, np.float64)
            e, n, _ = FRAME.geodetic_to_enu(r[:, 1], r[:, 0], 0.0)
            return float(np.hypot(np.diff(e), np.diff(n)).sum())
        kinds = {kind: [metres(r) for r in rings] for kind, rings in railways._pieces_of(f, 0, [(a, b, "bridge")])}
        self.assertEqual(len(kinds["bridge"]), 1)
        self.assertAlmostEqual(kinds["bridge"][0], 11.2, delta=0.5)
        self.assertEqual(len(kinds["ground"]), 2)

    def test_a_rail_bridges_level_is_the_maps(self):
        f = features([[(0.0, 0.0), (1000.0, 0.0)]], ["standard_gauge"], subtypes=["rail"],
                     flags=[(0, "is_bridge", 0.2, 0.4), (0, "is_bridge", 0.6, 0.8)], levels=[(0, 3.0, 0.2, 0.4)])
        self.assertEqual(len(dict(railways._pieces(f, 0))["bridge"]), 2)
        self.assertEqual(railways.bridge_levels(f, 0), [3.0, 1.0], "(the viaduct's own level, 1 with none mapped)")

    def test_a_sliver_of_a_short_line_still_goes(self):
        # (a 300 m line: 1e-3 of it is 0.3 m, under MIN_PART_M: as before)
        a, b = 0.5, 0.5008
        f = features([[(-150, 0), (150, 0)]], ["standard_gauge"], subtypes=["rail"])
        kinds = dict(railways._pieces_of(f, 0, [(a, b, "bridge")]))
        self.assertEqual(kinds["bridge"], [])


class HeightLimitTest(unittest.TestCase):
    def test_overture_reads_a_maxheight_restriction(self):
        veh = [{"dimension": "height", "comparison": "greater_than", "value": 3.3, "unit": "m"}]
        self.assertAlmostEqual(overture.height_limit_m("denied", None, None, None, None, veh), 3.3)
        feet = [{"dimension": "height", "comparison": "greater_than", "value": 12, "unit": "ft"}]
        self.assertAlmostEqual(overture.height_limit_m("denied", None, None, None, None, feet), 3.6576)
        # not a limit: a heading (one-way), a time, a weight, an allowance, a value that is no clearance
        self.assertIsNone(overture.height_limit_m("denied", "forward", None, None, None, veh))
        self.assertIsNone(overture.height_limit_m("denied", None, [{"x": 1}], None, None, veh))
        self.assertIsNone(overture.height_limit_m("allowed", None, None, None, None, veh))
        self.assertIsNone(overture.height_limit_m("denied", None, None, None, None,
                                                  [{"dimension": "weight", "comparison": "greater_than", "value": 7.5,
                                                    "unit": "t"}]))
        self.assertIsNone(overture.height_limit_m("denied", None, None, None, None,
                                                  [{"dimension": "height", "comparison": "greater_than", "value": 33,
                                                    "unit": "m"}]))
        self.assertIsNone(overture.height_limit_m("denied", None, None, None, None, None))

    def test_a_limit_is_the_polyline_of_its_range(self):
        f = features([[(0, 0), (1000, 0)]], ["secondary"])
        f.rules["max_height_m"] = rules([(0, 3.3, 0.5, 0.52)], "float")
        (ring, h), = roads.height_limit_lines(f)
        self.assertAlmostEqual(h, 3.3, places=5)
        e, n, _ = FRAME.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
        self.assertAlmostEqual(float(e.min()), 500.0, delta=1.0)
        self.assertAlmostEqual(float(e.max()), 520.0, delta=1.0)
        self.assertEqual(roads.height_limit_lines(features([[(0, 0), (10, 0)]], ["secondary"])), [])

    def test_a_car_park_aisle_without_a_sign_has_a_cars_limit(self):
        # (car parks are signed 2.0-2.3 m: La Defense's spiral and the lot under the Pregerson stack held a truck's 4 m)
        f = features([[(0, 0), (100, 0)], [(0, 50), (100, 50)]], ["service", "service"],
                     subclasses=[(0, "parking_aisle"), (1, "driveway")])
        (ring, h), = roads.height_limit_lines(f)
        self.assertEqual(h, roads.CAR_PARK_HEADROOM_M)
        e, n, _ = FRAME.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
        self.assertAlmostEqual(float(e.min()), 0.0, delta=1.0)
        self.assertAlmostEqual(float(e.max()), 100.0, delta=1.0)
        self.assertLess(float(np.abs(n).max()), 1.0)          # (the aisle's; the driveway keeps a truck's)
        # (a sign over a part of it: the sign's there, a car's either side)
        f = features([[(0, 0), (100, 0)]], ["service"], subclasses=[(0, "parking_aisle")])
        f.rules["max_height_m"] = rules([(0, 2.1, 0.4, 0.6)], "float")
        got = sorted((round(float(h), 3), round(float(FRAME.geodetic_to_enu(r[:, 1], r[:, 0], 0.0)[0].min()))) for r, h in
                     roads.height_limit_lines(f))
        self.assertEqual(got, [(2.1, 40), (roads.CAR_PARK_HEADROOM_M, 0), (roads.CAR_PARK_HEADROOM_M, 60)])


if __name__ == "__main__":
    unittest.main()
