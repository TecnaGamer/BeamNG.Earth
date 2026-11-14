"""apppaths.py: the paths the player chooses (the game, the download cache, the BeamMP launcher), from the system's
file window or typed (the user, 2026-10-25: "Open the explorer for that operating system but have a text input for
the path as fallback")."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import apppaths  # noqa: E402


class CacheDirTest(unittest.TestCase):
    def test_kept_made_and_forgotten(self):
        with tempfile.TemporaryDirectory() as dd:
            where = os.path.join(dd, "other drive", "cache")
            d, err = apppaths.set_cache_dir(f' "{where}" ', dd)
            self.assertEqual((d, err), (where, None))
            self.assertTrue(os.path.isdir(where), "the folder is made")
            self.assertEqual(apppaths.saved_cache_dir(dd), where, "kept for the next start")
            self.assertEqual(apppaths.set_cache_dir("default", dd), (None, None))
            self.assertIsNone(apppaths.saved_cache_dir(dd), "back to the default")

    def test_unusable_folder_is_an_error(self):
        with tempfile.TemporaryDirectory() as dd:
            blocker = os.path.join(dd, "a file")
            Path(blocker).write_text("x")
            d, err = apppaths.set_cache_dir(os.path.join(blocker, "cache"), dd)
            self.assertIsNone(d)
            self.assertIn("cannot use", err)
            self.assertIsNone(apppaths.saved_cache_dir(dd), "nothing kept")

    def test_drive_relative_path_refused(self):
        """GitHub #895: c:\\games\\beamng-cache with its backslashes dropped by the shell is c:gamesbeamng-cache,
        a folder under the current one (the home folder): refused, with how to write it."""
        for p in ("c:gamesbeamng-cache", '"C:games"', "d:"):
            self.assertTrue(apppaths.drive_relative(p), p)
        for p in (r"c:\games\beamng-cache", "c:/games/beamng-cache", r"\\server\share\cache", "/home/x/cache",
                  "cache", "", None, "~/cache"):
            self.assertFalse(apppaths.drive_relative(p), p)
        self.assertIn("c:/games/beamng-cache", apppaths.drive_relative_problem("c:gamesbeamng-cache"))
        with tempfile.TemporaryDirectory() as dd:
            d, err = apppaths.set_cache_dir("c:gamesbeamng-cache", dd)
            self.assertIsNone(d)
            self.assertIn("drive letter", err)
            self.assertIsNone(apppaths.saved_cache_dir(dd), "nothing kept")
        sys.path.insert(0, str(ROOT))
        import launcher
        with tempfile.TemporaryDirectory() as dd:
            with self.assertRaises(OSError):
                launcher.cache_dir_arg("c:gamesbeamng-cache", dd)
            self.assertIsNone(apppaths.saved_cache_dir(dd))
            where = os.path.join(dd, "games", "beamng-cache")
            self.assertEqual(launcher.cache_dir_arg(where, dd), where, "a full path with backslashes is kept")


class GameDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dd = os.path.join(self.tmp.name, "data")
        self.game = os.path.join(self.tmp.name, "Games", "BeamNG.drive")
        exe = os.path.join(self.game, apppaths.GAME_WIN if apppaths.IS_WIN else apppaths.GAME_LINUX)
        os.makedirs(os.path.dirname(exe))
        Path(exe).write_text("")
        self.exe = exe

    def tearDown(self):
        self.tmp.cleanup()

    def test_folder_program_or_its_bin_folder(self):
        for given in (self.game, self.exe, os.path.dirname(self.exe)):
            self.assertEqual(apppaths.game_folder(given), self.game, given)
        self.assertIsNone(apppaths.game_folder(self.tmp.name))
        self.assertIsNone(apppaths.game_folder(""))

    def test_kept_only_when_the_game_is_there(self):
        self.assertIsNone(apppaths.set_game_dir(self.tmp.name, self.dd))
        self.assertIsNone(apppaths.saved_game_dir(self.dd))
        self.assertEqual(apppaths.set_game_dir(self.exe, self.dd), self.game)
        self.assertEqual(apppaths.saved_game_dir(self.dd), self.game)
        apppaths.set_game_dir("default", self.dd)
        self.assertIsNone(apppaths.saved_game_dir(self.dd))

    def test_steam_library(self):
        self.assertTrue(apppaths.in_steam_library(os.path.join("x", "steamapps", "common", "BeamNG.drive")))
        self.assertFalse(apppaths.in_steam_library(self.game))


@unittest.skipIf(apppaths.IS_WIN, "Linux dialog programs")
class PickTest(unittest.TestCase):
    def test_commands(self):
        z = apppaths.pick_command("folder", "T", "/nonexistent", tool="zenity")[0]
        self.assertEqual(z[:2], ["zenity", "--file-selection"])
        self.assertIn("--directory", z)
        self.assertEqual(z[z.index("--filename") + 1], os.path.expanduser("~").rstrip("/") + "/", "home: no such start")
        k = apppaths.pick_command("file", "T", "/tmp", tool="kdialog")[0]
        self.assertEqual(k[:3], ["kdialog", "--getopenfilename", "/tmp"])

    def test_no_desktop_no_window(self):
        with mock.patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}):
            self.assertEqual(apppaths.pick("folder", "T"), ("none", None))
        with mock.patch.dict(os.environ, {"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "gamescope"}):
            self.assertEqual(apppaths.pick("folder", "T"), ("none", None), "Steam's Game Mode")

    def test_answers(self):
        def run(out, code):
            return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=out)
        with mock.patch.object(apppaths, "pick_command", return_value=(["x"], {})):
            self.assertEqual(apppaths.pick("folder", "T", run=run(b"/d/cache\n", 0)), ("ok", "/d/cache"))
            self.assertEqual(apppaths.pick("folder", "T", run=run(b"", 1)), ("cancel", None))
            self.assertEqual(apppaths.pick("folder", "T", run=run(b"", 255)), ("none", None))


class AskPathTest(unittest.TestCase):
    def answers(self, *texts):
        it = iter(texts)
        return lambda prompt: next(it)

    def test_typed(self):
        said = []
        got = apppaths.ask_path("Where?", "folder", lambda p: p if p.endswith("ok") else None,
                                ask=self.answers("/nope", "/x/ok"), say=said.append)
        self.assertEqual(got, "/x/ok")
        self.assertTrue(any("Not found" in s for s in said))

    def test_window_then_text_when_there_is_none(self):
        with mock.patch.object(apppaths, "pick", return_value=("none", None)):
            got = apppaths.ask_path("Where?", "folder", lambda p: p, ask=self.answers("", "/typed"), say=lambda m: None)
        self.assertEqual(got, apppaths.clean("/typed"))

    def test_window_choice(self):
        with mock.patch.object(apppaths, "pick", return_value=("ok", "/picked")):
            self.assertEqual(apppaths.ask_path("Where?", "file", lambda p: p, ask=self.answers(""), say=lambda m: None),
                             "/picked")

    def test_skip(self):
        self.assertIsNone(apppaths.ask_path("Where?", "folder", lambda p: p, ask=self.answers("s"), say=lambda m: None))


if __name__ == "__main__":
    unittest.main()
