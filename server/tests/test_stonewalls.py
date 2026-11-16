"""Mapped stone walls (fences.stone_walls, GitHub #938): OSM's barrier=wall + wall=dry_stone drawn as stone solids
along the mapped line, cut where a road runs, each tile drawing its own part."""
import json
import os
import sys
import unittest
from collections import namedtuple

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fences  # noqa: E402
import geodesy  # noqa: E402
import mapgeom  # noqa: E402
import roadmesh  # noqa: E402
from test_wallsolid import edge_counts  # noqa: E402

Spec = namedtuple("Spec", "width")
GROUND = 5.0


class StoneWallTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(53.35, -6.26, 0.0)        # (Ireland)

    def line(self, tags, xy, cls="wall", fid="wall-1", level=None):
        xy = np.asarray(xy, np.float64)
        lat, lon, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        attrs = {"subtype": np.array(["barrier"]), "class": np.array([cls]), "tags": np.array([json.dumps(tags)])}
        if level is not None:
            attrs["level"] = np.array([level], np.float64)
        return mapgeom.Features(
            layer="infrastructure", ids=np.array([fid]), kind=np.array([mapgeom.LINE], np.uint8),
            part_off=np.array([0, 1], np.int64), ring_off=np.array([0, 1], np.int64),
            coord_off=np.array([0, len(xy)], np.int64), lonlat=np.column_stack((lon, lat)), attrs=attrs)

    def walls(self, feats, box=(0.0, 0.0, 100.0, 100.0), roads=()):
        return fences.stone_walls(self.frame, lambda key: feats, box,
                                  lambda la, lo: np.full(np.shape(la), GROUND), roads=roads)

    def test_stone_wall_types(self):
        f = lambda tags, cls="wall": self.line(tags, [[0.0, 0.0], [10.0, 0.0]], cls=cls)  # noqa: E731
        self.assertEqual(fences.stone_height(f({"barrier": "wall", "wall": "dry_stone"}), 0), fences.STONE_H_M)
        self.assertEqual(fences.stone_height(f({"barrier": "wall", "material": "stone"}), 0), fences.STONE_H_M)
        self.assertEqual(fences.stone_height(f({"barrier": "wall", "wall": "dry_stone", "height": "1.6"}), 0), 1.6)
        # an implausible height= is the default's
        self.assertEqual(fences.stone_height(f({"barrier": "wall", "wall": "dry_stone", "height": "12"}), 0),
                         fences.STONE_H_M)
        self.assertIsNone(fences.stone_height(f({"barrier": "wall", "wall": "noise_barrier"}), 0))
        self.assertIsNone(fences.stone_height(f({"barrier": "wall", "wall": "brick"}), 0))
        self.assertIsNone(fences.stone_height(f({"barrier": "wall"}), 0))
        self.assertIsNone(fences.stone_height(f({"barrier": "fence", "material": "stone"}, cls="fence"), 0))

    def test_a_dry_stone_wall_stands_on_the_ground_along_its_line(self):
        got = self.walls(self.line({"barrier": "wall", "wall": "dry_stone"}, [[10.0, 50.0], [90.0, 50.0]]))
        self.assertEqual(len(got), 1)
        w = got[0]
        self.assertAlmostEqual(float(w["base"][:, 0].min()), 10.0, delta=0.01)
        self.assertAlmostEqual(float(w["base"][:, 0].max()), 90.0, delta=0.01)
        # the mapped line runs down its middle: the face half its thickness to one side, the back to the other
        mid = w["base"] - w["toward"] * (fences.STONE_THICK_M / 2)
        self.assertTrue(np.allclose(mid[:, 1], 50.0, atol=1e-6))
        self.assertTrue(np.allclose(w["top"], GROUND + fences.STONE_H_M, atol=0.01))
        self.assertTrue(np.allclose(w["foot"], GROUND - fences.STONE_FOOT_M, atol=0.01))
        self.assertTrue(w["parapet"] and not w["concrete"])

    def test_off_switch_and_other_walls(self):
        self.assertEqual(self.walls(self.line({"barrier": "wall", "wall": "brick"}, [[10.0, 50.0], [90.0, 50.0]])), [])
        old = fences.STONE_ON
        fences.STONE_ON = False
        try:
            self.assertEqual(self.walls(self.line({"barrier": "wall", "wall": "dry_stone"},
                                                  [[10.0, 50.0], [90.0, 50.0]])), [])
        finally:
            fences.STONE_ON = old

    def test_a_wall_on_a_bridge_is_left_out(self):
        for tags, level in (({"bridge": "yes"}, None), ({"layer": "1"}, None), ({}, 1.0)):
            feats = self.line(dict({"barrier": "wall", "wall": "dry_stone"}, **tags), [[10.0, 50.0], [90.0, 50.0]],
                              level=level)
            self.assertEqual(self.walls(feats), [], (tags, level))

    def test_a_road_through_the_wall_cuts_it(self):
        # a 6 m road across the wall at x = 50: the wall stops short of its edge on both sides
        road = ("road-1", Spec(6.0), np.array([[50.0, 0.0, GROUND], [50.0, 100.0, GROUND]]), None)
        got = self.walls(self.line({"barrier": "wall", "wall": "dry_stone"}, [[10.0, 50.0], [90.0, 50.0]]),
                         roads=[road])
        self.assertEqual(len(got), 2)
        gap = 3.0 + fences.STONE_ROAD_GAP_M
        for w in got:
            self.assertTrue((np.abs(w["base"][:, 0] - 50.0) >= gap - 1e-6).all())
        self.assertLess(min(abs(float(w["base"][:, 0][np.argmin(np.abs(w["base"][:, 0] - 50.0))]) - 50.0)
                            for w in got), gap + fences.STONE_STEP_M + 1e-6)

    def test_a_wall_across_two_tiles_is_drawn_once(self):
        feats = self.line({"barrier": "wall", "wall": "dry_stone"}, [[10.0, 50.0], [190.0, 50.0]])
        a = self.walls(feats, box=(0.0, 0.0, 100.0, 100.0))
        b = self.walls(feats, box=(100.0, 0.0, 200.0, 100.0))
        self.assertEqual((len(a), len(b)), (1, 1))
        self.assertAlmostEqual(float(a[0]["base"][-1, 0]), float(b[0]["base"][0, 0]), delta=1e-6)   # they meet
        self.assertLessEqual(float(a[0]["base"][-2, 0]), 100.0)
        self.assertGreaterEqual(float(b[0]["base"][1, 0]), 100.0)

    def test_the_mesh_is_a_closed_stone_solid(self):
        got = self.walls(self.line({"barrier": "wall", "wall": "dry_stone"}, [[10.0, 50.0], [40.0, 60.0], [90.0, 40.0]]))
        mesh = roadmesh.tunnel([], (0.0, 0.0, 0.0), walls=got)
        P, T = np.asarray(mesh[0], np.float64), np.asarray(mesh[1])
        self.assertEqual([e for e, n in edge_counts(P, T).items() if n != 2], [], "open edges")
        self.assertEqual(set(np.asarray(mesh[4]).tolist()), {2}, "all stone")


if __name__ == "__main__":
    unittest.main()
