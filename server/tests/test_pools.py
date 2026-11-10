"""pools.py: a process pool whose worker died is made again (GitHub #76, #91)."""
import os
import sys
import unittest
import unittest.mock
from concurrent.futures.process import BrokenProcessPool

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pools  # noqa: E402

import multiprocessing  # noqa: E402

# The server's pools spawn their processes (earthd); forked here, a pool made again right after its worker was killed
# could fork while another thread held a lock, and the new worker never started: test_a_hung_worker_is_restarted hung
# the CI 600 s (2026-11-05, ubuntu-22.04, Python 3.12) in the final shutdown
SPAWN = multiprocessing.get_context("spawn")


class NameTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "process names: Linux /proc")
    def test_workers_are_named_after_their_pool(self):
        import pathlib
        p = pools.RespawningPool("far building", 1, mp_context=SPAWN)
        try:
            comm = p.submit(pathlib.Path.read_text, pathlib.Path("/proc/self/comm")).result(timeout=60).strip()
            self.assertEqual(comm, "bnge:farbuildin")       # (15 bytes at most)
        finally:
            p.shutdown()

    def test_the_initializer_still_runs(self):
        seen = []
        pools._named_init("test", seen.append, 7)
        self.assertEqual(seen, [7])


class RespawnTest(unittest.TestCase):
    def test_a_dead_worker_is_replaced(self):
        p = pools.RespawningPool("test", 1, mp_context=SPAWN)
        try:
            self.assertEqual(p.submit(abs, -3).result(), 3)
            with self.assertRaises(BrokenProcessPool):
                p.submit(os._exit, 1).result()      # the worker dies
            with self.assertLogs("earthd.pools", "WARNING"):
                self.assertEqual(p.submit(abs, -4).result(), 4)
            self.assertEqual(p.respawns, 1)
        finally:
            p.shutdown()

    def test_not_made_again_and_again(self):
        p = pools.RespawningPool("test", 1, mp_context=SPAWN)
        try:
            with unittest.mock.patch.object(pools, "RESPAWN_S", 3600.0):
                for _ in range(2):
                    try:
                        p.submit(os._exit, 1).result()
                    except BrokenProcessPool:
                        pass
                with self.assertRaises(BrokenProcessPool):
                    p.submit(abs, -1)
            self.assertEqual(p.respawns, 1)
        finally:
            p.shutdown()

    def test_a_hung_worker_is_restarted(self):
        """A worker that never answers (alive, so the pool is not broken) is killed by recycle(): the call in flight
        ends with BrokenProcessPool and the next one is served (a road tile hung for minutes, 1.7.0-beta)."""
        import time
        p = pools.RespawningPool("test", 1, mp_context=SPAWN)
        try:
            hung = p.submit(time.sleep, 3600)
            time.sleep(0.5)                     # (it is running)
            with self.assertLogs("earthd.pools", "WARNING"):
                self.assertTrue(p.recycle("test"))
            with self.assertRaises(BrokenProcessPool):
                hung.result(timeout=30)
            self.assertEqual(p.submit(abs, -5).result(timeout=30), 5)
            self.assertFalse(p.recycle("again at once"))    # (at most every RESPAWN_S)
        finally:
            p.shutdown()

    def test_timeout_counted_from_its_start(self):
        """result_running: a job queued behind slow ones is not timed out for its wait (GitHub #663: road tiles in a
        backlog taken for hung workers), one that runs too long is."""
        import concurrent.futures
        import time
        p = pools.RespawningPool("test", 1, mp_context=SPAWN)
        try:
            # (4 s behind them, more than its 2.5 s; a process pool marks a job running once it is in the worker's call
            # queue, one job ahead of where it runs: the margin covers that and a slow Windows runner's start,
            # CI 2026-11-03: 0.8 s jobs and a 1.5 s timeout failed there)
            slow = [p.submit(time.sleep, 1.0) for _ in range(4)]
            last = p.submit(abs, -7)
            self.assertEqual(pools.result_running(last, 2.5), 7)
            self.assertTrue(all(f.done() for f in slow))
            with self.assertRaises(concurrent.futures.TimeoutError):
                pools.result_running(p.submit(time.sleep, 3), 0.5)
        finally:
            p.shutdown()

    def test_low_jobs_wait_for_the_rest(self):
        """submit_low: handed over while a process is free of the other jobs, else one at a time beside them; the
        other jobs go first (trees geotiles after the road tiles, 2026-11-02)."""
        import time
        p = pools.RespawningPool("test", 2, mp_context=SPAWN)
        try:
            busy = [p.submit(time.sleep, 1.0) for _ in range(4)]
            low = [p.submit_low(abs, -i) for i in range(1, 4)]
            time.sleep(0.3)
            self.assertEqual(sum(f.running() or f.done() for f in low), 1)     # (one beside them)
            later = p.submit(abs, -9)
            self.assertEqual(later.result(timeout=30), 9)
            self.assertEqual([f.result(timeout=30) for f in low], [1, 2, 3])     # (one at a time: never starved)
            cancelled = p.submit_low(time.sleep, 0)
            self.assertIn(cancelled.cancel(), (True, False))
            with self.assertRaises(ZeroDivisionError):
                p.submit_low(divmod, 1, 0).result(timeout=30)
        finally:
            p.shutdown()

    def test_lazy_low_jobs_take_their_arguments_at_handover(self):
        """submit_low_lazy: the arguments are made when the job goes to the processes, so what was added while it
        waited goes with it (the trees geotiles that join one job, Server._veg_geo_job)."""
        import time
        p = pools.RespawningPool("test", 1, mp_context=SPAWN)
        try:
            p.submit(time.sleep, 0.5).result(timeout=30)        # (the process started)
            busy = [p.submit(time.sleep, 0.5) for _ in range(2)]
            first = p.submit_low_lazy(sorted, lambda: (([3, 1],), {}))          # (handed over beside them)
            waiting = [2]
            later = p.submit_low_lazy(sorted, lambda: ((list(waiting),), {}))
            time.sleep(0.2)
            self.assertFalse(later.running())
            waiting += [9, 0]
            self.assertEqual(first.result(timeout=30), [1, 3])
            self.assertEqual(later.result(timeout=30), [0, 2, 9])
            self.assertTrue(all(f.done() for f in busy))

            def broken():
                raise KeyError("x")
            with self.assertRaises(KeyError):
                p.submit_low_lazy(sorted, broken).result(timeout=30)
            self.assertEqual(p.submit_low(abs, -4).result(timeout=30), 4)     # (the loop still runs)
        finally:
            p.shutdown()

    def test_jobs_in_flight_are_counted(self):
        """inflight (Server.live_busy: the saved areas' downloads wait for it): up at a submit, down when the job ends,
        fails or is cancelled, also when a pool made again cancels its futures (its own lock: no deadlock)."""
        import time
        p = pools.RespawningPool("test", 1, mp_context=SPAWN)
        try:
            self.assertEqual(p.inflight, 0)
            f = p.submit(time.sleep, 0.3)
            self.assertEqual(p.inflight, 1)
            f.result(timeout=30)
            time.sleep(0.05)
            self.assertEqual(p.inflight, 0)
            with self.assertRaises(ZeroDivisionError):
                p.submit(divmod, 1, 0).result(timeout=30)
            time.sleep(0.05)
            self.assertEqual(p.inflight, 0)
            hung = p.submit(time.sleep, 3600)
            queued = p.submit(abs, -1)
            time.sleep(0.3)
            with self.assertLogs("earthd.pools", "WARNING"):
                self.assertTrue(p.recycle("test"))
            for fut in (hung, queued):
                try:
                    fut.result(timeout=30)
                except Exception:
                    pass
            time.sleep(0.05)
            self.assertEqual(p.inflight, 0)
        finally:
            p.shutdown()


