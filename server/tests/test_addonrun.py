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
from unittest import mock
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



# (no online add-on list in these tests: addons.REGISTRY_URL points at the real data repo, and a fetch through the
# scripted transports here took minutes and replaced the lists under test)
_no_online = mock.patch.object(addons, "REGISTRY_URL", "")


def setUpModule():
    _no_online.start()


def tearDownModule():
    _no_online.stop()

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


class AddFromGitHubTest(unittest.TestCase):
    def make(self, repo_info, rels):
        d = tempfile.mkdtemp(prefix="addrepo_")
        a = addons.Addons(os.path.join(d, "user"), os.path.join(d, "cache"), FakeNet(), registry=addons.parse_registry(
            {"format": 1, "addons": [{"id": "known", "name": "K", "author": "x", "repo": "Owner/Known", "kind": "link",
                                      "summary": "s", "licence": "l", "asset": ""}]}), data_dir=os.path.join(d, "data"))
        pages = {"/repos/someone/cool-mod": repo_info, "/repos/Owner/Known": {"full_name": "Owner/Known"},
                 "/repos/someone/cool-mod/releases": rels}

        def get(url, accept=None, fresh=False):
            for k, v in pages.items():
                if url.split("api.github.com")[1].split("?")[0].lower() == k.lower():     # (as GitHub: any case)
                    return json.dumps(v), 0.0, False
            raise addons.AddonError("GitHub did not answer (404)")
        a.github.get = get
        return a

    def test_a_zip_release_is_a_game_mod(self):
        a = self.make({"full_name": "someone/Cool-Mod", "description": "A mod", "license": {"name": "MIT License"}},
                      [{"tag_name": "v2", "prerelease": True, "assets": [{"name": "x.py"}]},
                       {"tag_name": "v1", "assets": [{"name": "Cool_Mod_v1.zip"}]}])
        aid = a.add_repo("https://github.com/someone/cool-mod/releases/tag/v1")
        e = a.entry(aid)
        self.assertEqual((aid, e["kind"], e["modFile"], e["added"], e["licence"]),
                         ("cool-mod", "beamng-mod", "cool-mod.zip", True, "MIT License"))
        with open(a.local_path()) as f:
            self.assertEqual([x["id"] for x in json.load(f)["addons"]], ["cool-mod"])
        self.assertTrue([c for c in a.cards() if c["id"] == aid][0]["added"])
        self.assertEqual(a.add_repo("someone/cool-mod"), aid, "pasted again: the same entry")
        a.forget(aid)
        with self.assertRaises(addons.AddonError):
            a.entry(aid)
        with self.assertRaisesRegex(addons.AddonError, "only add-ons you added"):
            a.forget("known")

    def test_a_py_release_is_a_program_and_known_ones_are_not_added_twice(self):
        a = self.make({"full_name": "someone/cool-mod"}, [{"tag_name": "v1", "assets": [{"name": "tool.py"}]}])
        self.assertEqual(a.entry(a.add_repo("someone/cool-mod"))["kind"], "companion")
        self.assertEqual(a.add_repo("owner/known"), "known")

    def test_refused(self):
        a = self.make({"full_name": "someone/cool-mod"}, [])
        with self.assertRaisesRegex(addons.AddonError, "no release"):
            a.add_repo("someone/cool-mod")
        for bad in ("not a repo", "a/../b", "https://gitlab.com/a/b", ""):
            with self.assertRaises(addons.AddonError):
                a.add_repo(bad)
        with self.assertRaisesRegex(addons.AddonError, "could not read"):
            a.add_repo("nobody/nothing")


class OptionsTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="addonopts_")
        script = os.path.join(self.d, "tool_v1.py")
        with open(script, "w") as f:
            f.write(COMPANION)
        reg = addons.parse_registry({"format": 1, "addons": [{
            "id": "tool", "name": "Tool", "author": "someone", "repo": "someone/tool", "kind": "companion",
            "summary": "s", "licence": "l", "asset": r"^tool.*\.py$", "local": {"file": script, "tag": "v1"},
            "run": {"args": ["--feed"], "page": "http://127.0.0.1:{port}/map", "lan": True},
            "options": [{"key": "port", "label": "Port", "type": "int", "default": 47700, "min": 1024, "max": 65535,
                         "arg": "--port"},
                        {"key": "fast", "label": "Fast", "type": "bool", "arg": "--fast", "env": "BNE_FAST"},
                        {"key": "units", "label": "Units", "type": "choice", "choices": ["km/h", "mph"], "env": "BNE_UNITS"}]}]},
            local=True)
        self.a = addons.Addons(os.path.join(self.d, "user"), os.path.join(self.d, "cache"), FakeNet(), registry=reg,
                               data_dir=os.path.join(self.d, "data"))
        self.addCleanup(self.a.runner.stop_all)

    def test_defaults_values_args_and_page(self):
        e = self.a.entry("tool")
        self.assertEqual(self.a.values(e), {"port": 47700, "fast": False, "units": "km/h"})
        self.assertEqual(self.a.run_args(e), (["--feed", "--port", "47700"], {"BNE_FAST": "0", "BNE_UNITS": "km/h"}))
        self.assertEqual(self.a.page(e), "http://127.0.0.1:47700/map")
        self.assertFalse(self.a.set_options("tool", {"port": 47800, "fast": True, "units": "mph", "other": 1}))
        self.assertEqual(self.a.run_args(e), (["--feed", "--port", "47800", "--fast"], {"BNE_FAST": "1", "BNE_UNITS": "mph"}))
        c = self.a.cards()[0]
        self.assertEqual(c["page"], "http://127.0.0.1:47800/map")
        self.assertEqual({o["key"]: o["value"] for o in c["options"]}, {"port": 47800, "fast": True, "units": "mph"})
        self.assertTrue(self.a.openable("http://127.0.0.1:47800/map") and not self.a.openable("http://127.0.0.1:47700/map"))
        for bad in ({"port": 80}, {"port": "x"}, {"port": True}, {"units": "knots"}, {"fast": "yes"}):
            with self.assertRaisesRegex(addons.AddonError, "not a valid value"):
                self.a.set_options("tool", bad)
        self.assertEqual(self.a.values(e)["port"], 47800, "a refused save changes nothing")

    def test_a_running_program_restarts_with_them(self):
        self.a.cards()
        self.a.stage("tool", consent=True)
        self.a.run_op("tool", "start")
        seen = os.path.join(self.a.runner.dir("tool"), "seen.txt")
        end = time.monotonic() + 20
        while not os.path.exists(seen) and time.monotonic() < end:
            time.sleep(0.1)
        os.remove(seen)
        self.assertTrue(self.a.set_options("tool", {"port": 47901}))
        end = time.monotonic() + 20
        while not os.path.exists(seen) and time.monotonic() < end:
            time.sleep(0.1)
        with open(seen) as f:
            self.assertIn("--port 47901", f.read())

    def test_bad_declarations_refused(self):
        for bad in ([{"key": "Port", "type": "int"}], [{"key": "p", "type": "float"}],
                    [{"key": "p", "type": "int", "env": "LD_PRELOAD"}], [{"key": "p", "type": "choice", "choices": []}],
                    [{"key": "p", "type": "int", "arg": "; rm -rf"}], [{"key": "p", "type": "int"}] * 2):
            with self.assertRaises(ValueError):
                addons.parse_options(bad)

    def test_a_mod_zips_own_declaration(self):
        p = os.path.join(self.d, "m.zip")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("lua/ge/extensions/m/core.lua", "x")
            z.writestr(addons.MANIFEST, json.dumps({"options": [{"key": "level", "type": "int", "min": 1, "max": 5,
                                                                 "default": 3}]}))
        man = addonrun.mod_code(p)["manifest"]
        self.assertEqual(addons.parse_options(man["options"])[0]["default"], 3)


if __name__ == "__main__":
    unittest.main()
