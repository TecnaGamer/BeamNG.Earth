"""bdtopo hedgerows (`BDTOPO_V3:haie`): linear hedgerows sampled in Rust (`earthfast.bdtopo_hedgerows`) into cypress
and olive windbreak trees and shrub points in Provence / Mediterranean France, deciduous trees and shrubs further
north; cached under `<cache>/bdtopo/haie_v1` (managed by `cache.MANAGED`), never fetched outside France."""
import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bdtopo  # noqa: E402
import cache  # noqa: E402
import mapcells  # noqa: E402
import vegetation as veg  # noqa: E402

PROVENCE = (43.9942, 4.7408)        # Tavel / Provence
NORMANDY = (49.1829, -0.3707)       # Caen / Normandy bocage
BALTIMORE = (39.2904, -76.6122)     # outside France


def haie_feature(coords, hauteur=None, multi=False):
    geom = ({"type": "MultiLineString", "coordinates": coords}
            if multi else {"type": "LineString", "coordinates": coords})
    return {"type": "Feature", "geometry": geom, "properties": {"hauteur": hauteur}}


def make_chm(bounds, n=256, height=0):
    lon0, lat0, lon1, lat1 = bounds
    x0, y1 = veg._to3857(lon0, lat1)
    x1, y0 = veg._to3857(lon1, lat0)
    dx = (x1 - x0) / n
    dy = (y1 - y0) / n
    data = np.full((n, n), height, np.uint8)
    return SimpleNamespace(data=data, x0=x0, y0=y1, dx=dx, dy=dy, nodata=255)


class Res:
    def __init__(self, body):
        self.body = body
        self.headers = {}


class FakeHaieFetcher:
    def __init__(self, features, gzipped=True, fail=False):
        self.features = features
        self.gzipped = gzipped
        self.fail = fail
        self.calls = 0
        self.urls = []
        self.headers = []

    def get(self, url, kind="demand", lane=None, headers=None):
        self.calls += 1
        self.urls.append(url)
        self.headers.append(dict(headers or {}))
        if self.fail:
            raise IOError("offline")
        raw = json.dumps({"features": self.features}).encode("utf-8")
        return Res(gzip.compress(raw) if self.gzipped else raw)


class HaieParseAndSampleTest(unittest.TestCase):
    def test_parse_linestring_and_multilinestring(self):
        feats = [
            haie_feature([[4.740, 43.994], [4.742, 43.994]], hauteur=7.5),
            haie_feature([[[4.740, 43.995], [4.741, 43.995]], [[4.741, 43.995], [4.742, 43.996]]], hauteur=2.0,
                         multi=True),
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[4.74, 43.99]]}, "properties": {}},
        ]
        arrays = bdtopo.parse_haie(feats)
        recs = bdtopo.HaieRecords(arrays)
        self.assertEqual(recs.n, 3)
        self.assertEqual(list(recs.line_off), [0, 2, 4, 6])
        np.testing.assert_allclose(recs.h, [7.5, 2.0, 2.0])

    def test_provence_hedgerows_produce_cypress_olive_and_shrubs_deterministically(self):
        key = mapcells.cell_of(*PROVENCE)
        bounds = key.bounds()
        chm = make_chm(bounds, n=256)
        hs = np.zeros((256, 256), np.float32)
        blocked = np.zeros((256, 256), bool)
        lon_c = 0.5 * (bounds[0] + bounds[2])
        lat_c = 0.5 * (bounds[1] + bounds[3])
        recs = bdtopo.HaieRecords(bdtopo.parse_haie([
            haie_feature([[lon_c - 0.003, lat_c], [lon_c + 0.003, lat_c]], hauteur=8.0),
            haie_feature([[lon_c - 0.003, lat_c + 0.001], [lon_c + 0.003, lat_c + 0.001]], hauteur=1.8),
        ]))
        it1, mask1 = bdtopo.haie_items(recs, chm, hs, blocked, bounds)
        it2, mask2 = bdtopo.haie_items(recs, chm, hs, blocked, bounds)
        self.assertGreater(len(it1), 40)
        self.assertIsNotNone(mask1)
        self.assertTrue(mask1.any())
        for f in ("lon", "lat", "height", "radius", "kind", "group", "rank", "yaw", "scale"):
            np.testing.assert_array_equal(getattr(it1, f), getattr(it2, f))
        np.testing.assert_array_equal(mask1, mask2)
        groups = set(it1.group.tolist())
        self.assertIn(veg.G_CYPRESS, groups)
        self.assertIn(veg.G_OLIVE, groups)
        self.assertIn(veg.G_SHRUB, groups)
        self.assertTrue(groups.issubset({veg.G_CYPRESS, veg.G_OLIVE, veg.G_SHRUB}))

    def test_northern_france_hedgerows_use_deciduous_and_shrubs(self):
        key = mapcells.cell_of(*NORMANDY)
        bounds = key.bounds()
        chm = make_chm(bounds, n=256)
        hs = np.zeros((256, 256), np.float32)
        blocked = np.zeros((256, 256), bool)
        lon_c = 0.5 * (bounds[0] + bounds[2])
        lat_c = 0.5 * (bounds[1] + bounds[3])
        recs = bdtopo.HaieRecords(bdtopo.parse_haie([
            haie_feature([[lon_c - 0.003, lat_c], [lon_c + 0.003, lat_c]], hauteur=6.5),
        ]))
        it, mask = bdtopo.haie_items(recs, chm, hs, blocked, bounds)
        self.assertGreater(len(it), 20)
        self.assertIsNotNone(mask)
        groups = set(it.group.tolist())
        self.assertIn(veg.G_DECIDUOUS, groups)
        self.assertNotIn(veg.G_CYPRESS, groups)
        self.assertTrue(groups.issubset({veg.G_DECIDUOUS, veg.G_SHRUB}))

    def test_blocked_pixels_suppress_hedgerow_points(self):
        key = mapcells.cell_of(*PROVENCE)
        bounds = key.bounds()
        chm = make_chm(bounds, n=128)
        hs = np.zeros((128, 128), np.float32)
        blocked = np.ones((128, 128), bool)
        lon_c = 0.5 * (bounds[0] + bounds[2])
        lat_c = 0.5 * (bounds[1] + bounds[3])
        recs = bdtopo.HaieRecords(bdtopo.parse_haie([
            haie_feature([[lon_c - 0.002, lat_c], [lon_c + 0.002, lat_c]], hauteur=7.0),
        ]))
        it, mask = bdtopo.haie_items(recs, chm, hs, blocked, bounds)
        self.assertEqual(len(it), 0)
        self.assertIsNone(mask)


