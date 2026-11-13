"""The far field's bulk restore (farrestore.py, earthfast far_restore): a far tile kept as a "farfin" item and put
back writes the very bytes the per-tile path writes (farfield.write_built with the tile's pooled material name), links
its slot and texture, and returns the numbers its TileInfo needs; damaged or missing items come back as reasons (the
tile is then built the usual way), never as wrong files."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import farfield as ff  # noqa: E402
import farrestore  # noqa: E402
import geodesy as g  # noqa: E402
import revisit  # noqa: E402
import revisitpack  # noqa: E402

LAT0, LON0 = 36.5785, -118.2923


class FakeDem:
    def sample(self, lat, lon, zoom, progress=None):
        return 1500 + 300 * np.sin(lat * 90) * np.cos(lon * 70) + zoom * 7.0


class FarRestoreTest(unittest.TestCase):
    LAYOUT = ff.Layout(64)
    NAME = "bngEarthFar_2b54_00012"
    PX = 256

    @classmethod
    def setUpClass(cls):
        frame = g.EnuFrame(LAT0, LON0)
        tile = ff.Tile(1, 0, 0)
        cls.built = ff.build_tile(frame, FakeDem(), tile, ff.zoom_for_level(cls.LAYOUT, LAT0, 1), layout=cls.LAYOUT)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.cache = os.path.join(self.root, "cache")
        self.out = os.path.join(self.root, "out")
        os.makedirs(self.out)
        os.makedirs(self.cache)
        revisitpack.reset()
        self.tex_src = os.path.join(self.root, "rendered.dds")
        with open(self.tex_src, "wb") as f:
            f.write(b"DDS fake texture " * 50)

    def tearDown(self):
        revisitpack.reset()
        self.tmp.cleanup()

    def keep(self, k="k1", sides=True):
        side = {"tex.dds": revisit.Side(self.tex_src)} if sides else None
        self.assertTrue(farrestore.keep(self.cache, k, self.built, self.PX, len(self.NAME), side,
                                        {"tex_bytes": 850, "used": {"usgs": 12}}))
        revisitpack.reset()             # (read back as another process would)
        return farrestore.locate(self.cache, k)

    def item(self, loc, sides, material=None):
        material = material or self.NAME
        shape = os.path.join(self.out, "far_1_x.cdae")
        slot = os.path.join(self.out, "slot_00012.cdae")
        tex = os.path.join(self.out, "far_1_x.dds")
        side_list = [(sides["tex.dds"], tex + ".tmp", tex)] if "tex.dds" in sides else []
        return (loc, farrestore.placeholder(len(self.NAME)), material, (shape + ".tmp", shape, slot + ".tmp", slot),
                side_list)

    def test_same_bytes_as_the_per_tile_path(self):
        loc, sides = self.keep()
        self.assertIn("tex.dds", sides)
        (got,) = farrestore.restore([self.item(loc, sides)])
        self.assertIsInstance(got, tuple, got)
        size, extra, grid = got
        ref = os.path.join(self.root, "ref.cdae")
        n = ff.write_built(ref, self.built, "cdae", self.NAME, textured=True, tex_px=self.PX)
        with open(ref, "rb") as f:
            want = f.read()
        with open(os.path.join(self.out, "far_1_x.cdae"), "rb") as f:
            self.assertEqual(f.read(), want)
        self.assertEqual(size, n)
        with open(os.path.join(self.out, "slot_00012.cdae"), "rb") as f:
            self.assertEqual(f.read(), want)
        with open(os.path.join(self.out, "far_1_x.dds"), "rb") as f, open(self.tex_src, "rb") as g_:
            self.assertEqual(f.read(), g_.read())
        np.testing.assert_array_equal(grid, np.asarray(self.built.grid, np.float32))
        self.assertEqual(extra["pos"], [float(v) for v in self.built.pos])
        self.assertEqual(extra["err"], float(self.built.err))
        self.assertEqual(extra["skirt"], float(self.built.skirt))
        self.assertEqual(extra["tex_bytes"], 850)
        self.assertEqual(extra["used"], {"usgs": 12})
        self.assertEqual(extra["sides"], ["tex.dds"])
        self.assertFalse(any(n.endswith(".tmp") for n in os.listdir(self.out)))

    def test_placeholder_is_not_in_a_real_shape(self):
        p = farrestore.placeholder(len(self.NAME))
        self.assertEqual(len(p), len(self.NAME))
        ref = os.path.join(self.root, "ref.cdae")
        ff.write_built(ref, self.built, "cdae", self.NAME, textured=True, tex_px=self.PX)
        import cdae
        with open(ref, "rb") as f:
            self.assertNotIn(p.encode(), cdae.plain(f.read()))

    def test_damaged_or_mismatched_items_are_reasons(self):
        loc, sides = self.keep()
        bad_crc = (loc[0], loc[1], loc[2], loc[3], loc[4], loc[5] ^ 1)
        short = (loc[0], loc[1], loc[2], loc[3], loc[4] + 1, loc[5])
        gone = (loc[0] + ".missing",) + tuple(loc[1:])
        res = farrestore.restore([self.item(bad_crc, sides), self.item(short, sides), self.item(gone, sides),
                                  self.item(loc, sides, material="bngEarthFar_2b54_000123")])
        self.assertTrue(all(isinstance(r, str) for r in res), res)
        self.assertFalse(os.path.exists(os.path.join(self.out, "far_1_x.cdae")))

    def test_missing_key_and_side_file(self):
        self.assertIsNone(farrestore.locate(self.cache, "nothing"))
        loc, sides = self.keep(sides=True)
        side_file = sides["tex.dds"]
        os.remove(side_file)
        revisitpack.reset()
        loc2, sides2 = farrestore.locate(self.cache, "k1")
        self.assertNotIn("tex.dds", sides2)     # (the server then builds the tile: its record names tex.dds)

    def test_many_in_parallel(self):
        locs = []
        for i in range(12):
            locs.append(self.keep(f"k{i}"))
        items = []
        for i, (loc, sides) in enumerate(locs):
            it = self.item(loc, sides)
            shape = os.path.join(self.out, f"far_{i}.cdae")
            slot = os.path.join(self.out, f"slot_{i:05d}.cdae")
            items.append(it[:3] + ((shape + ".tmp", shape, slot + ".tmp", slot), []))
        res = farrestore.restore(items)
        self.assertTrue(all(isinstance(r, tuple) for r in res), res)
        sizes = {os.path.getsize(os.path.join(self.out, f"far_{i}.cdae")) for i in range(12)}
        self.assertEqual(len(sizes), 1)

    def test_worker_keeps_and_finds_by_texture_size(self):
        """A far tile kept by the worker (blockworker._keep_fin, the size it had then) is found by far_restore_find
        among the sizes asked, the first that is kept; nothing without keep_fin, nothing past the sizes asked."""
        import types
        import blockworker
        saved = blockworker._s
        blockworker._s = types.SimpleNamespace(cache_dir=self.cache, md=None, sea=None, airports=None, widths=None,
                                               surface=None)
        try:
            tile = ff.Tile(3, 1, 2)       # (a far one: no road surfaces in its key)
            tex = {"path": self.tex_src, "px": 256, "mask": None, "chain": ["usgs"], "tone": 1.0, "leaf_on": True,
                   "season_mode": "auto", "keep_fin": len(self.NAME)}
            args = (LAT0, LON0, tile, 14, None, 13, False, self.LAYOUT)
            blockworker._keep_fin(blockworker._far_tile_key(*args, tex), self.built, tex, None, (850, {"usgs": 3}))
            self.assertEqual(self.built.shape_plain[1:], (len(self.NAME), 256))
            plain = dict(tex)
            plain.pop("keep_fin")
            blockworker._keep_fin(blockworker._far_tile_key(*args, dict(tex, px=128)), self.built, plain, None,
                                  (850, {}))          # (no keep_fin: not kept)
            revisitpack.reset()
            spec = ("tile", tile, 14, None, 13, False, self.LAYOUT, tex)
            got = blockworker.far_restore_find(1, LAT0, LON0, [spec + ([512, 256, 128],), spec + ([512, 128],)])
            self.assertIsNotNone(got[0])
            loc, sides, px = got[0]
            self.assertEqual(px, 256)
            self.assertIn("tex.dds", sides)
            self.assertIsNone(got[1])
            (res,) = farrestore.restore([self.item(loc, sides)])
            self.assertEqual(res[1]["tex_bytes"], 850)
        finally:
            blockworker._s = saved

    def test_texture_kept_with_its_regions_to_come(self):
        """Server.keep_texture keeps a far texture whose only corrections to come are colour-match / de-shadow regions
        with them (kept_pending gives them back), not one with a quick render's photos to come."""
        import types
        import earthd
        import imagery
        srv = types.SimpleNamespace(out_dir=self.root, cache_dir=self.cache)
        name = os.path.basename(self.tex_src)
        earthd.Server.keep_texture(srv, "t1", name, 850, {"usgs": 2}, {(3, 4), (imagery.DS_PENDING, 1, 2)}, None)
        earthd.Server.keep_texture(srv, "t2", name, 850, {}, {(imagery.IMG_PENDING, 7)}, None)
        earthd.Server.keep_texture(srv, "t3", name, 850, {}, set(), None)
        revisitpack.reset()
        blobs, extra = revisit.load(self.cache, "fartex", "t1")
        self.assertEqual(earthd.kept_pending(extra), {(3, 4), (imagery.DS_PENDING, 1, 2)})
        self.assertIsNone(revisit.load(self.cache, "fartex", "t2"))
        self.assertEqual(earthd.kept_pending(revisit.load(self.cache, "fartex", "t3")[1]), set())

    def test_stamp_matches_write_built(self):
        plain = ff.built_shape(self.built, farrestore.placeholder(len(self.NAME)), self.PX, packed=False)
        ref = os.path.join(self.root, "ref.cdae")
        ff.write_built(ref, self.built, "cdae", self.NAME, textured=True, tex_px=self.PX)
        with open(ref, "rb") as f:
            self.assertEqual(farrestore.stamp(plain, len(self.NAME), self.NAME), f.read())


if __name__ == "__main__":
    unittest.main()
