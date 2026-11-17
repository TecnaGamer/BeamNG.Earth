"""The two categories are independent and cannot share cached generated content."""
import unittest

from cliffsettings import CliffSettings


class CliffSettingsTests(unittest.TestCase):
    def test_default_is_off(self):
        self.assertFalse(CliffSettings.from_env({}).enabled)

    def test_independent_flags_and_cache_keys(self):
        keys = set()
        for natural in (False, True):
            for road_cuts in (False, True):
                settings = CliffSettings.from_env({
                    "BNE_CLIFFS": str(int(natural)),
                    "BNE_CLIFF_ROAD_CUTS": str(int(road_cuts)),
                })
                self.assertEqual((settings.natural, settings.road_cuts), (natural, road_cuts))
                self.assertEqual(settings.enabled, natural or road_cuts)
                keys.add(settings.cache_key)
        self.assertEqual(len(keys), 4)

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
