"""roadsigns: bend warnings from the road's own geometry (MUTCD Section 2C rules), on the right side, facing the
right way, the same from any tile."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roads  # noqa: E402
import roadsigns  # noqa: E402

BIG = (-5000.0, -5000.0, 5000.0, 5000.0)


def arc_road(radius, turn_deg, left=True, lead=300.0, step=2.0):
    """A straight lead-in along +x, a circular bend of `radius` turning `turn_deg` (left or right), a lead-out."""
    pts = [(x, 0.0) for x in np.arange(-lead, 0.0, step)]
    sgn = 1.0 if left else -1.0
    cy = sgn * radius
    n = max(4, int(math.radians(turn_deg) * radius / step))
    for a in np.linspace(0.0, math.radians(turn_deg), n):
        pts.append((radius * math.sin(a), cy - sgn * radius * math.cos(a)))
    h = sgn * math.radians(turn_deg)
    ex, ey = pts[-1]
    for t in np.arange(step, lead, step):
        pts.append((ex + t * math.cos(h), ey + t * math.sin(h)))
    return np.array([(x, y, 0.0) for x, y in pts])


def spec(cls="secondary", oneway=False, d=0, width=7.0):
    return roads.Spec(width, "paved", False, cls, oneway, dir=d)


def heading_of(row):
    """Unit vector of a row's local -y (the face) from its yaw: local +x at yaw, +y 90 degrees left of it."""
    a = math.radians(row[4])
    x = np.array([math.cos(a), math.sin(a)])
    y = np.array([-x[1], x[0]])
    return -y


class AdvisoryTest(unittest.TestCase):
    def test_advisory_speeds(self):
        self.assertEqual(roadsigns.advisory_mph(30.0), 15)        # a hairpin
        self.assertEqual(roadsigns.advisory_mph(100.0), 30)
        self.assertGreaterEqual(roadsigns.advisory_mph(400.0), 55)
        self.assertEqual(roadsigns.advisory_mph(1.0), 10)          # never under 10

    def test_chevron_spacing(self):
        self.assertAlmostEqual(roadsigns.chevron_spacing_m(15), 40 * 0.3048)
        self.assertAlmostEqual(roadsigns.chevron_spacing_m(30), 80 * 0.3048)
        self.assertAlmostEqual(roadsigns.chevron_spacing_m(65), 200 * 0.3048)


class BendTest(unittest.TestCase):
    def test_finds_the_bend_and_its_radius(self):
        found = roadsigns.bends(arc_road(60.0, 90.0))
        self.assertEqual(len(found), 1, found)
        s0, s1, r, sgn, turn = found[0]
        self.assertEqual(sgn, 1.0)
        self.assertLess(abs(r - 60.0) / 60.0, 0.15, r)
        self.assertLess(abs(turn - 90.0), 10.0, turn)

    def test_sparse_hairpin_is_one_bend(self):
        """A hairpin drawn with a handful of map points (as OpenStreetMap often has them) is one bend at its
        real radius, not one per vertex."""
        for nv in (4, 6, 10):
            a = np.linspace(0.0, math.pi, nv)
            arc = [(40 * math.sin(t), 40 - 40 * math.cos(t), 0.0) for t in a]
            line = np.array([(-200.0, 0.0, 0.0)] + arc + [(-200.0, 80.0, 0.0)])
            found = roadsigns.bends(line)
            self.assertEqual(len(found), 1, (nv, found))
            self.assertEqual(roadsigns.advisory_mph(found[0][2]), roadsigns.advisory_mph(40.0), (nv, found))

    def test_right_bend_sign(self):
        self.assertEqual(roadsigns.bends(arc_road(60.0, 90.0, left=False))[0][3], -1.0)

    def test_straight_and_kinks_ignored(self):
        straight = np.array([(x, 0.0, 0.0) for x in range(0, 800, 10)])
        self.assertEqual(roadsigns.bends(straight), [])
        kink = np.array([(0, 0, 0), (400, 0, 0), (800, 30, 0)], float)     # 4 degrees
        self.assertEqual(roadsigns.bends(kink), [])

    def test_gentle_highway_curve_ignored(self):
        self.assertEqual(roadsigns.bends(arc_road(900.0, 30.0)), [])


