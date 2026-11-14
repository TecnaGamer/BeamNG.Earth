"""Connection.ground_ahead (the altitude rule's few-ms DEM look) is not queued behind a water plan
(Connection.sea_tiles): a plan takes seconds and holds while a teleport's block is built (pacer.hold), and the
ground ahead waited 2.2 s behind one at 275 km/h on the thread they shared (2026-11-07)."""
import asyncio
import os
import sys
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import earthd  # noqa: E402
import pacer  # noqa: E402


class GroundAheadLaneTest(unittest.TestCase):
    def setUp(self):
        self.ground_pool = ThreadPoolExecutor(1)
        self.sea_pool = ThreadPoolExecutor(1)
        self.sent = []
        anchor = types.SimpleNamespace(frame=object())
        s = types.SimpleNamespace(ground_pool=self.ground_pool, sea_pool=self.sea_pool, anchors={1: anchor},
                                  dem=object(), sea=types.SimpleNamespace(md=None, warm_asked=0),
                                  grader_for=lambda a: None)
        self.conn = types.SimpleNamespace(s=s, ground_busy=False, ground_sent=None, sea_busy=False,
                                          sea_checked=None, sea_sent=None, send=self.sent.append)

    def tearDown(self):
        pacer.foreground_end()
        self.ground_pool.shutdown(wait=True)
        self.sea_pool.shutdown(wait=True)

    def test_ground_answers_while_a_water_plan_holds(self):
        async def go():
            tasks = []
            self.conn.loop = asyncio.get_running_loop()
            self.conn.spawn = lambda coro: tasks.append(asyncio.ensure_future(coro))
            pacer.foreground_begin()            # a teleport's block: the water plan holds (pacer.hold)
            earthd.Connection.sea_tiles(self.conn, 1, 0.0, 0.0)
            await asyncio.sleep(0.05)           # (the plan on its thread, holding)
            t0 = time.perf_counter()
            earthd.Connection.ground_ahead(self.conn, 1, 0.0, 0.0, 76.0, 0.0)
            await asyncio.wait_for(tasks[1], 5.0)
            dt = time.perf_counter() - t0
            pacer.foreground_end()
            await asyncio.wait_for(tasks[0], 5.0)
            return dt
        with mock.patch.object(earthd.groundaheadmod, "highest", lambda *a: 123.4):
            dt = asyncio.run(go())
        self.assertLess(dt, 0.5)
        self.assertIn({"type": "ground_ahead", "anchorId": 1, "z": 123.4}, self.sent)


if __name__ == "__main__":
    unittest.main()
