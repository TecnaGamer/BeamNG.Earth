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



class ElasticTest(unittest.TestCase):
    """ElasticPool: the far-tile pool's processes for the 3D map and the loading boost end when they have no work (a
    desktop kept all ten for the session: ~540 MB each, 2026-11-12), the ones of a drive stay."""

    def test_extra_processes_come_with_the_work_and_go_without_it(self):
        import time
        p = pools.ElasticPool("test", 1, 2, idle_s=0.5, mp_context=SPAWN)
        try:
            kept = p.submit(os.getpid).result(timeout=60)
            self.assertEqual(p.submit(os.getpid).result(timeout=60), kept, "one job at a time: the kept process")
            self.assertFalse(p.extra_running())
            self.assertEqual((p.workers, p.started, p.retired), (3, 0, 0))
            with self.assertLogs("earthd.pools", "INFO") as logs:
                busy = p.submit(time.sleep, 1.0)        # the kept process busy: the next job starts another
                other = p.submit(os.getpid)
                self.assertTrue(p.extra_running())
                self.assertEqual(p.inflight, 2)
                self.assertNotEqual(other.result(timeout=60), kept)
                busy.result(timeout=60)
                end = time.monotonic() + 60
                while not any("ended after" in m for m in logs.output) and time.monotonic() < end:
                    time.sleep(0.05)
            self.assertTrue(any("ended after" in m for m in logs.output), logs.output)
            self.assertFalse(p.extra_running(), "no work for idle_s: its extra processes ended")
            self.assertEqual((p.started, p.retired, p.inflight), (1, 1, 0))
            self.assertEqual(p.submit(os.getpid).result(timeout=60), kept, "the kept process stayed")
            busy = p.submit(time.sleep, 1.0)            # ... and the work coming back makes them again
            self.assertNotEqual(p.submit(os.getpid).result(timeout=60), kept)
            self.assertEqual(p.started, 2)
            busy.result(timeout=60)
        finally:
            p.shutdown()

    def test_extra_processes_stay_while_they_work(self):
        import time
        p = pools.ElasticPool("test", 1, 1, idle_s=0.3, mp_context=SPAWN)
        try:
            busy = p.submit(time.sleep, 2.5)
            long = p.submit(time.sleep, 1.5)            # in the extra process, longer than idle_s
            self.assertTrue(p.extra_running())
            time.sleep(1.0)
            self.assertTrue(p.extra_running(), "its job still runs")
            self.assertEqual(p.retired, 0)
            long.result(timeout=60)
            self.assertEqual(p.submit(abs, -2).result(timeout=60), 2)     # (the kept one busy: the extra again)
            self.assertEqual((p.started, p.retired), (1, 0), "used again within idle_s: the same processes")
            busy.result(timeout=60)
        finally:
            p.shutdown()

    def test_no_extra_processes_is_the_plain_pool(self):
        import time
        p = pools.ElasticPool("test", 1, 0, idle_s=0.2, mp_context=SPAWN)
        try:
            busy = p.submit(time.sleep, 0.5)
            self.assertEqual(p.submit(abs, -3).result(timeout=60), 3)     # (waits for the kept process)
            busy.result(timeout=60)
            self.assertFalse(p.extra_running())
            self.assertEqual((p.workers, p.started, p.respawns), (1, 0, 0))
        finally:
            p.shutdown()

    def test_idle_time_off_keeps_them(self):
        import time
        with unittest.mock.patch.dict(os.environ, {"BNGE_POOL_IDLE_S": "0"}):
            p = pools.ElasticPool("test", 1, 1, mp_context=SPAWN)
        try:
            self.assertEqual(p.idle_s, 0.0)
            busy = p.submit(time.sleep, 0.5)
            self.assertEqual(p.submit(abs, -3).result(timeout=60), 3)
            busy.result(timeout=60)
            time.sleep(0.5)
            self.assertTrue(p.extra_running(), "BNGE_POOL_IDLE_S=0: as before, they stay")
        finally:
            p.shutdown()

    def test_a_full_pool_queues_where_it_is_shorter(self):
        p = pools.ElasticPool("test", 1, 3, idle_s=0, mp_context=SPAWN)
        try:
            picks = []
            for _ in range(8):          # (eight jobs that never end: the pick alone, nothing handed to a process)
                with p._lock:
                    pool = p._pick()
                    if pool is p.base:
                        p.base.inflight += 1
                    else:
                        p._extra_busy += 1
                    picks.append("kept" if pool is p.base else "extra")
            # 1 kept + 3 extra busy, then the waiting jobs 1 to 3 as the processes are
            self.assertEqual(picks, ["kept", "extra", "extra", "extra", "kept", "extra", "extra", "extra"])
        finally:
            p.base.inflight = 0
            p.shutdown()


if __name__ == "__main__":
    unittest.main()
