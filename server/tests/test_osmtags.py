"""osmtags.py: OpenStreetMap's road tags (lanes, width, speed limit...) joined to Overture's road segments by way id,
and the roads using them (roads.road_specs / carriageway, navgraph lanes)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import navgraph  # noqa: E402
import osmtags  # noqa: E402
import roads  # noqa: E402
from test_roads import features  # noqa: E402


def tagged(lines, classes, records, tags):
    f = features(lines, classes)
    f.attrs["src_record"] = np.array(records)
    return osmtags.join(f, tags)


class JoinTest(unittest.TestCase):
    def test_way_id(self):
        self.assertEqual(osmtags.way_id("w1360453999@2"), 1360453999)
        self.assertIsNone(osmtags.way_id("n12@1"))
        self.assertIsNone(osmtags.way_id(""))
        self.assertIsNone(osmtags.way_id(None))

    def test_join_by_way_id(self):
        f = tagged([[(0, 0), (100, 0)], [(0, 20), (100, 20)], [(0, 40), (100, 40)]], ["primary"] * 3,
                   ["w5@3", "w6@1", "t-tomtom"], {5: {"lanes": 4, "width": 13.5, "sidewalk": "both"}, 6: {}})
        self.assertEqual(f.attrs["osm_lanes"][0], 4)
        self.assertTrue(np.isnan(f.attrs["osm_lanes"][1]) and np.isnan(f.attrs["osm_lanes"][2]))
        self.assertEqual(str(f.attrs["osm_sidewalk"][0]), "both")
        self.assertEqual(str(f.attrs["osm_sidewalk"][2]), "")

    def test_relation_segment_takes_the_same_named_way_around_it(self):
        """Samara's Novo-Sadovaya: Overture's segments from a relation ("r3851280@53") had no way id, so no lane count,
        and were drawn 10 m wide instead of 6 lanes. Such a segment takes the tags of the way of its name whose bbox
        (grown by NAME_MATCH_M) holds its middle, not that way's oneway (one carriageway of the road, maybe)."""
        from test_roads import LAT, LON, M_LAT, M_LON, features
        f = features([[(0, 0), (100, 0)], [(0, 500), (100, 500)], [(0, 10), (100, 10)]], ["primary"] * 3)
        f.attrs["src_record"] = np.array(["r7@1", "r7@1", "r8@1"])
        f.attrs["name"] = np.array(["Main", "Main", "Side"])
        bbox = {"bx0": LON - 10 / M_LON, "by0": LAT - 5 / M_LAT, "bx1": LON + 110 / M_LON, "by1": LAT + 5 / M_LAT}
        osmtags.join(f, {9: dict(bbox, name="Main", lanes=6, osm_oneway="yes", sidewalk="separate")})
        self.assertEqual(f.attrs["osm_lanes"][0], 6)
        self.assertEqual(str(f.attrs["osm_sidewalk"][0]), "separate")
        self.assertEqual(str(f.attrs["osm_oneway"][0]), "", "oneway is not inherited")
        self.assertTrue(np.isnan(f.attrs["osm_lanes"][1]), "the same name 500 m away")
        self.assertTrue(np.isnan(f.attrs["osm_lanes"][2]), "another name inside the bbox")
        self.assertGreater(roads.road_specs(f)[0].width, roads.road_specs(f)[2].width)


class SpecTest(unittest.TestCase):
    def test_lanes_set_the_width_and_the_markings(self):
        f = tagged([[(0, 0), (200, 0)], [(0, 30), (200, 30)]], ["secondary", "secondary"], ["w1@1", "w2@1"],
                   {1: {"lanes": 4}})
        a, b = roads.road_specs(f)
        self.assertEqual(a.lanes, 4)
        self.assertIsNone(b.lanes)
        self.assertGreaterEqual(a.width, 4 * roads.LANE_W["secondary"], "four lanes wide")
        self.assertGreater(a.width, b.width, "wider than the class's guess")

    def test_osm_width_is_the_width(self):
        f = tagged([[(0, 0), (200, 0)]], ["residential"], ["w1@1"], {1: {"width": 9.0, "lanes": 2}})
        self.assertAlmostEqual(roads.road_specs(f)[0].width, 9.0)

    def test_nonsense_is_not_believed(self):
        f = tagged([[(0, 0), (200, 0)]], ["residential"], ["w1@1"], {1: {"width": 400.0, "lanes": 40}})
        sp = roads.road_specs(f)[0]
        self.assertIsNone(sp.lanes)
        self.assertLess(sp.width, 40.0)

    def test_maxspeed_fills_a_missing_limit(self):
        f = tagged([[(0, 0), (200, 0)]], ["residential"], ["w1@1"], {1: {"maxspeed": 40.2336}})
        self.assertAlmostEqual(roads.road_specs(f)[0].kmh, 40.2336)

    def test_navgraph_takes_the_lane_count(self):
        self.assertEqual(navgraph._lanes(7.0, False, 4), (2, 2))
        self.assertEqual(navgraph._lanes(7.0, True, 3), (3, 0))
        self.assertEqual(navgraph._lanes(7.0, False), navgraph._lanes(7.0, False, None))


class RacewayTest(unittest.TestCase):
    """GitHub #143: OpenStreetMap's raceways appended to Overture's roads (which have none), drawn as 12 m paved
    roads unless mapped wider, never graded flat (an oval's banking is the terrain's)."""

    def ways(self):
        return [{"id": 352070316, "name": "Speedway", "surface": "asphalt", "width": "18 m",
                 "coords": np.array([[-81.07, 29.18], [-81.06, 29.18], [-81.06, 29.19]])},
                {"id": 7, "coords": np.array([[-81.08, 29.18], [-81.075, 29.18]])},
                {"id": 8, "bridge": "yes", "coords": np.array([[-81.08, 29.185], [-81.075, 29.185]])}]

    def test_features_and_concat(self):
        import mapgeom
        rw = osmtags.raceway_features(self.ways())
        self.assertEqual(list(rw.ids), ["osm-raceway-000000000007", "osm-raceway-000000000008",
                                        "osm-raceway-000352070316"])
        base = features([[(0, 0), (100, 0)]], ["residential"])
        both = mapgeom.concat(base, rw)
        self.assertEqual(len(both), 4)
        self.assertEqual(list(both.ids), sorted(both.ids), "ids ascending")
        self.assertEqual(str(both.attrs["class"][3]), "raceway")
        self.assertEqual(str(both.attrs["subtype"][0]), "road", "the base keeps its columns")
        sp = roads.road_specs(both)
        self.assertEqual(sp[3].kind, "paved")
        self.assertAlmostEqual(sp[3].width, 18.0, msg="the mapped width")
        self.assertGreaterEqual(sp[1].width, roads.STYLE["raceway"][0])
        self.assertTrue(roads.bridge_specs(both)[2] is not None, "a raceway bridge")
        self.assertEqual(len(both.parts(3)[0][0]), 3)

    def test_not_graded(self):
        import grading
        self.assertIn("raceway", grading.UNGRADED)

    def test_wkb_linestring(self):
        import struct
        wkb = struct.pack("<BII", 1, 2, 2) + struct.pack("<4d", 1.0, 2.0, 3.0, 4.0)
        np.testing.assert_allclose(osmtags._linestring(wkb), [[1, 2], [3, 4]])
        self.assertIsNone(osmtags._linestring(struct.pack("<BI", 1, 3) + b"x" * 20), "a polygon is not a line")


if __name__ == "__main__":
    unittest.main()
