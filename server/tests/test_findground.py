"""Find the ground (findground.py): which of two crossing ways leaves it, and the railways' side of it: a rail bridge
over a road on the ground rises over the road by the road's headroom, whatever the elevation and the bridge's length
(the map wins), and a viaduct the map cuts into parts stands on the line between its own ends at their joints."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import findground  # noqa: E402
import gradelaw  # noqa: E402
import gradeshapes  # noqa: E402
import grading  # noqa: E402
import interchanges  # noqa: E402
import railways  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402

G, B, T = findground.GROUND, findground.BRIDGE, findground.TUNNEL
Z0 = 344.0
DEPTH = railways.DECK_D - railways.BALLAST_H


def level(xy):
    return np.full(len(xy), Z0)


class LeavesTest(unittest.TestCase):
    def test_a_tunnel_under_anything_goes_down(self):
        for upper in (G, B, T):
            self.assertEqual(findground.leaves(T, upper), "lower", upper)

    def test_a_bridge_over_a_way_that_is_no_tunnel_goes_up(self):
        for lower in (G, B):
            self.assertEqual(findground.leaves(lower, B), "upper", lower)

    def test_ways_with_no_structure_over_the_other_meet_at_grade(self):
        for lower in (G, B):
            for upper in (G, T):
                self.assertIsNone(findground.leaves(lower, upper), (lower, upper))

    def test_the_shapes_know_every_member_kind(self):
        self.assertEqual(set(gradeshapes.WAY), set(gradeshapes.LEVEL_BAND))
        self.assertEqual({gradeshapes.WAY[k] for k in ("deck", "rail_deck")}, {B})
        self.assertEqual({gradeshapes.WAY[k] for k in ("ground", "rail")}, {G})
        self.assertEqual(gradeshapes.WAY["tunnel"], T)


class _RailOverRoad(unittest.TestCase):
    """A rail bridge over a primary road, both on level ground. Whichever of the two moves is findground's cost law (the
    bridge climbs the lack at the track's grade, railways.GRADE, over its own run first and only the rest is fill; the
    road dips it and climbs out at its own, a cut each side): a 20 m bridge has 10 m of run each way where 5.8 m at 5 %
    asks 117, so 214 m of fill against the primary's 130 m of cut (9 %): the road goes down under the track, which stays
    on its ground. A viaduct of 300 m climbs it in its own run, free: the track rises over the road, which stays."""

    LENGTH = 20.0

    def setUp(self):
        half = self.LENGTH / 350.0 / 2.0
        self.f = features([[(-50.0, 100.0), (300.0, 100.0)], [(120.0, 20.0), (120.0, 180.0)]],
                          ["standard_gauge", "primary"], subtypes=["rail", "road"],
                          flags=[(0, "is_bridge", 170.0 / 350.0 - half, 170.0 / 350.0 + half)])
        self.box = roads.tile_bbox(0, 0)
        self.rails = railways.parts(FRAME, [self.f], self.box, clip=True)

    def beds(self, rise):
        out = []
        for (_fid, _cls, kind, xy), (zb, _zg) in zip(self.rails, railways.profile(self.rails, level, rise)):
            out.append((kind, xy, zb))
        return out

    def over_the_road(self, rise):
        return [float(zb[np.abs(xy[:, 0] - 120.0) <= rise[0][1]].min()) for kind, xy, zb in self.beds(rise) if kind == "bridge"]


class RailBridgeOverPlainRoadTest(_RailOverRoad):
    """The 20 m bridge: findground's law, the road goes down."""

    def test_the_map_wins_a_short_bridge_on_level_ground_is_a_bridge(self):
        self.assertIn("bridge", {kind for _f, _c, kind, _xy in self.rails})

    def test_the_cost_law_says_the_road_goes_down_under_a_short_bridge(self):
        rise = railways.plain_underpasses(FRAME, [self.f], self.box, self.rails)
        self.assertEqual(len(rise), 1)
        lack = rise[0].clear
        ends = (10.0, 10.0)
        self.assertGreater(findground.fill_run(lack, ends, railways.GRADE), findground.dip_run(lack, rise[0].grade))
        self.assertEqual(findground.leaves(G, B, lack, ends, railways.GRADE, rise[0].grade), "lower")
        for kind, xy, zb in self.beds(rise):
            self.assertLess(float(np.abs(zb - Z0).max()), 0.5, kind + ": the track stays on its ground")

    def test_without_the_rise_the_bridge_lies_on_the_ground(self):
        for kind, _xy, zb in self.beds([]):
            self.assertLess(float(np.abs(zb - Z0).max()), 0.5, kind)

    def test_a_signed_road_gets_its_limit_instead(self):
        sign = [(np.array([[120.0, 20.0], [120.0, 180.0]]), 3.2)]
        with mock.patch.object(railways, "height_limits", lambda frame, cells: sign):
            rise = railways.plain_underpasses(FRAME, [self.f], self.box, self.rails)
        self.assertEqual(len(rise), 1)
        self.assertAlmostEqual(rise[0].clear, float(roads.signed_road_clear(3.2, DEPTH)))
        self.assertLess(rise[0].clear, roads.deck_road_clear(DEPTH))

    def test_a_track_on_the_ground_under_the_bridge_gets_a_tracks_headroom(self):
        rails = [("a", "rail", "ground", np.array([[0.0, 100.0], [100.0, 100.0]])),
                 ("b", "rail", "bridge", np.array([[100.0, 100.0], [140.0, 100.0]])),
                 ("c", "rail", "ground", np.array([[140.0, 100.0], [240.0, 100.0]])),
                 ("d", "rail", "ground", np.array([[120.0, 0.0], [120.0, 200.0]]))]
        rise = railways.plain_underpasses(FRAME, [], self.box, rails)
        self.assertEqual(len(rise), 1, "the track under it; not the bridge's own track on from its ends")
        self.assertAlmostEqual(rise[0].clear, roads.deck_rail_clear(DEPTH))
        self.assertGreater(rise[0].clear, roads.deck_road_clear(DEPTH))
        self.assertLess(float(np.abs(rise[0][0][:, 0] - 120.0).max()), 1e-6)

    def test_no_bridge_no_rise(self):
        f = features([[(-50.0, 100.0), (300.0, 100.0)], [(120.0, 20.0), (120.0, 180.0)]],
                     ["standard_gauge", "primary"], subtypes=["rail", "road"])
        rails = railways.parts(FRAME, [f], self.box, clip=True)
        self.assertEqual(railways.plain_underpasses(FRAME, [f], self.box, rails), [])

    def test_a_road_in_its_own_tunnel_under_the_bridge_is_not_risen_over(self):
        f = features([[(-50.0, 100.0), (300.0, 100.0)], [(120.0, 20.0), (120.0, 180.0)]],
                     ["standard_gauge", "primary"], subtypes=["rail", "road"],
                     flags=[(0, "is_bridge", 160.0 / 350.0, 180.0 / 350.0), (1, "is_tunnel", 70.0 / 160.0, 90.0 / 160.0)])
        tun = railways.road_underpasses(FRAME, [f], self.box)
        self.assertEqual(len(tun), 1)
        rails = railways.parts(FRAME, [f], self.box, clip=True, underpasses=tun)
        self.assertEqual(railways.plain_underpasses(FRAME, [f], self.box, rails), [], "the tunnel goes down, the track stays")

    def test_nor_where_the_servers_roads_have_that_tunnel_as_an_open_road(self):
        # (gradesep: the road's short tunnel made ground, the track over it a bridge; the map's tunnel is still listed)
        under = [(np.array([[120.0, 90.0], [120.0, 110.0]]), 5.0)]
        self.assertEqual(railways.plain_underpasses(FRAME, [self.f], self.box, self.rails, tunnels=under), [])
        beside = [(np.array([[120.0, 150.0], [120.0, 170.0]]), 5.0)]
        self.assertEqual(len(railways.plain_underpasses(FRAME, [self.f], self.box, self.rails, tunnels=beside)), 1,
                         "a tunnel of the road further on is not under the bridge")


