"""Unit tests for Layer B Effort & Performance Tradeoff Controller (server/effort.py)."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import addons  # noqa: E402
import buildings  # noqa: E402
import earthd  # noqa: E402
import effort  # noqa: E402
import revisit  # noqa: E402
import vegetation  # noqa: E402


class EffortProfilesTest(unittest.TestCase):
    def setUp(self):
        addons.reset_registry(auto_discover_reference=True)
        effort.reset_effort_controller("balanced")

    def tearDown(self):
        effort.reset_effort_controller("balanced")
        addons.reset_registry(auto_discover_reference=True)

    def test_all_five_standardized_profiles_match_spec(self):
        low = effort.set_profile("low")
        self.assertEqual(low.profile, "low")
        self.assertEqual(low.effective_profile, "low")
        self.assertEqual(low.terrain_square_size, 2.0)
        self.assertEqual(low.crop_row_spacing_mult, 2.5)
        self.assertEqual(low.tree_spacing_mult, 1.8)
        self.assertEqual(low.aerial_blob_max_trees, 0)
        self.assertFalse(low.roof_ortho_sampling)
        self.assertEqual(low.building_col_rings, 1)
        self.assertFalse(low.far_geotiles)
        self.assertEqual(low.urgency_pause_s, 4.0)

        bal = effort.set_profile("balanced")
        self.assertEqual(bal.effective_profile, "balanced")
        self.assertEqual(bal.terrain_square_size, 1.0)
        self.assertEqual(bal.crop_row_spacing_mult, 1.4)
        self.assertEqual(bal.tree_spacing_mult, 1.2)
        self.assertEqual(bal.aerial_blob_max_trees, 120)
        self.assertTrue(bal.roof_ortho_sampling)
        self.assertEqual(bal.building_col_rings, 2)
        self.assertTrue(bal.far_geotiles)
        self.assertEqual(bal.urgency_pause_s, 3.0)

        high = effort.set_profile("high")
        self.assertEqual(high.effective_profile, "high")
        self.assertEqual(high.terrain_square_size, 1.0)
        self.assertEqual(high.crop_row_spacing_mult, 1.0)
        self.assertEqual(high.tree_spacing_mult, 1.0)
        self.assertEqual(high.aerial_blob_max_trees, 350)
        self.assertTrue(high.roof_ortho_sampling)
        self.assertEqual(high.building_col_rings, 2)
        self.assertTrue(high.far_geotiles)
        self.assertEqual(high.urgency_pause_s, 2.0)

        ultra = effort.set_profile("ultra")
        self.assertEqual(ultra.effective_profile, "ultra")
        self.assertEqual(ultra.terrain_square_size, 1.0)
        self.assertEqual(ultra.crop_row_spacing_mult, 0.85)
        self.assertEqual(ultra.tree_spacing_mult, 0.85)
        self.assertEqual(ultra.aerial_blob_max_trees, 600)
        self.assertTrue(ultra.roof_ortho_sampling)
        self.assertEqual(ultra.building_col_rings, 3)
        self.assertTrue(ultra.far_geotiles)
        self.assertEqual(ultra.urgency_pause_s, 1.5)

        auto = effort.set_profile("auto_fps", initial_auto_tier="high")
        self.assertEqual(auto.profile, "auto_fps")
        self.assertEqual(auto.effective_profile, "high")

    def test_auto_fps_governor_hysteresis_requires_3_consecutive_reports(self):
        effort.set_profile("auto_fps", initial_auto_tier="ultra")
        ctrl = effort.get_effort_controller()
        self.assertEqual(ctrl.effective_profile, "ultra")

        # 2 low-FPS reports do NOT drop tier yet
        effort.report_telemetry(fps_p50=42.0, fps_p95=30.0, frame_ms_p95=33.3, target_fps=60.0)
        self.assertEqual(ctrl.effective_profile, "ultra")
        effort.report_telemetry(fps_p50=44.0, fps_p95=32.0, frame_ms_p95=31.0, target_fps=60.0)
        self.assertEqual(ctrl.effective_profile, "ultra")

        # A normal frame resets the down streak (no thrashing)
        effort.report_telemetry(fps_p50=60.0, fps_p95=58.0, frame_ms_p95=17.0, target_fps=60.0)
        self.assertEqual(ctrl.governor.down_streak, 0)
        self.assertEqual(ctrl.effective_profile, "ultra")

        # Now 3 consecutive low-FPS reports step ultra -> high -> balanced -> low
        for expected_tier in ("high", "balanced", "low"):
            for i in range(3):
                b = effort.report_telemetry(fps_p50=38.0, fps_p95=28.0, frame_ms_p95=35.0, target_fps=60.0)
                if i < 2:
                    self.assertNotEqual(b.effective_profile, expected_tier)
            self.assertEqual(b.effective_profile, expected_tier)

        # Clamped at low
        for _ in range(3):
            b = effort.report_telemetry(fps_p50=25.0, fps_p95=20.0, frame_ms_p95=50.0, target_fps=60.0)
        self.assertEqual(b.effective_profile, "low")

        # Now 3 consecutive high-FPS reports step low -> balanced -> high -> ultra
        for expected_tier in ("balanced", "high", "ultra"):
            for i in range(3):
                b = effort.report_telemetry(fps_p50=90.0, fps_p95=75.0, frame_ms_p95=13.0, target_fps=60.0)
                if i < 2:
                    self.assertNotEqual(b.effective_profile, expected_tier)
            self.assertEqual(b.effective_profile, expected_tier)

    def test_vegetation_and_building_scaling_by_effort_budget(self):
        poly = np.array([[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [0.0, 50.0]], dtype=np.float64)

        effort.set_profile("low")
        crop_low = vegetation._structured_crop("vineyard", lat=44.01, lon=4.70, poly=poly)
        blobs_low = vegetation._aerial_blob_trees(list(range(500)))
        patch = np.full((9, 9, 3), [182, 108, 76], dtype=np.uint8)
        roof_low = buildings._sample_roof_ortho(patch)

        effort.set_profile("ultra")
        crop_ultra = vegetation._structured_crop("vineyard", lat=44.01, lon=4.70, poly=poly)
        blobs_ultra = vegetation._aerial_blob_trees(list(range(800)))
        roof_ultra = buildings._sample_roof_ortho(patch)

        self.assertAlmostEqual(crop_low["row_spacing"], 2.2 * 2.5, places=5)
        self.assertAlmostEqual(crop_ultra["row_spacing"], 2.2 * 0.85, places=5)
        self.assertGreater(len(crop_ultra["points"]), len(crop_low["points"]) * 4)

        self.assertEqual(len(blobs_low), 0)
        self.assertEqual(len(blobs_ultra), 600)

        self.assertIsNone(roof_low, "low profile disables roof_ortho_sampling")
        self.assertIsNotNone(roof_ultra, "ultra profile enables roof_ortho_sampling")

        # Disabling ign_orthophoto_20cm in Layer A AddonRegistry also gates _sample_roof_ortho even on ultra
        addons.get_registry().set_source_enabled("ign_orthophoto_20cm", False)
        self.assertIsNone(buildings._sample_roof_ortho(patch))

    def test_earthd_effort_api_and_revisit_cache_tag(self):
        code, st1 = earthd.handle_api_request("POST", "/api/effort", {"profile": "low"})
        self.assertEqual(code, 200)
        self.assertEqual(st1["effective_profile"], "low")
        k_low = revisit.key("roads", 44.01, 4.70, 1, 2)

        code2, st2 = earthd.handle_api_request("POST", "/api/effort", {"profile": "ultra"})
        self.assertEqual(code2, 200)
        self.assertEqual(st2["effective_profile"], "ultra")
        self.assertTrue(st2["cache_invalidated"])
        k_ultra = revisit.key("roads", 44.01, 4.70, 1, 2)
        self.assertNotEqual(k_low, k_ultra)

        code_bad, err = earthd.handle_api_request("POST", "/api/effort", {"profile": "nonexistent"})
        self.assertEqual(code_bad, 400)
        self.assertIn("error", err)


if __name__ == "__main__":
    unittest.main()
