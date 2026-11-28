"""season.amounts_for's "doy:<n>" (the mod's settings Season date 'game': message season_date) and Server.set_season_day."""
import datetime
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import season  # noqa: E402


class SeasonDateTest(unittest.TestCase):
    def test_doy_mode(self):
        today = datetime.date(2026, 11, 23)
        aug = season.amounts_for("doy:227", 50.0, today=today)
        self.assertEqual(aug, season.season_amounts(datetime.date(2026, 8, 15), 50.0))
        self.assertEqual(season.amounts_for("auto", 50.0, today=today), season.season_amounts(today, 50.0))
        self.assertLess(aug["fall"], season.amounts_for("auto", 50.0, today=today)["fall"] + 1e-9)
        self.assertEqual(season.amounts_for("doy:x", 50.0, today=today), season.season_amounts(today, 50.0))

    def test_set_season_day(self):
        import earthd
        s = SimpleNamespace(imagery=SimpleNamespace(season_mode="auto"), _season_base=None)
        f = earthd.Server.set_season_day
        self.assertTrue(f(s, 225))
        self.assertEqual(s.imagery.season_mode, "doy:225")
        self.assertFalse(f(s, 225), "unchanged")
        self.assertTrue(f(s, None))
        self.assertEqual(s.imagery.season_mode, "auto", "the server's own date again")
        fixed = SimpleNamespace(imagery=SimpleNamespace(season_mode="fall"), _season_base=None)
        self.assertFalse(f(fixed, 225))
        self.assertEqual(fixed.imagery.season_mode, "fall", "--season fall stays")


if __name__ == "__main__":
    unittest.main()