class RailViaductOverPlainRoadTest(_RailOverRoad):
    """The 300 m viaduct over the same road: it climbs the rise in its own run, so the track rises and the road stays."""
    LENGTH = 300.0

    def test_the_track_rises_over_the_road_by_the_laws_headroom(self):
        # (a viaduct: its own run carries the rise)
        rise = railways.plain_underpasses(FRAME, [self.f], self.box, self.rails)
        self.assertEqual(len(rise), 1)
        xy, half = rise[0]
        self.assertAlmostEqual(rise[0].clear, roads.deck_road_clear(DEPTH))
        self.assertLess(float(np.abs(xy[:, 0] - 120.0).max()), 0.5, "the road's own piece")
        self.assertAlmostEqual(float(xy[:, 1].min()), 100.0 - railways.RISE_PIECE_M, delta=0.5)
        self.assertAlmostEqual(float(xy[:, 1].max()), 100.0 + railways.RISE_PIECE_M, delta=0.5)
        self.assertGreater(half, railways.UNDERPASS_SIDE_M)
        over = self.over_the_road(rise)
        self.assertTrue(over)
        self.assertGreaterEqual(min(over), Z0 + rise[0].clear - 1e-6, "the road's headroom under the deck, over the road")
        ends = (150.0, 150.0)
        self.assertEqual(findground.leaves(G, B, rise[0].clear, ends, railways.GRADE, rise[0].grade), "upper")
        for _kind, xy, zb in self.beds(rise):
            run = np.hypot(*np.diff(xy, axis=0).T)
            self.assertLessEqual(float(np.abs(np.diff(zb) / np.maximum(run, 1e-9)).max()), railways.GRADE + 1e-6)