class PlacementTest(unittest.TestCase):
    def signs(self, line, lim=55, sp=None, left_hand=False, mph=True, box=BIG):
        return roadsigns.curve_signs([("w1", sp or spec(), line, 0.0)], {"w1": lim}, box, left_hand, mph)

    def test_sharp_turn_gets_turn_sign_plaque_and_chevrons(self):
        rows = self.signs(arc_road(40.0, 90.0))
        groups = [r[0] for r in rows]
        self.assertIn("warn_turn_l", groups)        # driving +x it turns left
        self.assertIn("warn_turn_r", groups)        # coming back it turns right
        adv = roadsigns.advisory_mph(40.0)
        self.assertIn(f"advisory{adv}", groups)
        self.assertGreaterEqual(sum(g.startswith("chevron") for g in groups), 6)    # 3+ per direction

    def test_gentle_curve_gets_curve_sign_no_chevrons(self):
        r = 220.0                                   # advisory 45 under a 55 limit: a sign, no chevrons
        self.assertEqual(roadsigns.advisory_mph(r), 45)
        groups = [x[0] for x in self.signs(arc_road(r, 50.0))]
        self.assertIn("warn_curve_l", groups)
        self.assertFalse([g for g in groups if g.startswith("chevron")], groups)

    def test_no_sign_when_limit_already_low(self):
        self.assertEqual(self.signs(arc_road(100.0, 90.0), lim=35), [])

    def test_warning_before_the_bend_on_the_right_facing_traffic(self):
        line = arc_road(40.0, 90.0)
        s0 = roadsigns.bends(line)[0][0]
        rows = [r for r in self.signs(line) if r[0] == "warn_turn_l"]
        self.assertEqual(len(rows), 1)
        w = rows[0]
        # driving +x on the lead-in (y = 0): the sign is before the bend (x < 0) on the right (y < 0)
        self.assertLess(w[1], -roadsigns.ADVANCE_MIN_M + 1)
        self.assertLess(w[2], 0.0)
        self.assertAlmostEqual(abs(w[2]), 3.5 + roadsigns.SIGN_OFF_M, places=1)
        # the face looks back at the traffic coming from -x
        self.assertGreater(float(heading_of(w) @ np.array([-1.0, 0.0])), 0.99)
        # braking distance from 55 to the advisory at ADVANCE_DECEL
        v1, v2 = 55 * roadsigns.MPH_MS, roadsigns.advisory_mph(40.0) * roadsigns.MPH_MS
        want = (v1 * v1 - v2 * v2) / (2 * roadsigns.ADVANCE_DECEL)
        self.assertAlmostEqual(s0 - (w[1] + 300.0), want, delta=roadsigns.STEP_M + 1)

    def test_plaque_on_the_same_post_under_the_sign(self):
        rows = self.signs(arc_road(40.0, 90.0))
        sign = next(r for r in rows if r[0] == "warn_turn_l")
        plaque = next(r for r in rows if r[0].startswith("advisory") and r[1:3] == sign[1:3])
        self.assertLess(plaque[6], sign[6])

    def test_left_hand_traffic_mirrors(self):
        rows = [r for r in self.signs(arc_road(40.0, 90.0), left_hand=True) if r[0] == "warn_turn_l"]
        self.assertGreater(rows[0][2], 0.0)          # keeping left: the sign on the left (y > 0)
        self.assertGreater(float(heading_of(rows[0]) @ np.array([-1.0, 0.0])), 0.99)

    def test_chevrons_on_the_outside(self):
        line = arc_road(40.0, 90.0)                 # centre of the left bend at (0, 40)
        rows = self.signs(line)
        for r in rows:
            if not r[0].startswith("chevron"):
                continue
            dist = math.hypot(r[1] - 0.0, r[2] - 40.0)
            self.assertGreater(dist, 40.0, r)       # outside the circle, for both directions
            self.assertAlmostEqual(dist, 40.0 + 3.5 + roadsigns.SIGN_OFF_M, delta=1.0)

    def test_chevrons_face_their_traffic(self):
        line = arc_road(40.0, 90.0)
        rows = self.signs(line)
        # chevrons for the +x direction turn left ("_l"): at the bend's start (heading +x) they face -x
        ch = [r for r in rows if r[0] == "chevron_l"]
        first = min(ch, key=lambda r: math.atan2(r[1], 40.0 - r[2]))
        self.assertGreater(float(heading_of(first) @ np.array([-1.0, 0.0])), 0.9, first)

    def test_one_way_signed_one_direction(self):
        rows = self.signs(arc_road(40.0, 90.0), sp=spec(oneway=True, d=1))
        groups = {r[0] for r in rows}
        self.assertIn("warn_turn_l", groups)
        self.assertNotIn("warn_turn_r", groups)
        rows = self.signs(arc_road(40.0, 90.0), sp=spec(oneway=True, d=-1))
        self.assertNotIn("warn_turn_l", {r[0] for r in rows})

    def test_kmh_plaque(self):
        groups = {r[0] for r in self.signs(arc_road(40.0, 90.0), mph=False)}
        self.assertTrue(any(g.startswith("advisoryk") for g in groups), groups)

    def test_residential_and_service_not_signed(self):
        for cls in ("residential", "service", "motorway"):
            self.assertEqual(self.signs(arc_road(40.0, 90.0), sp=spec(cls)), [], cls)

    def test_same_signs_from_neighbouring_tiles(self):
        line = arc_road(40.0, 90.0)
        whole = self.signs(line)
        tiles = []
        for x0 in range(-512, 512, 128):
            for y0 in range(-512, 512, 128):
                tiles += self.signs(line, box=(x0, y0, x0 + 128, y0 + 128))
        key = lambda r: (r[0], r[1], r[2])
        self.assertEqual(sorted(map(key, whole)), sorted(map(key, tiles)))

    def test_reversed_line_gives_the_same_signs(self):
        """The same signs, within one sample step, if the map line runs the other way."""
        line = arc_road(40.0, 90.0)
        a = sorted(self.signs(line), key=lambda r: (r[0], r[1], r[2]))
        b = sorted(self.signs(line[::-1].copy()), key=lambda r: (r[0], r[1], r[2]))
        self.assertEqual([r[0] for r in a], [r[0] for r in b])
        for ra, rb in zip(a, b):
            self.assertLess(math.hypot(ra[1] - rb[1], ra[2] - rb[2]), roadsigns.STEP_M + 1.0, (ra, rb))

    def test_clear_check_drops_signs(self):
        rows = roadsigns.curve_signs([("w1", spec(), arc_road(40.0, 90.0), 0.0)], {"w1": 55}, BIG,
                                     clear=lambda x, y, z, fid: False)
        self.assertEqual(rows, [])


