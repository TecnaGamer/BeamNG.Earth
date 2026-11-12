import gc
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
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
        if url == demmod.COVERAGE_URL:      # (no file list here: every tile asked)
            raise netfetch.HostUnavailable("no coverage list in this test")
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
            self.assertTrue(demmod.MIRROR_ZSTD or not (b.flags.owndata or b.flags.writeable), "the mirror's mapping")
            self.assertEqual(other.served[key], demmod.MAPTERHORN["name"])
            k2 = (15, 9301, 12500)
            down = demmod.Dem(d, fetcher=TerrariumFetcher(mapterhorn_fails=True))
            down._load_tile(k2)
            self.assertEqual(down.served[k2], demmod.AWS["name"])
            self.assertFalse(Path(down._mirror_path(k2)).exists(), "a failover tile is not kept")
            del a, b, other, down
            gc.collect()  # the mirror's mappings closed: Windows deletes no mapped file


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


class CoverageTest(unittest.TestCase):
    """Mapterhorn's file list (download_urls.json): a tile deeper than it has under a z6 tile is not asked for."""

    DOC = {"items": [{"name": "planet.pmtiles", "min_zoom": 0, "max_zoom": 12},
                     {"name": "6-10-22.pmtiles", "min_zoom": 13, "max_zoom": 16},
                     {"name": "6-11-22.pmtiles", "min_zoom": 13, "max_zoom": 13}]}

    def test_index(self):
        self.assertEqual(demmod.coverage_index(self.DOC), {(10, 22): 16, (11, 22): 13})

    def test_unlisted_tiles_are_not_asked(self):
        import json

        class Fetcher(LidarDepthFetcher):
            def get(self, url, *, kind="demand", headers=None):
                if url == demmod.COVERAGE_URL:
                    self.calls.append(url)

                    class R:
                        body = json.dumps(CoverageTest.DOC).encode()
                    return R()
                return super().get(url, kind=kind, headers=headers)
        saved = dict(demmod._coverage)
        demmod._coverage.update(index=None, at=-1e18, failed=False)
        try:
            with tempfile.TemporaryDirectory() as d:
                f = Fetcher()
                dem = demmod.Dem(d, fetcher=f)
                self.assertIsNone(dem._download(demmod.MAPTERHORN, 16, (12 << 10) + 5, 22 << 10), "unlisted z6 tile")
                self.assertIsNone(dem._download(demmod.MAPTERHORN, 14, (11 << 8) + 5, 22 << 8), "deeper than its file")
                self.assertEqual(f.calls, [demmod.COVERAGE_URL], "neither asked; the list read once")
                self.assertIsNotNone(dem._download(demmod.MAPTERHORN, 14, (10 << 8) + 5, 22 << 8))
                self.assertIsNotNone(dem._download(demmod.MAPTERHORN, 12, 12 << 6, 22 << 6), "the planet: every z12")
                self.assertEqual(len(f.calls), 3)
                self.assertTrue(os.path.exists(os.path.join(d, "mapterhorn", "download_urls.json")), "kept on disk")
                # routeahead's progress: a tile Mapterhorn does not list never lands on disk, and counts as done
                self.assertTrue(dem.has(16, (12 << 10) + 5, 22 << 10), "unlisted: nothing to download")
                self.assertFalse(dem.has(14, (10 << 8) + 6, 22 << 8), "listed, not fetched yet")
        finally:
            demmod._coverage.clear()
            demmod._coverage.update(saved)


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

    def test_has_is_a_cheap_exists_check(self):
        with tempfile.TemporaryDirectory() as d:
            dem = demmod.Dem(d, fetcher=FakeFetcher())
            self.assertFalse(dem.has(15, 100, 200))
            dem.prefetch([(15, 100, 200)])
            self.assertTrue(dem.has(15, 100, 200))
            miss = os.path.join(d, "mapterhorn", "15", "101", "200.404")
            os.makedirs(os.path.dirname(miss), exist_ok=True)
            open(miss, "w").close()
            self.assertTrue(dem.has(15, 101, 200), "a remembered 404: nothing to download")

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



