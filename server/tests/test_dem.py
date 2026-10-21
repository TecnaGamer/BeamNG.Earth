import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem as demmod  # noqa: E402


class FlakyDem(demmod.Dem):
    """No network: the first load of every tile fails, later loads succeed."""

    def __init__(self, cache_dir):
        super().__init__(cache_dir, fetch_workers=4)
        self.calls = {}
        self.calls_lock = threading.Lock()

    def _load_tile(self, key):
        with self.calls_lock:
            n = self.calls[key] = self.calls.get(key, 0) + 1
        if n == 1:
            raise IOError(f"simulated fetch failure {key}")
        return np.full((demmod.TILE, demmod.TILE), key[1], np.float32)


class DemCacheTest(unittest.TestCase):
    def test_failed_fetch_is_retried(self):
        with tempfile.TemporaryDirectory() as d:
            dem = FlakyDem(d)
            keys = [(5, x, 3) for x in range(6)]
            with self.assertRaises(IOError):
                dem.tiles(keys)
            dem.pool.shutdown(wait=True)        # let the other failing loads finish
            dem.pool = demmod.ThreadPoolExecutor(4)
            # A failure must not be cached: the retry loads every tile again.
            got = dem.tiles(keys)
            self.assertEqual(sorted(got), keys)
            self.assertEqual(dem._inflight, {})
            self.assertTrue(all(dem.calls[k] == 2 for k in keys), dem.calls)
            self.assertTrue(all(float(got[k][0, 0]) == k[1] for k in keys))
            # Third call is served from memory.
            dem.tiles(keys)
            self.assertTrue(all(dem.calls[k] == 2 for k in keys))


class FakeFetcher:
    """netfetch.Fetcher stand-in: records (url, kind); refuses prefetches after `allow` of them."""

    def __init__(self, allow=1000):
        self.calls, self.allow = [], allow

    def get(self, url, *, kind="demand", headers=None):
        import netfetch
        if kind == "prefetch" and sum(1 for _, k in self.calls if k == "prefetch") >= self.allow:
            raise netfetch.Deferred("no spare tokens")
        self.calls.append((url, kind))

        class R:
            body = b"png"
        return R()

    def miss_alive(self, path, url):
        return False

    def register_chain(self, name, urls):
        pass


class TerrariumFetcher:
    """Terrarium PNG tiles (height = 100 + x, the whole tile), Mapterhorn 512 px, AWS 256 px; Mapterhorn can be
    made to fail."""

    def __init__(self, mapterhorn_fails=False):
        self.calls, self.mapterhorn_fails = [], mapterhorn_fails

    def get(self, url, *, kind="demand", headers=None):
        import io
        import netfetch
        from PIL import Image
        self.calls.append(url)
        if "mapterhorn" in url and self.mapterhorn_fails:
            raise netfetch.HostUnavailable("down")
        size = 512 if "mapterhorn" in url else 256
        x = int(url.rsplit("/", 2)[1])
        v = (100 + x % 1000) + 32768
        a = np.zeros((size, size, 3), np.uint8)
        a[..., 0], a[..., 1] = v // 256, v % 256
        buf = io.BytesIO()
        Image.fromarray(a).save(buf, "PNG")

        class R:
            body = buf.getvalue()
        return R()

    def miss_alive(self, path, url):
        return False

    def register_chain(self, name, urls):
        pass


class MirrorTest(unittest.TestCase):
    """The decoded mirror: a tile decoded once is read by another process memory-mapped, the same numbers; a tile
    made from a stand-in (Mapterhorn failing: AWS) is not kept."""

    def test_mirror(self):
        with tempfile.TemporaryDirectory() as d:
            f = TerrariumFetcher()
            key = (15, 9300, 12500)
            a = demmod.Dem(d, fetcher=f)._load_tile(key)
            self.assertEqual(float(a[0, 0]), 100 + 9300 % 1000)
            n = len(f.calls)
            other = demmod.Dem(d, fetcher=f)
            got = []
            orig = demmod.decode_terrarium
            demmod.decode_terrarium = lambda *a_: got.append(1) or orig(*a_)
            try:
                b = other._load_tile(key)
            finally:
                demmod.decode_terrarium = orig
            self.assertEqual((len(f.calls), got), (n, []), "neither fetched nor decoded")
            np.testing.assert_array_equal(a, b)
            self.assertFalse(b.flags.owndata or b.flags.writeable, "the mirror's mapping")
            self.assertEqual(other.served[key], demmod.MAPTERHORN["name"])
            k2 = (15, 9301, 12500)
            down = demmod.Dem(d, fetcher=TerrariumFetcher(mapterhorn_fails=True))
            down._load_tile(k2)
            self.assertEqual(down.served[k2], demmod.AWS["name"])
            self.assertFalse(Path(down._mirror_path(k2)).exists(), "a failover tile is not kept")


