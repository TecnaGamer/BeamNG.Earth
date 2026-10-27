"""osmtags.py: OpenStreetMap's road tags (lanes, width, speed limit...) joined to Overture's road segments by way id,
and the roads using them (roads.road_specs / carriageway, navgraph lanes)."""
import sys
import unittest
import tempfile
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
        self.assertEqual(roads.road_specs(f)[0].lanes, 6)
        self.assertEqual(str(f.attrs["osm_sidewalk"][0]), "separate")
        self.assertEqual(str(f.attrs["osm_oneway"][0]), "", "oneway is not inherited")
        self.assertTrue(np.isnan(f.attrs["osm_lanes"][1]), "the same name 500 m away")
        self.assertTrue(np.isnan(f.attrs["osm_lanes"][2]), "another name inside the bbox")
        self.assertGreater(roads.road_specs(f)[0].width, roads.road_specs(f)[2].width)

    def test_osm_source_need_not_be_first(self):
        import duckdb
        import overture
        with duckdb.connect() as con:
            source = con.execute(f"""SELECT {overture._ROAD_SRC}.record_id FROM (
                SELECT [{{'dataset': 'Other', 'record_id': 'external', 'property': ''}},
                        {{'dataset': 'OpenStreetMap', 'record_id': 'w7@1', 'property': '/properties/name'}},
                        {{'dataset': 'OpenStreetMap', 'record_id': 'w123@2', 'property': '/geometry'}}] AS sources)""").fetchone()[0]
        f = tagged([[(0, 0), (200, 0)]], ['motorway'], [source], {123: {'lanes': 5}})
        self.assertEqual(roads.road_specs(f)[0].lanes, 5)

    def test_global_parquet_query_excludes_node_and_relation_id_collisions(self):
        import duckdb
        import rangeread
        import struct
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'highways.parquet'
            with duckdb.connect() as con:
                con.execute('''CREATE TABLE highways AS SELECT type, id, lanes, 'motorway' AS highway,
                    NULL::VARCHAR AS width, NULL::VARCHAR AS maxspeed, NULL::VARCHAR AS sidewalk,
                    NULL::VARCHAR AS lit, NULL::VARCHAR AS smoothness, NULL::VARCHAR AS surface,
                    'yes' AS oneway, NULL::VARCHAR AS layer, NULL::BLOB AS geometry, NULL::VARCHAR AS name,
                    'roundabout' AS junction,
                    {'xmin': lon, 'ymin': lat, 'xmax': lon + .001, 'ymax': lat + .001} AS bbox
                    FROM (VALUES ('way', 1, '4', -84.5, 42.7), ('node', 1, '9', -84.5, 42.7),
                        ('relation', 1, '8', -84.5, 42.7), ('way', 2, '3', 139.7, 35.7),
                        ('way', 3, ' 2 ', -.1, 51.5), ('way', 4, '2;3', 12., 45.),
                        ('way', 5, '2.5', 12., 45.)) t(type, id, lanes, lon, lat)''')
                wkb = struct.pack('<BII4d', 1, 2, 2, -84.5, 42.7, -84.499, 42.701)
                con.execute("UPDATE highways SET geometry=? WHERE type='way' AND id=1", [wkb])
                con.execute('COPY highways TO ? (FORMAT PARQUET)', [str(path)])
            reader = osmtags.OsmTags(tmp, None)
            tags, raceways = reader._cell(rangeread.MemoryRange(path.read_bytes()), (-180., -90., 180., 90.), 'demand')
            self.assertEqual([tags[i]['lanes'] for i in (1, 2, 3)], [4, 3, 2])
            self.assertEqual(tags[1]['osm_junction'], 'roundabout')
            self.assertNotIn('lanes', tags[4])
            self.assertNotIn('lanes', tags[5])
            self.assertEqual(raceways, [])
            np.testing.assert_allclose(tags[1]['_coords'], [[-84.5, 42.7], [-84.499, 42.701]])
            original = reader._reader._fetch
            calls = []
            def fail_geometry(*args):
                calls.append(1)
                if len(calls) == 2:
                    raise OSError('geometry block unavailable')
                return original(*args)
            with patch.object(reader._reader, '_fetch', side_effect=fail_geometry):
                tags, _ = reader._cell(rangeread.MemoryRange(path.read_bytes()), (-180., -90., 180., 90.), 'demand')
            self.assertEqual(tags[1]['lanes'], 4, 'optional geometry failure must not discard received tags')
            self.assertTrue(tags.incomplete, 'partial enrichment must retry, not cache missing counts forever')


class SpecTest(unittest.TestCase):
    def test_lanes_set_the_width_and_the_markings(self):
        f = tagged([[(0, 0), (200, 0)], [(0, 30), (200, 30)]], ["secondary", "secondary"], ["w1@1", "w2@1"],
                   {1: {"lanes": 4}})
        a, b = roads.road_specs(f)
        self.assertEqual(a.lanes, 4)
        self.assertIsNone(roads._lane_count(f, 1), 'the source count remains unknown')
        self.assertEqual(b.lanes, 2, 'the widened rural fallback keeps two lanes')
        self.assertGreaterEqual(a.width, 4 * roads.LANE_W["secondary"], "four lanes wide")
        self.assertGreater(a.width, b.width, "wider than the class's guess")

    def test_osm_width_respects_the_rural_lane_minimum(self):
        for width in (9.0, 15.0):
            f = tagged([[(0, 0), (200, 0)]], ["residential"], ["w1@1"], {1: {"width": width, "lanes": 2}})
            self.assertAlmostEqual(roads.road_specs(f)[0].width, max(11.0, width))

    def test_nonsense_is_not_believed(self):
        f = tagged([[(0, 0), (200, 0)]], ["residential"], ["w1@1"], {1: {"width": 400.0, "lanes": 40}})
        sp = roads.road_specs(f)[0]
        self.assertIsNone(roads._lane_count(f, 0), 'reject the invalid mapped count')
        self.assertEqual(sp.lanes, 2, 'use the rural fallback')
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
