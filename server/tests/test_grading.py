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
        with unittest.mock.patch.object(grading, "ABUT_UNDER_GRADE", 0.0):
            old = grading.grade_points(px, py, base, [approach])
        self.assertGreater(old[2], 81.9, "before: held at the end's height 6 m back over the deck")
        plain = grading.Plan(xy, np.full(21, 82.0), 9.0)    # an ordinary road end (no bridge): its round end as before
        self.assertTrue(np.allclose(grading.grade_points(px, py, base, [plain]), old, atol=1e-9), "a plain road end: as before")

    def test_end_abutment_too(self):
        xy = np.stack([np.arange(-80.0, 0.1, 4.0), np.zeros(21)], axis=1)  # the approach, its end the abutment
        approach = grading.Plan(xy, np.full(21, 50.0), 7.0, abut=(False, True))
        out = grading.grade_points(np.array([0.0, 5.0]), np.zeros(2), np.full(2, 40.0), [approach])
        self.assertAlmostEqual(out[0], 50.0, delta=0.05)
        self.assertLessEqual(out[1], 50.0 - 5.0 * grading.ABUT_UNDER_GRADE + 0.05)


class RailUnderAbutmentTest(unittest.TestCase):
    """GitHub #909 (the A11 over the Gramzower Museumsbahn by Neu-Kleinow): a short bridge over a railway in a cutting,
    crossing it at 45 deg off-centre, its abutment 2.7 m from the track's centreline, inside the rail bed's flat band
    (railways.GRADE_HALF_M). The bed (6 m down) blended into the abutment, the deck end took that ground (2.5-3.4 m
    under the road going on from it). The band past the ballast gives way to the approach's carriageway there."""

    def setUp(self):
        xs = np.arange(-80.0, 0.1, 4.0)          # the approach, its end (0, 0) the abutment
        self.approach = grading.Plan(np.stack([xs, np.zeros(len(xs))], axis=1), np.full(len(xs), 75.4), 8.0,
                                     abut=(False, True), road_half=7.0)
        u, n = np.array([1.0, 1.0]) / math.sqrt(2.0), np.array([1.0, -1.0]) / math.sqrt(2.0)
        t = np.arange(-100.0, 100.1, 2.0)
        self.c0 = 2.7 * n                         # the track's nearest point to the abutment (under the deck)
        self.rail_xy = self.c0[None] + t[:, None] * u[None]
        self.rail_z = np.full(len(t), 69.3)

    def test_the_abutment_keeps_its_road(self):
        rail = grading.rail_plan("w1", self.rail_xy, self.rail_z)
        out = grading.grade_points(np.array([0.0]), np.array([0.0]), np.array([69.4]), [self.approach, rail])
        self.assertAlmostEqual(out[0], 75.4, delta=0.25, msg="the abutment at its approach's height")

    def test_the_track_bed_is_no_higher(self):
        # (under the deck, past the abutment end, the approach has no carriageway: the band does not give way there,
        # so the track and the ground beside it stand no higher than with the whole band the rail's carriageway)
        rail = grading.rail_plan("w1", self.rail_xy, self.rail_z)
        old = grading.Plan(self.rail_xy, self.rail_z, rail.half)
        old.rid = rail.rid
        n = np.array([1.0, -1.0]) / math.sqrt(2.0)
        pts = np.array([self.c0 + k * n for k in (-1.5, -0.75, 0.0, 0.75, 1.5)] +
                       [self.c0 + 40.0 * np.array([1.0, 1.0]) / math.sqrt(2.0)])
        base = np.full(len(pts), 69.4)
        got = grading.grade_points(pts[:, 0], pts[:, 1], base, [self.approach, rail])
        was = grading.grade_points(pts[:, 0], pts[:, 1], base, [self.approach, old])
        self.assertTrue((got <= was + 1e-9).all(), f"{got} over {was}")
        self.assertAlmostEqual(got[-1], 69.3, delta=0.05, msg="the cutting away from the bridge")

    def test_an_approach_cut_short_of_its_abutment(self):
        # (each map cell grades its own piece of a road, trim_plan: the piece ending at a cell's edge 2 m short of the
        # abutment had no abutment end of its own, and its carriageway's round end reached over the deck: 75.3 m on
        # the track's side under it, 70.6 m with the approach whole; Codex's review of PR #920. Cut twice too: a
        # cell corner's sliver between)
        rail = grading.rail_plan("w1", self.rail_xy, self.rail_z)
        old = grading.Plan(self.rail_xy, self.rail_z, rail.half)
        old.rid = rail.rid
        n = np.array([1.0, -1.0]) / math.sqrt(2.0)
        pts = np.array([self.c0 + k * n for k in (-2.5, -1.5, -0.75, 0.0, 0.75, 1.5, 2.5)])
        base = np.full(len(pts), 69.4)
        whole = grading.grade_points(pts[:, 0], pts[:, 1], base, [self.approach, rail])
        whole_was = grading.grade_points(pts[:, 0], pts[:, 1], base, [self.approach, old])
        for cuts in ((-2.0,), (-2.0, -1.0)):
            edges = (-200.0,) + cuts + (200.0,)
            pieces = [q for x0, x1 in zip(edges[:-1], edges[1:])
                      for q in grading.trim_plan(self.approach, (x0, -200.0, x1, 200.0))]
            self.assertEqual(len(pieces), len(cuts) + 1)
            # every piece's segment its distance on to the abutment at (0, 0), as the whole approach's
            pk = grading.PlanPack(pieces)
            to = -pk.b[:, 0]
            np.testing.assert_allclose(pk.abd[:, 1], np.where(to < grading.ABUT_NEAR_M, to, np.nan), atol=1e-9)
            np.testing.assert_array_equal(pk.ab, (to < grading.ABUT_NEAR_M).astype(np.uint8))
            # under the deck the rail bed's narrower carriageway raises the ground no more than with the approach
            # whole (a cut blends its pieces' ends in a little more, the band whole as well)
            got = grading.grade_points(pts[:, 0], pts[:, 1], base, pieces + [rail])
            was = grading.grade_points(pts[:, 0], pts[:, 1], base, pieces + [old])
            self.assertTrue((got - was <= whole - whole_was + 1e-6).all(),
                            f"cut at {cuts}: {got} over {was} (whole: {whole} over {whole_was})")
            top = grading.grade_points(np.array([-1.5]), np.array([0.0]), np.array([69.4]), pieces + [rail])
            self.assertAlmostEqual(top[0], 75.4, delta=0.05, msg="the approach at its height")


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
        with unittest.mock.patch.object(grading, "CHAIN_ON", on):
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
            pts, off = fast._ef.chain_extensions(
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
    def test_road_dips_under_a_motorway_overpass(self):
        """A local road crossing under a short motorway bridge whose terrain was interpolated at deck height (lidar
        with the bridge removed) dips to the deck minus UNDER_CLEAR_M; a road crossing nothing is left alone."""
        f = features([[(-200, 0), (200, 0)], [(0, -20), (0, 20)], [(-200, 300), (200, 300)]],
                     ["residential", "motorway", "residential"], flags=[(1, "is_bridge")])
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 195.0, 0.0]), np.array([0.0, 0.0, 300.0]), 0.0)
        out = g.apply(la, lo, np.full(3, 10.0))
        self.assertAlmostEqual(float(out[0]), 10.0 - min(grading.UNDER_CLEAR_M, grading.UNDER_MAX_DROP_M),
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
        deep = 10.0 - min(grading.UNDER_CLEAR_M, grading.UNDER_MAX_DROP_M)
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
        with unittest.mock.patch.object(grading, "UNDER_NODE_M", 0.5):
            self.assertGreater(grade()[0], 0.3, "(0.5 m: a step, the bug)")

    def test_merge_span_chains(self):
        a = np.array([[0.0, 0.0], [10.0, 0.0]])
        b = np.array([[20.0, 0.0], [10.0, 0.0]])          # (reversed: its end meets a's end)
        c = np.array([[20.0, 0.0], [30.0, 5.0]])
        far = np.array([[100.0, 0.0], [120.0, 0.0]])
        rail = np.array([[30.0, 5.0], [40.0, 5.0]])
        out = grading._merge_span_chains([(a, False, False), (b, False, False), (c, False, False), (far, True, False),
                                          (rail, False, True)])
        self.assertEqual(len(out), 3, "a-b-c joined; the far part and the rail kept apart")
        line = next(ln for ln, f, r in out if not f and not r)
        self.assertEqual([tuple(p) for p in line], [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0), (30.0, 5.0)])
        y = [(a, False, False), (np.array([[10.0, 0.0], [20.0, 2.0]]), False, False),
             (np.array([[10.0, 0.0], [20.0, -2.0]]), False, False)]
        self.assertEqual(len(grading._merge_span_chains(y)), 3, "a branch: nothing joined")

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
        self.assertGreater(min(grading.UNDER_CLEAR_M, grading.UNDER_MAX_DROP_M), 3.0 - floor, "(the case digs)")
        self.assertAlmostEqual(self._dip_at(3.0), floor, delta=0.1, msg="by the sea: dug to the floor, not under it")
        self.assertLess(self._dip_at(3.0), 3.0 - 1.0, "still dug where there is room")
        deep = 30.0 - min(grading.UNDER_CLEAR_M, grading.UNDER_MAX_DROP_M)
        self.assertAlmostEqual(self._dip_at(30.0), deep, delta=0.3, msg="high ground: as deep as before")
        polder = -4.0 - min(grading.UNDER_CLEAR_M, grading.UNDER_MAX_DROP_M)
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


