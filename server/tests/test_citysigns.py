"""Town direction boards on the poles that stand (citysigns.py; dirsigns.build_tile, earthfast dirsign_mesh modes 0 / 1)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import citysigns  # noqa: E402
import dirsigns  # noqa: E402
import fast  # noqa: E402

HAVE = getattr(fast._ef, "VERSION", 0) >= dirsigns.KERNEL


def board(x=0.0, y=-4.0, t=(1.0, 0.0), road=(0.0, 0.0), urban=1.0, size=0.0):
    """A dirsign_place row: a board at (x, y) on an approach travelling along t, the road's centre at `road`."""
    return [x, y, t[0], t[1], road[0], road[1], urban, size, 0.0, 0.0, 0.0, 1.0]


class MountableTest(unittest.TestCase):
    def test_poles_and_lamps_carry_a_board_their_arms_and_towers_do_not(self):
        for g in ("pole", "hvpole", "streetlight", "catmast", "catmast_eu", "eu_wood_pole", "pole_concrete_recessed",
                  "fr_wood_pole"):
            self.assertTrue(citysigns.mountable(g), g)
        for g in ("polelamp", "pylon", "signal", "stop", "lamplens", "bench", "gantry", "giveway"):
            self.assertFalse(citysigns.mountable(g), g)


