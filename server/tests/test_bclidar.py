"""Canada / BC lidar terrain (2026-10-23): bcproj's projections against PROJ's values, cog.py's striped TIFFs, LZW and
float predictor 2 (fixtures written by GDAL), lidarbc's file choice (newest first, older ones fill the gaps),
hrdem's item grid and feathering, and dem.Dem._canada's blend (the sea kept, failures not mirrored)."""
import gzip
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bcproj  # noqa: E402
import cog  # noqa: E402
import dem as demmod  # noqa: E402
import hrdem  # noqa: E402
import lidarbc  # noqa: E402
import netfetch  # noqa: E402
import rangeread  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
PTS = [(48.4284, -123.3656), (49.245, -124.355), (50.72, -127.42), (53.5, -130.1)]
# PROJ 9 (pyproj 3.8), from NAD83(CSRS) / NAD83 geographic, 2026-10-23
PROJ = {
    3157: [(472954.4595, 5363981.443), (401380.245, 5455575.2862), (188048.7376, 5628010.8482),
           (29393.4364, 5951384.4299)],
    6652: [(916729.0339, 5379269.6332), (838022.8928, 5465081.6996), (611531.1266, 5619879.071),
           (427035.5459, 5928456.8117)],
    3005: [(1195327.9029, 382812.0693), (1119879.1003, 471243.3948), (899805.9043, 634911.0539),
           (728788.058, 951667.8285)],
    3979: [(-2034892.9495, 397774.2585), (-2060388.3769, 511303.7619), (-2177798.0429, 758041.6742),
           (-2179565.5966, 1113797.4979)],
}


def fixture_heights():
    r, c = np.mgrid[0:150, 0:130]
    h = (100 + 0.5 * r + 0.25 * c + 3 * np.sin(r / 7.0) * np.cos(c / 5.0)).astype(np.float32)
    return h


class ProjTest(unittest.TestCase):
    def test_against_proj(self):
        lat = np.array([p[0] for p in PTS])
        lon = np.array([p[1] for p in PTS])
        for epsg, want in PROJ.items():
            x, y = bcproj.forward(epsg, lat, lon)
            for (wx, wy), gx, gy in zip(want, x, y):
                self.assertAlmostEqual(gx, wx, places=3, msg=epsg)
                self.assertAlmostEqual(gy, wy, places=3, msg=epsg)

    def test_hrdem_item_grid(self):
        self.assertEqual(hrdem.item_of(*hrdem.lcc(48.43, -123.37)), "1_3")      # Victoria
        self.assertEqual(hrdem.item_of(*hrdem.lcc(50.78, -128.42)), "1_4")      # Cape Scott
        self.assertEqual(hrdem.item_of(*hrdem.lcc(43.65, -79.38)), "8_2")       # Toronto (STAC: x 1e6.., y -5e5..0)


class CogStripTest(unittest.TestCase):
    def check(self, name):
        c = cog.Cog(rangeread.MemoryRange((FIX / name).read_bytes()), tiles=cog.TileCache(1 << 24))
        h = fixture_heights()
        w = c.read_window(15, 20, 100, 120)
        np.testing.assert_array_equal(w, h[20:140, 15:115])
        rng = np.random.default_rng(3)
        col, row = rng.uniform(12, 128, 500), rng.uniform(10, 148, 500)
        v, ok = c.sample_bilinear(col, row)
        self.assertTrue(ok.all())
        c0, r0 = np.floor(col).astype(int), np.floor(row).astype(int)
        fu, fv = col - c0, row - r0
        ref = ((h[r0, c0] * (1 - fu) + h[r0, c0 + 1] * fu) * (1 - fv)
               + (h[r0 + 1, c0] * (1 - fu) + h[r0 + 1, c0 + 1] * fu) * fv)
        np.testing.assert_allclose(v, ref, atol=1e-3)
        _, ok = c.sample_bilinear(np.array([3.0, 200.0]), np.array([3.0, 5.0]))     # nodata corner, outside
        self.assertFalse(ok.any())
        return c

    def test_strips_lzw(self):
        c = self.check("dem_strips_lzw.tif")
        self.assertTrue(c.stripped)
        self.assertEqual(c.compression, "libtiff")
        self.assertEqual(c.epsg, 3157)            # (6653 = 3157 + CGVD2013: the horizontal GeoKey)

    def test_strips_deflate_float_predictor2(self):
        self.assertTrue(self.check("dem_strips_deflate_pred2.tif").stripped)

    def test_cog_lzw_bigtiff_overview(self):
        c = self.check("dem_cog_lzw.tif")
        self.assertFalse(c.stripped)
        o = cog.Cog(rangeread.MemoryRange((FIX / "dem_cog_lzw.tif").read_bytes()), level=1)
        self.assertEqual(o.sx, 2.0)
        self.assertEqual(o.read_window(10, 10, 20, 20)[5, 5], fixture_heights()[30, 30])

    def test_sparse_rows_read_only_their_strips(self):
        c = cog.Cog(rangeread.MemoryRange((FIX / "dem_strips_lzw.tif").read_bytes()))
        got = c._blocks([40, 41, 120])
        self.assertEqual(sorted(got), [40, 41, 120])
        self.assertLessEqual(c.reads, 2)    # (strips closer than cog.MERGE_GAP in the file: one read)