class RiseRampTest(unittest.TestCase):
    """The rise over a road (plain_underpasses) is carried by the track at railways.GRADE: the bridge's bed stands where
    the ground track's ends, with no step (measured at Perth's Tonkin Highway, 2026-11-18: the 5.25 m rise of a 24 m
    bridge ran 105 m out along each approach, every part's end equal at its joint; the 1 m ledge there is a motorway
    deck beside it, not the track), and it runs out on the ground's own bed, no further than clear / GRADE."""

    def line(self, x0, x1):
        x = np.arange(x0, x1 + 1e-9, railways.STEP_M)
        return np.c_[x, np.full(len(x), 100.0)]

    def setUp(self):
        self.clear = float(roads.deck_road_clear(DEPTH))
        # (a 120 m bridge, the road in its middle: 60 m of run to each end where the rise asks 117, so 114 m of fill
        # against the primary's 130 m of cut: the bridge rises, and carries the rest of its rise onto the ground track)
        self.rails = [("a", "rail", "ground", self.line(-100.0, 100.0)), ("b", "rail", "bridge", self.line(100.0, 220.0)),
                      ("c", "rail", "ground", self.line(220.0, 420.0))]
        piece = np.c_[np.full(21, 160.0), np.linspace(75.0, 125.0, 21)]
        self.rise = railways._Rise(piece, 6.0, self.clear, gradelaw.road_grade("primary"))
        (self.za, _), (self.zb, _), (self.zc, _) = railways.profile(self.rails, level, [self.rise])

    def test_the_bridge_and_its_approaches_meet_with_no_step(self):
        self.assertAlmostEqual(float(self.za[-1]), float(self.zb[0]), places=6)
        self.assertAlmostEqual(float(self.zb[-1]), float(self.zc[0]), places=6)

    def test_the_rise_is_over_the_road_by_its_headroom_and_never_steeper_than_the_grade_law(self):
        self.assertGreaterEqual(float(self.zb.min()), Z0 + self.clear - railways.GRADE * 60.0 - 1e-6)
        self.assertAlmostEqual(float(self.zb.max()), Z0 + self.clear, places=6)
        for xy, zb in zip((p[3] for p in self.rails), (self.za, self.zb, self.zc)):
            run = np.hypot(*np.diff(xy, axis=0).T)
            self.assertLessEqual(float(np.abs(np.diff(zb) / run).max()), railways.GRADE + 1e-6)

    def test_it_runs_out_along_the_track_at_the_grade_law_and_stops_on_the_ground(self):
        for zb in (self.zc, self.za[::-1]):
            joint = float(zb[0])
            d = np.arange(len(zb)) * railways.STEP_M
            want = np.maximum(Z0, joint - railways.GRADE * d)
            self.assertLess(float(np.abs(zb - want).max()), 1e-6, "GRADE per metre down to the ground, then the ground")
            self.assertAlmostEqual(float(zb[-1]), Z0, places=6)
        reach = (float(self.zc[0]) - Z0) / railways.GRADE
        self.assertLessEqual(reach, self.clear / railways.GRADE + 1e-6, "no longer than the rise needs")
        self.assertGreater(reach, (self.clear - railways.GRADE * 60.0) / railways.GRADE, "and the rise is carried in full")


