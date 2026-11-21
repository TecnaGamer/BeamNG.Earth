"""aheadpath: the way ahead of a driving player (the map data cells built ahead of it)."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aheadpath  # noqa: E402


class PointsTest(unittest.TestCase):
    def test_standing_has_no_path(self):
        self.assertEqual(aheadpath.points(5.0, 5.0, 0.5, 0.0), [])

    def test_length_follows_speed(self):
        town = aheadpath.points(0.0, 0.0, 10.0, 0.0)
        road = aheadpath.points(0.0, 0.0, 30.0, 0.0)
        motorway = aheadpath.points(0.0, 0.0, 70.0, 0.0)
        self.assertAlmostEqual(town[-1][1], aheadpath.MIN_M, delta=1)       # 900 m of 90 s: the minimum
        self.assertAlmostEqual(road[-1][1], 30.0 * aheadpath.LEAD_S, delta=1)
        self.assertAlmostEqual(motorway[-1][1], aheadpath.MAX_M, delta=1)
        self.assertAlmostEqual(road[-1][0], aheadpath.LEAD_S, delta=0.1)   # the time the player gets there

    def test_points_in_order_and_spaced(self):
        p = aheadpath.points(100.0, -50.0, 0.0, 25.0)
        self.assertEqual(p[0], (0.0, 100.0, -50.0, 0.0))
        ts = [q[0] for q in p]
        self.assertEqual(ts, sorted(ts))
        for a, b in zip(p, p[1:]):
            self.assertLessEqual(math.hypot(b[1] - a[1], b[2] - a[2]), aheadpath.STEP_M + 1e-6)
        self.assertTrue(all(abs(q[1] - 100.0) < 1e-6 for q in p))         # straight north
        self.assertLessEqual(p[-1][3], aheadpath.SPREAD_MAX_M)
        self.assertGreater(p[-1][3], p[1][3])

    def test_turn_bends_and_fades(self):
        left = aheadpath.points(0.0, 0.0, 30.0, 0.0, turn=0.1)
        right = aheadpath.points(0.0, 0.0, 30.0, 0.0, turn=-0.1)
        self.assertGreater(left[-1][2], 500.0)
        self.assertAlmostEqual(left[-1][2], -right[-1][2], delta=1e-6)
        # faded: the heading turns by at most turn * TURN_FADE_S (0.8 rad), never round a circle
        end_h = math.atan2(left[-1][2] - left[-2][2], left[-1][1] - left[-2][1])
        self.assertLess(end_h, 0.1 * aheadpath.TURN_FADE_S + 0.01)
        self.assertGreater(left[-1][1], 1000.0)

    def test_turn_capped(self):
        a = aheadpath.points(0.0, 0.0, 30.0, 0.0, turn=5.0)
        b = aheadpath.points(0.0, 0.0, 30.0, 0.0, turn=aheadpath.TURN_MAX)
        self.assertEqual(a, b)


class TrackTest(unittest.TestCase):
    def test_turn_rate(self):
        t = aheadpath.Track()
        for i in range(20):         # 0.2 rad/s to the left, a pose every 0.25 s
            h = 0.2 * i * 0.25
            t.add(i * 0.25, 7.5 * i, 0.0, 30 * math.cos(h), 30 * math.sin(h))
        self.assertAlmostEqual(t.turn(), 0.2, places=3)

    def test_heading_wraps(self):
        t = aheadpath.Track()
        for i, h in enumerate((math.pi - 0.1, math.pi - 0.05, -math.pi + 0.0, -math.pi + 0.05, -math.pi + 0.1)):
            t.add(i * 0.5, i * 15.0, 0.0, 30 * math.cos(h), 30 * math.sin(h))
        self.assertAlmostEqual(t.turn(), 0.1, places=3)

    def test_needs_a_second_and_restarts_on_a_jump_or_a_stop(self):
        t = aheadpath.Track()
        t.add(0.0, 0.0, 0.0, 30.0, 0.0)
        t.add(0.5, 15.0, 0.0, 0.0, 30.0)
        self.assertEqual(t.turn(), 0.0)                 # half a second: not yet
        t.add(1.0, 2000.0, 0.0, 30.0, 0.0)              # a teleport
        t.add(2.0, 2030.0, 0.0, 30.0, 0.0)
        self.assertEqual(t.turn(), 0.0)
        t.add(3.0, 2030.0, 0.0, 0.0, 0.0)               # stopped
        self.assertEqual(len(t.poses), 0)


class AlongTest(unittest.TestCase):
    """along: the path on the road the player drives on, then straight on from its end."""

    def test_follows_the_road_then_goes_straight_on(self):
        # east 1 km, then north 1 km; the lookahead reaches 4.8 km
        p = aheadpath.along([(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0)], 80.0, lead_s=60.0, min_m=1500.0,
                            max_m=10000.0)
        self.assertEqual(p[0], (0.0, 0.0, 0.0, 0.0))
        self.assertAlmostEqual(p[-1][0], 60.0, delta=0.01)
        self.assertAlmostEqual(p[-1][1], 1000.0, delta=1e-6)      # north on from the road's end, not east
        self.assertAlmostEqual(p[-1][2], 3800.0, delta=1e-6)
        on_road = [q for q in p if q[0] * 80.0 <= 2000.0]
        self.assertTrue(all(q[3] == 0.0 for q in on_road), "the road itself is no guess")
        past = [q for q in p if q[0] * 80.0 > 2000.0]
        self.assertTrue(all(b[3] >= a[3] > 0.0 for a, b in zip(past, past[1:])), "the guess past it spreads")
        self.assertLessEqual(p[-1][3], aheadpath.SPREAD_MAX_M)
        ts = [q[0] for q in p]
        self.assertEqual(ts, sorted(ts))
        for a, b in zip(p, p[1:]):
            self.assertLessEqual(math.hypot(b[1] - a[1], b[2] - a[2]), aheadpath.STEP_M + 1e-6)

    def test_a_road_longer_than_the_lookahead_is_cut(self):
        p = aheadpath.along([(0.0, 0.0), (0.0, 9000.0)], 20.0, lead_s=60.0, min_m=1200.0, max_m=6000.0)
        self.assertAlmostEqual(p[-1][2], 1200.0, delta=1e-6)
        self.assertTrue(all(q[3] == 0.0 for q in p))

    def test_the_end_direction_is_the_roads_last_stretch(self):
        # a long bend ending north-east: straight on north-east, not along the last 5 m kink east
        line = [(0.0, 0.0), (0.0, 500.0), (600.0, 1100.0), (605.0, 1100.0)]
        p = aheadpath.along(line, 50.0, lead_s=60.0, min_m=1200.0, max_m=6000.0)
        dx, dy = p[-1][1] - 605.0, p[-1][2] - 1100.0
        self.assertGreater(dx, 0.0)
        self.assertGreater(dy, 0.5 * dx, "north-east on, as the bend ended")

    def test_no_line_or_standing(self):
        self.assertEqual(aheadpath.along(None, 50.0), [])
        self.assertEqual(aheadpath.along([(0.0, 0.0)], 50.0), [])
        self.assertEqual(aheadpath.along([(0.0, 0.0), (0.0, 100.0)], 0.5), [])
