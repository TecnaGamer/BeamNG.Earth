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
    def test_decoded_row_group_rows(self):
        # a row group's table (OsmTags._decode's layout): texts as UTF-8 blobs, numbers NaN where missing
        names = list(osmtags.COLUMNS)
        t = {"id": np.array([7, 9], np.int64), "race": np.array([False, True]), "frn": np.array([100, 101], np.int64)}
        for c in names:
            if c in osmtags.TEXT_COLUMNS:
                vals = ["Straße", None] if c == "name" else [None, None]
                enc = [b"" if v is None else v.encode("utf-8") for v in vals]
                t[c + "_ok"] = np.array([v is not None for v in vals])
                t[c + "_off"] = np.concatenate([[0], np.cumsum([len(b) for b in enc])])
                t[c] = np.frombuffer(b"".join(enc), np.uint8)
            else:
                t[c] = np.array([2.0 if c == "lanes" else np.nan, np.nan])
        rows = osmtags._rows(t, [0, 1])
        self.assertEqual(rows[0][:3], (7, False, 100))
        got = dict(zip(names, rows[0][3:]))
        self.assertEqual(got["name"], "Straße")
        self.assertEqual(got["lanes"], 2)
        self.assertIsInstance(got["lanes"], int)
        self.assertIsNone(got["width"])
        self.assertEqual(rows[1][:3], (9, True, 101))
        self.assertTrue(all(v is None for v in rows[1][3:]))

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

    def test_a_road_not_built_yet_is_not_drawn(self):
        """GitHub #184: a trunk road under construction (OSM highway=construction, which Overture calls "unknown") was
        drawn as a finished road; highway=proposed likewise. Any other highway value is drawn as before."""
        f = tagged([[(0, 0), (200, 0)], [(0, 30), (200, 30)], [(0, 60), (200, 60)]], ["unknown"] * 3,
                   ["w1@1", "w2@1", "w3@1"],
                   {1: {"osm_highway": "construction"}, 2: {"osm_highway": "proposed"}, 3: {"lanes": 2}})
        specs = roads.road_specs(f)
        self.assertIsNone(specs[0])
        self.assertIsNone(specs[1])
        self.assertIsNotNone(specs[2])
        self.assertTrue(roads.unbuilt(f, 0) and not roads.unbuilt(f, 2))
        # a cell cached before the column existed: drawn as before
        g = features([[(0, 0), (200, 0)]], ["unknown"])
        self.assertIsNotNone(roads.road_specs(g)[0])

    def test_overture_s_under_construction_flag_over_most_of_it_is_not_built(self):
        """The Gotthard road tunnel's second bore (highway=construction until ~2029) came with the open tunnel's tags
        (Layercake has no construction ways) and Overture's is_under_construction flag: it was drawn beside the real
        bore, its portal in the A2's lanes. Flagged over a short stretch (roadworks): drawn."""
        f = features([[(0, 0), (200, 0)], [(0, 30), (200, 30)], [(0, 60), (200, 60)]], ["trunk"] * 3,
                     flags=[(0, "is_under_construction"), (1, "is_under_construction", 0.1, 0.2), (2, "is_tunnel")])
        self.assertTrue(roads.unbuilt(f, 0))
        self.assertFalse(roads.unbuilt(f, 1))
        self.assertFalse(roads.unbuilt(f, 2))
        specs = roads.road_specs(f)
        self.assertIsNone(specs[0])
        self.assertIsNotNone(specs[1])

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



