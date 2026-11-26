"""launchui.py: the launch screen's colours (enabled only where they can show), its banner and its
timed steps (a result line even when the step's body never says so, a failed step in red)."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import launchui  # noqa: E402


class Out:
    """A captured stdout that launchui writes through (the screen takes an out= callable)."""

    def __init__(self):
        self.lines = []
        self.encoding = "utf-8"

    def __call__(self, msg=""):
        self.lines.append(str(msg))


class EnableTest(unittest.TestCase):
    def tearDown(self):
        launchui.ENABLED = False
        os.environ.pop("NO_COLOR", None)

    def test_off_without_a_console(self):
        """A redirected or captured output: no colours (a piped run, a test's capture)."""
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertFalse(launchui.enable())
            self.assertFalse(launchui.ENABLED)
            self.assertEqual(launchui.paint("x", launchui.Color.ok), "x")

    def test_off_with_no_color(self):
        """NO_COLOR (https://no-color.org): plain text even on a real console."""
        os.environ["NO_COLOR"] = "1"
        fake = mock.Mock()
        fake.isatty = lambda: True
        with mock.patch.object(sys, "stdout", fake):
            self.assertFalse(launchui.enable())

    def test_plain_fallback_glyphs(self):
        """A console whose encoding cannot show the glyphs (a Windows console at its code page):
        their ASCII fallbacks, so no UnicodeEncodeError ever reaches the player."""
        self.assertEqual(launchui._glyph("sep"), "\u2500" if launchui._encodes("\u2500") else "-")
        with mock.patch.object(sys, "stdout") as fake:
            fake.encoding = "ascii"
            self.assertEqual(launchui._glyph("step"), ">")
            self.assertEqual(launchui._glyph("ok"), "ok")
            self.assertEqual(launchui._glyph("bad"), "x")
            self.assertFalse(launchui._encodes("\u2500"))


class ScreenTest(unittest.TestCase):
    def setUp(self):
        launchui.ENABLED = False             # (plain text: the assertions read the message alone)
        self.out = Out()
        self.ui = launchui.Screen(out=self.out)

    def test_banner_rows_and_tips(self):
        self.ui.banner("BeamNG.Earth 1.10.1-beta", "world server",
                       rows=[("Game folder", "C:/game"), ("Map data", None)],
                       tips=["Leave this window open while you play."])
        text = "\n".join(self.out.lines)
        self.assertIn("BeamNG.Earth 1.10.1-beta", text)
        self.assertIn("world server", text)
        self.assertIn("Game folder", text)
        self.assertIn("C:/game", text)
        self.assertNotIn("Map data", text)          # (a None row: left out)
        self.assertIn("Leave this window open", text)

    def test_step_done_skip_and_the_default(self):
        """A step says its status (st.done / st.skip); one whose body never does still gets its
        'done' line, with its seconds."""
        with self.ui.step("Preparing the mod") as st:
            st.done("up to date")
        with self.ui.step("Reading the server's options") as st:
            st.skip("not needed")
        with self.ui.step("Saving the configuration"):
            pass
        results = [ln for ln in self.out.lines if ln.startswith("  ")]
        self.assertEqual(results, ["  ok up to date (0.0 s)", "  ok not needed (0.0 s)",
                                   "  ok done (0.0 s)"])

    def test_failed_step_is_reported_and_raised(self):
        with self.assertRaises(ValueError), self.ui.step("Preparing the mod"):
            raise ValueError("no")
        self.assertIn("failed", "\n".join(self.out.lines))
        self.assertIn("Preparing the mod", "\n".join(self.out.lines))

    def test_summary_sums_the_steps(self):
        for _ in range(2):
            with self.ui.step("Preparing the mod") as st:
                st.done("up to date")
        self.ui.summary("ready")
        text = "\n".join(self.out.lines)
        self.assertIn("ready in", text)
        self.assertLess(self.ui.spent[0] + self.ui.spent[1], 1.0)


if __name__ == "__main__":
    unittest.main()