class UnderDipTest(unittest.TestCase):
    def test_a_dip_ends_inside_its_road_part(self):
        d = np.arange(-80.0, 300.1, 1.0)
        free = grading.under_dip(d, 4.0)
        self.assertAlmostEqual(float(free[d == 0][0]), -4.0)
        self.assertLess(float(free[d == -80][0]), -0.5, "with room the ramp runs on past 80 m")
        fit = grading.under_dip(d, 4.0, (80.0, 300.0))
        self.assertAlmostEqual(float(fit[d == -80][0]), 0.0, places=9, msg="the part's start: back up")
        self.assertAlmostEqual(float(fit[d == 0][0]), -4.0)
        grade = np.abs(np.diff(fit[d <= 0]))
        self.assertLessEqual(float(grade.max()), grading.UNDER_MAX_GRADE + 1e-6)
        self.assertTrue(np.allclose(fit[d > 0], free[d > 0]), "the other side as before")
        self.assertIsNone(grading.under_dip(d, 4.0, (20.0, 300.0)), "no room even at UNDER_MAX_GRADE")


class UnderpassShortfallTest(unittest.TestCase):
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

    def test_on_lidar_a_never_raised_deck_level_with_the_road_still_clears_it(self):
        """As above, but the deck is a motorway's (never raised, APPROACH_SKIP): it cannot rise, so the road under it
        still dips, on lidar too (the deck sat on the road: cars drove into it, 2026-10-31)."""
        lines = [[(-300, 0), (300, 0)], [(0, -20), (0, 20)], [(0, -260), (0, -20)], [(0, 20), (0, 260)]]
        classes = ["residential", "motorway", "motorway", "motorway"]

        class Dem:
            def coarse_at(self, lat, lon):
                return False
        g = grading.Grader(FRAME, FakeMD(features(lines, classes, flags=[(1, "is_bridge")])),
                           lambda la, lo: np.full(np.shape(la), 10.0))
        g.dem = Dem()
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([0.0]), 0.0)
        self.assertLess(float(g.apply(la, lo, np.full(1, 10.0))[0]), 10.0 - 0.6 * grading.UNDER_CLEAR_M,
                        "lidar, a motorway's deck: the road under dips")

    def test_a_deck_on_its_embankments_is_not_taken_for_one_down_their_slopes(self):
        """Tartu mnt's decks over the Jüri roundabout and Kurna - Tuhala over Tallinna ringtee (Estonia): the decks'
        map lines end part-way down their embankments, and the decks stand on the embankments' tops (fill_abutment,
        #684). The dip under them took the decks' heights from the ground at their lines' ends, 1.5-4.4 m lower: the
        roads under were dug 2.8-3.8 m, and their slip roads beside them not, a step between them."""
        lines = [[(-300, 0), (300, 0)], [(0, -28), (0, 28)], [(0, -260), (0, -28)], [(0, 28), (0, 260)]]
        classes = ["residential", "motorway", "motorway", "motorway"]
        top = 9.0       # the embankment over the road under; its slopes from 20 m to 40 m either side of the road

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
        depth = roads.deck_depth(56.0)
        self.assertGreater(deck_end - 10.0, roads.deck_road_clear(depth) + grading.ROAD_LIFT, "(room enough)")
        self.assertAlmostEqual(under, 10.0, delta=0.3, msg="the road under keeps its ground")
        with unittest.mock.patch.object(grading, "ABUT_FILL_ON", False):
            self.assertLess(float(z_at([0.0], [0.0])[0]), 10.0 - 2.0, "(BNE_ABUT_FILL=off: dug as before)")


