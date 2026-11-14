"""grading._Planner._enu_ahead: the deck cells' bridge tables (_cell_enu, ground False) made side by side, not one
after another (each can wait seconds for its lidar's answer: 28 of a cold first block's 42 s of deck patches at Grand
Island, 2026-11-11); the same decks as one by one, the helpers' revisit faults on the caller's thread, a cell that
fails left to the caller."""
import os
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import mapcells  # noqa: E402
import revisit  # noqa: E402

KEYS = [mapcells.CellKey(14, 4700 + i, 6200) for i in range(4)]


def table(k):
    x = float(k.x - 4700) * 100.0
    return {"bridges": [np.array([[x, 0.0], [x + 50.0, 0.0]])], "bridge_half": [5.0], "rail_bridges": [],
            "span_bridges": []}


class Planner:
    """A _Planner whose cells' tables come from table(), each taking `wait` s; `fault_at` counts a revisit fault, and
    `fail_at` raises the first time."""

    def __init__(self, wait=0.2, fault_at=None, fail_at=None):
        g = object.__new__(grading._Planner)
        g.lock, g.failed = threading.RLock(), {}
        self.calls, self.threads, self.wait, self.fault_at, self.fail_at = [], set(), wait, fault_at, fail_at

        def calc(k, ground):
            self.calls.append(str(k))
            self.threads.add(threading.current_thread().name)
            time.sleep(self.wait)
            if k == self.fault_at:
                revisit.fault()
            if k == self.fail_at and self.calls.count(str(k)) == 1:
                raise OSError("no road data")
            return table(k)
        g._cell_enu_calc = calc
        self.g = g


class EnuAheadTest(unittest.TestCase):
    def setUp(self):
        self.was = grading.ENU_AHEAD_ON
        grading.ENU_AHEAD_ON = True         # (off by default: BNGE_ENU_AHEAD=on)

    def tearDown(self):
        grading.ENU_AHEAD_ON = self.was

    def test_same_decks_side_by_side(self):
        grading.ENU_AHEAD_ON = False
        one_by_one = Planner(wait=0.0).g._decks_in(KEYS)
        grading.ENU_AHEAD_ON = True
        p = Planner(wait=0.3)
        t0 = time.monotonic()
        got = p.g._decks_in(KEYS)
        self.assertLess(time.monotonic() - t0, 0.3 * len(KEYS) * 0.75, "the cells' waits overlap")
        self.assertEqual(sorted(got[0]), sorted(one_by_one[0]))
        self.assertEqual(got[1], one_by_one[1])
        self.assertEqual(sorted(p.calls), sorted(str(k) for k in KEYS), "each cell made once")
        self.assertTrue(any(t.startswith("enu-ahead") for t in p.threads))

    def test_a_helpers_fault_is_the_callers(self):
        p = Planner(wait=0.0, fault_at=KEYS[2])
        f0 = revisit.thread_faults()
        p.g._decks_in(KEYS)
        # (the helper's fault reaches the caller; since the provenance fixes it can arrive by two paths, the helper's
        # replay and the kept geometry's own mark, so it is counted at least once: completeness asks n > 0 only)
        self.assertGreaterEqual(revisit.thread_faults() - f0, 1)

    def test_a_failed_cell_is_the_callers_to_handle(self):
        p = Planner(wait=0.0, fail_at=KEYS[1])
        decks, short = p.g._decks_in(KEYS)
        self.assertFalse(short, "made again by the caller, which got it")
        self.assertEqual(p.calls.count(str(KEYS[1])), 2)
        self.assertEqual(len(decks), len(KEYS))

    def test_tables_at_hand_are_not_made_again(self):
        p = Planner(wait=0.0)
        p.g._decks_in(KEYS)
        p.calls.clear()
        p.g._decks_in(KEYS)
        self.assertEqual(p.calls, [])


if __name__ == "__main__":
    unittest.main()
