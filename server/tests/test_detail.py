"""The 'detail' near-visual mode (2026-10-12): the mesh layout (64 cells, the 512² block) with the
physics block drawn as the near ground. The server forces L-1 tiles under the block (their 128 m edges
meet the block's 128 m-grid edges), gives them sinks, and writes the block's aerial photo (BASE_PX²,
the block's footprint) next to its heightmap; terrain messages name it (base, baseSize)."""
import os
import sys
import time
import unittest
import threading

import struct

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import bc7  # noqa: E402
import earthd  # noqa: E402
import terraincache  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402
import test_server  # noqa: E402
from test_server import LAT, LON, FakeDem, ServerHarness  # noqa: E402
from netclient import FarState  # noqa: E402

LAYOUT = ff.Layout(64, 512, True)


class SelectTest(unittest.TestCase):
    def test_forced_level_minus_one(self):
        """A 512² block centred off the 256 m grid: with forced_level -1 no tile coarser than L-1 overlaps it,
        so every tile under it lies wholly inside (covered) or wholly outside."""
        block = ff.Block(128.0, -128.0, 512).bbox                       # [-384, -640, 640, 384]
        tiles, _ = ff.select(2000.0, 2000.0, [block], min_level=LAYOUT.min_level, layout=LAYOUT, forced_level=-1)
        under = [t for t in tiles.values() if ff.overlaps(t.bbox, block)]
        self.assertTrue(under)
        self.assertEqual({t.level for t in under}, {-1})
        for t in under:
            x0, y0, x1, y1 = t.bbox
            self.assertTrue(x0 >= block[0] and y0 >= block[1] and x1 <= block[2] and y1 <= block[3]
                            or x1 <= block[0] or x0 >= block[2] or y1 <= block[1] or y0 >= block[3])
        # L0 forcing (terrain mode) leaves 256 m tiles straddling this block's edges
        tiles0, _ = ff.select(2000.0, 2000.0, [block], min_level=LAYOUT.min_level, layout=LAYOUT)
        self.assertTrue(any(ff.overlaps(t.bbox, block) and t.level == 0 for t in tiles0.values()))

    def test_sink_on_l_minus_one(self):
        frame = geodesy.EnuFrame(LAT, LON)
        b = ff.build_tile(frame, FakeDem(), ff.Tile(-1, 0, 0), 15, 1 / 64, with_sink=True, layout=LAYOUT)
        self.assertIsNotNone(b.sink)
        self.assertGreaterEqual(b.sink, ff.SINK_MARGIN)     # the exact level: its vertices are the lattice
        self.assertLess(b.sink, ff.SINK_MARGIN + 0.05)
        b2 = ff.build_tile(frame, FakeDem(), ff.Tile(-1, 0, 0), 15, 1 / 64, with_sink=False, layout=LAYOUT)
        self.assertIsNone(b2.sink)