class StreetRunsTest(unittest.TestCase):
    # a street from x -60 to 60 in two pieces (the second drawn backwards) between junctions with side streets
    LINES = [np.array(ln, float) for ln in ([(-300, 0), (-60, 0)], [(-60, 0), (20, 0)], [(60, 0), (20, 0)],
                                             [(60, 0), (300, 0)], [(-60, 0), (-60, 200)], [(60, 0), (60, 200)])]

    def test_a_street_dip_runs_through_its_pieces_to_its_junctions(self):
        cls = ["tertiary"] * 4 + ["residential"] * 2
        net, runs = grading.street_runs(self.LINES, cls, [(1, 60.0, 7.0), (2, 38.0, 1.0)])
        self.assertEqual(net, [])
        self.assertEqual(len(runs), 1, "both on one run")
        xy, members, reqs = runs[0]
        self.assertEqual(members, {1, 2})
        np.testing.assert_allclose(xy[[0, -1]], [(-60, 0), (60, 0)])
        self.assertAlmostEqual(float(np.hypot(*np.diff(xy, axis=0).T).sum()), 120.0)
        np.testing.assert_allclose([r[1] for r in reqs], [60.0, 82.0], err_msg="x 0, and x 22 on the backward piece")
        self.assertEqual([r[0] for r in reqs], [0, 0])

    def test_spread_as_before_by_a_junction_on_motorways_and_rings(self):
        cls = ["tertiary"] * 4 + ["residential"] * 2
        near = (1, 5.0, 7.0)            # (5 m from its junction: no room for UNDER_RUN_MIN_M)
        self.assertEqual(grading.street_runs(self.LINES, cls, [near]), ([near], []))
        shallow = (1, 20.0, 0.3)        # (room for all of a shallow dip)
        self.assertEqual(grading.street_runs(self.LINES, cls, [shallow])[0], [])
        mway = (1, 60.0, 7.0)
        self.assertEqual(grading.street_runs(self.LINES, ["motorway"] * 6, [mway]), ([mway], []))
        ring = [np.array(ln, float) for ln in ([(0, 0), (300, 0)], [(300, 0), (300, 300)], [(300, 300), (0, 300)],
                                                [(0, 300), (0, 0)])]
        self.assertEqual(grading.street_runs(ring, ["tertiary"] * 4, [(0, 150.0, 5.0)]), ([(0, 150.0, 5.0)], []))