class LidarDepthFetcher(TerrariumFetcher):
    """Mapterhorn with lidar to z14 (height = 100 + zoom, the whole tile) and nothing at z15 (404)."""

    def get(self, url, *, kind="demand", headers=None):
        import io
        import netfetch
        from PIL import Image
        self.calls.append(url)
        z = int(url.rsplit("/", 3)[1])
        if "mapterhorn" in url and z > 14:
            raise netfetch.NotFound("no lidar this deep")
        size = 512 if "mapterhorn" in url else 256
        v = 100 + z + 32768
        a = np.zeros((size, size, 3), np.uint8)
        a[..., 0], a[..., 1] = v // 256, v % 256
        buf = io.BytesIO()
        Image.fromarray(a).save(buf, "PNG")

        class R:
            body = buf.getvalue()
        return R()


class LidarDepthTest(unittest.TestCase):
    """A tile deeper than the lidar goes is its deepest lidar tile upsampled, not the z12 ground (Rotterdam: AHN to
    z14, z15 404; z12 gave 19 m steps on city roads)."""

    def test_from_deepest_lidar(self):
        saved, demmod.MIRROR_MB = demmod.MIRROR_MB, 0      # no memory-mapped files (Windows cannot delete them)
        try:
            with tempfile.TemporaryDirectory() as d:
                a = demmod.Dem(d, fetcher=LidarDepthFetcher())._load_tile((15, 16790, 10839))
                self.assertAlmostEqual(float(a.mean()), 114.0, places=3, msg="the z14 ancestor's heights, not z12's")
        finally:
            demmod.MIRROR_MB = saved


class FillHolesTest(unittest.TestCase):
    def test_small_hole_filled_sea_kept(self):
        a = np.full((64, 64), -1.5, np.float32)
        a[10:14, 10:14] = 0.0                   # a building's footprint without lidar ground
        a[:, 40:] = 0.0                         # the sea
        out = demmod.fill_holes(a, k=4)
        self.assertTrue(np.allclose(out[10:14, 10:14], -1.5), "the hole takes its surroundings' height")
        self.assertTrue((out[:, 40:] == 0).all(), "the sea stays at 0, its rim too")
        self.assertTrue((out[:, :40][a[:, :40] != 0] == -1.5).all(), "the land is untouched")

    def test_rounds_by_zoom(self):
        self.assertEqual(demmod.hole_px(14, 5419), demmod.HOLE_FILL_MAX_PX)     # Rotterdam z14: capped
        self.assertTrue(2 <= demmod.hole_px(9, 169) < demmod.HOLE_FILL_MAX_PX)  # z9: a few pixels

    def test_nothing_to_fill(self):
        a = np.full((8, 8), 3.0, np.float32)
        np.testing.assert_array_equal(demmod.fill_holes(a), a)
        z = np.zeros((8, 8), np.float32)
        np.testing.assert_array_equal(demmod.fill_holes(z), z)


class DemPrefetchTest(unittest.TestCase):
    def test_downloads_missing_tiles_at_prefetch_priority(self):
        with tempfile.TemporaryDirectory() as d:
            f = FakeFetcher()
            dem = demmod.Dem(d, fetcher=f)
            keys = [(15, 100, 200), (15, 101, 200)]
            self.assertEqual(dem.prefetch(keys), (2, False))
            self.assertTrue(all(k == "prefetch" for _, k in f.calls))
            self.assertEqual(dem.prefetch(keys), (0, False))      # on disk now: nothing asked again
            self.assertEqual(len(f.calls), 2)

    def test_stops_when_refused(self):
        with tempfile.TemporaryDirectory() as d:
            f = FakeFetcher(allow=1)
            dem = demmod.Dem(d, fetcher=f)
            self.assertEqual(dem.prefetch([(15, 1, 1), (15, 2, 1), (15, 3, 1)]), (1, True))

    def test_keys_around(self):
        with tempfile.TemporaryDirectory() as d:
            dem = demmod.Dem(d, fetcher=FakeFetcher())
            keys = dem.keys_around(36.17, -115.145, 500.0, 15)
            z15 = [k for k in keys if k[0] == 15]
            self.assertTrue(2 <= len(z15) <= 9, z15)
            import mapcells
            cx, cy = mapcells.tile_xy(36.17, -115.145, 15)
            self.assertEqual(z15[0][1:], (int(cx), int(cy)), "nearest first")
            self.assertEqual(keys[0][0], demmod.GLOBAL_ZOOM, "z12 ground parents first (ground filter)")


if __name__ == "__main__":
    unittest.main()
