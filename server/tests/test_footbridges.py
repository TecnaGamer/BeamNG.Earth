"""Footbridges on the walkways' bridge pieces (footbridges.py, GitHub #905)."""
import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import footbridges  # noqa: E402
import railways  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def valley(xy):
    """Banks at 10 m, a river 6 m down between x = 110 and 150."""
    x = np.asarray(xy, np.float64).reshape(-1, 2)[:, 0]
    return 10.0 - 6.0 * np.clip(1.0 - np.abs(x - 130.0) / 20.0, 0.0, 1.0) ** 0.5


def flat(xy):
    return np.full(len(np.asarray(xy).reshape(-1, 2)), 10.0)


def bridge(cls="footway", line=((100.0, 128.0), (160.0, 128.0)), extra=()):
    lines = [list(line)] + [list(e[0]) for e in extra]
    classes = [cls] + [e[1] for e in extra]
    return features(lines, classes, ids=[f"id{i:03d}" for i in range(len(lines))],
                    subtypes=["road"] * len(lines), flags=[(0, "is_bridge")])


class FootbridgeTest(unittest.TestCase):
    def mats(self, mesh):
        P, T, UV, mats, M = mesh
        return {mats[k] for k in np.unique(M)}

    def test_a_footbridge_over_a_river_has_a_deck(self):
        mesh = railways.tile_mesh(FRAME, 0, 0, [bridge()], valley)
        self.assertIsNotNone(mesh)
        P, T, UV, mats, M = mesh
        self.assertEqual(self.mats(mesh), {railways.CONCRETE})
        top = P[:, 2].max()
        self.assertAlmostEqual(float(top), 10.0 + footbridges.PARAPET_H, delta=0.05, msg="level between its banks")
        col = railways.collision_mesh(mesh)
        self.assertIsNotNone(col, "it collides (railcol)")
        y = P[:, 1]
        self.assertAlmostEqual(float(y.max() - y.min()), footbridges.WIDTH_DEFAULT_M + 2 * footbridges.PARAPET_T,
                               delta=0.1, msg="the path's width between its parapets")

    def test_width_by_class_and_piers_under_a_long_one(self):
        parts = footbridges.parts(FRAME, [bridge("cycleway")], (0.0, 0.0, 256.0, 256.0))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][2], footbridges.WIDTH_M["cycleway"])
        small = []
        mesh = railways._Mesh((0.0, 0.0, 0.0))
        footbridges.add(mesh, parts, valley, [], (-10.0, -10.0, 266.0, 266.0), mesh.mat(railways.CONCRETE), small)
        self.assertTrue(small, "a pier where the deck stands high over the river")

    def test_an_overpass_over_a_street_at_grade_is_left_out(self):
        # a footbridge across a street with both its ends at the street's level: a slab there would block it
        street = (((130.0, 0.0), (130.0, 256.0)), "residential")
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [bridge(extra=[street])], flat))

    def test_on_the_ground_or_switched_off_nothing(self):
        ground_path = features([[(100.0, 128.0), (160.0, 128.0)]], ["footway"], subtypes=["road"])
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [ground_path], valley))
        with unittest.mock.patch.object(railways, "FOOTBRIDGES_ON", False):
            self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [bridge()], valley))

    def test_model_suppresses_only_the_covered_matching_walkway(self):
        xy = np.c_[np.arange(100., 161., 2.), np.full(31, 128.)]
        fp = {'ax': 0, 'u0': 110., 'du': 2., 'cols': [[-3., 3.]] * 21, 'pedestrian': True}
        model = [(fp, (0., 128.), 55)]
        hit = footbridges.model_cover(xy, model)
        self.assertTrue(hit[10:20].all())
        self.assertFalse(hit[:4].any())
        self.assertFalse(hit[-4:].any())
        crossing = np.c_[np.full(31, 130.), np.arange(98., 159., 2.)]
        self.assertFalse(footbridges.model_cover(crossing, model).any())
        self.assertFalse(footbridges.model_cover(xy, []).any())
        self.assertFalse(footbridges.model_cover(xy, [({**fp, 'pedestrian': False}, (0., 128.), 1)]).any())
        mesh = railways._Mesh((0., 0., 0.))
        footbridges.add(mesh, [('id', xy, 3.)], valley, [], (0., 0., 256., 256.),
                        mesh.mat(railways.CONCRETE), [], model_bridges=model)
        positions = mesh.result()[0]
        self.assertTrue((positions[:, 0] < 110.).any())
        self.assertTrue((positions[:, 0] > 150.).any())
        self.assertFalse(((positions[:, 0] > 114.) & (positions[:, 0] < 146.)).any())

    def test_deck_rule(self):
        xy = np.c_[np.arange(0.0, 41.0, 2.0), np.zeros(21)]
        self.assertIsNotNone(footbridges.deck(xy, valley(xy + [110.0, 0.0]), []))
        self.assertIsNone(footbridges.deck(xy, flat(xy), []), "no room under it")


if __name__ == "__main__":
    unittest.main()
