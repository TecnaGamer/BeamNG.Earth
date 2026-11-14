"""pools.py: a job past its time whose pool's processes still use CPU is busy, not hung (central London's first visit,
2026-11-13: road processes planning big cells for minutes were restarted at 300 s, every tile in flight thrown away).
CPU readings faked, except RotatedPoolTest's (a real pool of one process)."""
import concurrent.futures
import multiprocessing
import os
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pools  # noqa: E402

HUNG_S = 300.0
SPAWN = multiprocessing.get_context("spawn")       # (as the server's pools and test_pools)


def spin(cpu_s):
    """A CPU-bound job: cpu_s s of this process's CPU."""
    t = time.process_time()
    while time.process_time() - t < cpu_s:
        pass
    return "done"


class FakeCpu:
    """CPU seconds per pid: rate (CPU s per s of `clock`) from 0, or None (unreadable)."""

    def __init__(self, rates, clock):
        self.rates, self.clock = rates, clock

    def __call__(self, pid):
        r = self.rates.get(pid)
        return None if r is None else 10.0 + r * self.clock()


def running_future():
    fut = concurrent.futures.Future()
    fut.set_running_or_notify_cancel()
    return fut


class WatchdogCpuTest(unittest.TestCase):
    def setUp(self):
        # (the pool's own watchdog thread asleep for the test: its steps are driven here, at fake times)
        with unittest.mock.patch.object(pools.RespawningPool, "WATCH_S", 3600.0):
            self.pool = pools.RespawningPool("test", 2, hung_s=HUNG_S)
        self.pool.cpu.every = 5.0
        self.now = 0.0
        self.rates = {}
        self.pool.cpu.read = FakeCpu(self.rates, lambda: self.now)
        self.pool._pids = lambda: [101, 102]
        self.recycles = []
        self.pool.recycle = lambda why: self.recycles.append(why) or True
        self.fut = running_future()
        self.pool._live[self.fut] = None

    def tearDown(self):
        self.fut.cancel()
        self.pool.shutdown(wait=False)

    def run_until(self, t_end, step=5.0):
        """Watchdog steps every `step` s up to t_end: the time of the first recycle, or None."""
        while self.now <= t_end:
            if self.pool._watch_step(self.now):
                return self.now
            self.now += step
        return None

    def test_rising_cpu_is_busy_one_warning_no_restart(self):
        self.rates.update({101: 0.9, 102: 0.0})      # (one process planning, the other waiting)
        with self.assertLogs("earthd.pools", "WARNING") as logs:
            self.assertIsNone(self.run_until(2.5 * HUNG_S))
        self.assertEqual(self.recycles, [])
        self.assertEqual(len(logs.records), 1, logs.output)         # (once for the job, not every step)
        msg = logs.output[0]
        self.assertIn("busy", msg)
        self.assertIn("pid 101", msg)
        self.assertIn("54.0 s CPU in the last 60 s", msg)           # (0.9 of a core over the window)

    def test_flat_cpu_is_hung_restart_at_its_time(self):
        self.rates.update({101: 0.0, 102: 0.001})
        t = self.run_until(2 * HUNG_S)
        self.assertEqual(t, HUNG_S)
        self.assertEqual(len(self.recycles), 1)
        self.assertIn("idle", self.recycles[0])

    def test_hard_ceiling_restarts_a_busy_one(self):
        self.rates.update({101: 1.0, 102: 1.0})
        with self.assertLogs("earthd.pools", "WARNING"):
            t = self.run_until(10 * HUNG_S)
        self.assertEqual(t, pools.HUNG_CEILING_X * HUNG_S)
        self.assertEqual(len(self.recycles), 1)
        self.assertIn("past 3x", self.recycles[0])

    def test_busy_then_idle_restarts_once_the_window_is_flat(self):
        self.rates.update({101: 1.0, 102: 0.0})
        with self.assertLogs("earthd.pools", "WARNING"):
            self.assertIsNone(self.run_until(400.0))
        self.pool.cpu.read = FakeCpu(dict(self.rates), lambda: 400.0)     # (frozen at their 400 s readings)
        t = self.run_until(2 * HUNG_S)
        self.assertIsNotNone(t)
        self.assertLessEqual(t, 400.0 + pools.CPU_WINDOW_S + 5.0)

    def test_unreadable_cpu_restarts_at_its_time_as_before(self):
        self.pool.cpu.read = lambda pid: None
        self.assertEqual(self.run_until(2 * HUNG_S), HUNG_S)
        self.assertIn("no answer", self.recycles[0])

    def test_cpu_sampled_at_most_every_few_seconds(self):
        reads = []
        self.pool.cpu.read = lambda pid: reads.append(pid) or 1.0
        for i in range(100):                    # (a step every 0.1 s over 10 s: 2 or 3 reads of 2 processes)
            self.pool._watch_step(i * 0.1)
        self.assertLessEqual(len(reads), 3 * 2)

    def test_no_job_running_reads_nothing(self):
        self.pool._live.clear()
        reads = []
        self.pool.cpu.read = lambda pid: reads.append(pid) or 1.0
        for i in range(20):
            self.assertFalse(self.pool._watch_step(i * 5.0))
        self.assertEqual(reads, [])


