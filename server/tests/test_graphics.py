"""The settings page's graphics knobs on the server (the user, 2026-10-28: "give player much more control in the graphics
settings (view distance, LOD quality, ETC. Also add an extreme quality setting that puts everything at max quality to
the real horizon"): the mod's 'graphics' message {lodScale, treeScale, buildingScale, photoPx} (Connection.set_graphics)
scales the far field's K (more / fewer tiles, the exact-level rule near the car unchanged), the tree tiles' and the far
buildings' / skyline's / far bridges' reach, and sets the ground photo's size (Server.set_base_px: the photo, its packed
cache entry and every size's flat AO map the level ships)."""
import asyncio
import os
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import buildings  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import terraincache  # noqa: E402
import vegetation  # noqa: E402
from test_server import LAT, LON, ServerHarness  # noqa: E402

LEVEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth")


class _Writer:
    def write(self, data):
        pass

    def close(self):
        pass

    def get_extra_info(self, name):
        return ("127.0.0.1", 0)


class GraphicsMessageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = earthd.Server(self.tmp.name, os.path.join(self.tmp.name, "cache"), imagery=None, cells=64, block=1024,
                               near_visual="detail")
        self.addCleanup(self.s.hm_pool.shutdown)
        self.addCleanup(self.s.tile_pool.shutdown)

    def conn(self, msgs):
        async def go():
            c = earthd.Connection(self.s, None, _Writer())
            for m in msgs:
                c.dispatch(m)
            return c
        return asyncio.run(go())

    def test_defaults_are_todays(self):
        c = self.conn([])
        self.assertEqual((c.lod_scale, c.tree_scale, c.building_scale), (1.0, 1.0, 1.0))
        self.assertIs(c.kfun(), self.s.kfun, "no scale: the server's own K")
        self.assertEqual((c.veg.send_m, c.bldfar.send_m), (vegetation.SEND_M, buildings.FAR_SEND_M))
        self.assertEqual(self.s.base_px, earthd.BASE_PX)

    def test_scales(self):
        c = self.conn([{"type": "graphics", "lodScale": 2.0, "treeScale": 2.2, "buildingScale": 1.5, "photoPx": 4096}])
        self.assertEqual(c.kfun()(1), 2.0 * self.s.kfun(1))
        self.assertEqual(c.kfun()(5), 2.0 * self.s.kfun(5))
        self.assertAlmostEqual(c.veg.send_m, vegetation.SEND_M * 2.2)
        self.assertAlmostEqual(c.veg.drop_m - c.veg.send_m, vegetation.DROP_M - vegetation.SEND_M, msg="its hysteresis")
        self.assertEqual(c.bldfar.send_m, buildings.FAR_SEND_M * 1.5)
        self.assertEqual(c.bldfar.drop_m - c.bldfar.send_m, buildings.FAR_DROP_M - buildings.FAR_SEND_M)
        self.assertEqual(c.bldsky.send_m, buildings.SKY_SEND_M * 1.5)
        self.assertEqual(c.bldsky.inner, (c.bldfar.send_m, buildings.SKY_UNIT), "the skyline leaves the far ones' groups")
        self.assertEqual(c.brfar.send_m, buildings.BRIDGE_FAR_SEND_M * 1.5)
        self.assertEqual(self.s.base_px, 4096)
        # back to the defaults; junk and out-of-range values
        c = self.conn([{"type": "graphics", "lodScale": 1, "treeScale": 1, "buildingScale": 1},
                       {"type": "graphics", "lodScale": "x", "treeScale": True, "buildingScale": float("nan")},
                       {"type": "graphics", "photoPx": 777}])
        self.assertEqual((c.lod_scale, c.tree_scale, c.building_scale), (1.0, 1.0, 1.0))
        self.assertEqual(c.veg.send_m, vegetation.SEND_M)
        self.assertEqual(self.s.base_px, 4096, "an unknown size: unchanged")
        c = self.conn([{"type": "graphics", "lodScale": 99, "treeScale": 0.01}])
        self.assertEqual((c.lod_scale, c.tree_scale), (earthd.GRAPHICS_SCALE[1], earthd.GRAPHICS_SCALE[0]), "clamped")

    def test_fixed_view_distance_caps_the_far_field(self):
        c = self.conn([{"type": "graphics", "viewM": 40000}])
        self.assertEqual(c.view_cap, 40000.0)
        c = self.conn([{"type": "graphics", "viewM": 500}, {"type": "graphics", "lodScale": 1.3}])
        self.assertEqual(c.view_cap, earthd.VIEW_CAP_MIN_M, "at least 10 km; a message without it keeps it")
        c = self.conn([{"type": "graphics", "viewM": 40000}, {"type": "graphics", "viewM": 0}])
        self.assertIsNone(c.view_cap, "0: the real horizon again")

    def test_terrain_detail_scales_the_far_field_but_not_the_exact_level(self):
        lay = self.s.layout
        block = ff.Block(0.0, 0.0, 512).bbox
        ring = (block[0] - 128, block[1] - 128, block[2] + 128, block[3] + 128)
        counts = {}
        for scale in (0.75, 1.0, 2.0):
            c = self.conn([{"type": "graphics", "lodScale": scale}])
            tiles, _ = ff.select(37.0, -91.0, [ring], kfun=c.kfun(), radius=150000.0, max_level=ff.HORIZON_MAX_LEVEL,
                                 min_level=lay.min_level, radii=self.s.near_radii, layout=lay, disc=True, forced_level=-1)
            counts[scale] = len(tiles)
            under = [t for t in tiles.values() if ff.overlaps(t.bbox, block)]
            self.assertEqual({t.level for t in under}, {lay.min_level}, f"x{scale}: the exact level under the block")
            near = [t for t in tiles.values() if t.distance(37.0, -91.0) < self.s.near_radii[lay.min_level + 1]]
            self.assertTrue(all(t.level <= lay.min_level + 1 for t in near), f"x{scale}: the near radii hold")
        self.assertLess(counts[0.75], counts[1.0])
        self.assertLess(counts[1.0], counts[2.0])
        self.assertLess(counts[2.0], 2.5 * counts[1.0])

    def test_photo_size_marks_built_blocks(self):
        a = self.s.new_anchor(40.0, -80.0)
        self.s.anchors[a.id] = a
        self.s.hm[(a.id, 0.0, 0.0)] = {}
        self.assertTrue(self.s.set_base_px(1024))
        self.assertIn((a.id, 0.0, 0.0), self.s.base_stale, "its photo again at the new size")
        self.assertFalse(self.s.set_base_px(1024), "no change")
        self.assertFalse(self.s.set_base_px("big"))

    def test_every_size_has_its_flat_maps(self):
        for px in earthd.BASE_SIZES:
            for m in ("b", "nm", "r", "ao", "h"):
                vfs = earthd.base_ao_vfs(px).replace("_ao", "_" + m)
                path = os.path.join(LEVEL, vfs.lstrip("/"))
                self.assertTrue(os.path.exists(path), vfs)
                with Image.open(path) as im:
                    self.assertEqual(im.size, (px, px), vfs)
        self.assertEqual(earthd.base_ao_vfs(earthd.BASE_PX), earthd.BASE_AO_VFS, "the default keeps today's name")


