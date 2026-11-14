"""Stock world props (worldprops.py): towers, containers, blank billboards and barns on a road tile."""
import json
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import billboardsource  # noqa: E402
import geodesy  # noqa: E402
import mapgeom as mg  # noqa: E402
import worldprops as wp  # noqa: E402

FRAME = geodesy.EnuFrame(40.44, -80.00)


def _ll(xy):
    xy = np.asarray(xy, np.float64)
    la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    return np.stack([lo, la], 1)


def feats(layer, rows, kind):
    rows = sorted(rows, key=lambda r: r[0])
    geoms = []
    for _, xy, _ in rows:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        geoms.append((kind, [[_ll(xy)]]))
    f = mg.build(layer, np.array([r[0] for r in rows]), geoms)
    keys = sorted({k for r in rows for k in r[2]})
    for k in keys:
        vals = [r[2].get(k) for r in rows]
        if any(isinstance(v, str) for v in vals):
            f.attrs[k] = np.array([v if v is not None else "" for v in vals], object)
        else:
            f.attrs[k] = np.array([np.nan if v is None else v for v in vals], np.float64)
    return f


def box(cx, cy, w, h):
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2),
            (cx - w / 2, cy + h / 2), (cx - w / 2, cy - h / 2)]


class FakeMD:
    def __init__(self, layers):
        self.layers = layers

    def cell(self, key, layers=None, kind="demand"):
        return type("C", (), {"layers": {k: self.layers.get(k) for k in (layers or ())}})()


def elev(la, lo):
    return np.full(np.shape(la), 5.0)


