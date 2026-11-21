"""arrival.py: how far ahead of a fast car its tiles are sent."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import arrival  # noqa: E402


class ArrivalTest(unittest.TestCase):
    def test_slow_car_notes_nothing(self):
        a = arrival.Arrival()
        a.note("trees", 0, 0, 10.0, 0, 500, 0, 400)
        self.assertEqual(a.lines(), [])

    def test_ahead_behind_under(self):
        t = [0.0]
        a = arrival.Arrival(clock=lambda: t[0])
        a.note("trees", 0, 0, 80.0, 0, 500, 0, 400)       # ahead, 5 s
        a.note("trees", 0, 0, 80.0, 0, 200, 0, 80)        # ahead, 1 s: short
        a.note("trees", 0, 0, 80.0, 0, -300, 0, 200)      # behind
        a.note("trees", 0, 0, 80.0, 0, 10, 0, 0.0)        # the car is in it
        (line,) = a.lines()
        self.assertIn("4 sent, 1 behind, 1 under the car, 2 ahead", line)
        self.assertIn("lead median 5.0 s (400 m), 1 under 2 s", line)
        self.assertEqual(a.total["trees"], [4, 1, 1, 2, 1])

    def test_flushes_after_every_s(self):
        t = [0.0]
        a = arrival.Arrival(clock=lambda: t[0])
        a.note("roads", 0, 0, 80.0, 0, 500, 0, 400)
        t[0] = arrival.EVERY_S + 1
        with self.assertLogs("earthd.arrival", "INFO") as cm:
            a.note("roads", 0, 0, 80.0, 0, 500, 0, 400)
        self.assertIn("arrival roads at 80 m/s: 2 sent", cm.output[0])
        self.assertEqual(a.rows, {})


if __name__ == "__main__":
    unittest.main()
