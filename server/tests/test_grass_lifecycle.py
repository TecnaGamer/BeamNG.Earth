"""Late grass imagery publishes coherent, current terrain generations without network activity."""
import asyncio
from collections import OrderedDict, deque
from concurrent.futures import Future
import itertools
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402
import geodesy  # noqa: E402
import grass_layers  # noqa: E402
import surface  # noqa: E402

KEY = (7, 128, 0)


class QueuedLoop:
    def __init__(self):
        self.ready = deque()
        self.later = []

    def call_soon_threadsafe(self, fn):
        self.ready.append(fn)

    def call_later(self, delay, fn, *args):
        self.later.append((delay, fn, args))

    def drain(self):
        while self.ready:
            self.ready.popleft()()


class QueuedWorker:
    def __init__(self):
        self.jobs = deque()

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((fn, args, future))
        return future

    def finish(self):
        fn, args, future = self.jobs.popleft()
        try:
            future.set_result(fn(*args))
        except Exception as e:
            future.set_exception(e)


def info(base="quick.png", ter="original.ter"):
    return {"base": base, "ter": ter, "layers": ["grass.png"], "mats": [1]}


class BaseLaterGrassTest(unittest.TestCase):
    def setUp(self):
        self.loop, self.pool = QueuedLoop(), QueuedWorker()
        self.messages, self.patches = [], []
        self.current = info()
        self.server = SimpleNamespace(
            connections=[], layout=SimpleNamespace(block=512), vfs_dir="/temp/earth",
            hm={KEY: self.current}, quick_pool=self.pool, quick_base_name=lambda key: "quick.png")

        def terrain_message(key, current):
            self.assertEqual(current["base"], "full.png", "publish the new base with its material table")
            return {"cx": key[1], "cy": key[2], "ter": current["ter"],
                    "materials": list(current["mats"]), "base": current["base"]}

        connection = SimpleNamespace(loop=self.loop, send=self.messages.append, terrain_msg=terrain_message)
        self.server.connections.append(connection)

        def patch(key, ter, layers, rgb, colors, current, refresh=False):
            self.assertTrue(refresh, "a shown .ter needs a fresh filename for its new layers")
            self.patches.append((key, ter, colors))
            current["ter"] = "grass-revision.ter"
            current["mats"] = [10]

        self.server._photo_lawns = patch
        self.future = Future()
        self.future.set_result(("full.png", ["display-rgb", "analysis-colors"], 0.1))

    def start(self):
        earthd.Server._base_later(self.server, KEY, self.future)
        self.loop.drain()

    def test_publishes_only_after_patch_with_matching_materials_ter_and_base(self):
        self.start()
        self.assertEqual(len(self.pool.jobs), 1)
        self.assertFalse(self.messages, "unpatched compact material indices must not be sent")
        self.assertEqual(self.current["base"], "quick.png")
        self.pool.finish()
        self.assertFalse(self.messages, "publishing returns to the event loop after the worker")
        self.loop.drain()
        self.assertEqual(len(self.messages), 1)
        message = self.messages[0]
        self.assertEqual(message["base"], "/temp/earth/full.png")
        self.assertEqual(message["terrain"]["ter"], "grass-revision.ter")
        self.assertEqual(message["terrain"]["materials"], [10])
        self.assertTrue(message["terrain"]["grassRefresh"], "retained gcpatch cells need regeneration")
        self.assertEqual(message["terrain"]["anchorId"], KEY[0])
        self.assertEqual(self.patches, [(KEY, "original.ter", "analysis-colors")])

    def test_replaced_block_is_not_published_by_an_older_patch(self):
        self.start()
        replacement = info(base="newer.png", ter="roads-rebuilt.ter")
        self.server.hm[KEY] = replacement
        self.pool.finish()
        self.loop.drain()
        self.assertFalse(self.messages, "a late grass job cannot restore the old road/terrain generation")
        self.assertEqual(replacement["base"], "newer.png")
        self.assertEqual(replacement["ter"], "roads-rebuilt.ter")

    def test_evicted_block_is_not_republished_by_a_late_patch(self):
        self.start()
        del self.server.hm[KEY]
        self.pool.finish()
        self.loop.drain()
        self.assertFalse(self.messages)

    def test_newer_full_photo_is_not_replaced_by_an_older_original_photo(self):
        self.current["base"] = "full_r9.png"
        self.start()
        self.assertFalse(self.pool.jobs)
        self.assertFalse(self.messages)
        self.assertEqual(self.current["base"], "full_r9.png")


class ImmediateExecutorLoop:
    async def run_in_executor(self, pool, fn, *args):
        return fn(*args)


