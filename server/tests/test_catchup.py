"""earthd.Connection.catch_up: the loading boost while the world is behind a driving player (2026-11-02: another
part of the server ended it and it never came back for the rest of a 150 mph drive)."""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402


def conn(behind=True):
    s = types.SimpleNamespace(boost_on=False, calls=[])

    def load_boost(on, why, half_only=False):
        s.calls.append((on, why))
        s.boost_on = on
    s.load_boost = load_boost
    c = types.SimpleNamespace(s=s, catchup=False, caught_up_at=None, pending_at=time.monotonic() - 5)
    c.behind = lambda: behind
    return c


class CatchUpTest(unittest.TestCase):
    def test_on_while_behind_and_again_after_another_ended_it(self):
        c = conn()
        earthd.Connection.catch_up(c, 40.0)
        self.assertTrue(c.catchup and c.s.boost_on)
        c.s.boost_on = False                # (ended elsewhere: a far field done, a teleport's own rule)
        earthd.Connection.catch_up(c, 40.0)
        self.assertTrue(c.catchup and c.s.boost_on, "on again while still behind")
        self.assertEqual([on for on, _ in c.s.calls], [True, True])

    def test_not_over_a_teleports_boost(self):
        c = conn()
        c.s.boost_on = True                 # (a teleport's own)
        earthd.Connection.catch_up(c, 40.0)
        self.assertFalse(c.catchup)
        self.assertEqual(c.s.calls, [])

    def test_off_once_caught_up(self):
        c = conn()
        earthd.Connection.catch_up(c, 40.0)
        c.behind = lambda: False
        earthd.Connection.catch_up(c, 40.0)
        c.caught_up_at -= earthd.CATCHUP_CALM_S + 0.1
        earthd.Connection.catch_up(c, 40.0)
        self.assertFalse(c.catchup or c.s.boost_on)

    def test_parked_never(self):
        c = conn()
        earthd.Connection.catch_up(c, 1.0)
        self.assertFalse(c.catchup)


if __name__ == "__main__":
    unittest.main()
