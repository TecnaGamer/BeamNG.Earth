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
        self.assertIn("lidar", roadz.sources_at(48.14, 11.58, True))        # (Bavaria: its tiles)
        self.assertNotIn("lidar", roadz.sources_at(48.21, 16.37, True))     # (Vienna)
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

    def test_shards_whatever_the_folder_order(self):
        """Two cells with samples at the same places (a bridge across their border, each state's value): the shards
        are the same bytes whatever order the file system lists the cells in (the first cell's sample is kept, and
        every keep-th sample of a tile, in the cells' sorted order)."""
        rng = np.random.default_rng(1531)
        a, b = mapcells.CellKey(14, 8500, 5492), mapcells.CellKey(14, 8501, 5492)
        lon0, lat0, lon1, lat1 = a.bounds()
        la, lo = rng.uniform(lat0, lat1, 600), rng.uniform(lon1 - 0.002, lon1, 600)

        def build(d, order):
            for key, dom in ((a, 60.0), (b, 61.0)):
                os.makedirs(os.path.join(d, "cells", "nw"), exist_ok=True)
                with open(build_de_decks.cell_path(d, "nw", key), "wb") as fh:
                    fh.write(dedom.encode(la, lo, np.full(len(la), dom), np.full(len(la), 50.0)))
            real = os.listdir
            with mock.patch.object(build_de_decks.os, "listdir", side_effect=lambda q: order(real(q))):
                build_de_decks.shards(d, 4)
            out = {}
            for root, _dirs, names in os.walk(os.path.join(d, "dedecks")):
                for n in names:
                    if n.endswith(".npz"):
                        with open(os.path.join(root, n), "rb") as fh:
                            out[n] = fh.read()
            return out
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2,                 mock.patch("builtins.print"):
            one, two = build(d1, sorted), build(d2, lambda names: sorted(names, reverse=True))
        self.assertTrue(one)
        self.assertEqual(one, two)
        got = dedom.decode(next(iter(one.values())))
        self.assertEqual(set(got["dom"].tolist()), {60.0})       # (the first cell's samples)

    def test_a_tile_without_a_shard_has_no_points(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {dedom.LOCAL_ENV: d}):
            dedom._shards.clear()
            got = dedom.points(None, 51.0, 7.0, 51.01, 7.01)
        dedom._shards.clear()
        self.assertEqual(len(got[0]), 0)


