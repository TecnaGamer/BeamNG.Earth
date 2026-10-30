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
        self.assertFalse(budget.apply(False))
        self.assertEqual(dem.MEM_TILES, self.saved[0], "the desktop sizes stay")
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
        self.assertFalse(budget.apply(False))
        self.assertEqual((imagery.MEM_FINISHED_MB, imagery.MEM_DECODED), self.fin)
        with mock.patch.object(budget, "total_gb", return_value=14.5):
            self.assertTrue(budget.apply(True))
        self.assertEqual(imagery.MEM_FINISHED_MB, 58)
        self.assertEqual(imagery.MEM_DECODED, budget.SMALL_FIN_DECODED)
        imagery.MEM_FINISHED_MB = self.fin[0]
        with mock.patch.object(budget, "total_gb", return_value=4.0):
            budget.apply(True)
        self.assertEqual(imagery.MEM_FINISHED_MB, budget.SMALL_FIN_MB, "at least this")
        im = imagery.Imagery(os.path.join(os.path.dirname(__file__), "_no_cache"), fetcher=mock.Mock())
        self.assertEqual(im.mem_finished, budget.SMALL_FIN_MB * 1_000_000, "read when the cache is made")
        im.close()


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
            self.assertEqual(self.run_child("import os; b=os.nice(0); budget.lower_priority(3); print(os.nice(0)-b)"),
                             "3")


if __name__ == "__main__":
    unittest.main()
