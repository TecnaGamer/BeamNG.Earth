"""Road treatment must follow length-sampled context, independently of mapped width."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapcells
import mapgeom
import maprasters
import roads
import revisit
import surface
from test_roads import FRAME, features


def _beside(ll, d):
    """The points d m left and right of each vertex of a lon/lat line (the old vertex rule's, for comparison)."""
    m = np.array([111320.0 * np.cos(np.radians(ll[:, 1].mean())), 110540.0])
    t = np.gradient(ll * m, axis=0)
    t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    off = np.stack([-t[:, 1], t[:, 0]], axis=1) * d / m
    return ll + off, ll - off


class UrbanSamplingTest(unittest.TestCase):
    def cover_with_isolated_endpoint_sites(self, lat, lon):
        east, north, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.)
        pavement = np.abs(north) < 6.
        isolated_sites = ((east < 10.) | (east > 390.)) & (np.abs(north) < 45.)
        return pavement | isolated_sites

    def test_sparse_endpoint_sites_do_not_widen_a_country_road(self):
        f = features([[(0., 0.), (400., 0.)]], ["secondary"])
        sp = roads.road_specs(f, self.cover_with_isolated_endpoint_sites)[0]
        self.assertFalse(sp.urban)
        self.assertEqual(sp.width, roads.carriageway("secondary", "paved", False))
        self.assertGreater(roads.carriageway("secondary", "paved", True), sp.width)

    def test_source_vertex_clustering_does_not_change_classification(self):
        sparse = features([[(0., 0.), (400., 0.)]], ["secondary"])
        clustered = features([[(float(x), 0.) for x in [*range(10), 200, *range(391, 401)]]], ["secondary"])
        a = roads.road_specs(sparse, self.cover_with_isolated_endpoint_sites)[0]
        b = roads.road_specs(clustered, self.cover_with_isolated_endpoint_sites)[0]
        self.assertEqual((a.urban, a.width), (b.urban, b.width))
        self.assertFalse(a.urban)

    def test_continuously_developed_streets_keep_urban_parking_width(self):
        f = features([[(0., 0.), (400., 0.)]], ["residential"])
        sp = roads.road_specs(f, lambda lat, lon: np.ones(np.shape(lat), bool))[0]
        self.assertTrue(sp.urban)
        self.assertEqual(sp.width, roads.carriageway("residential", "paved", True))

    def test_mapped_width_does_not_force_a_city_street_rural(self):
        f = features([[(0., 0.), (100., 0.)]], ["residential"], widths=[(0, 7.25)])
        sp = roads.road_specs(f, lambda lat, lon: np.ones(np.shape(lat), bool))[0]
        self.assertTrue(sp.urban)
        self.assertEqual(sp.width, 7.25)
        rural = roads.road_specs(f, lambda lat, lon: np.zeros(np.shape(lat), bool))[0]
        self.assertFalse(rural.urban)
        self.assertEqual(rural.width, 7.25)

    def test_osm_width_also_preserves_context_and_exact_width(self):
        f = features([[(0., 0.), (100., 0.)]], ["residential"])
        f.attrs["osm_width"] = np.array([8.5])
        sp = roads.road_specs(f, lambda lat, lon: np.ones(np.shape(lat), bool))[0]
        self.assertTrue(sp.urban)
        self.assertEqual(sp.width, 8.5)

    def test_point_queries_are_batched_and_source_geometry_is_untouched(self):
        f = features([[(0., 0.), (400., 0.)], [(0., 100.), (400., 100.)]], ["residential"] * 2)
        original = f.lonlat.copy()
        calls = []
        def built(lat, lon):
            calls.append(len(lat))
            return np.ones(np.shape(lat), bool)
        roads.road_specs(f, built)
        self.assertEqual(len(calls), 1)
        self.assertGreater(calls[0], len(f.lonlat) * 3)
        np.testing.assert_array_equal(f.lonlat, original)

    def test_incomplete_land_cover_does_not_freeze_road_classification(self):
        f = features([[(0., 0.), (100., 0.)]], ["residential"])
        calls = []
        def arriving_cover(lat, lon):
            calls.append(True)
            if len(calls) == 1:
                revisit.fault()  # Surface.classes marks pending land-cover cells this way.
                return np.zeros(np.shape(lat), bool)
            return np.ones(np.shape(lat), bool)
        initial = roads.road_specs(f, arriving_cover)[0]
        ready = roads.road_specs(f, arriving_cover)[0]
        self.assertFalse(initial.urban)
        self.assertTrue(ready.urban)
        self.assertEqual(ready.width, roads.carriageway("residential", "paved", True))
        self.assertEqual(len(calls), 2)


class ActualHorseCreekTest(unittest.TestCase):
    def load_fixture(self, name):
        with np.load(Path(__file__).parent / "fixtures" / name, allow_pickle=False) as archive:
            data = {key: archive[key] for key in archive.files}
        f = mapgeom.Features.from_arrays("roads", data, "road/")
        layers = {}
        for i in range(int(data["cover_count"])):
            key = mapcells.CellKey(*[int(v) for v in data[f"cover{i}/key"]])
            meta = json.loads(str(data[f"cover{i}/meta"]))
            layers[key] = maprasters.Raster(data[f"cover{i}/data"], **meta)
        class Cached:
            def has(self, key, layer):
                return key in layers and layer == "landcover"
            def cell(self, key, **kwargs):
                return SimpleNamespace(layers={"landcover": layers.get(key)})
            def prefetch(self, *args, **kwargs):
                raise AssertionError("fixture queries must stay inside captured cells")
        return f, surface.Surface(Cached())

    def test_original_pixels_do_not_turn_road_into_a_parked_city_street(self):
        f, s = self.load_fixture("horse_creek_urban.npz")
        try:
            ll = f.lonlat
            left, right = _beside(ll, roads.URBAN_BESIDE_M)
            original = np.concatenate([ll, left, right])
            votes = s.urban(original[:, 1], original[:, 0])
            n = len(ll)
            self.assertEqual(float(np.mean(votes[:n])), 1.)
            self.assertEqual(float(np.mean(votes[n:2 * n] | votes[2 * n:])), 1., "old vertex rule falsely urban")
            sp = roads.road_specs(f, s.urban)[0]
            self.assertFalse(sp.urban)
            self.assertEqual(sp.width, 8.)
            self.assertEqual(roads.carriageway("secondary", "paved", True), 12.1)
            self.assertFalse(roads.has_sidewalks(sp))
        finally:
            s.close()

    def test_minor_roadside_patch_does_not_widen_tertiary_road(self):
        f, s = self.load_fixture("horse_creek_tertiary_urban.npz")
        try:
            with mock.patch.object(roads, "URBAN_BESIDE_SHARE", .20):
                permissive = roads._road_specs(f, s.urban, False)[0]
            with mock.patch.object(roads, "URBAN_BESIDE_SHARE", .35):
                conservative = roads._road_specs(f, s.urban, False)[0]
            self.assertTrue(permissive.urban)
            self.assertEqual(permissive.width, 10.8)
            self.assertFalse(conservative.urban)
            self.assertEqual(conservative.width, 6.2)
            self.assertFalse(roads.has_sidewalks(conservative))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
