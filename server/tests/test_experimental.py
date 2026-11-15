"""The settings page's Experimental switches (server/experimental.py, 2026-11-15): the settings file's choices as the
BNE_* build switches in the server's environment (launcher.start_server, start.py), nothing for a switch that is off,
and every switch in the cache keys (revisit.switches_key / code_key, decisions.vkey_doc, the baked packs' meta, the
compute helpers' hello, the decoded elevation mirror)."""
import glob
import json
import os
import re
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(ROOT))

import experimental  # noqa: E402
import revisit  # noqa: E402

ALL_VARS = sorted({k for _sid, env, _n in experimental.FEATURES for k in env})


def write_settings(folder, d, name="beamngEarth.json"):
    os.makedirs(os.path.join(folder, "settings"), exist_ok=True)
    with open(os.path.join(folder, "settings", name), "w", encoding="utf-8") as f:
        json.dump(d, f)
    return folder


def clean_env():
    """os.environ without any of the switches (patch.dict restores it)."""
    p = mock.patch.dict(os.environ, {})
    p.start()
    for k in ALL_VARS:
        os.environ.pop(k, None)
    return p


def fresh_keys():
    """revisit's per-process keys made again (they are made once a process)."""
    with revisit._lock:
        revisit._switches.clear()
        revisit._code.clear()
    import bakepack
    bakepack._switches.clear()
    import peerwork
    peerwork._switches.clear()


