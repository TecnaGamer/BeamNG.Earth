import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from localization import Translator, configure, _system_locale
from apppaths import ask_path

spec = importlib.util.spec_from_file_location("check_locales", ROOT / "tools/check_locales.py")
check_locales = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_locales)


class LocalizationTests(unittest.TestCase):
    def test_launcher_uses_polish_and_falls_back_to_english(self):
        self.assertEqual(Translator("pl_PL.UTF-8").t("launcher.label.worldServer"), "Serwer świata")
        self.assertEqual(Translator("sv-SE").t("launcher.label.worldServer"), "World server")
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

    def test_launcher_rejects_non_finite_and_non_string_arguments(self):
        translator = Translator(" pl-PL ")
        self.assertEqual(translator.t("launcher.update.downloaded", version=float("nan")), "Pobrano BeamNG.Earth —")
        self.assertEqual(translator.t("launcher.update.downloaded", version=True), "Pobrano BeamNG.Earth —")
        self.assertEqual(translator.t([]), "—")

    def test_windows_prefers_the_display_language(self):
        kernel = Mock()
        kernel.GetUserDefaultUILanguage.return_value = 0x0415
        with patch.dict("os.environ", clear=True), patch("localization.os.name", "nt"), \
                patch("ctypes.windll", Mock(kernel32=kernel), create=True):
            self.assertEqual(Translator(_system_locale()).language, "pl-PL")
        kernel.GetUserDefaultLocaleName.assert_not_called()

    def test_shipped_game_ids_and_native_catalogs_cover_all_official_languages(self):
        manifest = json.loads((check_locales.CATALOGS / "catalogs.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 13)
        for tag, meta in manifest.items():
            with self.subTest(tag=tag, game=meta["gameLocale"]):
                self.assertEqual(Translator(meta["gameLocale"]).language, tag)
                self.assertEqual(Translator(meta["gameLocale"].lower().replace("_", "-")).language, tag)
        for alias, tag in (("es_MX", "es-419"), ("pt", "pt-BR"),
                           ("fr_CA", "fr-FR"), ("zh_TW", "zh-Hant"), ("zh_CN", "zh-Hans")):
            self.assertEqual(Translator(alias).language, tag)
        issues = []
        self.assertEqual(check_locales.host_report(manifest, issues),
                         {meta["gameLocale"]: 22 for meta in manifest.values()})
        self.assertEqual(issues, [])

    def test_partial_regional_catalog_falls_back_to_english_instead_of_sibling(self):
        with tempfile.TemporaryDirectory() as target:
            root = Path(target)
            for filename, contents in {
                "catalogs.json": {"en": {}, "pt-BR": {"aliases": ["pt"]}, "pt-PT": {}},
                "en.json": {"launcher.test": "English"},
                "pt-BR.json": {"launcher.test": "Brazilian Portuguese"},
                "pt-PT.json": {},
            }.items():
                (root / filename).write_text(json.dumps(contents), encoding="utf-8")
            self.assertEqual(Translator("pt", root).t("launcher.test"), "Brazilian Portuguese")
            self.assertEqual(Translator("pt_PT", root).t("launcher.test"), "English")

    def test_duplicate_locale_registrations_are_reported(self):
        with tempfile.TemporaryDirectory() as target:
            root = Path(target)
            (root / "catalogs.json").write_text(json.dumps({
                "en": {"name": "English", "direction": "ltr"},
                "EN": {"name": "Duplicate", "direction": "ltr"},
            }), encoding="utf8")
            (root / "en.json").write_text('{"launcher.test": "Hello"}', encoding="utf8")
            self.assertTrue(any("duplicate locale registration" in issue for issue in check_locales.report(root)["issues"]))

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
