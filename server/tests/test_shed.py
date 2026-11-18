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
            # (Windows: private bytes; it returned None there until 2026-11-11, a ctypes argument overflow, and the
            # ceiling never ran on Windows)
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
            os.environ.pop("BNGE_LOW_FREE_GB", None)
            with mock.patch.object(budget, "_malloc_small"):
                with mock.patch.object(budget, "total_gb", return_value=64.0):
                    budget.apply(is_small=False)
                    self.assertEqual(budget.SHED_MB, 3000, "a 64 GB desktop: the 24 GB cap's ceiling")
                    self.assertEqual(budget.LOW_FREE_GB, budget.LOW_FREE_MAX_GB)
                with mock.patch.object(budget, "total_gb", return_value=31.9):
                    budget.apply(is_small=False)
                    self.assertEqual(budget.SHED_MB, 3000, "a 32 GB desktop (GitHub #893, OOM after 90 miles)")
                    self.assertAlmostEqual(budget.LOW_FREE_GB, 0.08 * 31.9)

    def test_ceilings_by_ram(self):
        self.assertEqual(budget.shed_mb(True, 14.5), budget.SMALL_SHED_MB, "a Steam Deck")
        self.assertEqual(budget.shed_mb(True, 15.6), budget.SMALL_SHED_MB, "a 16 GB PC")
        self.assertEqual(budget.shed_mb(True, 7.8), budget.TINY_SHED_MB, "an 8 GB PC")
        self.assertEqual(budget.shed_mb(True, None), budget.SMALL_SHED_MB)
        # a desktop's by the RAM limit (Auto: 60 % of the RAM from 22 GB, at most 24 GB; budget.desktop_sizes)
        with mock.patch.dict(os.environ, {budget.SETTING_ENV: ""}):
            d24, d32, d48, d62 = (budget.shed_mb(False, gb) for gb in (24.0, 32.0, 48.0, 62.0))
            self.assertTrue(2000 <= d24 <= d32 <= d48 <= d62, (d24, d32, d48, d62))
            self.assertEqual(d32, 3000, "a 32 GB PC's Auto (17.9 GB): the full ceiling")
            self.assertEqual(d62, 3000, "the cap's")
            self.assertEqual(budget.rotate_mb(False, 62.0), 4000)
            self.assertEqual(budget.shed_mb(False, None), 0)
        with mock.patch.dict(os.environ, {budget.SETTING_ENV: "8"}):    # (the player's 8 GB on a 62 GB PC)
            self.assertLess(budget.shed_mb(False, 62.0), d24)
        self.assertEqual(budget.low_free_gb(8.0), budget.LOW_FREE_MIN_GB)
        self.assertEqual(budget.low_free_gb(128.0), budget.LOW_FREE_MAX_GB)
        self.assertEqual(budget.low_free_gb(None), budget.LOW_FREE_MIN_GB)

    def test_total_gb_override(self):
        with mock.patch.dict(os.environ, {"BNGE_TOTAL_GB": "16"}):
            self.assertEqual(budget.total_gb(), 16.0)


