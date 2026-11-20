"""ignrpg & vegetation: French IGN RPG lavender parcels (`LAV`, `LVH`) filtered and sampled as oriented furrow rows
(`G_LAVENDER`, `bne_lavender_row`) in Rust (`native/earthfast/src/ignrpg.rs`)."""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cache  # noqa: E402
import geodesy  # noqa: E402
import ignrpg  # noqa: E402
import mapcells  # noqa: E402
import vegetation as veg  # noqa: E402
from test_vegetation import Feats, Raster  # noqa: E402

SAULT = (44.081847, 5.365593)


def _feature(fid, code_cultu, ring, holes=()):
    coords = [[[float(lon), float(lat)] for lon, lat in ring]]
    for h in holes:
        coords.append([[float(lon), float(lat)] for lon, lat in h])
    return {
        "type": "Feature",
        "id": fid,
        "properties": {"id_parcel": fid, "code_cultu": code_cultu},
        "geometry": {"type": "Polygon", "coordinates": coords},
    }


class FakeRes:
    def __init__(self, body):
        self.body = body
        self.headers = {}


class FakeFetcher:
    def __init__(self, features, gz=True, fail=False):
        self.features = features
        self.gz = gz
        self.fail = fail
        self.calls = []

    def get(self, url, kind="demand", lane=None, headers=None):
        self.calls.append((url, kind, lane, headers))
        if self.fail:
            raise IOError("network error")
        raw = json.dumps({"type": "FeatureCollection", "features": self.features}).encode("utf-8")
        return FakeRes(gzip.compress(raw) if self.gz else raw)


class CoversAndCacheTest(unittest.TestCase):
    def test_ignrpg_in_managed_cache(self):
        self.assertIn("ignrpg", cache.MANAGED)

    def test_covers_france_only(self):
        for lat, lon in (SAULT, (43.994226, 4.740771), (48.8566, 2.3522), (42.15, 9.15)):
            self.assertTrue(ignrpg.covers(mapcells.cell_of(lat, lon).bounds()), (lat, lon))
        for lat, lon in ((39.29, -76.61), (51.5074, -0.1278), (41.3874, 2.1686)):
            self.assertFalse(ignrpg.covers(mapcells.cell_of(lat, lon).bounds()), (lat, lon))


class ParseAndRowsTest(unittest.TestCase):
    def _parcel_ring(self, e, n):
        frame = geodesy.EnuFrame(*SAULT)
        la, lo, _ = frame.enu_to_geodetic(np.asarray(e, float), np.asarray(n, float), np.zeros(len(e)))
        return frame, np.column_stack([lo, la])

    def test_filters_lav_and_lvh_only_and_is_order_independent(self):
        _, r1 = self._parcel_ring([0, 60, 60, 0, 0], [0, 0, 40, 40, 0])
        _, r2 = self._parcel_ring([100, 150, 150, 100, 100], [0, 0, 35, 35, 0])
        _, r3 = self._parcel_ring([200, 260, 260, 200, 200], [0, 0, 40, 40, 0])
        _, tiny = self._parcel_ring([0, 5, 5, 0, 0], [0, 0, 5, 5, 0])
        f_lav = _feature("p1", "LAV", r1)
        f_lvh = _feature("p2", "lvh", r2)
        f_vrc = _feature("p3", "VRC", r3)
        f_bth = _feature("p4", "BTH", r3)
        f_tiny = _feature("p5", "LAV", tiny)

        a = ignrpg.Records(ignrpg.parse([f_lav, f_lvh, f_vrc, f_bth, f_tiny]))
        b = ignrpg.Records(ignrpg.parse([f_bth, f_lvh, f_tiny, f_vrc, f_lav]))
        self.assertEqual(a.n, 2)
        self.assertEqual(b.n, 2)
        np.testing.assert_array_equal(a.seeds, b.seeds)
        np.testing.assert_array_equal(a.lonlat, b.lonlat)

    def test_oriented_rows_follow_long_edges_and_respect_holes_and_blocked(self):
        # Skewed parallelogram with long sides along East (+x, 80 m) and 45-degree slanted ends
        frame, ring = self._parcel_ring([0, 80, 110, 30, 0], [0, 0, 30, 30, 0])
        _, hole = self._parcel_ring([40, 55, 55, 40, 40], [10, 10, 20, 20, 10])
        recs = ignrpg.Records(ignrpg.parse([_feature("sault-1", "LAV", ring, holes=[hole])]))

        x, y = veg._to3857(ring[:, 0], ring[:, 1])
        pad, dx = 20.0, 2.0
        x0, y1 = float(x.min()) - pad, float(y.max()) + pad
        cols = int(np.ceil((float(x.max()) + pad - x0) / dx))
        rows = int(np.ceil((y1 - float(y.min()) + pad) / dx))
        chm = Raster(np.zeros((rows, cols), np.uint8), x0, y1, dx)
        clear = np.zeros((rows, cols), bool)
        mask = np.zeros((rows, cols), bool)

        it = veg.lavender_items(recs, chm, clear, mask=mask)
        self.assertGreater(len(it), 100)
        self.assertTrue(mask.any())
        self.assertTrue(np.all(it.group == veg.G_LAVENDER))
        self.assertTrue(np.all(it.kind == veg.KIND_BUSH))
        self.assertTrue(np.all((it.height >= 0.66) & (it.height <= 0.80)))
        self.assertTrue(np.all((it.scale >= 0.92) & (it.scale <= 1.08)))
        self.assertTrue(np.all(it.radius == veg.LAV_RADIUS_M))

        # Rows must align with the 80 m East-West long edges (yaw ~ 0 or 180 deg)
        yaw = np.asarray(it.yaw) % 180.0
        self.assertTrue(np.all(np.minimum(yaw, 180.0 - yaw) < 1.0), f"unexpected yaws: {yaw[:5]}")

        # None inside the hole
        hx, hy = veg._to3857(hole[:, 0], hole[:, 1])
        ix, iy = veg._to3857(it.lon, it.lat)
        in_hole_box = (ix > hx.min() + 1.0) & (ix < hx.max() - 1.0) & (iy > hy.min() + 1.0) & (iy < hy.max() - 1.0)
        self.assertFalse(bool(in_hole_box.any()), "no lavender row inside a parcel hole")

        # Deterministic across calls
        it2 = veg.lavender_items(recs, chm, clear)
        for f in ("lon", "lat", "height", "radius", "kind", "group", "rank", "yaw", "scale"):
            np.testing.assert_array_equal(getattr(it, f), getattr(it2, f))

        # Blocked CHM mask clears all items
        self.assertEqual(len(veg.lavender_items(recs, chm, np.ones_like(clear))), 0)