class FurnitureHookTest(unittest.TestCase):
    """furniture.place draws the bend signs only with the switch on (until the mod ships their models)."""

    def test_off_by_default_and_on_with_the_switch(self):
        import furniture
        r = ("w1", spec(), arc_road(40.0, 90.0), 0.0)
        old = furniture.SIGNS2
        try:
            furniture.SIGNS2 = False
            off = furniture.place([r], [], BIG, speeds={"w1": 55})
            self.assertFalse([x for x in off if x[0].startswith(("warn_", "chevron", "advisory"))])
            furniture.SIGNS2 = True
            on = furniture.place([r], [], BIG, speeds={"w1": 55})
            self.assertTrue([x for x in on if x[0] == "warn_turn_l"])
        finally:
            furniture.SIGNS2 = old

    def test_kept_off_another_carriageway(self):
        """A side road running right where a warning would stand: that warning is left out."""
        import furniture
        line = arc_road(40.0, 90.0)
        alone = roadsigns.curve_signs([("w1", spec(), line, 0.0)], {"w1": 55}, BIG)
        w = next(x for x in alone if x[0] == "warn_turn_l")
        side = ("w2", spec("residential"), np.array([(w[1] - 50, w[2], 0.0), (w[1] + 50, w[2], 0.0)]), 0.0)
        old = furniture.SIGNS2
        try:
            furniture.SIGNS2 = True
            rows = furniture.place([("w1", spec(), line, 0.0), side], [], BIG, speeds={"w1": 55})
        finally:
            furniture.SIGNS2 = old
        self.assertFalse([x for x in rows if x[0] == "warn_turn_l" and abs(x[1] - w[1]) < 1 and abs(x[2] - w[2]) < 1])


if __name__ == "__main__":
    unittest.main()
