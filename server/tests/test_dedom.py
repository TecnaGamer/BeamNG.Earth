"""Germany's measured bridge decks (dedom.py, roadz.RoadZ._dedom, tools/build_de_decks.py): the WCS GeoTIFF reader,
the request boxes, the sample grid across a deck, the source choice in Germany, the shards, and a whole cell sampled
offline from a synthetic surface model and measured in the game from the shards alone."""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import build_de_decks  # noqa: E402
import bcproj  # noqa: E402
import dedom  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import roadz  # noqa: E402

NATIVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= roadz.GAP_NATIVE


def geotiff(a, x0, y0, res=1.0, nodata=None):
    """A float32 GeoTIFF's bytes as the WCS answers: grid a, its top-left corner (x0, y0), res m pixels."""
    from PIL import Image, TiffImagePlugin
    im = Image.fromarray(np.asarray(a, np.float32), mode="F")
    info = TiffImagePlugin.ImageFileDirectory_v2()
    info[33550] = (res, res, 0.0)
    info[33922] = (0.0, 0.0, 0.0, x0, y0, 0.0)
    if nodata is not None:
        info[42113] = str(nodata)
    buf = io.BytesIO()
    im.save(buf, format="TIFF", tiffinfo=info)
    return buf.getvalue()


class ReaderTest(unittest.TestCase):
    def test_tiff_and_lookup(self):
        a = np.arange(12, dtype=np.float32).reshape(3, 4)
        a[2, 3] = -9999.0
        g = dedom.read_tiff(geotiff(a, 1000.0, 2000.0, nodata=-9999))
        self.assertEqual(g[1:], (1000.0, 2000.0, 1.0, 1.0))
        z = dedom.at(g, np.array([1000.5, 1003.5, 1003.5, 999.0]), np.array([1999.5, 1997.5, 1998.5, 1999.5]))
        self.assertEqual(z[0], 0.0)
        self.assertTrue(np.isnan(z[1]))         # (no-data)
        self.assertEqual(z[2], 7.0)
        self.assertTrue(np.isnan(z[3]))         # (outside)

    def test_boxes_stay_within_the_service_limit(self):
        x = np.r_[np.linspace(0, 4500, 50)]
        y = np.full(50, 10.0)
        got = dedom.boxes(x, y, 2000.0)
        self.assertEqual(len(got), 3)
        self.assertEqual(sum(len(b[4]) for b in got), 50)
        for x0, y0, x1, y1, _ in got:
            self.assertLessEqual(x1 - x0, 2000.0)
            self.assertLessEqual(y1 - y0, 2000.0)

    def test_grid_across_a_deck(self):
        g = dedom.grid_along(np.array([[0.0, 0.0], [10.0, 0.0]]), 3.0)
        self.assertEqual(len(g), 11 * 7)
        self.assertAlmostEqual(g[:, 1].min(), -3.0)
        self.assertAlmostEqual(g[:, 1].max(), 3.0)

    def test_sources(self):
        self.assertIn("lidar", roadz.sources_at(51.03, 6.97, True))         # (NRW)
        self.assertNotIn("lidar", roadz.sources_at(48.14, 11.58, True))     # (Bavaria: no service yet)
        with mock.patch.object(roadz, "DE_ON", False):
            self.assertNotIn("lidar", roadz.sources_at(51.03, 6.97, True))


class ShardTest(unittest.TestCase):
    def test_round_trip(self):
        la = np.array([51.0, 51.000005, 51.1])
        lo = np.array([7.0, 7.000003, 7.2])
        got = dedom.decode(dedom.encode(la, lo, np.array([56.31, 57.0, 60.0]), np.array([50.4, np.nan, 51.0])))
        order = np.argsort(got["lat"])
        np.testing.assert_allclose(got["lat"][order], la, atol=1e-6)
        np.testing.assert_allclose(got["dom"][order], [56.31, 57.0, 60.0], atol=0.006)
        self.assertTrue(np.isnan(got["dgm"][order][1]))
        self.assertEqual(dedom.decode(b"broken"), {})

    def test_a_tile_without_a_shard_has_no_points(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {dedom.LOCAL_ENV: d}):
            dedom._shards.clear()
            got = dedom.points(None, 51.0, 7.0, 51.01, 7.01)
        dedom._shards.clear()
        self.assertEqual(len(got[0]), 0)


