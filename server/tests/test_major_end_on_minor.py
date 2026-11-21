"""A higher-ranked road that ends on a lower road's middle (Mt Fuji, Subaru Line's 5th station: a secondary ends on the
parking lot's service loop) meets it: the lower road takes the end's height there, holds it across the higher road's
carriageway and returns to its own profile (grading.snap_junctions, JUNCTION_MAJOR_END_ON)."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402

GROUND = lambda x, y: 10.0 + 0.10 * x + 0.05 * y        # a hillside: 10 % up to the east, 5 % to the north


def _case():
    s = np.arange(0.0, 81.0, 4.0)
    # the secondary: from the node (0, 0) east, on the ground (12 %), its core 9.05 m, carriageway 4 m (as built)
    main = grading.Plan(np.stack([s, 0 * s], 1), GROUND(s, 0 * s), 9.05, road_half=4.0)
    # the service loop through the node, a V there (in from the north-west, out to the south-west, away from the
    # secondary), its profile smoothed 0.7 m under the ground at the node (as built: 0.88 against the end's 1.58)
    t = np.arange(-60.0, 61.0, 4.0)
    xy = np.stack([-0.6 * np.abs(t), 0.8 * t], 1)
    svc = grading.Plan(xy, GROUND(xy[:, 0], xy[:, 1]) - 0.7 * np.exp(-(t / 30.0) ** 2), 3.0, road_half=2.0)
    specs = [roads.Spec(8.0, "paved", False, "secondary", False), roads.Spec(4.0, "paved", False, "service", False)]
    main.rank = grading.GRADE_RANK_BUILT + grading.road_rank(specs[0])
    svc.rank = grading.GRADE_RANK_BUILT + grading.road_rank(specs[1])
    return main, svc, specs


def _step(main, svc):
    """The steepest change of the graded surface in 0.25 m, round the secondary's end."""
    gx, gy = np.meshgrid(np.arange(-8.0, 8.01, 0.25), np.arange(-8.0, 8.01, 0.25))
    z = grading.grade_points(gx.ravel(), gy.ravel(), GROUND(gx, gy).ravel(), [main, svc], grading.ROAD_LIFT)
    z = z.reshape(gx.shape)
    return max(np.abs(np.diff(z, axis=0)).max(), np.abs(np.diff(z, axis=1)).max())


class MajorEndOnMinorRoad(unittest.TestCase):
    def test_the_minor_road_meets_the_major_roads_end(self):
        main, svc, specs = _case()
        z_end = float(main.z[0])
        grading.snap_junctions([main, svc], specs)
        mid = int(np.argmin(np.hypot(*np.asarray(svc.xy)[:, :2].T)))
        self.assertAlmostEqual(float(svc.z[mid]), z_end, delta=0.02)          # (0.70 m apart without it)
        self.assertAlmostEqual(float(main.z[0]), z_end, places=6)              # the major road keeps its profile

    def test_no_step_round_the_end(self):
        main, svc, specs = _case()
        grading.snap_junctions([main, svc], specs)
        step = _step(main, svc)
        self.assertLess(step, 0.15, f"a {step:.2f} m step in 0.25 m beside the major road's end")

    def test_held_across_the_carriageway_then_back_to_its_own_profile(self):
        main, svc, specs = _case()
        own = np.asarray(svc.z, float).copy()
        z_end = float(main.z[0])
        grading.snap_junctions([main, svc], specs)
        xy = np.asarray(svc.xy)[:, :2]
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        mid = int(np.argmin(np.hypot(*xy.T)))
        d = np.abs(s - s[mid])
        z = np.asarray(svc.z, float)
        hold = main.road_half + grading.JUNCTION_HOLD_PAD_M
        self.assertTrue(np.allclose(z[d <= hold], z_end, atol=1e-9))         # flat under the secondary's carriageway
        dz = np.abs(np.diff(z)) / np.diff(s)
        self.assertLessEqual(float(dz[d[1:] > hold + 4.0].max()) if (d[1:] > hold + 4.0).any() else 0.0,
                             0.12 + 0.11 + 1e-9)                                # (back at most as steep as it is itself)
        self.assertTrue(np.allclose(z[d >= grading.JUNCTION_TAPER_M], own[d >= grading.JUNCTION_TAPER_M]))

    def test_the_same_class_is_left_alone(self):
        main, svc, specs = _case()
        own = np.asarray(svc.z, float).copy()
        grading.snap_junctions([main, svc], [specs[0], specs[0]])           # (two secondaries: no higher road)
        self.assertTrue(np.array_equal(own, svc.z))

    def test_off_switch(self):
        main, svc, specs = _case()
        own = np.asarray(svc.z, float).copy()
        with mock.patch.object(grading, "JUNCTION_MAJOR_END_ON", False):
            grading.snap_junctions([main, svc], specs)
        self.assertTrue(np.array_equal(own, svc.z))

    def test_a_gap_over_the_junction_limit_is_left_as_it_was(self):
        main, svc, specs = _case()
        svc.z = np.asarray(svc.z, float) - 8.0
        own = np.asarray(svc.z, float).copy()
        grading.snap_junctions([main, svc], specs)
        self.assertTrue(np.array_equal(own, svc.z))

    def test_a_road_ending_near_the_minor_roads_own_end_is_the_end_snaps(self):
        """The minor road's own end is the end snap's: a secondary ending 3 m past the service road's last vertex
        changes nothing more than that snap does."""
        def run(on):
            s = np.arange(0.0, 41.0, 4.0)
            main = grading.Plan(np.stack([s, 0 * s + 3.0], 1), 11.0 + 0 * s, 9.05, road_half=4.0)
            svc = grading.Plan(np.stack([0 * s, s - 40.0], 1), 10.0 + 0 * s, 3.0, road_half=2.0)
            specs = [roads.Spec(8.0, "paved", False, "secondary", False),
                     roads.Spec(4.0, "paved", False, "service", False)]
            main.rank = grading.GRADE_RANK_BUILT + grading.road_rank(specs[0])
            svc.rank = grading.GRADE_RANK_BUILT + grading.road_rank(specs[1])
            with mock.patch.object(grading, "JUNCTION_MAJOR_END_ON", on):
                grading.snap_junctions([main, svc], specs)
            return np.asarray(svc.z, float)
        self.assertTrue(np.array_equal(run(True), run(False)))


    def test_a_road_beside_a_higher_road_that_runs_on_is_left_alone(self):
        """A bus lane along a primary stays coupled to it: a service road ending on the lane does not pull it."""
        s = np.arange(0.0, 81.0, 4.0)
        t = np.arange(-60.0, 61.0, 4.0)
        lane = grading.Plan(np.stack([0 * t, t], 1), 10.0 + 0 * t, 3.0, road_half=3.5)
        primary = grading.Plan(np.stack([8.0 + 0 * t, t], 1), 10.0 + 0 * t, 6.0, road_half=5.0)
        service = grading.Plan(np.stack([-s - 2.0, 0 * s], 1), 10.6 + 0 * s, 3.0, road_half=2.0)
        specs = [roads.Spec(7.0, "paved", False, "busway", False), roads.Spec(10.0, "paved", False, "primary", False),
                 roads.Spec(4.0, "paved", False, "service", False)]
        own = np.asarray(lane.z, float).copy()
        grading.snap_junctions([lane, primary, service], specs)
        self.assertTrue(np.array_equal(own, lane.z))


if __name__ == "__main__":
    unittest.main(verbosity=2)
