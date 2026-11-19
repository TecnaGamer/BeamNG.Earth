"""railprofile.py: the beds of the rail lines as one surface, the same for every tile and grading cell (plan 035)."""
import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import railprofile  # noqa: E402
import railways  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def hill_xy(xy):
    la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    return hill(la, lo)


def hill(la, lo):
    """A DEM: a gentle slope with a ridge (the line follows it)."""
    x, y, _ = FRAME.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
    return 10.0 + 0.01 * x + 3.0 * np.exp(-((x - 300.0) / 40.0) ** 2)


class RailProfileTest(unittest.TestCase):
    def setUp(self):
        railprofile._NODES.clear()
        # a long line, with a bridge 330 m east of the seam at x = 256: the deck's ramp climbs past a tile's reach
        self.f = features([[(-600.0, 100.0), (586.0, 100.0)], [(586.0, 100.0), (610.0, 100.0)],
                           [(610.0, 100.0), (1500.0, 100.0)]],
                          ["standard_gauge"] * 3, subtypes=["rail"] * 3, flags=[(1, "is_bridge")])
        self.load = lambda key: self.f
        self.keys = lambda frame, box, reach: [0]
        self._cells = railways.cells_near
        railways.cells_near = self.keys

    def tearDown(self):
        railways.cells_near = self._cells

    def beds(self, tx):
        rails = railways.parts(FRAME, [self.f], roads.tile_bbox(tx, 0), clip=True)
        got = railprofile.beds(FRAME, "t", self.load, hill_xy, rails)
        return {(fid, kind, round(float(x), 2), round(float(y), 2)): float(z)
                for (fid, _c, kind, xy), zs in zip(rails, got) for (x, y), z in zip(xy, zs)}

    def test_the_bed_is_the_same_whichever_tile_asks(self):
        a, b = self.beds(0), self.beds(1)
        common = set(a) & set(b)
        self.assertGreater(len(common), 100, "the tiles share their overlap's points")
        for k in common:
            self.assertAlmostEqual(a[k], b[k], places=6, msg=str(k))

    def test_no_point_is_left_without_a_bed(self):
        for tx in (0, 1, 2):
            self.assertTrue(np.isfinite(list(self.beds(tx).values())).all())

    def test_the_nodes_beds_are_blended_continuously(self):
        # points across a lattice line (x = 256) a few cm apart differ by the bed's grade only, not by a node's step
        bed = self.beds(0) | self.beds(1)
        pts = sorted((k[2], z) for k, z in bed.items() if k[1] == "ground")
        xs = np.array([p[0] for p in pts])
        zs = np.array([p[1] for p in pts])
        order = np.argsort(xs)
        step = np.abs(np.diff(zs[order]) / np.maximum(np.diff(xs[order]), 1e-9))
        self.assertLess(float(step.max()), 0.1, "no step along the line: grade stays small (0.1 = 10 %)")

    def test_smoothing_levels_a_bump_but_pins_the_ends_and_limits_the_cut(self):
        xy = np.c_[np.arange(0.0, 400.0, 2.0), np.zeros(200)]
        zb = 10.0 + np.where(np.abs(xy[:, 0] - 200.0) < 6.0, 2.0, 0.0)          # a 12 m, 2 m high spike
        z = railprofile.smooth(xy, zb)
        self.assertLess(float(z.max()), 10.0 + 0.5, "the spike is levelled to a bump under half a metre")
        self.assertEqual(float(z[0]), float(zb[0]))
        self.assertEqual(float(z[-1]), float(zb[-1]))
        self.assertLessEqual(float(np.abs(z - zb).max()), railprofile.MAX_CUT_M + 1e-9)
        ramp = 10.0 + 0.01 * xy[:, 0]
        np.testing.assert_allclose(railprofile.smooth(xy, ramp), ramp, atol=1e-6, err_msg="a straight grade stays")

    def test_a_tram_is_left_to_the_window_profile(self):
        got = railprofile.beds(FRAME, "t", self.load, hill_xy, [("t", "tram", "ground", np.array([[0.0, 0.0], [2.0, 0.0]]))])
        self.assertTrue(np.isnan(got[0]).all())


