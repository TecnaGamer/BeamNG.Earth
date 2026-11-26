"""Winter W3 (snowroad.py, earthfast snowroad.rs): the road skins' winter part, the material remaps, the photo slots, the
look in the snowstate, and that nothing changes with winter off."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fast  # noqa: E402
import roadwear  # noqa: E402
import snowcover  # noqa: E402
import snowroad  # noqa: E402


def native():
    try:
        fast.require(snowroad.NATIVE, "test")
        return True
    except Exception:
        return False


def lines_of(*roads):
    """A line set: roads = ((points, half width, class, lanes, oneway, fid), ...)."""
    pts, off, hw, cls, lanes, ow, fids = [], [0], [], [], [], [], []
    for p, h, c, n, o, f in roads:
        p = np.asarray(p, np.float64)
        pts.append(p)
        off.append(off[-1] + len(p))
        hw.append(h)
        cls.append(c)
        lanes.append(n)
        ow.append(o)
        fids.append(f)
    return (np.concatenate(pts), np.asarray(off, np.int64), np.asarray(hw, np.float64), np.asarray(cls, np.uint8),
            np.asarray(lanes, np.uint8), np.asarray(ow, bool), fids)


class OffIsIdentical(unittest.TestCase):
    def test_off_by_default(self):
        if os.environ.get("BNE_WINTER"):
            self.skipTest("BNE_WINTER set in the environment")
        self.assertFalse(snowroad.ON)
        self.assertEqual(snowroad.skin_key({"tier": 2, "snow": {"q": 1, "banks": [1, 1, 1, 1, 0]}}), ())
        self.assertIsNone(snowroad.snow_of({"snow": {"q": 1}}))
        self.assertIsNone(snowroad.wear_snow({"bank_bin": [1, 0, 0, 0, 0]}))

    def test_wear_key_ignores_the_snow(self):
        a = {"tier": 2, "season": "off"}
        b = dict(a, snow={"q": 1, "banks": [2, 2, 1, 1, 0]})
        self.assertEqual(roadwear.wear_key(a, 45.0), roadwear.wear_key(b, 45.0))

    def test_geo_names_without_bank(self):
        import blockworker
        if not snowroad.ON:
            self.assertNotIn("bank", blockworker.geo_names("12_34_56", 7))


class SkinPart(unittest.TestCase):
    def test_key_part(self):
        with mock.patch.object(snowroad, "ON", True):
            self.assertEqual(snowroad.skin_key({"tier": 2}), ())
            self.assertEqual(snowroad.skin_key(2), ())
            k = snowroad.skin_key({"snow": {"q": 1, "banks": [2, 1, 1, 0, 0]}})
            self.assertEqual(k, (("snowroad", snowroad.VERSION, 1, (2, 1, 1, 0, 0)),))
            self.assertNotEqual(k, snowroad.skin_key({"snow": {"q": 1, "banks": [2, 1, 2, 0, 0]}}))
            self.assertEqual(snowroad.wear_snow({"bank_bin": [1, 1, 0, 0, 0]}), {"q": 1, "banks": [1, 1, 0, 0, 0]})


@unittest.skipUnless(native(), "earthfast 634")
class Remaps(unittest.TestCase):
    def setUp(self):
        # a P1 east-west, a P3 north-south crossing it at (0, 0)
        self.lines = lines_of((((-100, 0), (0, 0), (100, 0)), 6.0, 1, 2, False, "w/1"),
                              (((0, -100), (0, 0), (0, 100)), 4.0, 3, 2, False, "w/2"))

    def test_chunks(self):
        chunks = [{"m": "line_white", "n": [[-50, 0, 0], [-40, 0, 0]]},
                  {"m": "line_yellow", "n": [[1, 50, 0], [1, 60, 0]]},
                  {"m": "road_asphalt_2lane", "deck": True, "n": [[30, 1, 5], [40, 1, 5]]},
                  {"m": "road_asphalt_2lane", "n": [[30, 1, 0], [40, 1, 0]]},
                  {"m": "line_white", "n": [[500, 500, 0], [510, 500, 0]]},
                  {"m": "gutter1", "n": [[-50, 5, 0], [-40, 5, 0]]}]
        out = snowroad.remap_chunks(chunks, self.lines)
        self.assertEqual([c["m"] for c in out], ["bne_snow_line_white_p1", "bne_snow_line_yellow_p3",
                                                 "bne_snow_deck_p1", "road_asphalt_2lane", "bne_snow_line_white_p2",
                                                 "gutter1"])
        self.assertEqual(chunks[0]["m"], "line_white")      # (the input untouched)

    def test_walks(self):
        P = np.array([[-50, 8, 0], [-48, 8, 0], [-49, 9, 0], [8, 50, 0], [9, 50, 0], [8.5, 52, 0],
                      [-50, -8, 0], [-48, -8, 0], [-49, -9, 0]], np.float64)
        T = np.array([[0, 1, 2], [3, 4, 5], [6, 7, 8]])
        mesh = (P, T, np.zeros((9, 2)), ["curbs", "sidewalk1"], np.array([1, 1, 0]))
        P2, T2, UV2, mats, M = snowroad.remap_walks(mesh, self.lines, (0.0, 0.0, 0.0))
        self.assertEqual(mats[:2], ["curbs", "sidewalk1"])
        self.assertEqual(mats[2:], [snowroad.walk_name(s) for s in snowroad.WALK_SLOTS])
        self.assertEqual(mats[M[0]], "bne_snow_walk_s0")        # beside the P1: a main street's
        self.assertIn(mats[M[1]], ("bne_snow_walk_s1c", "bne_snow_walk_s1u"))   # the P3's: drawn per block face
        self.assertEqual(M[2], 0)                               # (a curb stays a curb)
        again = snowroad.remap_walks(mesh, self.lines, (0.0, 0.0, 0.0))
        self.assertEqual(list(again[4]), list(M))

    def test_banks(self):
        snow = {"q": 1, "banks": [0, 2, 0, 1, 0]}
        cuts = snowroad.crossing_cuts([{"m": "crossing_white", "w": 3.0, "n": [[-60, -6, 0], [-60, 6, 0]]}])
        self.assertEqual(cuts.shape, (1, 3))
        # (the skin's chunks carry numpy points: the game's first W3 run failed every skin on `n or ()`)
        cuts2 = snowroad.crossing_cuts([{"m": "bne_snow_crossing_white_p3", "w": 3.0,
                                         "n": np.array([[-60.0, -6, 0], [-60, 6, 0]])}, {"m": "crossing_white"}])
        self.assertTrue(np.allclose(cuts, cuts2))
        out = snowroad.remap_chunks([{"m": "line_white", "n": np.array([[-50.0, 0, 0], [-40, 0, 0]])}], self.lines)
        self.assertEqual(out[0]["m"], "bne_snow_line_white_p1")
        mesh = snowroad.bank_mesh(snow, self.lines, cuts, (-100, -100, 100, 100),
                                  lambda xy: np.full(len(xy), 10.0), (0.0, 0.0, 5.0))
        self.assertIsNotNone(mesh)
        P, T, UV, mats, M = mesh
        self.assertEqual(mats, [snowroad.bank_name(c) for c in range(5)])
        self.assertTrue(set(np.unique(M)) <= {1, 3})
        self.assertGreaterEqual(P[:, 2].min(), 5.0 - 1e-9)       # on the ground (10 - origin 5) or above it
        self.assertLessEqual(P[:, 2].max(), 5.0 + 0.6 * 1.15 + 1e-6)
        near_junction = np.hypot(P[:, 0], P[:, 1]) < 6.0
        self.assertFalse(near_junction.any())
        at_crossing = (np.abs(P[:, 0] + 60) < 1.0) & (P[:, 2] > 5.01)
        self.assertFalse(at_crossing.any())
        # the drawn ground unknown west of x = 0: those triangles left out, no NaN in the mesh
        half = snowroad.bank_mesh(snow, self.lines, cuts, (-100, -100, 100, 100),
                                  lambda xy: np.where(np.asarray(xy)[:, 0] < 0, np.nan, 10.0), (0.0, 0.0, 5.0))
        self.assertTrue(np.isfinite(half[0][np.unique(half[1])]).all())
        self.assertLess(len(half[1]), len(T))
        self.assertIsNone(snowroad.bank_mesh({"q": 1, "banks": [0] * 5}, self.lines, cuts, (-100, -100, 100, 100),
                                             lambda xy: np.zeros(len(xy)), (0, 0, 0)))


@unittest.skipUnless(native(), "earthfast 634")
class Kernel(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(list(fast._ef.snowroad_classes(["motorway", "trunk_link", "primary", "tertiary",
                                                         "residential", "service", ""])), [0, 0, 1, 2, 3, 4, 2])

    def test_zones_raster(self):
        segs = np.array([[0.0, 32.0, 64.0, 32.0]])
        z = fast._ef.snowroad_zones(segs, np.array([10.0]), np.array([0], np.int64), np.array([3], np.uint8),
                                    np.array([2], np.uint8), np.array([False]), 64, 0.5)
        vals = set(np.unique(z).tolist())
        self.assertEqual(vals, {0, 1 + 3, 1 + 5 + 3, 1 + 10 + 3})     # none, lanes, tracks, edge of a P3

    def test_look(self):
        rows = [[3, 0, -300, 8, 50, 0, 900, 2000]] + [[11, 200, -500, 10, 400, 1, 800, 3000]] * 4 + \
               [[6, 0, -700, 6, 0, 0, 0, 0]] * 5
        look = fast._ef.snowroad_look(rows, 1500, 8, 30, False, -500, 3, [-1] * 5)
        self.assertEqual(look["shares"][0], [0, 0, 5])          # salted wet, slush at the edges
        self.assertEqual(look["marking"][0], 1000)
        self.assertLess(look["marking"][3], 500)
        self.assertEqual(look["bank_bin"][4], 0)                # P4 never plowed
        self.assertEqual(len(look["walks"]), 4)

    def test_snow_message_look(self):
        import snow
        with mock.patch.object(snowroad, "ON", True):
            raw = {}
            msg = snow.preset_message("heavy", 44.95, -93.25, raw=raw)
        self.assertIn("look", msg)
        lk = msg["look"]
        self.assertEqual([len(lk[k]) for k in ("marking", "deck", "banks", "dirt", "walks")], [5, 5, 5, 5, 4])
        self.assertTrue(all(0.0 <= v <= 1.0 for v in lk["marking"] + lk["deck"]))
        rows, road, _bins = snow.w2_table(raw, [-1] * 8)
        self.assertEqual(len(road), 15)
        # the storm: banks along the main roads, a side street white with tracks
        self.assertGreater(max(lk["banks"][:4]), 0, lk)
        with mock.patch.object(snowroad, "ON", False):
            self.assertNotIn("look", snow.preset_message("heavy", 44.95, -93.25))


class Slots(unittest.TestCase):
    def test_road_slots(self):
        self.assertEqual(snowcover.road_slots((1, 2, 3, 4, 5)), [1, 2, 3, 4, 5] * 3)
        self.assertEqual(len(snowcover.road_slots(snowcover.PLOWED)), 15)
        with self.assertRaises(ValueError):
            snowcover.road_slots((1, 2))


if __name__ == "__main__":
    unittest.main()
