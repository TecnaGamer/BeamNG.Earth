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


class RailBridgeOverPlainRoadTest(unittest.TestCase):
    """A 20 m rail bridge over a primary road, both on level ground."""

    def setUp(self):
        self.f = features([[(-50.0, 100.0), (300.0, 100.0)], [(120.0, 20.0), (120.0, 180.0)]],
                          ["standard_gauge", "primary"], subtypes=["rail", "road"],
                          flags=[(0, "is_bridge", 160.0 / 350.0, 180.0 / 350.0)])
        self.box = roads.tile_bbox(0, 0)
        self.rails = railways.parts(FRAME, [self.f], self.box, clip=True)

    def beds(self, rise):
        out = []
        for (_fid, _cls, kind, xy), (zb, _zg) in zip(self.rails, railways.profile(self.rails, level, rise)):
            out.append((kind, xy, zb))
        return out

    def test_the_map_wins_a_short_bridge_on_level_ground_is_a_bridge(self):
        self.assertIn("bridge", {kind for _f, _c, kind, _xy in self.rails})

    def test_the_track_rises_over_the_road_by_the_laws_headroom(self):
        rise = railways.plain_underpasses(FRAME, [self.f], self.box, self.rails)
        self.assertEqual(len(rise), 1)
        xy, half = rise[0]
        self.assertAlmostEqual(rise[0].clear, roads.deck_road_clear(DEPTH))
        self.assertLess(float(np.abs(xy[:, 0] - 120.0).max()), 0.5, "the road's own piece")
        self.assertAlmostEqual(float(xy[:, 1].min()), 100.0 - railways.RISE_PIECE_M, delta=0.5)
        self.assertAlmostEqual(float(xy[:, 1].max()), 100.0 + railways.RISE_PIECE_M, delta=0.5)
        self.assertGreater(half, railways.UNDERPASS_SIDE_M)
        over = [float(zb[np.abs(xy[:, 0] - 120.0) <= half].min()) for kind, xy, zb in self.beds(rise) if kind == "bridge"]
        self.assertTrue(over)
        self.assertGreaterEqual(min(over), Z0 + rise[0].clear - 1e-6, "the road's headroom under the deck, over the road")
        for _kind, xy, zb in self.beds(rise):
            run = np.hypot(*np.diff(xy, axis=0).T)
            self.assertLessEqual(float(np.abs(np.diff(zb) / np.maximum(run, 1e-9)).max()), railways.GRADE + 1e-6)

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


class PopoliTest(unittest.TestCase):
    """Popoli (42.1712 N 13.8250 E, fixtures/interchange_popoli_ss17_underpass.npz): the SS17 in a walled trench under
    the two tracks of the Roma - Pescara line and a service road under them 45 m on, each a short tunnel in the map,
    the tracks with no bridge of their own there (the server spans them: gradesep), the ground level across both."""

    def test_the_roads_go_down_under_the_tracks_and_a_track_stays_on_its_ground(self):
        rep = interchanges.load("popoli_ss17_underpass")
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
            self.assertEqual(railways.plain_underpasses(rep.frame, cells, box, rails, 0.0, tun), [],
                             "no track rises: the roads under them are in their own tunnels")
            bridges = [xy for _f, _c, kind, xy in rails if kind == "bridge"]
            beds = []
            for hits in roads._crossings_by(bridges, [line for line, _half in tun]):
                for _part, _arc, pt in hits:
                    la, lo, _ = rep.frame.enu_to_geodetic(np.array([pt[0]]), np.array([pt[1]]), 0.0)
                    ground = float(rep.sample(la, lo)[0])
                    road = float(grader.apply(la, lo, rep.sample(la, lo), grading.ROAD_LIFT)[0])
                    bed = float(np.asarray(planner._track_bed(np.asarray(pt, np.float64)[None]))[0])
                    self.assertLess(road, ground - 3.0, "the road goes down")
                    self.assertGreaterEqual(bed - road, roads.MIN_BRIDGE_HEADROOM_M + DEPTH - 0.05, "and clears the deck")
                    beds.append(bed - ground)
        self.assertEqual(len(beds), 4, "two tracks over two roads")
        self.assertLess(abs(min(beds)), 0.3, "a track over them stays on its ground")


if __name__ == "__main__":
    unittest.main()
