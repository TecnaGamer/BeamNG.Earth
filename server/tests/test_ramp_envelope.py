"""grading.ramp_envelope: the roads round a raised one ramp up to it (GitHub #1327: its nodes as arrays)."""
import os
import sys
import time
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402


def _plan(xy, z, half=4.0, rid=0):
    p = grading.Plan(np.asarray(xy, np.float64), np.asarray(z, np.float64), half)
    p.rid = rid
    return p


def _street(cls="residential"):
    return types.SimpleNamespace(cls=cls, link=False)


class RampEnvelopeTest(unittest.TestCase):

    def test_a_side_road_joining_a_raised_roads_middle_ramps_up_to_it(self):
        # (the node on the raised road's middle: the side road's end meets its nearest vertex, 3 m over the ground)
        s = np.arange(0.0, 201.0, 4.0)
        main = _plan(np.c_[s, np.zeros_like(s)], np.full(len(s), 3.0), rid=1)
        side = _plan(np.c_[np.full(len(s), 100.0), -2.0 - s], np.zeros(len(s)), rid=2)
        plans = [("main", main), ("side", side)]
        grading.ramp_envelope(plans, [_street(), _street()], [0])
        z = np.asarray(plans[1][1].z)
        self.assertAlmostEqual(float(z[0]), 3.0 - grading.RAMP_GRADE * 0.0, delta=0.15)   # (at the main road)
        ds = np.diff(s)
        self.assertLessEqual(float(np.max(np.abs(np.diff(z)) / ds)), grading.RAMP_GRADE + 1e-9)
        self.assertAlmostEqual(float(z[-1]), 0.0, places=6)         # (past the ramp: its own ground)
        np.testing.assert_array_equal(np.asarray(plans[0][1].z), np.full(len(s), 3.0))  # (the raised one as it was)

    def test_a_dense_grid_is_quick(self):
        # (400 streets, thousands of nodes against every candidate road: 0.8 s as Python loops, GitHub #1327)
        rng = np.random.default_rng(99)
        plans, specs = [], []
        for k in range(400):
            c = (k // 2) * 25.0
            a0, a1 = sorted(rng.uniform(-200.0, 5200.0, 2))
            s = np.arange(a0, a1 + 8.0, 4.0)
            xy = np.c_[s, np.full(len(s), c)] if k % 2 == 0 else np.c_[np.full(len(s), c), s]
            plans.append((f"f{k}", _plan(xy, 10.0 + 0.01 * xy[:, 0], float(rng.uniform(3.0, 7.0)), k)))
            specs.append(_street())
        seeds = [3, 50, 120, 333]
        for i in seeds:
            plans[i][1].z = np.asarray(plans[i][1].z) + 3.0
        t0 = time.perf_counter()
        grading.ramp_envelope(plans, specs, seeds)
        self.assertLess(time.perf_counter() - t0, 0.5)


if __name__ == "__main__":
    unittest.main()