class ViaductJointTest(unittest.TestCase):
    """A viaduct the map cuts in two over a valley: its joint is on the line between its ends, not on the valley's
    floor."""

    def test_the_joint_of_two_bridge_parts_is_on_the_line_between_the_structures_ends(self):
        def valley(xy):
            return Z0 - 12.0 * ((xy[:, 0] > 105.0) & (xy[:, 0] < 295.0))

        def line(x0, x1):
            x = np.arange(x0, x1 + 1e-9, railways.STEP_M)
            return np.c_[x, np.full(len(x), 100.0)]
        rails = [("a", "rail", "ground", line(0.0, 100.0)), ("b", "rail", "bridge", line(100.0, 200.0)),
                 ("c", "rail", "bridge", line(200.0, 300.0)), ("d", "rail", "ground", line(300.0, 400.0))]
        prof = railways.profile(rails, valley, [])
        zb, zg = prof[1]
        self.assertAlmostEqual(float(zg[-1]), Z0 - 12.0, places=6, msg="the joint is over the valley's floor")
        self.assertLess(abs(float(zb[-1]) - Z0), 0.5, "and stands with the viaduct's ends")
        self.assertLess(abs(float(prof[2][0][0]) - float(zb[-1])), 1e-6, "both parts meet there")
        for (_f, _c, kind, _xy), (b, _g) in zip(rails, prof):
            if kind == "bridge":
                self.assertLess(float(np.abs(b - Z0).max()), 0.5, "level from end to end")


