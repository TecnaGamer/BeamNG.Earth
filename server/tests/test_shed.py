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
        if sys.platform.startswith("linux") or sys.platform == "win32":
            self.assertGreater(mb, 1.0)

    def test_resident_mb(self):
        """The memory log's "rss": this process's memory in RAM, on Windows too (its working set; it read 0 there)."""
        mb = budget.resident_mb()
        if sys.platform.startswith("linux") or sys.platform == "win32":
            self.assertGreater(mb, 1.0)
            keep = bytearray(64 * 2 ** 20)      # (zeroed: its pages are touched)
            self.assertGreater(budget.resident_mb(), mb + 32.0, "64 MB more in use")
            self.assertGreater(budget.memory_mb(), 64.0)
            del keep
        import blockworker
        self.assertEqual(blockworker._rss_mb() > 1.0, mb is not None)

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
