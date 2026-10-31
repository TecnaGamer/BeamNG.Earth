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

    def test_catalog_names_the_stock_shape(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("M.CATALOG.busshelter", source)
        self.assertIn("s_busstop_ecu", source)
        self.assertNotIn("M.NO_COLLIDE.busshelter", source)
