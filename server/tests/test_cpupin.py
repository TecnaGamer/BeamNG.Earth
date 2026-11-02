"""cpupin: the core split and the loading boost's CPU sets."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cpupin  # noqa: E402

CORES = [((0, c), [c, c + 8]) for c in range(8)]     # 8 cores, 16 CPUs, siblings c and c + 8


def fake_siblings(usable=None):
    return [(k, c) for k, c in CORES if usable is None or set(c) <= set(usable)]


class BoostTest(unittest.TestCase):
    def test_split_and_boost(self):
        with mock.patch.object(cpupin, "_siblings", side_effect=fake_siblings):
            server, game = cpupin.split()
            self.assertEqual(server, {6, 7, 14, 15})
            b = cpupin.boosted(server, game)
        self.assertTrue(server < b)
        self.assertEqual(b - server, {3, 4, 5, 11, 12, 13})     # the game's last three of six cores, both siblings
        self.assertLessEqual(len(b & game), len(game) // 2)

    def test_boost_from_a_pinned_process(self):
        # the server calls boosted() after pinning itself to its own CPUs: the game's are still found
        with mock.patch.object(cpupin, "_siblings", side_effect=fake_siblings), \
                mock.patch.object(cpupin.os, "sched_getaffinity", return_value={6, 7, 14, 15}, create=True):
            self.assertEqual(cpupin.boosted({6, 7, 14, 15}, {0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13}),
                             {3, 4, 5, 6, 7, 11, 12, 13, 14, 15})

    def test_full_split_after_a_teleport(self):
        # the settings page's teleportPriority: the server on every CPU, the game on its first two cores alone
        with mock.patch.object(cpupin, "_siblings", side_effect=fake_siblings):
            server, game = cpupin.split()
            full = cpupin.full_split(server, game)
        self.assertEqual(full[0], server | game)
        self.assertEqual(full[1], {0, 1, 8, 9})
        with mock.patch.object(cpupin, "_siblings", side_effect=fake_siblings):
            self.assertIsNone(cpupin.full_split({6, 14}, {0, 1, 8, 9}), "no cores to take from a two-core game")
        self.assertIsNone(cpupin.full_split(None, {1, 2}))

    def test_teleport_priority_setting_read(self):
        import json
        import tempfile
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "settings"))
        with open(os.path.join(d, "settings", "beamngEarth.json"), "w") as f:
            json.dump({"teleportPriority": True}, f)
        self.assertTrue(cpupin.player_settings(d).get("teleport_full"))
        with open(os.path.join(d, "settings", "beamngEarth.json"), "w") as f:
            json.dump({"teleportPriority": False}, f)
        self.assertFalse(cpupin.player_settings(d).get("teleport_full"))

    def test_nothing_to_lend(self):
        self.assertIsNone(cpupin.boosted(None, {1, 2}))
        self.assertIsNone(cpupin.boosted({1}, set()))

    @unittest.skipUnless(hasattr(os, "sched_getaffinity"), "Linux")
    def test_pin_tree_keeps_this_process(self):
        cpus = os.sched_getaffinity(0)
        self.assertGreaterEqual(cpupin.pin_tree(cpus), 1)
        self.assertEqual(os.sched_getaffinity(0), cpus)


if __name__ == "__main__":
    unittest.main()


class WindowsSplitTest(unittest.TestCase):
    """KyzoFox 1.5.56: the split on Windows, from the cores' processor masks (an i5-11400H: 6 cores, 12 threads)."""

    def setUp(self):
        self.masks = [0b11 << (2 * i) for i in range(6)]
        self.old = cpupin._siblings
        cores = cpupin.cores_from_masks(self.masks, list(range(12)))
        cpupin._siblings = lambda usable=None: (cores if usable is None else
                                                cpupin.cores_from_masks(self.masks, usable))

    def tearDown(self):
        cpupin._siblings = self.old
        os.environ.pop("BNGE_SERVER_CORES", None)

    def test_cores_from_masks(self):
        cores = cpupin.cores_from_masks(self.masks, list(range(12)))
        self.assertEqual([c for _k, c in cores], [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9], [10, 11]])
        # a process limited to some CPUs sees only those
        self.assertEqual([c for _k, c in cpupin.cores_from_masks(self.masks, [0, 1, 2])], [[0, 1], [2]])

    def test_two_cores_for_the_server_four_for_the_game(self):
        server, game = cpupin.split()
        self.assertEqual(server, {8, 9, 10, 11})
        self.assertEqual(game, set(range(8)))

    def test_server_cores_setting(self):
        os.environ["BNGE_SERVER_CORES"] = "1"
        self.assertEqual(cpupin.split()[0], {10, 11})
        os.environ["BNGE_SERVER_CORES"] = "5"           # never more than half
        self.assertEqual(len(cpupin.split()[0]), 6)
        os.environ["BNGE_SERVER_CORES"] = "x"
        self.assertEqual(cpupin.split()[0], {8, 9, 10, 11})

    def test_mask(self):
        self.assertEqual(cpupin._mask({8, 9, 10, 11}), 0xF00)
        self.assertEqual(cpupin._bits(0xF00), [8, 9, 10, 11])