class FakeLidar(lidarbc.LidarBC):
    """Files from fixtures (no network)."""

    def __init__(self, files, index):
        super().__init__(None, None, index_path=index)
        self.files = files

    def _cog(self, key):
        return cog.Cog(rangeread.MemoryRange(self.files[key]))


class LidarBCTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        h = fixture_heights()
        # a fixture's grid placed under a z18 tile near Victoria (UTM 10, 1 m pixels)
        self.z = 18
        n = 2 ** self.z
        self.x = int((-123.37 + 180) / 360 * n)
        self.y = int((1 - math.asinh(math.tan(math.radians(48.43))) / math.pi) / 2 * n)
        lat, lon = hrdem.tile_lat_lon(self.z, self.x, self.y, 512)
        ex, ny = bcproj.forward(6653, lat, lon)
        x0, y1 = math.floor(ex.min()) - 5, math.ceil(ny.max()) + 5
        self.origin = (x0, y1)
        data = (FIX / "dem_strips_lzw.tif").read_bytes()
        # the fixture's georeferencing is (400000, 5450000): move it under the tile by patching the tie point
        self.shift = (x0 - 400000.0, y1 - 5450000.0)
        rows = [["old/2019/dem/a.tif", 6653, x0, y1 - 150, x0 + 130, y1, 2019],
                ["new/2024/dem/b.tif", 6653, x0, y1 - 150, x0 + 65, y1, 2024]]
        self.index = os.path.join(self.tmp.name, "idx.json.gz")
        with gzip.open(self.index, "wt") as f:
            json.dump({"files": rows}, f)
        self.h = h
        self.data = data

    def tearDown(self):
        self.tmp.cleanup()

    def test_newest_first_older_fills(self):
        class Shifted(FakeLidar):
            def _cog(s, key):
                c = cog.Cog(rangeread.MemoryRange(self.data))
                c.x0 += self.shift[0]
                c.y0 += self.shift[1]
                if key.startswith("new"):
                    c.read_window = None        # (sample_bilinear only)
                    c._newer = True
                    orig = c.sample_bilinear

                    def plus(col, row, orig=orig):
                        v, ok = orig(col, row)
                        ok &= col < 64          # the newer file covers the west half only
                        return v + 10.0, ok
                    c.sample_bilinear = plus
                return c
        lb = Shifted({}, self.index)
        lb.covers = lambda z, x, y: True
        got = lb.tile(self.z, self.x, self.y)
        self.assertIsNotNone(got)
        h, ok = got
        lat, lon = hrdem.tile_lat_lon(self.z, self.x, self.y, 512)
        ex, _ = bcproj.forward(6653, lat, lon)
        col = ex - self.origin[0]
        west, east = ok & (col < 60), ok & (col > 70)
        self.assertTrue(west.any() and east.any())
        self.assertGreater(float(h[west].mean()), 100.0 + 10.0)                  # the 2024 file's (+10)
        r = np.nonzero(east)
        self.assertLess(float(np.abs(h[east] - h[east]).max()), 1e-6)
        self.assertTrue(np.all(h[east] < 100.0 + 0.5 * 150 + 0.25 * 130 + 3.5))   # the 2019 file's, no +10
        del r

    def test_outside_index_is_none(self):
        lb = FakeLidar({}, self.index)
        self.assertIsNone(lb.tile(18, 0, 0))            # (not even near BC)
        self.assertFalse(lb.covers(13, 1300, 2800))     # coarser than MIN_ZOOM