class DetailServerTest(ServerHarness):
    near_visual = "detail"
    with_imagery = True
    cells = 64
    block = 512

    def test_detail_session(self):
        c = self.c
        far = FarState()
        self.hello()                    # asserts hello.nearVisual == "detail"
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, 0.0, 0.0)
        done, seen = self.stream_until_done(far, aid)
        keys = far.keys(aid)
        block = ff.Block(0.0, 0.0, 512).bbox
        under = [ff.tile_of(k) for k in keys if ff.overlaps(ff.tile_of(k).bbox, block)]
        self.assertTrue(under)
        self.assertEqual({t.level for t in under}, {-1}, "only L-1 tiles under the drawn block")
        # and in a DETAIL_RING_M ring around it (the block's own 2 m lattice meets its neighbours)
        g = earthd.DETAIL_RING_M
        ring = (block[0] - g, block[1] - g, block[2] + g, block[3] + g)
        near = [ff.tile_of(k) for k in keys if ff.overlaps(ff.tile_of(k).bbox, ring)]
        self.assertEqual({t.level for t in near}, {-1}, "only L-1 tiles in the ring around the block")
        self.assertGreater(len(near), len(under))
        tiles = {m["key"]: m for m in seen if m["type"] == "far_tile"}
        self.assertTrue(all("sink" in tiles[t.key] for t in under if t.key in tiles), "tiles under the block have sinks")
        # a terrain: the base photo next to the heightmap, over the block's footprint
        c.send({"type": "prepare_terrain", "requestId": 9, "anchorId": aid, "cx": 130.0, "cy": -190.0})
        tr, _ = c.until(lambda m: m["type"] == "terrain_ready", timeout=60)
        self.assertEqual((tr["cx"], tr["cy"]), (128.0, -128.0))
        if "base" not in tr:
            photo, _ = c.until(lambda m: m["type"] == "block_base" and m.get("cx") == tr["cx"]
                              and m.get("cy") == tr["cy"], timeout=30)
            tr.update(base=photo["base"], baseSize=photo["baseSize"])
        self.assertEqual(tr["baseSize"], 512 * 2.0)
        path = self.fs(tr["base"])
        self.assertRegex(path, r"_base_r\d+\.png$")
        self.assertTrue(os.path.exists(path))
        im = Image.open(path)
        self.assertEqual((im.size, im.mode), ((earthd.BASE_PX, earthd.BASE_PX), "RGB"))
        self.assertGreater(np.asarray(im).std(), 5.0, "a real photo, not a flat fill")
        # its packed BC7 copy in the game's terrain texture cache, under the game's key, newer than the photo
        cache = os.path.join(self.tmp.name, "temp", "art", "terrainmaterialcache",
                             terraincache.base_key(tr["base"], earthd.BASE_AO_VFS) + ".dds")
        self.assertTrue(os.path.exists(cache))
        self.assertGreaterEqual(os.path.getmtime(cache), os.path.getmtime(path))
        data = open(cache, "rb").read()
        self.assertEqual((data[:4], data[84:88], struct.unpack("<I", data[128:132])[0]),
                         (b"DDS ", b"DX10", bc7.DXGI_BC7_UNORM_SRGB))
        self.assertEqual(struct.unpack("<3I", data[12:20] + data[28:32]), (earthd.BASE_PX, earthd.BASE_PX,
                                                                          int(np.log2(earthd.BASE_PX)) + 1))
        px = bc7.decode(data[148:148 + earthd.BASE_PX ** 2], earthd.BASE_PX, earthd.BASE_PX)
        self.assertTrue((np.abs(px[..., 3].astype(int) - earthd.BASE_AO) <= 1).all(), "alpha = the flat AO")
        self.assertLess(np.abs(px[..., :3].astype(int) - np.asarray(im)).mean(), 4.0)
        # evicted with its heightmap
        key = (aid, 128.0, -128.0)
        self.server.hm_committed.clear(); self.server.hm_sent.clear()
        old_cache = earthd.HM_CACHE
        earthd.HM_CACHE = 0
        try:
            self.server.evict_heightmaps()
        finally:
            earthd.HM_CACHE = old_cache
        self.assertFalse(os.path.exists(path), "base photo removed with its heightmap")
        self.assertFalse(os.path.exists(cache), "and its terrain cache entry")



class DetailBaseRefreshTest(ServerHarness):
    """A block whose base photo was rendered before its NAIP tiles arrived (lazy source) is rendered again
    once they are all on disk, under a new name, and the mod is told (block_base)."""
    near_visual = "detail"
    with_imagery = True
    cells = 64
    block = 512

    def setUp(self):
        super().setUp()
        im = self.server.imagery
        self.server.imagery = test_server.FakeImagery(im.cache_dir, chain=("naip", "usgs", "eox2023", "eox2016"))
        self.server.imagery._probe_status = lambda key: "covered"
        self.server.imagery.disk = im.disk

    def test_refresh(self):
        c = self.c
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        key = (aid, 0.0, 0.0)
        first = self.server.hm[key].get("base")
        if first is None:
            initial, _ = c.until(lambda m: m["type"] == "block_base", timeout=30)
            first = os.path.basename(initial["base"])
        self.assertIn(key, self.server.base_stale, "NAIP not on disk: the photo will be redone")
        self.pose(aid, 0.0, 0.0)
        quiet = c.drain(1.0)
        self.assertFalse([m for m in quiet if m["type"] == "block_base"], "not before its NAIP is here")
        self.server.imagery.lazy_ready = lambda *a: True        # every NAIP tile of the footprint arrived
        self.pose(aid, 0.0, 0.0)
        m, _ = c.until(lambda m: m["type"] == "block_base", timeout=30)
        self.assertEqual((m["anchorId"], m["cx"], m["cy"], m["baseSize"]), (aid, 0.0, 0.0, 512 * 2.0))
        path = self.fs(m["base"])
        self.assertRegex(path, r"_base_r\d+\.png$")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.server.hm[key]["base"], os.path.basename(path))
        self.assertNotEqual(os.path.basename(path), first)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "temp", "art", "terrainmaterialcache",
                                                    terraincache.base_key(m["base"], earthd.BASE_AO_VFS) + ".dds")))
        self.assertNotIn(key, self.server.base_stale)
        self.pose(aid, 0.0, 0.0)
        self.assertFalse([m for m in c.drain(1.0) if m["type"] == "block_base"], "once")