if __name__ == "__main__":
    unittest.main()


class HungWatchdogTest(unittest.TestCase):
    """GitHub #1058: every road process stopped finishing jobs; their waiters (the AI road graph, terrain patches) had
    no timeout, so nothing restarted the processes. A job running hung_s or more recycles the pool."""

    def test_a_hung_job_without_a_timeout_ends_in_a_restart(self):
        import time
        with unittest.mock.patch.object(pools.RespawningPool, "WATCH_S", 0.2):
            p = pools.RespawningPool("test", 1, mp_context=SPAWN, hung_s=1.5)
        try:
            self.assertEqual(p.submit(abs, -3).result(timeout=60), 3)      # (the process started)
            fut = p.submit(time.sleep, 3600)
            with self.assertLogs("earthd.pools", "WARNING"):
                with self.assertRaises(BrokenProcessPool):
                    fut.result(timeout=60)                              # (the bare wait ends: the pool recycled)
            self.assertEqual(p.respawns, 1)
            self.assertEqual(p.submit(abs, -4).result(timeout=60), 4)   # (and works again)
        finally:
            p.shutdown()

    def test_a_queued_job_is_not_counted_and_a_short_one_is_left_alone(self):
        p = pools.RespawningPool("test", 1, mp_context=SPAWN, hung_s=3600.0)
        try:
            self.assertEqual(p.submit(abs, -3).result(timeout=60), 3)
            self.assertFalse(p._watch_step(0.0))
            self.assertEqual(p.respawns, 0)
            self.assertEqual(p._live, {})                                # (done jobs leave the watch list)
        finally:
            p.shutdown()


class HungDumpTest(unittest.TestCase):
    def test_a_long_job_writes_its_stacks(self):
        import subprocess, textwrap
        code = textwrap.dedent("""
            import sys, os
            sys.path.insert(0, %r)
            os.environ["BNGE_HUNG_DUMP_S"] = "0.5"
            import pools, time
            print(pools._call(time.sleep, (1.2,), {}))
        """ % os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Thread", r.stderr)               # (faulthandler's dump of the running threads)
        self.assertIn("None", r.stdout)                 # (and the job ran to its end: not stopped)