class ViaductSwitchTest(unittest.TestCase):
    """A branch bridge leaving the middle of a viaduct the map has in one part (a crossover or a switch on it, the through
    track not cut there): its end joins no node of the chain graph, so it read the ground under the viaduct and stood
    6 m under the through track (Zurich's Duttweilerbruecke viaduct, both ends of a 118 m crossover: 6.0 and 6.2 m).
    The switch is a node of the structure (railways._switch_split): one height at the joint."""

    @staticmethod
    def valley(xy):
        return Z0 - 12.0 * ((xy[:, 0] > 105.0) & (xy[:, 0] < 295.0) & (np.abs(xy[:, 1] - 100.0) < 100.0))

    @staticmethod
    def rails(x0=160.0):
        """The viaduct in one part, ground either side; a crossover leaving it at x0 and rejoining it 40 m on, its
        middle 10 m off the line."""
        def line(x0, y0, x1, y1):
            n = int(round(float(np.hypot(x1 - x0, y1 - y0)) / railways.STEP_M)) + 1
            return np.c_[np.linspace(x0, x1, n), np.linspace(y0, y1, n)]
        cross = np.vstack([line(x0, 100.0, x0 + 20.0, 110.0), line(x0 + 20.0, 110.0, x0 + 40.0, 100.0)[1:]])
        return [("a", "rail", "ground", line(0.0, 100.0, 100.0, 100.0)), ("v", "rail", "bridge", line(100.0, 100.0, 300.0, 100.0)),
                ("d", "rail", "ground", line(300.0, 100.0, 400.0, 100.0)), ("x", "rail", "bridge", cross)]

    def test_a_crossover_leaving_a_viaduct_stands_at_the_viaducts_height_at_both_joints(self):
        for x0 in (160.0, 161.0):                       # (on a sample of the viaduct, and between two)
            rails = self.rails(x0)
            prof = railways.profile(rails, self.valley, [])
            zv, zg = prof[1]
            self.assertAlmostEqual(float(zg[int(x0 - 100.0) // 2]), Z0 - 12.0, places=6, msg="over the valley's floor")
            self.assertLess(float(np.abs(zv - Z0).max()), 0.5, "the viaduct stands level between its ends")
            zx = prof[3][0]
            self.assertLess(float(np.abs(zx - Z0).max()), 0.5, "the crossover with it, not down on the floor")
            self.assertEqual([len(z) for z, _g in prof], [len(r[3]) for r in rails], "a height per point of each part")
            self.assertTrue(all(np.isfinite(z).all() for z, _g in prof))

    def test_a_branch_end_beside_the_viaduct_is_not_a_switch(self):
        rails = self.rails()
        rails[3] = ("x", "rail", "bridge", rails[3][3] + np.array([0.0, 2.0]))      # (its ends 2 m off the viaduct)
        self.assertIsNone(railways._switch_split(rails))


class PopoliTest(unittest.TestCase):
    """Popoli (42.1712 N 13.8250 E, fixtures/interchange_popoli_ss17_underpass.npz): the SS17 in a walled trench under
    the two tracks of the Roma - Pescara line and a service road under them 45 m on, each a short tunnel in the map,
    the tracks with no bridge of their own there (the server spans them: gradesep), the ground level across both."""

    _beds = None

    @classmethod
    def beds(cls):
        """[bed - ground m] of each track at each road it crosses over (two tracks over two roads), after asserting the
        roads' side (they go down, and clear the deck); the replay graded once."""
        if cls._beds is not None:
            return cls._beds
        rep = interchanges.load("popoli_ss17_underpass")
        tc = cls("test_the_roads_go_down_under_the_tracks_and_a_track_stays_on_its_ground")
        with mock.patch.object(revisit, "ON", False):
            md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": rep.cells.get(str(key))}))
            store = grading.PlanStore(md, rep.sample)
            store.frame_of = lambda region: rep.frame
            grader = grading.Grader(rep.frame, md, rep.sample, store=store)
            planner = grading._Planner(rep.frame, md, lambda la, lo, *a: rep.sample(la, lo))
            box = (-300.0, -300.0, 300.0, 300.0)
            cells = [md.cell(k).layers.get("roads") for k in railways.cells_near(rep.frame, box, 0.0)]
            tun = railways.road_underpasses(rep.frame, cells, box, 0.0)
            rails = railways.parts(rep.frame, cells, box, reach=0.0, clip=True, underpasses=tun)
            tc.assertEqual(railways.plain_underpasses(rep.frame, cells, box, rails, 0.0, tun), [],
                           "no track rises: the roads under them are in their own tunnels")
            bridges = [xy for _f, _c, kind, xy in rails if kind == "bridge"]
            beds = []
            for hits in roads._crossings_by(bridges, [line for line, _half in tun]):
                for _part, _arc, pt in hits:
                    la, lo, _ = rep.frame.enu_to_geodetic(np.array([pt[0]]), np.array([pt[1]]), 0.0)
                    ground = float(rep.sample(la, lo)[0])
                    road = float(grader.apply(la, lo, rep.sample(la, lo), grading.ROAD_LIFT)[0])
                    bed = float(np.asarray(planner._track_bed(np.asarray(pt, np.float64)[None]))[0])
                    tc.assertLess(road, ground - 3.0, "the road goes down")
                    tc.assertGreaterEqual(bed - road, roads.MIN_BRIDGE_HEADROOM_M + DEPTH - 0.05, "and clears the deck")
                    beds.append(bed - ground)
        tc.assertEqual(len(beds), 4, "two tracks over two roads")
        PopoliTest._beds = beds
        return beds

    def test_the_roads_go_down_under_the_tracks_and_a_track_stays_on_its_ground(self):
        self.assertLess(abs(min(self.beds())), 0.3, "a track over them stays on its ground")

    def test_both_tracks_stay_on_their_ground(self):
        """In the real place both tracks are on one level. The second track's bed stood 5.9 m (at the service road, 4.3 m at
        the SR17) over the ground at railways.GRADE 2 %: no road or track rise applies here (plain_underpasses gives none);
        the lift was the railway's grade law meeting the elevation along the line (measured step by step, 2026-11-18). The
        line's highest bridge end, at (542, 550) in the replay's frame (270.7 m), run out at 2 % over the 850 m of track to
        the tunnels stands at 254.2 m; the elevation there is 248.3 m: it falls 22.4 m, 2.6 % on average (10 % over one 160 m
        stretch of hillside), so the bridge at (313, 397), 280 m on, was held 8 m over its ground, and the lift
        (railways._stepless) ran on along the ground track for 480 m to the tunnels. At 2.8 % both tracks stay on their
        ground (+0.0 to +0.5 m); at the one grade law of a track, railways.GRADE (5 %, the user, 2026-11-18: "rail grade to
        5% is a car game after all"): +0.04 and +0.01 m, both modes. What it costs elsewhere (the rail bridges' bed against
        the survey, 5th percentile of bed - real): the Letzigraben flyover's ramp better (Europabruecke +2.6, 3 m over the
        real, to -2.8 .. -0.3), the viaducts that stand higher than their crossings ask, lower (tests/test_truth.py)."""
        self.assertLess(max(abs(b) for b in self.beds()), 0.3)


class RailGradeLawTest(unittest.TestCase):
    """The steepest a track climbs was decided in three places (railways.GRADE 2 % for the decks' ramps, the approaches and
    the lifts carried along the line; gradeshapes.max_grade 2.5 % for the shapes' laws; railroadseam.SPREAD_GRADE 2 %, a
    literal with 'railways.GRADE' in its comment): one value now, read by all."""

    def test_one_grade_law_for_a_track(self):
        import railroadseam
        track = SimpleNamespace(kind="rail", cls="rail", kmh=None)
        self.assertEqual(gradeshapes.max_grade(track), railways.GRADE)
        self.assertEqual(gradeshapes.max_grade(SimpleNamespace(kind="rail_deck", cls="rail", kmh=120)), railways.GRADE)
        self.assertFalse(hasattr(railroadseam, "SPREAD_GRADE"), "a copy of the track's grade")
        got = []
        with mock.patch("fast.rail_seam_couple", lambda *a: got.append(a[-1]) or None):
            railroadseam.couple(np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]]), np.zeros(3), 2.0,
                                [(np.array([[0.0, 5.0], [20.0, 5.0]]), np.ones(2), 3.0)])
        self.assertEqual(got, [railways.GRADE], "the seam's move runs out along the track at the track's grade")

    def test_light_rail_climbs_the_same_law(self):
        # (railways.LINE_GRADE held light rail's 5 % apart from GRADE's 2 %: the law is one now, for every class)
        self.assertFalse(hasattr(railways, "LINE_GRADE"))


if __name__ == "__main__":
    unittest.main()
