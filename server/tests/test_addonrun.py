"""addonrun.py and the add-ons' local list: a mod zip's own GE code (mod_code: its extensions and modules, not the game's
own files it replaces, never BeamNG.Earth's), the local list's entries (a Local badge, a list id's place, a file
on this computer installed with its own hash), and a companion installed, started with this app's Python, given the
feed's address, stopped and removed."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import addonrun  # noqa: E402
import addons  # noqa: E402


def zip_of(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, b"-- x")
    return buf.getvalue()


class ModCodeTest(unittest.TestCase):
    def test_own_code_only(self):
        d = tempfile.mkdtemp(prefix="modcode_")
        game = os.path.join(d, "game")
        os.makedirs(os.path.join(game, "lua", "ge", "extensions", "career"))
        open(os.path.join(game, "lua", "ge", "extensions", "career", "career.lua"), "w").close()
        p = os.path.join(d, "m.zip")
        with open(p, "wb") as f:
            f.write(zip_of(["lua/ge/extensions/freight/core.lua", "lua/ge/extensions/freight/tiles/n0e10.lua",
                            "lua/ge/extensions/career/career.lua", "lua/ge/extensions/beamngEarth/x.lua",
                            "scripts/freight/modScript.lua", "ui/modules/apps/Freight/app.js"]))
        c = addonrun.mod_code(p, game)
        self.assertEqual(c["extensions"], ["freight_core", "freight_tiles_n0e10"])
        self.assertEqual(c["modules"], ["freight/core", "freight/tiles/n0e10"])
        self.assertEqual(c["overrides"], 1, "the game's own career.lua it replaces: a restart")
        self.assertTrue(c["ui"])
        self.assertFalse(c["vehicle"])
        self.assertEqual(addonrun.mod_code(p)["overrides"], 1, "without the game's folder: its stock top folders")
        self.assertEqual(addonrun.mod_code(os.path.join(d, "none.zip"))["extensions"], [])


class FakeNet:
    def get(self, url, **kw):
        raise addons.netfetch.FetchError(url, "api.github.com", status=503)


COMPANION = '''
import os, sys, time
open("seen.txt", "w").write(os.environ.get("BNE_FEED", "") + " " + " ".join(sys.argv[1:]))
while True:
    time.sleep(0.2)
'''


class LocalCompanionTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="addonlocal_")
        self.script = os.path.join(self.d, "tool_v1.py")
        with open(self.script, "w") as f:
            f.write(COMPANION)
        os.makedirs(os.path.join(self.d, "data", "addons"))
        reg = {"format": 1, "addons": [{
            "id": "tool", "name": "Tool", "author": "someone", "repo": "someone/tool", "kind": "companion",
            "summary": "s", "licence": "l", "asset": r"^tool.*\.py$",
            "local": {"file": self.script, "tag": "v1-local"},
            "run": {"args": ["--feed"], "page": "http://127.0.0.1:47999/map", "port": 47999, "lan": True}}]}
        with open(os.path.join(self.d, "data", "addons", addons.LOCAL_NAME), "w") as f:
            json.dump(reg, f)
        bundled = addons.parse_registry({"format": 1, "addons": [{
            "id": "tool", "name": "Tool", "author": "someone", "repo": "someone/tool", "kind": "companion",
            "summary": "s", "licence": "l", "asset": r"^tool.*\.py$"}]})
        self.a = addons.Addons(os.path.join(self.d, "user"), os.path.join(self.d, "cache"), FakeNet(), version="9.9",
                               registry=bundled, data_dir=os.path.join(self.d, "data"),
                               feed_url="http://127.0.0.1:47615")
        self.addCleanup(self.a.runner.stop_all)

    def test_local_entry_in_the_list_ids_place(self):
        cards = self.a.cards()
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertTrue(c["local"])
        self.assertTrue(c["canInstall"], "a local file installs although GitHub does not answer")
        self.assertEqual(c["latest"], "v1-local")

    def test_bundled_companion_without_run_is_downloads_only(self):
        os.remove(self.a.local_path())
        c = self.a.cards()[0]
        self.assertFalse(c["canInstall"])
        with self.assertRaisesRegex(addons.AddonError, "not installed from here"):
            self.a.stage("tool", consent=True)

    def test_local_entries_only_from_the_local_list(self):
        with self.assertRaises(ValueError):
            addons._entry({"id": "x1", "name": "n", "author": "a", "repo": "a/b", "kind": "companion", "summary": "s",
                           "licence": "l", "asset": "x", "local": {"file": "/tmp/x.py"}})

    def test_install_start_stop_remove(self):
        self.a.cards()
        with self.assertRaisesRegex(addons.AddonError, "notice"):
            self.a.stage("tool", consent=False)
        self.assertIsNone(self.a.stage("tool", consent=True))
        d = self.a.runner.dir("tool")
        self.assertTrue(os.path.isfile(os.path.join(d, "tool_v1.py")))
        with self.assertRaisesRegex(addons.AddonError, "up to date"):
            self.a.stage("tool", consent=True)
        self.a.run_op("tool", "start")
        seen = os.path.join(d, "seen.txt")
        end = time.monotonic() + 20
        while not os.path.exists(seen) and time.monotonic() < end:
            time.sleep(0.1)
        with open(seen) as f:
            self.assertEqual(f.read(), "http://127.0.0.1:47615 --feed", "the feed's address and its run args")
        c = self.a.cards()[0]
        self.assertTrue(c["running"] and c["installed"])
        self.assertEqual(c["version"], "v1-local")
        self.assertTrue(self.a.openable("http://127.0.0.1:47999/map") and not self.a.openable("http://127.0.0.1:1/x"))
        self.a.run_op("tool", "autostart_on")
        self.assertTrue(self.a.cards()[0]["autostart"])
        self.a.run_op("tool", "stop")
        self.assertFalse(self.a.runner.running("tool"))
        self.assertNotIn("exitCode", self.a.cards()[0], "a Stop is no error")
        self.a.autostart()
        self.assertTrue(self.a.runner.running("tool"), "started with BeamNG.Earth")
        self.a.run_op("tool", "uninstall")
        self.assertFalse(os.path.exists(d))
        self.assertFalse(self.a.cards()[0]["installed"])


if __name__ == "__main__":
    unittest.main()