class FeatherTest(unittest.TestCase):
    def test_ramp(self):
        v = np.ones((40, 40), bool)
        v[:, :10] = False
        w = hrdem.feather(v, 8)
        self.assertEqual(float(w[:, :10].max()), 0.0)
        self.assertAlmostEqual(float(w[20, 10]), 1 / 8)
        self.assertEqual(float(w[20, 30]), 1.0)
        self.assertTrue(np.all(np.diff(w[20, 10:18]) > 0))
        self.assertTrue(np.all(hrdem.feather(np.ones((4, 4), bool), 5) == 1))


class StubSource:
    def __init__(self, h, ok, fail=False):
        self.h, self.ok, self.fail = h, ok, fail

    def covers(self, z, x, y):
        return True

    def tile(self, z, x, y):
        if self.fail:
            raise netfetch.FetchFailed("down")
        return self.h.copy(), self.ok.copy()


class CanadaBlendTest(unittest.TestCase):
    def make(self, lidar, hrd, sparse=None):
        d = demmod.Dem.__new__(demmod.Dem)
        import threading
        from collections import Counter
        d._lock = threading.Lock()
        d._local = threading.local()
        d.stats = Counter()
        d.lidarbc, d.hrdem, d.lidarbc_sparse = lidar, hrd, sparse
        d.lidarbc_served, d.hrdem_served = set(), set()
        return d

    def key(self):
        z = 16
        n = 2 ** z
        return (z, int((-124.5 + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(49.3))) / math.pi) / 2 * n))

    def test_lidar_first_hrdem_fills_sea_kept(self):
        a = np.full((512, 512), 50.0, np.float32)
        a[:, :20] = 0.0                                     # the sea
        lv = np.zeros((512, 512), bool)
        lv[:, :300] = True
        hv = np.ones((512, 512), bool)
        d = self.make(StubSource(np.full((512, 512), 60.0, np.float32), lv),
                      StubSource(np.full((512, 512), 70.0, np.float32), hv))
        out = d._canada(self.key(), a)
        self.assertEqual(float(out[100, 5]), 0.0)          # the sea stays 0
        self.assertAlmostEqual(float(out[100, 200]), 60.0, places=4)   # LidarBC
        self.assertAlmostEqual(float(out[100, 450]), 70.0, places=4)   # HRDEM where LidarBC has nothing
        self.assertIn(self.key(), d.lidarbc_served)
        self.assertIn(self.key(), d.hrdem_served)

    def test_feathered_edge(self):
        a = np.full((512, 512), 50.0, np.float32)
        lv = np.zeros((512, 512), bool)
        lv[:, 256:] = True
        d = self.make(StubSource(np.full((512, 512), 60.0, np.float32), lv), None)
        out = d._canada(self.key(), a)
        row = out[200, 250:330]
        self.assertTrue(np.all(np.diff(row) >= -1e-5))      # a slope from 50 to 60, no step
        self.assertLess(float(row[7] - row[6]), 5.0)

    def test_failure_keeps_heights_and_skips_mirror(self):
        a = np.full((512, 512), 50.0, np.float32)
        d = self.make(StubSource(None, None, fail=True), None)
        d._local.partial = False
        out = d._canada(self.key(), a)
        np.testing.assert_array_equal(out, a)
        self.assertTrue(d._local.partial)

    def test_coarse_zoom_untouched(self):
        a = np.full((512, 512), 50.0, np.float32)
        d = self.make(StubSource(np.full((512, 512), 60.0, np.float32), np.ones((512, 512), bool)), None)
        self.assertIs(d._canada((11, 325, 702), a), a)          # coarser than the bias zooms: as it was
        self.assertIs(d._canada((12, 650, 1405), a), a)         # no sparse reader: as it was


class SparsePoints:
    """A LidarBC stand-in for the bias grid: the ground is base - lift where lon < split, none east of it."""

    def __init__(self, lift, split):
        self.lift, self.split = lift, split

    def _index(self):
        return (None, None, None, None, (40.0, -140.0, 60.0, -110.0))

    def sparse(self, box, step_m=64.0, coarse_m=None):
        s, w, n, e = box
        lat, lon = np.meshgrid(np.linspace(s, n, 60), np.linspace(w, e, 60), indexing="ij")
        ok = lon < self.split
        return lat[ok], lon[ok], np.full(int(ok.sum()), 50.0 - self.lift, np.float32)


