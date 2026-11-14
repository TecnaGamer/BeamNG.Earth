"""tools/release_notes.py: a release's notes end with the launch steps (docs/LAUNCH_STEPS.md) for that version."""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load():
    spec = importlib.util.spec_from_file_location("bne_release_notes", os.path.join(ROOT, "tools", "release_notes.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE = """# Changelog

## Unreleased

## 2.0.1 beta (2026-12-01)

### Fixed
- Two.

## 2.0.0 beta (2026-11-30)

### New
- One.
"""


class ReleaseNotes(unittest.TestCase):
    def test_section_then_launch_steps(self):
        rn = load()
        out = rn.notes("2.0.1-beta", SAMPLE)
        self.assertTrue(out.startswith("### Fixed\n- Two."))
        self.assertNotIn("One.", out)
        self.assertIn("## How to launch BeamNG.Earth", out)
        self.assertGreater(out.index("How to launch"), out.index("Two."))
        self.assertIn("BeamNG.Earth-2.0.1-beta.exe", out)
        self.assertNotIn("{version}", out)
        self.assertNotIn("<!--", out)

    def test_missing_section(self):
        with self.assertRaises(SystemExit):
            load().notes("9.9.9-beta", SAMPLE)

    def test_real_changelog_has_current_release(self):
        rn = load()
        out = rn.notes("1.8.1-beta")
        self.assertIn("Multiplayer freezes", out)
        self.assertTrue(out.rstrip().endswith("#install-and-play)."))


if __name__ == "__main__":
    unittest.main()