class ResultRunningCpuTest(unittest.TestCase):
    """The road tiles' own wait (earthd: result_running with pool=) asks the same: busy waited for on, flat given up."""

    def setUp(self):
        self.pool = pools.RespawningPool("test", 1)
        self.pool.cpu.every = 0.02
        self.pool._pids = lambda: [7]
        self.t0 = time.monotonic()

    def tearDown(self):
        self.pool.shutdown(wait=False)

    def test_busy_waited_for_up_to_the_ceiling(self):
        self.pool.cpu.read = lambda pid: (time.monotonic() - self.t0) * 0.8
        fut = running_future()
        with self.assertLogs("earthd.pools", "WARNING") as logs:
            with self.assertRaises(concurrent.futures.TimeoutError) as cm:
                pools.result_running(fut, 0.3, pool=self.pool, what="road tile 3_4")
        took = time.monotonic() - self.t0
        self.assertGreaterEqual(took, pools.HUNG_CEILING_X * 0.3)
        self.assertIn("past 3x", str(cm.exception))
        self.assertEqual(len(logs.records), 1, logs.output)
        self.assertIn("road tile 3_4", logs.output[0])

    def test_flat_given_up_at_its_time(self):
        self.pool.cpu.read = lambda pid: 5.0
        fut = running_future()
        with self.assertRaises(concurrent.futures.TimeoutError) as cm:
            pools.result_running(fut, 0.3, pool=self.pool)
        took = time.monotonic() - self.t0
        self.assertLess(took, 0.3 * pools.HUNG_CEILING_X - 0.2)
        self.assertIn("idle", str(cm.exception))

    def test_answer_after_its_time_while_busy_is_returned(self):
        self.pool.cpu.read = lambda pid: (time.monotonic() - self.t0) * 0.8
        fut = running_future()
        import threading
        threading.Timer(0.5, fut.set_result, (42,)).start()
        with self.assertLogs("earthd.pools", "WARNING"):
            self.assertEqual(pools.result_running(fut, 0.3, pool=self.pool), 42)

    def test_done_just_as_the_wait_ran_out_is_its_result(self):
        class Late(concurrent.futures.Future):
            """Its timed wait runs out, and the job finishes before the done check."""
            def result(self, timeout=None):
                if timeout is not None and not self.done():
                    self.set_result("finished")
                    raise concurrent.futures.TimeoutError()
                return super().result(timeout)
        fut = Late()
        fut.set_running_or_notify_cancel()
        self.assertEqual(pools.result_running(fut, 5.0, pool=self.pool), "finished")
        self.assertLess(time.monotonic() - self.t0, 1.0)

    def test_the_jobs_own_timeout_error_is_raised_at_once(self):
        """A TimeoutError raised by the job in its worker (its future done) is the caller's, at once: not taken for the
        wait running out and retried in a busy loop until the deadline (Codex review, 2026-11-13)."""
        reads = []
        self.pool.cpu.read = lambda pid: reads.append(pid) or 1.0
        fut = running_future()
        fut.set_exception(TimeoutError("the worker's own"))
        with self.assertRaises(TimeoutError) as cm:
            pools.result_running(fut, 5.0, pool=self.pool)
        self.assertEqual(str(cm.exception), "the worker's own")
        self.assertLess(time.monotonic() - self.t0, 1.0)
        self.assertLessEqual(len(reads), 1)

    def test_without_a_pool_as_before(self):
        fut = running_future()
        with self.assertRaises(concurrent.futures.TimeoutError):
            pools.result_running(fut, 0.1)


class RotatedPoolTest(unittest.TestCase):
    """A job that outlives a rotation (budget.ROTATE_MB) runs in the old executor's process, which its shutdown no
    longer lists: its CPU still counts, and a restart kills it too (Codex review, 2026-11-13)."""

    @unittest.skipUnless(sys.platform == "win32" or sys.platform.startswith("linux"), "CPU readable")
    def test_busy_job_in_a_rotated_process_is_waited_for(self):
        p = pools.RespawningPool("test", 1, mp_context=SPAWN)
        p.cpu.every = 0.1
        try:
            self.assertEqual(p.submit(abs, -1).result(timeout=60), 1)        # (the process started)
            old_pids = {pr.pid for pr in pools._procs(p._pool)}
            fut = p.submit(spin, 2.0)
            while not fut.running():
                time.sleep(0.01)
            time.sleep(0.3)
            self.assertTrue(p.rotate("test"))
            self.assertTrue(old_pids <= set(p._pids()))                    # (still sampled after its shutdown)
            with unittest.mock.patch.object(pools, "HUNG_CEILING_X", 100.0):     # (no ceiling on a slow runner)
                with self.assertLogs("earthd.pools", "WARNING") as logs:
                    self.assertEqual(pools.result_running(fut, 0.5, pool=p, what="spin"), "done")
            self.assertIn("busy", logs.output[0])
        finally:
            p.shutdown()

    def test_recycle_kills_a_rotated_process(self):
        from concurrent.futures.process import BrokenProcessPool
        p = pools.RespawningPool("test", 1, mp_context=SPAWN)
        try:
            self.assertEqual(p.submit(abs, -1).result(timeout=60), 1)
            fut = p.submit(time.sleep, 3600)
            while not fut.running():
                time.sleep(0.01)
            time.sleep(0.3)
            self.assertTrue(p.rotate("test"))
            with self.assertLogs("earthd.pools", "WARNING"):
                self.assertTrue(p.recycle("test"))
            with self.assertRaises(BrokenProcessPool):
                fut.result(timeout=30)                  # (its process killed: not left running for good)
            self.assertEqual(p._retired, [])
            self.assertEqual(p.submit(abs, -2).result(timeout=60), 2)
        finally:
            p.shutdown()


class ProcessCpuTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32" or sys.platform.startswith("linux"), "Linux /proc or Windows API")
    def test_own_cpu_rises(self):
        a = pools.process_cpu_s(os.getpid())
        self.assertIsNotNone(a)
        t = time.process_time()
        while time.process_time() - t < 0.3:
            pass
        b = pools.process_cpu_s(os.getpid())
        self.assertGreater(b, a + 0.1)

    def test_gone_process_is_none(self):
        self.assertIsNone(pools.process_cpu_s(2 ** 30))


if __name__ == "__main__":
    unittest.main()
