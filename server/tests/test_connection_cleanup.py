"""A disconnected readiness probe must not leave the stream's Event.wait task alive.

Exercises actual Connection coroutines with an in-memory reader, no engine or network.
"""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd


class ObservedEvent(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.wait_started = asyncio.Event()

    async def wait(self):
        self.wait_started.set()
        return await super().wait()


class ConnectionCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.baseline = asyncio.all_tasks()
        self.connection = earthd.Connection.__new__(earthd.Connection)
        c = self.connection
        c.s = SimpleNamespace(anchors={}, hm_here=0, tile_workers=1, remote_slots=lambda: 0,
                              connections=set(), slots_let_go=mock.Mock(), drop_claims=mock.Mock())
        c.s.stream_leases = SimpleNamespace(detach=lambda conn: False)
        c.reader = asyncio.StreamReader()
        c.writer = SimpleNamespace(drain=mock.AsyncMock(), close=mock.Mock())
        c.peer = ("in-memory-probe", 0)
        c.active = None
        c.wake = ObservedEvent()
        c.inflight = {}
        c.dirty = False
        c.route_dl_version = 0
        c.slot_holds = {}
        c.roads = c.signs = c.veg = c.bldfar = c.bldsky = c.brfar = c.nav = None
        c.distant_traffic = c.distant_traffic_far = None     # (PR #641's trackers)
        c.credits = SimpleNamespace(close=mock.Mock())
        c.tasks = set()
        c.closing = False

    def pending(self):
        return [task for task in asyncio.all_tasks() - self.baseline
                if task is not asyncio.current_task() and not task.done()]

    async def asyncTearDown(self):
        # Keep even a regression failure isolated from the following test.
        pending = self.pending()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def test_cancelled_stream_awaits_its_event_waiter(self):
        stream = asyncio.create_task(self.connection.stream())
        await asyncio.wait_for(self.connection.wake.wait_started.wait(), 2)
        stream.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stream
        self.assertEqual(self.pending(), [], "stream cancellation leaked an Event.wait task")

    async def test_bare_probe_eof_completes_stream_cleanup(self):
        c = self.connection
        run = asyncio.create_task(c.run())
        await asyncio.wait_for(c.wake.wait_started.wait(), 2)
        c.reader.feed_eof()  # connection closes before sending a protocol hello
        await asyncio.wait_for(run, 2)
        self.assertTrue(c.streamer.done())
        self.assertEqual(c.s.connections, set())
        c.writer.close.assert_called_once_with()
        self.assertEqual(self.pending(), [], "disconnect returned before owned tasks were cleaned up")

    async def test_disconnect_waits_for_owned_coroutine_cleanup(self):
        c = self.connection
        started, cleanup_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def owned():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release.wait()

        task = c.spawn(owned())
        run = asyncio.create_task(c.run())
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(c.wake.wait_started.wait(), 2)
            c.reader.feed_eof()
            await asyncio.wait_for(cleanup_started.wait(), 2)
            await asyncio.gather(c.streamer, return_exceptions=True)
            await asyncio.sleep(0)
            self.assertFalse(run.done(), "connection returned while its coroutine was still cleaning up")
            release.set()
            await asyncio.wait_for(run, 2)
            self.assertTrue(task.done())
            self.assertEqual(self.pending(), [])
        finally:
            release.set()
            await asyncio.gather(run, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