class PlausibleTest(unittest.TestCase):
    """dedom.plausible: a deck standing high over the ground at both free ends is kept only where its carriageway's
    samples read a road surface; tree crowns over a track (samples scattered round the profile) are not measured."""

    def setUp(self):
        self.rng = np.random.default_rng(1531)
        self.xy = np.array([[0.0, 0.0], [30.0, 0.0]])
        self.s = np.linspace(0.0, 30.0, 9)
        self.grid = np.mgrid[-2:33:1.0, -6:7:1.0].reshape(2, -1).T
        self.ground = np.c_[self.grid, np.zeros(len(self.grid))]     # (the DGM: flat at 0, beyond both ends too)

    def deck(self, z):
        return np.c_[self.grid, z]

    def check(self, pts, z=14.0, half=2.5, ground=None, dem=0.0, others=()):
        return dedom.plausible(self.xy, half, self.s, np.full(len(self.s), z), pts,
                               self.ground if ground is None else ground, lambda x, y: dem, others)

    def crowns(self):
        z = 14.0 + self.rng.normal(0.0, 0.8, len(self.grid))
        z[self.rng.random(len(z)) < 0.25] = 0.0         # (gaps in the crowns: the ground)
        return self.deck(z)

    def test_smooth_deck_high_over_the_ground_kept(self):
        """A viaduct's span 12 m over the ground at both ends: its samples on its surface (a few railings, cars)."""
        z = np.full(len(self.grid), 12.0) + self.rng.normal(0.0, 0.03, len(self.grid))
        z[self.rng.random(len(z)) < 0.2] += 1.5
        self.assertEqual(self.check(self.deck(z), 12.0, half=4.0), (True, ""))

    def test_crowns_over_a_track_dropped(self):
        ok, why = self.check(self.crowns())
        self.assertFalse(ok)
        self.assertIn("on the profile", why)

    def test_crowns_without_dgm_beyond_read_our_dem(self):
        self.assertFalse(self.check(self.crowns(), ground=np.zeros((0, 3)), dem=0.5)[0])
        # (the ground beyond as high: the deck stands on its approaches, its samples are not judged)
        self.assertTrue(self.check(self.crowns(), ground=np.zeros((0, 3)), dem=10.0)[0])

    def test_low_deck_not_judged(self):
        """A deck near the ground at an end (a crossing, its approaches) keeps its measurement whatever its scatter."""
        self.assertTrue(self.check(self.crowns(), ground=self.ground + [0.0, 0.0, 10.0])[0])

    def test_end_on_another_deck_kept(self):
        """An end that goes on as another deck stands on no approach: a viaduct's span is not judged."""
        other = np.array([[30.0, 1.0], [60.0, 5.0]])
        self.assertTrue(self.check(self.crowns(), others=[other])[0])

    def test_too_few_samples_dropped(self):
        """High over the ground at both free ends with a handful of samples: nothing to tell a road by."""
        z = np.full(len(self.grid), 14.0)
        few = self.deck(z)[(np.abs(self.grid[:, 1]) < 0.5) & (self.grid[:, 0] % 10 == 0)]
        self.assertLess(len(few), dedom.SUPPORT_MIN_N)
        self.assertFalse(self.check(few)[0])

    def test_only_the_carriageway_counts(self):
        """Crowns beside the deck (past its half width) and past its ends are not its samples."""
        z = np.full(len(self.grid), 14.0)
        off = (np.abs(self.grid[:, 1]) > 2.5) | (self.grid[:, 0] < 0.0) | (self.grid[:, 0] > 30.0)
        z[off] = 20.0 + self.rng.normal(0.0, 3.0, int(off.sum()))
        share, n = dedom.support(self.xy, 2.5, self.s, np.full(len(self.s), 14.0), self.deck(z))
        self.assertEqual((share, n), (1.0, 31 * 5))
        self.assertTrue(self.check(self.deck(z))[0])