class FindNearTest(unittest.TestCase):
    def test_a_point_another_window_split_differently_takes_the_lines_nearest_points(self):
        """Popoli's track 2 (2026-11-16): the asking window split the line into other parts than the node's, no exact key
        matched, the grader fell back to profiling the whole cell (decks 7.5 m up carried along the track). Points of the
        same line, any kind, within half a step are interpolated; another line's, or farther, stay NaN."""
        xy = np.array([[0.0, 0.0], [railways.STEP_M, 0.0], [2 * railways.STEP_M, 0.0]])
        cm = railprofile._cm(xy)
        # (a node as _make_node packs it: its groups by (fid, kind), their beds in one earthfast.RailNode)
        groups = {("f", "bridge"): 0, ("g", "ground"): 1}
        packed = railprofile.fast._ef.rail_node_pack(np.r_[cm, cm], np.r_[[10.0, 12.0, 14.0], np.full(3, 99.0)],
                                                     np.array([0, 3, 6], np.int64), np.array([0, 1], np.int64), 2)
        node = (groups, packed)
        ask = railprofile._cm(np.array([[0.5 * railways.STEP_M, 0.0], [0.0, 3 * railways.STEP_M]]))
        out = railprofile._find(node, "f", "ground", ask)
        self.assertTrue(np.isnan(out).all(), "(no exact key: other kind, other points)")
        got = railprofile._find_near(node, "f", ask, out.copy())
        self.assertAlmostEqual(float(got[0]), 11.0, places=6)
        self.assertTrue(np.isnan(got[1]), "(farther than half a step: left NaN)")