class PickTest(unittest.TestCase):
    JUNCTION = (80.0, 0.0)

    @staticmethod
    def poles(*xy):
        return np.array([[x, y, 10.0] for x, y in xy], np.float64)

    def test_the_nearest_pole_on_the_driving_side(self):
        # travelling east: the driver's right is south (y < 0)
        p = self.poles((10.0, -4.5), (-20.0, -4.0), (3.0, -4.2))
        got = citysigns.pick(p, board(), self.JUNCTION, False)
        self.assertEqual(got, (3.0, -4.2, 10.0))

    def test_a_pole_on_the_other_side_or_in_the_road_is_no_mount(self):
        p = self.poles((0.0, 4.0), (0.0, -0.5))
        self.assertIsNone(citysigns.pick(p, board(), self.JUNCTION, False))

    def test_left_hand_traffic_mounts_on_the_left(self):
        p = self.poles((0.0, -4.0), (0.0, 4.0))
        self.assertEqual(citysigns.pick(p, board(y=4.0), self.JUNCTION, True)[1], 4.0)

    def test_too_far_along_or_too_near_the_junction_is_no_mount(self):
        self.assertIsNone(citysigns.pick(self.poles((55.0, -4.0)), board(), self.JUNCTION, False))   # > SHIFT_M
        self.assertIsNone(citysigns.pick(self.poles((70.0, -4.0)), board(), (80.0, 0.0), False))     # 10 m short of it

    def test_no_poles(self):
        self.assertIsNone(citysigns.pick(np.zeros((0, 3)), board(), self.JUNCTION, False))
        self.assertIsNone(citysigns.pick(None, board(), self.JUNCTION, False))

    def test_the_same_pole_from_any_order(self):
        a = self.poles((5.0, -4.0), (-5.0, -4.0))
        b = a[::-1].copy()
        order = lambda p: p[np.lexsort((p[:, 1], p[:, 0]))]          # noqa: E731
        self.assertEqual(citysigns.pick(order(a), board(), self.JUNCTION, False),
                         citysigns.pick(order(b), board(), self.JUNCTION, False))


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="citysigns_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def write(self, tx, ty, rows, groups=("streetlight", "pole", "polelamp", "bench")):
        data = {"props": {"groups": list(groups), "items": rows}}
        with open(os.path.join(self.d, f"roads_3_{tx}_{ty}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_the_tile_and_its_neighbours_are_read_and_only_mountable_groups_kept(self):
        self.write(0, 0, [[0, 10.0, 20.0, 5.0, 0, 1, 9, 0], [2, 11.0, 20.0, 5.0, 0, 1, 1, 0], [3, 12.0, 20.0, 5.0, 0, 1, 1, 0]])
        self.write(1, 0, [[1, 300.0, 20.0, 6.0, 0, 1, 9, 0]])
        self.write(5, 5, [[1, 900.0, 900.0, 6.0, 0, 1, 9, 0]])          # (two tiles off: not read)
        p = citysigns.poles_near(self.d, 3, 0, 0)
        self.assertEqual(p.tolist(), [[10.0, 20.0, 5.0], [300.0, 20.0, 6.0]])

    def test_nothing_written_is_no_poles(self):
        self.assertEqual(len(citysigns.poles_near(self.d, 3, 0, 0)), 0)


class GeoTest(unittest.TestCase):
    """The game's road tiles are geotiles (rg_<key>.json, rows in the tile's own frame): their poles in the anchor's metres."""

    def test_a_geotiles_poles_are_moved_into_the_anchors_frame(self):
        import geodesy
        import geotiles
        anchor = geodesy.EnuFrame(49.23, 28.44)
        gx, gy = geotiles.tile_of(49.2344, 28.4370)
        la, lo = 49.2344, 28.4370
        src = geodesy.EnuFrame(float(la), float(lo))
        # a pole 10 m east, 20 m north of the geotile's frame origin: its place in the anchor's frame by the geodesy
        d = tempfile.mkdtemp(prefix="citysigns_geo_")
        self.addCleanup(shutil.rmtree, d, True)
        data = {"frame": [float(la), float(lo)], "props": {"groups": ["pole", "bench"], "items": [[0, 10.0, 20.0, 5.0, 0, 1, 9, 0], [1, 11.0, 20.0, 5.0, 0, 1, 1, 0]]}}
        with open(os.path.join(d, f"rg_{geotiles.key(gx, gy)}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)
        e, n, _ = anchor.geodetic_to_enu(*src.enu_to_geodetic(10.0, 20.0, 5.0)[:2], 0.0)
        box = (float(e) - 50.0, float(n) - 50.0, float(e) + 50.0, float(n) + 50.0)
        p = citysigns.geo_poles_near(d, anchor, box)
        self.assertEqual(len(p), 1, "the pole, not the bench")
        self.assertAlmostEqual(float(p[0, 0]), float(e), delta=0.5)
        self.assertAlmostEqual(float(p[0, 1]), float(n), delta=0.5)

    def test_no_geotile_written_is_no_poles(self):
        import geodesy
        d = tempfile.mkdtemp(prefix="citysigns_geo_")
        self.addCleanup(shutil.rmtree, d, True)
        self.assertEqual(len(citysigns.geo_poles_near(d, geodesy.EnuFrame(49.23, 28.44), (0.0, 0.0, 256.0, 256.0))), 0)


@unittest.skipUnless(HAVE, "earthfast with the town board mesh")
class MeshTest(unittest.TestCase):
    @staticmethod
    def mesh(mode, side=1.0, w=1.6, h=0.5, zg=10.0, mount=2.5):
        signs = np.array([[100.0, 200.0, zg, zg, 1.0, 0.0, w, h, 1, mount, mode, side]], np.float64)
        p, t, uv, mi = fast._ef.dirsign_mesh(signs, [0.0, 0.0, 0.0])
        return np.asarray(p), np.asarray(t), np.asarray(mi)

    def test_a_board_on_a_pole_hangs_its_height_above_the_pole_foot_with_clamps_and_no_posts(self):
        p, t, mi = self.mesh(0.0)
        face = p[np.isin(np.arange(len(p)), np.unique(t[mi == 1]))]
        self.assertAlmostEqual(float(face[:, 2].min()), 10.0 + 2.5, places=6)
        self.assertAlmostEqual(float(face[:, 2].max()), 10.0 + 2.5 + 0.5, places=6)
        self.assertGreater(float(p[:, 2].min()), 10.0 + 2.4, "no post below the board")

    def test_the_board_reaches_out_to_the_side_away_from_the_road(self):
        # travelling east (+x): the driver's right is -y; right-hand traffic: out is -y; left-hand: out is +y
        for side, sign in ((1.0, -1), (-1.0, 1)):
            p, t, mi = self.mesh(0.0, side=side)
            face = p[np.unique(t[mi == 1])]
            reach = (face[:, 1] - 200.0) * sign
            self.assertAlmostEqual(float(reach.max()), 1.6 - 0.2, places=6)
            self.assertAlmostEqual(float(reach.min()), -0.2, places=6)

    def test_a_stand_of_its_own_goes_into_the_ground(self):
        p, t, mi = self.mesh(1.0)
        self.assertAlmostEqual(float(p[:, 2].min()), 10.0 - 0.3, places=6)

    def test_the_old_ten_column_array_is_the_two_post_board(self):
        signs = np.array([[100.0, 200.0, 10.0, 10.0, 1.0, 0.0, 3.2, 1.0, 1, 1.8]], np.float64)
        p, t, uv, mi = fast._ef.dirsign_mesh(signs, [0.0, 0.0, 0.0])
        self.assertGreater(len(t), 20)
        self.assertAlmostEqual(float(np.asarray(p)[:, 2].min()), 10.0 - 0.3, places=6)


if __name__ == "__main__":
    unittest.main()
