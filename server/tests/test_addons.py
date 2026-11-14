"""addons.py: the Add-ons page's server side. The registry (only valid entries, only its repos), GitHub's pages kept on
disk (TTL, If-None-Match and 304, the copy on disk offline), releases and the file to install (the repo's own
downloads with a SHA-256 digest only, pre-releases and unreviewed files per trust), what is installed (by the file's
digest), the download checked and staged under a name the game's mod manager ignores (never the mod's own name),
refusals (no consent, a BeamMP session, not a game mod, already up to date, a bad zip), the links the page may open,
and the README / release notes as safe HTML through the Rust kernel (fast.md_html)."""
import contextlib
import hashlib
import io
import json
import os
import random
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import addons  # noqa: E402
import fast  # noqa: E402
import netfetch as nf  # noqa: E402
import updates  # noqa: E402
from test_netfetch import FakeClock, FakeSleep, FakeTransport  # noqa: E402

REPO = "someone/Some-Mod"
DL = f"https://github.com/{REPO}/releases/download/"


def mod_zip(files=None):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, data in (files or {"lua/ge/extensions/someMod/core.lua": b"return {}",
                                     "scripts/someMod/modScript.lua": b"-- x"}).items():
            z.writestr(name, data)
    return b.getvalue()


ZIP1, ZIP2 = mod_zip(), mod_zip({"lua/ge/extensions/someMod/core.lua": b"return {v=2}"})
SHA1, SHA2 = hashlib.sha256(ZIP1).hexdigest(), hashlib.sha256(ZIP2).hexdigest()


def registry(**over):
    e = {"id": "some-mod", "name": "Some Mod", "author": "someone", "repo": REPO, "kind": "beamng-mod",
         "summary": "A mod.", "licence": "All rights reserved.", "asset": r"^some-mod\.zip$",
         "modFile": "some-mod.zip", "oldFiles": ["old_some.zip"], "uiApps": ["SomeApp"], "minEarth": "1.0.0"}
    e.update(over)
    return {"format": 1, "addons": [e]}


def asset(tag, sha, size, name="some-mod.zip", url=None):
    return {"name": name, "size": size, "digest": "sha256:" + sha,
            "browser_download_url": url or DL + f"{tag}/{name}"}


def releases_json(*rels):
    return json.dumps(list(rels)).encode()


REL2 = {"tag_name": "build-2", "name": "Build 2", "published_at": "2026-11-05T11:35:23Z", "draft": False,
        "prerelease": False, "body": "## What's changed\n- new\n\n<details>\n<summary>Earlier</summary>\n\n- old\n</details>",
        "assets": [asset("build-2", SHA2, len(ZIP2))]}
REL1 = {"tag_name": "build-1", "name": "Build 1", "published_at": "2026-11-04T11:35:23Z", "prerelease": False,
        "body": "- first [docs](../../wiki)", "assets": [asset("build-1", SHA1, len(ZIP1))]}
PRE = {"tag_name": "build-3-rc", "prerelease": True, "body": "", "assets": [asset("build-3-rc", "1" * 64, 9)]}
DRAFT = {"tag_name": "build-4", "draft": True, "assets": [asset("build-4", "2" * 64, 9)]}


class FakeResp(io.BytesIO):
    def __init__(self, body, url="https://release-assets.githubusercontent.com/x"):
        super().__init__(body)
        self.status, self._url = 200, url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Net:
    """A netfetch.Fetcher on a scripted transport (the real 304 / lane / budget code) plus a session for downloads."""

    def __init__(self, script):
        self.clock = FakeClock()
        self.tr = FakeTransport(self.clock, script)
        self.f = nf.Fetcher(clock=self.clock, sleep=FakeSleep(self.clock), rng=random.Random(1), transport=self.tr,
                            wall=self.clock)
        self.sessions = []

    def get(self, url, **kw):
        return self.f.get(url, **kw)

    @contextlib.contextmanager
    def session(self, url, kind="demand", lane=None):
        self.sessions.append((url, lane))

        class S:
            headers = {"User-Agent": "test"}
            status, nbytes = 200, 0
        yield S()


