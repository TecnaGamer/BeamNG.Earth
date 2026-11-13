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
            gc.collect()  # (the walls' mappings sit in a reference cycle until collected: Windows deletes no mapped file)

class ManyTest(unittest.TestCase):
    """RoadWidths.measure_many: a cell's roads measured in one pass, the very numbers of one get / cap a road, a road
    across a cell's edge (the walls of both cells) included."""

    def town(self):
        import geodesy
        import mapcells
        home = mapcells.cell_of(39.283, -76.597)
        lon0, lat0, lon1, lat1 = home.bounds()
        frame = geodesy.EnuFrame(0.5 * (lat0 + lat1), lon1)         # (on the cell's east edge)
        rings = []
        for y_face, sign in ((9.0, 1), (-9.0, -1), (89.0, 1), (71.0, -1)):
            for x in np.arange(-400.0, 400.0, 8.0):
                rings.append([(x, y_face), (x + 7.5, y_face), (x + 7.5, y_face + sign * 10), (x, y_face + sign * 10),
                              (x, y_face)])
        cells = {}
        for r in rings:
            la, lo, _ = frame.enu_to_geodetic(np.array([p[0] for p in r]), np.array([p[1] for p in r]), 0.0)
            k = mapcells.cell_of(float(la[0]), float(lo[0]))
            ll, off = cells.setdefault((k.x, k.y), ([], [0]))
            ll += list(zip(lo, la))
            off.append(len(ll))

        def cell_fn(key):
            got = cells.get((key.x, key.y))
            if got is None:
                return None

            class F:
                lonlat, coord_off = np.array(got[0]), np.array(got[1])

                def __len__(self):
                    return len(self.coord_off) - 1
            return F()

        def road(pts):
            la, lo, _ = frame.enu_to_geodetic(np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float), 0.0)
            return np.c_[np.ravel(lo), np.ravel(la)]
        roads_ = [("in", road([(-380, 0.4), (-200, -0.3), (-120, 0.2)])), ("across", road([(-150, 0.5), (0, 0), (160, -0.5)])),
                  ("east", road([(60, 80.2), (300, 79.8)])), ("edge", road([(-20, 80), (25, 80)])),
                  ("open", road([(-300, 400), (300, 420)])), ("far", road([(5000, 5000), (5100, 5000)])),
                  ("short", road([(-250, 0), (-246, 0)])), ("point", road([(-250, 0)]))]
        self.assertEqual(len(cells), 2, "houses on both sides of the cell's edge")
        return cell_fn, roads_

    def test_same_as_one_road_at_a_time(self):
        cell_fn, roads_ = self.town()
        one = roadwidth.RoadWidths(cell_fn)
        want = [(one.get(f, ll), one.cap(f, ll)) for f, ll in roads_]
        self.assertIsNotNone(want[0][0], "a walled street")
        self.assertIsNotNone(want[1][0], "walled across the cells' edge")
        self.assertIsNone(want[4][0])
        many = roadwidth.RoadWidths(cell_fn)
        many.measure_many([(f, ll, 3) for f, ll in roads_])

        def no_walls(ll):
            raise AssertionError("measured again")
        many._near_walls = no_walls
        got = [(many.get(f, ll), many.cap(f, ll)) for f, ll in roads_ if len(ll) > 0]
        self.assertEqual([repr(g) for g in got], [repr(w) for w in want], "bit for bit")

    def test_only_what_is_asked_and_missing(self):
        cell_fn, roads_ = self.town()
        many = roadwidth.RoadWidths(cell_fn)
        f, ll = roads_[0]
        many.measure_many([(f, ll, 2), (roads_[1][0], roads_[1][1], 1)])
        self.assertIn(roadwidth._memo_key(f, ll), many.caps)
        self.assertNotIn(roadwidth._memo_key(f, ll), many.memo)
        self.assertIn(roadwidth._memo_key(*roads_[1]), many.memo)
        self.assertNotIn(roadwidth._memo_key(*roads_[1]), many.caps)
        many.measure_many([])
        bad = np.array([(float("nan"), 39.0), (-76.0, 39.0)])
        many.measure_many([("nan", bad, 3), (f, ll, 3)])           # (odd coordinates: left to get / cap)
        self.assertNotIn(roadwidth._memo_key("nan", bad), many.memo)
        self.assertEqual(repr(many.get(f, ll)), repr(roadwidth.RoadWidths(cell_fn).get(f, ll)))


class CopiesTest(unittest.TestCase):
    def test_each_copy_of_a_road_its_own_width(self):
        """Each map cell decodes its own copy of a road (its bends smoothed in that cell's frame): a width remembered
        by the road's id alone answered every later copy with the first one's, and a cell's plans depended on which
        cells were planned before it (2026-11-12)."""
        segs = row_of_houses(9.0, 1, -150.0, 0.0) + row_of_houses(-9.0, -1, -150.0, 0.0) \
            + row_of_houses(12.0, 1, 0.0, 150.0) + row_of_houses(-12.0, -1, 0.0, 150.0)
        walls = roadwidth.Walls(segs)

        def lonlat(x0, x1):
            la, lo, _ = FRAME.enu_to_geodetic(np.array([x0, x1]), np.array([0.0, 0.0]), 0.0)
            return np.c_[np.ravel(lo), np.ravel(la)]
        a, b = lonlat(-140.0, -10.0), lonlat(10.0, 140.0)
        rw = roadwidth.RoadWidths(lambda key: None)
        rw._near_walls = lambda ll: (FRAME, walls)
        first_a, then_b = rw.get("r1", a), rw.get("r1", b)
        fresh = roadwidth.RoadWidths(lambda key: None)
        fresh._near_walls = lambda ll: (FRAME, walls)
        self.assertEqual(then_b, fresh.get("r1", b))
        self.assertNotEqual(first_a, then_b)
        self.assertEqual(rw.cap("r1", b), fresh.cap("r1", b))


if __name__ == "__main__":
    unittest.main()