class LowFreeShedTest(unittest.TestCase):
    """budget.maybe_shed: the system's own free memory (every profile): a worker holding SHED_FLOOR_MB drops its caches
    while less than LOW_FREE_GB is free, before the system swaps the game's memory."""

    def setUp(self):
        self._old = (budget.SHED_MB, budget.LOW_FREE_GB, list(budget._shedders), budget.shed_count,
                     budget.low_free_sheds)
        budget._last_shed = None
        self.calls = []
        budget._shedders[:] = [lambda: self.calls.append(1)]
        budget.SHED_MB, budget.LOW_FREE_GB = 0, 2.0

    def tearDown(self):
        (budget.SHED_MB, budget.LOW_FREE_GB, budget._shedders[:], budget.shed_count,
         budget.low_free_sheds) = self._old

    def test_sheds_when_the_system_is_short(self):
        with mock.patch.object(budget, "memory_mb", return_value=900.0), \
                mock.patch.object(budget, "free_gb", return_value=1.2):
            self.assertTrue(budget.maybe_shed())
        self.assertEqual(self.calls, [1])
        self.assertEqual(budget.low_free_sheds, self._old[4] + 1)

    def test_not_with_memory_free(self):
        with mock.patch.object(budget, "memory_mb", return_value=900.0), \
                mock.patch.object(budget, "free_gb", return_value=5.0):
            self.assertFalse(budget.maybe_shed())
        self.assertEqual(self.calls, [])

    def test_not_a_small_worker(self):
        with mock.patch.object(budget, "memory_mb", return_value=budget.SHED_FLOOR_MB - 1.0), \
                mock.patch.object(budget, "free_gb", return_value=0.5):
            self.assertFalse(budget.maybe_shed())
        self.assertEqual(self.calls, [])

    def test_unknown_free_memory(self):
        with mock.patch.object(budget, "memory_mb", return_value=900.0), \
                mock.patch.object(budget, "free_gb", return_value=None):
            self.assertFalse(budget.maybe_shed())

    def test_off(self):
        budget.LOW_FREE_GB = 0.0
        with mock.patch.object(budget, "memory_mb", return_value=900.0), \
                mock.patch.object(budget, "free_gb", return_value=0.1):
            self.assertFalse(budget.maybe_shed())

    def test_at_most_every_min_s(self):
        with mock.patch.object(budget, "memory_mb", return_value=900.0), \
                mock.patch.object(budget, "free_gb", return_value=0.5):
            self.assertTrue(budget.maybe_shed())
            self.assertFalse(budget.maybe_shed())
        self.assertEqual(self.calls, [1])


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


