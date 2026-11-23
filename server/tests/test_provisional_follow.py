"""Connection.provisional_follow: a block built already (the prefetch) is answered at once, even with the
plan-check pool busy (GitHub #725: blocks built 15-34 s before their request answered 2.6-8.8 s after it,
each an edge hold); one not built yet still waits for the check."""
import asyncio
import os
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import earthd  # noqa: E402


class ProvisionalFollowTest(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.pool = ThreadPoolExecutor(2)
        for _ in range(2):          # both threads busy (provisional builds of a first visit)
            self.pool.submit(self.release.wait, 10)
        self.checked = []
        s = types.SimpleNamespace(prov_pool=self.pool, grading=True, prov_info={}, hm_proc={},
                                  block_planned=lambda a, cx, cy: self.checked.append((cx, cy)) or True)
        self.conn = types.SimpleNamespace(s=s, active=1)
        self.anchor = types.SimpleNamespace(id=1)

    def tearDown(self):
        self.release.set()
        self.pool.shutdown(wait=True)

    def run_follow(self, job_coro):
        async def go():
            job = asyncio.ensure_future(job_coro())
            t0 = time.perf_counter()
            r = await earthd.Connection.provisional_follow(self.conn, self.anchor, 0.0, 0.0, job)
            return r, time.perf_counter() - t0
        return asyncio.run(go())

    def test_built_block_goes_at_once(self):
        async def built():
            return {"ter": "x.ter"}
        if earthd.PROVISIONAL_FOLLOW_S <= 0 or not earthd.PROVISIONAL_FIRST:
            self.skipTest("provisional follow blocks off in this environment")
        r, dt = self.run_follow(built)
        self.assertIsNone(r)
        self.assertLess(dt, 0.5)

    def test_unbuilt_block_waits_for_the_check(self):
        if earthd.PROVISIONAL_FOLLOW_S <= 0 or not earthd.PROVISIONAL_FIRST:
            self.skipTest("provisional follow blocks off in this environment")

        async def slow():
            await asyncio.sleep(0.6)
            return {"ter": "x.ter"}
        threading.Timer(0.3, self.release.set).start()     # the pool frees up before the block is built
        r, dt = self.run_follow(slow)
        self.assertIsNone(r)                # (built within PROVISIONAL_FOLLOW_S of the check: no stand-in)
        self.assertEqual(self.checked, [(0.0, 0.0)])

    def test_graded_winner_discards_only_the_losing_workers_files(self):
        if earthd.PROVISIONAL_FOLLOW_S <= 0 or not earthd.PROVISIONAL_FIRST:
            self.skipTest("provisional follow blocks off in this environment")
        self.release.set()
        s = self.conn.s
        s.block_planned = lambda *args: False
        s.out_dir = "out"
        provisional = {"heightmap": "p.png", "ter": "p.ter", "layers": ["p_m0.png"], "base": "photo.jpg"}

        async def go():
            loop = asyncio.get_running_loop()
            started, removed = asyncio.Event(), loop.create_future()
            release = threading.Event()

            def worker(*args):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(10):
                    raise TimeoutError("the graded reply was held up")
                return provisional

            s.make_provisional_follow = worker
            s.remove_off_loop = lambda paths: removed.set_result(list(paths))
            job = loop.create_future()
            follow = asyncio.create_task(earthd.Connection.provisional_follow(self.conn, self.anchor, 0., 0., job))
            try:
                await asyncio.wait_for(started.wait(), 10)
                job.set_result({"heightmap": "g.png", "ter": "g.ter"})
                self.assertIsNone(await asyncio.wait_for(follow, 10))
                self.assertFalse(removed.done())
            finally:
                release.set()
            names = [os.path.basename(p) for p in await asyncio.wait_for(removed, 10)]
            self.assertCountEqual(names, ["p.png", "p.ter", "p_m0.png"])
            self.assertFalse(s.prov_info)
        asyncio.run(go())

    def test_stuck_check_starts_the_stand_in(self):
        """A plan check stuck past PROVISIONAL_FOLLOW_S (the pool busy for good) counts as planned: the stand-in is
        made after the follow wait, not after the check."""
        if earthd.PROVISIONAL_FOLLOW_S <= 0 or not earthd.PROVISIONAL_FIRST:
            self.skipTest("provisional follow blocks off in this environment")
        made = []
        stand_pool = ThreadPoolExecutor(1)
        self.conn.s.make_provisional_follow = lambda a, cx, cy: made.append(1) or {"ter": "prov.ter"}
        self.conn.spawn = lambda coro: coro.close()

        async def refine(*a):
            pass
        self.conn.refine_follow = refine

        async def never():
            await asyncio.sleep(30)

        async def go():
            loop = asyncio.get_running_loop()
            orig = loop.run_in_executor
            # (the check on the stuck pool, the stand-in on a free thread)
            loop.run_in_executor = lambda pool, fn, *a: orig(
                self.pool if fn is self.conn.s.block_planned else stand_pool, fn, *a)
            try:
                job = asyncio.ensure_future(never())
                t0 = time.perf_counter()
                r = await earthd.Connection.provisional_follow(self.conn, self.anchor, 0.0, 0.0, job)
                job.cancel()
                return r, time.perf_counter() - t0
            finally:
                loop.run_in_executor = orig
        try:
            r, dt = asyncio.run(go())
        finally:
            stand_pool.shutdown(wait=False)
        self.assertEqual(r, {"ter": "prov.ter"})
        self.assertEqual(made, [1])
        self.assertLess(dt, earthd.PROVISIONAL_FOLLOW_S + 1.5)


if __name__ == "__main__":
    unittest.main()
