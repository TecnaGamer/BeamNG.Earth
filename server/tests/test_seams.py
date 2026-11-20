"""Seams where two surfaces meet (plan 028, 2026-11-08): a carriageway road's abutment end is back at its deck's height
whatever the passes after the carriageway solve did to it, and the roads ending at the same abutment come down from it."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402


def spec(cls):
    return roads.Spec(*([None] * len(roads.Spec._fields)))._replace(
        width=9.6, kind="paved", urban=False, cls=cls, surf="asphalt", sides=0, walk=False, lanes=2, oneway=False,
        gap=0.0, median=0.0, link=False, dir=0, kmh=50)


class PinCwEndsTest(unittest.TestCase):
    DECK = 83.33

    def planner(self, profile_ok=True):
        g = object.__new__(grading._Planner)

        def cw(line, pts, fid=None):
            pts = np.asarray(pts, np.float64).reshape(-1, 2)
            return np.full(len(pts), self.DECK) if profile_ok else None
        g._cw_z_at = cw
        return g

    def plans(self):
        s = np.arange(0.0, 121.0, 4.0)
        trunk = grading.Plan(np.c_[s, np.zeros(len(s))], np.minimum(81.65 + 0.4 * s / 4, 83.34), 7.8, abut=(True, False))
        trunk.rid = 1
        street = grading.Plan(np.c_[np.zeros(14), np.linspace(60.0, 0.0, 14)] + [0.0, 0.0],
                              np.full(14, 81.65), 8.3, abut=(False, True))
        street.rid = 2
        return [("t", trunk), ("s", street)], [spec("trunk"), spec("residential")]

    def test_end_back_at_the_deck(self):
        plans, specs = self.plans()
        self.assertTrue(self.planner()._pin_cw_ends(plans, specs))
        z = np.asarray(plans[0][1].z)
        self.assertAlmostEqual(float(z[0]), self.DECK, places=6)
        self.assertLess(float(np.abs(np.diff(z)).max()), 0.1 * 4, "no step along the road")
        orig = self.plans()[0][0][1].z
        self.assertTrue(np.allclose(z[-10:], orig[-10:]), "the road far from the end is as it was")

    def test_street_at_the_abutment_comes_down_from_it(self):
        plans, specs = self.plans()
        self.planner()._pin_cw_ends(plans, specs)
        z = np.asarray(plans[1][1].z)
        self.assertAlmostEqual(float(z[-1]), self.DECK, places=6)       # (its last vertex is the abutment's)
        grade = np.abs(np.diff(z)).max() / (60.0 / 13)
        self.assertLessEqual(grade, 0.25)
        self.assertAlmostEqual(float(z[0]), 81.65, places=6)

    def test_nothing_to_do_when_it_already_meets(self):
        plans, specs = self.plans()
        plans[0][1].z[:] = self.DECK
        plans[1][1].z[:] = self.DECK
        self.assertFalse(self.planner()._pin_cw_ends(plans, specs))

    def test_no_profile_no_change(self):
        plans, specs = self.plans()
        self.assertFalse(self.planner(profile_ok=False)._pin_cw_ends(plans, specs))

    def test_off_switch(self):
        self.assertTrue(grading.SEAMS_ON)


class GradeMatchTest(unittest.TestCase):
    """_grade_match: a single span is not tilted more than SEAM_DECK_GRADE by the ground at its ends; the rest of the
    difference goes half to each approach, the lower end up, the higher down."""

    def planner(self, z0, z1, length=17.0, cls="secondary", chain=False):
        import mapcells
        g = object.__new__(grading._Planner)
        import threading
        g.lock = threading.Lock()
        import geodesy
        g.frame = geodesy.EnuFrame(50.5, 16.2)
        bridge = np.array([[0.0, 0.0], [length, 0.0]])
        g._cell_enu = lambda k, ground=True: {"ends": bridge, "end_of": [0, 0], "bridges": [bridge],
                                                "bridge_cls": [cls]}
        la, lo, _ = g.frame.enu_to_geodetic(bridge[:, 0], bridge[:, 1], 0.0)
        zs = {0: z0, 1: z1}

        def elev(a, b):
            a = np.asarray(a, np.float64)
            return np.where(np.abs(a - la[0]) < np.abs(a - la[1]), z0, z1) if abs(la[0] - la[1]) > 0 else np.full(a.shape, z0)
        g.elev = elev
        return g

    def test_steep_difference_is_shared(self):
        g = self.planner(398.1, 401.3)
        a = g._grade_match(np.array([0.0, 0.0]))
        b = g._grade_match(np.array([17.0, 0.0]))
        if a is None or b is None:
            self.skipTest("the stub's elevation does not tell the ends apart")
        self.assertGreater(a, 0.5)
        self.assertLess(b, -0.5)
        self.assertAlmostEqual(a, -b, places=6)

    def test_gentle_difference_is_left(self):
        g = self.planner(398.1, 398.4)
        self.assertIsNone(g._grade_match(np.array([0.0, 0.0])))

    def test_motorway_and_link_are_not_matched(self):
        for cls in ("motorway", "trunk", "primary_link"):
            self.assertIsNone(self.planner(398.1, 401.3, cls=cls)._grade_match(np.array([0.0, 0.0])), cls)


class HoldMouthTest(unittest.TestCase):
    """A street off a deck's abutment keeps the abutment's height over the junction's wedge, then goes back at 8 %."""

    def setUp(self):
        self.a = np.arange(0.0, 60.1, 3.0)                  # distance from the abutment
        self.z = 100.0 - 0.09 * np.minimum(self.a, 12.0) - 0.0 * self.a   # the street's own profile: falls 9 %, then level
        self.z = np.where(self.a > 12.0, 100.0 - 0.09 * 12.0, self.z)

    def test_held_then_back_at_grade(self):
        z = np.array(self.z)
        out = grading._Planner._hold_mouth(z, self.a)
        held = self.a <= grading.SEAM_MOUTH_M
        self.assertTrue(np.allclose(out[held], z[0]))
        g = np.abs(np.diff(out[~held | (self.a == self.a[held][-1])])) / 3.0
        self.assertLessEqual(g.max(), grading.SEAM_MOUTH_GRADE + 1e-9)
        self.assertAlmostEqual(out[-1], z[-1], places=6)    # (back on its own profile)

    def test_unreachable_profile_is_left(self):
        z = np.array(self.a * -0.5 + 100.0)                 # falls 50 %: the 8 % return never meets it
        out = grading._Planner._hold_mouth(z, self.a)
        self.assertTrue(np.array_equal(out, z))

    def test_short_street_is_left(self):
        a = np.array([0.0, 3.0])
        z = np.array([100.0, 99.0])
        self.assertTrue(np.array_equal(grading._Planner._hold_mouth(z, a), z))