class MirrorClaimTest(unittest.TestCase):
    """One process makes and writes a mirrored tile (its claim); another asking meanwhile waits for it and maps it, or
    after CLAIM_WAIT_S makes it without writing; a stale claim (a process that ended) is taken over."""

    def test_waits_for_the_claimer(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            key = (15, 9400, 12500)
            maker, waiter = demmod.Dem(d, fetcher=TerrariumFetcher()), demmod.Dem(d, fetcher=TerrariumFetcher())
            self.assertTrue(maker._mirror_claim(key))                 # (the other process is making it)
            made = maker._make_tile(key)

            def finish():
                maker._mirror_put(key, made)
                os.remove(maker._mirror_path(key) + ".claim")
            threading.Timer(0.05, finish).start()
            got = []
            orig = demmod.decode_terrarium
            demmod.decode_terrarium = lambda *a_: got.append(1) or orig(*a_)
            try:
                b = waiter._load_tile(key)
            finally:
                demmod.decode_terrarium = orig
            self.assertEqual(got, [], "not decoded again: waited for the claimer's")
            self.assertEqual(waiter.stats["dem:mirror_waited"], 1)
            self.assertTrue(demmod.MIRROR_ZSTD or not (b.flags.owndata or b.flags.writeable), "the mirror's mapping")
            del b
            gc.collect()

    def test_claimer_too_slow_makes_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            key = (15, 9401, 12500)
            other = demmod.Dem(d, fetcher=TerrariumFetcher())
            self.assertTrue(other._mirror_claim(key))                 # never finishes
            old = demmod.CLAIM_WAIT_S
            demmod.CLAIM_WAIT_S = 0.05
            try:
                me = demmod.Dem(d, fetcher=TerrariumFetcher())
                a = me._load_tile(key)
            finally:
                demmod.CLAIM_WAIT_S = old
            self.assertEqual(float(a[0, 0]), 100 + 9401 % 1000)
            self.assertFalse(Path(me._mirror_path(key)).exists(), "the claimer writes it, not this one")
            self.assertEqual(me.stats["dem:mirror_put"], 0)

    def test_stale_claim_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            key = (15, 9402, 12500)
            dem = demmod.Dem(d, fetcher=TerrariumFetcher())
            claim = dem._mirror_path(key) + ".claim"
            os.makedirs(os.path.dirname(claim), exist_ok=True)
            open(claim, "w").close()
            old = time.time() - demmod.CLAIM_STALE_S - 5
            os.utime(claim, (old, old))
            a = dem._load_tile(key)
            self.assertTrue(Path(dem._mirror_path(key)).exists())
            self.assertFalse(Path(claim).exists())
            del a
            gc.collect()

class NoWriteTest(unittest.TestCase):
    """A cache that cannot be written (a full disk): the downloaded tile is kept in memory and used, not failed and
    downloaded again at its next try; a 404 that cannot be recorded is remembered in memory too."""

    def test_full_disk(self):
        import netfetch

        class F(TerrariumFetcher):
            def get(self, url, *, kind="demand", headers=None):
                if "/9/" in url:
                    self.calls.append(url)
                    raise netfetch.NotFound(url)
                return super().get(url, kind=kind, headers=headers)
        saved = dict(demmod._coverage)
        demmod._coverage.update(index=None, at=time.time(), failed=True)         # (no file list: every tile asked)
        try:
            with tempfile.TemporaryDirectory() as d:
                f = F()
                dem = demmod.Dem(d, fetcher=f)
                real_open = open

                def full(path, mode="r", *a, **k):
                    if "w" in mode and str(path).endswith(".tmp"):
                        raise OSError(28, "No space left on device")
                    return real_open(path, mode, *a, **k)
                with mock.patch("builtins.open", full), \
                        mock.patch.object(demmod.missindex, "_append", side_effect=OSError(28, "full")):
                    a = dem._download(demmod.MAPTERHORN, 12, 700, 1500)
                    b = dem._download(demmod.MAPTERHORN, 12, 700, 1500)
                    self.assertIsNone(dem._download(demmod.MAPTERHORN, 9, 87, 187))
                    self.assertIsNone(dem._download(demmod.MAPTERHORN, 9, 87, 187))
                self.assertIs(a, b)
                self.assertEqual(len(f.calls), 2, f.calls)
                self.assertEqual(dem.stats["dem:nowrite"], 1)
                self.assertEqual(dem.stats["mapterhorn:memory"], 1)
                self.assertFalse(os.path.exists(dem._path(demmod.MAPTERHORN, 12, 700, 1500)))
        finally:
            demmod._coverage.clear()
            demmod._coverage.update(saved)


class PackFetcher(TerrariumFetcher):
    """The tile host's Mapterhorn packs (dem.MT_CDN_*): an index listing the packs in `packs`, each pack holding the
    tiles TerrariumFetcher would give for the endpoint except those in `holes`; the endpoint itself as
    TerrariumFetcher."""

    def __init__(self, packs, holes=(), fail=False):
        super().__init__()
        self.packs, self.holes, self.fail = set(packs), set(holes), fail

    def miss_alive(self, path, url):
        import netfetch
        return netfetch.miss_alive(path, None)

    def get(self, url, *, kind="demand", headers=None):
        import netfetch
        import tilepack
        if "/dem/mapterhorn/" not in url:
            return super().get(url, kind=kind, headers=headers)
        self.calls.append(url)
        if self.fail:
            raise netfetch.HostUnavailable("down")
        if url.endswith("/index.bin"):
            body = tilepack.index_build(3, 12, self.packs)
        else:
            z, px, py = (int(v) for v in url[:-len(".bngp")].rsplit("/", 3)[1:])
            if (z, px, py) not in self.packs:
                raise netfetch.NotFound(url)
            k = min(3, z)
            tiles = {}
            for i in range((1 << k) ** 2):
                tz, tx, ty = tilepack.tile_at(z, px, py, k, i)
                if (tz, tx, ty) not in self.holes:
                    tiles[i] = TerrariumFetcher.get(self, f"https://tiles.mapterhorn.com/{tz}/{tx}/{ty}.webp").body
                    self.calls.pop()
            body = tilepack.build(z, px, py, k, tiles)

        class R:
            pass
        R.body = body
        return R()


class CdnPackTest(unittest.TestCase):
    """Mapterhorn's z0-12 from the tile host's packs: one request for 64 tiles, every one written where the endpoint's
    would be (the same bytes); a tile the pack lacks and every tile of a pack the index does not list are 404s without
    asking the endpoint; z13+ and a failing host go to the endpoint."""

    def setUp(self):
        self.saved = dict(demmod._coverage)
        demmod._coverage.update(index=None, at=time.time(), failed=True)

    def tearDown(self):
        demmod._coverage.clear()
        demmod._coverage.update(self.saved)

    def test_packs(self):
        with tempfile.TemporaryDirectory() as d:
            f = PackFetcher(packs={(12, 87, 187), (2, 0, 0)}, holes={(12, 701, 1501)})
            dem = demmod.Dem(d, fetcher=f)
            dem.cdn_release = "0.0.13"
            a = dem._download(demmod.MAPTERHORN, 12, 700, 1500)
            self.assertEqual(f.calls, [demmod.mt_cdn_base("0.0.13") + "/index.bin",
                                       demmod.mt_cdn_base("0.0.13") + "/12/87/187.bngp"])
            ref = TerrariumFetcher().get("https://tiles.mapterhorn.com/12/700/1500.webp").body
            self.assertEqual(a, ref, "the endpoint's bytes")
            n = len(f.calls)
            self.assertEqual(dem._download(demmod.MAPTERHORN, 12, 703, 1499), TerrariumFetcher().get(
                "https://tiles.mapterhorn.com/12/703/1499.webp").body, "a tile of the same pack: from disk")
            self.assertIsNone(dem._download(demmod.MAPTERHORN, 12, 701, 1501), "not in its pack: a 404")
            self.assertTrue(demmod.missindex.known(os.path.join(d, "mapterhorn", "12", "701", "1501.404")))
            self.assertIsNone(dem._download(demmod.MAPTERHORN, 12, 0, 0), "a pack the index does not list")
            self.assertTrue(demmod.missindex.known(os.path.join(d, "mapterhorn", "12", "7", "7.404")), "all 64")
            self.assertEqual(len(f.calls), n, "none of those asked anything")
            self.assertIsNotNone(dem._download(demmod.MAPTERHORN, 2, 1, 3))
            self.assertIsNotNone(dem._download(demmod.MAPTERHORN, 13, 1400, 3000))
            self.assertEqual(f.calls[n:], [demmod.mt_cdn_base("0.0.13") + "/2/0/0.bngp",
                                           "https://tiles.mapterhorn.com/13/1400/3000.webp"])
            # another process: the index read from disk, nothing asked
            f2 = PackFetcher(packs={(12, 87, 187)})
            dem2 = demmod.Dem(d, fetcher=f2)
            dem2.cdn_release = "0.0.13"
            self.assertIsNone(dem2._download(demmod.MAPTERHORN, 12, 8, 8))
            self.assertIsNotNone(dem2._download(demmod.MAPTERHORN, 12, 702, 1502))
            self.assertEqual(f2.calls, [])

    def test_host_down_falls_back_to_the_endpoint(self):
        with tempfile.TemporaryDirectory() as d:
            f = PackFetcher(packs={(12, 87, 187)}, fail=True)
            dem = demmod.Dem(d, fetcher=f)
            dem.cdn_release = "0.0.13"
            self.assertIsNotNone(dem._download(demmod.MAPTERHORN, 12, 700, 1500))
            self.assertIsNotNone(dem._download(demmod.MAPTERHORN, 12, 700, 1501))
            self.assertEqual(f.calls, [demmod.mt_cdn_base("0.0.13") + "/index.bin",
                                       "https://tiles.mapterhorn.com/12/700/1500.webp",
                                       "https://tiles.mapterhorn.com/12/700/1501.webp"],
                             "the index asked once, then the endpoint for MT_CDN_RETRY_S")

    def test_another_process_fetching_the_pack(self):
        """A pack another process is fetching (its claim): this one waits for the tile on disk instead of asking."""
        with tempfile.TemporaryDirectory() as d:
            f = PackFetcher(packs={(12, 87, 187)})
            dem = demmod.Dem(d, fetcher=f)
            dem.cdn_release = "0.0.13"
            dem._cdn_index()
            n = len(f.calls)
            claim = os.path.join(d, "bngmt", "0.0.13", "claims", "12_87_187")
            os.makedirs(os.path.dirname(claim))
            open(claim, "w").close()
            path = dem._path(demmod.MAPTERHORN, 12, 700, 1500)

            def other():
                time.sleep(0.2)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(b"theirs")
            t = threading.Thread(target=other)
            t.start()
            self.assertEqual(dem._download(demmod.MAPTERHORN, 12, 700, 1500), b"theirs")
            t.join()
            self.assertEqual(len(f.calls), n, "nothing asked")
            self.assertEqual(dem.stats["bngmt:waited"], 1)
            os.utime(claim, (time.time() - 60, time.time() - 60))      # a stale claim: fetched here
            self.assertIsNotNone(dem._download(demmod.MAPTERHORN, 12, 701, 1500))
            self.assertEqual(len(f.calls), n + 1)
            self.assertFalse(os.path.exists(claim), "its claim removed after the fetch")


if __name__ == "__main__":
    unittest.main()
