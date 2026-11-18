"""The default widths of roads the map gives no width (roads.WIDTH_TABLE, measured against aerials with
tools/roadscore.py), no parking lanes on top of a mapped lane count, and narrow winding rural roads (WINDING_*)."""
import math
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import features  # noqa: E402  (first: it puts the server on the path)
import osmtags  # noqa: E402
import roads  # noqa: E402


def town(lat, lon):
    return np.ones(np.shape(lat), bool)


def table(region, cls):
    return roads.WIDTH_TABLE[region].get(cls)


def winding_line(length=1200.0, amp=25.0, wave=120.0, step=5.0):
    """A rural road swinging amp m either side every wave m (a touge's S bends)."""
    x = np.arange(0.0, length + step, step)
    return [(float(e), float(amp * math.sin(2 * math.pi * e / wave))) for e in x]


class WidthTable(unittest.TestCase):
    def test_town_streets_take_the_regions_measured_widths(self):
        """The test frame is Baltimore: the US row."""
        for cls in ("residential", "tertiary", "unclassified"):
            row = table("US", cls)
            if not row:
                continue
            with self.subTest(cls):
                if row[0] is not None:
                    f = features([[(0, 0), (200, 0)]], [cls])
                    self.assertAlmostEqual(roads.road_specs(f, town)[0].width, row[0], places=3)
                if row[1] is not None:
                    f = features([[(0, 0), (200, 0)]], [cls])
                    f.rules["oneway"] = roads_rules([(0, 1.0)])
                    self.assertAlmostEqual(roads.road_specs(f, town)[0].width, row[1], places=3)
                if row[2] is not None:
                    f = features([[(0, 0), (200, 0)]], [cls])
                    self.assertAlmostEqual(roads.road_specs(f, None)[0].width, row[2], places=3)

    def test_a_mapped_width_stands(self):
        f = features([[(0, 0), (200, 0)]], ["residential"], widths=[(0, 12.5)])
        self.assertAlmostEqual(roads.road_specs(f, town)[0].width, 12.5)

    def test_off_switch_gives_the_class_formula(self):
        f = features([[(0, 0), (200, 0)]], ["residential"])
        with mock.patch.object(roads, "WIDTH_MODEL_ON", False):
            w = roads.road_specs(f, town)[0].width
        self.assertAlmostEqual(w, roads.STYLE["residential"][0] + 2 * roads.PARKING_M)


class MappedLanes(unittest.TestCase):
    def test_no_parking_lanes_on_top_of_a_mapped_lane_count(self):
        """Muenster: a 4-lane secondary in town drew 4 x 3.3 m + two parking lanes, 17.8 m, against ~14 m."""
        f = features([[(0, 0), (300, 0)]], ["secondary"], ids=["a"])
        f.attrs["src_record"] = np.array(["w1@1"])
        osmtags.join(f, {1: {"lanes": 4}})
        sp = roads.road_specs(f, town)[0]
        self.assertEqual(sp.lanes, 4)
        self.assertAlmostEqual(sp.width, 4 * roads.LANE_W["secondary"])

    def test_a_two_lane_street_keeps_its_streets_default_at_least(self):
        f = features([[(0, 0), (300, 0)]], ["residential"], ids=["a"])
        f.attrs["src_record"] = np.array(["w1@1"])
        osmtags.join(f, {1: {"lanes": 2}})
        sp = roads.road_specs(f, town)[0]
        row = table("US", "residential")
        least = row[0] if row else roads.STYLE["residential"][0] + 2 * roads.PARKING_M
        self.assertAlmostEqual(sp.width, max(2 * roads.LANE_W_DEFAULT, least))


class Winding(unittest.TestCase):
    def test_a_winding_rural_road_is_narrow_a_straight_one_not(self):
        for cls in ("primary", "tertiary"):
            row = table("US", cls)
            cap = row[4] if row else None
            if cap is None:
                continue
            with self.subTest(cls):
                f = features([winding_line(), [(0, 400), (1200, 400)]], [cls, cls])
                sp = roads.road_specs(f, None)
                self.assertAlmostEqual(sp[0].width, cap)
                self.assertGreater(sp[1].width, cap)
                with mock.patch.object(roads, "WINDING_ON", False):
                    f = features([winding_line()], [cls])
                    self.assertGreater(roads.road_specs(f, None)[0].width, cap)

    def test_gentle_hills_road_stays(self):
        row = table("US", "primary")
        if not row or row[4] is None:
            self.skipTest("no winding width")
        f = features([winding_line(amp=8.0, wave=600.0)], ["primary"])
        self.assertGreater(roads.road_specs(f, None)[0].width, row[4])

    def test_a_town_street_is_never_a_winding_road(self):
        f = features([winding_line()], ["tertiary"])
        row = table("US", "tertiary")
        if not row:
            self.skipTest("no tertiary row")
        self.assertAlmostEqual(roads.road_specs(f, town)[0].width, row[0])


def roads_rules(entries):
    from test_roads import rules
    return rules([(i, v, 0.0, 1.0) for i, v in entries], "float")


if __name__ == "__main__":
    unittest.main()
