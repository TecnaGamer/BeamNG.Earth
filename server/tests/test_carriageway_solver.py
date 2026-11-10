"""Constrained native road profiles; synthetic regressions, including the opt-in grading consumer."""
import concurrent.futures
import os
import sys
import time
import unittest
from unittest import mock

import numpy as np

# run_tests runs modules in separate processes: exercise carriageways even when the product default is off.
os.environ["BNE_INTERCHANGE"] = "1"
os.environ["BNE_CARRIAGEWAYS"] = "on"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import carriageways as cw
import fast
import grading
import mapcells
import revisit
import test_carriageways as legacy
import test_grading
import test_roads
from carriageway_cases import cases, residuals


def road(target, **options):
    t = np.asarray(target, np.float64)
    return dict(t=t, w=np.ones(len(t)), grade=.5, K=.2, lam=2., length=1., **options)


class NativeProfilesTest(unittest.TestCase):
    def solve(self, roads, couplings=(), **options):
        info = {}
        z = cw.solve(roads, couplings, diagnostics=info, **options)
        self.assertTrue(info["converged"], info)
        self.assertTrue(info["feasible"], info)
        return z, info

    def test_unconstrained_matches_independent_dense_objective(self):
        t = np.array([3., 1., -2., 0., 2., 1., 4.])
        c = road(t)
        d1, d2 = np.diff(np.eye(len(t)), axis=0), np.diff(np.eye(len(t)), n=2, axis=0)
        h = np.diag(c["w"]) + c["length"] * d1.T @ d1 + c["lam"] * d2.T @ d2
        (z,), info = self.solve([c])
        np.testing.assert_allclose(z, np.linalg.solve(h, c["w"] * t), atol=1e-11)
        self.assertEqual(info["backend"], "banded")

    def test_analytic_junction_and_clearance(self):
        a, b = road([0.]), road([10.])
        z, _ = self.solve([a, b], [(0, -1, 1, 0, "eq", 0.)])
        np.testing.assert_allclose(np.concatenate(z), [5., 5.], atol=1e-6)
        z, _ = self.solve([a, a], [(0, 0, 1, -1, "ge", 7.)])
        np.testing.assert_allclose(np.concatenate(z), [3.5, -3.5], atol=1e-6)

    def test_empty_short_noncontiguous_and_missing_bounds(self):
        self.assertEqual(self.solve([])[0], [])
        z, _ = self.solve([road([]), road([3.]), road([1., 2.])])
        self.assertEqual([len(q) for q in z], [0, 1, 2])
        c = road(np.arange(12.)[::2], lo=np.full(6, np.nan), hi=np.full(6, np.nan))
        self.assertFalse(c["t"].flags.c_contiguous)
        z, _ = self.solve([c])
        self.assertTrue(np.isfinite(z[0]).all())

    def test_scalar_hard_bound_and_zero_weights(self):
        c = road([30.], lo=[-2.], hi=[5.])
        c["w"][:] = 0.
        (z,), _ = self.solve([c])
        self.assertAlmostEqual(z[0], 5., delta=1e-5)

    def test_feasible_corridors_and_sparse_storage(self):
        for name, (roads, couplings) in cases().items():
            with self.subTest(case=name):
                z, info = self.solve(roads, couplings)
                errors = residuals(roads, couplings, z)
                for metric, error in errors.items():
                    self.assertLess(error, 1e-5, (metric, errors, info))
                n = sum(len(c["t"]) for c in roads)
                self.assertLessEqual(info["objective_nnz"], 3 * n)
                self.assertLessEqual(info["constraint_nnz"], 12 * n + 2 * len(couplings))

    def test_impossible_bounds_and_clearance_cycle_are_not_heights(self):
        for roads, couplings in [([road([20.], lo=[30.], hi=[10.])], []),
                                 ([road([20.]), road([20.])],
                                  [(0, 0, 1, 0, "ge", 7.), (1, 0, 0, 0, "ge", 7.)])]:
            info = {}
            with self.assertLogs(cw.log, level="WARNING"):
                z = cw.solve(roads, couplings, diagnostics=info)
            self.assertTrue(info["infeasible"], info)
            self.assertFalse(info["converged"], info)
            # Clarabel's infeasibility certificate must never be mistaken for world elevations.
            np.testing.assert_allclose(np.concatenate(z), np.full(len(roads), 20.), atol=1e-8)

    def test_budget_and_iteration_exhaustion_are_explicit(self):
        c = road([30.], hi=[5.])
        for options, timed_out in [({"deadline": time.monotonic() - 1.}, True), ({"rounds": 1}, False)]:
            info = {}
            with self.assertLogs(cw.log, level="WARNING"):
                z = cw.solve([c], diagnostics=info, **options)
            self.assertFalse(info["converged"], info)
            self.assertEqual(info["timed_out"], timed_out, info)
            self.assertTrue(np.isfinite(z[0]).all())

    def test_invalid_inputs_fail_before_native_indexing(self):
        for key, value in [("w", [1.]), ("t", [[1., 2.]]), ("lo", [1.]),
                           ("hi", [-np.inf, 0.]), ("grade", -1.), ("K", 0.),
                           ("step", 0.), ("lam", -1.), ("length", np.nan), ("t", [np.inf, 0.])]:
            c = road([1., 2.]); c[key] = value
            with self.subTest(key=key, value=value), self.assertRaises((ValueError, TypeError)):
                cw.solve([c])
        # Aggregate lengths can match while individual chains' arrays are misaligned.
        a, b = road([0., 1.]), road([2., 3.]); a["w"], b["w"] = [1.], [1., 1., 1.]
        with self.assertRaises(ValueError): cw.solve([a, b])
        for cp in [(2, 0, 0, 0, "eq", 0.), (0, 2, 0, 0, "eq", 0.),
                   (0, -3, 0, 0, "eq", 0.), (0, 0, 0, 1, "bad", 0.), (0, 0, 0, 1, "ge", np.nan)]:
            with self.subTest(cp=cp), self.assertRaises(ValueError): cw.solve([road([1., 2.])], [cp])
        with self.assertRaises(ValueError): cw.solve([road([1.])], rounds=0)
        with self.assertRaises(ValueError): cw.solve([road([1.])], deadline=np.nan)
        a = np.zeros(2)
        with self.assertRaises(ValueError):
            fast._ef.carriageway_profiles(a, a, a, a, np.array([0, 3], np.int64), np.ones((1, 5)),
                                         np.empty((0, 5), np.int64), np.empty(0), 10, 1.)

    def test_repeatable_concurrent_calls_leave_inputs_unchanged(self):
        roads, couplings = cases()["stacked_corridor"]
        original = [c["t"].copy() for c in roads]
        expected = cw.solve(roads, couplings)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: cw.solve(roads, couplings), range(4)))
        for z in results:
            for got, want in zip(z, expected): np.testing.assert_array_equal(got, want)
        for c, want in zip(roads, original): np.testing.assert_array_equal(c["t"], want)

    @unittest.skipUnless(__import__("importlib.util").util.find_spec("scipy"), "SciPy (the test-only reference) not installed")
    def test_small_qp_matches_independent_slsqp(self):
        # A dense, independently assembled reference for small constrained systems, never used at runtime.
        from scipy.optimize import minimize
        for seed in range(3):
            rng = np.random.default_rng(seed)
            roads = [road(rng.normal(0., 2., 5), lo=np.full(5, -3.), hi=np.full(5, 3.)) for _ in range(2)]
            cp = [(0, -1, 1, 0, "eq", 0.), (0, 1, 1, 3, "ge", 1.)]
            target = np.concatenate([c["t"] for c in roads])
            h = np.zeros((10, 10)); a = []; b = []
            for k, c in enumerate(roads):
                d1, d2 = np.diff(np.eye(5), axis=0), np.diff(np.eye(5), n=2, axis=0)
                h[k*5:k*5+5, k*5:k*5+5] = np.eye(5) + d1.T @ d1 + 2. * d2.T @ d2
                for d, limit in [(d1, c["grade"] * 4.), (d2, 16. / (c["K"] * 100))]:
                    for row in d:
                        r = np.zeros(10); r[k*5:k*5+5] = row
                        a.extend([r, -r]); b.extend([limit, limit])
            eq = np.zeros(10); eq[4], eq[5] = 1., -1.
            ge = np.zeros(10); ge[1], ge[8] = 1., -1.
            a, b = np.asarray(a), np.asarray(b)
            objective = lambda z: .5 * z @ h @ z - target @ z
            reference = minimize(objective, np.zeros(10), jac=lambda z: h @ z - target,
                                 method="SLSQP", bounds=[(-3., 3.)] * 10,
                                 constraints=[dict(type="ineq", fun=lambda z: b-a@z, jac=lambda z: -a),
                                              dict(type="eq", fun=lambda z: eq@z, jac=lambda z: eq),
                                              dict(type="ineq", fun=lambda z: ge@z-1., jac=lambda z: ge)],
                                 options=dict(ftol=1e-11, maxiter=500))
            self.assertTrue(reference.success, reference.message)
            z, _ = self.solve(roads, cp)
            np.testing.assert_allclose(np.concatenate(z), reference.x, atol=2e-5)


