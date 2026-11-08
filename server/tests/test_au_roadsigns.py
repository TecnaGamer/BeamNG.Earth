"""auroadsigns (fork): Australia's chevron alignment markers, Wrong Way Go Back and gravel road signs: where they stand."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auroadsigns  # noqa: E402
import guardrails  # noqa: E402
import roads  # noqa: E402


class Frame:
    enu_to_geodetic = staticmethod(lambda x, y, z: (y, x, z))
    geodetic_to_enu = staticmethod(lambda la, lo, h: (lo, la, h))


BOX = (-5000.0, -5000.0, 5000.0, 5000.0)
SPEC = roads.Spec(9.0, "paved", False, "secondary", False)._replace(kmh=100.0, sides=())


def line(a, b, step=10.0):
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    n = max(int(np.hypot(*(b - a)) / step), 1)
    t = np.linspace(0.0, 1.0, n + 1)[:, None]
    xy = a + t * (b - a)
    return np.c_[xy, np.zeros(len(xy))]


def bend(radius, deg=90.0, lead=300.0, step=5.0):
    """Along +X to the origin, then a left turn of deg degrees about (0, radius), then straight on."""
    a = np.radians(deg)
    n = max(int(radius * a / step), 2)
    th = np.linspace(0.0, a, n + 1)
    arc = np.c_[radius * np.sin(th), radius * (1.0 - np.cos(th))]
    end, d = arc[-1], np.array([np.cos(a), np.sin(a)])
    pts = np.vstack([line((-lead, 0.0), (0.0, 0.0))[:-1, :2], arc, (end + d * np.arange(step, lead + step, step)[:, None])])
    return np.c_[pts, np.zeros(len(pts))]


def rows(near, country="AU", box=BOX):
    got = auroadsigns.tile_rows(Frame, near, box, lambda la, lo: la * 0 + 5.0, country)
    return np.array(got).reshape(-1, 17)


def group(r):
    return guardrails.GROUPS[int(r[0])]


def groups(r, name):
    return r[[group(x) == name for x in r]]


class ChevronTest(unittest.TestCase):
    R = 150.0

    def curve(self, spec=SPEC, radius=R, **kw):
        return [("c", spec, bend(radius, **kw), 12.0)]

    def test_a_tight_curve_has_a_run_outside_it_for_each_way_of_travel(self):
        r = rows(self.curve())
        left, right = groups(r, "au_chevron_l"), groups(r, "au_chevron_r")
        self.assertEqual(len(left) + len(right), len(r), "only chevrons here")
        for run in (left, right):
            self.assertTrue(auroadsigns.CAM_COUNT[0] <= len(run) <= auroadsigns.CAM_COUNT[1], len(run))
            # (outside the bend: further from its centre (0, R) than the road's edge)
            self.assertTrue((np.hypot(run[:, 1], run[:, 2] - self.R) > self.R + SPEC.width / 2).all())
            self.assertTrue((np.hypot(run[:, 1], run[:, 2] - self.R) < self.R + SPEC.width / 2 + 5.0).all())
        # (the drivers going along the geometry turn left, so they see the left pointing ones, travelling counter-clockwise)
        cross = lambda run: (run[:, 1] * run[:, 12] - (run[:, 2] - self.R) * run[:, 11])
        self.assertTrue((cross(left) > 0).all(), "facing the traffic going round the bend")
        self.assertTrue((cross(right) < 0).all(), "(and the other way, a right turn)")
        self.assertAlmostEqual(float(np.hypot(left[:, 11], left[:, 12]).min()), 1.0, places=2)

    def test_the_spacing_is_20_to_40_m_by_the_radius(self):
        for radius, lo, hi in ((90.0, 18.0, 22.0), (250.0, 36.0, 44.0)):
            run = groups(rows(self.curve(radius=radius)), "au_chevron_l")
            self.assertGreaterEqual(len(run), 3)
            gap = np.hypot(*np.diff(run[:, 1:3], axis=0).T)
            self.assertTrue(((gap > lo) & (gap < hi)).all(), (radius, gap))

    def test_a_straight_or_a_gentle_or_a_town_road_has_none(self):
        self.assertEqual(len(rows([("s", SPEC, line((-500.0, 0.0), (500.0, 0.0)), 12.0)])), 0)
        self.assertEqual(len(rows(self.curve(radius=600.0))), 0, "a gentle bend")
        self.assertEqual(len(rows(self.curve(deg=12.0))), 0, "a few degrees")
        self.assertEqual(len(rows(self.curve(SPEC._replace(urban=True)))), 0)
        self.assertEqual(len(rows(self.curve(SPEC._replace(sides=(1,))))), 0, "a footpath")
        self.assertEqual(len(rows(self.curve(SPEC._replace(link=True)))), 0, "a ramp")
        self.assertEqual(len(rows(self.curve(SPEC._replace(cls="motorway")))), 0)
        self.assertEqual(len(rows(self.curve(SPEC._replace(kind="gravel")))), 0)
        self.assertEqual(len(rows(self.curve(SPEC._replace(cls="residential", kmh=50.0)))), 0, "a slow minor road")

    def test_a_fast_road_of_another_class_has_them(self):
        self.assertGreater(len(rows(self.curve(SPEC._replace(cls="residential", kmh=80.0)))), 0)

    def test_other_countries_have_none(self):
        self.assertEqual(len(rows(self.curve(), country="SE")), 0)
        self.assertEqual(len(rows(self.curve(), country=None)), 0)

    def test_a_roundabout_has_none(self):
        ring = np.radians(np.arange(0.0, 361.0, 10.0))
        pts = np.c_[40.0 * np.cos(ring), 40.0 * np.sin(ring), np.zeros(len(ring))]
        self.assertEqual(len(rows([("r", SPEC, pts, 12.0)])), 0, "a closed ring")
        self.assertEqual(len(rows([("r", SPEC, pts[:25], 12.0)])), 0, "(or a long arc of one)")

    def test_a_one_way_road_has_the_run_for_its_way_only(self):
        r = rows(self.curve(SPEC._replace(oneway=True, dir=1)))
        self.assertEqual(len(groups(r, "au_chevron_r")), 0)
        self.assertGreaterEqual(len(groups(r, "au_chevron_l")), 3)
        r = rows(self.curve(SPEC._replace(oneway=True, dir=-1)))
        self.assertEqual(len(groups(r, "au_chevron_l")), 0)
        self.assertGreaterEqual(len(groups(r, "au_chevron_r")), 3)

    def test_a_road_near_a_sign_takes_it_away(self):
        base = rows(self.curve())
        side = ("j", SPEC, line((self.R * 0.7, self.R * 0.3 - 40.0), (self.R * 0.7 + 200.0, self.R * 0.3 - 40.0)), 12.0)
        near = self.curve() + [side]
        got = rows(near)
        self.assertLess(len(got), len(base))
        pos = np.c_[got[:, 1], got[:, 2]]
        self.assertTrue((auroadsigns.motesfri._distance(pos, side[2][:, :2]) > auroadsigns.CAM_JUNCTION_M).all())

    def test_the_tile_that_owns_the_point_keeps_it(self):
        whole = rows(self.curve())
        half = rows(self.curve(), box=(-5000.0, -5000.0, 5000.0, 60.0))
        rest = rows(self.curve(), box=(-5000.0, 60.0, 5000.0, 5000.0))
        self.assertEqual(len(half) + len(rest), len(whole))
        self.assertTrue(0 < len(half) < len(whole))


class WrongWayTest(unittest.TestCase):
    RAMP = SPEC._replace(cls="motorway", oneway=True, dir=1, link=True, width=5.0, kmh=60.0)

    def exit_ramp(self, ramp=RAMP, ordinary=SPEC, reverse=False):
        """A motorway along X; a ramp leaving it north up the Y axis to an ordinary road along X at y = 200."""
        pts = line((0.0, 0.0), (0.0, 200.0))
        if reverse:
            pts = pts[::-1]
            ramp = ramp._replace(dir=-ramp.dir)
        return [("m", SPEC._replace(cls="motorway"), line((-300.0, 0.0), (300.0, 0.0)), 12.0),
                ("r", ramp, pts, 6.0), ("o", ordinary, line((-300.0, 200.0), (300.0, 200.0)), 12.0)]

    def test_an_exit_ramp_end_has_one_sign_facing_the_road(self):
        r = rows(self.exit_ramp())
        self.assertEqual(len(r), 1)
        self.assertEqual(group(r[0]), "au_wrongway")
        self.assertAlmostEqual(r[0, 6], auroadsigns.HEIGHTS["au_wrongway"])
        # beside the ramp's last metres, on the driver's left (-X going north), clear of the ordinary road
        self.assertAlmostEqual(r[0, 1], -(2.5 + auroadsigns.WW_OUT_M), places=2)
        self.assertAlmostEqual(r[0, 2], 200.0 - (4.5 + auroadsigns.WW_BACK_M), places=1)
        # facing back down the ramp's end: the traffic entering it the wrong way travels south
        self.assertEqual(np.round(r[0, 11:13], 3).tolist(), [0.0, -1.0])

    def test_a_ramp_digitised_the_other_way_round_has_the_same_one(self):
        a, b = rows(self.exit_ramp()), rows(self.exit_ramp(reverse=True))
        self.assertEqual(len(b), 1)
        self.assertEqual(np.round(a[0, 1:3], 2).tolist(), np.round(b[0, 1:3], 2).tolist())

    def test_an_entry_ramp_or_one_ending_on_a_motorway_has_none(self):
        near = self.exit_ramp(ramp=self.RAMP._replace(dir=-1))
        self.assertEqual(len(rows(near)), 0, "the ramp's way of travel ends on the motorway")
        near = self.exit_ramp(ordinary=SPEC._replace(cls="motorway"))
        self.assertEqual(len(rows(near)), 0)

    def test_a_two_way_or_an_ordinary_link_or_another_country_has_none(self):
        self.assertEqual(len(rows(self.exit_ramp(ramp=self.RAMP._replace(oneway=False, dir=0)))), 0)
        self.assertEqual(len(rows(self.exit_ramp(ramp=self.RAMP._replace(cls="secondary")))), 0, "a slip lane")
        self.assertEqual(len(rows(self.exit_ramp(ramp=self.RAMP._replace(link=False)))), 0)
        self.assertEqual(len(rows(self.exit_ramp(), country="SE")), 0)

    def test_a_ramp_ending_in_the_air_has_none(self):
        near = self.exit_ramp()[:2]
        self.assertEqual(len(rows(near)), 0)


class GravelTest(unittest.TestCase):
    GRAVEL = SPEC._replace(kind="gravel", cls="unclassified", width=5.0)

    def change(self, sealed=SPEC, unpaved=GRAVEL, reverse=False):
        """A sealed road along +X to the origin, a gravel one carrying on from it."""
        u = line((0.0, 0.0), (300.0, 0.0))
        return [("s", sealed, line((-300.0, 0.0), (0.0, 0.0)), 12.0), ("u", unpaved, u[::-1] if reverse else u, 6.0)]

    def test_a_change_to_gravel_has_one_sign_facing_the_traffic_heading_onto_it(self):
        r = rows(self.change())
        self.assertEqual(len(r), 1, "one way only")
        self.assertEqual(group(r[0]), "au_gravel")
        self.assertAlmostEqual(r[0, 6], auroadsigns.HEIGHTS["au_gravel"])
        self.assertAlmostEqual(r[0, 1], -auroadsigns.GRAVEL_BACK_M, places=2)
        self.assertAlmostEqual(r[0, 2], 4.5 + auroadsigns.GRAVEL_OUT_M, places=2)     # (the driver's left going east)
        self.assertEqual(np.round(r[0, 11:13], 3).tolist(), [1.0, 0.0], "facing the traffic going east onto the gravel")

    def test_whichever_way_the_roads_are_digitised(self):
        sealed_back = self.change()
        sealed_back[0] = (sealed_back[0][0], SPEC, sealed_back[0][2][::-1], 12.0)
        for near in (self.change(reverse=True), sealed_back):
            r = rows(near)
            self.assertEqual(len(r), 1)
            self.assertEqual(np.round(r[0, 11:13], 3).tolist(), [1.0, 0.0])

    def test_one_way_roads_only_where_traffic_heads_onto_the_gravel(self):
        self.assertEqual(len(rows(self.change(sealed=SPEC._replace(oneway=True, dir=1)))), 1)
        self.assertEqual(len(rows(self.change(sealed=SPEC._replace(oneway=True, dir=-1)))), 0, "the sealed road leads away")
        self.assertEqual(len(rows(self.change(unpaved=self.GRAVEL._replace(oneway=True, dir=1)))), 1)
        self.assertEqual(len(rows(self.change(unpaved=self.GRAVEL._replace(oneway=True, dir=-1)))), 0, "its traffic comes the other way")

    def test_other_surfaces_and_places_have_none(self):
        self.assertEqual(len(rows(self.change(unpaved=SPEC))), 0, "sealed to sealed")
        self.assertEqual(len(rows(self.change(sealed=SPEC._replace(urban=True)))), 0)
        self.assertEqual(len(rows(self.change(unpaved=self.GRAVEL._replace(cls="track")))), 0)
        self.assertEqual(len(rows(self.change(), country="SE")), 0)
        dirt = self.change(unpaved=self.GRAVEL._replace(kind="dirt"))
        self.assertEqual(len(rows(dirt)), 1)

    def test_a_gravel_side_road_is_not_a_change_of_surface(self):
        # (the gravel road ends on the sealed one's side: both ways of the sealed road carry on)
        near = [("s1", SPEC, line((-300.0, 0.0), (0.0, 0.0)), 12.0), ("s2", SPEC, line((0.0, 0.0), (300.0, 0.0)), 12.0),
                ("u", self.GRAVEL, line((0.0, 0.0), (0.0, 300.0)), 6.0)]
        self.assertEqual(len(rows(near)), 0)

    def test_a_gravel_road_that_turns_back_has_none(self):
        near = [("s", SPEC, line((-300.0, 0.0), (0.0, 0.0)), 12.0), ("u", self.GRAVEL, line((0.0, 0.0), (-300.0, 20.0)), 6.0)]
        self.assertEqual(len(rows(near)), 0)


if __name__ == "__main__":
    unittest.main()
