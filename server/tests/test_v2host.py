"""Architecture v2 phase V1 (BNE_V2_HOST=on, v2host.py): the Rust deadline scheduler (earthfast.Sched, host/sched.rs).
Class and deadline order, cancel by place (a teleport drops the old place's queued jobs, stops its running ones and
preempts what still runs in a worker process: pools.SlotPool), no slot held while a job waits for another, the CPU
target following the game's frame time. The 2026-11-22 teleport (Bonneville -> Fells Point: old-place blocks kept the
heightmap workers 137-262 s, the new place's first graded block waited 61 s for one) as a test: TeleportWhileBusyTest."""
import asyncio
import concurrent.futures
import multiprocessing
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fast  # noqa: E402
import pools  # noqa: E402
import v2host  # noqa: E402
import xproc  # noqa: E402

SPAWN = multiprocessing.get_context("spawn")
HAVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= v2host.SCHED_VERSION


def slow_block(aid, seconds):
    """(a worker process) a block of anchor aid whose stage never looks at a cancel check (240 s of grading)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        sum(range(10000))
    return ("slow", aid)


def quick_block(aid):
    """(a worker process) a block of anchor aid that takes no time."""
    return ("quick", aid, os.getpid())


def make(lo=2, hi=4):
    return fast._ef.Sched("test-job", pools.Abandoned, lo, hi)


@unittest.skipUnless(HAVE, "earthfast with the v2 scheduler")
class OrderTest(unittest.TestCase):
    def test_class_then_deadline_and_the_urgent_slot(self):
        s = make()
        lane = s.lane("a", 1)
        gate, order = threading.Event(), []
        first = s.submit(lane, 2, None, None, None, gate.wait, 10)
        while s.running() < 1:
            time.sleep(0.005)
        futs = [s.submit(lane, 3, None, None, None, order.append, "p3"),
                s.submit(lane, 1, 9.0, None, None, order.append, "p1-late"),
                s.submit(lane, 1, 1.0, None, None, order.append, "p1-soon")]
        urgent = s.submit(lane, 0, None, None, None, order.append, "p0")
        urgent.result(timeout=5)
        self.assertEqual(order, ["p0"], "P0 takes the urgent slot beside the busy one")
        gate.set()
        first.result(timeout=5)
        for f in futs:
            f.result(timeout=5)
        self.assertEqual(order, ["p0", "p1-soon", "p1-late", "p3"])
        s.shutdown()


@unittest.skipUnless(HAVE, "earthfast with the v2 scheduler")
class CancelPlaceTest(unittest.TestCase):
    def test_queued_dropped_running_flagged_new_refused(self):
        s = make()
        lane = s.lane("a", 1)
        gate, seen = threading.Event(), {}

        def running():
            gate.wait(10)
            seen["flag"] = fast._ef.sched_cancelled()
            try:
                xproc.check_cancel()
                seen["check"] = "went on"
            except pools.Abandoned:
                seen["check"] = "gave up"

        xproc.extra_cancel(fast._ef.sched_cancelled)
        try:
            run = s.submit(lane, 1, None, 7, None, running)
            while s.running() < 1:
                time.sleep(0.005)
            queued = s.submit(lane, 1, None, 7, None, seen.setdefault, "queued", "ran")
            other = s.submit(lane, 2, None, 8, None, seen.setdefault, "other", "ran")
            self.assertEqual(s.cancel_place(7), (1, 1))
            with self.assertRaises(pools.Abandoned):
                queued.result(timeout=1)
            with self.assertRaises(pools.Abandoned):
                s.submit(lane, 1, None, 7, None, seen.setdefault, "late", "ran").result(timeout=1)
            gate.set()
            run.result(timeout=5)
            other.result(timeout=5)
            self.assertEqual(seen, {"flag": True, "check": "gave up", "other": "ran"})
            s.uncancel_place(7)
            self.assertEqual(s.submit(lane, 1, None, 7, None, lambda: "again").result(timeout=5), "again")
        finally:
            xproc.extra_cancel(None)
            s.shutdown()

    def test_the_place_comes_from_the_anchor_argument(self):
        s = make()
        ex = s.executor(s.lane("hm", 1, False, False), 0, None, 0)
        gate = threading.Event()
        anchor = SimpleNamespace(id=3, frame=None)
        busy = ex.submit(lambda a: gate.wait(10), anchor)
        while s.running() < 1:
            time.sleep(0.005)
        q = ex.submit(lambda a, x: x, anchor, 1)
        self.assertEqual(s.cancel_place(3), (1, 1))
        with self.assertRaises(pools.Abandoned):
            q.result(timeout=1)
        gate.set()
        busy.result(timeout=5)
        s.shutdown()


@unittest.skipUnless(HAVE, "earthfast with the v2 scheduler")
class NoSlotWhileWaitingTest(unittest.TestCase):
    def test_a_job_waiting_for_another_in_its_lane(self):
        s = make()
        lane = s.lane("a", 1, False, False)     # (one slot, no urgent one)

        def outer():
            inner = s.submit(lane, 3, None, None, None, lambda: "inner")
            return "outer+" + s.wait(inner, 5)

        self.assertEqual(s.submit(lane, 1, None, None, None, outer).result(timeout=10), "outer+inner")
        s.shutdown()

    def test_result_running_gives_its_slot_back_and_stops_for_a_place_left(self):
        old = v2host._sched
        s = v2host._sched = make()
        try:
            lane = s.lane("a", 1, False, False)
            never = concurrent.futures.Future()
            never.set_running_or_notify_cancel()          # (a worker process's job that goes on and on)

            def waiter():
                return pools.result_running(never, None)

            w = s.submit(lane, 1, None, 5, None, waiter)
            while s.running() < 1:
                time.sleep(0.005)
            t0 = time.monotonic()
            self.assertEqual(s.submit(lane, 1, None, 6, None, lambda: "free").result(timeout=5), "free",
                             "the waiter holds no slot")
            s.cancel_place(5)
            with self.assertRaises(pools.Abandoned):
                w.result(timeout=5)
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            v2host._sched = old
            s.shutdown()


@unittest.skipUnless(HAVE, "earthfast with the v2 scheduler")
class ExecutorTest(unittest.TestCase):
    def test_run_in_executor(self):
        s = make()
        ex = s.executor(s.lane("x", 2), 2, None, None)

        async def main():
            loop = asyncio.get_running_loop()
            return await asyncio.gather(*[loop.run_in_executor(ex, pow, 2, n) for n in range(5)])

        self.assertEqual(asyncio.run(main()), [1, 2, 4, 8, 16])
        end = time.monotonic() + 10     # (a worker resolves its future, then counts the task: wait for the count)
        while s.stats(False)["classes"]["P2"]["done"] < 5 and time.monotonic() < end:
            time.sleep(0.01)
        st = s.stats(True)
        self.assertEqual(st["classes"]["P2"]["done"], 5)
        self.assertIsNotNone(st["classes"]["P2"]["lateP95"])
        self.assertEqual(s.stats(True)["classes"]["P2"]["done"], 0, "reset")
        s.shutdown()

    def test_classify(self):
        import blockworker
        self.assertEqual(fast._ef.sched_classify(blockworker.roads_geo, (4, 0.0), 2), (1, 4))
        self.assertEqual(fast._ef.sched_classify(blockworker.build, (4, 0.0), 0), (0, 4))
        self.assertEqual(fast._ef.sched_classify(blockworker.far_buildings_tile, (9,), 1)[0], 3)
        self.assertEqual(fast._ef.sched_classify(pow, (2, 3), 2), (2, None))


@unittest.skipUnless(HAVE, "earthfast with the v2 scheduler")
class AdaptTest(unittest.TestCase):
    def test_frame_time_lowers_the_target_and_holds_raise_it(self):
        s = make(2, 8)
        self.assertEqual(s.target(), 8)
        for _ in range(10):
            s.note_frame(16.7, 20.0, False)
        s.note_frame(30.0, 40.0, False)
        n = s.note_frame(30.0, 40.0, False)
        self.assertLess(n, 8)
        self.assertEqual(s.note_frame(30.0, 40.0, True), 8, "the player waits for us")
        s.set_target(3)
        self.assertEqual(s.note_frame(16.7, 20.0, True), 3, "a fixed target")
        s.shutdown()


@unittest.skipUnless(HAVE, "earthfast with the v2 scheduler")
class TeleportWhileBusyTest(unittest.TestCase):
    """A block process busy with a block of the place left (a stage without a cancel check) is preempted: the new
    place's block starts at once, not after it (2026-11-22: 61 s for the first graded block at Fells Point)."""

    def test_the_new_place_waits_for_no_old_block(self):
        s = make()
        old = v2host._sched
        v2host._sched = s
        pool = pools.SlotPool(s, "block", 1, cls=0, mp_context=SPAWN)
        try:
            self.assertEqual(pool.submit(quick_block, 0).result(timeout=120)[0], "quick")     # (started, warm)
            busy = pool.submit(slow_block, 1, 120.0)
            while not busy.running():
                time.sleep(0.01)
            time.sleep(0.3)
            t0 = time.monotonic()
            s.cancel_place(1)                           # (the teleport)
            new = pool.submit(quick_block, 2)
            got = new.result(timeout=60)
            took = time.monotonic() - t0
            with self.assertRaises(pools.Abandoned):
                busy.result(timeout=10)
            self.assertEqual(got[:2], ("quick", 2))
            self.assertEqual(pool.preempts, 1)
            # (the grace, then a fresh process's start: seconds, not the 120 s left of the old block)
            self.assertLess(took, pools.PREEMPT_GRACE_S + 30.0)
            print(f"\nteleport while busy: the new place's block after {took:.2f} s (grace {pools.PREEMPT_GRACE_S} s)")
        finally:
            v2host._sched = old
            pool.shutdown(wait=False, cancel_futures=True)
            s.shutdown()

    def test_a_job_that_gives_up_by_itself_is_not_preempted(self):
        s = make()
        old = v2host._sched
        v2host._sched = s
        pool = pools.SlotPool(s, "block", 1, cls=0, mp_context=SPAWN)
        try:
            busy = pool.submit(slow_block, 1, 0.5)      # (ends within the grace)
            while not busy.running():
                time.sleep(0.01)
            s.cancel_place(1)
            self.assertEqual(busy.result(timeout=120), ("slow", 1))
            self.assertEqual(pool.preempts, 0)
        finally:
            v2host._sched = old
            pool.shutdown(wait=False, cancel_futures=True)
            s.shutdown()


