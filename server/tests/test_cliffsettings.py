"""The two categories are independent and cannot share cached generated content."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
import unittest

from cliffsettings import CliffSettings


class CliffSettingsTests(unittest.TestCase):
    def test_default_is_off(self):
        self.assertFalse(CliffSettings.from_env({}).enabled)

    def test_independent_controls_share_one_opt_in(self):
        keys = set()
        for natural in (False, True):
            for cuts in (False, True):
                saved = dict(naturalCliffs=natural, cliffRoadCuts=cuts)
                settings = CliffSettings.from_env({"BNE_CLIFFS": "1"}, saved)
                self.assertEqual((settings.natural, settings.road_cuts), (natural, cuts))
                self.assertFalse(CliffSettings.from_env({}, saved).enabled)
                keys.add(settings.cache_key)
        self.assertEqual(len(keys), 4)

    def test_legacy_flag_cannot_enable_cliffs(self):
        self.assertFalse(CliffSettings.from_env({"BNE_CLIFF_ROAD_CUTS": "1"}).enabled)

    def test_detail_and_startup_snapshot(self):
        import json
        keys = set()
        for detail in ("low", "medium", "normal", "high", "extreme"):
            env = {"BNE_CLIFFS": "1", "BNGEARTH_START_SETTINGS": json.dumps({"values": {
                "naturalCliffs": False, "cliffRoadCuts": True, "cliffDetail": detail}})}
            settings = CliffSettings.from_env(env)
            self.assertFalse(settings.natural)
            self.assertTrue(settings.road_cuts)
            self.assertEqual(settings.detail, detail)
            keys.add(settings.cache_key)
        self.assertEqual(len(keys), 5)
        low = CliffSettings(True, True, "low").profile
        normal = CliffSettings(True, True).profile
        self.assertLess(low[0], normal[0])
        self.assertGreater(low[2], normal[2])
        self.assertEqual(CliffSettings.from_env({"BNE_CLIFFS": "1"}, {"quality": "ultra"}).detail, "high")
        self.assertEqual(CliffSettings.from_env({"BNE_CLIFFS": "1"}, {"cliffDetail": "invalid"}).detail, "normal")

    def test_restart_snapshot_and_gate(self):
        import os
        import json
        import srvrestart
        from unittest.mock import patch
        import cliffsettings
        saved = dict(naturalCliffs=False, cliffRoadCuts=True, cliffDetail="medium", cliffTextures="4k")
        env = {"BNE_CLIFFS": "1"}
        srvrestart.supervisor_env(env, saved)
        self.assertEqual(CliffSettings.from_env(env), CliffSettings(False, True, "medium", "4k"))
        self.assertNotIn("cliffDetail", srvrestart.view(saved, {}))
        self.assertNotIn("cliffTextures", srvrestart.view(saved, {}))
        self.assertEqual(srvrestart.view(saved, env)["cliffDetail"], "medium")
        self.assertEqual(srvrestart.view(saved, env)["cliffTextures"], "4k")
        with patch.dict(os.environ, env, clear=True), patch.object(cliffsettings, "SETTINGS"):
            cliffsettings.capture_startup()
            self.assertEqual(cliffsettings.SETTINGS, CliffSettings(False, True, "medium", "4k"))
            self.assertEqual(json.loads(os.environ["BNGEARTH_START_SETTINGS"])["values"]["cliffDetail"], "medium")

    def test_disabled_build_does_not_touch_inputs(self):
        import cliffrocks
        class Forbidden:
            def __getattr__(self, name):
                raise AssertionError("disabled cliffs accessed " + name)
        self.assertIsNone(cliffrocks.build(Forbidden(), None, Forbidden(), Forbidden(), settings=CliffSettings()))

    def test_explicit_opt_in(self):
        for value in ("1", "on", "TRUE", " true "):
            self.assertTrue(CliffSettings.from_env({"BNE_CLIFFS": value}).natural)
        for value in ("", "0", "off", "false", "typo"):
            self.assertFalse(CliffSettings.from_env({"BNE_CLIFFS": value}).natural)
