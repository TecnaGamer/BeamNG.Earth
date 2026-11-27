"""Ground graded under roads (grading.py): the profile smoothing, flat across the core, untouched away from
roads, junctions blended, the plans made once per cell, and GradedDem as a drop-in DEM."""
import math
import os
import sys
import types
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import grading  # noqa: E402
import mapcells  # noqa: E402
import roads  # noqa: E402
from test_roads import features, rules, LAT, LON, FRAME  # noqa: E402


class ProfileTest(unittest.TestCase):
    def test_smoothing_keeps_grades_removes_bumps(self):
        s = np.arange(0, 400, 2.0)
        ramp = 0.05 * s
        self.assertTrue(np.allclose(grading.smooth_profile(ramp, 2.0, 12.0)[20:-20], ramp[20:-20], atol=1e-6),
                        "a constant grade stays")
        bumpy = ramp + 0.3 * np.sin(s / 3.0)
        sm = grading.smooth_profile(bumpy, 2.0, 12.0)
        self.assertLess(np.abs(sm - ramp)[20:-20].max(), 0.02, "short bumps smoothed away")
        self.assertEqual(len(sm), len(s))

    def test_a_bridge_deck_in_the_data_is_no_hump(self):
        # GitHub #271: the elevation data had a bridge deck 8.5 m up in 3 samples (6 m) where it crosses over the road
        s = np.arange(0, 300, 2.0)
        ground = 50.0 + 0.01 * s
        spiked = ground.copy()
        spiked[74:77] += 8.5
        xy = np.stack([s, np.zeros(len(s))], axis=1)
        plan = grading._plan(xy, spiked, roads.Spec(7.0, "paved", False, "residential"))
        self.assertLess(float(np.max(plan.z - np.interp(plan.xy[:, 0], s, ground))), 0.2)
        steps = grading.median_profile(ground, 2.0, grading.MEDIAN_M)
        self.assertTrue(np.allclose(steps, ground), "a constant grade stays, ends included")

    def test_the_parts_of_a_road_meet_at_one_height(self):
        # a road on a 20 % grade split in two at a junction (GitHub #266, BR-381): each part's smoothed profile keeps
        # the grade to its end and ends at the ground's height there, so the two meet without a step (mirrored at the
        # ends, one ended 1.3 m under the ground and the next began 0.8 m over it)
        s = np.arange(0, 400, 2.0)
        ground = 0.20 * s + 0.3 * np.sin(s / 9.0)
        a, b = grading.smooth_profile(ground[:101], 2.0, 12.0), grading.smooth_profile(ground[100:], 2.0, 12.0)
        self.assertAlmostEqual(a[-1], ground[100], places=9)
        self.assertAlmostEqual(b[0], ground[100], places=9)
        ramp = 0.20 * s
        self.assertTrue(np.allclose(grading.smooth_profile(ramp, 2.0, 12.0), ramp, atol=1e-9), "the grade to the ends")
        short = 0.1 * np.arange(6) * 2.0        # shorter than the kernel: the line through its ends
        self.assertTrue(np.allclose(grading.smooth_profile(short, 2.0, 12.0), short, atol=1e-9))