class JoinBedsTest(unittest.TestCase):
    def line(self, p0, p1, step=2.0):
        n = int(np.hypot(p1[0] - p0[0], p1[1] - p0[1]) // step) + 1
        t = np.linspace(0.0, 1.0, n)
        return np.c_[p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t]

    def test_a_crossover_is_brought_to_the_beds_of_both_lines_it_joins(self):
        a = self.line((0.0, 0.0), (600.0, 0.0))
        b = self.line((0.0, 4.5), (600.0, 4.5))
        c = self.line((100.0, 0.0), (148.0, 4.5))           # its ends on a and b, 48 m long
        za, zb_ = np.full(len(a), 100.0), np.full(len(b), 100.0)
        zc = np.linspace(100.45, 99.42, len(c))             # laid from its own ground: 0.45 over, 0.58 under
        rails = [("a", "standard_gauge", "ground", a), ("b", "standard_gauge", "ground", b),
                 ("c", "standard_gauge", "ground", c)]
        out = railprofile.join_beds(rails, [za, zb_, zc])
        self.assertAlmostEqual(float(out[2][0]), 100.0, places=6)
        self.assertAlmostEqual(float(out[2][-1]), 100.0, places=6)
        self.assertLess(float(np.abs(out[2] - 100.0).max()), 0.05, "level between the two")
        np.testing.assert_array_equal(out[0], za)
        with unittest.mock.patch.object(railprofile, "JOIN_ON", False):
            self.assertEqual(float(railprofile.join_beds(rails, [za, zb_, zc])[2][0]), float(zc[0]))

    def test_a_siding_leaving_a_line_runs_the_difference_out_and_a_joint_is_left(self):
        a = self.line((0.0, 0.0), (600.0, 0.0))
        s = self.line((100.0, 0.0), (400.0, 40.0))
        za, zs = np.full(len(a), 100.0), np.full(len(s), 100.5)
        rails = [("a", "standard_gauge", "ground", a), ("s", "standard_gauge", "ground", s)]
        out = railprofile.join_beds(rails, [za, zs])
        self.assertAlmostEqual(float(out[1][0]), 100.0, places=6)
        self.assertAlmostEqual(float(out[1][-1]), 100.5, places=6, msg="its free end is as laid")
        self.assertGreater(float(out[1][len(s) // 2]), 100.49, "and far on, as laid")
        ahead = self.line((600.0, 0.0), (900.0, 0.0))       # a joint: this line starts where a ends
        out = railprofile.join_beds([rails[0], ("j", "standard_gauge", "ground", ahead)],
                                    [za, np.full(len(ahead), 103.0)])
        self.assertEqual(float(out[1][0]), 103.0)


class NodeMemoFaultTest(RailProfileTest):
    """A node made on a short read of the ground is not kept (TecnaGamer's review of #1248): the build that read short is
    built again on the full DEM, and that rebuild must make the node again, reading (and faulting) anew."""

    def test_a_node_made_on_a_short_read_is_made_again(self):
        import revisit
        short = [True]

        def ground(xy):
            if short[0]:
                revisit.fault()             # (as dem.py where a lidar sheet is not there yet)
                return hill_xy(xy) - 5.0
            return hill_xy(xy)
        rails = railways.parts(FRAME, [self.f], roads.tile_bbox(0, 0), clip=True)
        with revisit.scope() as b:
            first = railprofile.beds(FRAME, "f", self.load, ground, rails)
        self.assertFalse(b.complete, "the build itself saw the fault")
        self.assertEqual(len(railprofile._NODES), 0, "no node kept")
        short[0] = False
        with revisit.scope() as b:
            again = railprofile.beds(FRAME, "f", self.load, ground, rails)
        self.assertTrue(b.complete)
        self.assertGreater(len(railprofile._NODES), 0, "made on the full ground: kept")
        gap = max(float(np.nanmax(z1 - z0)) for z0, z1 in zip(first, again))
        self.assertAlmostEqual(gap, 5.0, places=3, msg="the rebuild has the full ground's beds")

    def test_a_pool_tasks_fault_counts_too(self):
        import revisit
        from runtimeprofiler import ThreadPoolExecutor
        pool = ThreadPoolExecutor(1)

        def far(xy):
            revisit.fault()
            return hill_xy(xy)

        def ground(xy):                     # (dem.py reads its tiles in its fetch pool)
            return pool.submit(far, xy).result()
        rails = railways.parts(FRAME, [self.f], roads.tile_bbox(0, 0), clip=True)
        railprofile.beds(FRAME, "p", self.load, ground, rails)
        pool.shutdown()
        self.assertEqual(len(railprofile._NODES), 0)

    def test_a_nodes_beds_by_line_and_centimetre(self):
        """earthfast.rail_node_pack / RailNode.find: a point two parts of a line share has the later part's height, a
        key a centimetre off is found, another kind or line is not, a miss is NaN."""
        rails = [("a", "rail", "ground", np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])),
                 ("a", "rail", "bridge", np.array([[2.0, 0.0], [3.0, 0.0]])),
                 ("a", "rail", "ground", np.array([[2.0, 0.0], [2.5, 0.0]])),
                 ("b", "rail", "ground", np.array([[1.0, 0.0]]))]
        xy, off = railprofile._flat(rails)
        z = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        groups = {}
        group = np.array([groups.setdefault((f, k), len(groups)) for f, _c, k, _xy in rails], np.int64)
        node = (groups, railprofile.fast._ef.rail_node_pack(railprofile._cm(xy), z, off, group, len(groups)))
        self.assertEqual(railprofile._points(node), 7)
        q = railprofile._cm(np.array([[2.0, 0.0], [1.0, 0.01], [9.0, 9.0], [0.0, 0.0]]))
        np.testing.assert_array_equal(railprofile._find(node, "a", "ground", q), [6.0, 2.0, np.nan, 1.0])
        np.testing.assert_array_equal(railprofile._find(node, "a", "bridge", q), [4.0, np.nan, np.nan, np.nan])
        np.testing.assert_array_equal(railprofile._find(node, "b", "ground", q), [np.nan, 8.0, np.nan, np.nan])
        self.assertTrue(np.isnan(railprofile._find(node, "c", "ground", q)).all())
        self.assertTrue(np.isnan(railprofile._find(railprofile._EMPTY, "a", "ground", q)).all())


if __name__ == "__main__":
    unittest.main()



class MeshGraderLatticeTest(unittest.TestCase):
    """Popoli (42.1712 N 13.8250 E, 2026-11-17): a bridge's track level carried 700 m along the line at 2 % over a valley
    side, past the nodes' MARGIN_M: the nodes whose window held the bridge laid the ramp, the others the ground, 7 m
    apart. The mesh blended the nodes of the anchor's lattice, the grader those of its planning region's: the ground was
    graded 1.4-1.7 m over the track drawn on it and the wagons hit it. The mesh's bed is read from the grader's lattice:
    the same bed whatever the anchor. (railways.profile stands in here for a window that sees the bridge or not.)"""

    def setUp(self):
        railprofile._NODES.clear()
        self.f = features([[(-1500.0, 100.0), (-30.0, 100.0)], [(-30.0, 100.0), (-10.0, 100.0)],
                           [(-10.0, 100.0), (2500.0, 100.0)]],
                          ["standard_gauge"] * 3, subtypes=["rail"] * 3, flags=[(1, "is_bridge")])
        self.load = lambda key: self.f
        self._cells = railways.cells_near
        railways.cells_near = lambda frame, box, reach=railways.REACH_M: [0]

    def tearDown(self):
        railways.cells_near = self._cells
        railprofile._NODES.clear()

    @staticmethod
    def profile(rails, ground, underpasses=()):
        """railways.profile's stand-in: the bed 5 m over the ground in a window that holds the bridge (its ramp), on the
        ground in one that does not."""
        up = 5.0 if any(kind == "bridge" for _f, _c, kind, _xy in rails) else 0.0
        out = []
        for _f, _c, _kind, xy in rails:
            zg = np.asarray(ground(xy), np.float64)
            out.append((zg + up, zg))
        return out

    def test_the_mesh_has_the_bed_the_ground_is_graded_to(self):
        import geotiles
        elev = hill
        with unittest.mock.patch.object(railways, "profile", self.profile):
            region = geotiles.region_frame(*geotiles.region_of(FRAME.lat0, FRAME.lon0))
            # (the grader: grade_plans in its planning region's frame, the DEM's heights)
            corners = geotiles.world_xy_map(FRAME, region, np.array([[-1600.0, 0.0], [2600.0, 200.0]]))
            box = (float(corners[:, 0].min()), float(corners[:, 1].min()), float(corners[:, 0].max()),
                   float(corners[:, 1].max()))
            plans = railways.grade_plans(region, [self.f], box, elev, load=self.load)
            # (the mesh: tile_mesh's beds in the anchor's frame, in its heights)
            rails = railways.parts(FRAME, [self.f], (-1600.0, 0.0, 2600.0, 200.0), clip=True)
            ground = railways.point_ground(FRAME, elev)
            prof = railways._beds(FRAME, rails, ground, [], self.load, elev)
        gxy = np.concatenate([geotiles.world_xy_map(region, FRAME, xy) for _f, xy, _z in plans])
        gz = np.concatenate([z for _f, _xy, z in plans])
        worst, mixed = 0.0, 0
        for (_fid, _c, kind, xy), (zb, _zg) in zip(rails, prof):
            if kind != "ground":
                continue
            m = (xy[:, 0] > 200.0) & (xy[:, 0] < 1000.0)
            for p, z in zip(xy[m], zb[m]):
                d = np.hypot(gxy[:, 0] - p[0], gxy[:, 1] - p[1])
                j = int(np.argmin(d))
                self.assertLess(float(d[j]), 0.05, "(the grader plans the same stretch)")
                la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
                want = float(FRAME.geodetic_to_enu(la, lo, np.array([gz[j]]))[2][0])
                worst = max(worst, abs(float(z) - want))
                mixed += 0.5 < float(z) - float(ground(p[None])[0]) < 4.5
        self.assertGreater(mixed, 20, "(the case: points between the nodes that see the bridge and those that do not)")
        self.assertLess(worst, 0.01, "the track drawn on the bed the ground is graded to")