def md(text, base, fold=False):
    return fast.md_html(text, base, fold)


REAL_REGISTRY_URL = addons.REGISTRY_URL      # (Base turns it off for every test)


class Base(unittest.TestCase):
    def setUp(self):
        # (no online list unless a test sets one: REGISTRY_URL points at the real data repo)
        p = mock.patch.object(addons, "REGISTRY_URL", "")
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("BNGE_TEST_TMP") or None)
        self.user = os.path.join(self.tmp.name, "user")
        self.cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(os.path.join(self.user, "mods"))
        self.wall = [1_800_000_000.0]

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, script=None, reg=None, version="1.8.2-beta"):
        script = script or {r"/releases\?": [(200, {"etag": '"r1"'}, releases_json(PRE, REL2, REL1, DRAFT), 0)],
                            r"/readme$": [(200, {"etag": '"m1"'}, b"# Some Mod\n\nSee [releases](../../releases).", 0)]}
        self.net = Net(script)
        return addons.Addons(self.user, self.cache, self.net, version=version,
                             registry=addons.parse_registry(reg or registry()), md=md, clock=lambda: self.wall[0])

    def mods(self, name):
        return os.path.join(self.user, "mods", name)


class RegistryTest(unittest.TestCase):
    def test_bundled_registry_is_valid(self):
        # (only add-ons whose authors agreed to be listed: the trucking career back on 2026-11-09, the user: "add the
        # worldwide trucking carrer back to the addon list"; every entry it has must be valid)
        es = addons.load_registry()
        with open(addons.REGISTRY_PATH, encoding="utf-8") as f:
            self.assertEqual(len(es), len(json.load(f)["addons"]), "every bundled entry is valid")
        self.assertEqual(len({e["id"] for e in es}), len(es), "no id twice")
        t = next(e for e in es if e["id"] == "trucking-dc")
        self.assertEqual(t["modFile"], "worldwide-trucking-dangerous-contracts.zip")
        self.assertTrue(t["asset"].search("worldwide-trucking-dangerous-contracts.zip"))
        self.assertFalse(t["asset"].search("worldwide-trucking-server.zip"), "not its server package")

    def test_invalid_entries_are_left_out(self):
        bad = [dict(registry()["addons"][0], **ch) for ch in (
            {"id": "Bad Id"}, {"repo": "../etc"}, {"repo": "a/b/c"}, {"kind": "exe"}, {"modFile": "../x.zip"},
            {"modFile": "x.exe"}, {"oldFiles": ["a/b.zip"]}, {"name": ""}, {"asset": "("},
            {"reviewed": [{"tag": "x", "sha256": "nothex"}]}, {"trust": "anything"}, {"uiApps": ["a b"]})]
        good = registry()["addons"][0]
        es = addons.parse_registry({"format": 1, "addons": bad + [good, dict(good)]})
        self.assertEqual([e["id"] for e in es], ["some-mod"], "only the valid one, once")
        with self.assertRaises(ValueError):
            addons.parse_registry({"format": 2, "addons": []})


class GitHubCacheTest(Base):
    def test_ttl_then_if_none_match_and_304(self):
        a = self.make({r"/releases\?": [(200, {"etag": '"e1"'}, b"[1]", 0), (304, {}, b"", 0)]})
        url = addons.API + REPO + "/releases?per_page=8"
        self.assertEqual(a.github.get(url)[0], "[1]")
        self.assertEqual(a.github.get(url)[0], "[1]", "within the TTL: from disk")
        self.assertEqual(len(self.net.tr.calls), 1)
        self.wall[0] += addons.TTL_S + 1
        body, age, stale = a.github.get(url)
        self.assertEqual((body, age, stale), ("[1]", 0.0, False), "304: the same answer, fresh again")
        self.assertEqual(self.net.tr.calls[-1][2].get("If-None-Match"), '"e1"')
        self.assertEqual(len(self.net.tr.calls), 2)

    def test_offline_copy_and_nothing(self):
        a = self.make({r"/releases\?": [(200, {}, b"[1]", 0), (500, {}, b"", 0)], r"/readme$": [(500, {}, b"", 0)]})
        url = addons.API + REPO + "/releases?per_page=8"
        a.github.get(url)
        self.wall[0] += addons.TTL_S + 1
        body, age, stale = a.github.get(url)
        self.assertEqual(body, "[1]")
        self.assertTrue(stale and age > addons.TTL_S)
        with self.assertRaises(addons.AddonError):
            a.github.get(addons.API + "x/y/readme")

    def test_requests_use_their_own_lane(self):
        a = self.make()
        a.cards()
        self.assertIn("api.github.com#addons", a.net.f._hosts, "the update check's own count is not used")
        self.assertNotIn("api.github.com", a.net.f._hosts)