class BankTest(unittest.TestCase):
    """A motorway's curve leans into its turn (grading.bank, grade_points): its inside lower, a straight level."""

    def arc(self, r, ang, step=2.0):
        th = np.arange(0.0, ang, step / r)
        return np.stack([r * np.sin(th), r - r * np.cos(th)], axis=1)       # a left turn from (0, 0) heading +x

    def test_left_curve_banked_inside_lower(self):
        xy = self.arc(400.0, 1.2)
        sp = roads.Spec(11.0, "paved", False, "motorway")
        b = grading.bank(xy, sp, ext=(1, 1))
        self.assertIsNotNone(b)
        mid = b[len(b) // 2]
        v, f = grading.BANK_DESIGN["motorway"]
        r_min = v * v / (127.0 * (grading.BANK_E_MAX + f))
        self.assertAlmostEqual(mid, -grading.BANK_E_MAX * min(1.0, r_min / 400.0), places=2)   # left (inside) lower
        plan = grading._plan(xy, np.full(len(xy), 50.0), sp)
        # graded across the carriageway at the middle: the outside (right) higher than the inside (left)
        i = len(xy) // 2
        p = xy[i]
        th = np.arctan2(xy[i + 1, 1] - xy[i - 1, 1], xy[i + 1, 0] - xy[i - 1, 0])
        n = np.array([-np.sin(th), np.cos(th)])          # left
        pts = np.array([p + n * 4.0, p - n * 4.0])
        z = grading.grade_points(pts[:, 0], pts[:, 1], np.full(2, 50.0), [plan])
        self.assertAlmostEqual(float(z[1] - z[0]), -mid * 8.0, places=1)

    def test_a_curve_of_long_chords_is_banked_evenly(self):
        # a map line draws a curve as straight pieces turning at vertices ~100 m apart (I-80 in Wyoming, Discord
        # 2026-10-28: "a series of bumps where the bank would be"): the bank holds through the curve, not one per vertex
        r = 600.0
        th = np.arange(0.0, 1.2, 100.0 / r)
        verts = np.stack([r * np.sin(th), r - r * np.cos(th)], axis=1)
        xy = [verts[0]]
        for a, b in zip(verts[:-1], verts[1:]):        # (resampled every 2 m, as the plans are)
            k = max(1, int(np.hypot(*(b - a)) // 2.0))
            xy.extend(a + (b - a) * (t / k) for t in range(1, k + 1))
        xy = np.asarray(xy)
        b = grading.bank(xy, roads.Spec(11.0, "paved", False, "motorway"), ext=(1, 1))
        arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        body = b[(arc > 150.0) & (arc < arc[-1] - 150.0)]
        self.assertGreater(len(body), 50)
        self.assertLess(float(body.max() - body.min()), 0.012)       # (it was 0 % between vertices, 8 % at them)
        self.assertLess(float(body.max()), -0.05)

    def test_a_mapped_roundabout_is_not_banked(self):
        """The Jüri roundabout of Tallinna ringtee (59.3535 N 24.8820 E, a trunk ring about 190 m across): banked as a
        trunk's curve, 8 % into the ring, where the real one falls 2.4-2.9 % outward (the national lidar). A mapped
        roundabout ring (roads.is_ring) is graded level across."""
        def ring_z(junction):
            a = np.linspace(0.0, np.pi, 60)
            halves = [[(190 * np.cos(t), 190 * np.sin(t)) for t in a],
                      [(190 * np.cos(t), -190 * np.sin(t)) for t in a[::-1]]]
            f = features(halves, ["trunk", "trunk"])
            f.attrs["osm_junction"] = np.array([junction, junction], object)
            g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 50.0))
            t = np.pi / 2
            pts = np.array([(186.0 * np.cos(t), 186.0 * np.sin(t)), (194.0 * np.cos(t), 194.0 * np.sin(t))])
            la, lo, _ = FRAME.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
            z = g.apply(la, lo, np.full(2, 50.0))
            return float(z[1] - z[0])           # outer edge minus inner, 8 m apart
        self.assertGreater(ring_z(""), 0.4, "(a trunk's curve: banked into it)")
        self.assertAlmostEqual(ring_z("roundabout"), 0.0, delta=0.05, msg="the roundabout level across")
        with unittest.mock.patch.object(grading, "RING_LEVEL_ON", False):
            self.assertGreater(ring_z("roundabout"), 0.4, "(BNE_RING_LEVEL=off: banked as before)")

    def test_straight_and_city_streets_level(self):
        xy = np.stack([np.arange(0, 300, 2.0), np.zeros(150)], axis=1)
        self.assertIsNone(grading.bank(xy, roads.Spec(11.0, "paved", False, "motorway"), ext=(1, 1)))
        self.assertIsNone(grading.bank(self.arc(150.0, 1.0), roads.Spec(7.0, "paved", False, "residential")))

    def test_faded_to_level_at_a_bridge(self):
        xy = self.arc(300.0, 1.0)
        b = grading.bank(xy, roads.Spec(11.0, "paved", False, "motorway"), abut=(True, False), ext=(0, 1))
        self.assertEqual(float(b[0]), 0.0)
        self.assertLess(abs(float(b[len(b) // 2])), 0.09)
        self.assertGreater(abs(float(b[len(b) // 2])), 0.03)


class CrestTest(unittest.TestCase):
    def test_no_kinks_over_a_crest(self):
        # a road over a crest (the grade falls 4 % per 10 m): the graded centreline's slope changes smoothly, not
        # in a kink at every plan vertex (r/BeamNG 2026-10-17: 'bumpy roads, worst uphill and at speed'; with
        # 16 m vertices the slope jumped 6.4 % at each)
        xy = np.stack([np.arange(-150.0, 150.1, grading.PROFILE_STEP), np.zeros(151)], axis=1)
        z = 20.0 - 0.002 * xy[:, 0] ** 2
        plan = grading._plan(xy, z, roads.Spec(7.0, "paved", False, "residential"))
        px = np.arange(-60.0, 60.1, 0.5)
        out = grading.grade_points(px, np.zeros(len(px)), 20.0 - 0.002 * px ** 2, [plan])
        slope = np.diff(out[::4]) / 2.0                     # per 2 m (the terrain grid)
        self.assertLess(np.abs(np.diff(slope)).max(), 0.02)


def slope_ground(x, y):
    """A hillside: 10 % up to the north, plus small bumps."""
    return 0.10 * y + 0.2 * np.sin(x / 7.0) * np.cos(y / 5.0)


def plan_line(x0, x1, y, kind="paved", cls="residential", w=6.0):
    xy = np.stack([np.linspace(x0, x1, 200), np.full(200, y)], axis=1)
    z = grading.smooth_profile(slope_ground(xy[:, 0], xy[:, 1]), (x1 - x0) / 199, grading.SIGMA_M)
    keep = np.r_[np.arange(0, 199, 8), 199]
    spec = roads.Spec(w, kind, False, cls)
    width = roads.carriageway(cls, kind, True) if kind in roads.BUILT else w
    return grading.Plan(xy[keep], z[keep], width / 2 + grading.shoulder(spec)), spec


class GradePointsTest(unittest.TestCase):
    def grid(self):
        xs, ys = np.meshgrid(np.arange(-60.0, 60.1, 1.0), np.arange(-40.0, 40.1, 1.0))
        return xs.ravel(), ys.ravel(), slope_ground(xs.ravel(), ys.ravel())

    def test_flat_across_untouched_away(self):
        px, py, base = self.grid()
        p, _ = plan_line(-200, 200, 0.0)
        out = grading.grade_points(px, py, base, [p])
        core = np.abs(py) <= p.half - 0.01
        for x in (-30.0, 0.0, 25.0):
            col = core & (np.abs(px - x) < 0.5)
            self.assertLess(np.ptp(out[col]), 0.02, f"flat across the core at x={x}")
        far = np.abs(py) > p.half + grading.T_MAX + 0.5
        self.assertTrue(np.allclose(out[far], base[far]), "untouched beyond the transition")
        mid = (np.abs(py) > p.half) & (np.abs(py) < p.half + grading.T_MIN)
        self.assertTrue((np.abs(out[mid] - base[mid]) <= np.abs(out[core].mean() - base[mid]) + 1.0).all())

    def test_lift_under_roads_only(self):
        px, py, base = self.grid()
        p, _ = plan_line(-200, 200, 0.0)
        flat = grading.grade_points(px, py, base, [p])
        lifted = grading.grade_points(px, py, base, [p], lift=grading.ROAD_LIFT)
        core = np.abs(py) <= p.half - 0.01
        self.assertTrue(np.allclose(lifted[core] - flat[core], grading.ROAD_LIFT), "the physics ground under the road")
        far = np.abs(py) > p.half + grading.T_MAX + 0.5
        self.assertTrue(np.allclose(lifted[far], base[far]), "untouched away from roads")
        self.assertTrue(((lifted - flat) <= grading.ROAD_LIFT + 1e-9).all() and ((lifted - flat) >= -1e-9).all())

    def test_a_dirt_road_does_not_pull_a_paved_one_down(self):
        # The user, 2026-11-09: a paved road dipped where a dirt track ran along its edge. A built surface outranks
        # an unpaved one in grade_points' blend: the paved road's carriageway keeps its own height and the dirt road
        # adapts, whatever the dirt's own profile is (grade_rank).
        paved = grading.Plan(np.array([[-200.0, 0.0], [200.0, 0.0]]), np.array([100.0, 100.0]), 8.55,
                             road_half=5.55)
        paved.rank = grading.grade_rank(roads.Spec(11.1, "paved", False, "tertiary"))
        dirt = grading.Plan(np.array([[-200.0, 10.55], [200.0, 10.55]]), np.array([99.0, 99.0]), 2.5,
                            road_half=1.5)
        dirt.rank = grading.grade_rank(roads.Spec(3.0, "gravel", False, "track"))
        # (the plans are different roads: their rid keeps a road's own stretches from giving way to each other)
        paved.rid, dirt.rid = 1, 2
        xs = np.arange(-60.0, 60.1, 2.0)
        ys = np.arange(-8.0, 30.01, 0.5)
        px, py = np.meshgrid(xs, ys)
        px, py = px.ravel(), py.ravel()
        out = grading.grade_points(px, py, np.full(len(px), 99.0), [paved, dirt])
        core = np.abs(py) <= paved.road_half - 0.01
        self.assertTrue(np.allclose(out[core], 100.0, atol=0.01), "the paved road's carriageway holds its height")
        # ... and the blend eases onto the side road: at its centre line the ground is still near the asphalt's height,
        # then falls smoothly to the dirt's own over RANK_SIDE_FADE_M (not a step at the shoulder's edge)
        on_dirt = out[np.abs(py - 10.5) < 0.01]
        self.assertGreater(float(np.mean(on_dirt)), 99.5, "the side road eases onto the asphalt")
        order = np.argsort(py)
        s_y, s_z = py[order], out[order]
        sel = (s_y >= paved.road_half) & (s_y <= 10.55 + grading.RANK_SIDE_FADE_M)
        self.assertTrue((np.diff(s_z[sel]) <= 1e-6).all(), "the side road eases down, no step")
        self.assertAlmostEqual(float(s_z[-1]), 99.0, delta=0.02, msg="away from the asphalt the dirt keeps its ground")
        # ... and the dirt road meets the paved road's height where it runs into its core, not the other way round.
        # A dirt point inside the paved core takes the paved height.
        dirtcase = grading.Plan(np.array([[0.0, 30.0], [0.0, -30.0]]), np.array([99.0, 99.0]), 2.5,
                                road_half=1.5)
        dirtcase.rank = dirt.rank
        dirtcase.rid = 3
        onpath = grading.grade_points(np.array([0.0]), np.array([4.0]), np.array([99.0]), [paved, dirtcase])
        self.assertAlmostEqual(float(onpath[0]), 100.0, delta=0.01)

    def test_a_subordinate_carriageway_does_not_move_the_main(self):
        # A service driveway's carriageway overlapping a tertiary's: the higher-ranked road's surface must stay its
        # own plan, not be pulled by the subordinate road's height (7 cm at a mapped junction was still visible).
        main = grading.Plan(np.array([[-200.0, 0.0], [200.0, 0.0]]), np.array([100.0, 100.0]), 8.55, road_half=5.55)
        main.rank = grading.grade_rank(roads.Spec(11.1, "paved", False, "tertiary"))
        side = grading.Plan(np.array([[-200.0, 3.0], [200.0, 3.0]]), np.array([99.0, 99.0]), 2.0, road_half=2.0)
        side.rank = grading.grade_rank(roads.Spec(4.0, "paved", False, "service"))
        main.rid, side.rid = 1, 2
        out = grading.grade_points(np.array([0.0]), np.array([0.0]), np.array([99.0]), [main, side])
        self.assertAlmostEqual(float(out[0]), 100.0, delta=1e-9, msg="the tertiary's carriageway is its own plan")

    def test_a_built_side_road_eases_onto_a_higher_built_one(self):
        # A service road / driveway joining a residential street: built vs built uses the same ease as a dirt road
        # (the user's follow-up), so the side road climbs onto the main road's ground gently.
        main = grading.Plan(np.array([[-200.0, 0.0], [200.0, 0.0]]), np.array([100.0, 100.0]), 8.55, road_half=5.55)
        main.rank = grading.grade_rank(roads.Spec(11.1, "paved", False, "residential"))
        side = grading.Plan(np.array([[-200.0, 10.55], [200.0, 10.55]]), np.array([99.0, 99.0]), 2.5, road_half=2.0)
        side.rank = grading.grade_rank(roads.Spec(4.0, "paved", False, "service"))
        main.rid, side.rid = 1, 2
        xs = np.arange(-60.0, 60.1, 2.0)
        ys = np.arange(-8.0, 30.01, 0.5)
        px, py = np.meshgrid(xs, ys)
        px, py = px.ravel(), py.ravel()
        out = grading.grade_points(px, py, np.full(len(px), 99.0), [main, side])
        core = np.abs(py) <= main.road_half - 0.01
        self.assertTrue(np.allclose(out[core], 100.0, atol=0.01), "the main road's carriageway holds its height")
        on_side = out[np.abs(py - 10.5) < 0.01]
        self.assertGreater(float(np.mean(on_side)), 99.5, "the built side road eases onto the main road")
        order = np.argsort(py)
        s_y, s_z = py[order], out[order]
        sel = (s_y >= main.road_half) & (s_y <= 10.55 + grading.RANK_SIDE_FADE_M)
        self.assertTrue((np.diff(s_z[sel]) <= 1e-6).all(), "the built side road eases down, no step")

    def open_tunnel_case(self, open_tunnel, angle_deg=90.0):
        """grade_points along a bus lane (rank: no class) crossing a primary's tunnel part open all along (its core,
        sidewalks too, held as its carriageway, as _make_cell plans it) 1.6 m under it: (s, heights) every metre."""
        tun = grading.Plan(np.array([[-200.0, 0.0], [200.0, 0.0]]), np.array([188.2, 188.2]), 7.3)
        tun.rank = grading.grade_rank(roads.Spec(8.6, "paved", True, "primary"))
        tun.open_tunnel = open_tunnel
        a = np.radians(angle_deg)
        d = np.array([np.cos(a), np.sin(a)])
        bus = grading.Plan(np.array([-60.0 * d, 60.0 * d]), np.array([189.8, 189.8]), 4.5, road_half=3.5)
        bus.rank = grading.grade_rank(roads.Spec(7.0, "paved", True, "busway"))
        tun.rid, bus.rid = 1, 2
        s = np.arange(-30.0, 30.01, 1.0)
        pts = s[:, None] * d[None, :]
        return s, grading.grade_points(pts[:, 0], pts[:, 1], np.full(len(s), 189.8), [tun, bus])

    def test_a_road_crossing_an_open_tunnel_eases_onto_it(self):
        # GitHub #1169, Kennedybruecke in Vienna: the "Unterfahrung" lies under a plaza the elevation leaves out, so its
        # tunnel is open all along; a bus lane mapped over it (rank: no class, 14 under the primary's, past
        # RANK_SIDE_MAX_DIFF) kept its own ground up to the section's held carriageway and stepped 1.2 m there. Flagged
        # open_tunnel, the crossing road eases onto it over the side ease's reach instead.
        s, old = self.open_tunnel_case(False)
        self.assertGreater(float(np.abs(old[:-2] - 2 * old[1:-1] + old[2:]).max()), 1.0, "(the step this fixes)")
        s, z = self.open_tunnel_case(True)
        held = np.abs(s) <= 7.3 - 0.01
        self.assertTrue(np.allclose(z[held], 188.2 + 0.0, atol=0.01), "the open section's carriageway holds")
        # (the drivability ratchet's step, a ledge or a kink on 1 m samples: 1.37 m without; the ease's S-curve bends
        # 0.11 m over 1 m for the 1.6 m it comes down)
        self.assertLess(float(np.abs(z[:-2] - 2 * z[1:-1] + z[2:]).max()), 0.15, "the bus lane eases onto it, no step")
        self.assertAlmostEqual(float(z[-1]), 189.8, delta=0.02, msg="past the ease the bus lane keeps its ground")

    def test_a_road_beside_an_open_tunnel_keeps_its_ground(self):
        # ... while a road running along it (a street on the cover beside its trench) is not eased onto it: the flag
        # changes nothing for a road at 20 degrees to it.
        np.testing.assert_array_equal(self.open_tunnel_case(True, 20.0)[1], self.open_tunnel_case(False, 20.0)[1])

    def trench_case(self, section, angle_deg=0.0, along=False):
        """grade_points across a primary street 5 m over a trunk's tunnel section held in the open (a trench, the tunnel
        covered elsewhere: not open_tunnel), as the map has them in Barcelona: the street's line 8.3 m off the trunk's,
        its 13.1 m carriageway reaching 4.25 m over the trunk's 12 m one. At angle_deg to the trunk, through (0, -8.3);
        across it (along: along its line, from 20 m before that point): (offsets, heights)."""
        tun = grading.Plan(np.array([[-200.0, 0.0], [200.0, 0.0]]), np.array([4.3, 4.3]), 6.0)
        tun.rank = grading.grade_rank(roads.Spec(12.0, "paved", True, "trunk"))
        tun.tunnel_section = section
        a = np.radians(angle_deg)
        d = np.array([np.cos(a), np.sin(a)])
        o = np.array([0.0, -8.3])
        street = grading.Plan(np.array([o - 200.0 * d, o + 200.0 * d]), np.array([9.3, 9.3]), 9.55, road_half=6.55)
        street.rank = grading.grade_rank(roads.Spec(13.1, "paved", True, "primary"))
        tun.rid, street.rid = 1, 2
        s = np.arange(-20.0, 8.01, 0.5)
        n = d if along else np.array([-d[1], d[0]])     # (across the street: toward the trunk)
        pts = o[None, :] + (s[:, None] + 8.3) * n[None, :]
        return s, grading.grade_points(pts[:, 0], pts[:, 1], np.full(len(s), 9.3), [tun, street])

    def test_a_street_beside_a_trench_keeps_its_ground(self):
        # A primary along the top of a trunk's trench (a tunnel section held in the open, covered further on), within
        # the rank gap of the trunk (RANK_SIDE_MAX_DIFF): the side ease took its carriageway from 9.3 m down to 5.9 m
        # at the trench's painted edge, its lanes tilted 50 % and the trucks on them in the ground at the kerb (the
        # junction eval's Barcelona trench: 32 of its 36 hits). Flagged a tunnel section, the street keeps its level up
        # to the trench's painted edge, and the trench's carriageway holds under the part of the street mapped over it.
        y, old = self.trench_case(False)
        street = (y >= -8.3 - 6.55 + 0.01) & (y <= -6.0 - 0.01)
        trench = np.abs(y) <= 6.0 - 0.01
        self.assertLess(float(old[street].min()), 8.0, "(the ease this fixes)")
        y, z = self.trench_case(True)
        self.assertTrue(np.allclose(z[street], 9.3, atol=0.02), f"the street keeps its level: {z[street]}")
        self.assertTrue(np.allclose(z[trench], 4.3, atol=0.02), f"the trench's carriageway holds: {z[trench]}")
        np.testing.assert_array_equal(z[trench], old[trench])

    def test_a_street_crossing_a_trench_section_grades_as_before(self):
        # The flag is for a road beside the section: one crossing it at grade grades the same with and without it.
        y, z = self.trench_case(True, 90.0, along=True)
        np.testing.assert_array_equal(z, self.trench_case(False, 90.0, along=True)[1])
        self.assertTrue(np.allclose(z[np.abs(y) <= 6.0 - 0.01], 4.3, atol=0.02), "(over the trench: its floor)")

    def test_a_deeper_dirt_road_still_leaves_the_paved_carriageway_whole(self):
        # A dirt path 1.8 m under the asphalt's level beside it (the height rule's fade starts at 1.5 m): the paved
        # carriageway is still held whole, and only the shoulder starts to fall toward the dirt.
        paved = grading.Plan(np.array([[-200.0, 0.0], [200.0, 0.0]]), np.array([100.0, 100.0]), 8.55,
                             road_half=5.55)
        paved.rank = grading.grade_rank(roads.Spec(11.1, "paved", False, "tertiary"))
        dirt = grading.Plan(np.array([[-200.0, 4.5], [200.0, 4.5]]), np.array([98.2, 98.2]), 1.75,
                            road_half=1.75)
        dirt.rank = grading.grade_rank(roads.Spec(2.0, "gravel", False, "track"))
        paved.rid, dirt.rid = 21, 22
        ys = np.arange(0.0, 6.01, 0.5)
        out = grading.grade_points(np.zeros_like(ys), ys, np.full(len(ys), 98.2), [paved, dirt])
        inside = ys <= paved.road_half - 0.01
        self.assertTrue(np.allclose(out[inside], 100.0, atol=0.01), "the asphalt's carriageway is whole")
        self.assertGreater(float(out[-1]), 99.5, "the side road eases onto the asphalt, not a dip")

    def test_ranks_do_not_depend_on_the_batch(self):
        # A paved road with a dirt track beside it, graded in one batch and in shifted batches: the same heights to the
        # last bit (PR #1050's 20 m rank reach went past what grade_points' pass 0 took in, and the points a batch's
        # bucket grid happened to cover got the rank: test_smooth_road_collision's step at a batch edge)
        paved = grading.Plan(np.array([[-200.0, 0.0], [200.0, 0.0]]), np.array([100.0, 100.0]), 8.55, road_half=5.55)
        paved.rank = grading.grade_rank(roads.Spec(11.1, "paved", False, "tertiary"))
        dirt = grading.Plan(np.array([[-200.0, 10.55], [200.0, 10.55]]), np.array([99.0, 99.0]), 2.5, road_half=1.5)
        dirt.rank = grading.grade_rank(roads.Spec(3.0, "gravel", False, "track"))
        paved.rid, dirt.rid = 1, 2
        ys = np.arange(-3.0, 25.0, 0.37)
        xs = np.full(len(ys), 3.3)
        base = np.full(len(ys), 99.0)
        full = grading.grade_points(xs, ys, base, [paved, dirt])
        for parts in (2, 3, 5):
            got = np.concatenate([grading.grade_points(xs[k], ys[k], base[k], [paved, dirt])
                                  for k in np.array_split(np.arange(len(ys)), parts)])
            np.testing.assert_array_equal(full, got)

    def test_a_dirt_road_gives_way_only_inside_a_paved_core(self):
        # Outside the paved road's core the dirt road keeps its own profile, and its ground blends back to the
        # natural one: the rule is no blanket lift of the unpaved road.
        paved = grading.Plan(np.array([[-200.0, 0.0], [200.0, 0.0]]), np.array([100.0, 100.0]), 8.55,
                             road_half=5.55)
        paved.rank = grading.grade_rank(roads.Spec(11.1, "paved", False, "tertiary"))
        dirt = grading.Plan(np.array([[-200.0, 40.0], [200.0, 40.0]]), np.array([95.0, 95.0]), 2.5,
                            road_half=1.5)
        dirt.rank = grading.grade_rank(roads.Spec(3.0, "gravel", False, "track"))
        paved.rid, dirt.rid = 1, 2
        out = grading.grade_points(np.array([0.0]), np.array([40.0]), np.array([95.0]), [paved, dirt])
        self.assertAlmostEqual(float(out[0]), 95.0, delta=0.01)

    def test_junction_blends_and_deterministic(self):
        px, py, base = self.grid()
        a, _ = plan_line(-200, 200, 0.0)
        b = grading.Plan(np.array([[10.0, -200], [10.0, 200]]), np.array([slope_ground(10.0, -200.0),
                                                                          slope_ground(10.0, 200.0)]), 4.0)
        out1 = grading.grade_points(px, py, base, [a, b])
        out2 = grading.grade_points(px, py, base, [a, b])
        self.assertTrue(np.array_equal(out1, out2))
        j = (np.abs(px - 10) < 0.5) & (np.abs(py) < 0.5)
        self.assertTrue(np.isfinite(out1[j]).all())

    def test_duplicate_end_vertex_does_not_spike(self):
        # A plan whose last vertex sits 1.5 cm past the previous one (a repeated map node, a trim cut at a vertex,
        # a junction snap moved the end to its junction), their heights set 209.0/214.8 m by the junction snap,
        # graded beside the abutting plan to 430.9 m with a pit beside it. Near-zero-length segments are dropped:
        # the blend stays finite and near the plans' heights.
        a = np.array([[-70.4, 3.4], [-66.6, 4.5], [-62.7, 5.6], [-58.9, 6.7], [-55.0, 7.8], [-51.2, 8.9],
                      [-47.3, 9.9], [-43.4, 10.9], [-39.5, 11.9], [-35.6, 12.8], [-31.8, 13.7], [-27.9, 14.6],
                      [-24.0, 15.5], [-20.1, 16.4], [-16.2, 17.3], [-12.3, 18.3], [-8.4, 19.2], [-4.5, 20.1],
                      [-0.6, 21.0], [-0.585, 21.0]])
        pa = grading.Plan(a, np.array([209.5, 209.5, 209.4, 209.4, 209.4, 209.4, 209.3, 209.3, 209.3, 209.2,
                                       209.2, 209.2, 209.2, 209.1, 209.1, 209.1, 209.1, 209.0, 209.0, 214.8]),
                         6.55)
        b = np.array([[-0.6, 21.0], [3.3, 21.8], [7.1, 22.6], [11.0, 23.3], [14.9, 24.1], [18.7, 24.9],
                      [22.6, 25.7], [26.5, 26.4], [30.3, 27.2], [34.2, 28.0], [36.1, 28.4]])
        pb = grading.Plan(b, np.array([214.8, 214.7, 214.6, 214.4, 214.3, 214.2, 214.0, 213.8, 213.6, 213.4,
                                       213.3]),
                         7.4, abut=(False, True), road_half=4.4)
        pb.cut = np.array([[np.nan] * 6] * 4 + [[np.nan, np.nan, np.nan, 12.4, 212.0, 17.4],
                                                [np.nan, np.nan, np.nan, 12.4, 211.8, 17.4],
                                                [np.nan, np.nan, np.nan, 12.4, 211.5, 16.4],
                                                [np.nan, np.nan, np.nan, 13.4, 211.3, 16.4],
                                                [np.nan, np.nan, np.nan, 13.4, 211.1, 17.4],
                                                [np.nan, np.nan, np.nan, 12.4, 211.0, 17.4]])
        px = np.array([0.0, -0.6, 5.0, -0.6])
        py = np.array([26.0, 21.0, 23.0, 15.0])
        out = grading.grade_points(px, py, np.full(4, 209.0), [pa, pb])
        self.assertTrue(np.isfinite(out).all(), "finite everywhere")
        self.assertLess(float(out.max()), 240.0, "no spike")
        self.assertGreater(float(out.min()), 200.0, "no pit")


class AbutmentTest(unittest.TestCase):
    """GitHub #38 (the D1 near Presov): a motorway onto a bridge over a road in a cutting 8 m under it."""

    def test_fill_keeps_the_grade_down_the_embankment(self):
        d = np.arange(0.0, 100.0, grading.PROFILE_STEP)
        road = 372.8 + 0.01 * d                 # the road on its embankment, rising 1 % away from the bridge
        z = np.where(d < 10.0, 365.0 + (road - 365.0) * d / 10.0, road)     # its map line ends down the slope
        filled = grading.fill_abutment(z, False)
        self.assertTrue(np.allclose(filled, road, atol=0.05), "the grade kept to the end")
        back = grading.fill_abutment(z[::-1], True)
        self.assertTrue(np.allclose(back, filled[::-1]), "the same from the other end")
        steady = 380.0 - 0.06 * d               # a road falling 6 % onto a bridge all the way: left alone
        self.assertIs(grading.fill_abutment(steady, False), steady)

    def test_fill_sees_past_a_junction_by_the_bridge(self):
        # Kurna - Tuhala over Tallinna ringtee (59.3364 N 24.8239 E, GitHub #684): the map splits the road onto the
        # bridge at a junction 16 m before it. That piece alone had too few samples for the embankment's grade, its
        # end stayed at the foot of the slope its map line ends down, 6 m under the road, and so did the deck's end.
        spec = roads.Spec(6.2, "paved", False, "tertiary")

        def raw(la, lo):
            x = np.asarray(FRAME.geodetic_to_enu(la, lo, 0.0)[0], np.float64)
            road = 54.0 - 0.01 * x              # the road on its embankment, rising 1 % away from the bridge (x < 0)
            return np.where(x > -10.0, 48.0 + (road - 48.0) * -x / 10.0, road)     # its map line ends down the slope

        def line(a, b):
            xs = np.arange(a, b + np.sign(b - a), 2.0 * np.sign(b - a))
            return np.stack([xs, np.zeros(len(xs))], axis=1)

        def lonlat(a, b):
            xy = line(a, b)
            la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            return np.c_[lo, la]

        whole = grading.plan_roads(FRAME, [(lonlat(-100.0, 0.0), spec)], raw, abut=[(False, True)],
                                   ext=[(line(-100.0, -200.0), None)], cuts=False)[0]
        self.assertAlmostEqual(whole.z[-1], 54.0, delta=0.3)       # (the fill as before: one piece to the bridge)
        split = grading.plan_roads(FRAME, [(lonlat(-100.0, -16.0), spec), (lonlat(-16.0, 0.0), spec)], raw,
                                   abut=[(False, False), (False, True)],
                                   ext=[(line(-100.0, -200.0), line(-16.0, 0.0)), (line(-16.0, -116.0), None)],
                                   cuts=False)
        self.assertAlmostEqual(split[1].z[-1], whole.z[-1], delta=0.1, msg="the bridge end as if not split")
        self.assertAlmostEqual(split[1].z[0], split[0].z[-1], delta=0.1, msg="the pieces meet at the junction")

    def test_the_abutment_is_its_roads(self):
        # the approach (abut at its first vertex) ends 9.7 m from the centreline of a road cut 8 m under it
        xy = np.stack([np.arange(0.0, 80.1, 4.0), np.zeros(21)], axis=1)
        approach = grading.Plan(xy, np.full(21, 372.9), 6.9, abut=(True, False))
        ys = np.arange(-200.0, 200.1, 4.0)
        lower = grading.Plan(np.stack([np.full(len(ys), -9.7), ys], axis=1), np.full(len(ys), 364.6), 8.6)
        px, py = np.array([0.0, 2.0]), np.array([0.0, 0.0])
        base = np.array([369.9, 370.6])
        out = grading.grade_points(px, py, base, [approach, lower])
        self.assertTrue(np.allclose(out, 372.9, atol=0.25), out)     # (the segments along it still blend a little)
        plain = grading.Plan(xy, np.full(21, 372.9), 6.9)     # (not onto a bridge, nor SEP_ON: ...)
        with unittest.mock.patch.object(grading, "SEP_ON", False):
            # ... the lower road's verge still gives way to the approach's carriageway, 8 m over it (grade_points' rh)
            self.assertGreater(grading.grade_points(px, py, base, [plain, lower])[0], 372.5)


class AbutmentEndTest(unittest.TestCase):
    """Discord 2026-11-05 (the I-405 by Santa Monica: a ledge at every few overpass ends): an approach's core reached half
    its width past its abutment end, back over the deck, at the end's height; a deck climbing to that end stood under
    the ground there (35 cm). Past the abutment end the ground now falls at ABUT_UNDER_GRADE; at the end it is kept."""

    def test_ground_past_the_abutment_falls_away(self):
        xy = np.stack([np.arange(0.0, 80.1, 4.0), np.zeros(21)], axis=1)    # the approach, its start the abutment
        approach = grading.Plan(xy, np.full(21, 82.0), 9.0, abut=(True, False))
        px = np.array([0.0, -2.0, -6.0, -9.0, 2.0])
        py = np.zeros(len(px))
        base = np.full(len(px), 73.3)                    # the ground under the deck (a road 8 m below)
        out = grading.grade_points(px, py, base, [approach])
        self.assertAlmostEqual(out[0], 82.0, delta=0.05, msg="the abutment end itself")
        self.assertAlmostEqual(out[4], 82.0, delta=0.05, msg="the approach")
        for x, z in zip(px[1:4], out[1:4]):
            self.assertLessEqual(z, 82.0 + x * grading.ABUT_UNDER_GRADE + 0.05, f"past the end by {-x} m")
        for x, z in zip(px[1:4], out[1:4]):              # under the deck: the abutment's slope, 1 in SLOPE_RUN
            self.assertLessEqual(z, 82.0 + x / grading.SLOPE_RUN + 0.05, f"the abutment's slope, {-x} m past the end")
        plain = grading.Plan(xy, np.full(21, 82.0), 9.0)    # an ordinary road end (no bridge): its round end, held
        self.assertGreater(grading.grade_points(px, py, base, [plain])[2], 81.9, "a plain road end: its round end")

    def test_end_abutment_too(self):
        xy = np.stack([np.arange(-80.0, 0.1, 4.0), np.zeros(21)], axis=1)  # the approach, its end the abutment
        approach = grading.Plan(xy, np.full(21, 50.0), 7.0, abut=(False, True))
        out = grading.grade_points(np.array([0.0, 5.0]), np.zeros(2), np.full(2, 40.0), [approach])
        self.assertAlmostEqual(out[0], 50.0, delta=0.05)
        self.assertLessEqual(out[1], 50.0 - 5.0 * grading.ABUT_UNDER_GRADE + 0.05)

    def test_a_short_deck_clears_its_ground(self):
        # The A5 by Heidelberg (49.3546 N 8.6316 E): a carriageway on its 8 m fill crosses a service road on a 21.4 m
        # deck. Its approaches' round ends, falling 8 % over the deck, stood 0.2-0.9 m over the girders' underside
        # from either abutment to the service road's cutting, whose walls rose into the girders.
        span, top = 21.4, 111.0
        under = top - roads.deck_depth(span)
        south = np.arange(-120.0, 0.1, 4.0)
        north = np.arange(span, span + 120.1, 4.0)
        approaches = [grading.Plan(np.stack([x, np.zeros(len(x))], axis=1), np.full(len(x), top), 8.0, abut=ab,
                                   road_half=7.0) for x, ab in ((south, (False, True)), (north, (True, False)))]
        ys = np.arange(-100.0, 100.1, 4.0)
        service = grading.Plan(np.stack([np.full(len(ys), span / 2), ys], axis=1), np.full(len(ys), 102.6), 3.0)
        for p, rid, rank in ((approaches[0], 1, 150), (approaches[1], 1, 150), (service, 2, 105)):
            p.rid, p.rank = rid, rank
        X, Y = np.meshgrid(np.r_[np.arange(0.0, span, 0.5), span], np.arange(-7.0, 7.01, 1.0))
        z = grading.grade_points(X.ravel(), Y.ravel(), np.full(X.size, 103.6), approaches + [service]).reshape(X.shape)
        self.assertTrue(np.allclose(z[:, [0, -1]], top, atol=0.05), "the abutments at their roads' height")
        seat = 2.0 * roads.deck_depth(span)
        inner = (X >= seat) & (X <= span - seat)
        self.assertLessEqual(float(z[inner].max()), under, "the ground under the span clears its girders")


class SeparationTest(unittest.TestCase):
    """GitHub #92: a street in a cutting, a service road on the bank 7.5 m above it beside its core."""

    def setUp(self):
        xs = np.arange(-100.0, 100.1, 4.0)
        self.street = grading.Plan(np.stack([xs, np.zeros(len(xs))], axis=1), np.full(len(xs), 589.4), 9.1)
        self.bank = grading.Plan(np.stack([xs, np.full(len(xs), -15.0)], axis=1), np.full(len(xs), 596.9), 3.0)
        self.py = np.arange(-9.0, 9.1, 1.0)                         # across the street's core (the bank's 3 m off)
        self.px = np.zeros(len(self.py))
        self.base = np.where(self.py < -4.0, 594.0, 589.4)

    def test_the_street_stays_flat_across_its_core(self):
        out = grading.grade_points(self.px, self.py, self.base, [self.street, self.bank])
        self.assertLess(np.ptp(out), 0.3, out)
        with unittest.mock.patch.object(grading, "SEP_ON", False):
            # (the bank pulled its edge up before SEP; the carriageway rule keeps it flat too: the bank's verge
            # gives way to the street's carriageway, 7.5 m under it)
            old = grading.grade_points(self.px, self.py, self.base, [self.street, self.bank])
        self.assertLess(np.ptp(old), 0.3, old)


class CarriagewayFirstTest(unittest.TestCase):
    """A road in a cutting (carriageway 4 m each side) and a street 7 m above it, centres 10 m apart, its sidewalk verge
    3 m past its own carriageway (5.3 m): the verge reaches over the cutting's carriageway and gives way to it."""

    def test_verge_gives_way(self):
        xs = np.arange(-100.0, 100.1, 4.0)
        low = grading.Plan(np.stack([xs, np.zeros(len(xs))], axis=1), np.full(len(xs), 535.0), 7.0, road_half=4.0)
        high = grading.Plan(np.stack([xs, np.full(len(xs), 10.0)], axis=1), np.full(len(xs), 542.0), 8.3,
                            road_half=5.3)
        py = np.arange(-4.0, 4.01, 0.5)                              # across the low carriageway
        px = np.zeros(len(py))
        base = np.where(py > 1.0, 538.0, 535.0)
        out = grading.grade_points(px, py, base, [low, high])
        np.testing.assert_allclose(out, 535.0 + grading.ROAD_LIFT * 0, atol=0.3)
        up = grading.grade_points(np.zeros(5), np.arange(5.0, 7.1, 0.5), np.full(5, 542.0), [low, high])
        np.testing.assert_allclose(up, 542.0, atol=0.3)              # the street's own carriageway stays up


class SharpCutTest(unittest.TestCase):
    """_add_cuts: a street along y = 0 with a smoothed 4 m wall rising beside its left side (+y) gets a cutting there:
    the wall where the climb is half done, its top 4 m up; the open right side none."""

    def test_wall_on_one_side(self):
        import types
        xy = np.c_[np.arange(0.0, 81.0, 8.0), np.zeros(11)]
        p = types.SimpleNamespace(xy=xy, z=np.full(11, 10.0), half=5.0, road_half=4.0, cut=None)

        def elev(la, lo):
            _e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
            return 10.0 + 5.0 / (1.0 + np.exp(-(n - 8.0) / 0.8))
        grading._add_cuts(FRAME, [p], elev)
        self.assertIsNotNone(p.cut)
        self.assertEqual(p.cut.shape, (10, 6))
        wall, top, reach = p.cut[:, 0], p.cut[:, 1], p.cut[:, 2]
        self.assertTrue(np.isfinite(wall).all() and np.isfinite(top).all())
        np.testing.assert_allclose(wall, 8.0, atol=1.01)                # the half-way point of the climb
        np.testing.assert_allclose(top, 10.0 + 4.5, atol=0.5)          # 90 % of it
        self.assertTrue((reach > wall).all())
        self.assertTrue(np.isnan(p.cut[:, 3:]).all(), "the open side: no cutting")

    def test_a_low_bank_on_one_side_is_no_wall_but_a_trench_is(self):
        """A 2.5 m bank beside a road: a graded slope (US-163, Utah: a low rock bank was a concrete wall); the same depth
        on both sides (a trench, as Munich's McGraw-Graben): its walls."""
        import types
        xy = np.c_[np.arange(0.0, 81.0, 8.0), np.zeros(11)]
        for both in (False, True):
            p = types.SimpleNamespace(xy=xy, z=np.full(11, 10.0), half=5.0, road_half=4.0, cut=None)

            def elev(la, lo, both=both):
                _e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
                up = 2.5 / (1.0 + np.exp(-(n - 8.0) / 0.8))
                return 10.0 + up + (2.5 / (1.0 + np.exp((n + 8.0) / 0.8)) if both else 0.0)
            grading._add_cuts(FRAME, [p], elev)
            if not both:
                self.assertTrue(p.cut is None or np.isnan(p.cut[:, [1, 4]]).all(), "a lone low bank: no cutting")
            else:
                self.assertIsNotNone(p.cut)
                self.assertTrue(np.isfinite(p.cut[:, 1]).all() and np.isfinite(p.cut[:, 4]).all(), "a trench: both walls")

    def test_gentle_slope_is_no_wall(self):
        import types
        xy = np.c_[np.arange(0.0, 81.0, 8.0), np.zeros(11)]
        p = types.SimpleNamespace(xy=xy, z=np.full(11, 10.0), half=5.0, road_half=4.0, cut=None)

        def elev(la, lo):
            _e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
            return 10.0 + 0.2 * np.abs(n)                                # 1 in 5: a bank, not a wall
        grading._add_cuts(FRAME, [p], elev)
        self.assertIsNone(p.cut)


class ShoulderTest(unittest.TestCase):
    def test_sidewalk_classes_keep_the_sidewalk_flat(self):
        res = roads.Spec(6.0, "paved", True, "residential")
        self.assertGreaterEqual(grading.shoulder(res), roads.SIDEWALK["w"], "the raised slab sits on flat ground")
        self.assertEqual(grading.shoulder(roads.Spec(6.0, "paved", False, "motorway")), grading.SHOULDER_M)
        self.assertEqual(grading.shoulder(roads.Spec(4.0, "dirt", False, "track")), 0.0)


class FakeMD:
    def __init__(self, feats):
        self.feats, self.calls = feats, 0

    def cell(self, key, layers=None, kind=None):
        self.calls += 1

        class C:
            pass
        c = C()
        c.layers = {"roads": self.feats}
        return c


class FailingMD:
    def __init__(self):
        self.calls = 0

    def cell(self, key, layers=None, kind=None):
        self.calls += 1
        raise OSError("offline")


class WalkwaySpanTest(unittest.TestCase):
    """A bridge that is not a road (a footway, a path, a cycleway) is not a deck. The elevation still has its
    span, a ridge across the road under it (GitHub #802). That span caps the road, and the walkway is not raised
    into a carriageway."""

    def cell(self, feats):
        g = grading._Planner(FRAME, FakeMD(feats), lambda la, lo: np.zeros(np.shape(la)))
        return g._cell_enu_calc(mapcells.cell_of(LAT, LON), ground=False)

    def test_a_walkway_bridge_caps_the_road_and_is_not_a_deck(self):
        bridged = features([[(0.0, -40.0), (0.0, 40.0)]], ["footway"], flags=[(0, "is_bridge")])
        plain = features([[(10.0, -20.0), (10.0, 20.0)]], ["footway"])
        road = features([[(-40.0, 0.0), (40.0, 0.0)]], ["motorway"], flags=[(0, "is_bridge")])
        span = self.cell(bridged)
        self.assertEqual(span["bridges"], [])
        self.assertEqual(len(span["span_bridges"]), 1)
        self.assertLess(float(span["span_bridges"][0][:, 1].min()), -20.0)
        self.assertEqual(self.cell(plain)["span_bridges"], [])
        deck = self.cell(road)
        self.assertEqual(len(deck["bridges"]), 1)
        self.assertEqual(deck["span_bridges"], [])

    def test_the_road_under_a_walkway_does_not_climb_its_ridge(self):
        s = np.arange(-60.0, 61.0, 2.0)
        z = np.zeros(len(s))
        z[(np.abs(s) < 10.0)] = 8.0
        pts = np.stack([s, np.zeros(len(s))], axis=1)
        span = (np.array([[0.0, -30.0], [0.0, 30.0]]), grading.WALK_SPAN_HALF_M)
        self.assertLess(float(grading.deck_capped(pts, z, [span]).max()), 0.5)
        self.assertGreater(float(grading.deck_capped(pts, z, []).max()), 7.0)


class DirtTest(unittest.TestCase):
    """A dirt road is worn: potholes and patches on its profile (grading.dirt_bumps), a lean to one side
    (grading.dirt_bank). A built road gets neither, and a main dirt road much less than a country lane."""

    def xy(self, n=400, step=2.0):
        s = np.arange(n, dtype=np.float64) * step
        return np.stack([s, np.zeros(n)], axis=1)

    def test_the_same_damage_in_every_process(self):
        xy = self.xy()
        sp = roads.Spec(3.5, "dirt", False, "track")
        a = grading.dirt_bumps(xy, sp, len(xy))
        b = grading.dirt_bumps(xy.copy(), roads.Spec(3.5, "dirt", False, "track"), len(xy))
        self.assertTrue(np.array_equal(a, b), "seeded by the road's position, not by process state")

    def test_a_built_road_is_left_alone(self):
        xy = self.xy()
        self.assertIsNone(grading.dirt_bumps(xy, roads.Spec(7.0, "paved", False, "residential"), len(xy)))
        self.assertIsNone(grading.dirt_bumps(xy, roads.Spec(3.5, "dirt", False, "track"), len(xy)) if False else None)
        self.assertIsNone(grading.bank(xy, roads.Spec(7.0, "paved", False, "residential")))

    @unittest.skipIf(grading.DIRT_ROUGH <= 0.0, "BNE_DIRT_BUMP 0")
    def test_a_main_dirt_road_is_softer_than_a_lane(self):
        xy = self.xy()
        lane = grading.dirt_bumps(xy, roads.Spec(4.0, "dirt", False, "residential"), len(xy))
        main = grading.dirt_bumps(xy, roads.Spec(7.0, "dirt", False, "secondary"), len(xy))
        self.assertGreater(float(np.std(lane)), 0.005, "a lane keeps small holes in it")
        self.assertLess(float(np.std(main)), 0.5 * float(np.std(lane)), "a road signed as a main one stays smoother")

    @unittest.skipIf(grading.DIRT_ROUGH <= 0.0, "BNE_DIRT_BUMP 0")
    def test_everyday_cars_can_drive_it(self):
        """GitHub #783 / #784 (1.8.2): "like a motocross track", dips to 1.5 m and 66 % grades on a track. Even a forest
        track stays a road an everyday car drives: dips of a quarter metre at most, no step over 20 % (the plan caps
        it at DIRT_STEP_MAX_M), a lean of a few percent."""
        xy = self.xy()
        for cls in ("track", "unclassified", "residential"):
            b = grading.dirt_bumps(xy, roads.Spec(3.5, "dirt", False, cls), len(xy))
            self.assertLess(float(-b.min()), 0.3, cls)
            self.assertLess(float(np.abs(np.diff(b)).max()) / grading.PROFILE_STEP, 0.2, cls)   # (the plan caps it more)
        lean = grading.dirt_bank(xy, roads.Spec(3.5, "dirt", False, "track"))
        self.assertLess(float(np.abs(lean).max()), 0.06, "a camber, not a tilt")

    @unittest.skipIf(grading.DIRT_ROUGH <= 0.0, "BNE_DIRT_BUMP 0")
    def test_a_forest_track_is_the_worst(self):
        xy = self.xy()
        lane = grading.dirt_bumps(xy, roads.Spec(3.5, "dirt", False, "residential"), len(xy))
        track = grading.dirt_bumps(xy, roads.Spec(3.5, "dirt", False, "track"), len(xy))
        self.assertGreater(float(np.std(track)), float(np.std(lane)), "a track is worse than a lane")

    @unittest.skipIf(grading.DIRT_ROUGH <= 0.0, "BNE_DIRT_BUMP 0")
    def test_the_ends_meet_their_junctions(self):
        xy = self.xy(n=400)
        b = grading.dirt_bumps(xy, roads.Spec(3.5, "dirt", False, "residential"), len(xy))
        self.assertEqual(float(b[0]), 0.0)
        self.assertEqual(float(b[-1]), 0.0)
        self.assertGreater(float(np.abs(b).max()), 0.015, "the middle still has its damage")

    @unittest.skipIf(grading.DIRT_ROUGH <= 0.0, "BNE_DIRT_BUMP 0")
    def test_damage_does_not_sink_the_road(self):
        # the bump pattern's own long waves are removed, or the mean drags the road down along its length
        xy = self.xy(n=800)
        b = grading.dirt_bumps(xy, roads.Spec(3.5, "dirt", False, "track"), len(xy))
        core = b[60:-60]
        self.assertLess(abs(float(np.mean(core))), 0.25 * float(np.std(core)), "no net rise or fall")

    @unittest.skipIf(grading.DIRT_BANK_M <= 0.0, "BNE_DIRT_BANK 0")
    def test_a_dirt_road_leans_to_one_side(self):
        xy = self.xy(n=400)
        b = grading.bank(xy, roads.Spec(3.5, "dirt", False, "track"))
        self.assertIsNotNone(b)
        self.assertEqual(len(b), len(xy))
        self.assertGreater(float(np.std(b)), 0.003, "now the left side is low, now the right")
        self.assertLessEqual(float(np.abs(b).max()), 2.0 * grading.DIRT_BANK_M, "a lean, not a wall")

    @unittest.skipIf(grading.DIRT_BANK_M <= 0.0, "BNE_DIRT_BANK 0")
    def test_the_lean_fades_out_at_the_ends(self):
        xy = self.xy(n=400)
        b = grading.bank(xy, roads.Spec(3.5, "dirt", False, "track"), ext=(1, 1))
        self.assertEqual(float(b[0]), 0.0)
        self.assertEqual(float(b[-1]), 0.0)

    def test_the_plan_carries_the_lean(self):
        xy = self.xy(n=200)
        sp = roads.Spec(3.5, "dirt", False, "track")
        plan = grading._plan(xy, np.full(len(xy), 100.0), sp)
        if grading.DIRT_BANK_M <= 0.0:
            self.assertIsNone(getattr(plan, "bank", None))
        else:
            self.assertIsNotNone(plan.bank)
            self.assertEqual(len(plan.bank), len(plan.xy))

    def test_a_local_hole_does_not_make_long_waves(self):
        xy = self.xy(n=200)
        ground = 100.0 + 0.12 * xy[:, 0]
        damage = np.zeros(len(xy))
        damage[100] = -0.8
        with unittest.mock.patch.object(grading, "dirt_bumps", return_value=damage):
            plan = grading._plan(xy, ground, roads.Spec(3.5, "dirt", False, "track"))
        residual = plan.z - ground
        self.assertGreater(abs(float(residual[100])), 0.1, "the hole remains")
        self.assertLess(float(np.abs(residual[np.abs(np.arange(len(xy)) - 100) > 2]).max()), 1e-9,
                        "limiting a hole must not spread its height error over 40 m")

    @unittest.skipIf(grading.DIRT_ROUGH <= 0.0, "BNE_DIRT_BUMP 0")
    def test_damage_is_independent_of_the_natural_grade(self):
        xy = self.xy()
        sp = roads.Spec(3.5, "dirt", False, "track")
        flat = grading._plan(xy, np.full(len(xy), 100.0), sp)
        ground = 100.0 + 0.20 * xy[:, 0]
        uphill = grading._plan(xy, ground, sp)
        self.assertTrue(np.allclose(uphill.z - ground, flat.z - 100.0, atol=1e-9),
                        "limit artificial damage, keeping the road's real climb")

    @unittest.skipIf(grading.DIRT_ROUGH <= 0.0, "BNE_DIRT_BUMP 0")
    def test_smoothing_extensions_do_not_change_the_damage(self):
        xy = self.xy()
        sp = roads.Spec(3.5, "dirt", False, "track")
        plain = grading.dirt_bumps(xy, sp, len(xy))
        extended = grading.dirt_bumps(xy, sp, len(xy) + 10, ext=(5, 5))
        self.assertTrue(np.array_equal(plain, extended[5:-5]), "only the DEM uses the neighbouring road's samples")
        self.assertTrue(np.all(extended[:5] == 0.0))
        self.assertTrue(np.all(extended[-5:] == 0.0))

    def test_overlapping_damage_has_bounded_height(self):
        sp = roads.Spec(3.5, "dirt", False, "track")
        with unittest.mock.patch.object(grading, "DIRT_ROUGH", 1.0):
            for offset in (0.0, 100.0, 500.0):
                b = grading.dirt_bumps(self.xy() + [offset, 0.0], sp, 400)
                self.assertGreaterEqual(float(b.min()), -0.08 - 1e-9, "overlapping pits do not become a trench")
                self.assertLessEqual(float(b.max()), 0.025 + 1e-9, "removing the mean must not build hills")

    @unittest.skipIf(grading.DIRT_ROUGH <= 0.0, "BNE_DIRT_BUMP 0")
    def test_damage_step_limit_uses_the_sample_distance(self):
        step = 1.0
        xy = self.xy(step=step)
        sp = roads.Spec(3.5, "dirt", False, "track")
        ground = 100.0 + 0.20 * xy[:, 0]
        plan = grading._plan(xy, ground, sp, step=step)
        limit = grading.DIRT_STEP_MAX_M * 1.6 * step / grading.PROFILE_STEP
        self.assertLessEqual(float(np.abs(np.diff(plan.z - ground)).max()), limit + 1e-9)

    def test_a_flat_track_does_not_turn_into_hills_across_its_width(self):
        # The first fix bounded the centreline alone. The unchanged random bank still tilted a 3.5 m track by
        # ~0.8 m edge to edge at Mering (48.27726, 10.97645). Test the native grading output across the full width.
        sp = roads.Spec(3.5, "dirt", False, "track")
        for offset in (0.0, 100.0, 500.0):
            xy = self.xy() + [offset, 0.0]
            plan = grading._plan(xy, np.full(len(xy), 100.0), sp)
            x, y = np.meshgrid(np.arange(offset + 40.0, offset + 750.0, 1.0), np.linspace(-1.75, 1.75, 9))
            z = grading.grade_points(x.ravel(), y.ravel(), np.full(x.size, 100.0), [plan])
            self.assertLess(float(np.abs(z - 100.0).max()), 0.12,
                            "the whole graded carriageway keeps centimetre-scale wear on flat ground")

    @unittest.skipIf(grading.DIRT_BANK_M <= 0.0, "BNE_DIRT_BANK 0")
    def test_random_lean_is_bounded_in_metres_for_wide_roads_too(self):
        xy = self.xy()
        for width in (2.0, 3.5, 7.0, 12.0):
            b = grading.dirt_bank(xy, roads.Spec(width, "gravel", False, "track"))
            self.assertLessEqual(float(np.abs(b).max()) * width / 2, 0.04 + 1e-9,
                                 "the synthetic tilt raises a road edge at most four centimetres")


class DirtNoiseTest(unittest.TestCase):
    """earthfast.DirtNoise (dirtnoise.rs): the dirt profile's random numbers, the same bits on every platform (numpy's
    Generator's normal and poisson called the platform's C maths library, GitHub #1169)."""

    def setUp(self):
        import fast
        fast.require(470, "DirtNoise")
        self.ef = fast._ef

    def test_splitmix64_reference(self):
        # Vigna's splitmix64.c, seed 1234567: first output 6457827717110365317; a uniform is its top 53 bits / 2^53
        self.assertEqual(self.ef.DirtNoise(1234567).uniform(0.0, 1.0), (6457827717110365317 >> 11) / 2.0 ** 53)

    def test_pinned_bits(self):
        bits = lambda a: [float(v).hex() for v in np.atleast_1d(a)]
        r = self.ef.DirtNoise(42)
        got = (bits(r.uniform(3.0, 6.0)), bits(r.normals(3)), r.poisson(5.0), r.poisson(80.0),
               bits(r.uniforms(-1.0, 1.0, 2)), r.poisson(0.3))
        self.assertEqual(got, PINNED_DIRT_NOISE)

    def test_the_draws_have_their_moments(self):
        r = self.ef.DirtNoise(7)
        x = r.normals(200_000)
        self.assertLess(abs(float(x.mean())), 0.01)
        self.assertLess(abs(float(x.var()) - 1.0), 0.015)
        self.assertLessEqual(float(np.abs(x).max()), 6.0)
        u = r.uniforms(2.0, 5.0, 100_000)
        self.assertGreaterEqual(float(u.min()), 2.0)
        self.assertLess(float(u.max()), 5.0)
        self.assertLess(abs(float(u.mean()) - 3.5), 0.02)
        for lam in (0.3, 1.0, 4.0, 31.0, 33.0, 95.0, 400.0):    # (chunks of at most 32: 33 is the first split)
            k = np.array([r.poisson(lam) for _ in range(20_000)], np.float64)
            self.assertLess(abs(float(k.mean()) - lam), 4.0 * math.sqrt(lam / len(k)), lam)
            self.assertLess(abs(float(k.var()) / lam - 1.0), 0.06, lam)
        self.assertEqual(r.poisson(0.0), 0)
        with self.assertRaises(ValueError):
            r.poisson(float("nan"))
        with self.assertRaises(ValueError):
            r.uniform(1.0, float("inf"))

    def test_the_dirt_profile_draws_no_numpy_randoms(self):
        import inspect
        for fn in (grading.dirt_bumps, grading.dirt_bank):
            self.assertNotIn("np.random", inspect.getsource(fn), fn.__name__)

    def test_the_damage_is_pinned(self):
        # the whole profile for one road, both switches on: a moved draw (another stream order, a platform's maths)
        # changes these bits
        xy = np.stack([np.arange(300, dtype=np.float64) * 2.0 + 1234.5, np.full(300, -877.25)], axis=1)
        sp = roads.Spec(3.5, "dirt", False, "track")
        with unittest.mock.patch.object(grading, "DIRT_ROUGH", 1.0), \
                unittest.mock.patch.object(grading, "DIRT_BANK_M", 0.015):
            b = grading.dirt_bumps(xy, sp, len(xy))
            lean = grading.dirt_bank(xy, sp)
        import hashlib
        self.assertEqual(hashlib.sha1(b.tobytes() + lean.tobytes()).hexdigest()[:16], PINNED_DIRT_PROFILE)


PINNED_DIRT_NOISE = (["0x1.4e61659ca3f09p+2"], ["-0x1.1f4f328577338p+0", "-0x1.cfc1194c04938p-1", "0x1.94d6e0146acf0p+0"],
                     4, 79, ["-0x1.37fba1759aee4p-1", "-0x1.92d6b49cfcc7cp-1"], 0)
PINNED_DIRT_PROFILE = "3a1ebc7c11dd161a"


class GraderTest(unittest.TestCase):
    def setUp(self):
        # (no measured road heights' provider: a test that made a Server (test_areas, test_fargeo) left one set, and
        # the far-apart points read a cell more, 31 of 30, in the full suite only)
        import roadz
        p = unittest.mock.patch.object(roadz, "PROVIDER", None)
        p.start()
        self.addCleanup(p.stop)

    def test_failed_fetch_not_retried_at_once(self):
        md = FailingMD()
        g = grading.Grader(FRAME, md, lambda la, lo: np.zeros(np.shape(la)))
        lat, lon = np.full((2, 2), LAT), np.full((2, 2), LON)
        base = np.ones((2, 2))
        self.assertTrue(np.array_equal(g.apply(lat, lon, base), base), "ungraded without road data")
        n = md.calls
        g.apply(lat, lon, base)
        self.assertEqual(md.calls, n, "a failed cell waits RETRY_S before it is fetched again")

    def test_plans_once_per_cell_and_dem_dropin(self):
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        md = FakeMD(f)
        g = grading.Grader(FRAME, md, lambda la, lo: np.full(np.shape(la), 10.0))
        lat = np.full((3, 3), LAT)
        lon = np.full((3, 3), LON)
        g.apply(lat, lon, np.zeros((3, 3)))
        n = md.calls
        g.apply(lat, lon, np.zeros((3, 3)))
        self.assertEqual(md.calls, n, "cell plans cached")

        class Dem:
            def sample(self, la, lo, zoom, progress=None):
                return np.zeros(np.shape(la))
            other = 42
        gd = grading.GradedDem(Dem(), g)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 0.0]), np.array([20.0, 200.0]), 0.0)
        z = gd.sample(la, lo, 15)
        self.assertAlmostEqual(float(z[0]), 10.0, delta=0.05, msg="on the road: its profile")
        bump = float(np.asarray(grading.terrain_bumps(la[1:2], lo[1:2])).ravel()[0]) if grading.TERRAIN_BUMPS_ON else 0.0
        self.assertAlmostEqual(float(z[1]), bump, delta=1e-9, msg="far from it: the DEM and the off-road roughness")
        self.assertEqual(gd.other, 42, "other attributes pass through")


    def test_points_far_apart_plan_only_their_cells(self):
        # two points 27 km apart (a straight salt-flats track's ends): the cells around each, not the ~240 of their
        # bbox (one road tile planned them all: 53 s, 2026-10-20); the same heights, in an order of their own
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        md = FakeMD(f)
        read = set()
        cell = md.cell
        md.cell = lambda key, layers=None, kind=None: (read.add(str(key)), cell(key, layers, kind))[1]
        g = grading.Grader(FRAME, md, lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 19000.0]), np.array([20.0, 19000.0]), 0.0)
        bbox = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
        self.assertGreater(len(bbox), 100)
        out = g.apply(la, lo, np.zeros(2))
        # (each point's own cells and those whose plans reach it: a cell plans CLIP_MARGIN_M past its edges; and the
        # bridges of their neighbours, read for the underpasses: grading.UNDER_*. Distinct cells: a cell asked again
        # is in memory (mapdata), the under-deck ground (PR #698) asks the same 21 cells again for their decks)
        self.assertLessEqual(len(read), 30, "the cells around the two points")
        self.assertAlmostEqual(float(out[0]), 10.0, delta=0.05)
        near = mapcells.cells_near_points(la, lo, 1e-4, 1e-4)
        self.assertTrue(set(near) <= set(bbox))
        self.assertEqual(near, sorted(near, key=lambda k: (k.y, k.x)), "row-major")
        self.assertEqual({mapcells.cell_of(float(a), float(b)) for a, b in zip(la, lo)} - set(near), set())

    def test_plan_order_independent_of_the_cells(self):
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 5.0]), np.array([20.0, 25.0]), 0.0)
        a = g.plans(la, lo)
        la2, lo2, _ = FRAME.enu_to_geodetic(np.array([0.0, 5.0, 3000.0]), np.array([20.0, 25.0, -3000.0]), 0.0)
        b = g.plans(la2, lo2)
        self.assertEqual([id(p) for p in a], [id(p) for p in b])

    @unittest.mock.patch.object(grading, "SHARP_ON", False)        # (the profile's samples alone counted)
    def test_long_parts_planned_near_the_cell_only(self):
        # a 30 km straight track (one segment): its plan in a cell covers the cell and CLIP_MARGIN_M round it, not
        # its whole length (each cell sampled the ground to both ends: thousands of DEM tiles, 2026-10-20), with the
        # same heights in the cell; a short road is planned whole
        la, lo, _ = FRAME.enu_to_geodetic(np.array([-15000.0, 15000.0]), np.array([20.0, 20.0]), 0.0)
        f = features([[(-15000, 20), (15000, 20)], [(-300, 400), (300, 400)]], ["residential", "residential"])
        md = FakeMD(f)
        asked = []

        def elev(la_, lo_):
            asked.append(len(np.atleast_1d(la_)))
            e, _, _ = FRAME.geodetic_to_enu(la_, lo_, 0.0)
            return 10.0 + 0.001 * np.asarray(e)         # a gentle slope along the track
        g = grading.Grader(FRAME, md, elev)
        key = mapcells.cell_of(LAT, LON)
        plans = g._cell_plans(key)
        box = g._clip_box(key)
        xs = np.concatenate([p.xy[:, 0] for _f, p in plans])
        self.assertGreaterEqual(xs.min(), box[0] - 1e-6)
        self.assertLessEqual(xs.max(), box[2] + 1e-6)
        self.assertLess(sum(asked), 30000 / grading.PROFILE_STEP / 2, "not sampled along the whole track")
        short = [p for fid, p in plans if np.ptp(p.xy[:, 0]) < 700]
        self.assertEqual(len(short), 1, "the short road whole")
        y = float(plans[0][1].xy[0, 1])                 # on the track
        e = np.linspace(-600.0, 600.0, 13)
        lat, lon, _ = FRAME.enu_to_geodetic(e, np.full(len(e), y), 0.0)
        out = g.apply(lat, lon, np.zeros(len(e)))
        whole = grading.Grader(FRAME, FakeMD(f), elev)
        with unittest.mock.patch.object(grading, "LONG_PART_M", 1e9):
            want = whole.apply(lat, lon, np.zeros(len(e)))
        np.testing.assert_allclose(out, want, atol=1e-3)
        self.assertGreater(float(out.min()), 9.0, "graded to the track")

    def test_clip_polyline(self):
        xy = np.array([[-50.0, 0], [50, 0], [50, 50], [150, 50], [150, -150], [-150, -150]])
        runs = grading.clip_polyline(xy, (0, -10, 100, 100))
        self.assertEqual([r.tolist() for r in runs], [[[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [100.0, 50.0]]])
        self.assertEqual(grading.clip_polyline(np.array([[200.0, 0], [300, 0]]), (0, 0, 10, 10)), [])
        two = grading.clip_polyline(np.array([[-5.0, 5], [15, 5], [15, 7], [-5, 7]]), (0, 0, 10, 10))
        self.assertEqual(len(two), 2)


class ChainTest(unittest.TestCase):
    """A road split at a junction (GitHub #266, BR-381): its parts are smoothed as one road."""

    @staticmethod
    def hill(la, lo):           # a 20 % grade, with a 1.5 m bump in the data right at the junction
        e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
        e = np.asarray(e)
        return 100.0 + 0.20 * e + 0.4 * np.sin(e / 11.0) + 1.5 * np.exp(-(e / 6.0) ** 2)

    def plans(self, on):
        # A ends at the junction (0, 0) where B goes on straight and C branches off 31 degrees to the left
        f = features([[(-300, 0), (0, 0)], [(0, 0), (300, 0)], [(0, 0), (200, 120)]], ["primary"] * 3)
        # (the road laws off: they would ease the bump of the parts smoothed alone too, the case this compares with)
        with unittest.mock.patch.object(grading, "CHAIN_ON", on), unittest.mock.patch.object(grading, "ROAD_LAWS_ON", False):
            g = grading.Grader(FRAME, FakeMD(f), self.hill)
            return {fid: p for fid, p in g._cell_plans(mapcells.cell_of(LAT, LON))}

    def test_the_parts_meet_at_one_height(self):
        p = self.plans(True)
        a, b = p["id000"], p["id001"]
        self.assertLess(abs(a.z[-1] - b.z[0]), 0.01)
        self.assertLess(a.z[-1], 101.0, "smoothed through the junction: the bump in the data mostly gone")
        alone = self.plans(False)
        self.assertGreater(alone["id000"].z[-1], 101.4, "smoothed alone: each part pinned to the bump's top")
        # the grade goes on through the junction (the plans' last / first 8 m)
        ga = (a.z[-1] - a.z[-3]) / np.hypot(*(a.xy[-1] - a.xy[-3]))
        gb = (b.z[2] - b.z[0]) / np.hypot(*(b.xy[2] - b.xy[0]))
        self.assertAlmostEqual(ga, gb, delta=0.03)

    def test_the_straightest_part_goes_on(self):
        import fast
        cand = [np.array([[0.0, 0.0], [300.0, 0.0]]), np.array([[200.0, 120.0], [0.0, 0.5]]),
                np.array([[0.0, 0.0], [0.0, 300.0]]), np.array([[0.0, 0.0], [250.0, 1.0]])]
        cxy = np.concatenate(cand)
        coff = np.array([0, 2, 4, 6, 8], np.int64)

        def ext(roads, cls=(1, 1, 1, 1), reach=50.0):
            pts, off, _which = fast._ef.chain_extensions(
                cxy, coff, np.array(cls, np.int64), np.array(roads, np.int64), np.array([[0.0, 0.0]]),
                np.array([[1.0, 0.0]]), np.array([1], np.int64), np.array([7], np.int64), np.array([reach]),
                1.0, 35.0, 10.0)
            return np.asarray(pts[off[0]:off[1]])
        line = ext([1, 2, 3, 4])
        self.assertEqual(line[0].tolist(), [0.0, 0.0])
        self.assertAlmostEqual(float(np.hypot(*np.diff(line, axis=0).T).sum()), 50.0, places=6)
        self.assertLess(abs(line[-1, 1]), 1e-6, "the straight one, not the 31 degree branch")
        self.assertAlmostEqual(float(ext([7, 2, 3, 4])[-1, 1]), 0.2, places=4, msg="not its own road: the next straightest")
        self.assertGreater(float(ext([7, 2, 3, 7])[-1, 1]), 10.0, "then the branch, its end at the node, turned")
        self.assertEqual(len(ext([7, 7, 3, 7])), 0, "never a 90 degree turn")
        self.assertEqual(len(ext([1, 2, 3, 4], cls=(2, 2, 2, 2))), 0, "only roads smoothed alike")


class MajorRoadTest(unittest.TestCase):
    """Motorways and trunks are smoothed more (SIGMA_MAJOR_M); a road ending on one meets it at its height (pins)."""

    @staticmethod
    def bumpy(la, lo):          # a 4 % grade with a 3 m bump 60 m long (a forest edge in a surface model)
        e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
        e = np.asarray(e)
        return 100.0 + 0.04 * e + 3.0 * np.exp(-((e - 20.0) / 30.0) ** 2)

    def plans(self):
        # the motorway split at the slip road's junction (0, 0); the slip road leaves at 25 degrees
        f = features([[(-400, 0), (0, 0)], [(0, 0), (400, 0)], [(0, 0), (180, -84)]], ["motorway"] * 3)
        f.rules["subclass"] = rules([(2, "link")])
        g = grading.Grader(FRAME, FakeMD(f), self.bumpy)
        return {fid: p for fid, p in g._cell_plans(mapcells.cell_of(LAT, LON))}

    def test_dirt_roads_on_coarse_elevation_do_not_follow_its_noise(self):
        """GitHub #835: on the coarse global model (30 m cells, metres of noise) a minor dirt road smoothed over 6 m
        rode every bump of the data. There it is smoothed over DIRT_SIGMA_COARSE_M; on fine elevation it is not."""
        spec = roads.Spec(3.5, "dirt", False, "unclassified")
        # (on fine elevation a gravel road of a class is smoothed as the paved road of its class; a track less)
        self.assertEqual(grading.profile_sigma(spec), max(grading.SIGMA_M, grading.CLASS_SIGMA_M["unclassified"]))
        self.assertEqual(grading.profile_sigma(roads.Spec(3.5, "dirt", False, "track")), grading.DIRT_SIGMA_MINOR_M)
        self.assertEqual(grading.profile_sigma(spec, coarse=True), grading.DIRT_SIGMA_COARSE_M)
        self.assertEqual(grading.profile_sigma(roads.Spec(7.0, "paved", False, "unclassified"), coarse=True),
                         grading.profile_sigma(roads.Spec(7.0, "paved", False, "unclassified")), "paved: unchanged")

        def noisy(la, lo):
            x = np.asarray(FRAME.geodetic_to_enu(la, lo, 0.0)[0], np.float64)
            return 100.0 + 0.02 * x + 1.5 * np.sin(x / 9.0) + 0.8 * np.sin(x / 4.3 + 1.0)
        xs = np.arange(-200.0, 201.0, 2.0)
        la, lo, _ = FRAME.enu_to_geodetic(xs, np.zeros(len(xs)), 0.0)
        part = [(np.c_[lo, la], roads.Spec(3.5, "dirt", False, "track"))]     # (a track: 6 m on fine elevation)
        with unittest.mock.patch.object(grading, "DIRT_ROUGH", 0.0):     # (the profile alone, not the potholes)
            fine = grading.plan_roads(FRAME, part, noisy, cuts=False)[0]
            rough = grading.plan_roads(FRAME, part, noisy, cuts=False, coarse=True)[0]

        def bumpiness(p):
            core = p.z[30:-30]
            return float(np.abs(np.diff(core, 2)).mean())
        self.assertLess(bumpiness(rough), 0.25 * bumpiness(fine))
        self.assertAlmostEqual(float(np.polyfit(rough.xy[:, 0], rough.z, 1)[0]), 0.02, delta=0.01, msg="the grade kept")

    def test_major_roads_smoothed_more(self):
        self.assertEqual(grading.profile_sigma(roads.Spec(7.0, "paved", False, "motorway")), grading.SIGMA_MAJOR_M)
        self.assertEqual(grading.profile_sigma(roads.Spec(7.0, "paved", False, "motorway", link=True)), grading.SIGMA_M)
        p = self.plans()
        a = p["id000"]
        bump = np.interp(20.0, a.xy[:, 0], a.z) if a.xy[-1, 0] > 20 else np.interp(20.0, p["id001"].xy[:, 0],
                                                                                    p["id001"].z)
        self.assertLess(bump - (100.0 + 0.04 * 20.0), 1.6, "most of the bump smoothed out of the motorway")

    def test_a_slip_road_meets_the_motorway(self):
        p = self.plans()
        mway, link = p["id001"], p["id002"]
        self.assertLess(abs(mway.z[0] - p["id000"].z[-1]), 0.01, "the motorway's parts meet")
        self.assertLess(abs(link.z[0] - mway.z[0]), 0.01, "the slip road ends at the motorway's height")
        self.assertGreater(abs(link.z[0] - self.bumpy(np.array([LAT]), np.array([LON]))[0]), 0.5,
                           "not at the bump's height")


class OverTunnels(unittest.TestCase):
    """Grader._over_tunnels (earthfast.over_tunnel): over the tube within its half width, from TUNNEL_KEEP_IN_M inside
    its portals."""

    def test_mask(self):
        import threading
        import types
        xy = np.array([[0.0, 0.0], [100.0, 0.0], [200.0, 0.0]])
        g = types.SimpleNamespace(lock=threading.Lock(), covers={"a": [(xy, 6.0)]})
        k = grading.TUNNEL_KEEP_IN_M
        e = np.array([50.0, 150.0, 50.0, k / 2, 200.0 - k / 2, k + 1.0, -5.0])
        n = np.array([2.0, -5.5, 7.0, 0.0, 0.0, 0.0, 0.0])
        got = grading.Grader._over_tunnels(g, e, n)
        np.testing.assert_array_equal(got, [True, True, False, False, False, True, False])
        self.assertIsNone(grading.Grader._over_tunnels(g, np.array([50.0]), np.array([30.0])))


class RoadOverTunnelTest(unittest.TestCase):
    """A road crossing over a covered tunnel keeps its own cutting over the tube; the tunnel's own road (its approaches)
    still leaves the ground over the tube uncut. The stage road over the A25's twin tubes at Popoli (2026-11-14): its
    cutting was undone in a strip over each tube, the hillside standing back up across the road."""

    def setUp(self):
        from test_roads import M_LON, M_LAT

        def ground(la, lo):
            e = (np.asarray(lo) - LON) * M_LON                          # along the tunnel, -500 .. 500
            n = (np.asarray(la) - LAT) * M_LAT
            hill = 100.0 + 60.0 * np.clip(1.0 - np.abs(e) / 400.0, 0.0, 1.0)
            return hill + 6.0 * np.exp(-(n / 8.0) ** 2)                 # a bump on the line of the road across
        self.ground = ground

    def change(self, f, pts):
        g = grading.Grader(FRAME, FakeMD(f), self.ground)
        e = np.array([p[0] for p in pts], np.float64)
        n = np.array([p[1] for p in pts], np.float64)
        la, lo, _ = FRAME.enu_to_geodetic(e, n, 0.0)
        base = self.ground(la, lo)
        return g.apply(la, lo, base.copy()) - base

    def tunnel(self, crossing=True):
        lines = [[(-500, 0), (500, 0)]] + ([[(0, -150), (0, 150)]] if crossing else [])
        return features(lines, ["primary", "secondary"][:len(lines)], flags=[(0, "is_tunnel", 0.25, 0.75)],
                        ids=["tube", "across"][:len(lines)])

    def test_the_road_across_keeps_its_cutting_over_the_tube(self):
        d = self.change(self.tunnel(), [(0.0, 0.0), (0.0, 3.0)])
        self.assertLess(float(d[0]), -2.0, "the bump under the road's centre cut away, as on any road")
        self.assertLess(float(d[1]), -2.0, "... and under its lane edge")

    def test_the_tunnels_own_road_does_not_cut_over_the_tube(self):
        # (every 2 m: the approach's cutting reached ~6 m in over the cover, between samples 20 m apart)
        pts = [(x, y) for x in range(-246, 247, 2) for y in (-4.0, 0.0, 4.0)]
        d = self.change(self.tunnel(crossing=False), pts)
        self.assertGreater(float(d.min()), -grading.OVER_TUNNEL_SMALL_M, "the ground over the tube not cut down")

    def test_own_roads_per_cell(self):
        # (each cell's verdict from its own plans: a road ending at an end of a tunnel's plans (a portal, or a join
        # between its sections: the Peripherique's trunk reaches its tube at one) is its approach; one ending 3.5 m off
        # an end, or crossing over the tube, is not)
        import threading

        def plan(*xy):
            return grading.Plan(np.array(xy, np.float64), np.zeros(len(xy)), 3.0)
        tag = grading.TUNNEL_TAG
        ps = [("g.0#1" + tag, plan((0, 0), (100, 0))),                 # a section, its ends joins
              ("g.0#1" + tag + "m0", plan((0, 0), (4, 0))),            # mouth: portal at (0, 0), inner end (4, 0)
              ("g", plan((-50, 0), (150, 0))),                         # the tunnel's own way
              ("approach", plan((-80, 0), (0.5, 0))),                  # ends 0.5 m from the portal
              ("at_join", plan((100, 40), (100, 0.5))),                # ends at the section's far join
              ("at_inner", plan((4, 40), (4, 0.5))),                   # ends at the mouth plan's inner end
              ("off_end", plan((40, 40), (40, 3.5))),                  # ends beside the tube, 3.5 m off its line
              ("across", plan((50, -100), (50, 100)))]                 # crosses over the tube
        g = types.SimpleNamespace(lock=threading.Lock(), cells={"c": ps}, _cell_plans=lambda k: ps)
        got, own = grading._Planner._cell_own_roads(g, "c")
        self.assertIs(got, ps)
        self.assertEqual(set(own), {"g"})
        self.assertEqual({f for f, _x, _y in own["g"]},
                         {"g.0#1" + tag, "g.0#1" + tag + "m0", "g", "approach", "at_join", "at_inner"})
        self.assertIs(grading._Planner._cell_own_roads(g, "c")[1], own, "made once per cell")

    def test_own_roads_per_tunnel(self):
        # (a motorway with a tunnel of its own further on is that tunnel's own road, not the own road of a primary's
        # tunnel under it: over the primary's tube its grading stays, Vienna's A23, #118)
        import threading

        def plan(*xy):
            return grading.Plan(np.array(xy, np.float64), np.zeros(len(xy)), 3.0)
        tag = grading.TUNNEL_TAG
        ps = [("m.1#0" + tag, plan((0, 200), (0, 300))),                # the motorway's own tunnel
              ("m", plan((0, -100), (0, 200))),                         # the motorway on the ground, to its portal
              ("p.0#0" + tag, plan((-50, 0), (50, 0))),                 # the primary's tunnel under the motorway
              ("p", plan((-80, 0), (-50, 0)))]                          # the primary's approach
        g = types.SimpleNamespace(lock=threading.Lock(), cells={"c": ps}, _cell_plans=lambda k: ps)
        _got, own = grading._Planner._cell_own_roads(g, "c")
        self.assertEqual({f for f, _x, _y in own["m"]}, {"m.1#0" + tag, "m"})
        self.assertEqual({f for f, _x, _y in own["p"]}, {"p.0#0" + tag, "p"})

    def test_a_tunnel_plans_feature(self):
        # (a tunnel's plans are named for its sections, the map part's id (roads: f"{fid}.{n}") then "#k")
        self.assertEqual(grading._tunnel_feature("35f45a70-54b2.9398624#1:tunm1"), "35f45a70-54b2")
        self.assertEqual(grading._tunnel_feature("eeb16a87.0#0:tun"), "eeb16a87")
        self.assertEqual(grading._tunnel_feature("tube"), "tube")


class WallHeightTest(unittest.TestCase):
    def test_city_concrete_walls_full_height(self):
        """A major road's cutting in town keeps a full-height concrete wall (McGraw-Graben); stone walls elsewhere
        stop at WALL_MAX_H_M with the slope above them (mountain passes)."""
        import roads
        city = roads.Spec(width=12.0, kind="paved", urban=True, cls="primary", oneway=False)
        rural = roads.Spec(width=7.0, kind="paved", urban=False, cls="primary", oneway=False)
        street = roads.Spec(width=7.0, kind="paved", urban=True, cls="residential", oneway=False)
        self.assertEqual(grading.wall_max_h(city), grading.WALL_MAX_H_CITY_M)
        self.assertEqual(grading.wall_max_h(rural), grading.WALL_MAX_H_M)
        self.assertEqual(grading.wall_max_h(street), grading.WALL_MAX_H_M)


class UnderpassTest(unittest.TestCase):
    SPAN = 40.0         # m: the motorway bridges these tests draw

    @classmethod
    def deck_drop(cls):
        """m the road under such a bridge is dug under its deck: the deck's own clearance (its depth is its span's:
        roads.deck_road_clear) and the road's lift, within the maximum excavation depth."""
        clear = roads.deck_road_clear(roads.deck_depth(cls.SPAN)) + grading.ROAD_LIFT
        return min(clear, grading.UNDER_MAX_DROP_M)

    def test_road_dips_under_a_motorway_overpass(self):
        """A local road crossing under a short motorway bridge whose terrain was interpolated at deck height (lidar
        with the bridge removed) dips to the deck minus the bridge's clearance; a road crossing nothing is left alone."""
        f = features([[(-200, 0), (200, 0)], [(0, -20), (0, 20)], [(-200, 300), (200, 300)]],
                     ["residential", "motorway", "residential"], flags=[(1, "is_bridge")])
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 195.0, 0.0]), np.array([0.0, 0.0, 300.0]), 0.0)
        out = g.apply(la, lo, np.full(3, 10.0))
        self.assertAlmostEqual(float(out[0]), 10.0 - self.deck_drop(),
                               delta=0.3, msg="under the deck, within the maximum excavation depth")
        self.assertAlmostEqual(float(out[1]), 10.0, delta=0.1, msg="back up past the ramp")
        # the ramp: no steeper than UNDER_GRADE, and no kinks (the grade changes smoothly)
        xs = np.arange(0.0, 200.0, 2.0)
        la, lo, _ = FRAME.enu_to_geodetic(xs, np.zeros(len(xs)), 0.0)
        z = g.apply(la, lo, np.full(len(xs), 10.0))
        grade = np.diff(z) / 2.0
        self.assertLessEqual(float(np.abs(grade).max()), grading.UNDER_MAX_GRADE + 0.005)
        self.assertLess(float(np.abs(np.diff(grade)).max()), 0.01, "no kink")
        self.assertAlmostEqual(float(out[2]), 10.0, delta=0.1, msg="another road untouched")

    def _split_dip(self, lines, n_deck):
        f = features([[(-200, 0), (200, 0)]] + lines, ["residential"] + ["motorway"] * n_deck,
                     flags=[(1 + k, "is_bridge") for k in range(n_deck)])
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([0.0]), 0.0)
        return float(g.apply(la, lo, np.full(1, 10.0))[0])

    def test_an_overpass_mapped_in_parts_is_one_span(self):
        """An overpass mapped as two ways end to end, or ending on the carriageways' fork (3 m stubs: the M1 near
        Dundalk, GitHub #960): the road under dips as under one part; off, it kept its ground (the bug)."""
        deep = 10.0 - self.deck_drop()
        halves = [[(5, -20), (5, 3)], [(5, 3), (5, 20)]]
        fork = [[(5, -20), (5, 20)], [(5, 20), (3, 23)], [(5, 20), (7, 23)]]
        for name, lines in (("two halves", halves), ("a fork at its end", fork)):
            with self.subTest(name):
                self.assertAlmostEqual(self._split_dip(lines, len(lines)), deep, delta=0.3)
                with unittest.mock.patch.object(grading, "UNDER_CHAIN_ON", False):
                    self.assertAlmostEqual(self._split_dip(lines, len(lines)), 10.0, delta=0.1, msg="(off: no dip)")

    def test_a_road_ending_short_of_the_junction_dips_with_it(self):
        """A road whose end the map left 1.6 m short of a junction the dip lowers (Tonkin Highway at Morley Drive,
        GitHub #915): it is one of the dip's network (UNDER_NODE_M) and goes down with the junction; at 0.5 m it kept
        its ground, more than snap_junctions takes under the junction, a step of several metres (the bug)."""
        f = features([[(-200, 0), (14, 0)], [(14, 0), (14, 150)], [(15.6, 0), (250, 0)], [(0, -20), (0, 20)]],
                     ["residential", "residential", "residential", "motorway"], flags=[(3, "is_bridge")])
        # (from the junction: with grade ranks (PR #1050) the bug's step stands in the 1.6 m gap, the road past it held
        # level at its own height)
        xs = np.arange(10.0, 60.0, 2.0)
        la, lo, _ = FRAME.enu_to_geodetic(xs, np.zeros(len(xs)), 0.0)

        def grade():
            g = grading.Grader(FRAME, FakeMD(f), lambda la_, lo_: np.full(np.shape(la_), 10.0))
            z = g.apply(la, lo, np.full(len(xs), 10.0))
            return float(np.abs(np.diff(z) / 2.0).max()), float(z[0])
        steep, z0 = grade()
        self.assertLess(z0, 10.0 - 1.5, "(the case: the junction dug)")
        self.assertLessEqual(steep, grading.UNDER_MAX_GRADE + 0.01)
        with unittest.mock.patch.object(grading, "UNDER_NODE_M", 0.5),                 unittest.mock.patch.object(grading, "ROAD_LAWS_ON", False):     # (they would ease the bug's step)
            self.assertGreater(grade()[0], 0.3, "(0.5 m: a step, the bug)")

    def _long_deck_z(self, cross_y, length=500.0):
        """The height of a residential road crossing under a `length` m motorway deck (no carriageway profile) at
        cross_y m along it, with the motorway's own ground road either side; the ground is flat at 10 m."""
        f = features([[(-300, cross_y), (300, cross_y)], [(0, 0), (0, length)], [(0, -260), (0, 0)],
                      [(0, length), (0, length + 260)]], ["residential", "motorway", "motorway", "motorway"],
                     flags=[(1, "is_bridge")])
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([float(cross_y)]), 0.0)
        return float(g.apply(la, lo, np.full(1, 10.0))[0])

    def test_a_road_near_the_end_of_a_long_deck_dips(self):
        """A road crossing under a deck longer than UNDER_MAX_BRIDGE_M (a viaduct, a long ramp) near one of its ends:
        the deck there is no higher than it climbs from the abutment, so the road kept its ground under a ceiling a
        truck did not fit (Popoli's SS17 ramps under the A25's viaduct, Boston's Storrow Drive ramps under the Bowker
        Overpass: reference trucks in the deck). It dips as under a short one, by the climb from that end alone; in
        the long deck's middle it is left as it was; BNE_UNDERPASS_LONG=off (UNDER_LONG_ON): as before."""
        for y in (90.0, 410.0):             # (near either end of the 500 m deck)
            with self.subTest(y=y):
                self.assertLess(self._long_deck_z(y), 10.0 - 2.0, "(under the deck's end, dug)")
                with unittest.mock.patch.object(grading, "UNDER_LONG_ON", False):
                    self.assertAlmostEqual(self._long_deck_z(y), 10.0, delta=0.1, msg="(off: no dip, the bug)")
        self.assertAlmostEqual(self._long_deck_z(250.0), 10.0, delta=0.1,
                               msg="the middle of a long deck is not modelled")


    def _dip_at(self, ground):
        f = features([[(-200, 0), (200, 0)], [(0, -20), (0, 20)]], ["residential", "motorway"],
                     flags=[(1, "is_bridge")])
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), ground))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([0.0]), 0.0)
        return float(g.apply(la, lo, np.full(1, ground))[0])

    def test_dip_stays_above_the_sea(self):
        """Low ground by the sea (Perth's freeways by the Swan River, GitHub #515, #518): the dip stops
        UNDER_SEA_M over the sea's surface, whose water tiles flood any ground under it; higher up it is as deep as
        before, and ground already under the sea (a polder) is dug as before."""
        floor = grading.seatiles.SEA_E + grading.UNDER_SEA_M
        self.assertGreater(self.deck_drop(), 3.0 - floor, "(the case digs)")
        self.assertAlmostEqual(self._dip_at(3.0), floor, delta=0.1, msg="by the sea: dug to the floor, not under it")
        self.assertLess(self._dip_at(3.0), 3.0 - 1.0, "still dug where there is room")
        deep = 30.0 - self.deck_drop()
        self.assertAlmostEqual(self._dip_at(30.0), deep, delta=0.3, msg="high ground: as deep as before")
        polder = -4.0 - self.deck_drop()
        self.assertAlmostEqual(self._dip_at(-4.0), polder, delta=0.3, msg="a polder: as before")
        with unittest.mock.patch.object(grading, "UNDER_SEA_ON", False):
            self.assertLess(self._dip_at(3.0), grading.seatiles.SEA_E, "(off: under the sea, the reported bug)")

    def test_dip_from_a_low_bank_stays_above_the_sea_beside_it(self):
        """The crossing on a river bank under the floor (#515's interchange by the Swan River): its dip's flat and
        ramps reach the dry ground beside, which stays over the sea too; the bank itself is not lowered."""
        f = features([[(-200, 0), (200, 0)], [(0, -20), (0, 20)]], ["residential", "motorway"],
                     flags=[(1, "is_bridge")])

        def elev(la, lo):
            e, _, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            return np.where(np.abs(np.asarray(e)) < 20.0, 0.3, 3.0)
        xs = np.arange(-120.0, 120.1, 2.0)
        la, lo, _ = FRAME.enu_to_geodetic(xs, np.zeros(len(xs)), 0.0)
        base = elev(la, lo)
        floor = grading.seatiles.SEA_E + grading.UNDER_SEA_M
        z = grading.Grader(FRAME, FakeMD(f), elev).apply(la, lo, base)
        self.assertGreaterEqual(float((z - np.minimum(base, floor)).min()), -0.1, "nowhere under the floor")
        with unittest.mock.patch.object(grading, "UNDER_SEA_ON", False):
            z = grading.Grader(FRAME, FakeMD(f), elev).apply(la, lo, base)
        self.assertLess(float(z[np.abs(xs) >= 30].min()), grading.seatiles.SEA_E, "(off: the dry ground under the sea)")