class ManifestTest(unittest.TestCase):
    """The shards read through the dataset's manifest (pagesdata.Manifest): from its "data" host when it names one,
    else beside it; none published (404): no samples; unreachable: the cell is built again later."""

    PAGES = "https://pages.test/data"

    def setUp(self):
        self.addCleanup(self.reset)
        self.reset()
        self.shard = dedom.encode(np.array([51.001, 51.002]), np.array([7.001, 7.002]), np.array([60.0, 61.0]),
                                  np.array([50.0, np.nan]))
        self.asked = []
        p = mock.patch.object(dedom.pagesdata, "PAGES", self.PAGES)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def reset():
        dedom._state.update(cache=None, fetcher=None, pages=None)
        dedom._shards.clear()
        dedom.pagesdata.session_reset()     # (the run's decisions are per process: each test its own run)

    def serve(self, files):
        import netfetch

        def fetch(u):
            self.asked.append(u)
            got = files.get(u)
            if isinstance(got, Exception):
                raise got
            if got is None:
                raise netfetch.NotFound(u)
            return got
        return fetch

    def manifest(self, **kw):
        import json
        return json.dumps(dict({"built": "2026-11-22", "shards": 1}, **kw)).encode()

    def test_shards_from_the_data_host(self):
        x, y = dedom.tile_of(51.001, 7.001)
        data = "https://tiles.test/data/dedecks/2026_11_22-abc"
        fetch = self.serve({f"{self.PAGES}/dedecks/manifest.json": self.manifest(data=data),
                            f"{data}/{x}/{y}.npz": self.shard})
        la, lo, dom, dgm = dedom.points(fetch, 51.0, 7.0, 51.01, 7.01)
        self.assertEqual(len(la), 2)
        self.assertEqual(sorted(dom.tolist()), [60.0, 61.0])
        self.assertIn(f"{data}/{x}/{y}.npz", self.asked)
        self.assertFalse([u for u in self.asked if u.startswith(self.PAGES) and u.endswith(".npz")])

    def test_shards_beside_the_manifest_without_a_data_host(self):
        x, y = dedom.tile_of(51.001, 7.001)
        fetch = self.serve({f"{self.PAGES}/dedecks/manifest.json": self.manifest(),
                            f"{self.PAGES}/dedecks/{x}/{y}.npz": self.shard})
        self.assertEqual(len(dedom.points(fetch, 51.0, 7.0, 51.01, 7.01)[0]), 2)

    def test_not_published_no_samples(self):
        fetch = self.serve({})
        self.assertEqual(len(dedom.points(fetch, 51.0, 7.0, 51.01, 7.01)[0]), 0)
        self.assertEqual(self.asked, [f"{self.PAGES}/dedecks/manifest.json"])     # (no shard asked for)

    def test_manifest_unreachable_none_for_the_run(self):
        # (the run is decided without a manifest, a fault recorded by pagesdata: no samples in any process, and the
        # build is "none" in every key, dedom.identity, so nothing measured now is read once a build is had)
        import netfetch
        fetch = self.serve({f"{self.PAGES}/dedecks/manifest.json": netfetch.FetchFailed("504")})
        la, _lo, _dom, _dgm = dedom.points(fetch, 51.0, 7.0, 51.01, 7.01)
        self.assertEqual(len(la), 0)
        self.assertEqual(dedom.identity(network=False), "none")

    def test_the_manifest_pinned_on_disk(self):
        x, y = dedom.tile_of(51.001, 7.001)
        files = {f"{self.PAGES}/dedecks/manifest.json": self.manifest(),
                 f"{self.PAGES}/dedecks/{x}/{y}.npz": self.shard}

        class Fetcher:
            def get(_self, u, kind="demand", **_k):
                return mock.Mock(body=self.serve(files)(u))
        with tempfile.TemporaryDirectory() as d:
            dedom.configure(d, Fetcher())
            self.assertEqual(len(dedom.points(self.serve(files), 51.0, 7.0, 51.01, 7.01)[0]), 2)
            self.assertTrue(os.path.exists(os.path.join(d, "pagesdata", "dedecks", "manifest.json")))
            self.reset()


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

    def built(self, fetch, half=4.0):
        """The game's measured Lines of the cell from shards built offline with fetch as the state's service: the
        game's own fetch fails, so only the shards can have given it anything."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(dedom._shards.clear)
        dedom._shards.clear()
        build = roadz.RoadZ(None, None, None)
        with mock.patch.object(build, "_bridges", return_value=[(self.ring, half)]), \
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
                mock.patch.object(game, "_bridges", return_value=[(self.ring, half)]), \
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

    def test_canopy_over_a_mapped_track_not_measured(self):
        """A forest track's 30 m bridge under tree crowns 14 m high (15 % of the samples through to the ground):
        no deck measured, the inference stands; with the rule off the crowns would be its deck."""
        la, lo, _ = self.frame.enu_to_geodetic(np.array([-15.0, 15.0]), np.array([0.0, 0.0]), 0.0)
        self.ring = np.stack([np.asarray(lo), np.asarray(la)], 1)
        rng = np.random.default_rng(1531)
        crown = 50.4 + 14.0 + rng.normal(0.0, 0.5, self.dgm.shape)
        self.dom = np.where(rng.random(self.dgm.shape) < 0.15, 50.4, crown)
        self.assertEqual(len(self.built(self.fetch, half=2.5)), 0)
        with mock.patch.object(dedom, "ABOVE_M", 1e9):
            got = self.built(self.fetch, half=2.5)
        self.assertEqual(len(got), 1)
        self.assertGreater(float(np.nanmedian(got.z[got.off[0]:got.off[1]])), 60.0)

    def test_outside_the_state_no_decks(self):
        nan = np.full_like(self.dom, -9999.0)
        got = self.built(lambda u: geotiff(nan, self.x0, self.y0, nodata=-9999))
        self.assertEqual(len(got), 0)


if __name__ == "__main__":
    unittest.main()
