"""tools/build_s2_packs.py, the Sentinel-2 tile packs of the project's tile host (imagery source "bngs2"), offline on
synthetic 1-degree files: the window sampler (zoom 13+) gives the per-point sampler's colours away from a degree's
edge (and none of its seam there), a pack's tiles come out as WebP the server's decoder reads, and a region run skips
packs built already and stops at its deadline (the resumable workflow); z14's latitude limit."""
import io
import math
import os
import shutil
import sys
import tempfile
import time
import unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))
sys.path.insert(0, os.path.join(HERE, ".."))

import build_s2_packs as b  # noqa: E402
import tilepack  # noqa: E402

LVL = 3                         # overview / 8: 1500 px a degree, 1024 px blocks (2 x 2 a file)


class FakeCog:
    def __init__(self, la, lo):
        self.name = f"fake_{la}_{lo}"
        w = b.PPD
        self.levels = [{"w": w >> i, "tw": 1024} for i in range(5)]
        self.la, self.lo = la, lo


def fake_block(c, lvl, bx, by):
    """A smooth reflectance field of global position (no data in the file of 10 N, 10 E: the sea)."""
    if (c.la, c.lo) == (10, 10):
        return None
    ppd = b.PPD >> lvl
    t = 1024
    r = (89 - c.la) * ppd + by * t + np.arange(t)[:, None]
    q = (c.lo + 180) * ppd + bx * t + np.arange(t)[None, :]
    lat, lon = np.broadcast_arrays(90 - (r + 0.5) / ppd, (q + 0.5) / ppd - 180)
    out = np.stack([1000 + 400 * np.sin(lat * 7.0), 1200 + 300 * np.cos(lon * 5.0),
                    800 + 100 * np.sin((lat + lon) * 3.0)], -1)
    return out.astype(np.uint16)


class BuilderTest(unittest.TestCase):
    def setUp(self):
        self._cog, self._block = b.cog_for, b.block
        b.cog_for = lambda la, lo: FakeCog(la, lo) if -60 <= la < 60 else None
        b.block = fake_block
        self.addCleanup(self._restore)
        self.out = tempfile.mkdtemp(prefix="s2packs_")
        self.addCleanup(shutil.rmtree, self.out, True)

    def _restore(self):
        b.cog_for, b.block = self._cog, self._block

    def tile(self, z, lat, lon):
        n = 1 << z
        x = int((lon + 180) / 360 * n)
        y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        return b.tile_latlon(z, x, y)

    def test_window_sampler_matches_the_point_sampler(self):
        for z, lat, lon in ((13, 20.5, 30.5), (12, -33.4, 151.2), (13, 12.0, 12.0)):    # (the last on a corner)
            la, lo = self.tile(z, lat, lon)
            v1, ok1 = b.sample(la.ravel(), lo.ravel(), LVL)
            v2, ok2 = b.window_sample(la[:, 0], lo[0], LVL)
            v1, ok1 = v1.reshape(256, 256, 3), ok1.reshape(256, 256)
            self.assertTrue(ok2.all() and ok1.all())
            # away from a degree's edge the same; on one the window reads across it, the point sampler clamps
            edge = (np.abs(la - np.round(la)) < 2 / (b.PPD >> LVL)) | (np.abs(lo - np.round(lo)) < 2 / (b.PPD >> LVL))
            self.assertLess(np.abs(v1 - v2)[~edge].max(), 0.05)

    def test_sea_file_is_nodata(self):
        la, lo = self.tile(13, 10.5, 10.5)          # inside the file without data
        _, ok = b.window_sample(la[:, 0], lo[0], LVL)
        self.assertFalse(ok.any())

    def test_pack_as_webp(self):
        z = 13
        (pz, px, py), _ = tilepack.pack_of(z, int((30.5 + 180) / 360 * 8192), 3500, 3)
        data = b.build_pack(pz, px, py, fmt="webp", q=80)
        _, _, _, k, tiles = tilepack.parse(data)
        self.assertEqual(len(tiles), 64)
        for t in tiles.values():
            self.assertEqual(t[:4], b"RIFF")
            im = Image.open(io.BytesIO(t))
            self.assertEqual((im.format, im.size), ("WEBP", (256, 256)))
            self.assertGreater(np.asarray(im.convert("RGB")).mean(), 20)

    def test_run_skips_built_packs_and_stops_at_deadline(self):
        todo = [(13, 600 + i, 400) for i in range(4)]
        skip = {tilepack.path(*todo[0])}
        self.assertTrue(b.run(self.out, todo[:2], 2, fmt="webp", q=80, skip=skip))
        self.assertFalse(os.path.exists(os.path.join(self.out, tilepack.path(*todo[0]))))
        self.assertTrue(os.path.exists(os.path.join(self.out, tilepack.path(*todo[1]))))
        self.assertFalse(b.run(self.out, todo, 2, fmt="webp", q=80, skip=skip, deadline=time.time() - 1))
        self.assertFalse(os.path.exists(os.path.join(self.out, tilepack.path(*todo[2]))))

    def test_region_and_latitude_limit(self):
        packs = b.region_packs(13, 16, 10)
        self.assertEqual(len(packs), 1024)
        self.assertEqual(packs[0], (13, 512, 320))
        p14 = b.region_packs(14, 16, 10)
        kept = b.within_lat(p14, 50.0)
        self.assertTrue(0 < len(kept) < len(p14))
        for p in kept:
            self.assertLess(b.pack_lat_range(*p)[0], 50.0)
        s, n = b.pack_lat_range(13, 512, 320)
        self.assertTrue(-90 < s < n < 90)


if __name__ == "__main__":
    unittest.main()
