"""Picnic tables and grills in parks (picnic.py)."""
import json
import math
import os
import struct
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy
import mapgeom
import picnic
import picnicsource


def _square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


class KindTest(unittest.TestCase):
    def features(self, cls, tags=None, subtype="park"):
        attrs = {"subtype": np.array([subtype]), "class": np.array([cls])}
        if tags is not None:
            attrs["tags"] = np.array([json.dumps(tags)])
        return type("F", (), {"attrs": attrs})()

    def test_parks_and_picnic_marks(self):
        self.assertTrue(picnic.is_park(self.features("park"), 0))
        self.assertTrue(picnic.is_park(self.features("national_park"), 0))
        self.assertTrue(picnic.is_park(self.features("land", {"leisure": "park"}, subtype="land"), 0))
        self.assertFalse(picnic.is_park(self.features("recreation_ground", subtype="recreation"), 0))
        self.assertEqual(picnic.marked(self.features("picnic_table"), 0), "table")
        self.assertEqual(picnic.marked(self.features("amenity", {"amenity": "picnic_table"}), 0), "table")
        self.assertEqual(picnic.marked(self.features("land", {"tourism": "picnic_site"}), 0), "site")
        self.assertEqual(picnic.marked(self.features("amenity", {"amenity": "bbq"}), 0), "grill")
        self.assertEqual(picnic.marked(self.features("amenity", {"amenity": "barbecue"}), 0), "grill")
        self.assertIsNone(picnic.marked(self.features("bench", {"amenity": "bench"}), 0))
        self.assertIsNone(picnic.marked(self.features("park", {"leisure": "park"}), 0))


class PlaceTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(39.2834, -76.5934, 0.0)

    def feats(self, items):
        """items: (id, kind, rings of xy, class, tags). Rings are lists of [x, y]."""
        ids, kinds, parts, coords = [], [], [], []
        classes, subtypes, tags = [], [], []
        for fid, kind, rings, cls, tag in items:
            ids.append(fid)
            kinds.append(kind)
            parts.append(rings)
            classes.append(cls)
            subtypes.append("park" if cls in picnic.PARK_CLASSES else "infrastructure")
            tags.append(json.dumps(tag or {}))
        order = np.argsort(np.array(ids))
        ids = [ids[i] for i in order]
        kinds = [kinds[i] for i in order]
        parts = [parts[i] for i in order]
        classes = [classes[i] for i in order]
        subtypes = [subtypes[i] for i in order]
        tags = [tags[i] for i in order]
        lonlat, part_off, ring_off, coord_off = [], [0], [0], [0]
        for rings in parts:
            part_off.append(part_off[-1] + len(rings))
            for ring in rings:
                xy = np.asarray(ring, np.float64)
                lat, lon, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
                lonlat.append(np.column_stack((lon, lat)))
                ring_off.append(ring_off[-1] + 1)
                coord_off.append(coord_off[-1] + len(xy))
        return mapgeom.Features(
            layer="land_use", ids=np.array(ids), kind=np.array(kinds, np.uint8),
            part_off=np.array(part_off, np.int64), ring_off=np.array(ring_off, np.int64),
            coord_off=np.array(coord_off, np.int64),
            lonlat=np.vstack(lonlat) if lonlat else np.zeros((0, 2)),
            attrs={"subtype": np.array(subtypes), "class": np.array(classes), "tags": np.array(tags)})

    def rows(self, land, infra=None, box=(0.0, 0.0, 100.0, 100.0), osm=None, bld=None):
        return picnic.rows(self.frame, (lambda key: infra) if infra is not None else None,
                           (lambda key: land) if land is not None else None, box,
                           lambda la, lo: np.full(np.shape(la), 5.0),
                           (lambda keys: [osm]) if osm is not None else None,
                           (lambda key: bld) if bld is not None else None)

    def footprint(self, fid, rings, cls="yes", height=None):
        """One building polygon. rings[0] is the outer ring; later rings are holes."""
        lonlat, coord_off = [], [0]
        for ring in rings:
            xy = np.asarray(ring, np.float64)
            lat, lon, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
            lonlat.append(np.column_stack((lon, lat)))
            coord_off.append(coord_off[-1] + len(xy))
        attrs = {"class": np.array([cls])}
        if height is not None:
            attrs["height"] = np.array([height], np.float64)
        return mapgeom.Features(
            layer="buildings", ids=np.array([fid]), kind=np.array([mapgeom.POLYGON], np.uint8),
            part_off=np.array([0, 1], np.int64), ring_off=np.array([0, len(rings)], np.int64),
            coord_off=np.array(coord_off, np.int64), lonlat=np.vstack(lonlat), attrs=attrs)

    def _wkb(self, x, y):
        lat, lon, _ = self.frame.enu_to_geodetic(np.array([x]), np.array([y]), np.array([0.0]))
        return struct.pack("<BIdd", 1, 1, float(lon[0]), float(lat[0]))

    def test_a_picnic_table_in_a_park_gets_a_table_and_a_grill(self):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(0, 0, 80, 80)], "park", {"leisure": "park"})])
        table = self.feats([("tbl-1", mapgeom.POINT, [[[40.0, 40.0]]], "picnic_table", {"amenity": "picnic_table"})])
        got = self.rows(park, table)
        self.assertEqual([r[0] for r in got], ["picnictable", "picnicgrill"])
        self.assertEqual(got[0][6], picnic.TABLE_H)
        self.assertEqual(got[1][6], picnic.GRILL_H)
        self.assertTrue(all(abs(r[3] - 5.0) < 0.02 for r in got))
        yaw = math.radians(got[0][4])
        gx = 40.0 + picnic.GRILL_OFF * -math.sin(yaw)
        gy = 40.0 + picnic.GRILL_OFF * math.cos(yaw)
        ox, oy = picnic._grill_origin(gx, gy, (got[0][4] + 90.0) % 360.0)
        self.assertAlmostEqual(got[1][1], ox, delta=0.02)
        self.assertAlmostEqual(got[1][2], oy, delta=0.02)
        self.assertAlmostEqual(got[0][1], 40.0, delta=0.02)
        self.assertAlmostEqual(got[0][2], 40.0, delta=0.02)

    def test_an_osm_table_in_a_park_is_placed(self):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(0, 0, 80, 80)], "park", {"leisure": "park"})])
        osm = picnicsource.features_of([(7, "picnic_table", None, None, self._wkb(40.0, 40.0))])
        got = self.rows(park, osm=osm)
        self.assertEqual([r[0] for r in got], ["picnictable", "picnicgrill"])
        self.assertAlmostEqual(got[0][1], 40.0, delta=0.05)
        self.assertAlmostEqual(got[0][2], 40.0, delta=0.05)
        self.assertIsNone(picnicsource._mark("bench", "bench", None))

    def test_a_table_outside_a_park_is_left_out(self):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(0, 0, 80, 80)], "park", {"leisure": "park"})])
        table = self.feats([("tbl-1", mapgeom.POINT, [[[90.0, 40.0]]], "picnic_table", None)])
        self.assertEqual(self.rows(park, table), [])

    def test_a_recreation_ground_is_not_a_park(self):
        ground = self.feats([("rec-1", mapgeom.POLYGON, [_square(0, 0, 80, 80)], "recreation_ground", None)])
        table = self.feats([("tbl-1", mapgeom.POINT, [[[40.0, 40.0]]], "picnic_table", None)])
        self.assertEqual(self.rows(ground, table), [])

    def test_a_mapped_grill_in_a_park(self):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(0, 0, 80, 80)], "national_park", None)])
        grill = self.feats([("bbq-1", mapgeom.POINT, [[[20.0, 30.0]]], "amenity", {"amenity": "bbq"})])
        got = self.rows(park, grill)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "picnicgrill")
        self.assertLess(math.hypot(got[0][1] - 20.0, got[0][2] - 30.0), 1.0)

    def test_a_picnic_area_in_a_park_is_furnished(self):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(0, 0, 80, 80)], "park", {"leisure": "park"})])
        site = self.feats([("site-1", mapgeom.POLYGON, [_square(10, 10, 50, 50)], "land",
                            {"tourism": "picnic_site"})])
        got = self.rows(park, site)
        tables = [r for r in got if r[0] == "picnictable"]
        grills = [r for r in got if r[0] == "picnicgrill"]
        self.assertGreaterEqual(len(tables), 1)
        self.assertLessEqual(len(tables), picnic.SITE_MAX)
        self.assertEqual(len(tables), len(grills))
        self.assertTrue(all(10.0 < r[1] < 50.0 and 10.0 < r[2] < 50.0 for r in tables))

    def test_a_whole_park_tagged_as_a_picnic_site_is_not_filled(self):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(0, 0, 200, 200)], "park",
                            {"leisure": "park", "tourism": "picnic_site"})])
        self.assertEqual(self.rows(park, None, box=(0.0, 0.0, 256.0, 256.0)), [])

    def test_a_table_past_the_tile_is_left_out(self):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(-20, 0, 80, 80)], "park", None)])
        table = self.feats([("tbl-1", mapgeom.POINT, [[[-5.0, 40.0]]], "picnic_table", None)])
        self.assertEqual(self.rows(park, table), [])

    def test_catalog_names_the_stock_shapes(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("M.CATALOG.picnictable", source)
        self.assertIn("ut_picnic_table_a", source)
        self.assertIn("M.CATALOG.picnicgrill", source)
        self.assertIn("eca_BBQ", source)
        self.assertNotIn("M.NO_COLLIDE.picnictable", source)
        self.assertNotIn("M.NO_COLLIDE.picnicgrill", source)

    def _park_and_table(self, xy=(40.0, 40.0), fid="tbl-1"):
        park = self.feats([("park-1", mapgeom.POLYGON, [_square(0, 0, 80, 80)], "park", {"leisure": "park"})])
        table = self.feats([(fid, mapgeom.POINT, [[list(xy)]], "picnic_table", {"amenity": "picnic_table"})])
        return park, table

    def test_a_table_inside_a_building_is_left_out(self):
        park, table = self._park_and_table()
        bld = self.footprint("house-1", [_square(30, 30, 50, 50)])
        self.assertEqual(self.rows(park, table, bld=bld), [])

    def test_a_table_in_a_courtyard_is_placed(self):
        park, table = self._park_and_table()
        bld = self.footprint("house-1", [_square(15, 15, 65, 65), _square(30, 30, 50, 50)])
        got = self.rows(park, table, bld=bld)
        self.assertEqual([r[0] for r in got], ["picnictable", "picnicgrill"])

    def test_a_grill_inside_a_building_is_left_out(self):
        fid = "tbl-edge"
        yaw = picnic._yaw_of(fid, "table")
        rad = math.radians(yaw)
        ux, uy = -math.sin(rad), math.cos(rad)
        px, py = -uy, ux
        cx, cy, c, hw, hl = 40.0, 40.0, 4.0, 2.0, 2.0
        ring = [[cx + (c + su) * ux + sp * px, cy + (c + su) * uy + sp * py]
                for su, sp in ((-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl), (-hw, -hl))]
        park, table = self._park_and_table(fid=fid)
        got = self.rows(park, table, bld=self.footprint("house-1", [ring]))
        self.assertEqual([r[0] for r in got], ["picnictable"])

    def test_a_table_inside_a_building_drops_only_the_table_when_the_grill_is_outside(self):
        fid = "tbl-in"
        yaw = picnic._yaw_of(fid, "table")
        rad = math.radians(yaw)
        ux, uy = -math.sin(rad), math.cos(rad)
        px, py = -uy, ux
        cx, cy, c, hw, hl = 40.0, 40.0, -1.0, 2.5, 1.6
        ring = [[cx + (c + su) * ux + sp * px, cy + (c + su) * uy + sp * py]
                for su, sp in ((-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl), (-hw, -hl))]
        park, table = self._park_and_table(fid=fid)
        got = self.rows(park, table, bld=self.footprint("house-1", [ring]))
        self.assertEqual([r[0] for r in got], ["picnicgrill"])
        gx = 40.0 + picnic.GRILL_OFF * -math.sin(rad)
        gy = 40.0 + picnic.GRILL_OFF * math.cos(rad)
        self.assertLess(math.hypot(got[0][1] - gx, got[0][2] - gy), 1.0)

    def test_a_roof_and_a_flat_area_are_not_buildings(self):
        park, table = self._park_and_table()
        roof = self.footprint("roof-1", [_square(30, 30, 50, 50)], cls="roof")
        flat = self.footprint("plaza-1", [_square(30, 30, 50, 50)], height=0.05)
        self.assertEqual([r[0] for r in self.rows(park, table, bld=roof)], ["picnictable", "picnicgrill"])
        self.assertEqual([r[0] for r in self.rows(park, table, bld=flat)], ["picnictable", "picnicgrill"])
