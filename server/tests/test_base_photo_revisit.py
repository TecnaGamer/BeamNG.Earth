"""A block's finished base photo kept in the revisit cache (Server.base_revisit_key / _base_keep / _base_revisited): a
place loaded again in a fresh server takes the photo and the game's packed copy from there instead of painting it
again (its roofs' mask alone was 12.7 s on Windows, up to 47 s on a Steam Deck, the first block of a warm level load;
game perf report 2026-11-12). Kept only when final; a photo still to come or stale is painted again."""
import io
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import earthd  # noqa: E402
import revisit  # noqa: E402
import revisitpack  # noqa: E402
import terraincache  # noqa: E402


def fake_server(tmp, px=64):
    S = earthd.Server
    srv = SimpleNamespace(
        imagery=SimpleNamespace(chain=[SimpleNamespace(name="naip")], leaf_on=True, season_mode="auto"),
        layout=SimpleNamespace(block=512), base_px=px, tone=None, tex_modes=(None, None), buildings=True,
        mapdata=None, road_widths=None, surface=None, cache_dir=os.path.join(tmp, "cache"),
        user_dir=os.path.join(tmp, "user"), vfs_dir="/temp/beamngEarth/s1",
        out_dir=os.path.join(tmp, "user", "temp", "beamngEarth", "s1"), base_pending={}, base_stale=set(),
        base_lock=threading.Lock(), recent_full=[], hm_name=lambda key: "hm_%d_%g_%g.ter" % key,
        hm_stem=lambda key: "hm_%d_%g_%g" % key,
        _photo_data_cached=lambda anchor, x0, y0, size: True, base_ready=lambda anchor, cx, cy: True)
    for name in ("base_revisit_key", "_base_pack_path", "_base_revisited", "_base_keep", "base_name", "_base_kept"):
        setattr(srv, name, getattr(S, name).__get__(srv))
    os.makedirs(srv.out_dir)
    return srv


def painted(srv, anchor, cx, cy, rgb):
    """What write_base leaves: the photo file and its packed copy in the game's cache folder."""
    name = srv.base_name((anchor.id, cx, cy))
    with open(os.path.join(srv.out_dir, name), "wb") as f:
        f.write(terraincache.stored_photo(rgb))
    os.makedirs(terraincache.cache_dir(srv.user_dir), exist_ok=True)
    with open(srv._base_pack_path(name), "wb") as f:
        f.write(b"DDS packed")
    return name


class BasePhotoRevisitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(revisitpack.reset)
        self.on = revisit.ON
        revisit.ON = True
        self.addCleanup(setattr, revisit, "ON", self.on)

    def test_kept_and_taken_in_a_new_session(self):
        srv = fake_server(self.tmp.name)
        a1 = SimpleNamespace(id=1, lat=39.28, lon=-76.59, zooms=[17])
        rgb = np.zeros((64, 64, 3), np.uint8)
        rgb[:, :32] = (40, 160, 40)
        name = painted(srv, a1, 0.0, 384.0, rgb)
        bk = srv.base_revisit_key(a1, 0.0, 384.0)
        srv._base_kept(a1, 0.0, 384.0, ())
        srv._base_keep(a1, 0.0, 384.0, bk, name, (revisit.Build(),))
        # a fresh server process: another session folder, the same place as anchor 3
        srv2 = fake_server(os.path.join(self.tmp.name, "2"))
        srv2.cache_dir = srv.cache_dir
        a3 = SimpleNamespace(id=3, lat=39.28, lon=-76.59, zooms=[17])
        self.assertEqual(srv2.base_revisit_key(a3, 0.0, 384.0), bk)
        photo = []
        got = srv2._base_revisited(a3, 0.0, 384.0, bk, photo)
        self.assertEqual(got, srv2.base_name((3, 0.0, 384.0)))
        self.assertTrue(os.path.exists(os.path.join(srv2.out_dir, got)))
        with open(srv2._base_pack_path(got), "rb") as f:
            self.assertEqual(f.read(), b"DDS packed")
        self.assertEqual(photo[0].shape, (64, 64, 3))
        self.assertLess(abs(int(photo[0][10, 10, 1]) - 160), 8)        # (the JPEG's own: near enough)
        self.assertEqual(len(srv2.recent_full), 1)
        self.assertNotIn((3, 0.0, 384.0), srv2.base_stale)
        # another block, another place, another photo size: not the same item
        self.assertNotEqual(srv2.base_revisit_key(a3, 512.0, 384.0), bk)
        self.assertNotEqual(srv2.base_revisit_key(SimpleNamespace(id=3, lat=39.29, lon=-76.59, zooms=[17]), 0.0,
                                                  384.0), bk)
        srv2.base_px = 128
        self.assertNotEqual(srv2.base_revisit_key(a3, 0.0, 384.0), bk)

    def test_not_kept_unless_final(self):
        srv = fake_server(self.tmp.name)
        a1 = SimpleNamespace(id=1, lat=40.0, lon=-75.0, zooms=[17])
        name = painted(srv, a1, 0.0, 0.0, np.zeros((64, 64, 3), np.uint8))
        bk = srv.base_revisit_key(a1, 0.0, 0.0)
        srv.base_pending[(1, 0.0, 0.0)] = frozenset({"naip"})       # (a lazy source still to come)
        srv._base_keep(a1, 0.0, 0.0, bk, name, (revisit.Build(),))
        self.assertIsNone(revisit.load(srv.cache_dir, "basephoto", bk))
        srv.base_pending.clear()
        with revisit.scope() as f0:
            revisit.fault()                                         # (something it read failed meanwhile)
        srv._base_keep(a1, 0.0, 0.0, bk, name, (f0,))
        self.assertIsNone(revisit.load(srv.cache_dir, "basephoto", bk))
        srv._photo_data_cached = lambda *a: False                   # (a map cell under it not on disk)
        srv._base_keep(a1, 0.0, 0.0, bk, name, (revisit.Build(),))
        self.assertIsNone(revisit.load(srv.cache_dir, "basephoto", bk))
        self.assertIsNone(srv._base_revisited(a1, 0.0, 0.0, bk, []))

    def test_off(self):
        revisit.ON = False
        srv = fake_server(self.tmp.name)
        self.assertIsNone(srv.base_revisit_key(SimpleNamespace(id=1, lat=1.0, lon=2.0, zooms=[17]), 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