class GradingConsumerTest(legacy.StackTest):
    def test_failed_region_is_not_persisted(self):
        f = test_roads.features([[(-200., 0.), (200., 0.)]], ["motorway"])
        g = grading.Grader(test_roads.FRAME, test_grading.FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.))
        planner = g.store.planners_of(np.array([test_roads.FRAME.lat0]),
                                     np.array([test_roads.FRAME.lon0]))[0][0]
        home = mapcells.cell_of(test_roads.FRAME.lat0, test_roads.FRAME.lon0)
        shared = mock.Mock()
        shared.region_claim.return_value = (None, True)
        planner.shared = shared
        faults = revisit.thread_faults()
        def exhausted(chains, couplings=(), diagnostics=None, **kwargs):
            diagnostics.update(converged=False, feasible=False, status="MaxTime")
            return [np.asarray(c["t"]).copy() for c in chains]
        with mock.patch.object(cw, "solve", side_effect=exhausted):
            region = planner._cw_region(home)
        self.assertTrue(region["failed"])
        self.assertEqual(region["prof"], {})
        self.assertGreater(revisit.thread_faults(), faults)
        shared.region_store.assert_not_called()
        shared.region_release.assert_called_once()
        with mock.patch.object(planner, "_cw_region", return_value=region):
            for _ in range(2):
                faults = revisit.thread_faults()
                self.assertIsNone(planner._cw_z_at(np.array([[0., 0.], [4., 0.]]), np.array([[2., 0.]])))
                self.assertGreater(revisit.thread_faults(), faults)


if __name__ == "__main__": unittest.main()
