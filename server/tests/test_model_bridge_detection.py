"""3DMR classification/orientation regressions, using synthetic meshes without network access."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings
import threedmr


def box(length, width, height=10):
    return np.array([[x, y, z] for x in (-length / 2, length / 2)
                     for y in (-width / 2, width / 2) for z in (0, height)], float)


class ModelBridgeDetectionTest(unittest.TestCase):
    def test_long_decks_at_every_bearing(self):
        for length, width in ((660, 40), (335, 64), (200, 20)):
            for angle in range(0, 180, 5):
                with self.subTest(length=length, angle=angle):
                    p = threedmr.rotate_xz(box(length, width), angle)
                    self.assertTrue(buildings._is_bridge(p + [1234, -5467, 8]))

    def test_duplicate_vertices_do_not_change_classification(self):
        p = threedmr.rotate_xz(box(335, 64), 35)
        self.assertTrue(buildings._is_bridge(np.r_[p, np.repeat(p[:1], 2000, axis=0)]))

    def test_metadata_identifies_short_and_curved_bridges(self):
        p = box(88, 60, 22)  # wide S-shaped footbridge's envelope fails any long/thin heuristic
        for info in ({"tags": {"bridge": "yes"}}, {"tags": {"bridge": "viaduct"}},
                     {"tags": {"man_made": "bridge"}}, {"tags": {"building": "bridge"}},
                     {"categories": ["art", "bridge"]}, {"categories": ["Bridge", "Foot Bridge"]},
                     {"categories": " BRIDGE "}):
            with self.subTest(info=info):
                self.assertTrue(buildings._is_bridge(p, info))
        self.assertFalse(buildings._is_bridge(p))

    def test_negative_tag_overrides_category_and_shape(self):
        for value in ("no", "false", "0"):
            self.assertFalse(buildings._is_bridge(box(600, 20),
                             {"tags": {"bridge": value}, "categories": ["bridge"]}))

    def test_buildings_small_models_and_bad_geometry_are_not_inferred_bridges(self):
        for p in (box(35, 30, 204), box(150, 150, 20), box(80, 8), np.zeros((0, 3)),
                  np.array([[0, 0, 0], [1, 1, 0], [np.nan, 0, 0]])):
            self.assertFalse(buildings._is_bridge(p))

    def test_metadata_cache_read_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("threedmr._fetch_retry") as fetch:
            self.assertEqual(threedmr.model_info(8, d, cached_only=True), {})
            path = Path(d) / "threedmr_cache" / "info_8.json"
            path.parent.mkdir()
            info = {"tags": {"bridge": "yes"}, "categories": ["bridge"]}
            path.write_text(json.dumps(info))
            self.assertEqual(threedmr.model_info(8, d, cached_only=True), info)
            for bad in ("{", "null", "[]"):
                path.write_text(bad)
                self.assertEqual(threedmr.model_info(8, d, cached_only=True), {})
            fetch.assert_not_called()

    def test_bridge_preserves_uploaded_orientation_but_building_still_aligns(self):
        for p, info, expected_yaw_calls in ((box(660, 40), {}, 0),
                                            (box(88, 60), {"categories": ["bridge"]}, 0),
                                            (box(35, 30, 204), {}, 1)):
            with self.subTest(info=info, extent=p.max(0)), tempfile.TemporaryDirectory() as d:
                mesh = (p, None, np.empty((0, 3), dtype=int), None, [], np.empty(0, dtype=int))
                with mock.patch("threedmr.fetch_model", return_value=b"glb"), \
                     mock.patch("threedmr.gltf_mesh", return_value=mesh), \
                     mock.patch("threedmr.model_info", return_value=info), \
                     mock.patch("threedmr.osm_yaw", return_value=0) as yaw:
                    got = buildings._model_mesh({"id": 9999, "latlon": (-32, 116)}, d)
                np.testing.assert_array_equal(got[0], p)
                self.assertEqual(yaw.call_count, expected_yaw_calls)


if __name__ == "__main__":
    unittest.main()