@unittest.skipUnless(NATIVE, "earthfast with the lidar kernels")
class CellTest(unittest.TestCase):
    """A 120 m bridge over a road in a cell of NRW: the surface model's deck 6.3 m over flat ground at 50 m (the DGM
    0.4 m over our DEM: the datum), the deck measured at 6.3 m over our ground."""

    def setUp(self):
        self.key = mapcells.cell_of(51.0, 7.0)
        lat_c, lon_c = self.key.center()
        self.frame = geodesy.EnuFrame(lat_c, lon_c)
        la, lo, _ = self.frame.enu_to_geodetic(np.array([-60.0, 60.0]), np.array([0.0, 0.0]), 0.0)
        self.ring = np.stack([np.asarray(lo), np.asarray(la)], 1)
        x, y = bcproj.utm(np.asarray(la), np.asarray(lo), 32)
        self.x0, self.y0 = float(min(x)) - 100, float(max(y)) + 100
        yy, xx = np.mgrid[0:200, 0:330]
        px, py = self.x0 + xx + 0.5, self.y0 - yy - 0.5
        on = (np.abs(py - float(np.mean(y))) <= 6.0) & (px >= min(x) - 1) & (px <= max(x) + 1)
        self.dgm = np.full(px.shape, 50.4)
        self.dom = np.where(on, 50.4 + 6.3, 50.4)

    def fetch(self, url):
        a = self.dom if "dom" in url.split("COVERAGEID=")[1][:8] else self.dgm
        return geotiff(a, self.x0, self.y0)

    def built(self, fetch):
        """The game's measured Lines of the cell from shards built offline with fetch as the state's service: the
        game's own fetch fails, so only the shards can have given it anything."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(dedom._shards.clear)
        dedom._shards.clear()
        build = roadz.RoadZ(None, None, None)
        with mock.patch.object(build, "_bridges", return_value=[(self.ring, 4.0)]), \
                mock.patch.object(build, "_gap_candidates", return_value=[]), \
                mock.patch.object(build, "_fetch", side_effect=lambda u, **k: fetch(u)):
            la, lo, dom, dgm = build.dedom_samples(self.key, dedom.SERVICES[0])
        ok = np.isfinite(dom)
        if ok.any():
            build_de_decks.write_shards(tmp.name, la[ok], lo[ok], dom[ok], dgm[ok])
        game = roadz.RoadZ(None, None, None)

        def no_service(u, **k):
            raise AssertionError("the game asked " + u)
        with mock.patch.dict(os.environ, {dedom.LOCAL_ENV: tmp.name}), \
                mock.patch.object(game, "_bridges", return_value=[(self.ring, 4.0)]), \
                mock.patch.object(game, "_gap_candidates", return_value=[]), \
                mock.patch.object(game, "_fetch", side_effect=no_service), \
                mock.patch.object(game, "_dem_raw", side_effect=lambda la, lo: np.full(len(np.atleast_1d(la)), 50.0)):
            return game._lidar(self.key)

    def test_deck_measured(self):
        got = self.built(self.fetch)
        self.assertEqual(len(got), 1)
        self.assertEqual(int(got.level[0]), 1)
        z = got.z[got.off[0]:got.off[1]]
        self.assertGreater(np.isfinite(z).sum(), 10)
        self.assertAlmostEqual(float(np.nanmedian(z)), 50.0 + 6.3, delta=0.3)

    def test_outside_the_state_no_decks(self):
        nan = np.full_like(self.dom, -9999.0)
        got = self.built(lambda u: geotiff(nan, self.x0, self.y0, nodata=-9999))
        self.assertEqual(len(got), 0)


if __name__ == "__main__":
    unittest.main()
