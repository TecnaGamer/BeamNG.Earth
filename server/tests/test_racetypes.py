"""Racetrack types (racetypes.py): drag strips and speedway ovals from the raceways' geometry, their widths and paved
areas; a drag strip's one straight grade (grading.drag_grade)."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import racetypes as rt  # noqa: E402

# Bandimere Speedway's OSM way 137280810 (sport=motor, oneway=yes, no width): one straight 1100 m line
BANDIMERE = [[-105.187252, 39.659558], [-105.1885974, 39.6693815]]


def arc(lon0, lat0, r_m, a0, a1, n=60):
    t = np.linspace(a0, a1, n)
    kx = 111320.0 * math.cos(math.radians(lat0))
    return np.c_[lon0 + r_m * np.cos(t) / kx, lat0 + r_m * np.sin(t) / 110540.0]


class RaceTypes(unittest.TestCase):
    def test_dead_end_straight_is_a_drag_strip(self):
        w = {"id": 1, "coords": BANDIMERE, "name": "Bandimere Speedway", "oneway": "yes"}
        rt.classify([w])
        self.assertEqual(w["race_kind"], "drag")
        self.assertEqual(w["race_dir"], 1)
        self.assertEqual(rt.road_width("drag"), rt.DRAG_STRIP_M)

    def test_circuit_straight_joined_to_its_turns_is_not(self):
        # (Talladega's Superstretch: a straight 954 m way, its ends on the turns)
        ws = self.oval_loop()
        rt.classify(ws)
        self.assertEqual([w["race_kind"] for w in ws], ["oval"] * 4)

    @staticmethod
    def oval_loop():
        # (straights 930 m, turns 280 m radius: the loop's four pieces end to end)
        r = 280.0
        dy = 2 * r / 110540.0
        t1 = arc(-86.06, 33.565 + dy / 2, r, -math.pi / 2, math.pi / 2)
        t2 = arc(-86.07, 33.565 + dy / 2, r, math.pi / 2, 3 * math.pi / 2)
        return [{"id": 1, "coords": [[-86.07, 33.565], [-86.06, 33.565]]}, {"id": 2, "coords": t1},
                {"id": 3, "coords": [t1[-1].tolist(), t2[0].tolist()]}, {"id": 4, "coords": t2}]

    def test_short_paperclip_is_an_oval(self):
        # (Martinsville: 60 m turns, 250 m straights, 847 m round)
        r = 60.0
        dy = 2 * r / 110540.0
        kx = 111320.0 * math.cos(math.radians(36.63))
        x1 = -79.85 + 250.0 / kx
        t1 = arc(x1, 36.63 + dy / 2, r, -math.pi / 2, math.pi / 2)
        t2 = arc(-79.85, 36.63 + dy / 2, r, math.pi / 2, 3 * math.pi / 2)
        ws = [{"id": 1, "coords": [[-79.85, 36.63], [x1, 36.63]]}, {"id": 2, "coords": t1},
              {"id": 3, "coords": [t1[-1].tolist(), t2[0].tolist()]}, {"id": 4, "coords": t2}]
        rt.classify(ws)
        self.assertEqual([w["race_kind"] for w in ws], ["short_oval"] * 4)
        self.assertIsNone(rt.road_width("short_oval"))      # (the class's width, its own smoothing: a road course's)

    def test_road_course_sweeper_is_not_an_oval(self):
        # (Spa's Kemmel, 2026-11-07: a 1 km piece bending no tighter than 180 m, its ends on the circuit's corners)
        ws = self.oval_loop()
        ws[1]["coords"] = arc(-86.06, 33.565 + 280.0 / 110540.0, 280.0 , -math.pi / 2, math.pi / 2)
        hair = arc(-86.06 - 40.0 / (111320.0 * math.cos(math.radians(33.57))), float(ws[1]["coords"][-1][1]), 40.0,
                   0.0, math.pi)
        ws[2]["coords"] = [ws[1]["coords"][-1].tolist()] + hair[::-1].tolist()[1:]
        rt.classify(ws)
        self.assertIsNone(ws[0]["race_kind"])
        self.assertIsNone(ws[1]["race_kind"])
        self.assertIsNone(ws[2]["race_kind"])

    def test_neighbour_found_by_lookup(self):
        s = [[-86.07, 33.565], [-86.06, 33.565]]
        a = {"id": 1, "coords": s}
        rt.classify([a], lookup=lambda box: [{"id": 2, "coords": [[-86.06, 33.565], [-86.055, 33.57]]}])
        self.assertNotEqual(a["race_kind"], "drag")

    def test_road_course_corner_and_pit_lane(self):
        hairpin = arc(-86.0, 33.5, 40.0, 0.0, math.pi)
        longer = np.r_[hairpin, [[-85.99, 33.51]]]
        self.assertIsNone(rt.kind({"coords": longer}, True))
        self.assertIsNone(rt.kind({"coords": BANDIMERE, "name": "Pit Lane"}, False))

    def test_drag_pads_square_and_apron_behind_start(self):
        rect, apron = rt.pads(BANDIMERE, "drag", 20.0, 1)
        # the apron lies south of the start (the way runs north)
        self.assertLess(float(np.max(apron[0])), BANDIMERE[0][1] + 1e-4)
        self.assertLess(float(np.min(apron[0])), BANDIMERE[0][1] - 30.0 / 110540.0)
        # the rectangle reaches half the width past the far end (the round cap squared)
        self.assertGreater(float(np.max(rect[0])), BANDIMERE[1][1] + 9.0 / 110540.0)

    def test_oval_apron_inside(self):
        turn = arc(-86.06, 33.57, 300.0, 0.0, math.pi)     # anticlockwise: the inside on the left
        (la, lo), = rt.pads(turn, "oval", 18.0, 1)
        kx = 111320.0 * math.cos(math.radians(33.57))
        r = np.hypot((lo + 86.06) * kx, (la - 33.57) * 110540.0)
        self.assertLess(float(r.max()), 300.0 - 8.0)
        self.assertGreater(float(r.min()), 300.0 - 9.0 - rt.OVAL_APRON_M - 1.0)


class DragGrade(unittest.TestCase):
    def test_straight_line_through_bumps(self):
        import grading
        s = np.arange(0.0, 1000.0, 2.0)
        xy = np.c_[s, np.zeros_like(s)]
        z = 100 + 0.01 * s + 0.3 * np.sin(s / 15.0)
        zs = grading.drag_grade(xy, z)
        d2 = np.abs(np.diff(zs, 2))
        self.assertLess(float(d2.max()), 1e-9)
        self.assertAlmostEqual(float((zs[-1] - zs[0]) / (s[-1] - s[0])), 0.01, places=3)


if __name__ == "__main__":
    unittest.main()