class DiscTest(unittest.TestCase):
    """The reach of every tile kind is a disc (the user, 2026-10-28: "change the quality/view distance radius from a
    square to a circle"): the far buildings' whole groups and the skyline's squares agree (no overlap, no hole), and a
    corner square inside the old square but beyond the radius is not wanted."""

    def test_far_buildings_and_skyline_meet(self):
        import roads
        far = vegetation.Tracker(buildings.FAR_SEND_M, buildings.FAR_DROP_M, 1, "bldfar", group=buildings.SKY_UNIT)
        sky = vegetation.Tracker(buildings.SKY_SEND_M, buildings.SKY_DROP_M, 1, "bldsky", unit=buildings.SKY_UNIT,
                                 inner=(buildings.FAR_SEND_M, buildings.SKY_UNIT))
        u = buildings.SKY_UNIT
        for x, y in ((0.0, 0.0), (700.0, -300.0), (1023.0, 1023.0)):
            groups = {(k[0] // u, k[1] // u) for k in far._near(x, y, far.send_m)}
            inner = set(sky._near(x, y, sky.inner[0], unit=sky.unit, group=sky.inner[1]))
            self.assertEqual(groups, inner, "the skyline leaves out exactly the far buildings' groups")
            for g in groups:        # every group within the radius by its own square's nearest point
                self.assertLessEqual(roads.box_gap(g[0] * u * 256, g[1] * u * 256, (g[0] + 1) * u * 256,
                                                   (g[1] + 1) * u * 256, x, y), far.send_m)
        # a 1 km square whose corner is 1.7 km away (each axis 1.2 km: inside the old square of 1.6 km) is not wanted
        self.assertNotIn((1, 1), sky._near(-200.0, -200.0, 1600.0))
        self.assertIn((1, 0), sky._near(-200.0, -200.0, 1600.0))

    def test_tree_tiles_disc(self):
        t = vegetation.Tracker()
        near = set(t._near(0.0, 0.0, t.send_m))
        self.assertIn((4, 0), near)                 # its side 1024 m away
        self.assertNotIn((4, 4), near)              # its corner 1448 m away (Chebyshev 1024: wanted before)
        self.assertEqual(t._dist((4, 4), 0.0, 0.0), 1024 * 2 ** 0.5)


class PhotoSizeSessionTest(ServerHarness):
    near_visual = "detail"
    with_imagery = True
    cells = 64
    block = 1024

    def test_a_1024_photo(self):
        c = self.c
        self.hello()
        c.send({"type": "graphics", "photoPx": 1024})
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "prepare_terrain", "requestId": 9, "anchorId": aid, "cx": 0.0, "cy": 0.0})
        tr, _ = c.until(lambda m: m["type"] == "terrain_ready" and m.get("requestId") == 9, timeout=60)
        im = Image.open(self.fs(tr["base"]))
        self.assertEqual(im.size, (1024, 1024))
        self.assertGreater(np.asarray(im).std(), 5.0)
        cache = os.path.join(self.tmp.name, "temp", "art", "terrainmaterialcache",
                             terraincache.base_key(tr["base"], earthd.base_ao_vfs(1024)) + ".dds")
        self.assertTrue(os.path.exists(cache), "packed under the 1024 AO map's key (the layers' aoBaseTex at 1024)")

    def test_view_cap_and_terrain_detail_in_a_session(self):
        import time
        c = self.c
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, 0.0, 0.0)

        def desired(n0=None):
            t0 = time.monotonic()
            while time.monotonic() - t0 < 10:
                d = dict(self.conn().desired)
                if d and (n0 is None or len(d) != n0):
                    return d
                time.sleep(0.05)
            return dict(self.conn().desired)
        full = desired()
        c.send({"type": "graphics", "viewM": 20000})
        capped = desired(len(full))
        self.assertLess(len(capped), len(full))
        self.assertTrue(all(ff.tile_of(k).distance(0.0, 0.0) < 20000 for k in capped), "nothing past the fixed view")
        c.send({"type": "graphics", "viewM": 0, "lodScale": 2.0})
        finer = desired(len(capped))
        self.assertGreater(len(finer), len(full), "the real horizon again, and finer")


if __name__ == "__main__":
    unittest.main()