class DetailQuickPhotoTest(ServerHarness):
    """A teleport's provisional photo (write_base quick) does not wait for a fresh place's buildings and roads: it goes
    without the roofs' mask while they are not on disk, and is rendered again in full later (base_stale)."""
    near_visual = "detail"
    with_imagery = True
    cells = 64
    block = 512

    def test_quick_skips_missing_map_data(self):
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        s = self.server
        anchor = s.anchors[aid]
        asked = []

        class MD:
            cached = False

            def has(self, key, layer):
                return self.cached

            def cell(self, key, layers=None, kind=None):
                asked.append(layers)
                raise OSError("not here")
        s.mapdata, s.buildings = MD(), True
        s.base_stale.discard((aid, 0.0, 0.0))
        self.assertIsNotNone(s.write_base(anchor, 0.0, 0.0, quick=True))
        self.assertEqual(asked, [], "no map data asked for")
        self.assertIn((aid, 0.0, 0.0), s.base_stale, "rendered again in full later")
        s.mapdata.cached = True                 # a place seen before: the full photo at once
        s.write_base(anchor, 0.0, 0.0, quick=True)
        self.assertEqual(asked, [("buildings",)] * len(asked))
        self.assertTrue(asked)


class DetailPhotoLaterTest(ServerHarness):
    """At ordinary speeds a blocked photo cannot hold drivable ground; it still arrives in full later."""
    near_visual = "detail"
    with_imagery = True
    cells = 64
    block = 512

    def test_photo_follows_the_block(self):
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        s = self.server
        orig = s.write_base
        release = threading.Event()

        def slow(*a, **kw):
            if not release.wait(10):
                raise TimeoutError("test did not release photo")
            return orig(*a, **kw)
        s.write_base = slow
        try:
            self.c.send({"type": "prepare_terrain", "requestId": 3, "anchorId": aid, "cx": 512.0, "cy": 0.0})
            tr, _ = self.c.until(lambda m: m["type"] == "terrain_ready", timeout=5)
            self.assertNotIn("base", tr, "sent while the photo remains blocked")
            self.assertFalse(release.is_set())
        finally:
            release.set()
        m, _ = self.c.until(lambda m: m["type"] == "block_base" and m.get("cx") == 512., timeout=30)
        self.assertEqual((m["anchorId"], m["cx"], m["cy"]), (aid, 512.0, 0.0))
        self.assertTrue(os.path.exists(self.fs(m["base"])))
        self.assertEqual(s.hm[(aid, 512.0, 0.0)]["base"], os.path.basename(m["base"]))


class DetailNoImageryTest(ServerHarness):
    near_visual = "detail"
    with_imagery = False
    cells = 64
    block = 512

    def test_no_imagery_no_base(self):
        """Without imagery the terrain has no base (the mod keeps the level's flat base)."""
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        self.c.send({"type": "prepare_terrain", "requestId": 3, "anchorId": aid, "cx": 0.0, "cy": 0.0})
        tr, _ = self.c.until(lambda m: m["type"] == "terrain_ready", timeout=60)
        self.assertNotIn("base", tr)


if __name__ == "__main__":
    unittest.main()
