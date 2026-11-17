"""The road revamp plan's WIDTH repair (roads.width_rejected, earthfast RoadRules.width_rejects): a mapped width wider
than a road's lanes can be (WIDTH_PLATFORM_X x lanes x lane width, two lanes at least on a two-way road, and a shoulder
each side) is a right-of-way / platform, not the carriageway: every reader of a mapped width leaves it out, so the lane /
class width stands (MA Route 2, a two-lane rural trunk mapped 30.5 m, GitHub #901). The lanes are the most any lane
data gives the road anywhere along it, from its home cell's copy (every copy decides alike). Real wide roads keep
theirs."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import features, rules  # noqa: E402  (first: it puts the server on the path)
import railtags  # noqa: E402
import roads  # noqa: E402

LINE = [(0, 0), (300, 0)]


def layer(cls, width=None, osm_width=None, lanes=None, oneway=False, parts=None, line=LINE):
    f = features([line], [cls], widths=[(0, width)] if width is not None else [])
    f.attrs["osm_width"] = np.array([np.nan if osm_width is None else osm_width], np.float64)
    f.attrs["osm_lanes"] = np.array([np.nan if lanes is None else lanes], np.float64)
    if parts is not None:
        col = np.empty(1, object)
        col[0] = parts
        f.attrs[railtags.LANE_PARTS] = col
    if oneway:
        f.rules["oneway"] = rules([(0, 1.0)], "float")
    return f


def kept(f):
    """(the width rule row kept, OSM's width kept, the spec's width)."""
    return (roads.mapped_rule_width(f, 0) is not None, roads.mapped_osm_width(f, 0) is not None,
            roads.road_specs(f)[0].width)


class WidthRepair(unittest.TestCase):
    def test_a_right_of_way_on_a_two_lane_trunk_is_left_out(self):
        """MA Route 2: 30.5 m (100 ft) and 18.3 m (60 ft) mapped on the Mohawk Trail, mapped lanes or none."""
        for w, lanes in ((30.5, 2), (18.3, 2), (30.5, None), (18.3, None)):
            with self.subTest(w=w, lanes=lanes):
                f = layer("trunk", width=w, osm_width=w, lanes=lanes)
                rule, osm, width = kept(f)
                self.assertFalse(rule)
                self.assertFalse(osm)
                self.assertAlmostEqual(width, roads.road_specs(layer("trunk", lanes=lanes))[0].width)
                self.assertLess(width, 12.0)
                self.assertEqual(len(f.rules["width"].feature), 1, "the layer itself untouched")

    def test_real_widths_stand(self):
        """A two-lane trunk with wide paved shoulders, a two-lane town street with parking, a 4-lane primary, a one-way
        6-lane motorway carriageway, a two-way road mapped one lane (unstriped) at 6.7 m: the mapped width kept."""
        for cls, w, lanes, ow in (("trunk", 13.5, 2, False), ("tertiary", 14.5, 2, False), ("primary", 20.0, 4, False),
                                  ("motorway", 27.0, 6, True), ("residential", 6.7, 1, False),
                                  ("motorway", 16.0, None, True), ("motorway", 26.0, None, False),
                                  ("motorway", 24.0, None, True), ("primary", 25.0, None, False)):
            with self.subTest(cls=cls, w=w):
                rule, osm, _ = kept(layer(cls, width=w, osm_width=w, lanes=lanes, oneway=ow))
                self.assertTrue(rule)
                self.assertTrue(osm)

    def test_no_lane_count_no_test(self):
        """A street with no lane count (none mapped, not a highway or a link): its mapped width stands, however wide."""
        rule, osm, width = kept(layer("residential", width=30.0, osm_width=30.0))
        self.assertTrue(rule and osm)
        self.assertAlmostEqual(width, 30.0, places=4)

    def test_narrow_widths_stand(self):
        """The plan's lower side is not applied: Avenue de Verdun, 2 lanes mapped, 3.6 m, one lane on the aerial."""
        _, _, width = kept(layer("primary", width=3.6, osm_width=3.6, lanes=2, oneway=True))
        self.assertAlmostEqual(width, 3.6, places=4)

    def test_only_the_bad_source_goes(self):
        """Overture's rule a right-of-way, OpenStreetMap's a carriageway: OpenStreetMap's stands."""
        rule, osm, width = kept(layer("trunk", width=30.5, osm_width=10.0, lanes=2))
        self.assertFalse(rule)
        self.assertTrue(osm)
        self.assertAlmostEqual(width, 10.0, places=4)

    def test_not_a_road(self):
        f = layer("trunk", width=30.5, lanes=2)
        f.attrs["subtype"] = np.array(["rail"])
        self.assertFalse(roads.width_rejected(f)[0].any())

    def test_a_merged_highway_keeps_the_width_its_widest_section_has(self):
        """A one-way trunk merged from a 2-lane and a 4-lane way (its lane parts; the feature's own count the 2-lane
        way's): 22 m is a 4-lane section's width, kept (Codex: deleted against the feature's 2 lanes)."""
        parts = [(0.0, 0.5, 1.0, {"lanes": "2"}), (0.5, 1.0, 1.0, {"lanes": "4"})]
        rule, osm, _ = kept(layer("trunk", width=22.0, osm_width=22.0, lanes=2, oneway=True, parts=parts))
        self.assertTrue(rule and osm)
        rule, osm, _ = kept(layer("trunk", width=25.0, osm_width=25.0, lanes=2,
                                  parts=[(0.0, 0.5, 1.0, {"lanes": "2"}), (0.5, 1.0, 1.0, {"lanes": "4"})]))
        self.assertTrue(rule and osm, "a two-way road: its 4-lane part's lanes count")
        rule, osm, _ = kept(layer("trunk", width=25.0, osm_width=25.0, lanes=2,
                                  parts=[(0.0, 1.0, 1.0, {"lanes": "2"})]))
        self.assertFalse(rule or osm, "two lanes all along: a right-of-way")

    def test_a_one_way_carriageway_keeps_its_width(self):
        """A divided road's carriageway is mapped as wide as its roadway or boulevard, and the pairing caps it to the
        gap to its twin already: Boston's Storrow Drive (21.3 m, 2 lanes) and Commonwealth Avenue (59.1 m) MassGIS
        widths are kept (judged against their own lanes the decks round them got steeper, drivability)."""
        for cls, w, lanes in (("motorway", 21.3, 2), ("primary", 59.1, 2), ("trunk", 22.0, 2)):
            with self.subTest(cls=cls, w=w):
                rule, osm, _ = kept(layer(cls, width=w, osm_width=w, lanes=lanes, oneway=True))
                self.assertTrue(rule and osm)

    def test_lanes_forward_and_backward_count(self):
        parts = [(0.0, 1.0, 1.0, {"lanes:forward": "3", "lanes:backward": "3"})]
        rule, _, _ = kept(layer("trunk", width=30.0, parts=parts))
        self.assertTrue(rule, "six lanes: 30 m is a carriageway")

    def test_every_copy_decides_as_its_home_copy(self):
        """Two cells' copies of one segment, the home copy's way tagged 4 lanes, the other copy's cell without that
        way's tags (2 lanes by the trunk default): both keep a 25 m width; and the reverse: both take it out."""
        line = [(0, 0), (1500, 0)]
        for home_lanes, other_lanes, keep in ((4, None, True), (None, 4, False), (2, 4, False), (4, 2, True)):
            with self.subTest(home=home_lanes, other=other_lanes):
                home = layer("trunk", width=25.0, lanes=home_lanes, line=line)
                other = layer("trunk", width=25.0, lanes=other_lanes, line=line)
                home_key = str(roads._home_cells(home)[0])
                home.__dict__["_bnge_cell"] = (home_key, "roads", "{}")
                other.__dict__["_bnge_cell"] = ("14/0/0", "roads", "{}")
                other.__dict__["_bnge_neighbours"] = lambda h=home: [h]
                home.__dict__["_bnge_neighbours"] = lambda o=other: [o]
                self.assertEqual(kept(home)[0], keep)
                self.assertEqual(kept(other)[0], keep)

    def test_the_widest_section_keeps_the_width_after_the_lane_section_split(self):
        """mapdata splits a merged highway into its lane sections (lanesections.split) before any width is read: each
        section's osm_lanes is its own count and its lane parts are cleared. The 2-lane section kept the 22 m only
        while the road was whole (Codex round 2): every section carries the road's most lanes (LANE_MAX)."""
        import lanesections
        parts = [(0.0, 0.5, 11, {"lanes": "2"}), (0.5, 1.0, 12, {"lanes": "4"})]
        f = layer("trunk", width=22.0, osm_width=22.0, lanes=2, oneway=True, parts=parts, line=[(0, 0), (600, 0)])
        g = lanesections.split(f)
        self.assertGreater(len(g), len(f), "split into its sections")
        self.assertEqual(sorted(int(n) for n in g.attrs["osm_lanes"]), [2, 4])
        self.assertEqual(g.attrs[lanesections.LANE_MAX].tolist(), [4] * len(g))
        for i in range(len(g)):
            self.assertIsNotNone(roads.mapped_rule_width(g, i), i)
            self.assertIsNotNone(roads.mapped_osm_width(g, i), i)
            self.assertAlmostEqual(roads.road_specs(g)[i].width, 22.0, places=4)
        # (the sections' evidence as the road's: a two-way road's width judged on the split layer as on the whole)
        f2 = layer("trunk", width=25.0, lanes=2, line=[(0, 0), (600, 0)],
                   parts=[(0.0, 0.5, 11, {"lanes": "2"}), (0.5, 1.0, 12, {"lanes": "4"})])
        self.assertIsNotNone(roads.mapped_rule_width(lanesections.split(f2), 0))

    def test_a_layer_without_the_home_cell_at_hand_decides_on_its_own(self):
        f = layer("trunk", width=25.0, lanes=4)
        f.__dict__["_bnge_cell"] = ("14/0/0", "roads", "{}")
        f.__dict__["_bnge_neighbours"] = lambda: []
        self.assertTrue(kept(f)[0])


if __name__ == "__main__":
    unittest.main()
