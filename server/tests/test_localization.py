import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from localization import Translator, configure
from apppaths import ask_path

spec = importlib.util.spec_from_file_location("check_locales", ROOT / "tools/check_locales.py")
check_locales = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_locales)


class LocalizationTests(unittest.TestCase):
    def test_launcher_uses_polish_and_falls_back_to_english(self):
        self.assertEqual(Translator("pl_PL.UTF-8").t("launcher.label.worldServer"), "Serwer świata")
        self.assertEqual(Translator("fr-CA").t("launcher.label.worldServer"), "World server")
        self.assertEqual(Translator("pl").t("launcher.update.downloaded", version="1.2.3"),
                         "Pobrano BeamNG.Earth 1.2.3")

    def test_path_prompt_is_localized_and_keeps_input_unchanged(self):
        configure("pl-PL")
        self.addCleanup(configure, "en")
        user_path = r"C:\Gry\Łódź"
        ask = Mock(return_value=user_path)
        accept = Mock(return_value=user_path)
        self.assertEqual(ask_path("Wybierz folder", "folder", accept, ask=ask), user_path)
        self.assertIn("Możesz też wpisać lub wkleić ścieżkę", ask.call_args.args[0])
        accept.assert_called_once_with(user_path)

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
