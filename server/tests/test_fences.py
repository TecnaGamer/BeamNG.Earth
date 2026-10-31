"""Mapped chain-link fences and noise walls (fences.py)."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fences
import geodesy
import mapgeom


class KindTest(unittest.TestCase):
    def features(self, cls, tags=None):
        attrs = {"subtype": np.array(["barrier"]), "class": np.array([cls])}
        if tags is not None:
            attrs["tags"] = np.array([json.dumps(tags)])
        return type("F", (), {"attrs": attrs})()

    def test_chain_link_and_noise_wall_only(self):
        self.assertEqual(fences.kind_of(self.features("fence", {"fence_type": "chain_link"}), 0), "chainlink")
        self.assertEqual(fences.kind_of(self.features("fence", {"barrier": "fence", "fence_type": "chainlink"}), 0),
                         "chainlink")
        self.assertIsNone(fences.kind_of(self.features("fence", {"fence_type": "wood"}), 0))
        self.assertIsNone(fences.kind_of(self.features("fence"), 0))
        self.assertEqual(fences.kind_of(self.features("wall", {"wall": "noise_barrier"}), 0), "noisewall")
        self.assertEqual(fences.kind_of(self.features("noise_barrier"), 0), "noisewall")
        self.assertIsNone(fences.kind_of(self.features("wall", {"wall": "brick"}), 0))
        self.assertIsNone(fences.kind_of(self.features("barrier", {"tags": "nope"}), 0))
        self.assertIsNone(fences.kind_of(self.features("wall", {"tags": "null"}), 0))


class PlaceTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(39.2834, -76.5934, 0.0)

    def line(self, cls, tags, xy, fid="line-1"):
        xy = np.asarray(xy, np.float64)
        lat, lon, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        return mapgeom.Features(
            layer="infrastructure", ids=np.array([fid]), kind=np.array([mapgeom.LINE], np.uint8),
            part_off=np.array([0, 1], np.int64), ring_off=np.array([0, 1], np.int64),
            coord_off=np.array([0, len(xy)], np.int64), lonlat=np.column_stack((lon, lat)),
            attrs={"subtype": np.array(["barrier"]), "class": np.array([cls]),
                   "tags": np.array([json.dumps(tags)])})

    def rows(self, feats, box=(0.0, 0.0, 100.0, 100.0)):
        return fences.rows(self.frame, lambda key: feats, box, lambda la, lo: np.full(np.shape(la), 5.0))

    def test_chain_link_panels_follow_the_line(self):
        got = self.rows(self.line("fence", {"fence_type": "chain_link"}, [[10.0, 50.0], [90.0, 50.0]]))
        self.assertGreaterEqual(len(got), 30)
        self.assertLessEqual(len(got), 45)
        self.assertTrue(all(r[0] == "chainlink" and r[6] == fences.CHAIN_H for r in got))
        xs = np.array([r[1] for r in got])
        self.assertAlmostEqual(float(np.median(np.diff(np.sort(xs)))), fences.CHAIN_LEN, delta=0.05)
        self.assertTrue(all(abs(r[2] - 50.0) < 0.05 and abs(r[4]) < 0.5 and abs(r[3] - 5.16) < 0.02 for r in got))

    def test_a_plain_fence_is_not_chain_link(self):
        self.assertEqual(self.rows(self.line("fence", {"barrier": "fence"}, [[10.0, 50.0], [90.0, 50.0]])), [])

    def test_noise_wall_panels(self):
        got = self.rows(self.line("wall", {"wall": "noise_barrier"}, [[10.0, 20.0], [90.0, 20.0]]))
        self.assertGreaterEqual(len(got), 16)
        self.assertLessEqual(len(got), 22)
        self.assertTrue(all(r[0] == "noisewall" and r[6] == fences.WALL_H and abs(r[3] - 5.0) < 0.02 for r in got))
        xs = np.sort([r[1] for r in got])
        self.assertAlmostEqual(float(np.median(np.diff(xs))), fences.WALL_LEN, delta=0.05)
        self.assertTrue(all(abs(r[4] - 270.0) < 0.5 for r in got))

    def test_a_panel_past_the_tile_is_left_out(self):
        feats = self.line("fence", {"fence_type": "chain_link"}, [[-30.0, 10.0], [40.0, 10.0]])
        got = self.rows(feats, (0.0, 0.0, 100.0, 100.0))
        self.assertTrue(got)
        self.assertTrue(all(r[1] >= 0.0 for r in got))
        self.assertLess(min(r[1] for r in got), 5.0)

    def test_catalog_names_the_stock_shapes(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("M.CATALOG.chainlink", source)
        self.assertIn("s_chainlink_old", source)
        self.assertIn("M.CATALOG.noisewall", source)
        self.assertIn("s_concrete_noiseblocker", source)
        self.assertNotIn("M.NO_COLLIDE.chainlink", source)
        self.assertNotIn("M.NO_COLLIDE.noisewall", source)
