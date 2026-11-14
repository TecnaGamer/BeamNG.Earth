"""budget.maybe_shed / pools._call: a worker over its memory ceiling (small machines) drops its registered caches
between jobs; under it, or with the ceiling off (a desktop), nothing happens."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget  # noqa: E402
import pools  # noqa: E402


class ShedTest(unittest.TestCase):
    def setUp(self):
        self._old = (budget.SHED_MB, list(budget._shedders), budget.shed_count)
        budget._last_shed = None
        self.calls = []
        budget._shedders[:] = [lambda: self.calls.append(1)]

    def tearDown(self):
        budget.SHED_MB, budget._shedders[:], budget.shed_count = self._old[0], self._old[1], self._old[2]

    def test_off(self):
        budget.SHED_MB = 0
        self.assertFalse(budget.maybe_shed())
        self.assertEqual(self.calls, [])

    def test_under(self):
        budget.SHED_MB = 10 ** 9
        self.assertFalse(budget.maybe_shed())
        self.assertEqual(self.calls, [])

    def test_over(self):
        budget.SHED_MB = 1
        with mock.patch.object(budget, "memory_mb", return_value=5000.0):
            self.assertTrue(budget.maybe_shed())
        self.assertEqual(self.calls, [1])

    def test_at_most_every_min_s(self):
        budget.SHED_MB = 1
        with mock.patch.object(budget, "memory_mb", return_value=5000.0):
            self.assertTrue(budget.maybe_shed())
            self.assertFalse(budget.maybe_shed())          # (SHED_MIN_S not over yet)
            budget._last_shed -= budget.SHED_MIN_S + 1
            self.assertTrue(budget.maybe_shed())
        self.assertEqual(self.calls, [1, 1])

    def test_failing_shedder_does_not_stop_others(self):
        budget.SHED_MB = 1
        budget._shedders[:] = [lambda: 1 / 0, lambda: self.calls.append(2)]
        with mock.patch.object(budget, "memory_mb", return_value=5000.0):
            budget.maybe_shed()
        self.assertEqual(self.calls, [2])

    def test_memory_mb(self):
        mb = budget.memory_mb()
        if sys.platform.startswith("linux"):
            self.assertGreater(mb, 1.0)

    def test_call_runs_job_then_sheds(self):
        budget.SHED_MB = 1
        with mock.patch.object(budget, "memory_mb", return_value=5000.0):
            self.assertEqual(pools._call(lambda a, b=0: a + b, (2,), {"b": 3}), 5)
            budget._last_shed = None
            with self.assertRaises(ZeroDivisionError):        # (a failed job sheds too, and its error goes on)
                pools._call(lambda: 1 / 0, (), {})
        self.assertEqual(self.calls, [1, 1])

    def test_apply_sets_ceiling(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BNGE_SHED_MB", None)
            with mock.patch.object(budget, "_malloc_small"):
                budget.apply(is_small=False)
                self.assertEqual(budget.SHED_MB, 0)


class ClearMemoryTest(unittest.TestCase):
    def test_roadwidth_memo_capped(self):
        import roadwidth
        self.assertGreater(roadwidth.MEMO_MAX, 1000)

    def test_clearers_exist(self):
        import dem
        import mapdata
        import roads
        import roadwidth
        for obj, name in ((dem.Dem, "clear_memory"), (mapdata.MapData, "clear_memory"),
                          (roadwidth.RoadWidths, "clear_memory"), (roads, "clear_facts_memory")):
            self.assertTrue(callable(getattr(obj, name)), name)


if __name__ == "__main__":
    unittest.main()


class IdleShedTest(unittest.TestCase):
    """budget.idle_shed_now: a worker with no job for IDLE_SHED_S drops its caches once (GitHub #1026)."""

    def setUp(self):
        self._old = (list(budget._shedders), budget._last_job_end, budget._idle_shed, budget._idle_thread,
                     budget.IDLE_SHED_S)
        self.calls = []
        budget._shedders[:] = [lambda: self.calls.append(1)]
        budget._idle_thread = True          # (no watcher thread: the test asks itself)
        budget.IDLE_SHED_S = 300.0

    def tearDown(self):
        (budget._shedders[:], budget._last_job_end, budget._idle_shed, budget._idle_thread,
         budget.IDLE_SHED_S) = self._old

    def test_once_after_idle(self):
        budget.job_ended()
        t = budget._last_job_end
        self.assertFalse(budget.idle_shed_now(t + 10))
        self.assertTrue(budget.idle_shed_now(t + 301))
        self.assertFalse(budget.idle_shed_now(t + 900))         # (once until the next job)
        budget.job_ended()
        self.assertTrue(budget.idle_shed_now(budget._last_job_end + 301))
        self.assertEqual(self.calls, [1, 1])

    def test_never_during_a_job(self):
        budget.job_ended()
        t = budget._last_job_end
        with budget.job_lock():
            self.assertFalse(budget.idle_shed_now(t + 301))
        self.assertEqual(self.calls, [])

    def test_off(self):
        budget.IDLE_SHED_S = 0
        budget.job_ended()
        self.assertFalse(budget.idle_shed_now(budget._last_job_end + 10 ** 6))

    def test_a_job_through_the_pool_starts_the_clock(self):
        budget._last_job_end = None
        self.assertEqual(pools._call(lambda a: a + 1, (1,), {}), 2)
        self.assertIsNotNone(budget._last_job_end)
