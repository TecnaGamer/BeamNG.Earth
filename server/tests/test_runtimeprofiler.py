"""Opt-in profiler: real threads/spawned processes, queueing, failure and bounds."""
import os
import asyncio
from pathlib import Path
import queue
import sys
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtimeprofiler as profiler
import pools


@profiler.step("test stage")
def work(value, delay=0.02):
    time.sleep(delay)
    if value == "fail":
        raise ValueError("original failure")
    return value


def threaded_work():
    with profiler.ThreadPoolExecutor(2, thread_name_prefix="child-fetch") as pool:
        return [f.result() for f in [pool.submit(work, 1), pool.submit(work, 2)]]


class ProfilerTest(unittest.TestCase):
    def setUp(self):
        self.c = profiler.Collector()
        self.patch = patch.object(profiler, "collector", self.c)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.c.close()

    def finished(self, n):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            state = self.c.snapshot()
            if len(state.get("recent", [])) >= n:
                return state
            time.sleep(0.01)
        self.fail("telemetry did not finish")

    def test_disabled_does_not_start_manager_or_wrap_jobs(self):
        with profiler.ThreadPoolExecutor(1) as pool:
            self.assertEqual(pool.submit(work, 3).result(), 3)
        self.assertIsNone(self.c.manager)
        self.assertEqual(self.c.snapshot(), {"enabled": False})

    def test_live_stage_and_queue_wait(self):
        self.c.set_enabled(True)
        with profiler.ThreadPoolExecutor(1, thread_name_prefix="test lane") as pool:
            first = pool.submit(work, 1, 0.3)
            second = pool.submit(work, 2)
            until = time.monotonic() + 5
            while time.monotonic() < until:
                live = self.c.snapshot()["active"]
                if any(j["stage"] == "test stage" for j in live):
                    break
                time.sleep(0.005)
            else:
                self.fail("running stage not visible")
            self.assertTrue(any(j["stage"] == "queued" for j in live))
            self.assertEqual(first.result(), 1)
            self.assertEqual(second.result(), 2)
        state = self.finished(2)
        self.assertEqual(state["active"], [])
        first, second = state["recent"]
        self.assertGreater(second["queueMs"], 100)
        self.assertGreater(first["runMs"], 200)
        self.assertLess(first["cpuMs"], first["runMs"])
        self.assertEqual(first["steps"][0]["name"], "test stage")
        self.assertEqual(first["steps"][0]["count"], 1)
        self.assertGreaterEqual(first["startOffsetMs"], first["submittedOffsetMs"])
        self.assertGreaterEqual(first["endOffsetMs"], first["startOffsetMs"])

    def test_spawn_result_and_original_exception(self):
        self.c.set_enabled(True)
        p = pools.RespawningPool("process lane", 1, mp_context=profiler.multiprocessing.get_context("spawn"))
        try:
            self.assertEqual(p.submit(work, {"result": [1, 2]}).result(timeout=20), {"result": [1, 2]})
            with self.assertRaisesRegex(ValueError, "original failure"):
                p.submit(work, "fail").result(timeout=20)
        finally:
            p.shutdown()
        state = self.finished(2)
        self.assertEqual([j["outcome"] for j in state["recent"]], ["ok", "error"])
        self.assertTrue(all(j["worker"].split('/')[0] != str(os.getpid()) for j in state["recent"]))
        self.assertTrue(all(j["steps"] for j in state["recent"]))

    def test_cancelled_job_and_lost_telemetry_clear_active(self):
        # Saturated telemetry cannot affect results or leave jobs busy forever.
        self.c.channel = queue.Queue(1)
        self.c.enabled = True
        with profiler.ThreadPoolExecutor(1) as pool:
            first = pool.submit(work, 4, 0.1)
            second = pool.submit(work, 5)
            self.assertTrue(second.cancel())
            self.assertEqual(first.result(), 4)
        with self.c.lock:
            for row in self.c.active.values():
                row["doneAt"] -= 2
        state = self.c.snapshot()
        self.assertEqual(len(state["recent"]), 2)
        self.assertEqual(state["active"], [])
        self.assertTrue(all(j["incomplete"] for j in state["recent"]))
        self.assertIn("cancelled", [j["outcome"] for j in state["recent"]])

    def test_disable_reenable_discards_old_session(self):
        self.c.set_enabled(True)
        with profiler.ThreadPoolExecutor(1) as pool:
            pending = pool.submit(work, 9, 0.2)
            old = self.c.session
            self.c.set_enabled(False)
            self.c.set_enabled(True)
            self.assertNotEqual(old, self.c.session)
            self.assertEqual(pending.result(), 9)
        state = self.c.snapshot()
        self.assertEqual(state["active"], [])
        self.assertEqual(state["recent"], [])

    def test_history_and_active_are_bounded(self):
        self.c.channel = queue.Queue(2048)
        self.c.enabled = True
        with patch.object(profiler, "MAX_ACTIVE", 1), profiler.ThreadPoolExecutor(1) as pool:
            first = pool.submit(work, 1, 0.1)
            second = pool.submit(work, 2)
            self.assertEqual(first.result(), 1)
            self.assertEqual(second.result(), 2)
        self.assertEqual(self.c.snapshot()["skipped"], 1)
        for _ in range(profiler.MAX_RECENT + 10):
            with profiler.ThreadPoolExecutor(1) as pool:
                pool.submit(abs, -1).result()
            self.c.snapshot()
        self.assertEqual(len(self.c.snapshot()["recent"]), profiler.MAX_RECENT)

    def test_submission_failure_leaves_no_active_job(self):
        self.c.set_enabled(True)
        pool = profiler.ThreadPoolExecutor(1)
        pool.shutdown()
        with self.assertRaises(RuntimeError):
            pool.submit(work, 1)
        self.assertEqual(self.c.snapshot()["active"], [])

    def test_crashed_process_does_not_stay_busy(self):
        self.c.set_enabled(True)
        p = pools.RespawningPool("crash lane", 1, mp_context=profiler.multiprocessing.get_context("spawn"))
        try:
            with self.assertRaises(pools.BrokenProcessPool):
                p.submit(os._exit, 1).result(timeout=20)
        finally:
            p.shutdown()
        state = self.finished(1)
        self.assertEqual(state["active"], [])
        self.assertEqual(state["recent"][0]["outcome"], "error")
        self.assertTrue(state["recent"][0]["incomplete"])

    def test_threads_inside_spawned_worker_are_visible(self):
        self.c.set_enabled(True)
        p = pools.RespawningPool("process lane", 1, mp_context=profiler.multiprocessing.get_context("spawn"))
        try:
            self.assertEqual(p.submit(threaded_work).result(timeout=20), [1, 2])
        finally:
            p.shutdown()
        state = self.finished(3)
        self.assertEqual(state["active"], [])
        children = [j for j in state["recent"] if j["pool"] == "child-fetch"]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(j["worker"].split('/')[1].startswith('child-fetch') for j in children))
        self.assertTrue(all(j["steps"][0]["name"] == 'test stage' for j in children))