@unittest.skipUnless(HAVE, "earthfast with the v2 scheduler")
class WarmBoardTest(unittest.TestCase):
    """The block lanes' plans-ahead list as a file of the latest list: no lock shared with the processes (a
    multiprocessing queue's reader lock stays held for good by a process killed in its get())."""

    def test_the_latest_list_and_nothing_twice(self):
        import tempfile
        import mapcells
        old = v2host._sched
        v2host._sched = make()
        v2host._warm_io = None
        try:
            with tempfile.TemporaryDirectory() as d:
                b = v2host.WarmBoard(os.path.join(d, "warm-0.json"))
                self.assertEqual(v2host.read_warm(b.path, None), (None, None), "nothing yet")
                b.put_nowait((3, 1.5, 2.5, [mapcells.CellKey(14, 1, 2)], False))
                b.put_nowait((3, 1.5, 2.5, [mapcells.CellKey(14, 5, 6), mapcells.CellKey(14, 5, 7)], True))
                end = time.monotonic() + 5
                got, seen = None, None
                while got is None and time.monotonic() < end:
                    got, seen = v2host.read_warm(b.path, None)
                    time.sleep(0.01)
                while v2host._sched.running() or v2host._sched.pending():
                    time.sleep(0.01)
                got, seen = v2host.read_warm(b.path, None)
                self.assertEqual(got, (3, 1.5, 2.5, [(14, 5, 6), (14, 5, 7)], [True]), "the latest")
                self.assertIsInstance(got[3][0], mapcells.CellKey)
                self.assertEqual(v2host.read_warm(b.path, seen), (None, seen), "unchanged: nothing")
                self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [])
        finally:
            v2host._sched.shutdown()
            v2host._sched, v2host._warm_io = old, None


class OffTest(unittest.TestCase):
    """The switch off: nothing of the scheduler is made or asked."""

    def test_off_by_default(self):
        if os.environ.get("BNE_V2_HOST"):
            self.skipTest("BNE_V2_HOST set")
        self.assertFalse(v2host.ON)
        self.assertIsNone(v2host.sched())
        self.assertIsNone(v2host.summary())
        self.assertFalse(v2host.cancelled())
        with v2host.blocked():
            pass


if __name__ == "__main__":
    unittest.main()
