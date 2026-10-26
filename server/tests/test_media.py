"""media: desktop Spotify's song from its window title; the media keys (Windows only)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media  # noqa: E402


class MediaTest(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(media.parse_title("M83 - Midnight City"), (True, "M83", "Midnight City"))
        self.assertEqual(media.parse_title("Spotify Premium"), (False, "", ""))
        self.assertEqual(media.parse_title("Spotify"), (False, "", ""))
        self.assertEqual(media.parse_title("AC/DC - Back In Black - Remastered"), (True, "AC/DC", "Back In Black - Remastered"))

    def test_not_windows(self):
        if sys.platform == "win32":
            self.skipTest("Windows")
        self.assertFalse(media.state()["ok"])
        self.assertFalse(media.press("play"))
        self.assertFalse(media.press("nope"))


if __name__ == "__main__":
    unittest.main()