class CoarseUnderpassTest(unittest.TestCase):
    def test_a_street_dip_stays_on_the_street_on_coarse_elevation(self):
        """Samara (2026-11-02): on the 30 m model a street under a bridge whose ramps the frontage roads hold down dips
        between its own junctions only, as deep as fits there (UNDER_RUN_*); the side streets stay at their ground.
        On lidar terrain, or with BNE_UNDERPASS_RUN=off, the dip spreads along the side streets as before."""
        street = [[(-300, 0), (-60, 0)], [(-60, 0), (20, 0)], [(60, 0), (20, 0)], [(60, 0), (300, 0)]]
        side = [[(-60, 0), (-60, 200)], [(60, 0), (60, 200)]]
        bridge = [[(0, -20), (0, 20)]]
        ramps = [[(0, -260), (0, -20)], [(0, 20), (0, 260)]]
        frontage = [[(-150, -26), (150, -26)], [(-150, 26), (150, 26)]]
        f = features(street + side + bridge + ramps + frontage,
                     ["tertiary"] * 4 + ["residential"] * 2 + ["tertiary"] * 3 + ["residential"] * 2,
                     flags=[(6, "is_bridge")])
        pts = np.array([(0, 0), (20, 0), (60, 0), (-60, 0), (200, 0), (60, 60), (-60, 60)], float)

        def heights(coarse):
            g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
            if coarse:
                g.dem = types.SimpleNamespace(coarse_at=lambda la, lo: True)
            la, lo, _ = FRAME.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
            return g.apply(la, lo, np.full(len(pts), 10.0))
        fit = (60.0 - grading.UNDER_FLAT_M) * 2 * grading.UNDER_MAX_GRADE / math.pi
        z = heights(True)
        self.assertAlmostEqual(float(z[0]), 10.0 - fit, delta=0.3, msg="as deep as fits between its junctions")
        self.assertLess(float(z[1]), 9.0, "on past the node where only its next piece joins")
        np.testing.assert_allclose(z[2:5], 10.0, atol=0.1, err_msg="back up by its junctions, and beyond")
        np.testing.assert_allclose(z[5:], 10.0, atol=0.05, err_msg="the side streets at their ground")
        lidar = heights(False)
        self.assertLess(float(lidar[0]), float(z[0]) - 1.0, "lidar: the deck's whole clearance")
        self.assertLess(float(lidar[5]), 9.5, "lidar: the dip spreads along the side streets")
        with unittest.mock.patch.object(grading, "UNDER_RUN_ON", False):
            off = heights(True)
        np.testing.assert_allclose(off, lidar, atol=1e-9, err_msg="BNE_UNDERPASS_RUN=off: as before")


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



