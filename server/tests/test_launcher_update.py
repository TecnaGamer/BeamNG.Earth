"""The launcher's update question before the game starts (launcher.update_at_start)."""
import argparse
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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


class Popup(unittest.TestCase):
    def test_linux_zenity_yes_no_and_none_installed(self):
        run = lambda code: (lambda cmd, timeout: mock.Mock(returncode=code))
        self.assertTrue(launcher.update_popup("t", run=run(0), which=lambda n: n == "zenity", windows=False))
        self.assertFalse(launcher.update_popup("t", run=run(1), which=lambda n: n == "kdialog", windows=False))
        self.assertIsNone(launcher.update_popup("t", run=run(0), which=lambda n: None, windows=False))


if __name__ == "__main__":
    unittest.main()
