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


class VolumeTest(unittest.TestCase):
    """The Music app's slider: Spotify's mixer level (Windows only)."""

    def test_clamp(self):
        import media
        self.assertEqual([media.clamp_volume(v) for v in (-5, 0, 42.4, 42.6, 100, 180, "55")], [0, 0, 42, 43, 100, 100, 55])
        self.assertIsNone(media.clamp_volume(None))
        self.assertIsNone(media.clamp_volume("loud"))
        self.assertIsNone(media.clamp_volume(float("nan")))

    def test_elsewhere(self):
        import media
        if sys.platform == "win32":
            self.skipTest("Windows: the real mixer")
        self.assertIsNone(media.volume())
        self.assertFalse(media.set_volume(50))
        self.assertNotIn("volume", media.state())


if __name__ == "__main__":
    unittest.main()