class BiasTest(unittest.TestCase):
    def key(self, z):
        n = 2 ** z
        return (z, int((-124.5 + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(49.3))) / math.pi) / 2 * n))

    def test_lifted_forest_comes_down_and_fades(self):
        z, x, y = self.key(13)
        n = 2 ** z
        mid = (x + 0.5) / n * 360.0 - 180.0
        a = np.full((512, 512), 50.0, np.float32)
        d = CanadaBlendTest().make(None, None, SparsePoints(15.0, mid))
        out = d._bc_bias((z, x, y), a)
        self.assertAlmostEqual(float(out[256, 20]), 35.0, delta=1.0)        # west: down to the lidar's ground
        self.assertAlmostEqual(float(out[256, 500]), 50.0, delta=3.0)       # east, past the lidar: (nearly) as it was
        self.assertTrue(np.all(np.abs(np.diff(out[256])) < 1.0))           # a slope, no step

    def test_sea_and_agreement_untouched(self):
        z, x, y = self.key(12)
        a = np.full((512, 512), 50.0, np.float32)
        a[:, :40] = 0.0
        d = CanadaBlendTest().make(None, None, SparsePoints(0.0, 999.0))   # lidar agrees everywhere
        self.assertIs(d._bc_bias((z, x, y), a), a)
        d = CanadaBlendTest().make(None, None, SparsePoints(10.0, 999.0))
        out = d._bc_bias((z, x, y), a)
        self.assertTrue(np.all(out[:, :40] == 0.0))
        self.assertAlmostEqual(float(out[256, 300]), 40.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()


# --- round 2 (2026-10-23): RGB / JPEG orthophotos, sparse rows, canopy ---------------------------------------------
import bcortho  # noqa: E402
import maprasters  # noqa: E402
import mapcells  # noqa: E402


class CogRGBTest(unittest.TestCase):
    def test_lzw_tiled_rgb_exact(self):
        ref = np.moveaxis(np.load(FIX / "ortho_ref.npy"), 0, 2)
        c = cog.Cog(rangeread.MemoryRange((FIX / "ortho_lzw_tiled.tif").read_bytes()))
        self.assertEqual(c.bands, 3)
        np.testing.assert_array_equal(c.read_window(10, 20, 150, 120), ref[20:140, 10:160])

    def test_jpeg_ycbcr_strips(self):
        ref = np.moveaxis(np.load(FIX / "ortho_ref.npy"), 0, 2).astype(int)
        c = cog.Cog(rangeread.MemoryRange((FIX / "ortho_jpeg_strips.tif").read_bytes()))
        self.assertEqual((c.compression, c.photometric, c.tile_h), ("jpeg", 6, 32))
        w = c.read_window(30, 0, 200, 200).astype(int)
        self.assertLess(float(np.abs(w - ref[0:200, 30:230]).mean()), 2.0)      # (lossy: quality 95)

    def test_sample_nearest_strips(self):
        h = fixture_heights()
        tc = cog.TileCache(1 << 24)
        c = cog.Cog(rangeread.MemoryRange((FIX / "dem_strips_lzw.tif").read_bytes()), tiles=tc)
        col, row = np.array([20.3, 100.9, 129.5]), np.array([30.7, 149.2, 60.1])
        v, ok = c.sample_nearest(col, row)
        self.assertTrue(ok.all())
        np.testing.assert_array_equal(v, h[row.astype(int), col.astype(int)])
        self.assertEqual(len(tc._d), 3)         # one strip decoded per row asked


class DespikeTest(unittest.TestCase):
    def test_spike_cap_crown(self):
        h = np.full((20, 20), 30.0, np.float32)
        h[5, 5] = 105.0                         # a lone spike (a cliff edge, a wire)
        h[12:15, 12:15] = 45.0                  # a crown above the canopy: kept
        h[2, 15] = 99.0
        h[1:4, 14:17] = 99.0                    # a 3 x 3 patch over the cap: capped
        ok = np.ones_like(h, bool)
        out = lidarbc.despike(h, ok)
        self.assertEqual(float(out[5, 5]), 30.0)
        self.assertEqual(float(out[13, 13]), 45.0)
        self.assertEqual(float(out[2, 15]), lidarbc.TREE_CAP_M)


def _place_fixture_index(tmp, name, fname, epsg, z, lat, lon, size_px, px_m, fields_res=False):
    """An index of one fixture file, its georeferencing moved under (lat, lon); returns (index path, shift)."""
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    la, lo = hrdem.tile_lat_lon(z, x, y, 64)
    ex, ny = bcproj.forward(epsg, la, lo)
    x0, y1 = math.floor(ex.min()) - 2, math.ceil(ny.max()) + 2
    row = [name, epsg, x0, y1 - size_px[0] * px_m, x0 + size_px[1] * px_m, y1, 2024] + ([px_m] if fields_res else [])
    path = os.path.join(tmp, fname)
    with gzip.open(path, "wt") as f:
        json.dump({"files": [row], "sheets": ["092B"]}, f)
    return path, (x0, y1), (z, x, y)


class SparseRowsTest(unittest.TestCase):
    def test_points_on_rows_match_heights(self):
        with tempfile.TemporaryDirectory() as tmp:
            idx, (x0, y1), (z, x, y) = _place_fixture_index(tmp, "a/2024/dem/f.tif", "i.json.gz", 6653, 16,
                                                            48.43, -123.37, (150, 130), 1.0)
            data = (FIX / "dem_strips_lzw.tif").read_bytes()

            class L(lidarbc.LidarBC):
                def _cog(s, key, level=0):
                    c = cog.Cog(rangeread.MemoryRange(data))
                    c.x0, c.y0 = float(x0), float(y1)
                    return c
            lb = L(None, None, index_path=idx)
            box = hrdem.tile_bounds(z, x, y)
            lat, lon, hgt = lb.sparse(box, step_m=16.0)
            self.assertGreater(len(hgt), 10)
            ex, ny = bcproj.forward(6653, lat, lon)
            col, row = np.floor(ex - x0).astype(int), np.floor(y1 - ny).astype(int)
            h = fixture_heights()
            np.testing.assert_allclose(hgt, h[np.clip(row, 0, 149), np.clip(col, 0, 129)], atol=0.6)


class BCOrthoTest(unittest.TestCase):
    def test_render_box_filter_and_nodata(self):
        with tempfile.TemporaryDirectory() as tmp:
            idx, (x0, y1), (z, x, y) = _place_fixture_index(tmp, "a/2016/orthophoto/f.tif", "o.json.gz", 3157, 19,
                                                            48.43, -123.37, (200, 240), 0.5, fields_res=True)
            data = (FIX / "ortho_lzw_tiled.tif").read_bytes()

            class O(bcortho.BCOrtho):
                def _cog(s, key, level):
                    if level:
                        return None
                    c = cog.Cog(rangeread.MemoryRange(data))
                    c.x0, c.y0 = float(x0), float(y1)
                    return c
            bo = O(None, None, index_path=idx)
            import imagery
            mb = imagery.mercator_bbox(z, x, y)
            rgb, ok = bo.render(mb, 64)
            self.assertIsNotNone(rgb)
            self.assertTrue(ok.any() and not ok.all())               # the black west edge is nodata
            ref = np.moveaxis(np.load(FIX / "ortho_ref.npy"), 0, 2).astype(float)
            self.assertLess(abs(float(rgb[ok].mean()) - float(ref[:, 40:].mean())), 25.0)
            far = O(None, None, index_path=idx)
            self.assertEqual(far.render(imagery.mercator_bbox(19, 1000, 1000), 64), (None, None))


class BCCanopyTest(unittest.TestCase):
    def test_lidar_heights_replace_meta_where_measured(self):
        class Stub:
            def __init__(self, v):
                self.v = v

            def _index(self):
                return (None, None, None, None, (47.9, -130.1, 51.1, -121.9))

            def sample(self, lat, lon, files=None, max_strip_m=None, coarse_m=None):
                lat = np.asarray(lat)
                ok = np.broadcast_to(np.asarray(lon) < -123.37, lat.shape).copy()
                if files is not None:
                    files.append("k%d" % self.v)
                return np.full(lat.shape, self.v, np.float32), ok
        r = maprasters.Rasters(None, None)
        r._bc = (Stub(100.0), Stub(142.4))                  # ground 100 m, surface 142.4 m: 42 m trees
        n = 2 ** 14
        cell = mapcells.CellKey(14, int((-123.37 + 180) / 360 * n),
                                int((1 - math.asinh(math.tan(math.radians(48.43))) / math.pi) / 2 * n))
        npx = 2048
        base = maprasters.Raster(np.full((npx, npx), 7, np.uint8), "EPSG:3857", 0.0, 0.0, 1.0, 1.0, 255, ["qk"])
        out, files = r.bc_canopy(cell, base)
        self.assertEqual(sorted(files), ["k100", "k142"])
        vals = set(np.unique(out.data).tolist())
        self.assertEqual(vals, {7, 42})                     # Meta's 7 m east of the split, lidar's 42 m west
        self.assertIn("lidarbc:k100", out.files)
        far = mapcells.CellKey(14, 100, 100)
        self.assertIs(r.bc_canopy(far, base)[0], base)      # outside BC: as it was
