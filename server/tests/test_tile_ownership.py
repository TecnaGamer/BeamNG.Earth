"""A cancelled tile waiter must not release a running worker's pooled shape.

Real executor threads and temporary shape files, with barriers instead of timing
assumptions. No engine, geographic provider, or game process is involved.
"""
import asyncio
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd
import matslots


class TileOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bnge-tile-ownership-")
        self.s = earthd.Server(self.tmp.name, self.tmp.name)
        self.s.schedule_prune = mock.Mock()
        self.s.queue_delete = mock.Mock()
        self.anchor = SimpleNamespace(id=1)
        self.s.anchors[1] = self.anchor
        self.tile = SimpleNamespace(key="0_0_0")
        self.ck = (1, self.tile.key)
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.calls = 0
        self.name = None
        loop = asyncio.get_running_loop()

        def build(anchor, tile, prev):
            self.calls += 1
            self.name = self.s.matslots.take(self.ck)
            loop.call_soon_threadsafe(self.started.set)
            if not self.release.wait(5):
                raise TimeoutError("tile fixture was not released")
            path = Path(self.s.out_dir, self.s.tile_name(*self.ck))
            path.write_bytes(b"OLD TILE")
            shape = matslots.MatSlots.file(self.name)
            matslots.place(str(path), str(Path(self.s.out_dir, shape)))
            return earthd.TileInfo([0, 0, 0], 0, None, 0, None, 8, 0,
                                  self.name, "old.dds", shape=shape)

        self.s.make_tile = build

    async def asyncTearDown(self):
        # Even an assertion failure releases the bounded fixture before joining.
        self.release.set()
        producers = list(self.s.tile_inflight.values())
        if producers:
            await asyncio.wait_for(asyncio.gather(*producers, return_exceptions=True), 3)
        self.s.tile_pool.shutdown(wait=True)
        self.s.hm_pool.shutdown(wait=True)
        self.tmp.cleanup()

    async def start(self):
        waiter = self.s.tile(self.anchor, self.tile)
        await asyncio.wait_for(self.started.wait(), 2)
        return waiter

    async def test_cancelled_waiter_cannot_reassign_running_workers_shape(self):
        waiter = await self.start()
        producer = self.s.tile_inflight[self.ck]
        waiter.cancel()  # the same operation as a connection's rebase/disconnect
        await asyncio.sleep(0)  # deliver cancellation callbacks
        self.assertFalse(producer.done())
        self.assertIs(self.s.tile_inflight[self.ck], producer)
        self.s.reclaim_names()
        self.assertTrue(self.s.matslots.owns(self.ck, self.name))
        new_ck = (2, "0_0_0")
        self.s.names_in_use = lambda: {new_ck}  # the replacement is wanted by a connection
        new_name = self.s.matslots.take(new_ck)
        self.assertNotEqual(new_name, self.name)
        new_path = Path(self.s.out_dir, matslots.MatSlots.file(new_name))
        new_path.write_bytes(b"NEW TILE")
        self.release.set()
        await asyncio.wait_for(asyncio.shield(producer), 2)
        self.assertTrue(self.s.matslots.owns(new_ck, new_name))
        self.assertEqual(new_path.read_bytes(), b"NEW TILE")

    async def test_cancelling_one_consumer_does_not_cancel_another(self):
        first = await self.start()
        second = self.s.tile(self.anchor, self.tile)
        self.assertIsNot(first, second)
        first.cancel()
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        self.release.set()
        info = await asyncio.wait_for(second, 2)
        self.assertEqual(self.calls, 1)
        self.assertEqual(info.material, self.name)
        self.assertNotIn(self.ck, self.s.tile_inflight)

    async def test_prune_defers_slot_and_file_release_until_worker_finishes(self):
        waiter = await self.start()
        producer = self.s.tile_inflight[self.ck]
        self.s.prune(2)
        self.assertIn(1, self.s.anchors)
        self.assertFalse(producer.done())
        self.assertTrue(self.s.matslots.owns(self.ck, self.name))
        self.s.schedule_prune.assert_called_once_with(2)
        self.s.queue_delete.assert_not_called()
        self.release.set()
        await asyncio.wait_for(waiter, 2)
        self.s.prune(2)
        self.assertNotIn(1, self.s.anchors)
        self.assertNotIn(self.ck, self.s.tile_inflight)
        self.assertFalse(self.s.matslots.owns(self.ck, self.name))
        self.s.queue_delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
