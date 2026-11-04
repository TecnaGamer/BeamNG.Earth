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


if __name__ == "__main__":
    unittest.main()