class GradeMatchBankEndTest(unittest.TestCase):
    """_grade_match reads each end as its approach is laid (fill_abutment): a stream bridge whose map line ends down the
    bank on one side is not tilted by that end's low ground (Alsfeld, B 62 over a stream: 2.6 m over a 16 m deck)."""

    def planner(self, ground):
        import threading
        import geodesy
        g = object.__new__(grading._Planner)
        g.lock = threading.Lock()
        g.frame = geodesy.EnuFrame(50.7563, 9.2870)
        bridge = np.array([[0.0, 0.0], [16.0, 0.0]])
        g._cell_enu = lambda k, ground=True: {"ends": bridge, "end_of": [0, 0], "bridges": [bridge],
                                                "bridge_cls": ["primary"]}

        def elev(la, lo):
            x, _y, _ = g.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            return ground(np.asarray(x, np.float64))
        g.elev = elev
        return g

    @staticmethod
    def stream(x):
        # the road's flat 243.25 m, a stream's 3.5 m valley from x = -6 to 9 (its bed at x = 3): the bridge's west end
        # (x = 0) on the bank 3 m down, its east end (x = 16) on the flat
        return np.where((x > -6.0) & (x < 9.0), 243.25 - 3.5 * np.cos(np.clip((x - 3.0) / 6.0, -1, 1) * np.pi / 2), 243.25)

    def test_end_down_the_bank_is_not_matched(self):
        g = self.planner(self.stream)
        self.assertLess(float(self.stream(np.array([0.0]))[0]), 241.0)     # (the end's own ground: down the bank)
        self.assertIsNone(g._grade_match(np.array([0.0, 0.0])))
        self.assertIsNone(g._grade_match(np.array([16.0, 0.0])))

    def test_off_switch_brings_the_old_match_back(self):
        from unittest import mock
        with mock.patch.object(grading, "SEAM_FILLED_ON", False):
            g = self.planner(self.stream)
            a = g._grade_match(np.array([0.0, 0.0]))
            b = g._grade_match(np.array([16.0, 0.0]))
        self.assertGreater(a, 0.5)
        self.assertLess(b, -0.5)

    def test_real_step_between_the_ends_is_still_shared(self):
        # the ground really differs on either side (flat at 398.1 m to the west, 401.3 m to the east): matched as before
        g = self.planner(lambda x: np.where(x < 8.0, 398.1, 401.3))
        a = g._grade_match(np.array([0.0, 0.0]))
        b = g._grade_match(np.array([16.0, 0.0]))
        self.assertGreater(a, 0.5)
        self.assertLess(b, -0.5)
        self.assertAlmostEqual(a, -b, places=6)

    def test_an_end_on_a_hills_brow_is_not_filled_above_its_ground(self):
        # a track up a hill: the ground falls ever more steeply west of the bridge's lower end (x = 0) and rises past
        # its upper one; fill_abutment's line through the far slope would read the lower end 4 m over the higher one
        # (Riva del Garda's Pontera hill, test_drivability): matched on the ground at the ends, as with the switch off
        def hill(x):
            return np.where(x < 0.0, 195.0 + 0.2 * x - 0.008 * x * x, 195.0 + 0.2 * x)
        from unittest import mock
        g = self.planner(hill)
        line = np.array([[0.0, 0.0], [16.0, 0.0]])
        self.assertGreater(g._end_filled(line, False), hill(np.array([16.0]))[0] + 1.0)     # (the bare fill: over the top)
        self.assertAlmostEqual(g._end_filled(line, False, past=True), 195.0, places=3)
        a = g._grade_match(np.array([0.0, 0.0]))
        b = g._grade_match(np.array([16.0, 0.0]))
        with mock.patch.object(grading, "SEAM_FILLED_ON", False):
            g = self.planner(hill)
            self.assertEqual((a, b), (g._grade_match(np.array([0.0, 0.0])), g._grade_match(np.array([16.0, 0.0]))))
        self.assertGreater(a, 0.5)                                          # (the lower end's approach raised, as before)


