"""start.py --status: what this machine's BeamNG.Earth is, without starting anything (the versions,
the mod's state in the game, the download cache's size and the disk room left), read only."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import apppaths  # noqa: E402
import install  # noqa: E402
import start  # noqa: E402


class StatusTest(unittest.TestCase):
    def status_of(self, config, cache_dir, data_dir):
        """--status run in-process, its rows' text: config what load_config answers, the cache and
        the app data folders where the player's would be."""
        out = io.StringIO()
        with mock.patch.object(install, "load_config", return_value=config), \
             mock.patch.object(apppaths, "saved_cache_dir", return_value=None), \
             mock.patch.object(apppaths, "default_cache_dir", return_value=cache_dir), \
             mock.patch.object(apppaths, "data_dir", return_value=data_dir), \
             contextlib.redirect_stdout(out):
            start.status()
        return out.getvalue()

    def test_it_says_what_this_machine_has(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "cache")
            os.makedirs(os.path.join(cache, "cdn"))
            Path(os.path.join(cache, "cdn", "switch.json")).write_text("{}")
            text = self.status_of({"user_dir": "C:/game"}, cache, os.path.join(tmp, "data"))
            self.assertIn("status", text)
            self.assertIn("Version", text)
            self.assertIn("Game folder", text)
            self.assertIn("C:/game", text)
            self.assertIn("not installed", text)               # (no mod in that folder: said, not failed)
            self.assertIn("Cache", text)
            self.assertIn("0.0 GB, 1 file", text)
            self.assertIn("Disk free", text)

    def test_no_install_still_answers(self):
        """A machine install.py has not run on (no user folder known, no cache): the rows say so
        instead of failing."""
        with tempfile.TemporaryDirectory() as tmp:
            text = self.status_of({}, os.path.join(tmp, "gone"), os.path.join(tmp, "data"))
            self.assertIn("no game folder known", text)
            self.assertIn("(empty: nothing downloaded yet)", text)

    def test_the_mods_state_when_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = os.path.join(tmp, "mods", "unpacked", install.MOD_NAME)
            os.makedirs(mod)
            Path(os.path.join(mod, "some.lua")).write_text("--")
            text = self.status_of({"user_dir": tmp}, os.path.join(tmp, "cache"), os.path.join(tmp, "data"))
            self.assertIn("installed", text)


if __name__ == "__main__":
    unittest.main()