class PublisherTest(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_publish_expire_and_backpressure(self):
        class FakeCollector:
            enabled = False
            def set_enabled(self, value): self.enabled = value
            def configure(self, value, mode='full'): self.enabled = value
            def io_snapshot(self, server=None): return {}
            def snapshot(self): return {"enabled": self.enabled, "active": [], "recent": []}
        class Client:
            profile_until = time.monotonic() + 60
            size = 0
            def __init__(self):
                self.sent = []
                self.writer = SimpleNamespace(transport=SimpleNamespace(get_write_buffer_size=lambda: self.size))
            def send(self, msg): self.sent.append(msg)
        c, client = FakeCollector(), Client()
        with patch.object(profiler, "collector", c), patch.object(profiler, "PUBLISH_S", 0.01):
            task = asyncio.create_task(profiler.stream(SimpleNamespace(connections={client})))
            try:
                for _ in range(100):
                    if client.sent: break
                    await asyncio.sleep(0.01)
                self.assertTrue(c.enabled)
                self.assertEqual(client.sent[0]["type"], "runtime_profiler")
                client.size = 1024 * 1024
                await asyncio.sleep(0.03)
                count = len(client.sent)
                await asyncio.sleep(0.03)
                self.assertEqual(len(client.sent), count)
                client.profile_until = 0
                for _ in range(100):
                    if not c.enabled: break
                    await asyncio.sleep(0.01)
                self.assertFalse(c.enabled)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertFalse(c.enabled)


if __name__ == "__main__":
    unittest.main()