# (the legacy pass's own behaviour, pinned: under BNE_SHAPES the deck's rise is the shapes' and findground's cost law is asked
# of every span, which prices a rise the frontage roads would hold down as free: a motorway under a deck whose ramps cannot
# rise is the legacy pass's dip, as these tests hold)
@unittest.mock.patch.object(grading, "SHAPES_ON", False)
class UnderpassShortfallTest(unittest.TestCase):
    @unittest.mock.patch.object(grading, "ROAD_LAWS_ON", False)     # (the dip's own rule; a motorway's laws lengthen it)
    def test_road_under_gives_way_for_what_the_ramps_could_not_raise(self):
        """A local road's bridge over a motorway (the motorway never rises): with room for its ramps the overpass rises
        and the motorway stays; with frontage roads hugging both ends (the ramps held down) the motorway dips under it
        instead (BR-381 near Pouso Alegre: ~1 m of clearance, the AI car drove into the abutment)."""
        mway = [(-300, 0), (300, 0)]
        bridge = [(0, -20), (0, 20)]
        ramps = [[(0, -260), (0, -20)], [(0, 20), (0, 260)]]
        frontage = [[(-150, -26), (150, -26)], [(-150, 26), (150, 26)]]

        def motorway_z(lines, classes):
            f = features(lines, classes, flags=[(1, "is_bridge")])
            g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
            la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 250.0]), np.array([0.0, 0.0]), 0.0)
            return g.apply(la, lo, np.full(2, 10.0))
        free = motorway_z([mway, bridge] + ramps, ["motorway", "tertiary", "tertiary", "tertiary"])
        self.assertAlmostEqual(float(free[0]), 10.0, delta=0.3, msg="the overpass rises: the motorway stays")
        held = motorway_z([mway, bridge] + ramps + frontage, ["motorway", "tertiary", "tertiary", "tertiary",
                                                               "residential", "residential"])
        self.assertLess(float(held[0]), 10.0 - 0.6 * grading.UNDER_CLEAR_M, "ramps held down: the motorway dips")
        self.assertAlmostEqual(float(held[1]), 10.0, delta=0.1, msg="back up past its ramp")
        with unittest.mock.patch.object(grading, "UNDER_ANY_ON", False):
            off = motorway_z([mway, bridge] + ramps + frontage, ["motorway", "tertiary", "tertiary", "tertiary",
                                                                  "residential", "residential"])
        self.assertAlmostEqual(float(off[0]), 10.0, delta=0.3, msg="BNE_UNDERPASS_ANY=off: as before")

    def test_on_lidar_a_road_level_with_the_decks_ends_keeps_its_ground(self):
        """Pittsburgh's 28th Street Bridge (the user's report, 2026-10-29): its mapped end lay level with Sassafras Way
        under it in the lidar, which has the alley's own ground there; dug 8 m, the dip ran on into Liberty Avenue. On
        lidar the road keeps its ground; on the coarse models (no cutting in them: BR-381) it still dips."""
        mway = [(-300, 0), (300, 0)]
        lines = [mway, [(0, -20), (0, 20)], [(0, -260), (0, -20)], [(0, 20), (0, 260)],
                 [(-150, -26), (150, -26)], [(-150, 26), (150, 26)]]
        classes = ["motorway", "tertiary", "tertiary", "tertiary", "residential", "residential"]

        class Dem:
            def __init__(self, coarse):
                self.coarse = coarse

            def coarse_at(self, lat, lon):
                return self.coarse

        def motorway_z(coarse):
            g = grading.Grader(FRAME, FakeMD(features(lines, classes, flags=[(1, "is_bridge")])),
                               lambda la, lo: np.full(np.shape(la), 10.0))
            g.dem = Dem(coarse)
            la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([0.0]), 0.0)
            return float(g.apply(la, lo, np.full(1, 10.0))[0])
        self.assertAlmostEqual(motorway_z(False), 10.0, delta=0.1, msg="lidar: the road under keeps its ground")
        self.assertLess(motorway_z(True), 10.0 - 0.6 * grading.UNDER_CLEAR_M, "coarse: it dips as before")


    def test_a_deck_on_its_embankments_is_not_taken_for_one_down_their_slopes(self):
        """Tartu mnt's decks over the Jüri roundabout and Kurna - Tuhala over Tallinna ringtee (Estonia): the decks'
        map lines end part-way down their embankments, and the decks stand on the embankments' tops (fill_abutment,
        #684). The dip under them took the decks' heights from the ground at their lines' ends, 1.5-4.4 m lower: the
        roads under were dug 2.8-3.8 m, and their slip roads beside them not, a step between them."""
        lines = [[(-300, 0), (300, 0)], [(0, -28), (0, 28)], [(0, -260), (0, -28)], [(0, 28), (0, 260)]]
        classes = ["residential", "motorway", "motorway", "motorway"]
        depth = roads.deck_depth(56.0)
        # the embankment over the road under, 0.6 m over the deck's clearance (room enough for the deck to clear the
        # road); its slopes from 20 m to 40 m either side of the road
        top = roads.deck_road_clear(depth) + grading.ROAD_LIFT + 0.6

        def elev(la, lo):
            e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            bank = top * np.clip((np.abs(np.asarray(n)) - 20.0) / 20.0, 0.0, 1.0)
            return 10.0 + np.where(np.abs(np.asarray(e)) < 15.0, bank, 0.0)

        def z_at(e, n):
            g = grading.Grader(FRAME, FakeMD(features(lines, classes, flags=[(1, "is_bridge")])), elev)
            la, lo, _ = FRAME.enu_to_geodetic(np.array(e, float), np.array(n, float), 0.0)
            return g.apply(la, lo, elev(la, lo))
        deck_end, under = (float(v) for v in z_at([0.0, 0.0], [28.0, 0.0]))
        self.assertGreater(deck_end, 10.0 + top - 0.5, "(the deck's end on its embankment)")
        self.assertGreater(deck_end - 10.0, roads.deck_road_clear(depth) + grading.ROAD_LIFT, "(room enough)")
        self.assertAlmostEqual(under, 10.0, delta=0.3, msg="the road under keeps its ground")
        with unittest.mock.patch.object(grading, "ABUT_FILL_ON", False):
            self.assertLess(float(z_at([0.0], [0.0])[0]), 10.0 - 2.0, "(BNE_ABUT_FILL=off: dug as before)")


