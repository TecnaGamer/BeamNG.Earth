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
