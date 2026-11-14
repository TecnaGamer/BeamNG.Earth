"""The server's RAM limit (the settings page's "Max RAM for BeamNG.Earth"): budget.py's limit and tiers by it, the
settings file read at start (cpupin.player_settings), the watchdog's requests to the workers (budget.shed_ask /
shed_asked) and its steps (memwatch._Watch: shed, then replace the pool holding the largest process)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import budget  # noqa: E402
import cpupin  # noqa: E402
import memwatch  # noqa: E402


class LimitTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"BNGE_BUDGET": "", budget.SETTING_ENV: ""})
        p.start()
        self.addCleanup(p.stop)

    def test_auto_by_ram(self):
        self.assertAlmostEqual(budget.limit_mb(16.0), 0.33 * 16e9 / 2 ** 20, places=3)
        self.assertAlmostEqual(budget.limit_mb(32.0), 0.6 * 32e9 / 2 ** 20, places=3, msg="a big machine: 60 %")
        self.assertEqual(budget.limit_mb(64.0), 24576.0, "the cap: more RAM does not mean bigger caches")
        self.assertEqual(budget.limit_mb(128.0), 24576.0)
        self.assertEqual(budget.limit_mb(4.0), 1536.0, "the floor")
        with mock.patch.object(budget, "total_gb", return_value=None):
            self.assertEqual(budget.limit_mb(), 0.0, "unknown RAM, Auto: no limit")
            self.assertEqual(budget.tier(), "desktop")

    def test_setting_sets_limit_and_tier(self):
        with mock.patch.dict(os.environ, {budget.SETTING_ENV: "4"}):
            self.assertEqual(budget.setting_gb(), 4.0)
            self.assertEqual(budget.limit_mb(64.0), 4096.0)
            self.assertEqual(budget.tier(64.0), "small", "4 GB on a big PC: the small profile's caches")
            self.assertTrue(budget.small(64.0))
            self.assertFalse(budget.small_machine(64.0), "the CPU share stays the big PC's")
        with mock.patch.dict(os.environ, {budget.SETTING_ENV: "2"}):
            self.assertEqual(budget.tier(64.0), "lean")
        with mock.patch.dict(os.environ, {budget.SETTING_ENV: "32"}):
            self.assertEqual(budget.limit_mb(16.0), 0.9 * 16e9 / 2 ** 20, "never past 90 % of the RAM")
        for junk in ("auto", "", "-3", "x", "0"):
            with mock.patch.dict(os.environ, {budget.SETTING_ENV: junk}):
                self.assertEqual(budget.setting_gb(), 0.0, junk)

    def test_desktop_sizes(self):
        self.assertIsNone(budget.desktop_sizes(0))
        dem, md, shed, rot, road, far = budget.desktop_sizes(16384.0)
        self.assertEqual((dem, md, shed, rot), (512, 256, 3000, 4000))
        small = budget.desktop_sizes(budget.limit_mb(24.0))
        self.assertLess(small[0], 512)
        self.assertGreaterEqual(small[4], 3)


class HelpTest(unittest.TestCase):
    def test_server_help(self):
        """earthd's --help (argparse checks every help string: a bare '%' in --max-ram-gb's kept the server from
        starting at all on Python 3.14)."""
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                         "earthd.py"), "--help"], capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("--max-ram-gb", r.stdout)


class SettingsFileTest(unittest.TestCase):
    def _settings(self, d):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "settings"))
        with open(os.path.join(tmp, "settings", "beamngEarth.json"), "w") as f:
            json.dump(d, f)
        return tmp

    def test_read_and_applied(self):
        self.assertEqual(cpupin.player_settings(self._settings({"maxRamGb": "6"})).get("max_ram_gb"), 6.0)
        self.assertNotIn("max_ram_gb", cpupin.player_settings(self._settings({"maxRamGb": "auto"})))
        self.assertNotIn("max_ram_gb", cpupin.player_settings(self._settings({"maxRamGb": "nonsense"})))
        env = {}
        cpupin.apply_player_settings(self._settings({"maxRamGb": "6"}), 10, 10, environ=env)
        self.assertEqual(env.get(budget.SETTING_ENV), "6")
        env = {budget.SETTING_ENV: "3"}         # (--max-ram-gb, or the environment: wins)
        cpupin.apply_player_settings(self._settings({"maxRamGb": "6"}), 10, 10, environ=env)
        self.assertEqual(env[budget.SETTING_ENV], "3")


class AskTest(unittest.TestCase):
    """The watchdog's shed request through the shared block's header."""

    def setUp(self):
        self.saved = (budget._tree_shm, budget._tree_owner, budget._asked_seen, os.environ.get(budget.TREE_ENV))
        budget._tree_shm = None
        os.environ.pop(budget.TREE_ENV, None)
        self.assertTrue(budget.tree_init())

    def tearDown(self):
        budget.tree_close()
        budget._tree_shm, budget._tree_owner, budget._asked_seen, env = self.saved
        if env is None:
            os.environ.pop(budget.TREE_ENV, None)
        else:
            os.environ[budget.TREE_ENV] = env

    def test_asked_once_over_target(self):
        self.assertIsNone(budget.shed_asked(5000), "nothing asked yet")
        self.assertTrue(budget.shed_ask(1000))
        self.assertIsNotNone(budget.shed_asked(1500), "over the target: shed")
        self.assertIsNone(budget.shed_asked(1500), "the same request answered once")
        self.assertTrue(budget.shed_ask(2000))
        self.assertIsNone(budget.shed_asked(1500), "under the target: kept")
        self.assertTrue(budget.shed_ask(100))
        self.assertIsNone(budget.shed_asked(budget.SHED_FLOOR_MB - 1), "under the floor: kept")


class _Pool:
    def __init__(self, pids, inflight=0, workers=1):
        self._pool = mock.Mock(_processes={pid: None for pid in pids})
        self.inflight, self.workers = inflight, workers
        self.rotated = []

    def rotate(self, why, now=None):
        self.rotated.append(why)
        return True


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(memwatch._state), budget.OVER, budget.LOW_FREE_GB, budget.LIMIT_MB
        memwatch._state.update(limit=4000.0, peak=0.0, sheds=0, rotations=0)
        budget.LOW_FREE_GB = 1.0
        self.t = [1000.0]
        self.w = memwatch._Watch(clock=lambda: self.t[0])
        self.asks = []
        p = mock.patch.object(budget, "shed_ask", side_effect=lambda mb: self.asks.append(mb) or True)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        memwatch._state.clear()
        memwatch._state.update(self.saved[0])
        budget.OVER, budget.LOW_FREE_GB, budget.LIMIT_MB = self.saved[1:]

    def test_under_soft_nothing(self):
        a, b = _Pool([11]), _Pool([12])
        self.assertEqual(self.w.step({1: 1000, 11: 1000, 12: 1000}, 3000, 8000, [a, b]), "ok")
        self.assertFalse(budget.OVER)
        self.assertEqual(self.asks, [])

    def test_shed_then_replace_the_largest(self):
        a, b, busy = _Pool([11]), _Pool([12, 13], workers=2), _Pool([14], inflight=5)
        mbs = {1: 1500, 11: 800, 12: 900, 13: 1200, 14: 2000}
        total = sum(mbs.values())
        self.assertEqual(self.w.step(mbs, total, 8000, [a, b, busy]), "shed")
        self.assertTrue(budget.OVER)
        self.assertAlmostEqual(self.asks[0], memwatch.SHED_SHARE * 4000 / 5)
        self.t[0] += 10
        self.assertEqual(self.w.step(mbs, total, 8000, [a, b, busy]), "wait")
        self.t[0] += 15             # (over the limit 25 s, the shed 25 s ago)
        self.assertEqual(self.w.step(mbs, total, 8000, [a, b, busy]), "rotate")
        self.assertEqual((len(a.rotated), len(b.rotated), len(busy.rotated)), (0, 1, 0),
                         "the largest process's pool without a backlog")
        self.assertEqual(memwatch._state["rotations"], 1)

    def test_short_system_lowers_the_limit(self):
        mbs = {1: 1000, 11: 1000, 12: 1000}         # 3000 of 4000, but 200 MB available of a 954 MB reserve
        self.assertEqual(self.w.step(mbs, 3000, 200, [_Pool([11]), _Pool([12])]), "shed")
        self.assertLess(memwatch._state["eff"], 3000)

    def test_status_and_live_setting(self):
        with mock.patch.dict(os.environ, {budget.SETTING_ENV: ""}), \
                mock.patch.object(budget, "total_gb", return_value=64.0), \
                mock.patch.object(memwatch, "start", return_value=True):
            mb = memwatch.set_setting(6)
            self.assertEqual(mb, 6144.0)
            s = memwatch.status()
            self.assertEqual((s["limitMB"], s["setting"], s["autoMB"]), (6144, 6.0, 24576))
            self.assertEqual(os.environ[budget.SETTING_ENV], "6.0")
            memwatch.set_setting("bad")
            self.assertEqual(memwatch.status()["limitMB"], 24576)


if __name__ == "__main__":
    unittest.main()
