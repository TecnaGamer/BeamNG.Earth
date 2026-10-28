"""A terrain block built before every road cell under it was on disk (surface.Surface.roads leaves a missing cell's
roads out) is built again once those cells are here (Connection.roads_refresh): its roads kept grass and gravel, and
their grip, on a fast first visit's mountain roads (Discord, 2026-10-26)."""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import earthd  # noqa: E402
import surface as sf  # noqa: E402
from test_gasstations import _Writer  # noqa: E402


class RoadsRefreshTest(unittest.TestCase):
    def server(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sur = sf.Surface(None)
        self.addCleanup(sur.close)
        cached = set()
        md = types.SimpleNamespace(has=lambda key, layer: (str(key), layer) in cached)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, cells=64, block=512, surface=sur,
                          mapdata=md)
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(s.tile_pool.shutdown)
        return s, cached

    def test_block_built_again_once_its_road_cells_are_here(self):
        import mapcells
        import heightmap
        import numpy as np
        s, cached = self.server()
        a = s.new_anchor(36.36, 138.63)
        s.anchors[a.id] = a
        key = (a.id, 0.0, 0.0)
        # a block finished without some of its roads: marked for building again
        fut = types.SimpleNamespace(cancelled=lambda: False, exception=lambda: None, result=lambda: {
            "ter": "old.ter", "zoom": 16, "seconds": 1.0, "zBase": 0.0, "maxHeight": 100.0, "q": 1 / 64,
            "surfaceComplete": False, "roadsComplete": False})
        s._hm_done(key, fut)
        self.assertIn(key, s.roads_rebuild)
        s.hm_committed.append(key)
        built = []

        async def terrain(anchor, cx, cy, *args, **kw):
            built.append((cx, cy))
            s.hm[(anchor.id, cx, cy)] = info = {"ter": "new.ter"}
            return info
        s.terrain = terrain
        half = s.layout.block * heightmap.SQUARE / 2
        la, lo, _ = a.frame.enu_to_geodetic(np.array([-half, half]), np.array([-half, half]), 0.0)
        cells = mapcells.cells_for_bbox(float(min(la)), float(min(lo)), float(max(la)), float(max(lo)))

        async def go():
            w = _Writer()
            c = earthd.Connection(s, None, w)
            c.terrain_msg = lambda k, info: {"cx": k[1], "cy": k[2], "size": 512}
            c.vel = (30.0, 0.0)                     # (at any speed: the car is on it)
            c.roads_refresh(a.id)
            await asyncio.sleep(0.05)
            before = list(built)                    # (its cells not here yet: nothing)
            cached.update((str(k), "roads") for k in cells)
            c.roads_refresh(a.id)
            await asyncio.sleep(0.05)
            roads_only = list(built)                # (its land cover not here yet either: still nothing)
            cached.update((str(k), "landcover") for k in cells)
            c.roads_refresh(a.id)
            await asyncio.sleep(0.05)
            c.roads_refresh(a.id)                   # (once)
            await asyncio.sleep(0.05)
            return before, roads_only, list(built), w.msgs
        before, roads_only, done, msgs = asyncio.run(go())
        self.assertEqual(before, [])
        self.assertEqual(roads_only, [])
        self.assertEqual(done, [(0.0, 0.0)])
        self.assertEqual([m["type"] for m in msgs if m["type"] == "terrain_refresh"], ["terrain_refresh"])
        self.assertNotIn(key, s.roads_rebuild)

    def test_complete_block_not_marked(self):
        s, _ = self.server()
        fut = types.SimpleNamespace(cancelled=lambda: False, exception=lambda: None, result=lambda: {
            "ter": "t.ter", "zoom": 16, "seconds": 1.0, "zBase": 0.0, "maxHeight": 100.0, "q": 1 / 64,
            "surfaceComplete": True, "roadsComplete": True})
        s._hm_done((1, 0.0, 0.0), fut)
        self.assertEqual(s.roads_rebuild, set())

    def test_block_without_its_land_cover_marked(self):
        # (a first visit's block before its land cover: bare dirt for good, Harz 2026-10-27, GitHub #316; built again
        # only once the land cover is on disk, roads_refresh: no rebuild loop)
        s, _ = self.server()
        fut = types.SimpleNamespace(cancelled=lambda: False, exception=lambda: None, result=lambda: {
            "ter": "t.ter", "zoom": 16, "seconds": 1.0, "zBase": 0.0, "maxHeight": 100.0, "q": 1 / 64,
            "surfaceComplete": False, "roadsComplete": True})
        s._hm_done((1, 0.0, 0.0), fut)
        self.assertEqual(s.roads_rebuild, {(1, 0.0, 0.0)})


if __name__ == "__main__":
    unittest.main()