class SourceAndVegetationTest(unittest.TestCase):
    def test_source_gzip_fetch_disk_cache_and_non_france_skip(self):
        key_fr = mapcells.cell_of(*SAULT)
        key_us = mapcells.cell_of(39.29, -76.61)
        lon0, lat0, lon1, lat1 = key_fr.bounds()
        mlon, mlat = 0.5 * (lon0 + lon1), 0.5 * (lat0 + lat1)
        ring = [[mlon - 0.0005, mlat - 0.0003], [mlon + 0.0005, mlat - 0.0003],
                [mlon + 0.0005, mlat + 0.0003], [mlon - 0.0005, mlat + 0.0003],
                [mlon - 0.0005, mlat - 0.0003]]
        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher([_feature("lav-cell", "LAV", ring)], gz=True)
            src = ignrpg.Source(d, fx)
            self.assertEqual(src.cell(key_us).n, 0)
            self.assertEqual(len(fx.calls), 0, "outside France: never fetches")

            recs = src.cell(key_fr)
            self.assertEqual(recs.n, 1)
            self.assertEqual(len(fx.calls), 1)
            self.assertEqual(fx.calls[0][2], "ign_wfs")
            self.assertEqual(fx.calls[0][3], {"Accept-Encoding": "gzip"})
            self.assertTrue(os.path.exists(src.path(key_fr)))

            # Second source reads from disk even when fetcher fails
            src2 = ignrpg.Source(d, FakeFetcher([], fail=True))
            self.assertEqual(src2.cell(key_fr).n, 1)

    def test_failed_download_not_cached_on_disk(self):
        key_fr = mapcells.cell_of(*SAULT)
        with tempfile.TemporaryDirectory() as d:
            src = ignrpg.Source(d, FakeFetcher([], fail=True))
            self.assertIsNone(src.load(key_fr))
            self.assertFalse(os.path.exists(src.path(key_fr)))

    def test_vegetation_cell_integrates_ignrpg_lavender(self):
        key = mapcells.cell_of(*SAULT)
        lon0, lat0, lon1, lat1 = key.bounds()
        x0, y1 = veg._to3857(lon0, lat1)
        x1, y0 = veg._to3857(lon1, lat0)
        dx = (x1 - x0) / 256
        h = np.zeros((256, 256), np.uint8)
        # Put a 2 m CHM bush bump inside the parcel (should be stripped) and a 12 m tree outside (should stay)
        h[128, 128] = 2
        yy, xx = np.mgrid[0:256, 0:256]
        h = np.maximum(h, np.clip(12 - np.hypot(yy - 40, xx - 40), 0, 12).astype(np.uint8))
        chm = Raster(h, x0, y1, dx, nodata=255)

        px0, py0 = x0 + 110 * dx, y1 - 140 * dx
        px1, py1 = x0 + 145 * dx, y1 - 115 * dx
        lo0, la0 = veg._to4326(px0, py0)
        lo1, la1 = veg._to4326(px1, py1)
        ring = [[lo0, la0], [lo1, la0], [lo1, la1], [lo0, la1], [lo0, la0]]

        cd = SimpleNamespace(
            bounds=key.bounds(),
            layers={
                "canopy": chm,
                "landcover": Raster(None, 0, 0, 1),
                "buildings": Feats(3, []),
                "roads": Feats(2, []),
                "water": Feats(3, []),
                "land_use": Feats(3, []),
                "infrastructure": Feats(3, []),
            },
        )

        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher([_feature("sault-lav", "LVH", ring)])
            src = ignrpg.Source(d, fx)

            class MD:
                release = "t"
                ignrpg = src

                def cell(self, k, layers=None, kind="demand"):
                    if tuple(k)[1:] == (key.x, key.y):
                        return cd
                    return SimpleNamespace(bounds=None, layers={"buildings": None, "roads": None, "water": None})

            items = veg.Vegetation(MD(), None).cell(key)
            lav = items.take(items.group == veg.G_LAVENDER)
            trees = items.take(items.kind == veg.KIND_TREE)
            self.assertGreater(len(lav), 40)
            self.assertEqual(len(trees), 1, "the 12 m tree outside the lavender parcel stays")


if __name__ == "__main__":
    unittest.main()
