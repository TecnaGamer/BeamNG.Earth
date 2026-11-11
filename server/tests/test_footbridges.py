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

    def test_under_a_model_bridge_left_out(self):
        """GitHub #1071: Perth's Elizabeth Quay footbridge is a 3dmr model; the walkway's own deck stood in it."""
        everywhere = lambda xy: np.ones(len(xy), bool)          # noqa: E731
        nowhere = lambda xy: np.zeros(len(xy), bool)            # noqa: E731
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [bridge()], valley, under=everywhere))
        self.assertIsNotNone(railways.tile_mesh(FRAME, 0, 0, [bridge()], valley, under=nowhere))

    def test_on_the_ground_or_switched_off_nothing(self):
        ground_path = features([[(100.0, 128.0), (160.0, 128.0)]], ["footway"], subtypes=["road"])
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [ground_path], valley))
        with unittest.mock.patch.object(railways, "FOOTBRIDGES_ON", False):
            self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [bridge()], valley))

    def test_a_rendered_path_deck_has_one_owner_without_elevation_work(self):
        f = bridge("cycleway")
        xy = footbridges.parts(FRAME, [f], (0., 0., 256., 256.))[0][1]
        decks = [(str(f.ids[0]), None, xy, np.full(len(xy), 10.), 0.)]
        with unittest.mock.patch.object(footbridges, "deck", side_effect=AssertionError("duplicate profile")):
            mesh = railways.tile_mesh(FRAME, 0, 0, [f],
                lambda xy: self.fail("handled path needs no fallback elevation"), path_decks=decks)
        self.assertIsNone(mesh)

    def test_only_the_matching_source_piece_is_excluded(self):
        f = bridge()
        part = footbridges.parts(FRAME, [f], (0., 0., 256., 256.))[0]
        fid, xy, _ = part
        for source, line, z in (("another-way", xy, np.full(len(xy), 10.)),
                                (fid, xy + [0., 1.], np.full(len(xy), 10.)),
                                (fid, xy, np.full(len(xy), np.nan))):
            with self.subTest(source=source, first=line[0].tolist()):
                self.assertIsNotNone(railways.tile_mesh(FRAME, 0, 0, [f], valley,
                    path_decks=[(source, None, line, z, 0.)]))

    def test_an_unhandled_bridge_piece_of_the_same_way_keeps_its_fallback(self):
        f = bridge()
        fid, xy, _ = footbridges.parts(FRAME, [f], (0., 0., 256., 256.))[0]
        # A separately mapped span sharing this feature's ID must not disappear.
        f2 = bridge(line=((100., 140.), (160., 140.)))
        got = footbridges.parts(FRAME, [f2], (0., 0., 256., 256.),
            path_decks=[(fid, None, xy, np.full(len(xy), 10.), 0.)])
        self.assertEqual(len(got), 1)
        self.assertGreater(np.abs(got[0][1][:, 1] - xy[0, 1]).min(), 10.)

    def test_deck_rule(self):
        xy = np.c_[np.arange(0.0, 41.0, 2.0), np.zeros(21)]
        self.assertIsNotNone(footbridges.deck(xy, valley(xy + [110.0, 0.0]), []))
        self.assertIsNone(footbridges.deck(xy, flat(xy), []), "no room under it")


if __name__ == "__main__":
    unittest.main()