class WorldPropsTest(unittest.TestCase):
    # Mapped street lamps (PR #981 by wyo-backroads): OSM nodes, the stock post by lamp_mount, the arm toward the road.
    def lamps(self, md, tx=0, ty=0):
        out = []
        rows, _ = wp.tile_props(FRAME, tx, ty, md, elev, lamps=out)
        return rows, out

    def test_mapped_lamps_guess_none_along_their_road_only(self):
        import furniture
        from test_furniture import LampTest, road
        mapped, unmapped = road("mapped", "residential", True, y=50.), road("unmapped", "residential", True, y=200.)
        self.assertEqual(furniture.roads_with_points([mapped, unmapped], [[100., 55.], [160., 46.]], 15.), {"mapped"})
        self.assertEqual(furniture.roads_with_points([mapped, unmapped], [[100., 55.]], 15.), set(),
                         "one stray lamp leaves the road its guessed ones")
        walks = [w for r in (mapped, unmapped) for s in (1, -1) for w in LampTest().walks(r, s)]
        lamp = ["streetlight", 100., 55.5, 0., 270., 1., 9., 0.]
        rows = furniture.place([mapped, unmapped], walks, (0, 0, 256, 256),
                               mapped_lamps=[lamp, ["streetlight", 160., 45., 0., 90., 1., 9., 0.],
                                             ["streetlight", 300., 45., 0., 90., 1., 9., 0.]])
        posts = [r for r in rows if r[0] == furniture.G_LAMP]
        self.assertEqual(sorted((r[1], r[2]) for r in posts if r[2] < 100), [(100., 55.5), (160., 45.)],
                         "the mapped road: its two own mapped lamps (the one off the tile left to its owner)")
        self.assertTrue([r for r in posts if r[2] > 150], "the unmapped road keeps its guessed lamps")
        plain = furniture.place([mapped, unmapped], walks, (0, 0, 256, 256))
        self.assertEqual([r for r in rows if r[0] == furniture.G_POLE], [r for r in plain if r[0] == furniture.G_POLE],
                         "its utility poles stay where they were")
        self.assertFalse([r for r in rows if r[0] == furniture.G_POLELAMP and r[2] < 100], "without their lamps")
        # a town street without sidewalks: its lamps on its utility poles; mapped lamps: the poles alone
        lamps2 = [lamp, ["streetlight", 160., 45., 0., 90., 1., 9., 0.]]
        edge, edge_plain = (furniture.place([mapped], [], (0, 0, 256, 256), mapped_lamps=m) for m in (lamps2, None))
        self.assertTrue([r for r in edge_plain if r[0] == furniture.G_POLELAMP])
        self.assertEqual([r for r in edge if r[0] == furniture.G_POLE], [r for r in edge_plain if r[0] == furniture.G_POLE])
        self.assertFalse([r for r in edge if r[0] == furniture.G_POLELAMP])
        self.assertEqual(len([r for r in rows if r[0] == furniture.G_LENS and r[2] < 100]), 2)
        self.assertEqual(furniture.place([mapped], walks[:2], (0, 0, 256, 256), mapped_lamps=None),
                         furniture.place([mapped], walks[:2], (0, 0, 256, 256)))

    def test_mapped_lamps_face_road_on_both_sides_and_ignore_footpath(self):
        lamps = feats("infrastructure", [
            ("west", (100., 120.), {"class": "street_lamp", "src_record": "n101@1",
                "tags": json.dumps({"highway": "street_lamp", "lamp_mount": "straight_mast", "direction": "270"})}),
            ("east", (120., 120.), {"class": "street_lamp", "src_record": "n102@1",
                "tags": json.dumps({"highway": "street_lamp", "lamp_mount": "straight_mast", "direction": "90"})}),
        ], mg.POINT)
        road_feats = feats("roads", [
            ("road", [(110., 0.), (110., 256.)], {"class": "residential"}),
            ("path", [(99., 0.), (99., 256.)], {"class": "footway"}),
        ], mg.LINE)
        _, out = self.lamps(FakeMD({"infrastructure": lamps, "roads": road_feats}))
        poles = sorted((r for r in out if r[0] == "streetlight_straight_mast"), key=lambda r: r[1])
        self.assertAlmostEqual(poles[0][4], 90., places=1)
        self.assertAlmostEqual(poles[1][4], 270., places=1)
        import furniture
        lenses = sorted((r for r in furniture.lamp_lenses(out) if r[0] == "lamplens"), key=lambda r: r[1])
        self.assertGreater(lenses[0][1], poles[0][1], "the west lamp's head reaches east, over the road")
        self.assertLess(lenses[1][1], poles[1][1])
        # a road just across the tile's edge still turns the lamp (an ordinary post: its arm along +x)
        road_feats = feats("roads", [("edge", [(260., 0.), (260., 256.)], {"class": "motorway"})], mg.LINE)
        lamps = feats("infrastructure", [("edge", (254., 100.), {"class": "street_lamp", "src_record": "n103@1"})],
                      mg.POINT)
        _, out = self.lamps(FakeMD({"infrastructure": lamps, "roads": road_feats}))
        self.assertAlmostEqual(next(r for r in out if r[0] == "streetlight")[4], 0., places=1)

    def test_lamp_rotation_curved_road_double_arm_and_layers(self):
        import fast
        fast.require(199, "test")

        def yaw(point, kind, lines, layer=math.nan):
            off = np.zeros(len(lines) + 1, np.int64)
            off[1:] = np.cumsum([len(l) for l, _ in lines])
            return fast._ef.lamp_road_yaw(np.array([point], float), np.array([layer]), np.array([kind], np.int64),
                                          np.concatenate([l for l, _ in lines]).astype(float), off,
                                          np.array([lv for _, lv in lines], float), wp.LAMP_ROAD_M)[0]
        self.assertAlmostEqual(yaw((110., 80.), 0, [(np.array([[0., 0.], [100., 0.], [100., 100.]]), math.nan)]), 180.)
        diagonal = np.array([[0., 0.], [100., 100.]])
        for g in (diagonal, diagonal[::-1]):    # a median's double arm crosses the carriageway
            self.assertAlmostEqual(yaw((50., 50.), 2, [(g, math.nan)]), 135.)
        layered = [(np.array([[101., 0.], [101., 200.]]), 1.), (np.array([[90., 0.], [90., 200.]]), 0.)]
        self.assertAlmostEqual(yaw((100., 100.), 0, layered, 0.), 180., msg="the lamp's own layer's road")
        self.assertAlmostEqual(yaw((100., 100.), 0, layered), 0., msg="no layer: the nearest road")
        self.assertTrue(math.isnan(yaw((500., 500.), 0, layered)), "no road in reach: the tag's direction")

    def test_mapped_lamp_mounts_have_lighting_at_each_head(self):
        import furniture
        infra = feats("infrastructure", [
            ("double", (254., 100.), {"class": "street_lamp", "src_record": "n901@1",
                "tags": json.dumps({"highway": "street_lamp", "lamp_mount": "double_arm"})}),
            ("high", (100., 100.), {"class": "street_lamp", "src_record": "n902@1",
                "tags": json.dumps({"highway": "street_lamp", "lamp_mount": "high_mast"})}),
            ("straight", (160., 100.), {"class": "street_lamp", "src_record": "n903@1",
                "tags": json.dumps({"highway": "street_lamp", "lamp_mount": "straight_mast", "direction": "90"})}),
            ("off", (180., 180.), {"class": "street_lamp", "src_record": "n904@1",
                "tags": json.dumps({"highway": "street_lamp", "lamp_mount": "double_arm", "disused": "yes"})}),
        ], mg.POINT)
        md = FakeMD({"infrastructure": infra})
        rows, out = self.lamps(md)
        self.assertEqual(rows, [], "the lamps are furniture.place's to draw")
        poles = {r[0]: r for r in out}
        self.assertEqual(set(poles), {"streetlight_double_arm", "streetlight_high_mast", "streetlight_straight_mast"})
        self.assertEqual(poles["streetlight_straight_mast"][4], 90.)
        self.assertEqual(poles["streetlight_straight_mast"][6], wp.STRAIGHT_MAST_H)
        self.assertEqual(poles["streetlight_double_arm"][6], wp.DOUBLE_ARM_H)
        lit = furniture.lamp_lenses(out)
        ordinary = [r for r in lit if r[0] == "lamplens"]
        flood = [r for r in lit if r[0] == furniture.G_HIGH_MAST_LENS]
        self.assertEqual(len(ordinary), 3)  # straight + both double arms
        self.assertEqual(len(flood), 1)
        double = [r for r in ordinary if r[1] > 200]
        self.assertEqual(sorted(r[1] for r in double), [249.1, 258.9])
        self.assertEqual(sorted(r[4] for r in double), [0., 180.])
        for r in double:
            self.assertAlmostEqual(r[3] - poles["streetlight_double_arm"][3], 12.62, places=2)
        self.assertEqual(flood[0][3], 22.9)
        glow = [r for r in lit if r[0] == "lampglow"]
        self.assertEqual(sorted(tuple(r[1:5]) for r in glow), sorted(tuple(r[1:5]) for r in ordinary + flood))
        # the next tile sees the double arm (within LAMP_NEAR_M) but furniture.place leaves it to its owner
        _, near = self.lamps(md, 1, 0)
        self.assertEqual([r[0] for r in near], ["streetlight_double_arm"])
        self.assertEqual([r for r in furniture.place([], [], (256., 0., 512., 256.), mapped_lamps=near)
                          if r[0].startswith("streetlight")], [])

    def test_mapped_lamps_are_osm_nodes_only(self):
        infra = feats("infrastructure", [
            ("a", (100., 120.), {"class": "street_lamp", "src_record": "n123@1"}),
            ("b", (140., 120.), {"class": "street_lamp", "src_record": "w456@1"}),
            ("c", (180., 120.), {"class": "street_lamp"}),
            ("d", (200., 120.), {"class": "street_lamp", "src_record": "n789@1",
                                  "tags": json.dumps({"highway": "street_lamp", "lamp_mount": "high_mast"})}),
            ("e", (60., 120.), {"class": "utility_pole", "src_record": "n555@1",
                                 "tags": json.dumps({"highway": "street_lamp"})}),
        ], mg.POINT)
        md = FakeMD({"infrastructure": infra})
        _, out = self.lamps(md)
        lamps = [r for r in out if r[0] == "streetlight"]
        self.assertEqual(len(lamps), 1)
        self.assertEqual(lamps[0][1:3], [100., 120.])
        high = [r for r in out if r[0] == "streetlight_high_mast"]
        self.assertEqual(len(high), 1)
        self.assertEqual(high[0][1:3], [200., 120.])
        self.assertEqual(high[0][3], 5.)
        self.assertEqual(high[0][6], wp.HIGH_MAST_H)
        self.assertEqual(wp.tile_props(FRAME, 0, 0, md, elev), ([], set()), "lamps=None: none made")
        self.assertEqual(self.lamps(md, 2, 0)[1], [])

    def md(self):
        infra = feats("infrastructure", [
            ("cell", (100.0, 120.0), {"class": "communication_tower"}),
            ("water", (130.0, 120.0), {"class": "water_tower"}),
            ("pylon", (160.0, 120.0), {"class": "power_tower"}),
            ("off", (300.0, 40.0), {"class": "mobile_phone_tower"}),
            ("sign", (100.0, 80.0), {"class": "tower", "tags": json.dumps({"advertising": "billboard"})}),
            ("poster", (140.0, 80.0), {"class": "tower", "tags": json.dumps({"advertising": "poster"})}),
        ], mg.POINT)
        land = feats("land_use", [
            ("yard", box(40.0, 200.0, 36.0, 72.0), {"class": "industrial", "tags": ""}),
            ("tiny", box(200.0, 40.0, 10.0, 10.0), {"class": "works", "tags": ""}),
        ], mg.POLYGON)
        blds = feats("buildings", [
            ("barn-1", box(80.0, 40.0, 10.36, 6.64), {"class": "barn"}),
            ("barn-big", box(180.0, 40.0, 40.0, 25.0), {"class": "barn"}),
            ("shed", box(40.0, 200.0, 8.0, 8.0), {"class": "industrial"}),
        ], mg.POLYGON)
        roads = feats("roads", [
            ("rd", [(150.0, 70.0), (150.0, 90.0)], {"class": "residential"}),
        ], mg.LINE)
        return FakeMD({"infrastructure": infra, "land_use": land, "buildings": blds, "roads": roads})

    def rows(self, tx=0, ty=0):
        rows, skip = wp.tile_props(FRAME, tx, ty, self.md(), elev)
        return rows, skip

    def by(self, rows, group):
        return [r for r in rows if r[0] == group]

    def test_towers(self):
        rows, _ = self.rows()
        cell = self.by(rows, "celltower")
        water = self.by(rows, "watertower")
        self.assertEqual(len(cell), 1)
        self.assertEqual(len(water), 1)
        self.assertAlmostEqual(cell[0][3], 5.02, places=2)
        self.assertAlmostEqual(cell[0][6], wp.CELL_H, places=2)
        self.assertAlmostEqual(water[0][3], 5.0 + wp.WATER_LIFT, places=2)
        self.assertAlmostEqual(water[0][6], wp.WATER_H, places=2)
        self.assertFalse(any(r[0] == "pylon" for r in rows))
        other, _ = self.rows(1, 0)
        self.assertEqual(len(self.by(other, "celltower")), 1)
        self.assertAlmostEqual(self.by(other, "celltower")[0][1], 300.0, delta=1.0)
        self.assertEqual(len(self.by(rows, "celltower")), 1)

    def test_water_towers_at_their_mapped_height(self):
        # GitHub #887: a 45 m town tower is the model scaled to 45 m (its feet still on the ground), a tag's height
        # counts too, and an implausible one is held to WATER_SCALE
        infra = feats("infrastructure", [
            ("w1", (100.0, 120.0), {"class": "water_tower", "height": 45.0}),
            ("w2", (130.0, 120.0), {"class": "water_tower", "tags": json.dumps({"height": "15 m"})}),
            ("w3", (160.0, 120.0), {"class": "water_tower", "height": 400.0}),
            ("w4", (190.0, 120.0), {"class": "water_tower"}),
        ], mg.POINT)
        md = FakeMD({"infrastructure": infra})
        rows, _ = wp.tile_props(FRAME, 0, 0, md, elev)
        water = sorted(self.by(rows, "watertower"), key=lambda r: r[1])
        self.assertEqual(len(water), 4)
        s = [45.0 / wp.WATER_H, 15.0 / wp.WATER_H, wp.WATER_SCALE[1], 1.0]
        for r, k in zip(water, s):
            self.assertAlmostEqual(r[6], wp.WATER_H * k, places=1)
            self.assertAlmostEqual(r[3], 5.0 + wp.WATER_LIFT * k, places=1)

    def test_containers_line_up_and_stay_out_of_buildings(self):
        rows, _ = self.rows()
        boxes = self.by(rows, "container")
        self.assertGreaterEqual(len(boxes), 1)
        self.assertLessEqual(len(boxes), 8)
        from polygon import Path
        yard = Path(np.array(box(40.0, 200.0, 36.0, 72.0)))
        shed = Path(np.array(box(40.0, 200.0, 8.0, 8.0)))
        pts = np.array([[r[1], r[2]] for r in boxes])
        self.assertTrue(bool(yard.contains_points(pts).all()))
        self.assertFalse(bool(shed.contains_points(pts).any()))
        yaw = math.radians(boxes[0][4])
        local_y = np.array([-math.sin(yaw), math.cos(yaw)])
        self.assertAlmostEqual(abs(local_y[1]), 1.0, places=2)
        self.assertAlmostEqual(boxes[0][6], wp.CONT_H, places=2)

    def test_billboard_faces_the_road(self):
        rows, _ = self.rows()
        signs = self.by(rows, "billboard")
        self.assertEqual(len(signs), 1)
        yaw = signs[0][4]
        self.assertAlmostEqual(yaw % 360.0, 270.0, places=0)
        ox, oy = wp._local_offset(yaw, wp.BILL_OX, wp.BILL_OY)
        self.assertAlmostEqual(signs[0][1] + ox, 100.0, delta=0.05)
        self.assertAlmostEqual(signs[0][2] + oy, 80.0, delta=0.05)
        self.assertAlmostEqual(signs[0][6], wp.BILL_H, places=2)

    def test_an_osm_billboard_is_placed(self):
        md = self.md()
        lat, lon, _ = FRAME.enu_to_geodetic(np.array([50.0]), np.array([50.0]), np.zeros(1))
        got = billboardsource.features_of(np.array([9]), lat, lon)
        md.billboard_osm = type("S", (), {"cells": lambda self, keys: [got]})()
        rows, _ = wp.tile_props(FRAME, 0, 0, md, elev)
        signs = self.by(rows, "billboard")
        self.assertEqual(len(signs), 2)
        self.assertTrue(any(abs(r[1] - 50.0) < 15.0 and abs(r[2] - 50.0) < 15.0 for r in signs))

    def test_barn_replaces_a_fitting_footprint(self):
        rows, skip = self.rows()
        barns = self.by(rows, "barn")
        self.assertEqual(len(barns), 1)
        self.assertIn("barn-1", skip)
        self.assertNotIn("barn-big", skip)
        self.assertAlmostEqual(barns[0][6], wp.BARN_H, delta=0.05)
        self.assertAlmostEqual(barns[0][3], 5.0 + wp.BARN_LIFT, delta=0.05)
        yaw = math.radians(barns[0][4])
        self.assertAlmostEqual(abs(math.cos(yaw)), 1.0, places=2)