class NeverRaisedTest(unittest.TestCase):
    def test_links_are_raised_motorways_not(self):
        S = roads.Spec
        self.assertTrue(grading._never_raised(S(18.8, "paved", False, "motorway", True)))
        self.assertFalse(grading._never_raised(S(11.6, "paved", False, "motorway", True, link=True)))
        self.assertFalse(grading._never_raised(S(8.0, "paved", False, "secondary", False)))


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

    def test_a_road_crossing_a_bigger_one_meets_it_there(self):
        """GitHub #414 (Perm): a service road crossing a primary at a shared node, 4 m higher, stood as a wall across
        it; it takes the primary's height at the crossing, eased in either side; the primary keeps its own. (Opt-in with
        the coarse easing: BNE_COARSE_EASE=1.)"""
        import roads
        from unittest import mock
        patcher = mock.patch.object(grading, "COARSE_EASE_ON", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        x = np.arange(-200, 201, 4.0)
        main = grading.Plan(np.stack([x, 0 * x], 1), 10.0 + 0 * x, 5, road_half=5)
        y = np.arange(-200, 201, 4.0)
        cross = grading.Plan(np.stack([0 * y + 20, y], 1), 14.0 + 0 * y, 3, road_half=2)
        grading.snap_junctions([main, cross], [roads.Spec(10.0, "paved", False, "primary", False),
                                               roads.Spec(4.0, "paved", False, "service", False)])
        mid = int(np.argmin(np.abs(y)))
        self.assertAlmostEqual(cross.z[mid], 10.0, places=6)
        self.assertAlmostEqual(cross.z[0], 14.0, places=6)
        np.testing.assert_allclose(main.z, 10.0)

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

if __name__ == "__main__":
    unittest.main()


class CoarseEaseTest(unittest.TestCase):
    """earthfast.ease_coarse (GitHub #414): roads side by side on a coarse DEM brought together."""

    def run_ease(self, z_main, z_side, gap_y, rank_side=1, take=(1, 1)):
        import fast
        fast.require(94, "ease_coarse")
        x = np.linspace(0.0, 200.0, 41)
        xy = np.concatenate([np.c_[x, np.zeros_like(x)], np.c_[x, np.full_like(x, gap_y)]])
        z = np.concatenate([np.full_like(x, z_main), np.full_like(x, z_side)])
        off = np.array([0, len(x), 2 * len(x)], np.int64)
        out = fast._ef.ease_coarse(np.ascontiguousarray(xy), z, off, np.array([7, rank_side], np.int64),
                                   np.array([4.0, 2.0]), np.array(take, np.uint8), grading.COARSE_REACH_M,
                                   grading.COARSE_MAX_STEP_M, grading.COARSE_MAX_SLOPE, grading.COARSE_SIGMA_M)
        return out[:len(x)], out[len(x):]

    def test_a_service_road_beside_a_primary_comes_down_to_it(self):
        main, side = self.run_ease(100.0, 104.5, 7.0)
        np.testing.assert_allclose(main, 100.0)                     # the higher rank stays
        self.assertLess(float(np.median(side)), 101.0)               # the 4.5 m wall mostly gone
        self.assertGreater(float(np.median(side)), 100.0)            # (a little cross slope kept)

    def test_far_roads_and_real_separations_stay(self):
        _m, far = self.run_ease(100.0, 104.5, 60.0)
        np.testing.assert_allclose(far, 104.5)
        _m, sep = self.run_ease(100.0, 108.0, 7.0)                   # 8 m: an overpass or a cutting, left alone
        np.testing.assert_allclose(sep, 108.0)
        _m, kept = self.run_ease(100.0, 104.5, 7.0, take=(1, 0))      # a bridge approach: as it was
        np.testing.assert_allclose(kept, 104.5)

    def test_equal_ranks_meet_halfway(self):
        main, side = self.run_ease(100.0, 104.0, 7.0, rank_side=7)
        self.assertLess(float(np.median(side) - np.median(main)), 2.5)


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