class OsmAheadOfClassTest(unittest.TestCase):
    """Lane counts and widths follow OpenStreetMap ahead of the class's defaults (the user, 2026-10-27: "it leans on
    the road's class definition more than the road's actual metadata")."""

    def box(self, x0, x1, y=0.0):
        from test_roads import LAT, LON, M_LAT, M_LON
        return {"bx0": LON + x0 / M_LON, "by0": LAT + (y - 5) / M_LAT, "bx1": LON + x1 / M_LON,
                "by1": LAT + (y + 5) / M_LAT}

    def test_way_without_lanes_takes_the_same_named_way_going_the_same_way(self):
        f = features([[(0, 0), (100, 0)], [(100, 0), (200, 0)], [(0, 300), (100, 300)], [(200, 0), (300, 0)]],
                     ["secondary"] * 4)
        f.attrs["src_record"] = np.array(["w1@1", "w2@1", "w3@1", "w4@1"])
        f.attrs["name"] = np.array(["Main"] * 4)
        tags = {1: dict(self.box(0, 210), name="Main", lanes=4), 2: dict(self.box(100, 200), name="Main"),
                3: dict(self.box(0, 100, 300), name="Main"),
                4: dict(self.box(200, 300), name="Main", osm_oneway="yes")}
        osmtags.join(f, tags)
        self.assertEqual(f.attrs["osm_lanes"][1], 4, "the untagged way of the street takes its count")
        self.assertTrue(np.isnan(f.attrs["osm_lanes"][2]), "the same name 300 m off: none")
        self.assertTrue(np.isnan(f.attrs["osm_lanes"][3]), "a one-way does not take a two-way's count")

    def test_a_two_way_with_lanes_is_not_made_a_divided_carriageway(self):
        """Portland's Rosa Parks Way: two two-way ways of one name 20 m apart, lanes=3 each (1 + 1 + a turn lane),
        were made one-way carriageways of 3 lanes each."""
        f = tagged([[(0, 0), (300, 0)], [(300, 20), (0, 20)]], ["primary"] * 2, ["w1@1", "w2@1"],
                   {1: {"lanes": 3}, 2: {"lanes": 3}})
        f.attrs["name"] = np.array(["Rosa Parks Way"] * 2)
        for sp in roads.road_specs(f):
            self.assertFalse(sp.oneway)
            self.assertEqual(navgraph._lanes(navgraph.driving_width(sp), False, sp.lanes), (1, 1))

    def test_bridge_without_lanes_takes_the_same_named_way_count(self):
        """GitHub #378: a bridge drawn at its class's 8 m instead of OSM's 4 lanes. Here the bridge's own way has no
        lane count; it takes the street's (4), and the deck is as wide as 4 lanes."""
        f = features([[(-300, 0), (-100, 0)], [(-100, 0), (100, 0)]], ["primary"] * 2,
                     levels=[(1, 1.0, 0.0, 1.0)])
        f.attrs["src_record"] = np.array(["w1@1", "w2@1"])
        f.attrs["name"] = np.array(["Kommunalny"] * 2)
        osmtags.join(f, {1: dict(self.box(-300, -90), name="Kommunalny", lanes=4),
                         2: dict(self.box(-100, 100), name="Kommunalny")})
        sp = roads.bridge_specs(f)[1]
        self.assertEqual(sp.lanes, 4)
        self.assertGreaterEqual(sp.width, 4 * roads.LANE_W.get("primary", roads.LANE_W_DEFAULT))

    def test_relation_segment_takes_the_two_way_count_not_split(self):
        """GitHub #175 (Kostiantynivka, vul. Konrada Hampera): a relation segment (no way id) beside the same street's
        two-way 4-lane way, a one-way 1-lane ramp and an untagged side piece was drawn as one 3.5 m one-way lane: the road narrowed
        sharply. It takes the two-way's 4 lanes and stays two-way."""
        f = features([[(0, 0), (100, 0), (200, 0)], [(200, 0), (400, 0)], [(0, 10), (200, 10)], [(50, 0), (150, 0)]],
                     ["secondary", "secondary", "secondary", "unclassified"])
        f.attrs["src_record"] = np.array(["r9@1", "w1@1", "w2@1", "w3@1"])
        f.attrs["name"] = np.array(["Hampera"] * 4)
        osmtags.join(f, {1: dict(self.box(0, 400), name="Hampera", lanes=4),
                         2: dict(self.box(0, 200, 10), name="Hampera", lanes=1, osm_oneway="yes"),
                         3: dict(self.box(50, 150), name="Hampera")})    # (the smallest around it: no count)
        self.assertEqual(f.attrs["osm_lanes"][0], 4)
        sp = roads.road_specs(f)[0]
        self.assertFalse(sp.oneway)
        self.assertGreaterEqual(sp.width, 4 * roads.LANE_W.get("secondary", roads.LANE_W_DEFAULT))

    def test_driving_width_follows_the_lane_count(self):
        sp = tagged([[(0, 0), (200, 0)]], ["residential"], ["w1@1"], {1: {"lanes": 4}})
        sp = roads.road_specs(sp)[0]
        self.assertAlmostEqual(navgraph.driving_width(sp), 4 * roads.LANE_W_DEFAULT, "not the class's 2 lanes")

    def test_measured_width_does_not_override_the_lane_count(self):
        f = tagged([[(0, 0), (200, 0)]], ["secondary"], ["w1@1"], {1: {"lanes": 4}})
        sp = roads.road_specs(f, None, lambda fid, ll: (7.0, 2.0))[0]
        self.assertGreaterEqual(sp.width, 4 * roads.LANE_W["secondary"])


class OnewayJoinTest(unittest.TestCase):
    def test_one_way_end_moves_to_its_own_lane_of_the_two_way(self):
        """A one-way (north) ending on a two-way's centre line moves right of its travel by a quarter of the two-way's
        width, faded along it; its other end, on another one-way, stays."""
        import oneway_joins
        from test_roads import M_LON
        f = features([[(-100, 0), (0, 0), (100, 0)], [(0, -60), (0, -30), (0, 0)], [(0, -60), (40, -60)]],
                     ["secondary", "residential", "residential"])
        f.attrs["osm_oneway"] = np.array(["", "yes", "yes"])
        f.attrs["osm_lanes"] = np.array([4.0, np.nan, np.nan])
        x0 = f.lonlat[:, 0].copy()
        self.assertEqual(oneway_joins.apply(f), 1)
        dx = (f.lonlat[:, 0] - x0) * M_LON
        self.assertAlmostEqual(dx[5], min(4 * oneway_joins.LANE_M / 4, oneway_joins.MAX_SHIFT_M), places=3)
        self.assertEqual(dx[4], 0.0, "30 m along: past the fade")
        self.assertEqual(dx[3], 0.0, "the end on the other one-way stays")
        self.assertTrue(np.all(dx[:3] == 0.0), "the two-way stays")

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