class StructEndsTest(unittest.TestCase):
    """BNE_DECK_STRUCT=abut (road revamp RS S2b part 2a): twin decks side by side share one abutment height at each
    common end (_struct_end: earthfast.structure_abutments), and every approach road ending there is brought to it
    (_pin_struct_ends)."""

    def planner(self, zs, lift=None, cls="primary", roads_=()):
        import threading
        import geodesy
        g = object.__new__(grading._Planner)
        g.lock = threading.Lock()
        g.frame = geodesy.EnuFrame(50.5, 16.2)
        # twins 60 m long, 9 m apart centre to centre (1 m edge to edge at 4 m half widths), b drawn the other way
        a = np.array([[0.0, 0.0], [30.0, 0.0], [60.0, 0.0]])
        b = np.array([[62.0, 9.0], [32.0, 9.0], [2.0, 9.0]])
        far = np.array([[500.0, 500.0], [560.0, 500.0]])      # (a deck of its own far off)
        lines = [a, b, far]
        g._cell_enu = lambda k, ground=True: {
            "ends": np.array([ln[i] for ln in lines for i in (0, -1)]), "end_of": [0, 0, 1, 1, 2, 2],
            "bridges": lines, "bridge_ids": ["a", "b", "far"], "bridge_parts": [0, 0, 0],
            "bridge_cls": [cls] * 3, "bridge_levels": [1.0] * 3, "bridge_half": [4.0] * 3,
            "ground": [(np.asarray(ln, np.float64), float(h), "primary") for ln, h in roads_]}
        ends = {(0.0, 0.0): zs[0], (60.0, 0.0): zs[1], (62.0, 9.0): zs[2], (2.0, 9.0): zs[3]}

        def elev(la, lo):
            e, n, _ = g.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            out = [min(ends.items(), key=lambda kv: (kv[0][0] - x) ** 2 + (kv[0][1] - y) ** 2)[1]
                   for x, y in zip(np.atleast_1d(e), np.atleast_1d(n))]
            return np.asarray(out, np.float64)
        g.elev = elev
        g._end_filled = lambda line, at_end, past=False: -np.inf
        g._end_lift_calc = lambda xy: lift(xy) if lift is not None else None
        return g

    @staticmethod
    def z(g, xy):
        got = g._struct_end(np.asarray(xy, np.float64))
        return None if got is None else got[0]

    def test_twins_share_each_end(self):
        g = self.planner((10.0, 11.0, 11.6, 10.3))
        self.assertAlmostEqual(self.z(g, [0.0, 0.0]), 10.15, places=6)
        self.assertAlmostEqual(self.z(g, [2.0, 9.0]), 10.15, places=6)
        self.assertAlmostEqual(self.z(g, [61.0, 0.5]), 11.3, places=6)    # (a road end near it)
        self.assertAlmostEqual(self.z(g, [62.0, 9.0]), 11.3, places=6)
        self.assertIsNone(self.z(g, [500.0, 500.0]))     # (no twin)
        self.assertIsNone(self.z(g, [30.0, -20.0]))      # (no bridge end)

    def test_each_point_answers_for_itself_whatever_asked_first(self):
        """Two road ends in one half metre either side of the APPROACH_TOUCH_M cutoff: each its own answer, in
        either order (Codex review: a half-metre memory answered for whoever asked first)."""
        inside, outside = [-(grading.APPROACH_TOUCH_M - 0.01), 0.0], [-(grading.APPROACH_TOUCH_M + 0.01), 0.0]
        for order in ((inside, outside), (outside, inside)):
            g = self.planner((10.0, 11.0, 11.6, 10.3))
            got = {tuple(p): self.z(g, p) for p in order}
            self.assertAlmostEqual(got[tuple(inside)], 10.15, places=6)
            self.assertIsNone(got[tuple(outside)])

    def test_a_clearance_need_lifts_the_whole_end(self):
        def lift(xy):
            return (1.5, [], [], False) if abs(float(xy[0])) < 1 and abs(float(xy[1])) < 1 else None
        g = self.planner((10.0, 11.0, 10.2, 10.1), lift=lift)
        self.assertAlmostEqual(self.z(g, [0.0, 0.0]), 11.5, places=6)
        self.assertAlmostEqual(self.z(g, [2.0, 9.0]), 11.5, places=6)

    def test_a_small_need_over_the_mean_is_kept(self):
        """A clearance target under APPROACH_MIN_LIFT over its own end but over the group's mean still bounds the
        shared height from below (Codex review: it was dropped and the end lowered under it)."""
        def lift(xy):
            return (0.4, [], [], False) if abs(float(xy[0]) - 2.0) < 1 and abs(float(xy[1]) - 9.0) < 1 else None
        g = self.planner((10.0, 11.0, 11.6, 10.6), lift=lift)
        self.assertAlmostEqual(self.z(g, [0.0, 0.0]), 11.0, places=6)     # (10.6 + 0.4, over the mean 10.3)

    def test_spread_ends_are_left(self):
        g = self.planner((10.0, 11.0, 11.6, 11.5))
        self.assertIsNone(self.z(g, [0.0, 0.0]))
        self.assertIsNotNone(self.z(g, [60.0, 0.0]))

    def test_a_raised_end_is_never_lowered(self):
        """A plan end standing over its own ground (a solved or measured deck end, a small lift) keeps its height
        where sharing would lower it (Codex review)."""
        g = self.planner((10.0, 11.0, 11.6, 9.4))      # (shared at the first ends: 9.7, under a's ground)
        s = np.arange(0.0, 101.0, 4.0)
        ra = grading.Plan(np.c_[-100.0 + s, np.zeros(len(s))], np.full(len(s), 10.4), 5.0, abut=(False, True))
        ra.rid, ra.max_wall = 1, None
        self.assertFalse(g._pin_struct_ends([("ra", ra)], [spec("primary")]))

    def test_fill_onto_a_road_beside_is_left(self):
        g = self.planner((10.0, 11.0, 11.6, 10.6))      # (shared 10.3: a's approach up 0.3 m)
        beside = (np.array([[-100.0, -6.5], [0.0, -6.5]]), 3.0)       # (a road 6.5 m off, edge to edge 0.5 m)
        orig = g._struct_end
        g._struct_end = lambda xy: None if orig(xy) is None else (orig(xy)[0], [beside], orig(xy)[2])
        s = np.arange(0.0, 101.0, 4.0)
        ra = grading.Plan(np.c_[-100.0 + s, np.zeros(len(s))], np.full(len(s), 10.0), 3.0, abut=(False, True))
        ra.rid, ra.max_wall = 1, None
        self.assertFalse(g._pin_struct_ends([("ra", ra)], [spec("primary")]))

    # (the ground roads round the first ends: both approaches, which rise with them, and a street the decks span 5 m in)
    APPROACHES = [([[-100.0, 0.0], [0.0, 0.0]], 3.0), ([[-98.0, 9.0], [2.0, 9.0]], 3.0),
                  ([[5.0, -40.0], [5.0, 40.0]], 4.0)]

    def unlifted_raise(self, ground):
        """a's approach raised 0.3 m to its twin's height at the first ends (shared 10.3), with no lift of its own:
        _end_lift_calc None, so no caps from it."""
        g = self.planner((10.0, 11.0, 11.6, 10.6), roads_=ground)
        s = np.arange(0.0, 101.0, 4.0)
        ra = grading.Plan(np.c_[-100.0 + s, np.zeros(len(s))], np.full(len(s), 10.0), 3.0, abut=(False, True))
        ra.rid, ra.max_wall = 1, None
        plans = [("ra", ra)]
        return g._pin_struct_ends(plans, [spec("primary")]), np.asarray(plans[0][1].z)

    def test_an_unlifted_end_keeps_clear_of_a_road_beside(self):
        """An end with no lift of its own raised to its group's height beside a through road inside the raise's fill
        keeps its height (Codex review: no lift gave no caps, and its fill was laid onto the road)."""
        beside = ([[-100.0, -6.5], [-10.0, -6.5]], 3.0)      # (6.5 m off, its edge 0.5 m from the approach's)
        done, z = self.unlifted_raise(self.APPROACHES + [beside])
        self.assertFalse(done)
        self.assertAlmostEqual(float(z[-1]), 10.0, places=6)

    def test_an_unlifted_end_with_no_road_beside_is_raised(self):
        done, z = self.unlifted_raise(self.APPROACHES + [([[-100.0, -30.0], [-10.0, -30.0]], 3.0)])   # (30 m off)
        self.assertTrue(done)
        self.assertAlmostEqual(float(z[-1]), 10.3, places=6)
        self.assertAlmostEqual(float(z[0]), 10.0, places=6)

    def test_an_unlifted_end_keeps_clear_of_a_spanned_road_turning_alongside(self):
        """A street that passes under the decks and then runs along the approach is kept where it is out from under
        them (Codex review: any crossing exempted the whole road)."""
        bend = ([[5.0, 30.0], [5.0, -6.5], [-100.0, -6.5]], 3.0)
        done, z = self.unlifted_raise(self.APPROACHES[:2] + [bend])
        self.assertFalse(done)
        self.assertAlmostEqual(float(z[-1]), 10.0, places=6)

    def test_beside_keeps_a_twin_approach_unless_it_rises(self):
        """A twin's approach is let in the fill only where it stands at the shared height (rise); one raised only by
        its own pin may be left low (Codex review)."""
        g = self.planner((10.0, 11.0, 11.6, 10.6), roads_=self.APPROACHES[:2])
        spans = [(np.array([[0.0, 0.0], [60.0, 0.0]]), 4.0), (np.array([[62.0, 9.0], [2.0, 9.0]]), 4.0)]
        ends = [[0.0, 0.0], [2.0, 9.0]]

        def firsts(rise):
            return sorted(tuple(np.round(np.asarray(ln)[0], 1)) for ln, _h in g._struct_beside([0.0, 0.0], ends, rise,
                                                                                                spans))
        self.assertEqual(firsts([[0.0, 0.0]]), [(-98.0, 9.0)])
        self.assertEqual(firsts(ends), [])

    def test_beside_finds_a_fast_deck_past_the_group_reach(self):
        """FAST_DECK_GROUND: a motorway deck beside the far end of a long seam, past the end group's STRUCT_NEAR_M, is
        an obstacle (Codex review: only the group's own deck search found them); the structure's own decks are not."""
        g = self.planner((10.0, 11.0, 11.6, 10.6))
        cell = g._cell_enu
        mw = np.array([[-140.0, -6.0], [-115.0, -6.0]])

        def with_mw(k, ground=True):
            c = dict(cell(k, ground))
            c["bridges"] = list(c["bridges"]) + [mw]
            c["bridge_cls"] = list(c["bridge_cls"]) + ["motorway"]
            c["bridge_half"] = list(c["bridge_half"]) + [5.0]
            return c
        g._cell_enu = with_mw
        spans = [(np.array([[0.0, 0.0], [60.0, 0.0]]), 4.0), (np.array([[62.0, 9.0], [2.0, 9.0]]), 4.0)]
        got = g._struct_beside([0.0, 0.0], [[0.0, 0.0], [2.0, 9.0]], [[0.0, 0.0]], spans, True)
        self.assertEqual([tuple(np.asarray(ln)[0]) for ln, _h in got], [(-140.0, -6.0)])
        self.assertEqual(g._struct_beside([0.0, 0.0], [[0.0, 0.0], [2.0, 9.0]], [[0.0, 0.0]], spans, False), [])

    def test_a_short_approach_is_left(self):
        g = self.planner((10.0, 11.0, 11.6, 10.3))
        s = np.arange(0.0, 16.1, 4.0)
        ra = grading.Plan(np.c_[-16.0 + s, np.zeros(len(s))], np.full(len(s), 11.0), 5.0, abut=(False, True))
        ra.rid, ra.max_wall = 1, None
        self.assertFalse(g._pin_struct_ends([("ra", ra)], [spec("primary")]))

    def test_approaches_meet_at_the_shared_height(self):
        g = self.planner((10.0, 11.0, 11.6, 10.3))
        s = np.arange(0.0, 101.0, 4.0)
        ra = grading.Plan(np.c_[-100.0 + s, np.zeros(len(s))], np.full(len(s), 10.0), 5.0, abut=(False, True))
        rb = grading.Plan(np.c_[-98.0 + s, np.full(len(s), 9.0)], np.full(len(s), 10.3), 5.0,
                          abut=(False, True))
        for p, rid in ((ra, 1), (rb, 2)):
            p.rid, p.max_wall = rid, None
        plans = [("ra", ra), ("rb", rb)]
        self.assertTrue(g._pin_struct_ends(plans, [spec("primary"), spec("primary")]))
        za, zb = np.asarray(plans[0][1].z), np.asarray(plans[1][1].z)
        self.assertAlmostEqual(float(za[-1]), 10.15, places=6)
        self.assertAlmostEqual(float(zb[-1]), 10.15, places=6)
        self.assertLessEqual(float(np.abs(np.diff(za)).max()) / 4.0, grading.STRUCT_SEAM_GRADE * 1.5 + 1e-9)
        self.assertAlmostEqual(float(za[0]), 10.0, places=6)            # (far from the end as it was)
        self.assertFalse(g._pin_struct_ends(plans, [spec("primary"), spec("primary")]))   # (already there)


if __name__ == "__main__":
    unittest.main()
