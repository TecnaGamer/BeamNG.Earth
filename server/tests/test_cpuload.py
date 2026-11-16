"""cpuload: the cores each group of the server's processes used since the last look (the fast drive's log line)."""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpuload  # noqa: E402


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.now, self.cpu = [0.0], {1: 0.0, 2: 0.0, 3: 0.0}
        self.pids = {"road tile": [1, 2], "trees": [3]}
        self.load = cpuload.Load({n: (lambda n=n: self.pids[n]) for n in self.pids}, read=self.cpu.get,
                                 clock=lambda: self.now[0])

    def test_cores_since_the_last_sample(self):
        self.assertIsNone(self.load.sample())
        self.now[0] = 10.0
        self.cpu.update({1: 5.0, 2: 10.0, 3: 1.0})
        got = self.load.sample()
        self.assertAlmostEqual(got["road tile"], 1.5)
        self.assertAlmostEqual(got["trees"], 0.1)

    def test_a_process_that_came_or_went(self):
        self.load.sample()
        self.now[0] = 10.0
        self.cpu.update({1: 5.0, 4: 7.0})
        del self.cpu[2]                     # (gone: unreadable)
        self.pids["road tile"] = [1, 2, 4]  # (4 is new, started meanwhile: all its CPU so far)
        got = self.load.sample()
        self.assertAlmostEqual(got["road tile"], 1.2)
        self.now[0] = 20.0
        self.cpu[4] = 17.0
        self.assertAlmostEqual(self.load.sample()["road tile"], 1.0)

    def test_tick_logs_while_fast_every_so_often(self):
        self.assertIsNone(self.load.tick(10.0))                 # slow: nothing
        self.assertIsNone(self.load.tick(90.0))                 # the first fast pose starts the count
        self.now[0] = 5.0
        self.assertIsNone(self.load.tick(90.0))                 # not yet
        self.now[0] = cpuload.EVERY_S
        self.cpu.update({1: cpuload.EVERY_S, 3: cpuload.EVERY_S / 2})
        with self.assertLogs("earthd.cpuload", "INFO") as logs:
            got = self.load.tick(90.0, cores=3)
        self.assertAlmostEqual(got["road tile"], 1.0)
        self.assertIn("1.5 in use of the server's 3 (road tile 1.0, trees 0.5)", logs.output[0])
        self.now[0] += 1.0
        self.assertIsNone(self.load.tick(5.0))                  # a slow stretch: the count starts again after it
        self.now[0] += cpuload.EVERY_S
        self.assertIsNone(self.load.tick(90.0))

    def test_of_server_counts_each_pool_once_and_elastic_pools_whole(self):
        road = types.SimpleNamespace(name="road tile", _pids=lambda: [11, 12])
        far = types.SimpleNamespace(local=types.SimpleNamespace(      # (peerwork.SharedFarPool round an ElasticPool)
            name="far tile", base=types.SimpleNamespace(_pids=lambda: [21]),
            extra=types.SimpleNamespace(_pids=lambda: [22])))
        block_a = types.SimpleNamespace(name="block", _pids=lambda: [31])
        block_b = types.SimpleNamespace(name="block", _pids=lambda: [32])
        load = cpuload.of_server([road, None, far, block_a, block_b, block_a])
        self.assertEqual(sorted(load.groups), ["block", "far tile", "road tile", "server"])
        self.assertEqual(load.groups["block"](), [31, 32])
        self.assertEqual(load.groups["far tile"](), [21, 22])
        self.assertEqual(len(load.groups["server"]()), 1)


if __name__ == "__main__":
    unittest.main()