class ReleasesTest(unittest.TestCase):
    def setUp(self):
        self.e = addons.parse_registry(registry())[0]

    def test_assets_only_from_the_repo_with_a_digest(self):
        r = dict(REL2, assets=[asset("b", SHA2, 5, url="https://evil.example/some-mod.zip"),
                               dict(asset("b", SHA2, 5), digest=""), asset("b", SHA2, 5, name="other.zip"),
                               asset("b", SHA2, 5, url=f"https://github.com/other/repo/releases/download/b/some-mod.zip")])
        self.assertIsNone(addons.releases(self.e, json.dumps([r]))[0]["asset"])
        got = addons.releases(self.e, json.dumps([REL2]))[0]["asset"]
        self.assertEqual((got["sha256"], got["size"]), (SHA2, len(ZIP2)))

    def test_drafts_out_prereleases_not_offered(self):
        rels = addons.releases(self.e, releases_json(PRE, REL2, REL1, DRAFT))
        self.assertEqual([r["tag"] for r in rels], ["build-3-rc", "build-2", "build-1"])
        self.assertEqual(addons.offered(self.e, rels)["tag"], "build-2")

    def test_trust_reviewed_offers_reviewed_files_only(self):
        e = addons.parse_registry(registry(trust="reviewed", reviewed=[{"tag": "build-1", "sha256": SHA1}]))[0]
        rels = addons.releases(e, releases_json(REL2, REL1))
        self.assertEqual(addons.offered(e, rels)["tag"], "build-1")
        e2 = addons.parse_registry(registry(trust="reviewed"))[0]
        self.assertIsNone(addons.offered(e2, rels))


class CardsTest(Base):
    def test_not_installed(self):
        c = self.make().cards()[0]
        self.assertEqual((c["installed"], c["latest"], c["canInstall"], c["update"]), (False, "build-2", True, False))
        self.assertEqual(c["size"], len(ZIP2))
        self.assertEqual(c["uiApps"], ["SomeApp"])

    def test_installed_version_by_digest_and_update(self):
        with open(self.mods("some-mod.zip"), "wb") as f:
            f.write(ZIP1)
        with open(self.mods("db.json"), "w") as f:
            json.dump({"mods": {"some-mod": {"active": False}}}, f)
        open(self.mods("old_some.zip"), "w").close()
        c = self.make().cards()[0]
        self.assertEqual((c["installed"], c["version"], c["update"], c["active"]), (True, "build-1", True, False))
        self.assertEqual(c["oldFiles"], ["old_some.zip"])
        with open(self.mods("some-mod.zip"), "wb") as f:
            f.write(b"by hand")
        c = self.make().cards()[0]
        self.assertEqual((c["version"], c["update"]), ("unknown", True))

    def test_needs_newer_earth(self):
        c = self.make(reg=registry(minEarth="9.0.0")).cards()[0]
        self.assertTrue(c["tooNew"])
        self.assertFalse(c["canInstall"])

    def test_github_down_is_an_error_on_the_card(self):
        c = self.make({r".": [(500, {}, b"", 0)]}).cards()[0]
        self.assertTrue(c["error"])
        self.assertFalse(c["canInstall"])


