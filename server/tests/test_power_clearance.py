"""Native power-line road avoidance against the original NumPy algorithm and analytic cases."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import fast
import powerlines
from power_reference import margin, move


def offsets():
    angles = 2. * np.pi * np.arange(powerlines.CLEAR_DIRS) / powerlines.CLEAR_DIRS
    rings = np.arange(powerlines.CLEAR_STEP_M, powerlines.CLEAR_SEARCH_M + 1e-6, powerlines.CLEAR_STEP_M)
    return (rings[:, None, None] * np.stack([np.cos(angles), np.sin(angles)], -1)[None]).reshape(-1, 2)


def packed(lines):
    return np.concatenate([np.column_stack([line[:-1], np.diff(line, axis=0), np.full(len(line)-1, half)])
                           for line, half in lines]) if lines else np.empty((0, 5))


def ends(lines):
    return np.cumsum([len(line)-1 for line, _ in lines], dtype=np.int64)


class NativePowerClearanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fast.require(168, 'power-line road avoidance tests')

    def check_move(self, point, lines, clear=2., around=None):
        around = offsets() if around is None else np.asarray(around, np.float64).reshape(-1, 2)
        want = move(point, lines, around, clear)
        got = np.asarray(fast._ef.power_off_roads(packed(lines), ends(lines), *point, around, clear))
        np.testing.assert_allclose(got, want, atol=2e-10, rtol=0)
        return got

    def test_no_roads_and_points_already_clear_are_unchanged(self):
        np.testing.assert_array_equal(self.check_move((1., -2.), []), (1., -2.))
        lines = [(np.array([[-100., 0.], [100., 0.]]), 5.)]
        for point in [(0., 20.), (0., 7.), (0., -7.), (200., 0.)]:
            np.testing.assert_array_equal(self.check_move(point, lines), point)

    def test_single_road_both_sides_and_centreline(self):
        lines = [(np.array([[-100., 0.], [100., 0.]]), 5.)]
        for point, want in [((10., 1.), (10., 7.)), ((10., -1.), (10., -7.)), ((0., 0.), (0., 7.))]:
            np.testing.assert_allclose(self.check_move(point, lines), want, atol=1e-12, rtol=0)

    def test_dual_carriageways_and_junction_search(self):
        a = np.array([[-100., 0.], [100., 0.]])
        fixtures = [([(a, 4.), (a + (0., 13.), 4.)], (0., .8)),
                    ([(a, 5.), (a[:, ::-1].copy(), 5.)], (3., 4.))]
        for lines, point in fixtures:
            got = self.check_move(point, lines)
            self.assertGreaterEqual(margin(got[None], lines, 2.)[0], -1e-6)

    def test_ordered_offsets_and_no_clear_candidate(self):
        line = np.array([[-200., 0.], [200., 0.]])
        lines = [(line, 100.), (line[:, ::-1].copy(), 100.)]
        got = self.check_move((0., 0.), lines, around=[(1., 1.), (-1., -1.)])
        self.assertLess(margin(got[None], lines, 2.)[0], 0.)
        zero = [(np.zeros((2, 2)), 5.)]
        np.testing.assert_array_equal(self.check_move((0., 0.), zero, around=[(0., -7.), (0., 7.)]), (0., -7.))

    def test_road_ties_repeated_vertices_and_tiny_segments(self):
        for size in [0., 1e-9, 1e-6, 1., 1000.]:
            line = np.array([[0., 0.], [size, 0.], [size, 0.], [size, size]])
            for lines in [[(line, 4.)], [(line, 4.), (line[::-1].copy(), 4.)]]:
                self.check_move((.1, -.2), lines)
        self.check_move((0., 0.), [(np.array([[-10., 0.], [10., 0.]]), 1.)], around=[])

    def test_boundary_tolerance(self):
        road = [(np.array([[-100., 0.], [100., 0.]]), 5.)]
        for delta in [-2e-6, -1e-6, -5e-7, 0., 5e-7, 1e-6]:
            self.check_move((1., 7. + delta), road)

    def test_segment_distance_tie_is_resolved_before_subtracting_road_width(self):
        # Subtracting a width can round distinct segment distances to the same margin. The original
        # algorithm chooses the nearest segment inside each road before comparing roads by margin.
        line = np.array([[-2., 1.+1e-14], [2., 1.+1e-14], [2., -2.], [-1., -2.], [-1., 2.]])
        one_road = self.check_move((0., 0.), [(line, 1000.)])
        two_roads = self.check_move((0., 0.), [(line[:2], 1000.), (line[-2:], 1000.)])
        self.assertGreater(one_road[0], 900.)
        self.assertLess(two_roads[1], -900.)

    def test_seeded_mixed_road_networks(self):
        rng = np.random.default_rng(20261106)
        for case in range(240):
            lines = [(rng.normal(0., 20., (int(rng.integers(2, 9)), 2)), float(rng.uniform(1., 12.)))
                     for _ in range(int(rng.integers(1, 9)))]
            with self.subTest(case=case):
                self.check_move(tuple(rng.normal(0., 8., 2)), lines, clear=float(rng.uniform(.5, 5.)))

    def test_noncontiguous_arrays(self):
        lines = [(np.array([[-100., 0.], [100., 0.]]), 5.)]
        seg = np.zeros((1, 10)); seg[:, ::2] = packed(lines)
        around = np.zeros((len(offsets()), 4)); around[:, ::2] = offsets()
        got = fast._ef.power_off_roads(seg[:, ::2], ends(lines), 1., 2., around[:, ::2], 2.)
        np.testing.assert_array_equal(got, (1., 7.))

    def test_shape_and_finite_validation(self):
        for roads, point, around, clear in [
                (np.zeros((1, 4)), (0., 0.), offsets(), 2.),
                (np.zeros((1, 5)), (0., 0.), np.zeros((1, 3)), 2.),
                (np.full((1, 5), np.nan), (0., 0.), offsets(), 2.),
                (np.empty((0, 5)), (float('inf'), 0.), offsets(), 2.),
                (np.empty((0, 5)), (0., 0.), np.array([[np.nan, 0.]]), 2.),
                (np.empty((0, 5)), (0., 0.), offsets(), float('nan'))]:
            with self.assertRaises(ValueError):
                fast._ef.power_off_roads(roads, np.array([len(roads)], np.int64), *point, around, clear)

    def test_road_end_validation(self):
        segments = np.zeros((3, 5))
        for road_ends in [[], [0, 3], [-1, 3], [2, 1, 3], [1, 4], [1, 2]]:
            with self.assertRaises(ValueError):
                fast._ef.power_off_roads(segments, np.array(road_ends, np.int64), 0., 0., offsets(), 2.)
        segments[1, 4] = 3.
        with self.assertRaises(ValueError):
            fast._ef.power_off_roads(segments, np.array([3], np.int64), 0., 0., offsets(), 2.)

    def test_concurrent_deterministic_search(self):
        rng = np.random.default_rng(19)
        lines = [(rng.normal(0., 20., (12, 2)), 10.) for _ in range(80)]
        seg, around = packed(lines), offsets()
        def solve(_):
            return fast._ef.power_off_roads(seg, ends(lines), 1., 2., around, 2.)
        want = self.check_move((1., 2.), lines)
        with ThreadPoolExecutor(4) as pool:
            for got in pool.map(solve, range(24)):
                np.testing.assert_array_equal(got, want)


if __name__ == '__main__':
    unittest.main()