class PlayerSettingsTest(unittest.TestCase):
    """The settings page's Performance section (GitHub #529): the server's cores and priority from the mod's settings
    file in the game's user folder, where nothing was set by hand."""

    def write(self, d, name="beamngEarth.json"):
        import json
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "settings"))
        with open(os.path.join(tmp.name, "settings", name), "w", encoding="utf-8") as f:
            json.dump(d, f)
        return tmp.name

    def test_choices_read(self):
        self.assertEqual(cpupin.player_settings(self.write({"serverCores": "2", "serverPriority": "normal"})),
                         {"cores": 2, "priority": "normal"})
        # (Linux: the game writes its file names in lower case)
        self.assertEqual(cpupin.player_settings(self.write({"serverCores": "3"}, "beamngearth.json")), {"cores": 3})
        for d in ({}, {"serverCores": "auto", "serverPriority": "low"}, {"serverCores": "99", "serverPriority": 1},
                  {"serverCores": None}, ["not", "a", "dict"]):
            with self.subTest(d=d):
                self.assertEqual(cpupin.player_settings(self.write(d)), {}, "the defaults")
        self.assertEqual(cpupin.player_settings(None), {})
        self.assertEqual(cpupin.player_settings("/nonexistent/user/dir"), {}, "no settings file: the defaults")

    def test_applied_where_nothing_was_set_by_hand(self):
        user = self.write({"serverCores": "2", "serverPriority": "normal"})
        env = {}
        nice, notes = cpupin.apply_player_settings(user, 10, 10, env)
        self.assertEqual((nice, env.get("BNGE_SERVER_CORES")), (0, "2"))
        self.assertEqual(notes, ["2 cores", "normal priority"])
        env = {"BNGE_SERVER_CORES": "1"}                    # run_server.bat's own line, a --nice given: they win
        nice, notes = cpupin.apply_player_settings(user, 5, 10, env)
        self.assertEqual((nice, env["BNGE_SERVER_CORES"], notes), (5, "1", []))
        env = {}
        self.assertEqual(cpupin.apply_player_settings(self.write({}), 10, 10, env), (10, []))
        self.assertEqual(env, {}, "the defaults: nothing changed")

    def test_two_of_four_cores(self):
        # the reporter's i5-3470 (4 cores, no hyperthreads): the default split gives the server one core, the setting 2
        four = [((0, c), [c]) for c in range(4)]
        env = {}
        cpupin.apply_player_settings(self.write({"serverCores": "2"}), 10, 10, env)
        with mock.patch.object(cpupin, "_siblings", return_value=four), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BNGE_SERVER_CORES", None)
            self.assertEqual(cpupin.split(), ({3}, {0, 1, 2}), "(the default: one core)")
            os.environ["BNGE_SERVER_CORES"] = env["BNGE_SERVER_CORES"]
            self.assertEqual(cpupin.split(), ({2, 3}, {0, 1}), "the setting: two")
            os.environ["BNGE_SERVER_CORES"] = "4"
            self.assertEqual(len(cpupin.split()[0]), 2, "never more than half")

    def test_the_page_offers_what_the_server_reads(self):
        import re
        mod = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mod",
                           "beamng_earth")
        with open(os.path.join(mod, "lua", "ge", "extensions", "beamngEarth", "settings.lua"), encoding="utf-8") as f:
            lua = f.read()
        with open(os.path.join(mod, "ui", "ui-vue", "mods", "beamngEarth", "earthSettingsStore.js"),
                  encoding="utf-8") as f:
            page = f.read()
        for key, want in (("serverCores", ("auto",) + cpupin.CORE_CHOICES), ("serverPriority", cpupin.PRIORITIES)):
            with self.subTest(key=key):
                m = re.search(key + r" = \{ kind = 'enum', default = '(\w+)', values = \{ ([^}]*) \}", lua)
                self.assertIsNotNone(m, "the mod's setting")
                self.assertEqual(tuple(re.findall(r"'(\w+)'", m.group(2))), want)
                self.assertEqual(m.group(1), want[0], "the default first")
                row = re.search(r'\{ id: "' + key + r'".*?options: \[([^\]]*)\]', page, re.S)
                self.assertIsNotNone(row, "the page's row")
                self.assertEqual(tuple(re.findall(r'value: "(\w+)"', row.group(1))), want)
