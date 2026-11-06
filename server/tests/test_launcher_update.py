"""The launcher's update question before the game starts (launcher.update_at_start)."""
import argparse
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # (server/: selfupdate)
import launcher

NEWER = {"current": "1.0.0", "latest": "1.1.0", "url": "https://github.com/TecnaGamer/BeamNG.Earth/releases/tag/v1.1.0"}


class UpdateAtStart(unittest.TestCase):
    def setUp(self):
        tty = mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True))
        tty.start()
        self.addCleanup(tty.stop)
        self.started, self.downloads = [], []

    def go(self, answer="", got=NEWER, packaged=True, args=None, fail=None, popup=None):
        def download(net, current, path, progress):
            if fail:
                raise fail
            self.downloads.append(current)
            progress(50, 100)
            return got["latest"], "/x/BeamNG.Earth-new.exe"
        asked = []

        def ask(q):
            asked.append(q)
            return answer
        r = launcher.update_at_start(args or argparse.Namespace(no_update_check=False), ask=ask, popup=popup or (lambda text: None),
                                     packaged=packaged,
                                     check=lambda current, net: got, download=download,
                                     start=lambda: self.started.append(1) or True)
        return r, asked

    def test_yes_or_enter_downloads_it_and_starts_the_new_copy(self):
        for answer in ("", "y", "YES"):
            self.started.clear()
            r, asked = self.go(answer)
            self.assertTrue(r)
            self.assertEqual(len(asked), 1)
            self.assertEqual(self.started, [1])

    def test_no_starts_this_version(self):
        r, _ = self.go("n")
        self.assertFalse(r)
        self.assertEqual((self.downloads, self.started), ([], []))

    def test_up_to_date_or_offline_asks_nothing(self):
        r, asked = self.go(got=None)
        self.assertFalse(r)
        self.assertEqual(asked, [])

    def test_a_source_copy_or_the_switch_asks_nothing(self):
        self.assertEqual(self.go(packaged=False), (False, []))
        self.assertEqual(self.go(args=argparse.Namespace(no_update_check=True)), (False, []))

    def test_a_failed_download_starts_this_version(self):
        r, _ = self.go(fail=RuntimeError("GitHub did not answer"))
        self.assertFalse(r)
        self.assertEqual(self.started, [])

    def test_no_window_to_answer_in_asks_nothing(self):
        with mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: False)):
            self.assertEqual(self.go(), (False, []))

    def test_the_popup_answers_first(self):
        shown = []
        r, asked = self.go(popup=lambda text: shown.append(text) or True)
        self.assertTrue(r)
        self.assertEqual(asked, [])
        self.assertIn("1.1.0 is out (you have 1.0.0)", shown[0])
        self.started.clear()
        r, asked = self.go(popup=lambda text: False)
        self.assertFalse(r)
        self.assertEqual((asked, self.started), ([], []))

    def test_the_popup_works_in_a_window_with_no_console_input(self):
        with mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: False)):
            r, _ = self.go(popup=lambda text: True)
        self.assertTrue(r)


class HandOver(unittest.TestCase):
    """hand_over: the downloaded newer copy starts instead of this one, unless it dies at once (an antivirus, a damaged
    download: every start flashed a window and closed, Discord 2026-11-05)."""

    def go(self, wait=None, popen_error=None, sac=False):
        import subprocess
        import selfupdate
        proc = mock.Mock()
        if isinstance(wait, BaseException):
            proc.wait.side_effect = wait
        else:
            proc.wait.return_value = wait
        dropped = []
        with mock.patch.object(launcher, "FROZEN", True), \
                mock.patch.object(selfupdate, "staged", lambda current: "/x/BeamNG.Earth-new.exe"), \
                mock.patch.object(selfupdate, "drop_staged",
                                  lambda path=None, blocked_by=None: dropped.append(blocked_by or 1)), \
                mock.patch.object(launcher, "smart_app_control_on", lambda: sac), \
                mock.patch.object(subprocess, "Popen", side_effect=popen_error, return_value=proc) as popen, \
                mock.patch.object(launcher, "problem", lambda *a, **k: self.said.append(a)):
            r = launcher.hand_over()
        return r, dropped, popen

    def setUp(self):
        self.said = []

    def test_the_new_copy_running_takes_over(self):
        import subprocess
        r, dropped, popen = self.go(wait=subprocess.TimeoutExpired("x", 5))
        self.assertTrue(r)
        self.assertEqual(dropped, [])
        self.assertEqual(popen.call_args[0][0][0], "/x/BeamNG.Earth-new.exe")

    def test_a_new_copy_that_dies_at_once_is_dropped_and_this_one_runs(self):
        r, dropped, _ = self.go(wait=1)
        self.assertFalse(r)
        self.assertEqual(dropped, [1])

    def test_smart_app_control_block_remembered(self):
        """GitHub #823: Windows 11 Smart App Control blocks an unsigned new copy with no "run anyway": this copy runs,
        the player is told why, and that version is remembered so it is not offered (and blocked) again."""
        r, dropped, _ = self.go(popen_error=OSError("[WinError 4551] An Application Control policy has blocked this file"))
        self.assertFalse(r)
        self.assertEqual(dropped, ["smart_app_control"])
        self.assertIn("Smart App Control", self.said[-1][0])
        r, dropped, _ = self.go(wait=1, sac=True)
        self.assertEqual(dropped, ["smart_app_control"], "Smart App Control on, the new copy closed at once")

    def test_a_new_copy_that_cannot_start(self):
        r, dropped, _ = self.go(popen_error=OSError("blocked"))
        self.assertFalse(r)
        self.assertEqual(dropped, [1])


class TeeLog(unittest.TestCase):
    def test_windows_output_also_in_the_log_appended_and_trimmed(self):
        import io
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        out, err = sys.stdout, sys.stderr
        self.addCleanup(setattr, sys, "stdout", out)
        self.addCleanup(setattr, sys, "stderr", err)
        self.assertIsNone(launcher.tee_log(d, windows=False))
        for n in range(2):
            sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
            path = launcher.tee_log(d, windows=True)
            print(f"start {n}")
            self.assertIn(f"start {n}", sys.stdout._stream.getvalue())     # the console still gets it
            sys.stdout._log.close()
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("start 0", text)
        self.assertIn("start 1", text)
        with open(path, "a") as f:
            f.write("x" * (launcher.LOG_KEEP_BYTES + 10))
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        launcher.tee_log(d, windows=True)
        sys.stdout._log.close()
        self.assertLess(os.path.getsize(path), launcher.LOG_KEEP_BYTES)


class GameStillRunning(unittest.TestCase):
    def test_a_closing_game_is_not_already_running(self):
        seq = iter([True, True, False])
        self.assertFalse(launcher.game_still_running(alive=lambda: next(seq), sleep=lambda s: None))

    def test_a_running_game_and_none(self):
        self.assertTrue(launcher.game_still_running(alive=lambda: True, sleep=lambda s: None))
        self.assertFalse(launcher.game_still_running(alive=lambda: False, sleep=lambda s: None))


class Popup(unittest.TestCase):
    def test_linux_zenity_yes_no_and_none_installed(self):
        run = lambda code: (lambda cmd, timeout: mock.Mock(returncode=code))
        self.assertTrue(launcher.update_popup("t", run=run(0), which=lambda n: n == "zenity", windows=False))
        self.assertFalse(launcher.update_popup("t", run=run(1), which=lambda n: n == "kdialog", windows=False))
        self.assertIsNone(launcher.update_popup("t", run=run(0), which=lambda n: None, windows=False))


if __name__ == "__main__":
    unittest.main()
