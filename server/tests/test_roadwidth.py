"""Street widths between the buildings (roadwidth.py) and their use by the roads (roads.road_specs, the
sidewalks of build_tile)."""
import gc
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402
import roadwidth  # noqa: E402
from test_roads import features, flat, FRAME  # noqa: E402


def row_of_houses(y_face, depth_sign, x0=-150.0, x1=150.0, w=8.0):
    """Wall segments of houses side by side along y = y_face (fronts), going depth_sign deep."""
    segs = []
    for x in np.arange(x0, x1, w):
        a, b = (x, y_face), (x + w - 0.5, y_face)
        c, d = (x + w - 0.5, y_face + depth_sign * 10), (x, y_face + depth_sign * 10)
        for p, q in ((a, b), (b, c), (c, d), (d, a)):
            segs.append((*p, *q))
    return segs


class MeasureTest(unittest.TestCase):
    def test_walled_street(self):
        walls = roadwidth.Walls(row_of_houses(9.0, 1) + row_of_houses(-9.0, -1))
        self.assertAlmostEqual(walls.ray(np.array([0.0, 0.0]), np.array([0.0, 1.0])), 9.0, places=6)
        self.assertIsNone(walls.ray(np.array([0.0, 0.0]), np.array([1.0, 0.0]), reach=5.0))
        line = np.array([(-100.0, 0.5), (100.0, -0.5)])
        self.assertAlmostEqual(roadwidth.measure_row(line, walls), 18.0, delta=0.1)
        cw, walk = roadwidth.split(18.0)
        self.assertAlmostEqual(cw + 2 * walk, 18.0)
        self.assertTrue(roadwidth.WALK_MIN <= walk <= roadwidth.WALK_MAX)

    def test_open_or_one_sided_street(self):
        one = roadwidth.Walls(row_of_houses(9.0, 1))
        self.assertIsNone(roadwidth.measure_row(np.array([(-100.0, 0.0), (100.0, 0.0)]), one), "a quay: one side")
        self.assertIsNone(roadwidth.measure_row(np.array([(-100.0, 0.0), (100.0, 0.0)]), roadwidth.Walls([])))
        self.assertIsNone(roadwidth.split(4.0), "no plausible carriageway and sidewalks")


class UseTest(unittest.TestCase):
    def test_measured_width_and_sidewalks(self):
        f = features([[(-300, 50), (300, 50)]], ["residential"])
        widths = lambda fid, ll: (14.0, 3.6)
        sp = roads.road_specs(f, lambda la, lo: np.ones(np.shape(la), bool), widths)[0]
        self.assertEqual((sp.width, sp.walk), (14.0, 3.6))
        walks = []
        roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks,
                         widths=widths)
        runs = [w for w in walks if "pts" in w]
        self.assertTrue(runs and all(w["w"] == 3.6 for w in runs))
        for w in runs:                          # the sidewalk centre: half the street + half the sidewalk out
            self.assertAlmostEqual(abs(float(np.mean(w["pts"][:, 1])) - 50.0), 7.0 + 1.8, delta=0.3)
        too_wide = lambda fid, ll: (60.0, 4.0)
        self.assertNotEqual(roads.road_specs(f, None, too_wide)[0].width, 60.0, "implausible: the default stays")
        svc = features([[(-300, 50), (300, 50)]], ["service"])
        self.assertEqual(roads.road_specs(svc, None, widths)[0].walk, roads.SIDEWALK["w"], "service: not measured")



class StoreTest(unittest.TestCase):
    """The walls kept on disk (store_dir): read back by another process the same, the buildings not loaded."""

    def houses(self):
        import mapcells

        class Feats:
            pass
        rings = []
        for y_face, sign in ((9.0, 1), (-9.0, -1)):
            for x in np.arange(-150.0, 150.0, 8.0):
                rings.append([(x, y_face), (x + 7.5, y_face), (x + 7.5, y_face + sign * 10), (x, y_face + sign * 10),
                              (x, y_face)])
        ll, off = [], [0]
        for r in rings:
            la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in r]), np.array([p[1] for p in r]), 0.0)
            ll += list(zip(lo, la))
            off.append(len(ll))
        f = Feats()
        f.lonlat, f.coord_off = np.array(ll), np.array(off)
        la0, lo0, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([0.0]), 0.0)
        return f, mapcells.cell_of(float(la0[0]), float(lo0[0]))

    def test_read_back_and_incomplete_not_kept(self):
        import tempfile
        feats, home = self.houses()
        la, lo, _ = FRAME.enu_to_geodetic(np.array([-100.0, 100.0]), np.array([0.5, -0.5]), 0.0)
        road = np.c_[np.ravel(lo), np.ravel(la)]
        calls = []

        class F:
            def __len__(self):
                return len(feats.coord_off) - 1
        F.lonlat, F.coord_off = feats.lonlat, feats.coord_off

        def cell_fn(key):
            calls.append(key)
            return F() if (key.x, key.y) == (home.x, home.y) else None
        with tempfile.TemporaryDirectory() as d:
            a = roadwidth.RoadWidths(cell_fn, d).get("r1", road)
            self.assertIsNotNone(a)
            n = len(calls)
            b = roadwidth.RoadWidths(cell_fn, d).get("r1", road)
            self.assertEqual(len(calls), n, "read from disk: no buildings loaded")
            self.assertEqual(a, b)
            # the walls and their bucket index read memory-mapped (float32, shared by the processes), the same rays
            rw = roadwidth.RoadWidths(cell_fn, d)
            _, walls = rw._walls(home)
            self.assertFalse(walls.segs.flags.owndata or walls.segs.flags.writeable, "the file's mapping")
            self.assertEqual(walls.segs.dtype, np.float32)
            self.assertFalse(walls.seg_of.flags.owndata or walls.seg_of.flags.writeable)
            fresh = roadwidth.Walls(np.asarray(walls.segs))
            for name in ("keys", "first", "counts", "seg_of"):
                np.testing.assert_array_equal(getattr(walls, name), getattr(fresh, name), name)
            p = np.array([[0.0, 0.0], [10.0, -3.0]])
            dd = np.array([[0.0, 1.0], [0.0, -1.0]])
            np.testing.assert_array_equal(walls.rays(p, dd), fresh.rays(p, dd))
            np.testing.assert_array_equal(fresh.rays(p, dd),
                                          roadwidth.Walls(np.asarray(walls.segs, np.float64)).rays(p, dd))
            del rw, walls, fresh
            gc.collect()  # the file's mappings closed: Windows deletes no mapped file

            def flaky(key):
                if (key.x, key.y) != (home.x, home.y):
                    raise OSError("offline")
                return F()
        with tempfile.TemporaryDirectory() as d:
            roadwidth.RoadWidths(flaky, d).get("r1", road)
            files = [os.path.relpath(os.path.join(r, f), d) for r, _, fs in os.walk(d) for f in fs]
            # (a cell's walls are its own buildings: a neighbour that failed is not kept, the home cell is)
            self.assertTrue(all(f.startswith(os.path.join(str(home.z), str(home.x), str(home.y) + ".")) for f in files),
                            files)

if __name__ == "__main__":
    unittest.main()