class DetailTest(Base):
    def test_readme_and_changelog_as_safe_html(self):
        a = self.make({r"/releases\?": [(200, {}, releases_json(REL2, REL1), 0)],
                       r"/readme$": [(200, {}, b"# T\n<script>x()</script>\n[r](../../releases) [j](javascript:x)\n"
                                                b"![shot](https://img.example/a.png)", 0)]})
        d = a.detail("some-mod")
        self.assertNotIn("<script", d["readme"])
        self.assertNotIn("href", d["readme"])
        self.assertIn(f"https://github.com/{REPO}/releases", d["readmeLinks"])
        self.assertIn("https://img.example/a.png", d["readmeLinks"])
        self.assertEqual([r["tag"] for r in d["releases"]], ["build-2", "build-1"])
        self.assertIn("new", d["releases"][0]["html"])
        self.assertNotIn("old", d["releases"][0]["html"], "the folded earlier notes are cut")
        self.assertIn(f"https://github.com/{REPO}/wiki", d["releases"][1]["links"])
        self.assertTrue(a.openable("https://img.example/a.png"), "a link of an add-on's text")
        self.assertTrue(a.openable(f"https://github.com/{REPO}/issues/1"), "the repo's pages")
        self.assertTrue(a.openable(f"https://github.com/{REPO}"))
        self.assertFalse(a.openable(f"https://github.com/{REPO}x/a"), "another repo with the same start")
        self.assertFalse(a.openable("https://other.example/"))
        self.assertFalse(a.openable("javascript:alert(1)"))
        self.assertFalse(a.openable('https://img.example/a.png" onclick="x'))

    def test_open_page_takes_the_allow_list(self):
        a = self.make()
        a.detail("some-mod")
        calls = []
        self.assertFalse(updates.open_page("https://img.example/z", popen=lambda *x, **k: calls.append(x)))
        self.assertTrue(updates.open_page(f"https://github.com/{REPO}/releases", popen=lambda *x, **k: calls.append(x),
                                          allow=a.openable) or sys.platform == "win32")
        self.assertFalse(updates.open_page("https://other.example/", popen=lambda *x, **k: calls.append(x),
                                           allow=a.openable))

    def test_companion_downloads(self):
        rel = dict(REL2, assets=[asset("build-2", SHA2, 10, name="tool-v2.py"), asset("build-2", SHA1, 20, name="app-v2.apk")])
        reg = registry(kind="companion", asset=r"^tool.*\.py$", links=[{"label": "Phone app", "asset": r"\.apk$"}])
        a = self.make({r"/releases\?": [(200, {}, releases_json(rel), 0)], r"/readme$": [(200, {}, b"x", 0)]}, reg=reg)
        d = a.detail("some-mod")
        self.assertEqual([x["label"] for x in d["releases"][0]["downloads"]], ["Download tool-v2.py", "Phone app"])
        self.assertFalse(d["card"]["canInstall"], "not installed from the page")
        self.assertTrue(a.openable(d["releases"][0]["downloads"][1]["url"]))

    def test_unknown_id(self):
        with self.assertRaises(addons.AddonError):
            self.make().detail("nope")


