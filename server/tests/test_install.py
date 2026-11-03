"""Installer dependency failures: no DuckDB source build on Windows, and a usable recovery path."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import install  # noqa: E402


class SetupVenv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.venv = os.path.join(self.tmp.name, ".venv")
        self.stamp = os.path.join(self.venv, ".requirements.sha256")
        for name, value in (("VENV", self.venv), ("REQ_STAMP", self.stamp)):
            patch = mock.patch.object(install, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def existing_venv(self, windows):
        py = os.path.join(self.venv, "Scripts", "python.exe") if windows else os.path.join(self.venv, "bin", "python")
        os.makedirs(os.path.dirname(py), exist_ok=True)
        Path(py).touch()
        return py

    def test_success_uses_the_existing_interpreter_and_stamps_requirements(self):
        # The existing environment can differ from the Python running install.py (or start.py).
        for windows in (True, False):
            with self.subTest(windows=windows), mock.patch.object(install, "IS_WIN", windows):
                py = self.existing_venv(windows)
                with mock.patch.object(install.subprocess, "run", side_effect=[
                        mock.Mock(returncode=0), mock.Mock(returncode=0, stdout="numpy / Pillow\n")]) as run:
                    install.setup_venv(dry_run=False)
                cmd = run.call_args_list[0].args[0]
                self.assertEqual(cmd[:4], [py, "-m", "pip", "install"])
                self.assertEqual("--only-binary=duckdb" in cmd, windows)
                self.assertEqual(cmd[-2:], ["-r", install.REQUIREMENTS])
                self.assertEqual(Path(self.stamp).read_text(), install.requirements_hash())

    def test_missing_windows_wheel_explains_how_to_replace_the_existing_venv(self):
        with mock.patch.object(install, "IS_WIN", True):
            py = self.existing_venv(True)
            Path(self.stamp).write_text("old requirements")
            with mock.patch.object(install.subprocess, "run", return_value=mock.Mock(returncode=1)) as run:
                with self.assertRaises(SystemExit) as error:
                    install.setup_venv(dry_run=False)
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(run.call_count, 1)
            self.assertIn("--only-binary=duckdb", run.call_args.args[0])
            self.assertTrue(os.path.isfile(py))
            self.assertEqual(Path(self.stamp).read_text(), "old requirements")
        message = self.output.getvalue()
        for text in ("No matching distribution found", "64-bit Python 3.12", "rename", "unused backup name",
                     "py -3.12 install.py", "does not replace an existing .venv", "standalone Windows .exe"):
            self.assertIn(text, message)

    def test_failed_linux_install_keeps_the_network_hint_without_windows_commands(self):
        with mock.patch.object(install, "IS_WIN", False):
            self.existing_venv(False)
            with mock.patch.object(install.subprocess, "run", return_value=mock.Mock(returncode=1)):
                with self.assertRaises(SystemExit):
                    install.setup_venv(dry_run=False)
        self.assertIn("Check your internet connection", self.output.getvalue())
        self.assertNotIn("py -3.12", self.output.getvalue())
        self.assertFalse(os.path.exists(self.stamp))

    def test_failed_import_does_not_mark_requirements_as_ready(self):
        with mock.patch.object(install, "IS_WIN", True):
            self.existing_venv(True)
            with mock.patch.object(install.subprocess, "run", side_effect=[
                    mock.Mock(returncode=0), mock.Mock(returncode=1, stderr="broken import")]):
                with self.assertRaises(SystemExit):
                    install.setup_venv(dry_run=False)
        self.assertIn("broken import", self.output.getvalue())
        self.assertFalse(os.path.exists(self.stamp))

    def test_dry_run_does_not_create_an_environment_or_run_pip(self):
        with mock.patch.object(install.subprocess, "run") as run:
            install.setup_venv(dry_run=True)
        run.assert_not_called()
        self.assertFalse(os.path.exists(self.venv))


class ModActive(unittest.TestCase):
    """ensure_mod_active: the mod turned back on in mods/db.json (a BeamMP server turns it off), nothing else."""

    def write(self, d, db):
        os.makedirs(os.path.join(d, "mods"), exist_ok=True)
        with open(os.path.join(d, "mods", "db.json"), "w", encoding="utf-8") as f:
            import json
            json.dump(db, f)

    def read(self, d):
        import json
        with open(os.path.join(d, "mods", "db.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_turned_back_on_others_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"header": {"version": 1.1}, "mods": {install.MOD_NAME: {"active": False, "modname": "x"},
                                                                "other": {"active": False}}})
            self.assertTrue(install.ensure_mod_active(d))
            db = self.read(d)
            self.assertIs(db["mods"][install.MOD_NAME]["active"], True)
            self.assertEqual(db["mods"][install.MOD_NAME]["modname"], "x")
            self.assertIs(db["mods"]["other"]["active"], False, "another mod stays as it was")
            self.assertEqual(db["header"], {"version": 1.1})
            self.assertFalse(install.ensure_mod_active(d), "already on: nothing written")

    def test_no_list_or_no_entry(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(install.ensure_mod_active(d))
            self.write(d, {"mods": {}})
            self.assertFalse(install.ensure_mod_active(d))
            with open(os.path.join(d, "mods", "db.json"), "w", encoding="utf-8") as f:
                f.write("{broken")
            self.assertFalse(install.ensure_mod_active(d))


class RunningGameReload(unittest.TestCase):
    """reload_running_game: the one-shot script written to the user folder, the reload sent through the game's pipe;
    with no game to take it, the script removed again."""

    def files(self, d):
        return [os.path.join(d, *rel.split("/")) for rel in install.RELOADED_FILES]

    def test_sent_keeps_the_script(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(install, "send_game_command",
                                                                   return_value=True) as send:
            self.assertTrue(install.reload_running_game(d))
            send.assert_called_once_with(install.RELOAD_COMMAND)
            for path in self.files(d):
                self.assertTrue(os.path.isfile(path), path)
            install.clear_reload_files(d)
            for path in self.files(d):
                self.assertFalse(os.path.exists(path), path)
            self.assertFalse(os.path.exists(os.path.join(d, "scripts", "beamng_earth_reload")))

    def test_no_game_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(install, "send_game_command", return_value=False):
            self.assertFalse(install.reload_running_game(d))
            for path in self.files(d):
                self.assertFalse(os.path.exists(path), path)

    @unittest.skipIf(os.name == "nt", "a FIFO")
    def test_pipe_without_a_reader_does_not_block(self):
        with tempfile.TemporaryDirectory() as d:
            fifo = os.path.join(d, "pipe")
            os.mkfifo(fifo)
            with mock.patch.object(install, "GAME_PIPE", fifo):
                self.assertFalse(install.send_game_command(b"v1/x"), "no game reading: refused, not waiting")
                r = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    self.assertTrue(install.send_game_command(b"v1/x"))
                    self.assertEqual(os.read(r, 100), b"v1/x")
                finally:
                    os.close(r)
            with mock.patch.object(install, "GAME_PIPE", os.path.join(d, "none")):
                self.assertFalse(install.send_game_command(b"v1/x"))


class ModUpdateTimes(unittest.TestCase):
    """The game repacks a terrain texture (seconds, on its main thread) when the file is newer than its cache entry:
    an update keeps an unchanged file's time and gives a changed one the current time."""

    def test_unchanged_files_keep_their_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, user = os.path.join(tmp, "src"), os.path.join(tmp, "user")
            dest = os.path.join(user, "mods", "unpacked", install.MOD_NAME)
            os.makedirs(src)
            os.makedirs(dest)
            for root, files in ((src, {"tex.png": b"same", "code.lua": b"new"}),
                                (dest, {"tex.png": b"same", "code.lua": b"old"})):
                for name, data in files.items():
                    with open(os.path.join(root, name), "wb") as f:
                        f.write(data)
            with open(os.path.join(src, "added.png"), "wb") as f:
                f.write(b"added")
            old = 1_600_000_000
            for name in ("tex.png", "code.lua"):
                os.utime(os.path.join(dest, name), (old, old))
            for name in ("tex.png", "code.lua", "added.png"):
                os.utime(os.path.join(src, name), (old + 50, old + 50))
            with mock.patch.object(install, "MOD_SRC", src), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(install.install_mod(user, True, dry_run=False, quiet=True), "updated")
            mtime = lambda name: os.stat(os.path.join(dest, name)).st_mtime     # noqa: E731
            self.assertEqual(mtime("tex.png"), old, "unchanged: the time of the copy it replaced")
            now = __import__("time").time()
            self.assertGreater(mtime("code.lua"), now - 60, "changed: now (newer than any cache of the old content)")
            self.assertGreater(mtime("added.png"), now - 60, "new: now")
            with open(os.path.join(dest, "code.lua"), "rb") as f:
                self.assertEqual(f.read(), b"new")

    def test_in_place_update_never_deletes_unmodified_files_and_removes_obsolete_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, user = os.path.join(tmp, "src"), os.path.join(tmp, "user")
            dest = os.path.join(user, "mods", "unpacked", install.MOD_NAME)
            os.makedirs(os.path.join(src, "levels", "beamng_earth"))
            os.makedirs(os.path.join(dest, "levels", "beamng_earth"))
            os.makedirs(os.path.join(dest, "obsolete_dir"))
            for root in (src, dest):
                with open(os.path.join(root, "levels", "beamng_earth", "main.level.json"), "wb") as f:
                    f.write(b"{}")
            with open(os.path.join(src, "code.lua"), "wb") as f:
                f.write(b"v2")
            with open(os.path.join(dest, "code.lua"), "wb") as f:
                f.write(b"v1")
            with open(os.path.join(dest, "obsolete_dir", "stale.lua"), "wb") as f:
                f.write(b"stale")
            with mock.patch.object(install, "MOD_SRC", src), \
                 mock.patch.object(install.shutil, "rmtree", side_effect=AssertionError("rmtree breaks live level")) as rmtree, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(install.install_mod(user, True, dry_run=False, quiet=True), "updated")
            rmtree.assert_not_called()
            self.assertTrue(os.path.isfile(os.path.join(dest, "levels", "beamng_earth", "main.level.json")))
            self.assertFalse(os.path.exists(os.path.join(dest, "obsolete_dir", "stale.lua")))
            self.assertFalse(os.path.exists(os.path.join(dest, "obsolete_dir")))


if __name__ == "__main__":
    unittest.main()
