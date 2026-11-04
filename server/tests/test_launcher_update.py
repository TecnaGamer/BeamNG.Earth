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

    def go(self, answer="", got=NEWER, packaged=True, args=None, fail=None):
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
        r = launcher.update_at_start(args or argparse.Namespace(no_update_check=False), ask=ask, packaged=packaged,
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


if __name__ == "__main__":
    unittest.main()