class ChoiceTest(unittest.TestCase):
    def test_defaults_add_nothing(self):
        for d in ({}, {"version": 3}, {"version": 3, **{sid: False for sid in experimental.IDS}}):
            self.assertEqual(experimental.env_for(d), {}, d)
            self.assertEqual(experimental.chosen(d), [])
        with tempfile.TemporaryDirectory() as folder:
            env = {"PATH": "x"}
            self.assertEqual(experimental.apply(env, folder), [], "no settings file")
            self.assertEqual(env, {"PATH": "x"}, "nothing added: an 'off' written out would change every key")
            self.assertEqual(experimental.apply(env, None), [])
            with open(os.path.join(write_settings(folder, {}), "settings", "beamngEarth.json"), "w") as f:
                f.write("{broken")
            self.assertEqual(experimental.apply(env, folder), [], "an unreadable file: the defaults")
            self.assertEqual(env, {"PATH": "x"})

    def test_each_switch(self):
        for sid, want, needs in experimental.FEATURES:
            with self.subTest(sid=sid):
                d = {"version": 3, sid: True}
                if needs:
                    self.assertEqual(experimental.env_for(d), {}, "without the setting it needs: nothing")
                    d[needs] = True
                got = experimental.env_for(d)
                for k, v in want.items():
                    self.assertEqual(got.get(k), v)
                self.assertIn(sid, experimental.chosen(d))
                self.assertEqual(experimental.env_for({"version": 3, sid: "true"}), {}, "only a real true")

    def test_road_signs_from_version_3(self):
        # (version 2 had the hidden Road signs switch on by default: its true was not the player's choice)
        self.assertEqual(experimental.env_for({"version": 2, "roadSigns": True}), {})
        self.assertEqual(experimental.env_for({"roadSigns": True}), {})
        self.assertEqual(experimental.env_for({"version": 3, "roadSigns": True}), {"BNE_ROADSIGNS": "on"})
        self.assertEqual(experimental.env_for({"version": 2, "expRoadBumps": True}), {"BNE_BUMPS": "1"},
                         "the new switches count in any version")
        self.assertEqual(experimental.env_for({"version": 2, "roadSigns": True, "expNlRoadSigns": True}), {})

    def test_hand_set_variable_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            write_settings(folder, {"version": 3, "expRoadBumps": True, "expRailCollide": True})
            env = {"BNE_BUMPS": "2.5"}
            self.assertEqual(experimental.apply(env, folder), ["expRoadBumps", "expRailCollide"])
            self.assertEqual(env, {"BNE_BUMPS": "2.5", "BNE_RAIL_COLLIDE": "on"})

    def test_lower_case_file_name(self):
        # (the game writes its file names in lower case on Linux: cpupin.player_settings reads both)
        with tempfile.TemporaryDirectory() as folder:
            write_settings(folder, {"version": 3, "expCatenary": True}, name="beamngearth.json")
            if not os.path.exists(os.path.join(folder, "settings", "beamngEarth.json")):    # (a case-sensitive disk)
                self.assertEqual(experimental.env_for(experimental.read_settings(folder)), {"BNE_CATENARY": "1"})

    def test_every_variable_is_read_by_the_server(self):
        src = "".join(Path(p).read_text(encoding="utf-8", errors="replace")
                      for p in glob.glob(str(SERVER / "*.py")) if not p.endswith("experimental.py"))
        for k in ALL_VARS:
            self.assertRegex(src, r"environ\.get\(\s*['\"]" + re.escape(k) + r"['\"]", k)

    def test_the_mod_has_each_setting_off_by_default(self):
        lua = (ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/settings.lua").read_text(encoding="utf-8")
        js = (ROOT / "mod/beamng_earth/ui/ui-vue/mods/beamngEarth/earthSettingsStore.js").read_text(encoding="utf-8")
        order = re.search(r"M\.ORDER = \{(.*?)\}", lua, re.S).group(1)
        for sid in experimental.IDS:
            with self.subTest(sid=sid):
                self.assertRegex(lua, r"\n  " + sid + r" = \{ kind = 'bool', default = false,")
                self.assertIn(f"'{sid}'", order)
                self.assertRegex(js, r'\{ id: "' + sid + r'", type: "bool", default: false,')
        self.assertRegex(lua, r"M\.VERSION = 3\b")
        self.assertEqual(experimental.SINCE_VERSION["roadSigns"], 3)


class KeysTest(unittest.TestCase):
    """Every switch that changes output is in the keys of what is kept: the revisit cache's (code_key, the format
    markers' switches_key), the shared decision archives' (vkey_doc), the baked packs' (meta), the helpers' hello."""

    def setUp(self):
        self.addCleanup(fresh_keys)
        self.addCleanup(clean_env().stop)
        fresh_keys()

    def keys(self, env):
        with mock.patch.dict(os.environ, env):
            fresh_keys()
            return revisit.switches_key()

    def test_each_switch_changes_the_keys(self):
        base = self.keys({})
        seen = {base}
        for sid, env, needs in experimental.FEATURES:
            with self.subTest(sid=sid):
                got = self.keys(experimental.env_for({"version": 3, sid: True, **({needs: True} if needs else {})}))
                if all(k in revisit.NOT_SWITCHES for k in env):
                    # (an older version shown while this one builds: what is kept is this version's either way)
                    self.assertEqual(sid, "expStaleRevisit")
                    self.assertEqual(got, base, "not a key")
                else:
                    self.assertNotEqual(got, base)
                    if not needs:
                        self.assertNotIn(got, seen, "each its own key")
                    seen.add(got)
        self.assertEqual(self.keys({}), base, "off again: the default keys")

    def test_code_key_and_format_markers(self):
        with mock.patch.dict(os.environ, {}):
            fresh_keys()
            code0, mark0 = revisit.code_key(), revisit.format_marker("plans", "x")
        with mock.patch.dict(os.environ, experimental.env_for({"expTunnelLids": True})):
            fresh_keys()
            code1, mark1 = revisit.code_key(), revisit.format_marker("plans", "x")
        self.assertNotEqual(code0, code1, "the road tiles, skins, blocks (blockcache: code_key)")
        self.assertNotEqual(mark0, mark1, "the plans and cell facts (format_key)")

    def test_decision_archives_only_with_the_defaults(self):
        import decisions
        versions = {"overture": "r1"}
        with mock.patch.dict(os.environ, {}):
            fresh_keys()
            v0 = decisions.vkey(dict(versions))
        with mock.patch.dict(os.environ, experimental.env_for({"expRoadBumps": True})):
            fresh_keys()
            doc = decisions.vkey_doc(dict(versions))
            v1 = decisions.vkey(dict(versions))
            self.assertEqual(doc["switches"], revisit.switches_key())
        self.assertNotEqual(v0[0], v1[0], "a player with a switch on never reads the archives built with the defaults")

    def test_baked_packs_need_the_same_switches(self):
        import bakepack

        class MD:
            def versions(self):
                return {"overture": "r1"}
        with mock.patch.dict(os.environ, {}):
            fresh_keys()
            e0 = bakepack._expect_for(MD(), "w1s1")
        with mock.patch.dict(os.environ, experimental.env_for({"expGradeGuard": True})):
            fresh_keys()
            e1 = bakepack._expect_for(MD(), "w1s1")
        self.assertIn("switches", e0)
        self.assertNotEqual(e0["switches"], e1["switches"])
        pack = types.SimpleNamespace(meta=dict(e0))
        self.assertTrue(bakepack._matches(pack, e0))
        self.assertFalse(bakepack._matches(pack, e1), "a pack made with the defaults: not for a player with a switch on")
        old = types.SimpleNamespace(meta={k: v for k, v in e0.items() if k != "switches"})
        self.assertFalse(bakepack._matches(old, e0), "a pack from before the switches were in its meta: not read")
        # (the BeamMP helper and bake_area make theirs with Hong Kong's data off, the cells it changes never shared,
        # and without the bridge profile earthd writes: the same switches as a player's default server)
        with mock.patch.dict(os.environ, {"BNE_ROADZ_HK": "off", "BNE_HKDTM": "off", "BNE_BRIDGE_PROFILE": "curve"}):
            fresh_keys()
            self.assertEqual(bakepack._expect_for(MD(), "w1s1")["switches"], e0["switches"])

    def test_helper_refuses_other_switches(self):
        import peerwork
        helper = peerwork.Helper.__new__(peerwork.Helper)
        helper.workers = 1
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.settimeout(5)
        fresh_keys()
        peerwork.send_msg(a, {"op": "hello", "protocol": peerwork.PROTOCOL, "version": peerwork.version(),
                              "code": peerwork.code_id(), "switches": "0123456789abcdef"})
        helper.handle(b, ("127.0.0.1", 0))
        h, _ = peerwork.recv_msg(a)
        self.assertFalse(h["ok"])
        self.assertIn("build switches", h["error"])
        self.assertEqual(h["switches"], peerwork.switches())

    def test_decoded_elevation_mirror(self):
        import dem

        def path(lidarbc=None, near=False):
            d = dem.Dem.__new__(dem.Dem)
            d.cache_dir, d.hkdtm, d.lidarbc, d.hrdem_near = "/c", None, lidarbc, near
            return d._mirror_path((15, 5000, 11000))
        base = path()
        self.assertIn(os.sep + "g1" + os.sep, base, "off: the mirror's tiles as before")
        self.assertNotEqual(path(lidarbc=object()), base, "BNE_LIDARBC's tiles apart")
        self.assertNotEqual(path(near=True), base, "BNE_HRDEM_NEAR's tiles apart")
        self.assertNotEqual(path(lidarbc=object()), path(near=True))


class WiringTest(unittest.TestCase):
    """The launcher and start.py start the server with the switches in its environment."""

    def setUp(self):
        self.addCleanup(clean_env().stop)

    def test_launcher_start_server(self):
        import launcher
        for d, want in (({}, {}),
                        ({"version": 3, "roadSigns": True, "expEuScenery": True},
                         {"BNE_ROADSIGNS": "on", "BNE_EU_ROAD_DETAILS": "on", "BNE_FR_ROADS": "on",
                          "BNE_FR_HOUSES": "on"})):
            with self.subTest(d=d), tempfile.TemporaryDirectory() as folder:
                write_settings(folder, d)
                with mock.patch.object(launcher.subprocess, "Popen", return_value=mock.Mock()) as popen, \
                        mock.patch.object(launcher.threading, "Thread"), \
                        mock.patch.object(launcher, "say"):
                    launcher.start_server(folder, [], os.path.join(folder, "server.log"))
                env = popen.call_args.kwargs["env"]
                got = {k: v for k, v in env.items() if k in ALL_VARS}
                self.assertEqual(got, want)
                self.assertEqual({k for k in env if k.startswith("BNE_")} - set(want),
                                 {k for k in os.environ if k.startswith("BNE_")}, "nothing else added")

    def test_start_py(self):
        import start
        with tempfile.TemporaryDirectory() as folder:
            write_settings(folder, {"version": 3, "expAiTunnels": True})
            env = {}
            self.assertEqual(start.experimental_env(folder, env), ["expAiTunnels"])
            self.assertEqual(env, {"BNE_AI_TUNNELS": "on"})
            env = {}
            self.assertEqual(start.experimental_env(os.path.join(folder, "none"), env), [])
            self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