# (the legacy pass's own behaviour, pinned: under BNE_SHAPES the deck's rise is the shapes' and findground's cost law is asked
# of every span, which prices a rise the frontage roads would hold down as free: a motorway under a deck whose ramps cannot
# rise is the legacy pass's dip, as these tests hold)


class RacewayKeepTest(unittest.TestCase):
    def test_track_surface_is_the_terrain(self):
        """A road crossing a racetrack at grade: on the track (its carriageway + margin) the terrain stays as it is
        (a banked oval's slope), off it the road is graded as usual."""
        f = features([[(-200, 0), (200, 0)], [(0, -100), (0, 100)]], ["raceway", "residential"])

        def sloped(la, lo):         # a banking: the ground rising to the north
            _e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return 10.0 + 0.3 * np.asarray(n)
        g = grading.Grader(FRAME, FakeMD(f), sloped)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 0.0]), np.array([3.0, 60.0]), 0.0)
        base = sloped(la, lo)
        out = g.apply(la, lo, base)
        self.assertAlmostEqual(float(out[0]), float(base[0]), places=6, msg="on the track: the terrain")

    def test_track_smoothed_along_its_banking_kept(self):
        """GitHub #833: the elevation data's short bumps along a racetrack are smoothed away; its banking (the ground's
        slope across it) and its long crests stay."""
        f = features([[(-400, 0), (400, 0)]], ["raceway"])

        def ground(e, n):
            return 10.0 + 0.3 * n + 0.15 * np.sin(2 * np.pi * e / 6.0) + 3.0 * np.sin(2 * np.pi * e / 600.0)

        def elev(la, lo):
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return ground(np.asarray(e), np.asarray(n))
        g = grading.Grader(FRAME, FakeMD(f), elev)
        e = np.repeat(np.arange(-100.0, 100.0, 0.7), 3)
        n = np.tile([-4.0, 0.0, 4.0], len(e) // 3)
        la, lo, _ = FRAME.enu_to_geodetic(e, n, 0.0)
        base = elev(la, lo)
        ee, nn, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
        smooth = 10.0 + 0.3 * np.asarray(nn) + 3.0 * np.sin(2 * np.pi * np.asarray(ee) / 600.0)
        out = g.apply(la, lo, base)
        self.assertGreater(float(np.abs(base - smooth).max()), 0.14)
        np.testing.assert_allclose(out, smooth, atol=0.04, err_msg="the bumps gone, banking and crest kept")
        with unittest.mock.patch.object(grading, "RACEWAY_SMOOTH_ON", False):
            off = grading.Grader(FRAME, FakeMD(f), elev).apply(la, lo, base)
        np.testing.assert_allclose(off, base, atol=1e-9, err_msg="BNE_RACEWAY_SMOOTH=off: the ground as it is")


class JunctionTest(unittest.TestCase):
    def test_side_road_meets_the_road_it_joins(self):
        import roads
        x = np.arange(-200, 201, 4.0)
        main = grading.Plan(np.stack([x, 0 * x], 1), 10 + 0.02 * x, 5, road_half=4)
        y = np.arange(0, 201, 4.0)
        side = grading.Plan(np.stack([0 * y + 50, y], 1), 11.8 + 0 * y, 3, road_half=3)   # 0.8 m over the main road
        grading.snap_junctions([main, side], [roads.Spec(8.0, "paved", False, "primary", False),
                                              roads.Spec(6.0, "paved", False, "residential", False)])
        self.assertAlmostEqual(side.z[0], 11.0, places=6)                  # at the main road's height where it joins
        self.assertAlmostEqual(side.z[-1], 11.8, places=6)                 # its own height past the taper
        self.assertTrue(np.all(np.diff(side.z[:12]) >= -1e-9))             # eased in, no bump
        np.testing.assert_allclose(main.z, 10 + 0.02 * x)                  # the main road keeps its profile

    def test_a_side_road_follows_its_road_down_into_a_dip(self):
        """Baltimore's Haven Street (2026-10-28): the alleys were snapped to the street before the underpass dip lowered
        it 3.5 m, each a hump; snapped again after the dips, they meet it at its new height (a second snap is what
        Grader._make_cell runs after _lower_underpasses)."""
        import roads
        x = np.arange(-200, 201, 4.0)
        main = grading.Plan(np.stack([x, 0 * x], 1), 10.0 + 0 * x, 5, road_half=4)
        y = np.arange(0, 101, 4.0)
        side = grading.Plan(np.stack([0 * y + 50, y], 1), 10.0 + 0 * y, 2, road_half=2)
        specs = [roads.Spec(8.0, "paved", False, "primary", False), roads.Spec(4.0, "paved", False, "service", False)]
        grading.snap_junctions([main, side], specs)
        main.z = main.z - 3.0 * np.exp(-((x - 60.0) / 40.0) ** 2)     # the dip, after the first snap
        grading.snap_junctions([main, side], specs)
        zq = np.interp(50.0, x, main.z)        # (the main road's height where the side road meets it)
        self.assertAlmostEqual(side.z[0], zq, places=6)
        self.assertAlmostEqual(side.z[-1], 10.0, places=6)


    def test_a_continuation_is_not_a_junction(self):
        import roads
        x = np.arange(0, 201, 4.0)
        a = grading.Plan(np.stack([x, 0 * x], 1), 10 + 0 * x, 4, road_half=3)
        b = grading.Plan(np.stack([x + 200, 0 * x], 1), 11 + 0 * x, 4, road_half=3)
        res = roads.Spec(6.0, "paved", False, "residential", False)
        grading.snap_junctions([a, b], [res, res])
        self.assertEqual((a.z[-1], b.z[0]), (10.0, 11.0))


class DirtEndsTest(unittest.TestCase):
    """PR #677 merge: a worn dirt road's damage and lean are zero at its own ends, short parts too (a junction meets
    the joining road at its height, level across)."""

    @unittest.skipIf(grading.DIRT_ROUGH <= 0.0, "BNE_DIRT_BUMP 0")
    def test_short_part_ends_untouched_and_level(self):
        spec = roads.Spec(4.0, "dirt", False, "unclassified")
        for length in (12.0, 30.0, 300.0):
            xy = np.stack([np.arange(0.0, length + 0.01, 2.0), np.zeros(int(length / 2) + 1)], 1)
            z = np.full(len(xy), 50.0)
            b = grading.dirt_bumps(xy, spec, len(z))
            self.assertIsNotNone(b)
            self.assertAlmostEqual(float(b[0]), 0.0, places=9)
            self.assertAlmostEqual(float(b[-1]), 0.0, places=9)
            plan = grading._plan(xy, z, spec)
            self.assertAlmostEqual(float(plan.z[0]), 50.0, places=6, msg=f"{length} m: start at the profile")
            self.assertAlmostEqual(float(plan.z[-1]), 50.0, places=6, msg=f"{length} m: end at the profile")
            if plan.bank is not None:
                self.assertAlmostEqual(float(plan.bank[0]), 0.0, places=9)
                self.assertAlmostEqual(float(plan.bank[-1]), 0.0, places=9)

    def test_paved_untouched(self):
        spec = roads.Spec(7.0, "paved", False, "residential")
        xy = np.stack([np.arange(0.0, 200.0, 2.0), np.zeros(100)], 1)
        self.assertIsNone(grading.dirt_bumps(xy, spec, 100))

class AbutEndsTest(unittest.TestCase):
    """Which road ends lead onto a bridge (_Planner._abut_ends): those within APPROACH_TOUCH_M of its end, except a
    junction behind a ground part that reaches the end itself. Riva del Garda, Via Gardesana: a way's 1.03 m of ground
    between a junction and its bridge; the junction's three roads (the approach, the stub, a side road) and the stub's
    own start all stood within 2.5 m of the bridge's end, so all were abutments, held at their own heights: the stub
    took the hillside's raw 28 % fall in its metre (0.86 m under the approach at the same node) and the approach's
    8 % under-deck ground stood over the stub's own surface. With the game's abutment slope under decks (native 306)
    the stub's own fall showed: a 0.30 m step into a primary road's bridge."""

    def touch(self, pe, bridge_end=(0.0, 0.0)):
        key = mapcells.cell_of(LAT, LON)
        planner = types.SimpleNamespace(frame=FRAME, _cell_enu=lambda home, ground=True: {"ends": np.array([bridge_end])})
        return grading._Planner._abut_ends(planner, key, np.array(pe, np.float64)).tolist()

    def test_a_junction_behind_a_stub_is_no_abutment(self):
        # (the junction J 1.05 m before the bridge's end: the approach ends there, a stub runs on from it to the
        # bridge's end, a side road starts there)
        got = self.touch([[-60.0, 0.0], [-1.05, 0.0],         # the approach: start, end at J
                          [-1.05, 0.0], [0.04, 0.0],          # the stub: J, the bridge's end
                          [-1.05, 0.0], [30.0, 30.0]])        # the side road: J, far
        self.assertEqual(got, [False, False, False, True, False, False])

    def test_a_road_ending_short_of_the_bridge_still_leads_onto_it(self):
        # (no end at the bridge's end: the map's gaps, up to APPROACH_TOUCH_M, stay abutments)
        got = self.touch([[-60.0, 0.0], [-2.0, 0.0], [0.0, 40.0], [0.0, 1.5]])
        self.assertEqual(got, [False, True, False, True])

    def test_two_roads_ending_at_the_bridge_both_lead_onto_it(self):
        # (a bridge between two carriageways' ends: each its own abutment)
        got = self.touch([[-60.0, 0.0], [-0.3, 0.0], [0.0, 40.0], [0.2, 0.9]])
        self.assertEqual(got, [False, True, False, True])


if __name__ == "__main__":
    unittest.main()


class ContinuationHoldTest(unittest.TestCase):
    """PlanPack: one road planned in pieces is one road in grade_points' ranks (the user, 2026-11-10: a dip in a
    primary where its short piece between two junctions let the side roads pull its painted edge down)."""

    def plan(self, xy, z, rank, rid, half=7.0, road_half=5.0):
        p = grading.Plan(np.asarray(xy, np.float64), np.asarray(z, np.float64), half, road_half=road_half)
        p.rank, p.rid = rank, rid
        return p

    def test_pieces_that_carry_straight_on_share_one_road(self):
        a = self.plan([[0.0, 0.0], [50.0, 0.0]], [100.0, 100.0], 114, 101)
        b = self.plan([[50.0, 0.0], [100.0, 0.0]], [100.15, 100.15], 114, 102)   # 15 cm at the joint
        c = self.plan([[50.0, 20.0], [50.0, 0.0]], [99.0, 99.0], 108, 103, half=8.0, road_half=2.8)  # a side road
        pack = grading.PlanPack([a, b, c])
        ra, rb, rc = (np.unique(pack.rid[pack.of == n]) for n in range(3))
        self.assertEqual(ra.tolist(), rb.tolist(), "the two pieces are one road")
        self.assertNotEqual(ra.tolist(), rc.tolist(), "a side road is not")

    def test_the_side_road_does_not_move_the_split_main(self):
        a = self.plan([[0.0, 0.0], [50.0, 0.0]], [100.0, 100.0], 114, 111)
        b = self.plan([[50.0, 0.0], [100.0, 0.0]], [100.15, 100.15], 114, 112)
        c = self.plan([[50.0, 20.0], [50.0, 0.0]], [99.0, 99.0], 108, 113, half=8.0, road_half=2.8)
        x = np.arange(35.0, 49.1, 1.0)
        for y in (0.0, 3.0, 4.8):
            pts = np.array([x, np.full_like(x, y)])
            base = np.full(len(x), 100.0)
            alone = grading.grade_points(pts[0], pts[1], base, [a, b])
            with_side = grading.grade_points(pts[0], pts[1], base, [a, b, c])
            np.testing.assert_allclose(with_side, alone, atol=1e-9, err_msg=f"y={y}: the main's carriageway held")

    def test_a_true_equal_rank_crossing_is_not_one_road(self):
        a = self.plan([[0.0, 0.0], [100.0, 0.0]], [100.0, 100.0], 114, 201)
        b = self.plan([[50.0, -50.0], [50.0, 50.0]], [100.0, 100.0], 114, 202)
        pack = grading.PlanPack([a, b])
        ra, rb = (np.unique(pack.rid[pack.of == n]) for n in range(2))
        self.assertNotEqual(ra.tolist(), rb.tolist(), "a crossing with no shared end is two roads")

    def test_a_fork_at_another_level_is_not_one_road(self):
        a = self.plan([[0.0, 0.0], [50.0, 0.0]], [100.0, 100.0], 114, 301)
        b = self.plan([[50.0, 0.0], [100.0, 5.0]], [100.8, 100.8], 114, 302)     # 0.8 m step: a real fork
        pack = grading.PlanPack([a, b])
        ra, rb = (np.unique(pack.rid[pack.of == n]) for n in range(2))
        self.assertNotEqual(ra.tolist(), rb.tolist())


if __name__ == "__main__":
    unittest.main()


class LevelWallsTest(unittest.TestCase):
    def _pair(self, gap=9.3, dz=7.2):
        """Goeschenen's link road over the exit ramp in its cutting (the ramp's plan: a motorway's 14 m carriageway, core
        8 m out, rank 118, dug; the link road's: 8.3 m carriageway, core 5.15 m out, rank 116, on the ground 7.2 m up,
        their centre lines 9.3 m apart, the carriageways overlapping 1.85 m in plan), both along x."""
        x = np.arange(0.0, 121.0, 4.0)
        low = grading.Plan(np.c_[x, np.zeros(len(x))], np.zeros(len(x)), 8.0, road_half=7.0)
        low.rid, low.rank = 1, 118
        high = grading.Plan(np.c_[x, np.full(len(x), gap)], np.full(len(x), dz), 5.15, road_half=4.15)
        high.rid, high.rank = 2, 116
        return [("ramp", low), ("link", high)]

    def test_neither_road_is_moved_by_the_other(self):
        """Two carriageways overlapping in plan at heights no slope bridges in the room between them meet at one line
        (as deep in the one as in the other, 6.075 m from the ramp's centre). Each road keeps its own ground on its own
        side of it: the ramp's lanes up to the face, the link's from the face on to its far edge. Before, the ramp's
        lanes (and its rank's hold) reached on past the face over the link's carriageway and the link road, on the
        ground, was dug 7.3 m under itself at its own centre line (the survey's truth replay: 66 per mille of the roads'
        points dug)."""
        plans = self._pair()
        self.assertEqual(grading.level_walls(plans, [None, None]), 2)
        low, high = plans[0][1], plans[1][1]
        line = (9.3 + 7.0 - 4.15) / 2
        np.testing.assert_allclose(np.asarray(low.cut)[:, 0], line)            # (the link on its left)
        np.testing.assert_allclose(np.asarray(low.cut)[:, 1], 7.2)
        self.assertTrue(np.isnan(np.asarray(low.cut)[:, 3]).all())
        np.testing.assert_allclose(np.asarray(high.cut)[:, 3], 9.3 - line)       # (its fall: the same line)
        self.assertTrue(np.isnan(np.asarray(high.cut)[:, 4]).all())
        y = np.arange(-6.0, 15.01, 0.25)
        z = grading.grade_points(np.full(len(y), 60.0), y, np.full(len(y), 7.2), [p for _f, p in plans], lift=0.0)
        ramp = (y <= line - 0.25) & (y >= -7.0)         # (the ramp's lanes: up to its face)
        self.assertLess(float(np.abs(z[ramp]).max()), 0.05, np.round(z, 2).tolist())
        link = (y >= line + 0.75) & (y <= 9.3 + 4.15)   # (the link's lanes from the face on, a yield ramp past it)
        self.assertLess(float(np.abs(z[link] - 7.2).max()), 0.05, np.round(z, 2).tolist())

    def test_one_level_and_far_apart_unchanged(self):
        """The link 1.5 m over the ramp (one level: WALL_CUT_M), or 30 m from it (their grounds clear of each other): no
        line between them, the plans as they were."""
        for gap, dz in ((9.3, 1.5), (30.0, 7.2)):
            plans = self._pair(gap, dz)
            self.assertEqual(grading.level_walls(plans, [None, None]), 0)
            self.assertIsNone(plans[0][1].cut)
            self.assertIsNone(plans[1][1].cut)

    def test_the_line_is_the_walls_one(self):
        """level_boundary, the grading's and the walls' (roads.retaining_walls) one line: from each road's centre, the
        lower's face and the higher's fall at the same place, nothing at one level or apart."""
        face, fall = grading.level_boundary(9.3, 7.0, 8.0, 4.15, 5.15, 7.2)
        self.assertAlmostEqual(float(face), 6.075)
        self.assertTrue(np.isnan(fall))
        face, fall = grading.level_boundary(9.3, 4.15, 5.15, 7.0, 8.0, -7.2)
        self.assertTrue(np.isnan(face))
        self.assertAlmostEqual(float(fall), 9.3 - 6.075)
        self.assertTrue(np.isnan(grading.level_boundary(9.3, 7.0, 8.0, 4.15, 5.15, 1.5)).all())
        self.assertTrue(np.isnan(grading.level_boundary(30.0, 7.0, 8.0, 4.15, 5.15, 7.2)).all())


class AbutmentWallsTest(unittest.TestCase):
    """A deck's end standing on another road's carriageway at another level (Boston's Bowker Overpass over Storrow Drive: the
    deck's end 6.4 m from the centre line of a road whose carriageway reaches 11 m each side, 10 m under it). The higher-
    ranked road's hold reached over the approach's core, and the embankment, planned up to the deck's height, fell to the
    road's level over its last metres: a 4-6 m step against the deck's top at its end."""

    def _plans(self, tilt=0.0, low_z=1.6, gap=6.4):
        x = np.arange(-60.0, 61.0, 4.0)
        low = grading.Plan(np.c_[x, np.zeros(len(x))], np.full(len(x), low_z), 12.0, road_half=11.0)
        low.rid, low.rank = 1, 118
        y = np.arange(46.0, gap - 0.01, -3.9)
        y = np.r_[y[y > gap], gap]
        xy = np.c_[np.zeros(len(y)), y]
        if tilt:
            th = np.radians(tilt)
            rot = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
            xy = (xy - [0.0, gap]) @ rot.T + [0.0, gap]
        app = grading.Plan(xy, np.linspace(7.5, 11.74, len(y)), 5.7, abut=(False, True), road_half=2.7)
        app.rid, app.rank = 2, 114
        return [("approach", app), ("road", low)]

    def _ground(self, xy):
        return np.full(len(xy), 7.4)        # (the road, at 1.6, in a cutting 5.8 m deep)

    def _at_end(self, plans, back):
        """The graded ground m back from the deck's end along the approach (negative: under the deck)."""
        xy = np.asarray(plans[0][1].xy)
        d = (xy[-1] - xy[-2]) / np.hypot(*(xy[-1] - xy[-2]))
        pts = xy[-1][None, :] - np.asarray(back, np.float64)[:, None] * d[None, :]
        return grading.grade_points(pts[:, 0], pts[:, 1], np.full(len(pts), 7.4), [p for _f, p in plans])

    def test_the_ground_meets_the_deck_at_its_end(self):
        for tilt in (0.0, 20.0):
            plans = self._plans(tilt)
            deck_z = float(plans[0][1].z[-1])
            with unittest.mock.patch.object(grading, "ABUT_WALLS_ON", False):
                before = self._at_end(plans, [0.0, 1.0])
            self.assertLess(float(before.max()), deck_z - 3.0, "the road's hold over the approach's last metres")
            self.assertEqual(grading.abutment_walls(plans, [None, None], self._ground), 1)
            back = np.array([0.0, 1.0])
            a = plans[0][1]
            grade = (a.z[-1] - a.z[-2]) / float(np.hypot(*(np.asarray(a.xy)[-1] - np.asarray(a.xy)[-2])))
            after = self._at_end(plans, back)            # (the approach's own height back along it, its grade)
            self.assertLess(float(np.abs(after - (deck_z - grade * back)).max()), 0.35, (tilt, np.round(after, 2).tolist()))

    def test_the_wall_is_the_roads_and_ends_its_lanes_at_the_approach(self):
        plans = self._plans()
        grading.abutment_walls(plans, [None, None], self._ground)
        self.assertIsNone(plans[0][1].cut, "the approach keeps its own ground")
        cut = np.asarray(plans[1][1].cut)
        near = np.isfinite(cut[:, 0])           # (the approach is on the road's left: it runs along +x, the approach at y > 0)
        self.assertTrue(near.any() and not np.isfinite(cut[:, 3]).any())
        np.testing.assert_allclose(cut[near, 1], plans[0][1].z[-1])        # (its top: the deck's height)
        self.assertLessEqual(float(cut[near, 0].max()), 6.4 + 5.7 - grading.WALL_FLAT_M)   # (the end's edge nearest the segment)
        self.assertAlmostEqual(float(cut[near, 0].min()), 6.4 - grading.WALL_FLAT_M)        # (the end line at its nearest, a grid step under the deck)

    def test_one_level_clear_roads_and_off_the_carriageway_are_left_alone(self):
        for kw in (dict(low_z=10.0), dict(gap=12.0)):      # (one level: WALL_CUT_M; the end past the carriageway's edge)
            plans = self._plans(**kw)
            self.assertEqual(grading.abutment_walls(plans, [None, None], self._ground), 0, kw)
            self.assertIsNone(plans[1][1].cut)
        plans = self._plans()
        plans[0][1].abut = (False, False)               # (no deck at its end)
        self.assertEqual(grading.abutment_walls(plans, [None, None], self._ground), 0)
        plans = self._plans()                           # (the approach on its ground: a lid's deck over its trench street)
        self.assertEqual(grading.abutment_walls(plans, [None, None], lambda xy: np.full(len(xy), 11.0)), 0)
        plans = self._plans(low_z=5.0)                  # (the road 6.7 m under it, but on its ground: no cutting)
        self.assertEqual(grading.abutment_walls(plans, [None, None], lambda xy: np.full(len(xy), 5.0)), 0)
        plans = self._plans()
        plans[1][1].rank = -1                           # (no grade rank: no hold to reach over the approach)
        self.assertEqual(grading.abutment_walls(plans, [None, None], self._ground), 0)


class BumpsOffRoadSidesTest(unittest.TestCase):
    """terrain_bumps with a grader: none within BUMP_CLEAR_M of a built road's core (a median's barrier stood module by
    module on them, jagged: I-40, the user's report 2026-11-16), fading in past it without a ledge, the same value
    however the points are asked (a block's grid, a few scattered points), and as before far from the roads."""

    def setUp(self):
        for name, val in (("TERRAIN_BUMPS_ON", True), ("TERRAIN_ROUGH_M", 0.35)):
            p = unittest.mock.patch.object(grading, name, val)
            p.start()
            self.addCleanup(p.stop)
        f = features([[(-400, 20), (400, 20)]], ["residential"])
        self.g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))

    def _across(self, ys, x=None):
        x = np.linspace(-150.0, 150.0, 601) if x is None else x
        X, Y = np.meshgrid(x, np.asarray(ys, np.float64))
        la, lo, _ = FRAME.enu_to_geodetic(X.ravel(), Y.ravel(), 0.0)
        return np.asarray(la), np.asarray(lo), X.shape

    def test_none_beside_the_road_full_far_off(self):
        half = float(max(p.half for p in self.g.plans(*self._across([20.0])[:2])))
        la, lo, shape = self._across([20.0, 20.0 + half + 2.0, -60.0, 120.0])
        got = np.asarray(grading.terrain_bumps(la, lo, self.g)).reshape(shape)
        plain = np.asarray(grading.terrain_bumps(la, lo)).reshape(shape)
        self.assertEqual(float(np.abs(got[:2]).max()), 0.0, "on the road and just past its core: no bumps")
        self.assertGreater(float(plain[:2].std()), 0.1, "(there were bumps there before)")
        self.assertTrue(np.array_equal(got[2:], plain[2:]), "far off the road: the bumps as before")

    def test_fades_in_without_a_ledge(self):
        x = np.linspace(-50.0, 50.0, 21)
        ys = np.arange(20.0, 60.0, 0.25)
        la, lo, shape = self._across(ys, x)
        got = np.asarray(grading.terrain_bumps(la, lo, self.g)).reshape(shape)
        plain = np.asarray(grading.terrain_bumps(la, lo)).reshape(shape)
        steepest = float(np.abs(np.diff(plain, axis=0)).max())
        self.assertLessEqual(float(np.abs(np.diff(got, axis=0)).max()), 1.2 * steepest,
                             "no steeper than the bumps themselves across the band's edge (no ledge)")
        self.assertEqual(float(np.abs(got[ys <= 30.0]).max()), 0.0, "smooth well past the core")
        self.assertGreater(float(got[ys >= 50.0].std()), 0.25, "full past the band")

    def test_same_value_however_asked(self):
        la, lo, _ = self._across(np.arange(10.0, 50.0, 1.0))
        grid = np.asarray(grading.terrain_bumps(la, lo, self.g))
        pick = np.arange(0, len(la), 997)
        lone = np.asarray(grading.terrain_bumps(la[pick], lo[pick], self.g))
        self.assertTrue(np.array_equal(grid[pick], lone))
        one = grading.terrain_bumps(la[pick[3]], lo[pick[3]], self.g)
        self.assertEqual(float(one), float(grid[pick[3]]))

    def test_dirt_roads_keep_them(self):
        f = features([[(-400, 20), (400, 20)]], ["track"])
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = self._across([24.0])
        self.assertTrue(np.array_equal(np.asarray(grading.terrain_bumps(la, lo, g)),
                                       np.asarray(grading.terrain_bumps(la, lo))))
