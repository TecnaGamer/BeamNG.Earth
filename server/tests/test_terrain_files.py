"""The game owns delivered files independently of the server cache and of the socket's lifetime."""
import asyncio
from collections import deque
from concurrent.futures import Future
import itertools
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd
import fast
import streamleases


class TerrainFilesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.s = earthd.Server.__new__(earthd.Server)
        self.s.out_dir = tmp.name
        self.s.vfs_dir = "/temp/beamngEarth/test"
        self.s.layout = SimpleNamespace(block=1024, block_at=lambda cx, cy: (cx, cy))
        self.s.block_files = fast.native(569, "terrain files").BlockFiles()
        self.s.connections = set()
        self.addCleanup(self.finish)

    def finish(self):
        self.s.removes_flush()
        if getattr(self.s, "_remove_pool", None):
            self.s._remove_pool.shutdown()

    def game(self):
        c = earthd.Connection.__new__(earthd.Connection)
        c.s, c.closing, c.terrain_files = self.s, False, True
        c.lease = streamleases.Lease(c, True)
        self.s.connections.add(c)
        return c

    def revision(self, n):
        stem = f"hm_1_0_0_{n}"
        info = dict(ter=stem + ".ter", heightmap=stem + ".png", layers=[f"{stem}_m{i}.png" for i in range(9)],
                    mats=[0], zBase=0, maxHeight=128, q=1/512)
        paths = [Path(self.s.out_dir, p) for p in earthd.block_files(info)]
        for p in paths:
            p.write_bytes(p.name.encode())
        return info, paths

    def test_release_requires_every_delivery_and_cache_retirement(self):
        a, b = self.game(), self.game()
        key = 1, 0, 0
        old, files = self.revision(0)
        new, current = self.revision(1)
        a1, a2, b1 = (c.terrain_msg(key, old)["terrainTicket"] for c in (a, a, b))
        b2 = b.terrain_msg(key, new)["terrainTicket"]
        self.s.retire_terrain(earthd.block_files(old))
        b.dispatch(dict(type="terrain_release", tickets=[a1, a2, b2]))
        a.dispatch(dict(type="terrain_release", tickets=[a1, a1]))
        # Reconnect snapshot drops a delivery lost in flight. The other game's rollback still holds this set.
        self.s.remove_off_loop(self.s.block_files.resume(a.lease.token, []))
        self.s.removes_flush()
        self.assertTrue(all(p.read_bytes() == p.name.encode() for p in files + current))
        b.dispatch(dict(type="terrain_release", tickets=[b1]))
        self.s.removes_flush()
        self.assertFalse(any(p.exists() for p in files))
        self.assertTrue(all(p.exists() for p in current))

    def test_older_game_keeps_same_centre_repaints_until_it_commits_elsewhere(self):
        c = self.game()
        c.terrain_files, c.swap_token, c.pending_block = False, None, SimpleNamespace(cx=128, cy=0)
        key, following = (1, 0, 0), (1, 128, 0)
        old, files = self.revision(0)
        pending, wanted = self.revision(1)
        rebuilt, current = self.revision(2)
        for centre, info in [(key, old), (following, pending), (key, rebuilt), (key, rebuilt)]:
            self.assertNotIn("terrainTicket", c.terrain_msg(centre, info))
        names = [str(p) for p in files]
        self.assertEqual(self.s.block_files.hold_legacy(c.lease.token, key, names),
                         self.s.block_files.hold_legacy(c.lease.token, key, names))
        for info in (old, pending, rebuilt):
            self.s.retire_terrain(earthd.block_files(info))
        c.release_legacy_terrain(key)
        self.s.removes_flush()
        self.assertTrue(all(p.exists() for p in files + wanted + current))
        c.release_legacy_terrain(following)
        self.s.removes_flush()
        self.assertFalse(any(p.exists() for p in files + current))
        self.assertTrue(all(p.exists() for p in wanted))

    def test_superseded_follow_reply_finishing_last_does_not_repin_its_files(self):
        c = self.game()
        c.active, c.swap_token, c.terrain_files = 1, None, False
        self.s.layout.grid = 128
        self.s.layout.block_at = lambda cx, cy: SimpleNamespace(cx=cx, cy=cy)
        c.block = self.s.layout.block_at(0, 0)
        c.pending_block = self.s.layout.block_at(256, 0)
        c.send = lambda msg: None
        current, installed = self.revision(0)
        late, obsolete = self.revision(1)
        next_block, pending = self.revision(2)
        c.terrain_msg((1, 0, 0), current)
        c.commit_terrain(dict(anchorId=1, cx=0, cy=0))
        c._send_terrain_ready(22, (1, 256, 0), next_block)
        c._send_terrain_ready(21, (1, 128, 0), late)
        for rev in (current, late, next_block):
            self.s.retire_terrain(earthd.block_files(rev))
        self.s.removes_flush()
        self.assertFalse(any(p.exists() for p in obsolete))
        self.assertTrue(all(p.exists() for p in installed + pending))

    def test_worker_error_after_publishing_some_files_collects_only_its_revision(self):
        s, key = self.s, (1, 0, 0)
        old, current = self.revision(1)
        self.game().terrain_msg(key, old)
        s.ter_serial = itertools.count(2)
        s.anchor_q = lambda a: 1 / 512
        s.near_visual = "mesh"

        def fail_after_height_and_mask(_job, *args, **kwargs):
            height, ter = args[-2:]
            stem = Path(height).stem
            for name in (height, stem + "_m0.png", ter + ".tmp"):
                Path(s.out_dir, name).write_bytes(b"unfinished")
            future = Future()
            future.set_exception(OSError("partial block"))
            return future

        s.photo_pool = SimpleNamespace(submit=fail_after_height_and_mask)
        anchor = SimpleNamespace(id=1, lat=0, lon=0, hm_zoom=14)
        for _ in range(3):
            with self.assertRaisesRegex(OSError, "partial block"):
                s.make_provisional_follow(anchor, 0, 0)
        s.removes_flush()
        self.assertEqual(set(Path(s.out_dir).iterdir()), set(current))
        self.assertTrue(all(p.read_bytes() == p.name.encode() for p in current))

    def test_first_block_finalization_failure_retires_only_its_revision(self):
        s = self.s
        old, installed = self.revision(1)
        self.game().terrain_msg((1, 0, 0), old)
        s.ter_serial = itertools.count(2)
        s.near_visual, s.dem = "mesh", SimpleNamespace()
        s.anchor_q = lambda a: 1 / 512

        def built(_job, *args, **kw):
            self.assertEqual(kw["ground_at"], (5.0, -7.0))
            height, terrain = args[-2:]
            for name in (height, terrain):
                Path(s.out_dir, name).write_bytes(b"private revision")
            future = Future()
            future.set_result(dict(heightmap=height, ter=terrain))
            return future

        s.photo_pool = SimpleNamespace(submit=built)
        s._finish_heightmap = mock.Mock(side_effect=OSError("finalization failed"))
        anchor = SimpleNamespace(id=1, lat=0, lon=0, hm_zoom=14)
        with mock.patch.object(earthd, "PROVISIONAL_ELSEWHERE", True):
            with self.assertRaisesRegex(OSError, "finalization failed"):
                s.make_provisional(anchor, 0, 0, None, ground_at=(5.0, -7.0))
        s.removes_flush()
        self.assertEqual(set(Path(s.out_dir).iterdir()), set(installed))
        self.assertTrue(all(p.read_bytes() == p.name.encode() for p in installed))

    def test_failed_png_removes_its_own_staging_file(self):
        import numpy as np
        import heightmap
        import staging
        session = Path(self.s.out_dir, "user", "temp", "beamngEarth", "session")
        session.mkdir(parents=True)
        path = str(session / "hm_1_0_0_1.png")
        stage = Path(staging.staging_dir(path))
        with staging.staged_write(path) as active:
            Path(active).write_bytes(b"other writer")

            def fail(_image, tmp, **kwargs):
                Path(tmp).write_bytes(b"partial PNG")
                raise OSError("encode failed")

            with mock.patch.object(heightmap.Image.Image, "save", fail), self.assertRaisesRegex(OSError, "encode failed"):
                heightmap.write_png(path, np.zeros((8, 8)))
            self.assertEqual(list(stage.iterdir()), [Path(active)])
        self.assertEqual(Path(path).read_bytes(), b"other writer")
        self.assertEqual(list(stage.iterdir()), [])

    async def test_repaint_survives_waiter_cancellation_and_keeps_the_visible_revision(self):
        c = self.game()
        key = 1, 0, 0
        old, files = self.revision(0)
        new, current = self.revision(1)
        ticket = c.terrain_msg(key, old)["terrainTicket"]
        self.s.hm = {key: old}
        started, finish = asyncio.Event(), asyncio.Event()

        async def build(*args, **kwargs):
            started.set()
            await finish.wait()
            self.s.hm[key] = new
            return new

        self.s.terrain = build
        producer = asyncio.create_task(self.s.repaint_terrain(None, key))
        waiter = asyncio.shield(producer)
        await started.wait()
        waiter.cancel()
        finish.set()
        await producer
        self.s.removes_flush()
        self.assertTrue(all(p.exists() for p in files + current))
        c.dispatch(dict(type="terrain_release", tickets=[ticket]))
        self.s.removes_flush()
        self.assertFalse(any(p.exists() for p in files))
        self.assertTrue(all(p.exists() for p in current))

    async def test_released_session_folder_survives_a_sharing_violation_then_drains(self):
        s, loop = self.s, asyncio.get_running_loop()
        s.root = s.out_dir
        folder = Path(s.root, "old-session")
        folder.mkdir()
        path = folder / "hm_1_0_0_0.ter"
        path.write_bytes(b"locked")
        s.delete_queue = deque([(str(folder), None)])
        s.claimed_sessions = lambda: set()
        queued = loop.create_future()
        s.queue_delete = queued.set_result
        with mock.patch.object(earthd, "BLOCK_DELETE_RETRY_S", 0), mock.patch.object(earthd.shutil, "rmtree"):
            await asyncio.to_thread(s._delete_pick(loop))
            self.assertEqual(await asyncio.wait_for(queued, 2), [str(folder)])
            self.assertTrue((folder / earthd.SESSION_MARKER).is_file())
        s.delete_queue.append((str(folder), None))
        await asyncio.to_thread(s._delete_pick(loop))
        self.assertFalse(folder.exists())

    async def test_windows_sharing_violation_retries_only_retired_revision(self):
        c = self.game()
        old, files = self.revision(0)
        new, current = self.revision(1)
        ticket = c.terrain_msg((1, 0, 0), old)["terrainTicket"]
        target = str(files[0])
        blocked, removed = asyncio.Event(), asyncio.Event()
        loop, unlink = asyncio.get_running_loop(), os.remove

        def sharing(path):
            if path == target and not blocked.is_set():
                loop.call_soon_threadsafe(blocked.set)
                raise PermissionError(13, "simulated open handle")
            unlink(path)
            if path == target:
                loop.call_soon_threadsafe(removed.set)

        self.s.retire_terrain(earthd.block_files(old))
        with mock.patch.object(earthd, "BLOCK_DELETE_RETRY_S", 0.05), mock.patch.object(os, "remove", sharing):
            c.dispatch(dict(type="terrain_release", tickets=[ticket]))
            await asyncio.wait_for(blocked.wait(), 2)
            self.assertTrue(files[0].exists())
            await asyncio.wait_for(removed.wait(), 3)
            self.s.removes_flush()
        self.assertFalse(any(p.exists() for p in files))
        self.assertTrue(all(p.exists() for p in current))
