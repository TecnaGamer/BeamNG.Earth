import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from localization import Translator

spec = importlib.util.spec_from_file_location("check_locales", ROOT / "tools/check_locales.py")
check_locales = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_locales)


class LocalizationTests(unittest.TestCase):
    def test_launcher_uses_polish_and_falls_back_to_english(self):
        self.assertEqual(Translator("pl_PL.UTF-8").t("launcher.label.worldServer"), "Serwer świata")
        self.assertEqual(Translator("fr-CA").t("launcher.label.worldServer"), "World server")
        self.assertEqual(Translator("pl").t("launcher.update.downloaded", version="1.2.3"),
                         "Pobrano BeamNG.Earth 1.2.3")

    def test_damaged_catalog_falls_back_and_validator_reports_it(self):
        with tempfile.TemporaryDirectory() as target:
            root = Path(target)
            (root / "catalogs.json").write_text(json.dumps({
                "en": {"name": "English", "direction": "ltr"},
                "pl-PL": {"name": "Polski", "direction": "ltr", "aliases": ["pl"]},
            }), encoding="utf8")
            (root / "en.json").write_text('{"launcher.test": "Hello {name}"}', encoding="utf8")
            (root / "pl-PL.json").write_text('{"launcher.test":"Witaj {user}"}', encoding="utf8")
            self.assertEqual(Translator("pl", root).t("launcher.test", name="Łódź"), "Hello Łódź")
            report = check_locales.report(root)
            self.assertEqual(report["locales"]["pl-PL"]["missing"], ["launcher.test"])
            self.assertTrue(any("placeholders differ" in issue for issue in report["issues"]))
            (root / "pl-PL.json").write_text('{"launcher.test":42,"launcher.test":""}', encoding="utf8")
            self.assertTrue(any("duplicate key" in issue for issue in check_locales.report(root)["issues"]))
            (root / "pl-PL.json").write_text("{", encoding="utf8")
            self.assertEqual(Translator("pl", root).t("launcher.test"), "Hello —")


if __name__ == "__main__":
    unittest.main()