class HaieSourceAndVegetationTest(unittest.TestCase):
    def test_outside_france_never_fetches(self):
        key = mapcells.cell_of(*BALTIMORE)
        with tempfile.TemporaryDirectory() as d:
            fx = FakeHaieFetcher([])
            src = bdtopo.HaieSource(d, fx)
            got = src.load(key)
            self.assertIs(got, bdtopo.EMPTY_HAIE)
            self.assertEqual(fx.calls, 0)

    def test_gzip_wfs_fetch_and_disk_cache(self):
        key = mapcells.cell_of(*PROVENCE)
        bounds = key.bounds()
        lon_c = 0.5 * (bounds[0] + bounds[2])
        lat_c = 0.5 * (bounds[1] + bounds[3])
        with tempfile.TemporaryDirectory() as d:
            fx = FakeHaieFetcher([haie_feature([[lon_c - 0.001, lat_c], [lon_c + 0.001, lat_c]], hauteur=7.0)])
            src = bdtopo.HaieSource(d, fx)
            recs = src.load(key)
            self.assertEqual(fx.calls, 1)
            self.assertEqual(fx.headers[0].get("Accept-Encoding"), "gzip")
            self.assertIn("BDTOPO_V3:haie", fx.urls[0])
            self.assertEqual(recs.n, 1)
            self.assertTrue(os.path.exists(src.path(key)))
            # Second source reads from disk without touching network
            src2 = bdtopo.HaieSource(d, FakeHaieFetcher([], fail=True))
            self.assertEqual(src2.cached(key).n, 1)

    def test_cache_dir_is_managed(self):
        self.assertIn("bdtopo", cache.MANAGED)

    def test_vegetation_cell_integrates_haie_in_france_only(self):
        key = mapcells.cell_of(*PROVENCE)
        bounds = key.bounds()
        chm = make_chm(bounds, n=128)
        lon_c = 0.5 * (bounds[0] + bounds[2])
        lat_c = 0.5 * (bounds[1] + bounds[3])

        class Grass:
            def sample(self, lon, lat):
                return np.full(np.shape(lon), 30, np.uint8)

        cd = SimpleNamespace(bounds=bounds, layers={
            "canopy": chm, "landcover": Grass(), "buildings": None, "roads": None, "water": None,
            "land_use": None, "infrastructure": None,
        })
        with tempfile.TemporaryDirectory() as d:
            fx = FakeHaieFetcher([haie_feature([[lon_c - 0.002, lat_c], [lon_c + 0.002, lat_c]], hauteur=8.5)])
            md = SimpleNamespace(
                release="t",
                cache_dir=d,
                fetcher=fx,
                cell=lambda k, layers=None, kind="demand": (
                    cd if tuple(k)[1:] == (key.x, key.y)
                    else SimpleNamespace(bounds=None, layers={})
                ),
            )
            v = veg.Vegetation(md, None)
            items = v.cell(key)
            self.assertEqual(fx.calls, 1)
            self.assertTrue(np.any(items.group == veg.G_CYPRESS))


if __name__ == "__main__":
    unittest.main()
