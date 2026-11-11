"""Bus shelters at mapped bus stops (shelters.py)."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy
import mapgeom
import shelters


class KindTest(unittest.TestCase):
    def features(self, cls, tags=None):
        attrs = {"subtype": np.array(["transit"]), "class": np.array([cls])}
        if tags is not None:
            attrs["tags"] = np.array([json.dumps(tags)])
        return type("F", (), {"attrs": attrs})()

    def test_bus_stops_and_transit_shelters(self):
        self.assertTrue(shelters.is_shelter(self.features("bus_stop"), 0))
        self.assertTrue(shelters.is_shelter(self.features("bus_stop", {"shelter": "yes"}), 0))
        self.assertTrue(shelters.is_shelter(self.features("amenity", {"highway": "bus_stop"}), 0))
        self.assertTrue(shelters.is_shelter(
            self.features("amenity", {"amenity": "shelter", "shelter_type": "public_transport"}), 0))
        self.assertFalse(shelters.is_shelter(self.features("bus_stop", {"shelter": "no"}), 0))
        self.assertFalse(shelters.is_shelter(self.features("amenity", {"amenity": "shelter"}), 0))
        self.assertFalse(shelters.is_shelter(self.features("platform"), 0))
        # a stop_position node is where the bus halts, on the road: a shelter only when it says so
        self.assertFalse(shelters.is_shelter(
            self.features("bus_stop", {"highway": "bus_stop", "public_transport": "stop_position"}), 0))
        self.assertTrue(shelters.is_shelter(
            self.features("bus_stop", {"public_transport": "stop_position", "shelter": "yes"}), 0))
        self.assertFalse(shelters.is_shelter(self.features("bench"), 0))


class PlaceTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(39.2834, -76.5934, 0.0)

    def stop(self, xy, cls="bus_stop", tags=None, fid="stop-1"):
        xy = np.asarray(xy, np.float64).reshape(1, 2)
        lat, lon, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(1))
        return mapgeom.Features(
            layer="infrastructure", ids=np.array([fid]), kind=np.array([mapgeom.POINT], np.uint8),
            part_off=np.array([0, 1], np.int64), ring_off=np.array([0, 1], np.int64),
            coord_off=np.array([0, 1], np.int64), lonlat=np.column_stack((lon, lat)),
            attrs={"subtype": np.array(["transit"]), "class": np.array([cls]),
                   "tags": np.array([json.dumps(tags or {})])})

    def road(self, width=8.0):
        spec = type("S", (), {"width": width})()
        pts = np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]])
        return [("rd", spec, pts, 0.0)]

    def rows(self, feats, roads=(), box=(0.0, 0.0, 100.0, 100.0)):
        return shelters.rows(self.frame, lambda key: feats, box, lambda la, lo: np.full(np.shape(la), 5.0), roads)

    def test_a_stop_faces_the_road_from_the_sidewalk(self):
        got = self.rows(self.stop([[50.0, 8.0]]), self.road())
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "busshelter")
        self.assertEqual(got[0][6], shelters.SHELTER_H)
        self.assertAlmostEqual(got[0][3], 5.0 + shelters.LIFT, delta=0.02)
        self.assertAlmostEqual(got[0][4], 90.0, delta=0.5)
        self.assertAlmostEqual(got[0][1], 50.0, delta=0.02)
        self.assertGreater(got[0][2], 8.0)

    def test_the_shelter_stands_clear_of_the_carriageway(self):
        # the signed face (origin - STREET_X) must lie at least CURB_PAD_M past the edge of an 8 m road
        got = self.rows(self.stop([[50.0, 4.2]]), self.road())
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(got[0][2] - shelters.STREET_X, 4.0 + shelters.CURB_PAD_M - 0.02)

    def test_no_post_stands_on_a_road_beside_the_one_it_faces(self):
        # an 8 m road along x and a 6 m lane across it at x = 52: every post clear of both
        lane = ("lane", type("S", (), {"width": 6.0})(), np.array([[52.0, -50.0, 0.0], [52.0, 50.0, 0.0]]), None)
        roads = list(self.road()) + [lane]
        got = self.rows(self.stop([[50.0, 8.0]]), roads)
        self.assertEqual(len(got), 1)
        posts = shelters._posts(got[0][1], got[0][2], got[0][4])
        self.assertTrue((np.abs(posts[:, 1]) >= 4.0 + shelters.POST_PAD_M - 0.02).all(), posts)
        self.assertTrue((np.abs(posts[:, 0] - 52.0) >= 3.0 + shelters.POST_PAD_M - 0.02).all(), posts)

    def test_the_base_sits_on_the_lowest_ground_under_the_posts(self):
        # a bank rising away from the road (y): the posts uphill are in the ground, the downhill ones touch it
        def elev(la, lo):
            _, y, _ = self.frame.geodetic_to_enu(la, lo, np.zeros(np.shape(la)))[:3]
            return 2.0 + 0.5 * (y - 8.0)
        got = shelters.rows(self.frame, lambda key: self.stop([[50.0, 8.0]]), (0.0, 0.0, 100.0, 100.0), elev,
                            self.road())
        y0 = got[0][2]
        low = 2.0 + 0.5 * (y0 - shelters.STREET_X - 8.0)       # the street-side posts, the lowest
        self.assertAlmostEqual(got[0][3], low + shelters.LIFT, delta=0.05)

    def road_across(self, x=50.0, width=6.0):
        spec = type("S", (), {"width": width})()
        return ("across", spec, np.array([[x, -60.0, 0.0], [x, 60.0, 0.0]]), None)

    def on_road(self, feats, roads, box=(0.0, -20.0, 100.0, 100.0)):
        """Rows, and the deepest body point of each in any carriageway."""
        got = self.rows(feats, roads, box)
        return got, [shelters._intrusion(r[1], r[2], r[4], roads) for r in got]

    def test_a_stop_on_a_junction_node_stands_clear_of_every_road(self):
        # a stop node at the T junction of an 8 m road along x and a 6 m road across it: no body point on either
        roads = list(self.road()) + [self.road_across()]
        for fid in ("a", "b", "c", "d", "e", "f"):          # whichever side the hash would have picked
            got, deep = self.on_road(self.stop([[50.0, 0.0]], fid=fid), roads)
            if got:
                self.assertLessEqual(deep[0], shelters.BODY_ON_ROAD_M, (fid, got))

    def test_a_stop_on_the_centre_line_picks_the_side_with_room(self):
        # a service lane runs alongside, 2 m past the main road's edge on the +y side: the free side is -y
        lane = ("lane", type("S", (), {"width": 4.0})(), np.array([[0.0, 7.0, 0.0], [100.0, 7.0, 0.0]]), None)
        roads = list(self.road()) + [lane]
        for fid in ("a", "b", "c", "d", "e", "f"):
            got, deep = self.on_road(self.stop([[50.0, 0.0]], fid=fid), roads)
            self.assertEqual(len(got), 1, fid)
            self.assertLessEqual(deep[0], shelters.BODY_ON_ROAD_M, fid)
            self.assertLess(got[0][2], 0.0, fid)

    def test_a_stop_with_no_clear_spot_is_left_out(self):
        # two wide roads 1 m apart leave no room for a 1.5 m wide shelter between them
        spec = type("S", (), {"width": 40.0})()
        roads = [("w", spec, np.array([[0.0, 20.5, 0.0], [100.0, 20.5, 0.0]]), None),
                 ("e", spec, np.array([[0.0, -20.5, 0.0], [100.0, -20.5, 0.0]]), None)]
        self.assertEqual(self.rows(self.stop([[50.0, 0.0]]), roads, (0.0, -20.0, 100.0, 100.0)), [])

    def test_a_stop_position_node_without_a_shelter_tag_is_left_out(self):
        tags = {"highway": "bus_stop", "public_transport": "stop_position"}
        self.assertEqual(self.rows(self.stop([[50.0, 0.0]], tags=tags), self.road()), [])
        self.assertEqual(len(self.rows(self.stop([[50.0, 0.0]], tags=dict(tags, shelter="yes")), self.road())), 1)

    def test_a_stop_in_the_carriageway_is_set_back_to_the_curb(self):
        got = self.rows(self.stop([[50.0, 1.0]]), self.road())
        self.assertEqual(len(got), 1)
        self.assertGreater(got[0][2], 4.0)
        self.assertAlmostEqual(got[0][4], 90.0, delta=0.5)

    def test_the_other_side_faces_the_other_way(self):
        got = self.rows(self.stop([[50.0, -8.0]]), self.road(), box=(0.0, -20.0, 100.0, 100.0))
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0][4], 270.0, delta=0.5)
        self.assertLess(got[0][2], -8.0)

    def test_a_stop_tagged_without_a_shelter_is_left_out(self):
        self.assertEqual(self.rows(self.stop([[50.0, 8.0]], tags={"shelter": "no"}), self.road()), [])

    def test_a_stop_with_no_road_still_stands(self):
        got = self.rows(self.stop([[40.0, 40.0]]))
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0][1], 40.0, delta=0.02)
        self.assertAlmostEqual(got[0][2], 40.0, delta=0.02)

    def test_a_stop_past_the_tile_is_left_out(self):
        self.assertEqual(self.rows(self.stop([[-5.0, 8.0]]), self.road()), [])

    def test_a_shelter_stays_off_a_track_beside_the_road(self):
        # a track 3 m past the road's edge (Nemiia, prospekt Heroiv, 2026-11-08) that bends away from it at x = 70: the
        # stop at x = 30 stood on the rails; it slides along the curb to where the track has left (or is left out)
        feats = self.stop([[30.0, 8.0]])
        axis = np.array([[x, 7.2 + max(0.0, x - 55.0) * 0.6, 0.0] for x in np.linspace(0.0, 100.0, 51)])
        rails = [("rail:1", 2.6, axis)]
        road = self.road()
        self.assertEqual(shelters._rail_short(30.0, 8.0, 90.0, []), 0.0)
        self.assertGreater(shelters._rail_short(30.0, 8.0, 90.0, rails), 0.5)
        got = shelters._clear_of_rails(30.0, 8.0, 90.0, road, rails)
        self.assertIsNone(got)              # (within the 15 m it can slide the track is still beside the road)
        axis_far = np.array([[x, 7.2 + max(0.0, x - 35.0) * 1.0, 0.0] for x in np.linspace(0.0, 100.0, 51)])
        got = shelters._clear_of_rails(30.0, 8.0, 90.0, road, [("rail:1", 2.6, axis_far)])
        self.assertIsNotNone(got)
        self.assertLessEqual(shelters._rail_short(got[0], got[1], 90.0, [("rail:1", 2.6, axis_far)]), 1e-3)
        self.assertGreater(abs(got[0] - 30.0), 1.0, "it slid along the road")
        self.assertEqual(len(self.rows(feats, road)), 1, "no tracks: the shelter stands")

    def test_catalog_names_the_stock_shape(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("M.CATALOG.busshelter", source)
        self.assertIn("s_busstop_ecu", source)
        self.assertNotIn("M.NO_COLLIDE.busshelter", source)


class RegionTest(unittest.TestCase):
    def test_regional_shelters(self):
        self.assertIs(shelters.shelter_for("US", 39.3), shelters.SHELTERS["na"])
        self.assertIs(shelters.shelter_for("DE", 48.1), shelters.SHELTERS["eu"])
        self.assertIs(shelters.shelter_for("BR", -12.0), shelters.SHELTERS["tropics"])
        self.assertIs(shelters.shelter_for("AU", -33.9), shelters.SHELTERS["other"])
        self.assertIs(shelters.shelter_for(None, 39.3), shelters.SHELTERS["na"])

    def test_a_european_stop_gets_its_shelter_clear_of_the_road(self):
        frame = geodesy.EnuFrame(48.137, 11.575, 0.0)
        xy = np.array([[50.0, 2.0]])
        lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(1))
        feats = mapgeom.Features(
            layer="infrastructure", ids=np.array(["s"]), kind=np.array([mapgeom.POINT], np.uint8),
            part_off=np.array([0, 1], np.int64), ring_off=np.array([0, 1], np.int64),
            coord_off=np.array([0, 1], np.int64), lonlat=np.column_stack((lon, lat)),
            attrs={"subtype": np.array(["transit"]), "class": np.array(["bus_stop"]),
                   "tags": np.array([json.dumps({"shelter": "yes"})])})
        spec = type("S", (), {"width": 8.0})()
        road = [("rd", spec, np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]), 0.0)]
        got = shelters.rows(frame, lambda key: feats, (0.0, 0.0, 100.0, 100.0),
                            lambda la, lo: np.full(np.shape(la), 5.0), road, country="DE")
        self.assertEqual(len(got), 1)
        d = shelters.SHELTERS["eu"]
        self.assertEqual(got[0][0], d.group)
        self.assertEqual(got[0][6], d.h)
        self.assertLessEqual(shelters._intrusion(got[0][1], got[0][2], got[0][4], road, d), shelters.BODY_ON_ROAD_M)
        self.assertGreaterEqual(got[0][2] - d.front, 4.0 + shelters.CURB_PAD_M - 0.02)