class StageTest(Base):
    def stage(self, a, body=ZIP2, **kw):
        kw.setdefault("consent", True)
        with mock.patch.object(addons, "_urlopen", lambda req: FakeResp(body)):
            return a.stage("some-mod", **kw)

    def test_fresh_install_staged_under_a_name_the_game_ignores(self):
        a = self.make()
        seen = []
        m = self.stage(a, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual((m["type"], m["op"], m["file"], m["tmp"], m["tag"]),
                         ("addon_apply", "install", "some-mod.zip", "some-mod.zip.bnge-tmp", "build-2"))
        self.assertFalse(os.path.exists(self.mods("some-mod.zip")), "the mod's own name is the game's to make")
        with open(self.mods(m["tmp"]), "rb") as f:
            self.assertEqual(f.read(), ZIP2)
        self.assertFalse(os.path.exists(self.mods("some-mod.zip.bnge-part")))
        self.assertTrue(seen and seen[-1] == (len(ZIP2), len(ZIP2)))
        self.assertEqual(self.net.sessions[-1], (DL + "build-2/some-mod.zip", "addons"))

    def test_update_keeps_the_mod_off_if_it_was(self):
        with open(self.mods("some-mod.zip"), "wb") as f:
            f.write(ZIP1)
        with open(self.mods("db.json"), "w") as f:
            json.dump({"mods": {"some-mod": {"active": False}}}, f)
        m = self.stage(self.make())
        self.assertEqual((m["op"], m["active"]), ("update", False))
        with open(self.mods("some-mod.zip"), "rb") as f:
            self.assertEqual(f.read(), ZIP1, "the installed file is untouched by the server")

    def test_refusals(self):
        a = self.make()
        for kw, why in (({"consent": False}, "agreed"), ({"mp": True}, "BeamMP")):
            with self.assertRaisesRegex(addons.AddonError, why):
                self.stage(a, **kw)
        with self.assertRaisesRegex(addons.AddonError, "checksum"):
            self.stage(a, body=ZIP1)
        self.assertEqual([n for n in os.listdir(self.mods("")) if "bnge" in n], [], "a bad download is deleted")
        with open(self.mods("some-mod.zip"), "wb") as f:
            f.write(ZIP2)
        with self.assertRaisesRegex(addons.AddonError, "up to date"):
            self.stage(a)
        lg = self.make(reg=registry(kind="companion"))
        with self.assertRaisesRegex(addons.AddonError, "not installed from here"):
            with mock.patch.object(addons, "_urlopen", lambda req: FakeResp(ZIP2)):
                lg.stage("some-mod", consent=True)

    def test_not_a_mod_zip(self):
        bad = mod_zip({"Some Mod/lua/x.lua": b"x"})
        sha = hashlib.sha256(bad).hexdigest()
        rel = dict(REL2, assets=[asset("build-2", sha, len(bad))])
        a = self.make({r"/releases\?": [(200, {}, releases_json(rel), 0)]})
        with self.assertRaisesRegex(addons.AddonError, "laid out"):
            self.stage(a, body=bad)
        self.assertFalse(any("bnge" in n for n in os.listdir(self.mods(""))))

    def test_check_mod_zip(self):
        p = os.path.join(self.tmp.name, "z.zip")
        for data, ok in ((ZIP1, True), (b"not a zip", False), (mod_zip({"../evil.lua": b"x"}), False),
                         (mod_zip({"/abs/lua/x.lua": b"x"}), False), (mod_zip({"README.md": b"x"}), False)):
            with open(p, "wb") as f:
                f.write(data)
            self.assertEqual(addons.check_mod_zip(p) == "", ok, data[:20])

    def test_applied_cleans_up(self):
        a = self.make()
        m = self.stage(a)
        a.applied("some-mod", "install", False, "mod manager not ready")
        self.assertFalse(os.path.exists(self.mods(m["tmp"])), "a failed hand-over leaves no file behind")

    def test_stale_leftovers_removed(self):
        p = self.mods("x.zip.bnge-part")
        open(p, "w").close()
        old = time.time() - addons.STALE_S - 10
        os.utime(p, (old, old))
        q = self.mods("y.zip.bnge-tmp")
        open(q, "w").close()
        self.make()
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.exists(q), "a fresh one may be on its way to the game")


class RegistryUrlTest(Base):
    def test_pages_copy_replaces_the_bundled_one_when_valid(self):
        other = registry(id="other-mod")
        script = {r"pages\.example": [(200, {}, json.dumps(other).encode(), 0)],
                  r"/releases\?": [(200, {}, releases_json(REL2), 0)]}
        a = self.make(script)
        with mock.patch.object(addons, "REGISTRY_URL", "https://pages.example/addons.json"):
            self.assertEqual([c["id"] for c in a.cards()], ["other-mod"])
        self.cache = os.path.join(self.tmp.name, "cache2")         # (a's copy on disk would answer within its TTL)
        b = self.make({r"pages\.example": [(200, {}, b"{broken", 0)], r"/releases\?": [(200, {}, b"[]", 0)]})
        with mock.patch.object(addons, "REGISTRY_URL", "https://pages.example/addons.json"):
            self.assertEqual([c["id"] for c in b.cards()], ["some-mod"], "a broken copy: the bundled one stays")

    def test_an_empty_online_list_takes_every_addon_off(self):
        empty = {"format": 1, "addons": []}
        a = self.make({r"pages\.example": [(200, {}, json.dumps(empty).encode(), 0)], r"/releases\?": [(200, {}, b"[]", 0)]})
        with mock.patch.object(addons, "REGISTRY_URL", "https://pages.example/addons.json"):
            self.assertEqual(a.cards(), [], "an empty list is an answer")

    def test_all_invalid_entries_keep_the_bundled_list(self):
        bad = {"format": 1, "addons": [{"id": "x", "futureField": True}]}
        a = self.make({r"pages\.example": [(200, {}, json.dumps(bad).encode(), 0)], r"/releases\?": [(200, {}, b"[]", 0)]})
        with mock.patch.object(addons, "REGISTRY_URL", "https://pages.example/addons.json"):
            self.assertEqual([c["id"] for c in a.cards()], ["some-mod"], "nothing valid in it: the bundled one stays")

    def test_the_online_copy_is_on_an_allowed_host(self):
        import urllib.parse
        host = urllib.parse.urlparse(REAL_REGISTRY_URL).hostname
        self.assertIn(host, nf.POLICIES, "netfetch has a policy for the online list's host")


