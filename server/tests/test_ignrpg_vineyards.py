"""ignrpg & vegetation: French IGN RPG vineyard (`VRC`, `VRN`, `VRT`, `VRG`, group `21`) and olive / truffle (`OLI`,
`TRU`, group `23`) parcel fallback and Rust polygon deduplication against Overture `vineyard` / `orchard` polygons."""
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
import mapgeom  # noqa: E402
import vegetation as veg  # noqa: E402
from test_vegetation import Feats, Raster  # noqa: E402

TAVEL = (43.994226, 4.740771)


def _feature(fid, code_cultu="", code_group="", ring=()):
    coords = [[[float(lon), float(lat)] for lon, lat in ring]]
    return {
        "type": "Feature",
        "id": fid,
        "properties": {"id_parcel": fid, "code_cultu": code_cultu, "code_group": code_group},
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


class CoversAndParseTest(unittest.TestCase):
    def _ring(self, e, n):
        frame = geodesy.EnuFrame(*TAVEL)
        la, lo, _ = frame.enu_to_geodetic(np.asarray(e, float), np.asarray(n, float), np.zeros(len(e)))
        return frame, np.column_stack([lo, la])

    def test_ignrpg_in_managed_cache_and_france_coverage(self):
        self.assertIn("ignrpg", cache.MANAGED)
        for lat, lon in (TAVEL, (44.89, -0.15), (43.5297, 5.4474), (42.15, 9.15)):
            self.assertTrue(ignrpg.covers(mapcells.cell_of(lat, lon).bounds()), (lat, lon))
        for lat, lon in ((39.29, -76.61), (38.29, -122.28), (51.5074, -0.1278)):
            self.assertFalse(ignrpg.covers(mapcells.cell_of(lat, lon).bounds()), (lat, lon))

    def test_filters_vineyard_and_olive_codes_only(self):
        _, r1 = self._ring([0, 80, 80, 0, 0], [0, 0, 50, 50, 0])
        _, r2 = self._ring([100, 180, 180, 100, 100], [0, 0, 50, 50, 0])
        _, r3 = self._ring([200, 280, 280, 200, 200], [0, 0, 50, 50, 0])
        _, r4 = self._ring([300, 380, 380, 300, 300], [0, 0, 50, 50, 0])
        f_vrc = _feature("p1", "VRC", "21", r1)
        f_vrn = _feature("p2", "VRN", "", r2)
        f_oli = _feature("p3", "OLI", "23", r3)
        f_lav = _feature("p4", "LAV", "24", r4)
        f_bth = _feature("p5", "BTH", "1", r4)

        recs_a = ignrpg.Records(ignrpg.parse([f_vrc, f_vrn, f_oli, f_lav, f_bth]))
        recs_b = ignrpg.Records(ignrpg.parse([f_bth, f_oli, f_lav, f_vrn, f_vrc]))
        self.assertEqual(recs_a.n, 3)
        self.assertEqual(sorted(recs_a.kinds.tolist()), [ignrpg.KIND_VINEYARD, ignrpg.KIND_VINEYARD, ignrpg.KIND_OLIVE])
        np.testing.assert_array_equal(recs_a.seeds, recs_b.seeds)
        np.testing.assert_array_equal(recs_a.kinds, recs_b.kinds)


class DeduplicationAndSamplingTest(unittest.TestCase):
    def _setup_grid(self, rings):
        all_pts = np.vstack(rings)
        x, y = veg._to3857(all_pts[:, 0], all_pts[:, 1])
        pad, dx = 40.0, 2.0
        x0, y1 = float(x.min()) - pad, float(y.max()) + pad
        cols = int(np.ceil((float(x.max()) + pad - x0) / dx))
        rows = int(np.ceil((y1 - float(y.min()) + pad) / dx))
        return Raster(np.zeros((rows, cols), np.uint8), x0, y1, dx), np.zeros((rows, cols), bool)

    def test_vineyard_dedup_prevents_double_planting_and_fills_unmapped_parcels(self):
        frame = geodesy.EnuFrame(*TAVEL)
        # Field 1: mapped in BOTH Overture and IGN RPG (0..180 m East, 0..60 m North)
        la1, lo1, _ = frame.enu_to_geodetic(np.array([0, 180, 180, 0, 0.0]), np.array([0, 0, 60, 60, 0.0]), np.zeros(5))
        ring_mapped = np.column_stack([lo1, la1])
        # Field 2: unmapped in Overture, present ONLY in IGN RPG (220..400 m East, 0..60 m North)
        la2, lo2, _ = frame.enu_to_geodetic(np.array([220, 400, 400, 220, 220.0]), np.array([0, 0, 60, 60, 0.0]), np.zeros(5))
        ring_unmapped = np.column_stack([lo2, la2])

        ov_feats = mapgeom.build("land_use", np.array(["ov-vy-1"]), [(mapgeom.POLYGON, [[ring_mapped]])])
        ov_feats.attrs["class"] = np.array(["vineyard"], object)

        rpg_recs = ignrpg.Records(ignrpg.parse([
            _feature("rpg-dup", "VRC", "21", ring_mapped),
            _feature("rpg-new", "VRN", "21", ring_unmapped),
        ]))
        self.assertEqual(rpg_recs.n, 2)

        keep = ignrpg.dedup(rpg_recs, ov_feats, classes=("vineyard", "orchard"))
        self.assertEqual(int(keep.sum()), 1, "overlapping RPG parcel dropped, unmapped RPG parcel kept")

        chm, clear = self._setup_grid([ring_mapped, ring_unmapped])
        ov_only = veg.vineyard_items(ov_feats, chm, clear, None)
        combined = veg.vineyard_items(ov_feats, chm, clear, None, rpg=rpg_recs)

        e_ov, _, _ = frame.geodetic_to_enu(ov_only.lat, ov_only.lon, np.zeros(len(ov_only)))
        e_comb, _, _ = frame.geodetic_to_enu(combined.lat, combined.lon, np.zeros(len(combined)))

        # Field 1 (e < 190) must have the EXACT same count in combined as in ov_only (zero double-planting!)
        self.assertEqual(int((e_comb < 190.0).sum()), len(ov_only), "no double-planting on Overture-mapped vineyard")
        # Field 2 (e > 210) must be populated by the RPG fallback
        self.assertGreater(int((e_comb > 210.0).sum()), 30, "unmapped RPG vineyard parcel planted")
        self.assertTrue(np.all(combined.group == veg.G_VINE))

    def test_olive_parcels_dedup_against_overture_orchards_and_emit_olive_trees(self):
        frame = geodesy.EnuFrame(*TAVEL)
        la1, lo1, _ = frame.enu_to_geodetic(np.array([0, 80, 80, 0, 0.0]), np.array([0, 0, 60, 60, 0.0]), np.zeros(5))
        ring_orchard = np.column_stack([lo1, la1])
        la2, lo2, _ = frame.enu_to_geodetic(np.array([120, 200, 200, 120, 120.0]), np.array([0, 0, 60, 60, 0.0]), np.zeros(5))
        ring_olive = np.column_stack([lo2, la2])

        ov_feats = mapgeom.build("land_use", np.array(["ov-orch-1"]), [(mapgeom.POLYGON, [[ring_orchard]])])
        ov_feats.attrs["class"] = np.array(["orchard"], object)

        rpg_recs = ignrpg.Records(ignrpg.parse([
            _feature("oli-dup", "OLI", "23", ring_orchard),
            _feature("oli-new", "OLI", "23", ring_olive),
        ]))
        chm, clear = self._setup_grid([ring_orchard, ring_olive])
        mask = np.zeros(clear.shape, bool)
        olives = veg.olive_items(ov_feats, chm, clear, None, rpg=rpg_recs, mask=mask)
        self.assertGreater(len(olives), 40)
        self.assertTrue(mask.any())
        self.assertTrue(np.all(olives.kind == veg.KIND_TREE))
        self.assertTrue(np.all(olives.group == veg.G_MEDITERRANEAN))
        self.assertTrue(np.all((olives.height >= 3.6) & (olives.height <= 4.4)), "heights in olive range (4.3 m shape)")
        e_ol, _, _ = frame.geodetic_to_enu(olives.lat, olives.lon, np.zeros(len(olives)))
        self.assertTrue(np.all(e_ol > 110.0), "none inside the Overture-mapped orchard")


class SourceAndCellIntegrationTest(unittest.TestCase):
    def test_source_gzip_fetch_and_vegetation_cell_integration(self):
        key = mapcells.cell_of(*TAVEL)
        lon0, lat0, lon1, lat1 = key.bounds()
        x0, y1 = veg._to3857(lon0, lat1)
        x1, y0 = veg._to3857(lon1, lat0)
        dx = (x1 - x0) / 256
        h = np.zeros((256, 256), np.uint8)
        chm = Raster(h, x0, y1, dx, nodata=255)

        # Unmapped vineyard parcel and olive parcel inside the cell
        v_lo0, v_la0 = veg._to4326(x0 + 40 * dx, y1 - 90 * dx)
        v_lo1, v_la1 = veg._to4326(x0 + 100 * dx, y1 - 50 * dx)
        v_ring = [[v_lo0, v_la0], [v_lo1, v_la0], [v_lo1, v_la1], [v_lo0, v_la1], [v_lo0, v_la0]]

        o_lo0, o_la0 = veg._to4326(x0 + 130 * dx, y1 - 180 * dx)
        o_lo1, o_la1 = veg._to4326(x0 + 180 * dx, y1 - 130 * dx)
        o_ring = [[o_lo0, o_la0], [o_lo1, o_la0], [o_lo1, o_la1], [o_lo0, o_la1], [o_lo0, o_la0]]

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
            fx = FakeFetcher([
                _feature("tavel-vrc", "VRC", "21", v_ring),
                _feature("tavel-oli", "OLI", "23", o_ring),
            ])
            src = ignrpg.Source(d, fx)

            class MD:
                release = "t"
                ignrpg = src

                def cell(self, k, layers=None, kind="demand"):
                    if tuple(k)[1:] == (key.x, key.y):
                        return cd
                    return SimpleNamespace(bounds=None, layers={"buildings": None, "roads": None, "water": None})

            items = veg.Vegetation(MD(), None).cell(key)
            self.assertEqual(len(fx.calls), 1)
            self.assertEqual(fx.calls[0][3], {"Accept-Encoding": "gzip"})
            self.assertTrue(os.path.exists(src.path(key)))
            self.assertGreater(int((items.group == veg.G_VINE).sum()), 10)
            self.assertGreater(int(((items.group == veg.G_MEDITERRANEAN) & (items.kind == veg.KIND_TREE)).sum()), 15)


if __name__ == "__main__":
    unittest.main()
