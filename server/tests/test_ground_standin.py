"""Server._finish_heightmap_now with ground=True (a teleport's first block, l0roads.GROUND_ON): when the photo is not
done and no imagery stand-in can be made, the block goes at once with the ground stand-in (write_ground_base) instead
of waiting BASE_NO_STANDIN_MAX_S for the photo; the photo still follows (_base_later). Without ground, as before."""
import concurrent.futures
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import earthd  # noqa: E402


class GroundStandInTest(unittest.TestCase):
    def setUp(self):
        self.later = []
        self.ground_calls = []
        s = types.SimpleNamespace(fast=False, quick_futs={}, imagery=object(), speed=0.0,
                                  quick_pool=types.SimpleNamespace(submit=lambda *a: None))
        s._quick_once = lambda anchor, cx, cy, make: None          # (no imagery on disk: no stand-in)
        s._base_later = lambda key, f: self.later.append(key)
        s._quick_later = lambda *a: None

        def ground(anchor, cx, cy, info):
            self.ground_calls.append((cx, cy))
            return "hm_1_0_0_base_g7.jpg"
        s.write_ground_base = ground
        self.s = s
        self.anchor = types.SimpleNamespace(id=1, hm_zoom=16)

    def finish(self, ground, base):
        info = {"stages": {}, "ter": "x.ter", "q": 1 / 64}
        t0 = time.perf_counter()
        out = earthd.Server._finish_heightmap_now(self.s, self.anchor, 0.0, 0.0, info, t0, base, None, ground=ground)
        return out, time.perf_counter() - t0

    def test_ground_stand_in_instead_of_the_long_wait(self):
        old = earthd.BASE_WAIT_S, earthd.BASE_NO_STANDIN_MAX_S
        earthd.BASE_WAIT_S, earthd.BASE_NO_STANDIN_MAX_S = 0.05, 3.0
        try:
            base = concurrent.futures.Future()          # (the photo still downloading)
            out, dt = self.finish(True, base)
            self.assertEqual(out["base"], "hm_1_0_0_base_g7.jpg")
            self.assertLess(dt, 1.0, "not the no-stand-in wait")
            base.set_result(("photo.jpg", None, 1.0))   # (the photo done later: sent as block_base)
            self.assertEqual(self.later, [(1, 0.0, 0.0)], "its photo still follows")
            self.later.clear()
            base2 = concurrent.futures.Future()
            out, dt = self.finish(False, base2)
            self.assertIsNone(out["base"])
            self.assertGreater(dt, 2.5, "without ground: waited as before")
            self.assertEqual(len(self.ground_calls), 1)
        finally:
            earthd.BASE_WAIT_S, earthd.BASE_NO_STANDIN_MAX_S = old


class WriteGroundBaseTest(unittest.TestCase):
    def test_writes_a_new_photo_and_its_pack(self):
        """write_ground_base: the stand-in photo file under a new name each call, its packed copy in the game's terrain
        cache, remembered for removal with the provisional block (ground_bases)."""
        import itertools
        import tempfile
        import threading
        import numpy as np
        import surface as sf
        import terraincache
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            os.makedirs(out)
            n, q = 64, 1 / 64
            idx = np.full((n, n), sf.GRASS, np.uint8)
            name, _ = sf.write_ter(out, "b.ter", np.zeros((n, n), np.uint16), idx)
            s = types.SimpleNamespace(out_dir=out, user_dir=d, vfs_dir="/temp/beamngEarth/x", base_px=128,
                                      ter_serial=itertools.count(5), block_pools=[], base_lock=threading.Lock(),
                                      ground_bases={}, near_visual="detail")
            s.hm_name = lambda key: earthd.Server.hm_name(s, key)
            s.hm_stem = lambda key: earthd.Server.hm_stem(s, key)
            anchor = types.SimpleNamespace(id=1, lat=40.0, lon=-80.0)
            info = {"ter": name, "q": q, "surfaceComplete": True}
            a = earthd.Server.write_ground_base(s, anchor, 0.0, 0.0, info)
            b = earthd.Server.write_ground_base(s, anchor, 0.0, 0.0, info)
            self.assertTrue(a and b and a != b, "a new name each time (a file the game read is never rewritten)")
            self.assertTrue(os.path.exists(os.path.join(out, a)))
            self.assertIn("_base", a, "named so a session's end clears its pack (_base_cache_files)")
            self.assertEqual(s.ground_bases[(1, 0.0, 0.0)], [a, b])
            if not terraincache.NO_PACK:
                self.assertTrue(os.listdir(terraincache.cache_dir(d)), "packed for the game")


if __name__ == "__main__":
    unittest.main()