class NetfetchNotModifiedTest(unittest.TestCase):
    def test_304_is_an_answer_not_a_failure(self):
        n = Net({r".": [(304, {"etag": '"x"'}, b"", 0)]})
        for _ in range(8):
            r = n.get("https://api.github.com/repos/a/b/readme", headers={"If-None-Match": '"x"'}, lane="addons")
            self.assertEqual(r.status, 304)
        self.assertEqual(len(n.tr.calls), 8, "the breaker stayed closed")


class SessionTest(Base):
    """The messages through a real server Connection (earthd.py's handlers), with the Addons service on the fake
    GitHub: the list, an add-on's page, Install (progress, then the hand-over for the mod), the mod's answer (logged,
    the list again), and open_page taking an add-on's link."""

    def test_messages(self):
        import test_server
        a = self.make()

        class H(test_server.ServerHarness):
            def runTest(self):
                pass
        h = H()
        h.setUp()
        try:
            h.server._addons = a
            h.c.send({"type": "addons_list"})
            m, _ = h.c.until(lambda m: m["type"] == "addons", 20)
            self.assertEqual([c["id"] for c in m["addons"]], ["some-mod"])
            h.c.send({"type": "addon_detail", "id": "some-mod"})
            m, _ = h.c.until(lambda m: m["type"] == "addon_detail", 20)
            self.assertIn("Some Mod", m["readme"])
            self.assertEqual([r["tag"] for r in m["releases"]], ["build-3-rc", "build-2", "build-1"])
            self.assertTrue(m["releases"][0]["prerelease"])
            h.c.send({"type": "addon_install", "id": "some-mod", "consent": False})
            m, _ = h.c.until(lambda m: m["type"] == "addon_status" and m["state"] == "error", 20)
            self.assertIn("agreed", m["text"])
            with mock.patch.object(addons, "_urlopen", lambda req: FakeResp(ZIP2)):
                h.c.send({"type": "addon_install", "id": "some-mod", "consent": True, "mp": False})
                m, _ = h.c.until(lambda m: m["type"] == "addon_apply", 20)
            self.assertEqual((m["file"], m["tmp"], m["op"]), ("some-mod.zip", "some-mod.zip.bnge-tmp", "install"))
            os.replace(self.mods(m["tmp"]), self.mods(m["file"]))       # (what the game's mod manager does)
            h.c.send({"type": "addon_applied", "id": "some-mod", "op": "install", "ok": True})
            m, _ = h.c.until(lambda m: m["type"] == "addons", 20)
            self.assertEqual((m["addons"][0]["installed"], m["addons"][0]["version"]), (True, "build-2"))
            with mock.patch.object(updates, "open_page", return_value=True) as op:
                h.c.send({"type": "open_page", "url": f"https://github.com/{REPO}/releases"})
                end = time.monotonic() + 5
                while not op.called and time.monotonic() < end:
                    time.sleep(0.02)
            self.assertTrue(op.called)
            self.assertTrue(op.call_args.kwargs["allow"](f"https://github.com/{REPO}/releases"))
        finally:
            h.tearDown()


if __name__ == "__main__":
    unittest.main()
