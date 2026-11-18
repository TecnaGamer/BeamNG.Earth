"""Connection.prov_refresh: a provisional block sent before its road / land cover cells were on disk is built again
(still provisional: the roads painted in and laid flat, l0roads.py) once they are, and sent as terrain_refresh with
the photo the mod shows; never after the graded block went, and only once."""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import earthd  # noqa: E402
import geodesy  # noqa: E402


class FakeMapData:
    def __init__(self):
        self.here = False

    def has(self, key, layer):
        return self.here


class ProvRefreshTest(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.pool = ThreadPoolExecutor(1)
        self.md = FakeMapData()
        self.built = []
        self.gate = None
        anchor = types.SimpleNamespace(id=1, frame=geodesy.EnuFrame(40.0, -80.0), lat=40.0, lon=-80.0)
        self.anchor = anchor

        def remake(a, cx, cy):
            if self.gate is not None:
                self.gate()
            name = f"hm_1_0_0_r{len(self.built)}.ter"
            open(os.path.join(self.out, name), "wb").close()
            self.built.append(name)
            return {"ter": name, "stages": {"l0Samples": 10.0, "l0roads": 0.01}}
        s = types.SimpleNamespace(mapdata=self.md, surface=object(), anchors={1: anchor},
                                  layout=types.SimpleNamespace(block=1024), prov_pool=self.pool,
                                  remake_provisional=remake, base_shown={}, out_dir=self.out, prov_info={},
                                  _remove=earthd.Server._remove, ground_bases={}, hm_committed=[(1, 0.0, 0.0)])
        self.grounds = []

        def ground(a, cx, cy, info):
            name = f"g{len(self.grounds)}.jpg"
            open(os.path.join(self.out, name), "wb").close()
            self.grounds.append((name, info["ter"]))
            s.ground_bases.setdefault((a.id, cx, cy), []).append(name)
            return name
        s.write_ground_base = ground
        self.sent = []
        conn = types.SimpleNamespace(s=s, prov_live={}, prov_busy=False, sent=self.sent, prov_last=None)
        conn.send = self.sent.append
        conn.terrain_msg = lambda key, info: {"cx": key[1], "cy": key[2], "ter": info.get("ter"),
                                              "base": info.get("base")}
        for name in ("note_provisional", "graded_sent", "prov_refresh", "_prov_refresh_run"):
            setattr(conn, name, getattr(earthd.Connection, name).__get__(conn))
        self.conn = conn

    def tearDown(self):
        self.pool.shutdown(wait=True)

    def run_loop(self, coro_fn):
        async def go():
            self.conn.loop = asyncio.get_running_loop()
            tasks = []
            self.conn.spawn = lambda c: tasks.append(asyncio.ensure_future(c))
            await coro_fn()
            for t in tasks:
                await t
        asyncio.run(go())

    def test_complete_block_is_not_kept(self):
        if not earthd.L0_REFRESH:
            self.skipTest("BNGE_L0_REFRESH=0")
        self.conn.note_provisional(self.anchor, (1, 0.0, 0.0), {"ter": "a.ter", "roadsComplete": True,
                                                                 "surfaceComplete": True})
        self.assertEqual(self.conn.prov_live, {})

    def test_built_again_once_its_cells_are_here(self):
        if not earthd.L0_REFRESH:
            self.skipTest("BNGE_L0_REFRESH=0")
        key = (1, 0.0, 0.0)
        open(os.path.join(self.out, "photo.dds"), "wb").close()
        self.conn.s.base_shown[key] = "photo.dds"

        async def go():
            self.conn.note_provisional(self.anchor, key, {"ter": "a.ter", "roadsComplete": False})
            self.conn.prov_refresh(1)
            self.assertFalse(self.conn.prov_busy, "cells not here: nothing built")
            self.md.here = True
            self.conn.prov_refresh(1)
            self.assertTrue(self.conn.prov_busy)
        self.run_loop(go)
        self.assertEqual(len(self.built), 1)
        self.assertEqual([m["type"] for m in self.sent], ["terrain_refresh"])
        self.assertEqual(self.sent[0]["ter"], self.built[0])
        self.assertEqual(self.sent[0]["base"], "photo.dds", "sent with the photo the mod shows")

        async def again():
            self.conn.prov_refresh(1)
        self.run_loop(again)
        self.assertEqual(len(self.built), 1, "once per block")
        olds = self.conn.graded_sent(key)
        self.assertEqual(sorted(olds), sorted(["a.ter", self.built[0]]), "the graded block's sender deletes both")

    def test_ground_stand_in_made_again(self):
        """A ground stand-in shown (l0roads.ground_photo: no imagery on disk at the teleport) is made again from the
        refreshed block's layers, the roads in it, under a new name."""
        if not earthd.L0_REFRESH:
            self.skipTest("BNGE_L0_REFRESH=0")
        key = (1, 0.0, 0.0)
        self.md.here = True
        open(os.path.join(self.out, "g_first.jpg"), "wb").close()
        self.conn.s.base_shown[key] = "g_first.jpg"
        self.conn.s.ground_bases[key] = ["g_first.jpg"]

        async def go():
            self.conn.note_provisional(self.anchor, key, {"ter": "a.ter", "roadsComplete": False})
            self.conn.prov_refresh(1)
        self.run_loop(go)
        self.assertEqual(self.grounds, [("g0.jpg", self.built[0])], "made from the refreshed block")
        self.assertEqual(self.sent[0]["base"], "g0.jpg")

    def test_not_sent_after_the_graded_block(self):
        if not earthd.L0_REFRESH:
            self.skipTest("BNGE_L0_REFRESH=0")
        key = (1, 0.0, 0.0)
        self.md.here = True
        import threading
        started, release = threading.Event(), threading.Event()

        def gate():
            started.set()
            release.wait(5)
        self.gate = gate

        async def go():
            self.conn.note_provisional(self.anchor, key, {"ter": "a.ter", "surfaceComplete": False})
            self.conn.prov_refresh(1)
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
            self.conn.graded_sent(key)          # (refine_first sent the graded block meanwhile)
            release.set()
        self.run_loop(go)
        self.assertEqual(self.sent, [], "the graded block is the better one: no refresh after it")
        self.assertFalse(os.path.exists(os.path.join(self.out, self.built[0])), "its .ter removed")


if __name__ == "__main__":
    unittest.main()