class BaseRefreshGrassTest(unittest.TestCase):
    def refresh_fixture(self, replace_during_patch=False):
        current, replacement = info(base="old.png"), info(base="newer.png", ter="roads-rebuilt.ter")
        removed, messages, patches, tasks = [], [], [], []
        server = SimpleNamespace(
            base_stale={KEY}, imagery=object(), near_visual="detail", anchors={KEY[0]: object()},
            hm_committed=[KEY], hm_sent=[KEY], hm={KEY: current}, base_skipped=set(),
            base_ready=lambda *args: True, layout=SimpleNamespace(block=512), block_pools=[],
            hm_pool=object(), quick_pool=object(), base_rev=itertools.count(1), vfs_dir="/temp/earth",
            out_dir="unused", _remove=removed.append, base_name=lambda key: "initial.png")

        def write_base(anchor, cx, cy, pool, revision, photo):
            photo.extend(("rgb", "colors"))
            return "full_r1.png"

        def patch(key, ter, layers, rgb, colors, current_info, refresh=False):
            self.assertTrue(refresh)
            patches.append((key, colors))
            current_info["mats"] = [10]
            current_info["ter"] = "grass-revision.ter"
            if replace_during_patch:
                server.hm[KEY] = replacement

        server.write_base, server._photo_lawns = write_base, patch
        connection = SimpleNamespace(s=server, base_busy=False, loop=ImmediateExecutorLoop(),
                                     send=messages.append, spawn=tasks.append)
        connection.terrain_msg = lambda key, state: {"ter": state["ter"], "mats": list(state["mats"]),
                                                     "base": state["base"]}
        return connection, server, current, replacement, messages, patches, tasks

    def test_refresh_sends_new_palette_with_new_base_after_patch(self):
        connection, server, current, replacement, messages, patches, tasks = self.refresh_fixture()
        earthd.Connection.base_refresh(connection, KEY[0])
        self.assertTrue(connection.base_busy)
        asyncio.run(tasks.pop())
        self.assertFalse(connection.base_busy)
        self.assertEqual(patches, [(KEY, "colors")])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["base"], "/temp/earth/full_r1.png")
        self.assertEqual(messages[0]["terrain"], {"ter": "grass-revision.ter", "mats": [10],
                                                   "base": "full_r1.png", "anchorId": KEY[0],
                                                   "grassRefresh": True})

    def test_refresh_cannot_publish_a_block_replaced_while_patch_was_running(self):
        connection, server, current, replacement, messages, patches, tasks = self.refresh_fixture(True)
        earthd.Connection.base_refresh(connection, KEY[0])
        asyncio.run(tasks.pop())
        self.assertFalse(connection.base_busy)
        self.assertFalse(messages)
        self.assertIs(server.hm[KEY], replacement)
        self.assertEqual(replacement["base"], "newer.png")


class GrassRevisionFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        idx = np.full((32, 32), surface.GRASS, np.uint8)
        self.heights = np.arange(idx.size, dtype=np.uint16).reshape(idx.shape)
        _, mats = surface.write_ter(self.root, "b_0.ter", self.heights, idx)
        masks = surface.write_masks(idx[::-1], self.root, "b")
        self.state = {"ter": "b_0.ter", "layers": masks, "mats": mats}
        self.server = SimpleNamespace(
            grass_lock=threading.Lock(), out_dir=self.root, ter_serial=itertools.count(10),
            anchors={KEY[0]: SimpleNamespace(frame=geodesy.EnuFrame(41.14, -104.82), first_terrain=None)},
            layout=SimpleNamespace(block=32), hm=OrderedDict(((KEY, self.state),)),
            hm_committed=[], hm_sent=[], connections=[], base_stale=set(), base_pending={},
            near_visual="mesh", surface=object(), _remove=earthd.Server._remove,
            hm_name=lambda key: "b.png", base_name=lambda key: "b_base.png", quick_base_name=lambda key: "b_quick.png")
        self.server._remove_base = lambda name: earthd.Server._remove(os.path.join(self.root, name))

    def refresh(self, color):
        rgb = np.broadcast_to(np.array((42, 78, 36), np.uint8), (32, 32, 3))
        colors = np.broadcast_to(np.array(color, np.uint8), (32, 32, 3))
        earthd.Server._photo_lawns(self.server, KEY, self.state["ter"], self.state["layers"],
                                   rgb, colors, self.state, True)

    def test_refresh_creates_a_new_ter_and_tracks_predecessors_for_safe_cleanup(self):
        self.refresh((160, 130, 80))
        first = self.state["ter"]
        self.assertNotEqual(first, "b_0.ter", "the engine skips an unchanged terrainFile name")
        self.assertEqual(self.state["grassOldTers"], ["b_0.ter"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "b_0.ter")), "an in-flight client can still read it")
        self.refresh((125, 151, 80))
        second = self.state["ter"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.state["grassOldTers"], ["b_0.ter", first])
        self.assertEqual(second.count("_grass"), 1, "revision names do not grow recursively")
        heights, layers, mats = grass_layers.read_ter(os.path.join(self.root, second))
        np.testing.assert_array_equal(heights, self.heights)
        self.assertTrue((layers[1:-1, 1:-1] == surface.GRASS_LIGHT).all())
        self.assertEqual(self.state["mats"], mats)

    def test_eviction_removes_current_and_predecessor_terrain_files(self):
        self.refresh((160, 130, 80))
        self.refresh((125, 151, 80))
        files = self.state["grassOldTers"] + [self.state["ter"]]
        self.assertTrue(all(os.path.exists(os.path.join(self.root, p)) for p in files))
        with patch.object(earthd, "HM_CACHE", 0):
            earthd.Server.evict_heightmaps(self.server)
        self.assertNotIn(KEY, self.server.hm)
        self.assertTrue(all(not os.path.exists(os.path.join(self.root, p)) for p in files))


if __name__ == "__main__":
    unittest.main()
