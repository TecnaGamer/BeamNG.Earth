"""The road revamp plan's WIDTH repair (roads.width_repair, earthfast RoadRules.width_rejects): a mapped width wider than
a road's lanes can be (WIDTH_PLATFORM_X x lanes x lane width, two lanes at least on a two-way road, and a shoulder each
side) is a right-of-way / platform, not the carriageway: taken out, so the lane / class width stands (MA Route 2, a
two-lane rural trunk mapped 30.5 m, GitHub #901). Real wide roads keep theirs."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import features, rules  # noqa: E402  (first: it puts the server on the path)
import roads  # noqa: E402

LINE = [(0, 0), (300, 0)]


def layer(cls, width=None, osm_width=None, lanes=None, oneway=False):
    f = features([LINE], [cls], widths=[(0, width)] if width is not None else [])
    f.attrs["osm_width"] = np.array([np.nan if osm_width is None else osm_width], np.float64)
    f.attrs["osm_lanes"] = np.array([np.nan if lanes is None else lanes], np.float64)
    if oneway:
        f.rules["oneway"] = rules([(0, 1.0)], "float")
    return f


def repaired(*a, **k):
    f = roads.width_repair(layer(*a, **k))
    return f, roads.road_specs(f)[0]


class WidthRepair(unittest.TestCase):
    def test_a_right_of_way_on_a_two_lane_trunk_is_taken_out(self):
        """MA Route 2: 30.5 m (100 ft) and 18.3 m (60 ft) mapped on the Mohawk Trail, mapped lanes or none."""
        for w, lanes in ((30.5, 2), (18.3, 2), (30.5, None), (18.3, None)):
            with self.subTest(w=w, lanes=lanes):
                f, sp = repaired("trunk", width=w, osm_width=w, lanes=lanes)
                self.assertEqual(len(f.rules["width"].feature), 0)
                self.assertTrue(np.isnan(f.attrs["osm_width"][0]))
                plain = roads.road_specs(layer("trunk", lanes=lanes))[0]
                self.assertAlmostEqual(sp.width, plain.width)
                self.assertLess(sp.width, 12.0)

    def test_real_widths_stand(self):
        """A two-lane trunk with wide paved shoulders, a two-lane town street with parking, a 4-lane primary, a one-way
        6-lane motorway carriageway, a two-way road mapped one lane (unstriped) at 6.7 m: the mapped width kept."""
        for cls, w, lanes, ow in (("trunk", 13.5, 2, False), ("tertiary", 14.5, 2, False), ("primary", 20.0, 4, False),
                                  ("motorway", 27.0, 6, True), ("residential", 6.7, 1, False),
                                  ("motorway", 16.0, None, True)):
            with self.subTest(cls=cls, w=w):
                f, sp = repaired(cls, width=w, osm_width=w, lanes=lanes, oneway=ow)
                self.assertEqual(len(f.rules["width"].feature), 1)
                self.assertEqual(float(f.attrs["osm_width"][0]), w)

    def test_no_lane_count_no_test(self):
        """A street with no lane count (none mapped, not a highway or a link): its mapped width stands, however wide."""
        f, sp = repaired("residential", width=30.0, osm_width=30.0)
        self.assertEqual(len(f.rules["width"].feature), 1)
        self.assertAlmostEqual(sp.width, 30.0, places=4)

    def test_narrow_widths_stand(self):
        """The plan's lower side is not applied: Avenue de Verdun, 2 lanes mapped, 3.6 m, one lane on the aerial."""
        f, sp = repaired("primary", width=3.6, osm_width=3.6, lanes=2, oneway=True)
        self.assertEqual(len(f.rules["width"].feature), 1)
        self.assertAlmostEqual(sp.width, 3.6, places=4)

    def test_only_the_bad_source_goes(self):
        """Overture's rule a right-of-way, OpenStreetMap's a carriageway: OpenStreetMap's stands."""
        f, sp = repaired("trunk", width=30.5, osm_width=10.0, lanes=2)
        self.assertEqual(len(f.rules["width"].feature), 0)
        self.assertAlmostEqual(sp.width, 10.0, places=4)

    def test_not_a_road(self):
        f = layer("trunk", width=30.5, lanes=2)
        f.attrs["subtype"] = np.array(["rail"])
        self.assertEqual(len(roads.width_repair(f).rules["width"].feature), 1)


if __name__ == "__main__":
    unittest.main()
