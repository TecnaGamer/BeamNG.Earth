"""budget.py: the small-machine profile (a Steam Deck: 8.2 GB of server caches next to the game, the game killed by
the system's out-of-memory guard, 2026-10-20)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import budget  # noqa: E402
import dem  # noqa: E402
import imagery  # noqa: E402
import mapdata  # noqa: E402
import roadwidth  # noqa: E402
import seacarve  # noqa: E402


class BudgetTest(unittest.TestCase):
    def setUp(self):
        import copy
        import farfield
        self.saved = (dem.MEM_TILES, imagery.MEM_TILES, mapdata.MEM_MB, seacarve.MEM_CELLS, roadwidth.WALL_CELLS)
        self.fin = (imagery.MEM_FINISHED_MB, imagery.MEM_DECODED)
        self.far = (copy.deepcopy(farfield.K_TABLE), copy.deepcopy(farfield.TEX_DEFAULT))
        # (the tiers by the RAM these tests mock, whatever tier or limit the runner forces: BNGE_BUDGET, the Max RAM)
        p = mock.patch.dict(os.environ, {"BNGE_BUDGET": "", budget.SETTING_ENV: ""})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        import farfield
        dem.MEM_TILES, imagery.MEM_TILES, mapdata.MEM_MB, seacarve.MEM_CELLS, roadwidth.WALL_CELLS = self.saved
        imagery.MEM_FINISHED_MB, imagery.MEM_DECODED = self.fin
        farfield.K_TABLE.clear(), farfield.K_TABLE.update(self.far[0])
        farfield.TEX_DEFAULT.clear(), farfield.TEX_DEFAULT.update(self.far[1])

    def test_profile_by_ram(self):
        with mock.patch.dict(os.environ, {"BNGE_BUDGET": ""}):
            self.assertTrue(budget.small(14.5), "a Steam Deck")
            self.assertFalse(budget.small(64.0), "a desktop")
            self.assertFalse(budget.small(None) and budget.total_gb() is None, "unknown RAM: desktop")
        with mock.patch.dict(os.environ, {"BNGE_BUDGET": "small"}):
            self.assertTrue(budget.small(64.0))
        with mock.patch.dict(os.environ, {"BNGE_BUDGET": "desktop"}):
            self.assertFalse(budget.small(8.0))

    def test_road_processes_by_threads_and_memory(self):
        self.assertEqual(budget.road_processes(16, 62.0), 4)
        self.assertEqual(budget.road_processes(12, 16.0), 3)     # (16 GB: not four ~1.5 GB processes)
        self.assertEqual(budget.road_processes(8, 16.0), 3)
        self.assertEqual(budget.road_processes(6, 16.0), 2)
        self.assertEqual(budget.road_processes(16, 8.0), 2)

    def test_small_caches(self):
        with mock.patch.object(budget, "total_gb", return_value=64.0):     # (Auto's cap: the desktop sizes)
            self.assertFalse(budget.apply(False))
        self.assertEqual(dem.MEM_TILES, self.saved[0], "the desktop sizes stay")
        with mock.patch.object(budget, "total_gb", return_value=14.5):     # (a Steam Deck: not a lean tier)
            self.assertTrue(budget.apply(True))
        self.assertEqual((dem.MEM_TILES, roadwidth.WALL_CELLS, mapdata.MEM_MB),
                         (budget.SMALL_DEM_TILES, budget.SMALL_WALL_CELLS, budget.SMALL_MAPDATA_MB))
        self.assertEqual((imagery.MEM_TILES, seacarve.MEM_CELLS), (self.saved[1], self.saved[3]),
                         "these keep theirs (smaller, they thrashed)")
        self.assertGreaterEqual(mapdata.MEM_MB, 128, "a road tile's cells fit (Baltimore's: ~124 MB)")
        d = dem.Dem(os.path.join(os.path.dirname(__file__), "_no_cache"), fetcher=mock.Mock())
        self.assertEqual(d.mem_tiles, budget.SMALL_DEM_TILES, "read when the cache is made")

    def test_small_finished_imagery(self):
        """The finished imagery pyramid's memory by the RAM on a small machine (a Steam Deck: ~58 MB of its JPEGs,
        96 decoded, for the desktop's 160 MB and 384), the desktop's kept."""
        with mock.patch.object(budget, "total_gb", return_value=64.0):
            self.assertFalse(budget.apply(False))
        self.assertEqual((imagery.MEM_FINISHED_MB, imagery.MEM_DECODED), self.fin)
        with mock.patch.object(budget, "total_gb", return_value=14.5):
            self.assertTrue(budget.apply(True))
        self.assertEqual(imagery.MEM_FINISHED_MB, 58)
        self.assertEqual(imagery.MEM_DECODED, budget.SMALL_FIN_DECODED)
        imagery.MEM_FINISHED_MB = self.fin[0]
        with mock.patch.object(budget, "total_gb", return_value=4.0), mock.patch.object(budget, "LEAN_ON", False):
            budget.apply(True)
        self.assertEqual(imagery.MEM_FINISHED_MB, budget.SMALL_FIN_MB, "at least this (the lean tiers off)")
        im = imagery.Imagery(os.path.join(os.path.dirname(__file__), "_no_cache"), fetcher=mock.Mock())
        self.assertEqual(im.mem_finished, budget.SMALL_FIN_MB * 1_000_000, "read when the cache is made")
        im.close()


class LeanTierTest(unittest.TestCase):
    """The lean memory tiers (2026-11-12, "also check for machines with 8gb or even 4gb of ram"): an 8 GB PC's
    server in four processes with smaller caches and a coarser far field, a 4 GB one smaller still."""

    def setUp(self):
        import copy
        import farfield
        import pmtiles
        import photosurface
        import rangeread
        import roads
        self.mods = [(m, n, getattr(m, n)) for m, n in (
            (dem, "MEM_TILES"), (imagery, "MEM_TILES"), (imagery, "MEM_FINISHED_MB"), (imagery, "MEM_DECODED"),
            (mapdata, "MEM_MB"), (seacarve, "MEM_CELLS"), (roadwidth, "WALL_CELLS"), (pmtiles, "MEM_TILES_MB"),
            (rangeread, "MEM_BYTES"), (photosurface, "MEM_TILES"), (roads, "FACTS_MEM_KEPT"),
            (budget, "SHED_MB"), (budget, "ROTATE_MB"), (budget, "TIER"), (budget, "LOW_FREE_GB"),
            (budget, "TREE_MB"))]
        self.far = (copy.deepcopy(farfield.K_TABLE), copy.deepcopy(farfield.TEX_DEFAULT))
        p = mock.patch.dict(os.environ, {"BNGE_BUDGET": "", "BNGE_SHED_MB": "", "BNGE_ROTATE_MB": "",
                                         "BNGE_DEM_MEM_TILES": ""})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        import farfield
        for m, n, v in self.mods:
            setattr(m, n, v)
        farfield.K_TABLE.clear(), farfield.K_TABLE.update(self.far[0])
        farfield.TEX_DEFAULT.clear(), farfield.TEX_DEFAULT.update(self.far[1])

    def test_tier_by_ram(self):
        self.assertEqual(budget.tier(64.0), "desktop")
        self.assertEqual(budget.tier(14.5), "small", "a Steam Deck")
        self.assertEqual(budget.tier(12.5), "small", "a 12 GB PC: the small profile's tiny sizes")
        self.assertEqual(budget.tier(8.3), "lean", "an 8 GB PC")
        self.assertEqual(budget.tier(7.6), "lean")
        self.assertEqual(budget.tier(6.3), "lean", "a 6 GB PC")
        self.assertEqual(budget.tier(4.1), "emergency", "a 4 GB PC")
        self.assertTrue(budget.small(8.3) and budget.lean(8.3) and budget.lean(4.1) and not budget.lean(14.5))
        with mock.patch.object(budget, "LEAN_ON", False):
            self.assertEqual(budget.tier(4.1), "small", "BNGE_LEAN=off: the small profile as before")
        with mock.patch.dict(os.environ, {"BNGE_BUDGET": "lean"}):
            self.assertEqual(budget.tier(64.0), "lean")
            self.assertTrue(budget.small(64.0))
        with mock.patch.dict(os.environ, {"BNGE_BUDGET": "emergency"}):
            self.assertEqual(budget.tier(64.0), "emergency")
        with mock.patch.dict(os.environ, {"BNGE_BUDGET": "desktop"}):
            self.assertEqual(budget.tier(4.0), "desktop")
            self.assertFalse(budget.small(4.0))

    def _apply(self, gb):
        with mock.patch.object(budget, "total_gb", return_value=gb):
            return budget.apply()

    def test_lean_sizes(self):
        import farfield
        import pmtiles
        import rangeread
        k0, tex0 = dict(farfield.K_TABLE[64]), farfield.TEX_DEFAULT[64]
        self.assertTrue(self._apply(8.3))
        self.assertEqual(budget.TIER, "lean")
        self.assertEqual((budget.SHED_MB, budget.ROTATE_MB), (budget.LEAN_SHED_MB, budget.LEAN_ROTATE_MB))
        self.assertEqual((dem.MEM_TILES, mapdata.MEM_MB, roadwidth.WALL_CELLS),
                         (budget.LEAN_DEM_TILES, budget.LEAN_MAPDATA_MB, budget.LEAN_WALL_CELLS))
        self.assertGreaterEqual(mapdata.MEM_MB, 128, "a road tile's cells fit (Baltimore's: ~124 MB)")
        self.assertEqual((imagery.MEM_TILES, pmtiles.MEM_TILES_MB, rangeread.MEM_BYTES, seacarve.MEM_CELLS),
                         (budget.LEAN_IMAGERY_TILES, budget.LEAN_PMTILES_MB, budget.LEAN_RANGEREAD_MB * 2 ** 20,
                          budget.LEAN_SEA_CELLS))
        self.assertLessEqual(imagery.MEM_FINISHED_MB, budget.LEAN_FIN_MB)
        self.assertAlmostEqual(farfield.K_TABLE[64][1], k0[1] * budget.LEAN_FAR_K)
        self.assertEqual(farfield.TEX_DEFAULT[64], tuple(min(t, budget.LEAN_FAR_TEX) for t in tex0))

    def test_emergency_sizes(self):
        import farfield
        k0 = dict(farfield.K_TABLE[64])
        self.assertTrue(self._apply(4.1))
        self.assertEqual(budget.TIER, "emergency")
        self.assertEqual((budget.SHED_MB, budget.ROTATE_MB),
                         (budget.EMERGENCY_SHED_MB, budget.EMERGENCY_ROTATE_MB))
        self.assertEqual((dem.MEM_TILES, mapdata.MEM_MB), (budget.EMERGENCY_DEM_TILES, budget.EMERGENCY_MAPDATA_MB))
        self.assertAlmostEqual(farfield.K_TABLE[64][1], k0[1] * budget.EMERGENCY_FAR_K)
        self.assertEqual(max(farfield.TEX_DEFAULT[64]), budget.EMERGENCY_FAR_TEX)

    def test_lean_off_keeps_the_tiny_sizes(self):
        with mock.patch.object(budget, "LEAN_ON", False):
            self.assertTrue(self._apply(8.3))
        self.assertEqual(budget.TIER, "small")
        self.assertEqual((dem.MEM_TILES, mapdata.MEM_MB, budget.SHED_MB),
                         (budget.TINY_DEM_TILES, budget.TINY_MAPDATA_MB, budget.TINY_SHED_MB))

    def test_a_small_machine_unchanged(self):
        self.assertTrue(self._apply(14.5))
        self.assertEqual(budget.TIER, "small")
        self.assertEqual((dem.MEM_TILES, budget.SHED_MB), (budget.SMALL_DEM_TILES, budget.SMALL_SHED_MB))

    def test_memory_notice(self):
        self.assertIsNone(budget.memory_notice(16.5))
        self.assertIsNone(budget.memory_notice(64.0))
        head, text = budget.memory_notice(8.3)
        self.assertEqual(head, "Low memory")
        self.assertIn("8 GB", text)
        self.assertIn("16 GB", text, "what the game asks for")
        head, text = budget.memory_notice(4.1)
        self.assertEqual(head, "Not enough memory")
        self.assertIn("4 GB", text)
        self.assertIn("close every other program", text)
        with mock.patch.object(budget, "LEAN_ON", False):
            self.assertIsNone(budget.memory_notice(4.1), "BNGE_LEAN=off: no notice either")


class CgroupMemoryTest(unittest.TestCase):
    """budget.free_gb / total_gb under a cgroup v2 memory limit (a container, the low-memory tests' capped scopes):
    the room under the limit counts when smaller than the machine's."""

    def _tree(self, limits):
        import tempfile
        root = tempfile.mkdtemp(prefix="cgtest_")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        base = os.path.join(root, "sys", "fs", "cgroup")
        d = base
        for name, mx, cur, inactive in limits:
            d = os.path.join(d, name)
            os.makedirs(d)
            open(os.path.join(d, "memory.max"), "w").write(mx + "\n")
            open(os.path.join(d, "memory.current"), "w").write(f"{cur}\n")
            open(os.path.join(d, "memory.stat"), "w").write(f"anon {cur}\ninactive_file {inactive}\n")
        return d, base

    def test_tightest_limit(self):
        d, base = self._tree([("user.slice", "max", 9_000_000_000, 0),
                              ("app.slice", "4000000000", 3_000_000_000, 500_000_000),
                              ("run.scope", "1500000000", 1_400_000_000, 100_000_000)])
        self.assertAlmostEqual(budget.cgroup_limit_gb(d, base), 1.5)
        # run.scope: 1.5 - 1.4 + 0.1 inactive cache = 0.2 GB; app.slice 4 - 3 + 0.5 = 1.5: the tighter wins
        self.assertAlmostEqual(budget.cgroup_free_gb(d, base), 0.2)

    def test_unlimited(self):
        d, base = self._tree([("user.slice", "max", 9_000_000_000, 0), ("run.scope", "max", 1_000_000_000, 0)])
        self.assertIsNone(budget.cgroup_limit_gb(d, base))
        self.assertIsNone(budget.cgroup_free_gb(d, base))

    def test_free_and_total_take_the_limit(self):
        with mock.patch.object(budget, "cgroup_free_gb", return_value=0.25), \
                mock.patch.object(budget, "cgroup_limit_gb", return_value=1.5), \
                mock.patch.dict(os.environ, {"BNGE_TOTAL_GB": ""}):
            if sys.platform.startswith("linux"):
                self.assertAlmostEqual(budget.free_gb(), 0.25)
                self.assertAlmostEqual(budget.total_gb(), 1.5)
            with mock.patch.object(budget, "CGROUP_MEM", False):
                self.assertGreater(budget.total_gb() or 99, 1.5, "BNGE_CGROUP_MEM=off: the machine's")

    def test_no_cgroup(self):
        self.assertIsNone(budget.cgroup_free_gb(""))
        self.assertIsNone(budget.cgroup_limit_gb(""))


class PriorityTest(unittest.TestCase):
    """lower_priority in a child process: Windows' classes (below normal for the server; a worker keeps its
    parent's, never idle: starved on the server's few cores), elsewhere niceness."""

    def run_child(self, code):
        import subprocess
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {here!r}); import budget\n" + code],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_lowered(self):
        self.assertIsNone(budget.lower_priority(0))
        if sys.platform == "win32":
            get = ("import ctypes; from ctypes import wintypes; k=ctypes.WinDLL('kernel32'); "
                   "k.GetCurrentProcess.restype=wintypes.HANDLE; k.GetPriorityClass.argtypes=(wintypes.HANDLE,); "
                   "print(budget.lower_priority({n}, {w}), hex(k.GetPriorityClass(k.GetCurrentProcess())))")
            self.assertEqual(self.run_child(get.format(n=10, w=False)), "below normal 0x4000")
            for n in (3, 6, 9):
                self.assertTrue(self.run_child(get.format(n=n, w=True)).startswith("None "), "a worker: as inherited")
        elif hasattr(os, "nice"):
            # (+3, or up to the ceiling of 19 when the test itself already runs nice: a niced shell's gate run)
            got = self.run_child("import os; b=os.nice(0); budget.lower_priority(3); print(os.nice(0)-b, b)").split()
            self.assertEqual(int(got[0]), min(3, 19 - int(got[1])))


class RaiseThreadTest(unittest.TestCase):
    """raise_thread: the event loop's thread one step above the process's others on Windows, nothing elsewhere."""

    def test_raised(self):
        if sys.platform != "win32":
            self.assertIsNone(budget.raise_thread())
            return
        import threading
        got = []

        def run():          # (a thread of its own: the test runner's keeps its priority)
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32")
            k.GetCurrentThread.restype = wintypes.HANDLE
            k.GetThreadPriority.argtypes = (wintypes.HANDLE,)
            got.append((budget.raise_thread(), k.GetThreadPriority(k.GetCurrentThread())))
        t = threading.Thread(target=run)
        t.start()
        t.join()
        self.assertEqual(got, [("thread above normal", 1)])


if __name__ == "__main__":
    unittest.main()
