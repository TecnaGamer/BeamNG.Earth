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

    def test_more_cores_when_behind_all_along(self):
        from unittest import mock
        c = conn()
        c.s.boost_cpus = {3, 4, 5, 6, 7}
        pins = []
        with mock.patch.object(earthd.cpupin, "pin_tree", lambda cpus: pins.append(set(cpus)) or 1):
            earthd.Connection.catch_up(c, 40.0)
            earthd.Connection.catch_up(c, 40.0)
            self.assertEqual(pins, [], "not yet")
            c.catchup_since -= earthd.CATCHUP_MORE_S + 0.1
            earthd.Connection.catch_up(c, 40.0)
            self.assertEqual(pins, [c.s.boost_cpus])
            earthd.Connection.catch_up(c, 40.0)
            self.assertEqual(len(pins), 1, "once")

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


class HoldBoostTest(unittest.TestCase):
    """Connection.hold_boost / Server.split_boost: every core while the edge guard holds the car, the cores as before
    after (2026-11-02)."""

    def server(self):
        s = types.SimpleNamespace(boost_full=False, boost_on=False, server_cpus={6, 7, 14, 15},
                                  game_cpus={0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13}, boost_cpus={3, 4, 5, 6, 7, 11, 12, 13, 14, 15})
        s.split_boost = lambda on, why: earthd.Server.split_boost(s, on, why)
        return s

    def test_split_while_held_then_back(self):
        from unittest import mock
        s = self.server()
        c = types.SimpleNamespace(s=s)
        pins = []
        with mock.patch.object(earthd.cpupin, "pin_tree", lambda cpus: pins.append(("server", set(cpus))) or 1), \
                mock.patch.object(earthd.cpupin, "pin_game", lambda cpus: pins.append(("game", set(cpus) if cpus else None)) or 1):
            earthd.Connection.hold_boost(c, True)
            self.assertTrue(c.hold_split and s.split_on)
            self.assertEqual(pins[0], ("server", s.server_cpus | s.game_cpus))
            self.assertEqual(pins[1][0], "game")
            self.assertLess(len(pins[1][1]), len(s.game_cpus))
            earthd.Connection.hold_boost(c, True)       # (still held: nothing more)
            self.assertEqual(len(pins), 2)
            earthd.Connection.hold_boost(c, False)      # (just ended: kept HOLD_BOOST_CALM_S)
            self.assertTrue(c.hold_split)
            c.hold_seen -= earthd.HOLD_BOOST_CALM_S + 0.1
            earthd.Connection.hold_boost(c, False)
            self.assertFalse(c.hold_split or s.split_on)
            self.assertEqual(pins[2:], [("server", s.server_cpus), ("game", s.game_cpus)])

    def test_not_over_full_priority(self):
        s = self.server()
        s.boost_full = True
        self.assertFalse(earthd.Server.split_boost(s, True, "edge hold"))