class TreeBudgetTest(unittest.TestCase):
    """budget's whole-server memory table: every process's memory in a shared slot; a worker over its share of
    TREE_MB drops its caches while the server is over it (a 32 GB PC's server: 19.5 GB in 21 processes after eight
    minutes of a fresh Los Angeles, each under its own caps; 2026-11-11)."""

    def setUp(self):
        self._old = (budget.SHED_MB, budget.LOW_FREE_GB, budget.TREE_MB, list(budget._shedders),
                     budget.tree_sheds, budget._tree_slot)
        budget._last_shed = None
        self.calls = []
        budget._shedders[:] = [lambda: self.calls.append(1)]

    def tearDown(self):
        (budget.SHED_MB, budget.LOW_FREE_GB, budget.TREE_MB, budget._shedders[:], budget.tree_sheds,
         budget._tree_slot) = self._old
        budget.tree_close()
        os.environ.pop(budget.TREE_ENV, None)

    def test_off_unless_asked(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(budget, "_malloc_small"), \
                mock.patch.object(budget, "total_gb", return_value=32.0):
            os.environ.pop("BNGE_TREE_MB", None)
            with mock.patch.object(budget, "TREE_ON", False):
                budget.apply(is_small=False)
                self.assertEqual(budget.TREE_MB, 0, "out of reach in the drives: opt-in (BNGE_TREE=on)")
            with mock.patch.object(budget, "TREE_ON", True):
                budget.apply(is_small=False)
                self.assertEqual(budget.TREE_MB, 10560)

    def test_budget_by_ram(self):
        self.assertEqual(budget.tree_mb(32.0), 10560)
        self.assertEqual(budget.tree_mb(6.0), budget.TREE_MIN_MB)
        self.assertEqual(budget.tree_mb(None), 0)

    def test_the_big_ones_first(self):
        self.assertFalse(budget.tree_should_shed(1500, 9000, 12, 10000), "under the budget")
        self.assertTrue(budget.tree_should_shed(1500, 12000, 12, 10000), "over it, and over an equal share")
        self.assertFalse(budget.tree_should_shed(700, 12000, 12, 10000), "under its share (833 MB)")
        self.assertFalse(budget.tree_should_shed(budget.SHED_FLOOR_MB - 1, 12000, 2, 2500), "too small to matter")
        self.assertFalse(budget.tree_should_shed(5000, 12000, 12, 0), "off")

    def test_slots_shared_and_summed(self):
        self.assertTrue(budget.tree_init())
        budget._tree_slot = None
        now = 1000.0
        for pid, mb in ((101, 900.0), (102, 1500.0), (103, 300.0)):
            budget._tree_slot = None
            with mock.patch.object(os, "getpid", return_value=pid):
                self.assertTrue(budget.tree_publish(mb, now=now))
        with mock.patch.object(os, "getpid", return_value=102):
            budget._tree_slot = None
            budget.tree_publish(1600.0, now=now + 1)       # (its own slot again, not a new one)
        self.assertEqual(budget.tree_total(now=now + 2), (2800.0, 3))
        self.assertEqual(budget.tree_total(now=now + budget.TREE_FRESH_S + 0.5), (1600.0, 1), "gone processes age out")
        budget._tree_slot = None
        with mock.patch.object(os, "getpid", return_value=104):     # (a stale slot taken over)
            self.assertTrue(budget.tree_publish(200.0, now=now + budget.TREE_FRESH_S + 1))
        self.assertEqual(budget.tree_total(now=now + budget.TREE_FRESH_S + 1)[1], 2)

    def test_no_table(self):
        os.environ.pop(budget.TREE_ENV, None)
        budget.tree_close()
        self.assertFalse(budget.tree_publish(100.0))
        self.assertEqual(budget.tree_total(), (0.0, 0))

    def test_maybe_shed_on_the_whole_server(self):
        budget.SHED_MB, budget.LOW_FREE_GB, budget.TREE_MB = 0, 0.0, 10000
        with mock.patch.object(budget, "memory_mb", return_value=1500.0), \
                mock.patch.object(budget, "tree_total", return_value=(12000.0, 12)):
            self.assertTrue(budget.maybe_shed())
        self.assertEqual(self.calls, [1])
        budget._last_shed = None
        with mock.patch.object(budget, "memory_mb", return_value=1500.0), \
                mock.patch.object(budget, "tree_total", return_value=(9000.0, 12)):
            self.assertFalse(budget.maybe_shed())

    def test_the_servers_own_process_not_by_a_workers_ceiling(self):
        budget.SHED_MB, budget.LOW_FREE_GB, budget.TREE_MB = 1000, 0.0, 10000
        with mock.patch.object(budget, "memory_mb", return_value=2500.0), \
                mock.patch.object(budget, "tree_total", return_value=(9000.0, 12)), \
                mock.patch("gc.collect") as collect:
            self.assertFalse(budget.maybe_shed(ceiling=False, collect=False), "over SHED_MB, the server under budget")
            self.assertTrue(budget.maybe_shed(ceiling=True, collect=False))
            collect.assert_not_called()


class RotateByRamTest(unittest.TestCase):
    def test_desktops_rotate_too(self):
        self.assertEqual(budget.rotate_mb(True, 15.6), budget.SMALL_ROTATE_MB)
        self.assertEqual(budget.rotate_mb(False, 31.9), 4000, "a 32 GB desktop grows the same way, later")
        self.assertEqual(budget.rotate_mb(False, 48.0), 4000)
        self.assertEqual(budget.rotate_mb(False, 62.0), 4000, "the RAM limit's cap")
        self.assertEqual(budget.rotate_mb(False, None), 0)
        self.assertGreater(budget.rotate_mb(False, 31.9), budget.shed_mb(False, 31.9), "a shed first, rotation after")


class HeldMemoryTest(unittest.TestCase):
    def test_arrays_counted_once_for_the_first_holder(self):
        import numpy as np
        import blockworker
        a = np.zeros(3 * 2 ** 20, np.uint8)
        b = np.zeros(5 * 2 ** 20, np.uint8)
        got = blockworker.held_memory([("x", {"a": a, "view": a[10:]}), ("y", [a, {"b": b}]), ("z", [1, "s"])],
                                      min_mb=1.0)
        self.assertEqual(got, [(5.0, "y"), (3.0, "x")])
