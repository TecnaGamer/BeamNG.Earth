"""The trees trackers' pools start their threads off the event loop (runtimeprofiler.ThreadPoolExecutor.prestart): a pass
on the loop never waits for a new thread to start (a teleport's first pass of 64 submits stalled the loop 2.5 s, 2026-11-16)."""
import asyncio
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import runtimeprofiler  # noqa: E402
import vegetation  # noqa: E402


class PrestartTest(unittest.TestCase):
    def test_prestart_starts_every_worker_and_leaves_them_idle(self):
        pool = runtimeprofiler.ThreadPoolExecutor(8, thread_name_prefix="t")
        self.addCleanup(pool.shutdown)
        pool.prestart()
        self.assertEqual(len(pool._threads), 8)
        before = threading.active_count()
        futs = [pool.submit(lambda: 1) for _ in range(8)]
        self.assertEqual([f.result() for f in futs], [1] * 8)
        self.assertEqual(threading.active_count(), before, "no thread started by a submit")

    def test_the_tracker_waits_for_its_pool_off_the_loop(self):
        async def run():
            loop = asyncio.get_running_loop()
            t = vegetation.Tracker(per_pose=6, kind="veg")
            self.assertFalse(t._pool_ready(loop, "veg-geo"))       # (just made: its threads start in the default executor)
            await t._warm
            self.assertTrue(t._pool_ready(loop, "veg-geo"))
            self.assertEqual(t.pool._pool.threads(), 6)    # (the host's PyJobs workers: runtimeprofiler.TileJobs)
            t.pool.shutdown()
        asyncio.run(run())

    def test_a_stand_in_loop_is_ready_at_once(self):
        t = vegetation.Tracker(per_pose=3, kind="veg")
        self.assertTrue(t._pool_ready(object(), "veg-geo"))
        t.pool.shutdown()


if __name__ == "__main__":
    unittest.main()
