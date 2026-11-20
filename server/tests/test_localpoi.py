"""Local points of interest outside the parks (localpoi.py)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import furniture
import localpoi
import mapgeom
import picnicsource
from test_picnic import PlaceTest as _PicnicBase, _square


class FakeClearance:
    """RoadClearance stand-in: a rectangle x < 'wall' is the carriageway."""

    def __init__(self, wall):
        self.wall = wall
        self.calls = 0

    def clear(self, row):
        self.calls += 1
        return row[1] > self.wall


class FakeRoad:
    """RoadClearance stand-in with one carriageway: the segment a-b (its a / b arrays are what face_road reads)."""

    def __init__(self, a, b):
        self.a = np.array([[a[0], a[1], 0.0]])
        self.b = np.array([[b[0], b[1], 0.0]])

    def clear(self, row):
        return True


class LocalPoiTest(_PicnicBase):
    def lp(self, land=None, infra=None, osm=None, bld=None, box=(0.0, 0.0, 100.0, 100.0), clearance=None, country=None):
        return localpoi.rows(self.frame, (lambda key: infra) if infra is not None else None,
                             (lambda key: land) if land is not None else None, box,
                             lambda la, lo: np.full(np.shape(la), 5.0),
                             (lambda keys: [osm]) if osm is not None else None,
                             (lambda key: bld) if bld is not None else None, clearance=clearance, country=country)

    def osm_rows(self, *spots):
        """spots: (osm id, leisure, amenity, tourism, x, y[, historic])."""
        return picnicsource.features_of([(sp[0], sp[1], sp[2], sp[3], self._wkb(sp[4], sp[5])) + tuple(sp[6:])
                                         for sp in spots])

    def groups(self, rows):
        return [r[0] for r in rows]

    def test_a_forest_table_outside_a_park_is_drawn(self):
        osm = self.osm_rows((1, "picnic_table", None, None, 40.0, 40.0))
        got = self.lp(osm=osm)
        self.assertEqual(self.groups(got), ["lp_table"])
        self.assertAlmostEqual(got[0][1], 40.0, delta=0.05)
        self.assertEqual(got[0][6], localpoi.HEIGHT["lp_table"])
        self.assertAlmostEqual(got[0][3], 5.0, delta=0.02)

    def test_a_table_in_a_park_is_picnic_pys(self):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(0, 0, 80, 80)], "park", {"leisure": "park"})])
        osm = self.osm_rows((1, "picnic_table", None, None, 40.0, 40.0))
        self.assertEqual(self.lp(land=park, osm=osm), [])
        self.assertEqual(self.groups(self.rows(park, osm=osm)), ["picnictable", "picnicgrill"])

    def test_a_picnic_site_point_gets_one_table_unless_a_mapped_one_is_near(self):
        site = self.osm_rows((1, None, None, "picnic_site", 40.0, 40.0))
        self.assertEqual(self.groups(self.lp(osm=site)), ["lp_table"])
        both = self.osm_rows((1, None, None, "picnic_site", 40.0, 40.0), (2, "picnic_table", None, None, 50.0, 40.0))
        got = self.lp(osm=both)
        self.assertEqual(self.groups(got), ["lp_table"])
        self.assertAlmostEqual(got[0][1], 50.0, delta=0.05)         # the mapped one, not the site's

    def test_a_picnic_area_polygon_gets_a_few_tables(self):
        site = self.feats([("site-1", mapgeom.POLYGON, [_square(10, 10, 60, 60)], "land", {"tourism": "picnic_site"})])
        got = self.lp(land=site)
        self.assertGreaterEqual(len(got), 1)
        self.assertLessEqual(len(got), localpoi.SITE_MAX)
        self.assertTrue(all(r[0] == "lp_table" and 10.0 < r[1] < 60.0 and 10.0 < r[2] < 60.0 for r in got))

    def test_two_points_of_one_table_are_one_table(self):
        osm = self.osm_rows((1, "picnic_table", None, None, 40.0, 40.0), (2, "picnic_table", None, None, 41.5, 40.0))
        self.assertEqual(self.groups(self.lp(osm=osm)), ["lp_table"])

    def test_a_shelter_is_a_hut_and_swallows_its_table(self):
        osm = self.osm_rows((1, None, "shelter", None, 40.0, 40.0), (2, "picnic_table", None, None, 41.0, 40.0))
        self.assertEqual(self.groups(self.lp(osm=osm)), ["lp_altan"])

    def test_a_shelter_at_a_bus_stop_is_the_stops(self):
        osm = self.osm_rows((1, None, "shelter", None, 40.0, 40.0))
        stop = self.feats([("stop-1", mapgeom.POINT, [[[50.0, 40.0]]], "bus_stop", None)])
        self.assertEqual(self.lp(osm=osm, infra=stop), [])
        far = self.feats([("stop-1", mapgeom.POINT, [[[90.0, 40.0]]], "bus_stop", None)])
        self.assertEqual(self.groups(self.lp(osm=osm, infra=far)), ["lp_altan"])

    def test_a_huge_shelter_polygon_is_not_a_hut(self):
        big = self.feats([("sh-1", mapgeom.POLYGON, [_square(10, 10, 40, 40)], "shelter", {"amenity": "shelter"})])
        self.assertEqual(self.lp(infra=big), [])

    def test_a_fire_pit_and_an_outside_grill(self):
        osm = self.osm_rows((1, "firepit", None, None, 30.0, 30.0), (2, None, "bbq", None, 60.0, 60.0))
        got = self.lp(osm=osm)
        self.assertEqual(sorted(self.groups(got)), ["lp_firepit", "picnicgrill"])

    def test_a_spot_inside_a_building_is_left_out(self):
        osm = self.osm_rows((1, "firepit", None, None, 40.0, 40.0))
        bld = self.footprint("house-1", [_square(30, 30, 50, 50)])
        self.assertEqual(self.lp(osm=osm, bld=bld), [])

    def test_a_spot_on_the_carriageway_is_moved_off_it_or_dropped(self):
        osm = self.osm_rows((1, "picnic_table", None, None, 40.0, 40.0))
        moved = self.lp(osm=osm, clearance=FakeClearance(41.5))
        self.assertEqual(self.groups(moved), ["lp_table"])
        self.assertGreater(moved[0][1], 41.5)
        self.assertLessEqual(np.hypot(moved[0][1] - 40.0, moved[0][2] - 40.0), localpoi.SHIFT_MAX_M + 1e-6)
        self.assertEqual(self.lp(osm=osm, clearance=FakeClearance(60.0)), [])

    def test_a_clear_spot_stays_put(self):
        osm = self.osm_rows((1, "picnic_table", None, None, 40.0, 40.0))
        got = self.lp(osm=osm, clearance=FakeClearance(0.0))
        self.assertEqual((got[0][1], got[0][2]), (40.0, 40.0))

    def test_a_spot_past_the_tile_is_left_to_its_own_tile(self):
        osm = self.osm_rows((1, "picnic_table", None, None, 140.0, 40.0))
        self.assertEqual(self.lp(osm=osm), [])

    def test_a_roadside_cross_faces_the_road_and_a_shrine_is_a_column(self):
        osm = self.osm_rows((1, None, None, None, 40.0, 40.0, "wayside_cross"),
                            (2, None, None, None, 70.0, 40.0, "wayside_shrine"))
        road = FakeRoad((0.0, 30.0), (100.0, 30.0))              # south of the cross: it looks towards -Y
        got = self.lp(osm=osm, clearance=road, country="CZ")
        self.assertEqual(len(got), 2)
        cross = [r for r in got if r[0] in localpoi.CROSS_GROUPS]
        shrine = [r for r in got if r[0] == localpoi.G_SHRINE]
        self.assertEqual(len(cross), 1)
        self.assertEqual(len(shrine), 1)
        self.assertAlmostEqual(cross[0][4] % 360.0, 0.0, delta=0.2)
        north = FakeRoad((0.0, 60.0), (100.0, 60.0))               # north: the front must turn to +Y
        again = self.lp(osm=osm, clearance=north, country="CZ")
        self.assertAlmostEqual([r for r in again if r[0] in localpoi.CROSS_GROUPS][0][4], 180.0, delta=0.2)
        east = FakeRoad((60.0, 0.0), (60.0, 100.0))               # to the east: local -Y must point to +X (yaw 90)
        right = self.lp(osm=osm, clearance=east, country="CZ")
        self.assertAlmostEqual([r for r in right if r[0] in localpoi.CROSS_GROUPS][0][4], 90.0, delta=0.2)

    def test_a_cross_in_a_park_is_still_drawn_and_two_close_crosses_are_one(self):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(0, 0, 80, 80)], "park", {"leisure": "park"})])
        osm = self.osm_rows((1, None, None, None, 40.0, 40.0, "wayside_cross"),
                            (2, None, None, None, 41.5, 40.0, "wayside_cross"))
        got = self.lp(land=park, osm=osm)
        self.assertEqual(len(got), 1)

    def test_a_cross_with_no_road_near_still_gets_a_yaw(self):
        osm = self.osm_rows((1, None, None, None, 40.0, 40.0, "wayside_cross"))
        got = self.lp(osm=osm, clearance=FakeRoad((0.0, 500.0), (100.0, 500.0)))
        self.assertEqual(len(got), 1)
        self.assertTrue(0.0 <= got[0][4] < 360.0)

    def test_off_switch(self):
        osm = self.osm_rows((1, "picnic_table", None, None, 40.0, 40.0))
        localpoi.ON = False
        try:
            self.assertEqual(self.lp(osm=osm), [])
        finally:
            localpoi.ON = True


class SourceAndCatalogTest(unittest.TestCase):
    def test_osm_marks_of_the_new_classes(self):
        self.assertEqual(picnicsource._mark(None, "shelter", None)[0], "shelter")
        self.assertEqual(picnicsource._mark("firepit", None, None)[0], "firepit")
        self.assertIsNone(picnicsource._mark("bench", "bench", None))
        # picnic.py must not take them: it only draws tables, sites and grills
        import picnic

        class F:
            attrs = {"class": np.array(["shelter"]), "tags": np.array(['{"amenity": "shelter"}'])}
        self.assertIsNone(picnic.marked(F(), 0))

    def test_wayside_marks_of_the_osm_rows(self):
        self.assertEqual(picnicsource._mark(None, None, None, "wayside_cross")[0], "wayside_cross")
        self.assertEqual(picnicsource._mark(None, None, None, "Wayside_Shrine")[0], "wayside_shrine")
        self.assertEqual(picnicsource._mark(None, None, None, "memorial", "Pomník padlým")[1],
                         {"historic": "memorial", "description": "Pomník padlým"})
        self.assertEqual(picnicsource._mark(None, None, None, "monument")[0], "monument")
        self.assertIsNone(picnicsource._mark(None, None, None, "ruins"))
        import picnic

        class F:
            attrs = {"class": np.array(["wayside_cross"]), "tags": np.array(['{"historic": "wayside_cross"}'])}
        self.assertIsNone(picnic.marked(F(), 0))
        self.assertEqual(localpoi._kind(F(), 0), "wayside_cross")

    def test_the_cross_a_country_puts_up(self):
        counts = {g: 0 for g in localpoi.CROSS_GROUPS}
        for i in range(3000):
            counts[localpoi.cross_group(f"id{i}", "CZ")] += 1
        self.assertGreater(counts[localpoi.G_CROSS_STONE], 0.6 * 3000)
        self.assertEqual(localpoi.cross_group("w123", "CZ"), localpoi.cross_group("w123", "cz"))
        pl = {g: 0 for g in localpoi.CROSS_GROUPS}
        for i in range(3000):
            pl[localpoi.cross_group(f"id{i}", "PL")] += 1
        self.assertGreater(pl[localpoi.G_CROSS_WOOD], pl[localpoi.G_CROSS_STONE])
        self.assertIn(localpoi.cross_group("x", None), localpoi.CROSS_GROUPS)

    def test_collision_boxes_are_registered(self):
        for group in (localpoi.G_TABLE, localpoi.G_ALTAN, localpoi.G_FIREPIT, *localpoi.CROSS_GROUPS, localpoi.G_SHRINE):
            self.assertIn(group, furniture.COLLISION_BOXES)
            self.assertEqual(furniture.COLLISION_BOXES[group][0], localpoi.HEIGHT[group])

    def test_catalog_and_models(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        lua = os.path.join(root, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua")
        with open(lua, encoding="utf-8") as f:
            source = f.read()
        shapes = os.path.join(root, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                              "localpoi")
        for group, model in (("lp_table", "bne_lp_table_forest"), ("lp_altan", "bne_lp_altan"),
                             ("lp_firepit", "bne_lp_firepit"), ("lp_cross_stone", "bne_lp_cross_stone"),
                             ("lp_cross_wood", "bne_lp_cross_wood"), ("lp_cross_metal", "bne_lp_cross_metal"),
                             ("lp_shrine", "bne_lp_shrine_column")):
            self.assertIn(f"M.CATALOG.{group} = {{ {{ LP .. '{model}', {localpoi.HEIGHT[group]} }} }}", source)
            self.assertTrue(os.path.exists(os.path.join(shapes, model + ".dae")), model)
        self.assertTrue(os.path.exists(os.path.join(shapes, "main.materials.json")))


if __name__ == "__main__":
    unittest.main()
