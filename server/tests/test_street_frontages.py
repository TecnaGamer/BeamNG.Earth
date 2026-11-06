import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import earthd
import fences
import geodesy
import mapgeom
import roads
import sidewalks
import streetfront
import streetparking
import surface


FRAME = geodesy.EnuFrame(48.82, 7.78)
BOX = (-6., -12., 30., 28.)
STREET = roads.Spec(6., "paved", True, "residential")
STREETS = [("street", STREET, np.array([[-10., 0., 0.], [35., 0., 0.]]), 7.78)]


def flat(x, y):
    return np.zeros(len(x))


def feature(line, cls, kind, tags=None):
    line = np.asarray(line)
    lat, lon, _ = FRAME.enu_to_geodetic(line[:, 0], line[:, 1], 0.)
    return mapgeom.Features("infrastructure", np.array(["one"]), np.array([kind]), np.array([0, 1]),
                            np.array([0, 1]), np.array([0, len(line)]), np.c_[lon, lat],
                            attrs={"class": np.array([cls]), "tags": np.array([json.dumps(tags or {})])})


def rectangle(x0, y0, x1, y1):
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]])


class StreetParkingTest(unittest.TestCase):
    def test_narrow_mapped_bays_supply_markings_and_slots_in_the_same_place(self):
        rect = rectangle(1., 3.5, 15.6, 5.66)
        features = [feature(rect, "parking", mapgeom.POLYGON, {
            "parking": "street_side", "orientation": "parallel", "capacity": "3"})]
        args = (FRAME, BOX, features, STREETS, [], [], [], [], [], [], flat)
        rows, marks = streetparking.tile_layout(*args)
        self.assertEqual(len(rows), 3)
        self.assertTrue(marks)
        self.assertTrue(all(r[4] == 0 and r[5:] == [2., 4.65] and abs(r[3] - 180.) < .01 for r in rows))
        for mark in marks:
            self.assertTrue(np.all((np.asarray(mark["n"])[:, 1] >= 3.5) & (np.asarray(mark["n"])[:, 1] <= 5.66)))
        left, _ = streetparking.tile_layout(*args, left_hand=True)
        self.assertTrue(all(abs(r[3]) < .01 for r in left))

    def test_mapped_driveway_only_parks_clear_of_street_and_footway(self):
        drive = ("drive", roads.Spec(3.1, "paved", True, "service"), np.array([[8., 0., 0.], [8., 15., 0.]]))
        home = rectangle(4., 16., 13., 24.)
        walk = {"pts": np.array([[0., 4., 0.], [16., 4., 0.]]), "w": 1.5}
        rows, marks = streetparking.tile_layout(FRAME, BOX, [], STREETS, [drive], [walk], [home], [home],
                                                [], [], flat)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0][1], 9.)
        self.assertEqual(rows[0][4], 1)
        self.assertEqual(marks, [])
        barrier = np.array([[5., 12.], [11., 12.]])
        rows, _ = streetparking.tile_layout(FRAME, BOX, [], STREETS, [drive], [walk], [home], [home],
                                            [barrier], [], flat)
        self.assertEqual(rows, [])


class PavementTest(unittest.TestCase):
    def test_paved_widening_keeps_curb_position_and_respects_gardens(self):
        rgb = np.full((128, 128, 3), 139, np.uint8)
        photo = streetfront.PhotoPaving(rgb, -10., -12., 40.)
        house = rectangle(0., 8.3, 24., 16.)
        points = np.array([[1., 3.75, 0.], [20., 3.75, 0.]])
        walk = {"pts": points, "side": 1, "w": 1.5, "europe": True}
        curb, _ = sidewalks.run_geometry(points, 1, 1.5)
        self.assertTrue(streetfront.widen_walks([walk], photo, [house], [], [], STREETS))
        widened, _ = sidewalks.run_geometry(walk["pts"], 1, walk["w"])
        self.assertGreater(walk["w"], 3.)
        np.testing.assert_allclose(widened[[0, -1], :2], curb[:, :2])
        beside_garden = {"pts": points, "side": 1, "w": 1.5, "europe": True}
        garden = rectangle(0., 4.6, 24., 8.2)
        self.assertFalse(streetfront.widen_walks([beside_garden], photo, [house], [garden], [], STREETS))

    def test_pending_road_paint_can_refresh_after_the_data_arrives(self):
        pending = set()
        cached = SimpleNamespace(roads=lambda la, lo: ([], False))
        rgb = np.zeros((16, 16, 3), np.uint8)
        surface.paint_square(rgb, FRAME, 0., 0., 20., cached, simple=True, pending=pending)
        self.assertEqual(pending, {surface.PHOTO_ROADS_PENDING})
        key = (1, 0, 0)
        server = SimpleNamespace(
            base_pending={}, base_stale=set(), layout=SimpleNamespace(block=16),
            imagery=SimpleNamespace(lazy_ready=lambda *args: True),
            mapdata=SimpleNamespace(has=lambda *args: True),
        )
        server.base_ready = lambda a, x, y: earthd.Server.base_ready(server, a, x, y)
        anchor = SimpleNamespace(id=1, frame=FRAME)
        earthd.Server._base_kept(server, anchor, 0, 0, pending)
        self.assertIn(key, server.base_stale)
        self.assertTrue(server.base_ready(anchor, 0, 0))
        server.mapdata.has = lambda *args: False
        self.assertFalse(server.base_ready(anchor, 0, 0))


class FrenchBoundaryTest(unittest.TestCase):
    def test_regional_models_follow_mapped_boundaries_only(self):
        line = feature([[0., 10.], [10., 10.]], "fence", mapgeom.LINE, {"material": "wood"})
        with patch("house_regions.profile", return_value="alsace"):
            self.assertFalse(fences.rows(FRAME, lambda _: line, BOX, flat))
            rows = fences.rows(FRAME, lambda _: line, BOX, flat, french=True)
            self.assertTrue(rows)
            self.assertTrue(all(row[0] == "fr_fence_wood" and abs(row[2] - 10.) < .02 for row in rows))
            self.assertFalse(fences.rows(FRAME, lambda _: None, BOX, flat, french=True))
        self.assertEqual(fences.french_panel("wall", {}, "north", "one"), "brick")
        self.assertEqual(fences.french_panel("wall", {}, "central", "one"), "dark_stone")
