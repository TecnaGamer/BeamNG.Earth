import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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
    def test_default_asphalt_reaches_a_setback_without_a_sidewalk_but_spares_green(self):
        import roadmesh

        house = rectangle(0., 11., 24., 21.)
        args = (FRAME, [], [house], [], STREETS, [], BOX)
        paved = streetfront.chunks(*args)
        self.assertTrue(paved)
        self.assertEqual({c["m"] for c in paved}, {roadmesh.FRONTAGE_ASPHALT})
        self.assertAlmostEqual(max(max(y for _, y in c["poly"]) for c in paved), 11. - .07)
        self.assertFalse(streetfront.chunks(*args, extend=False, residential=False))
        green = streetfront.PhotoPaving(np.full((80, 80, 3), [65, 100, 60], np.uint8), -16., -16., 60.)
        grass = []
        self.assertFalse(streetfront.chunks(*args, photo=green, grass=grass))
        self.assertTrue(grass)

    def test_terrain_apron_pads_do_not_remove_lawn_outside_the_pavement(self):
        rings = streetfront.pad_items(FRAME, BOX, "tile", [
            streetfront._paving(rectangle(2., 4., 6., 8.)[:-1], "concrete")],
            [rectangle(4., 4., 8., 8.)[:-1]])
        y, x = np.mgrid[0:16:2., 0:16:2.]
        lat, lon, _ = FRAME.enu_to_geodetic(x, y, 0.)
        ground = np.full(x.shape, surface.DIRT, np.uint8)
        sf = surface.Surface(None)
        self.addCleanup(sf.close)
        self.assertTrue(sf.add_fuel_pads(rings))
        self.assertFalse(sf.add_fuel_pads(rings))
        self.assertFalse(sf.fuel_pads(lat, lon))
        sf.clear_fuel_pads()
        paved = sf.paint_ground_pads(ground, lat, lon)
        self.assertTrue(np.all(paved[2:4, 1:3] == surface.ASPHALT))
        self.assertEqual(np.count_nonzero(paved == surface.ASPHALT), 4)
        self.assertTrue(np.all(paved[2:4, 3] == surface.GRASS))
        self.assertEqual(np.count_nonzero(paved == surface.GRASS), 2)
        self.assertTrue(sf.add_fuel_pads([(key, []) for key, _ in rings]))
        np.testing.assert_array_equal(sf.paint_ground_pads(ground, lat, lon), ground)

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
        self.assertEqual(pending, set())        # (the European road details off: as before)
        with patch.dict(os.environ, {"BNE_EU_ROAD_DETAILS": "on"}):
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
    def test_hedge_crowns_clear_the_road_and_the_crossing(self):
        along = feature([[0., 3.2], [14., 3.2]], "hedge", mapgeom.LINE)
        across = feature([[0., -.2], [0., 9.]], "hedge", mapgeom.LINE)
        with patch("house_regions.profile", return_value="alsace"):
            for hedge in (along, across):
                rows = fences.rows(FRAME, lambda _: hedge, BOX, flat, french=True, carriageways=STREETS)
                self.assertTrue(rows)
                self.assertTrue(all(r[2] >= 3. + r[6] * fences.HEDGE_CROWN_PER_M + .14 for r in rows))
                if hedge is across:
                    self.assertTrue(all(abs(r[1]) < .01 for r in rows))

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

    def test_unmapped_garden_gets_a_boundary_but_an_access_stays_open(self):
        ring = rectangle(4., 12., 17., 23.)[:-1]
        house = SimpleNamespace(door=(0, 6.5), seed=99)
        green = streetfront.PhotoPaving(np.full((80, 80, 3), [65, 100, 60], np.uint8), -16., -16., 60.)
        args = (FRAME, [(house, ring)], [ring], [], STREETS, [], [], [])
        with patch("furniture._h01", return_value=.3), patch("house_regions.profile", return_value="alsace"):
            rows = fences.inferred_rows(*args, [], flat, green)
            self.assertGreaterEqual(len(rows), 4)
            self.assertTrue(all(abs(r[1] - 10.5) > 1.6 and r[2] == 4. for r in rows))
            drive = {"n": [[6., 0., 0.], [6., 13., 0.]], "w": 2.8}
            self.assertFalse(fences.inferred_rows(*args, [drive], flat, green))
            house.door = None
            self.assertTrue(fences.inferred_rows(*args, [], flat, None))
            self.assertFalse(fences.inferred_rows(*args, [drive], flat, None))
        hedge = feature([[0., 10.], [10., 10.]], "hedge", mapgeom.LINE)
        with patch("house_regions.profile", return_value="alsace"):
            rows = fences.rows(FRAME, lambda _: hedge, BOX, flat, french=True)
        self.assertGreaterEqual(len(rows), 8)
        self.assertEqual({row[0] for row in rows}, {fences.G_HEDGE})
        self.assertTrue(all(1.6 <= row[6] <= 2. for row in rows))


class CityFrontageTest(unittest.TestCase):
    """Outside Europe's details a city's sidewalk runs to the buildings' faces (city_frontage), houses keep lawns."""

    WALK = {"pts": np.array([[-8., 4.25, 0.], [33., 4.25, 0.]]), "side": 1, "w": 2.5}     # (curb 3.0, back 5.5)

    def test_the_sidewalk_itself_is_widened_to_the_walls(self):
        walk = dict(self.WALK, pts=np.array([[x, 4.25, 0.] for x in np.arange(-8., 33.5, 1.5)]))
        shops = [rectangle(-8., 8., 10., 18.), rectangle(10., 9., 33., 18.)]      # (fronts 2.5 and 3.5 m back)
        walks = [walk]
        self.assertEqual(streetfront.widen_city_walks(walks, shops, shops, [], [], STREETS), 1)
        curb, back = streetfront._walk_outline(walks[0])
        self.assertAlmostEqual(float(np.median(curb[:, 1])), 3.0, places=2, msg="the curb stays")
        self.assertGreaterEqual(float(np.median(back[:, 1])), 8.5, "the slab's back at (or in) the walls")
        self.assertGreater(walks[0]["w"], 5.0)
        lawn = [dict(self.WALK, pts=walk["pts"].copy(), w=2.5)]
        green = rectangle(-8., 6.5, 33., 7.5)
        streetfront.widen_city_walks(lawn, shops, shops, [green], [], STREETS)
        self.assertLess(lawn[0]["w"], 2.5 + 1.01, "a mapped lawn in front: no further than it")
        self.assertEqual(streetfront.widen_city_walks([dict(self.WALK)], [], [], [], [], STREETS), 0)

    def test_city_buildings_and_houses(self):
        from types import SimpleNamespace as NS
        ring = rectangle(0., 0., 10., 10.)[:-1]
        def spec(cls, floors, shop=False):
            return (NS(ring=ring, cls=cls, floors=floors, shop=shop, deck=None, min_h=0., outline=None), None, None)
        with patch("buildings.drawn_ring", lambda s: s.ring):
            pick = lambda items, dense: len(streetfront.city_targets(items, np.zeros(2), dense))
            self.assertEqual(pick([spec("retail", 1), spec("", 4), spec("", 1, shop=True)], False), 3)
            self.assertEqual(pick([spec("house", 2), spec("", 2), spec("detached", 1)], False), 0)
            self.assertEqual(pick([spec("", 3)], True), 1)
            self.assertEqual(pick([spec("terrace", 2), spec("", 2)], True), 0, "row houses of two floors: their yards")
            self.assertEqual(pick([spec("shed", 1), spec("greenhouse", 1)], True), 0)
