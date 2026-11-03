"""Unit tests for Native BeamNG Addon Registry, Compatibility Gate & Ground Truth Layer A (server/addons.py)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import addons  # noqa: E402
import earthd  # noqa: E402
import mapdata  # noqa: E402
import package_addon  # noqa: E402
import revisit  # noqa: E402


def _make_addon_payload(
    addon_id: str = "custom_region",
    version: str = "1.0.0",
    priority: int = 100,
    min_version: str = "1.7.0",
    max_version: str = "1.8.x",
    abi_min: int = 113,
    abi_max: int = 160,
    revisit_schema: int = 2,
    plans_format: int = 1,
    row_spacing_m: float = 2.0,
    roof_rgb: list[int] | None = None,
) -> dict:
    return {
        "id": addon_id,
        "manifest": {
            "schema_version": 1,
            "id": addon_id,
            "name": f"Test Addon {addon_id}",
            "version": version,
            "priority": priority,
            "engine_compat": {
                "min_version": min_version,
                "max_version": max_version,
                "earthfast_abi": {"min": abi_min, "max": abi_max},
                "revisit_schema": revisit_schema,
                "plans_format": plans_format,
            },
            "approval": {
                "channel": "community",
                "publisher": "tester",
            },
            "capabilities": ["data_sources", "biomes", "structured_crops", "roof_palettes"],
        },
        "data_sources": [
            {
                "id": "ign_rpg_parcels",
                "name": "Test RPG Parcels",
                "type": "wfs_geojson",
                "bbox": [43.0, 4.0, 44.6, 6.8],
                "default_enabled": True,
            },
            {
                "id": "ign_orthophoto_20cm",
                "name": "Test Ortho",
                "type": "wmts_ortho",
                "bbox": [43.0, 4.0, 44.6, 6.8],
                "default_enabled": True,
            },
        ],
        "biomes": [
            {
                "id": f"{addon_id}_biome",
                "name": "Test Biome",
                "bbox": [43.0, 4.0, 44.6, 6.8],
                "structured_crops": {
                    "vineyard": {
                        "pattern": "parallel_rows",
                        "row_spacing_m": row_spacing_m,
                        "point_spacing_m": 1.1,
                    }
                },
                "building_palettes": {
                    "roof_style": {
                        "style_name": "mediterranean",
                        "default_rgb": roof_rgb or [180, 100, 70],
                    }
                },
            }
        ],
    }


class AddonCompatibilityTest(unittest.TestCase):
    def setUp(self):
        addons.reset_registry(auto_discover_reference=True)

    def tearDown(self):
        addons.reset_registry(auto_discover_reference=True)

    def test_semver_and_suffix_parsing(self):
        self.assertEqual(addons.parse_semver("1.7.0"), (1, 7, 0))
        self.assertEqual(addons.parse_semver("1.7.0-beta"), (1, 7, 0))
        self.assertEqual(addons.parse_semver("v1.7.2-dev-experimental+g1234"), (1, 7, 2))

    def test_compat_accepts_valid_semver_and_wildcard_and_abi(self):
        payload = _make_addon_payload(min_version="1.7.0", max_version="1.8.x", abi_min=113, abi_max=160)
        for ver in ("1.7.0", "1.7.0-beta", "1.7.5-dev-experimental", "1.8.9"):
            res = addons.check_addon_compat(payload, engine_version=ver, earthfast_abi=142)
            self.assertTrue(res, f"Should accept {ver}: {res.reasons}")
            self.assertTrue(res.compatible)
            self.assertEqual(res.status, "compatible")

    def test_compat_rejects_version_and_abi_mismatch(self):
        payload = _make_addon_payload(min_version="1.7.0", max_version="1.7.x", abi_min=113, abi_max=160)
        too_old = addons.check_addon_compat(payload, engine_version="1.6.9", earthfast_abi=142)
        self.assertFalse(too_old.compatible)
        self.assertIn("min_version", "; ".join(too_old.reasons))

        too_new = addons.check_addon_compat(payload, engine_version="1.8.0-beta", earthfast_abi=142)
        self.assertFalse(too_new.compatible)
        self.assertIn("max_version", "; ".join(too_new.reasons))

        bad_abi_low = addons.check_addon_compat(payload, engine_version="1.7.0-beta", earthfast_abi=110)
        self.assertFalse(bad_abi_low.compatible)
        self.assertIn("earthfast ABI", "; ".join(bad_abi_low.reasons))

        bad_abi_high = addons.check_addon_compat(payload, engine_version="1.7.0-beta", earthfast_abi=199)
        self.assertFalse(bad_abi_high.compatible)
        self.assertIn("earthfast ABI", "; ".join(bad_abi_high.reasons))

        bad_revisit = addons.check_addon_compat(payload, engine_version="1.7.0-beta", earthfast_abi=142, revisit_schema=99)
        self.assertFalse(bad_revisit.compatible)
        self.assertIn("revisit_schema", "; ".join(bad_revisit.reasons))


class AddonApprovalPolicyTest(unittest.TestCase):
    def test_reference_provence_is_approved_in_all_modes(self):
        repo_root = Path(__file__).resolve().parents[2]
        prov_dir = repo_root / "addons" / "beamng_earth_provence" / "beamng_earth_addons" / "provence"
        prov_payload = addons.load_addon_from_dir(prov_dir)
        for policy in ("strict", "warn", "dev"):
            res = addons.verify_addon_approval(prov_payload, policy=policy)
            self.assertTrue(res.allowed)
            self.assertTrue(res.approved)
            self.assertEqual(res.status, "active")
            self.assertEqual(res.approval_status, "approved")

    def test_unapproved_addon_under_strict_warn_and_dev_policies(self):
        unapproved = _make_addon_payload(addon_id="unverified_pack")

        strict_res = addons.verify_addon_approval(unapproved, policy="strict")
        self.assertFalse(strict_res.allowed)
        self.assertEqual(strict_res.status, "rejected_unapproved")
        self.assertEqual(strict_res.approval_status, "rejected_unapproved")

        warn_res = addons.verify_addon_approval(unapproved, policy="warn")
        self.assertTrue(warn_res.allowed)
        self.assertEqual(warn_res.status, "active")
        self.assertEqual(warn_res.approval_status, "unverified_warn")
        self.assertGreater(len(warn_res.warnings), 0)

        dev_res = addons.verify_addon_approval(unapproved, policy="dev")
        self.assertTrue(dev_res.allowed)
        self.assertEqual(dev_res.status, "active")
        self.assertEqual(dev_res.approval_status, "dev_override")

    def test_tampered_provence_manifest_is_rejected_in_strict_mode(self):
        repo_root = Path(__file__).resolve().parents[2]
        prov_dir = repo_root / "addons" / "beamng_earth_provence" / "beamng_earth_addons" / "provence"
        tampered = copy.deepcopy(addons.load_addon_from_dir(prov_dir))
        tampered["biomes"][0]["structured_crops"]["vineyard"]["row_spacing_m"] = 9.99
        strict_res = addons.verify_addon_approval(tampered, policy="strict")
        self.assertFalse(strict_res.allowed)
        self.assertEqual(strict_res.status, "rejected_unapproved")


class AddonRegistryAndGroundTruthTest(unittest.TestCase):
    def setUp(self):
        self.reg = addons.reset_registry(auto_discover_reference=False)

    def tearDown(self):
        addons.reset_registry(auto_discover_reference=True)

    def test_priority_merging_and_spatial_queries(self):
        low_prio = _make_addon_payload(addon_id="base_provence", priority=100, row_spacing_m=2.2, roof_rgb=[182, 108, 76])
        high_prio = _make_addon_payload(addon_id="local_tavel_hd", priority=250, row_spacing_m=1.65, roof_rgb=[200, 95, 65])
        self.reg.sync_addons([low_prio, high_prio], policy="warn")

        # Tavel checkpoint (44.011161, 4.701794) is inside [43.0, 4.0, 44.6, 6.8]
        biomes = self.reg.get_matching_biomes(44.011161, 4.701794)
        self.assertEqual(len(biomes), 2)
        self.assertEqual(biomes[0]["addon_id"], "local_tavel_hd")
        self.assertEqual(biomes[1]["addon_id"], "base_provence")

        crop_spec = self.reg.get_crop_row_spec("vineyard", lat=44.011161, lon=4.701794)
        self.assertIsNotNone(crop_spec)
        self.assertAlmostEqual(crop_spec["row_spacing_m"], 1.65)
        self.assertEqual(crop_spec["addon_id"], "local_tavel_hd")

        roof = self.reg.get_roof_style(44.011161, 4.701794)
        self.assertIsNotNone(roof)
        self.assertEqual(roof["default_rgb"], [200, 95, 65])

        # Disabling higher-priority addon falls back to lower-priority addon
        self.reg.set_addon_enabled("local_tavel_hd", False)
        crop_spec2 = self.reg.get_crop_row_spec("vineyard", lat=44.011161, lon=4.701794)
        self.assertAlmostEqual(crop_spec2["row_spacing_m"], 2.2)
        self.assertEqual(crop_spec2["addon_id"], "base_provence")

        # Outside bbox (e.g. Baltimore 39.29, -76.61) returns empty
        self.assertEqual(self.reg.get_matching_biomes(39.29, -76.61), [])
        self.assertIsNone(self.reg.get_roof_style(39.29, -76.61))

    def test_source_toggling_and_cache_fingerprint_determinism(self):
        a1 = _make_addon_payload(addon_id="alpha", priority=100)
        a2 = _make_addon_payload(addon_id="beta", priority=200)

        self.reg.sync_addons([a1, a2], policy="warn")
        fp1 = self.reg.cache_fingerprint()
        self.assertEqual(len(fp1), 12)

        # Registering in reverse order yields the exact same deterministic 12-hex-char fingerprint
        self.reg.sync_addons([a2, a1], policy="warn")
        fp2 = self.reg.cache_fingerprint()
        self.assertEqual(fp1, fp2)

        # Toggling a data source changes cache_fingerprint and is_source_enabled
        self.assertTrue(self.reg.is_source_enabled("ign_rpg_parcels", lat=44.0, lon=4.7))
        self.reg.set_source_enabled("ign_rpg_parcels", False)
        self.assertFalse(self.reg.is_source_enabled("ign_rpg_parcels", lat=44.0, lon=4.7))
        fp3 = self.reg.cache_fingerprint()
        self.assertNotEqual(fp1, fp3)

    def test_mapdata_gates_ign_rpg_parcels_on_registry(self):
        self.reg.sync_addons([_make_addon_payload(addon_id="provence_test", priority=100)], policy="warn")
        calls = []

        class DummyIgn:
            def cell(self, key, bounds=None, kind="demand"):
                calls.append(key)
                return "fr_cell_data"

            def join_buildings(self, feats, key, frame=None, kind="demand"):
                calls.append(("bld", key))
                return "joined_blds"

        md = object.__new__(mapdata.MapData)
        md.ign_fr = DummyIgn()

        self.assertEqual(md._fr_parcels((14, 8400, 5900)), "fr_cell_data")
        self.assertEqual(len(calls), 1)

        self.reg.set_source_enabled("ign_rpg_parcels", False)
        self.assertIsNone(md._fr_parcels((14, 8400, 5900)))
        self.assertEqual(len(calls), 1, "Should not call ign_fr when ign_rpg_parcels is disabled")

    def test_earthd_http_routes_and_package_addon_zip(self):
        repo_root = Path(__file__).resolve().parents[2]
        prov_dir = repo_root / "addons" / "beamng_earth_provence"
        with tempfile.TemporaryDirectory() as tmp:
            zip_path, digest = package_addon.package_addon(prov_dir, tmp)
            self.assertTrue(zip_path.is_file())
            loaded = addons.load_addons_from_zip(zip_path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(addons.compute_addon_digest(loaded[0]), digest)

            code, status = earthd.handle_api_request(
                "POST",
                "/api/addons/sync",
                {"addons": loaded, "policy": "strict"},
            )
            self.assertEqual(code, 200)
            self.assertEqual(status["active"], ["provence"])
            self.assertEqual(status["rejected"], [])

            code_get, status_get = earthd.handle_api_request("GET", "/api/addons")
            self.assertEqual(code_get, 200)
            self.assertEqual(status_get["cache_fingerprint"], status["cache_fingerprint"])

            # Toggling source via /api/addons/sync invalidates revisit runtime cache tag
            rk_before = revisit.key("roads", 44.01, 4.70, 0, 0)
            earthd.handle_api_request(
                "POST",
                "/api/addons/sync",
                {"addon_id": "provence", "source_id": "ign_orthophoto_20cm", "enabled": False},
            )
            rk_after = revisit.key("roads", 44.01, 4.70, 0, 0)
            self.assertNotEqual(rk_before, rk_after)


if __name__ == "__main__":
    unittest.main()
