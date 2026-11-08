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
    def test_mapped_lamp_rule_is_global_and_preserves_unmapped_road_fallback(self):
        import furniture
        import roads
        spec=roads.Spec(8.,"paved",True,"residential",False)
        network=[("mapped",spec,np.array([[0.,50.,0.],[256.,50.,0.]]),0.),
                 ("unmapped",spec,np.array([[0.,220.,0.],[256.,220.,0.]]),0.)]
        self.assertEqual(furniture.mapped_lighting(network,{"unmapped":"yes"},[[100.,55.]]),
                         {"mapped":"no","unmapped":"yes"})
        self.assertEqual(furniture.mapped_lighting(network,{"mapped":"yes"},[]),{"mapped":"yes"})

    def test_mapped_lamps_face_road_on_both_sides_and_ignore_footpath(self):
        lamps = feats("infrastructure", [
            ("west", (100.,120.), {"class":"street_lamp", "src_record":"n101@1",
                "tags":json.dumps({"highway":"street_lamp","lamp_mount":"straight_mast","direction":"270"})}),
            ("east", (120.,120.), {"class":"street_lamp", "src_record":"n102@1",
                "tags":json.dumps({"highway":"street_lamp","lamp_mount":"straight_mast","direction":"90"})}),
        ], mg.POINT)
        roads = feats("roads", [
            ("road", [(110.,0.),(110.,256.)], {"class":"residential"}),
            ("path", [(99.,0.),(99.,256.)], {"class":"footway"}),
        ],mg.LINE)
        rows,_ = wp.tile_props(FRAME,0,0,FakeMD({"infrastructure":lamps,"roads":roads}),elev,mapped_lamps=True)
        poles=sorted((r for r in rows if r[0]=="streetlight_straight_mast"),key=lambda r:r[1])
        self.assertAlmostEqual(poles[0][4],90.,places=1)
        self.assertAlmostEqual(poles[1][4],270.,places=1)
        lenses=sorted((r for r in rows if r[0]=="lamplens"),key=lambda r:r[1])
        self.assertGreater(lenses[0][1],poles[0][1])
        self.assertLess(lenses[1][1],poles[1][1])
        # Adjacent tiles must see the road just across their boundary.
        roads = feats("roads",[("edge",[(260.,0.),(260.,256.)],{"class":"motorway"})],mg.LINE)
        lamps = feats("infrastructure",[("edge",(254.,100.),{"class":"street_lamp","src_record":"n103@1"})],mg.POINT)
        rows,_=wp.tile_props(FRAME,0,0,FakeMD({"infrastructure":lamps,"roads":roads}),elev,mapped_lamps=True)
        self.assertAlmostEqual(next(r for r in rows if r[0]=="streetlight")[4],0.,places=1)

    def test_lamp_rotation_uses_curved_road_segment_and_double_arm_normal(self):
        line=np.array([[0.,0.],[100.,0.],[100.,100.]])
        sources=[("curve",line,None)]
        self.assertAlmostEqual(wp._lamp_yaw(np.array([110.,80.]),"streetlight",{},sources),180.)
        # Median arms cross the carriageway even if a segment's endpoint is nearest.
        diagonal=np.array([[0.,0.],[100.,100.]])
        for geometry in (diagonal,diagonal[::-1]):
            yaw=wp._lamp_yaw(np.array([50.,50.]),"streetlight_double_arm",{},[("road",geometry,None)])
            self.assertAlmostEqual(yaw,135.)
        layered=[("bridge",np.array([[101.,0.],[101.,200.]]),1.),
                 ("ground",np.array([[90.,0.],[90.,200.]]),0.)]
        self.assertAlmostEqual(wp._lamp_yaw(np.array([100.,100.]),"streetlight",{"layer":"0"},layered),180.)

    def test_mapped_lamp_mounts_have_lighting_at_each_head(self):
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
        rows, _ = wp.tile_props(FRAME, 0, 0, md, elev, mapped_lamps=True)
        poles = {r[0]:r for r in rows if r[0].startswith("streetlight")}
        self.assertEqual(set(poles), {"streetlight_double_arm", "streetlight_high_mast", "streetlight_straight_mast"})
        self.assertEqual(poles["streetlight_straight_mast"][4], 90.)
        self.assertEqual(poles["streetlight_straight_mast"][6], wp.STRAIGHT_MAST_H)
        self.assertEqual(poles["streetlight_double_arm"][6], wp.DOUBLE_ARM_H)
        ordinary = [r for r in rows if r[0] == "lamplens"]
        flood = [r for r in rows if r[0] == "high_mast_light"]
        self.assertEqual(len(ordinary), 3)  # straight + both double arms
        self.assertEqual(len(flood), 1)
        double = [r for r in ordinary if r[1] > 200]
        self.assertEqual(sorted(r[1] for r in double), [249.1, 258.9])
        self.assertEqual(sorted(r[4] for r in double), [0., 180.])
        for r in double:
            self.assertAlmostEqual(r[3]-poles["streetlight_double_arm"][3], 12.62, places=2)
        self.assertEqual(flood[0][3], 22.9)
        glow = [r for r in rows if r[0] == "lampglow"]
        self.assertEqual(sorted(tuple(r[1:5]) for r in glow), sorted(tuple(r[1:5]) for r in ordinary+flood))
        # A head crosses the tile boundary, but belongs to its pole's sole tile.
        self.assertEqual(wp.tile_props(FRAME, 1, 0, md, elev, mapped_lamps=True)[0], [])

    def test_mapped_lamps_are_osm_nodes_only(self):
        infra = feats("infrastructure", [
            ("a", (100., 120.), {"class": "street_lamp", "src_record": "n123@1"}),
            ("b", (140., 120.), {"class": "street_lamp", "src_record": "w456@1"}),
            ("c", (180., 120.), {"class": "street_lamp"}),
            ("d", (200., 120.), {"class": "street_lamp", "src_record": "n789@1",
                                  "tags": json.dumps({"highway": "street_lamp", "lamp_mount": "high_mast"})}),
        ], mg.POINT)
        md = FakeMD({"infrastructure": infra})
        rows, _ = wp.tile_props(FRAME, 0, 0, md, elev, mapped_lamps=True)
        lamps = [r for r in rows if r[0] == "streetlight"]
        self.assertEqual(len(lamps), 1)
        self.assertEqual(lamps[0][1:3], [100., 120.])
        high = [r for r in rows if r[0] == "streetlight_high_mast"]
        self.assertEqual(len(high), 1)
        self.assertEqual(high[0][1:3], [200., 120.])
        self.assertEqual(high[0][3], 5.)
        self.assertEqual(high[0][6], wp.HIGH_MAST_H)
        self.assertEqual(wp.tile_props(FRAME, 0, 0, md, elev)[0], rows)
        self.assertEqual(wp.tile_props(FRAME, 0, 0, md, elev, mapped_lamps=False)[0], [])
        self.assertEqual(wp.tile_props(FRAME, 1, 0, md, elev, mapped_lamps=True)[0], [])

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
