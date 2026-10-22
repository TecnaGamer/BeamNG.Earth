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
