"""A road's end at a tunnel's mouth is not ground the elevation measures: the node stands on the portal's face, and the
elevation there is the hillside over the tube. Goeschenen, the road into the Gotthard tunnel: the node read 1070.4 m,
the road 2 m before it 1065.6 and the survey's own 1065.2; the road's plan held it, the tunnel took its portal from it
and the roads over the tunnel were raised to clear it (the road filled 8.3 m). The find-ground rule: the ground at a
tunnel's end is the road's own level where it meets the mouth, not the face over it (roads.fill_mouth)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gradelaw  # noqa: E402
import grading  # noqa: E402
import mapcells  # noqa: E402
import roads  # noqa: E402
from test_grading import FakeMD  # noqa: E402
from test_roads import features, FRAME, LAT, LON  # noqa: E402

STEP = 2.0
ROAD_Z = 1065.0                 # m at the mouth (x = 0)
GRADE = -0.02                   # the road falls toward the mouth ... (x < 0 is the approach, the tunnel is x > 0)
FACE_M = 3.0                    # m before the node the hillside's face begins to climb
FACE_UP = 2.0                   # m of face per m in front of it: 6 m at the node


def road_z(x):
    """The road's own level along its approach (x <= 0) and the hill's face over the mouth."""
    return ROAD_Z + GRADE * -np.asarray(x, np.float64)


def hillside(x):
    """What the elevation says: the road on the approach, the portal's face from FACE_M before the node on."""
    x = np.asarray(x, np.float64)
    return road_z(x) + FACE_UP * np.clip(x + FACE_M, 0.0, None)


class FillMouthTest(unittest.TestCase):
    def test_the_face_is_not_the_road(self):
        d = np.arange(0.0, 120.0, STEP)             # (from the mouth outward: samples 0 .. 119 m back along the road)
        z = hillside(-d)
        self.assertGreater(z[0] - road_z(0.0), 5.0, "the elevation at the node is the face, 6 m over the road")
        filled = roads.fill_mouth(z[::-1], True, STEP)      # (the last sample is the mouth)
        self.assertAlmostEqual(filled[-1], ROAD_Z, delta=0.05)
        want = road_z(-d)[::-1]
        self.assertTrue(np.allclose(filled, want, atol=0.05), "the road's own line to the end")
        self.assertTrue(np.allclose(roads.fill_mouth(z, False, STEP), road_z(-d), atol=0.05), "the same from the start")

    def test_a_road_with_nothing_wrong_is_left_as_it_is(self):
        d = np.arange(0.0, 120.0, STEP)
        z = road_z(-d)
        self.assertTrue(np.allclose(roads.fill_mouth(z, False, STEP), z, atol=1e-9))

    def test_too_little_road_to_read_a_grade_from(self):
        z = np.array([1070.4, 1065.6, 1065.1, 1065.0, 1064.9])
        self.assertIs(roads.fill_mouth(z, False, STEP), z)


class PlanTest(unittest.TestCase):
    def _plan(self, mouth):
        xs = np.arange(-300.0, 0.1, STEP)
        xy = np.c_[xs, np.zeros(len(xs))]
        la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)

        def elev(la_, lo_):
            e, _, _ = FRAME.geodetic_to_enu(la_, lo_, 0.0)
            return hillside(e)
        spec = roads.Spec(10.0, "paved", False, "trunk")
        return grading.plan_roads(FRAME, [(np.c_[lo, la], spec)], elev, mouth=mouth, cuts=False)[0]

    def test_the_plan_ends_at_the_roads_level_at_a_mouth(self):
        held = self._plan([(False, True)])
        self.assertAlmostEqual(float(held.z[-1]), ROAD_Z, delta=0.15, msg="the road's own level at the mouth")
        near = held.xy[:, 0] > -60.0
        self.assertLess(float(np.abs(held.z[near] - road_z(held.xy[near, 0])).max()), 0.15, "the road's line to its end")

    def test_without_a_mouth_it_reads_the_face(self):
        # (what every consumer read before: pins, the tunnel's portal, the roads over it)
        face = self._plan(None)
        self.assertGreater(float(face.z[-1]), ROAD_Z + 5.0)


class PortalGroundTest(unittest.TestCase):
    """The ground a tunnel's portal stands on where no road joins it (TunnelProfiles._ends_ground): read the road's own
    way, outward from the mouth, not at the node."""

    def test_the_portal_is_at_the_road_level_not_the_face(self):
        t = roads.TunnelProfiles(FRAME, lambda key: None, None)

        def raw(la, lo):
            e, _, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return hillside(e)
        t.raw = raw
        asked = []

        def ends(la, lo, base=None):
            asked.append(base is not None)
            return raw(la, lo) if base is None else base
        t.ends = ends
        xy = np.array([[0.0, 0.0]])
        out = np.array([[-1.0, 0.0]])
        z = float(np.asarray(t._ends_ground(xy, out))[0])
        self.assertTrue(asked and asked[-1], "the ground read outward goes to the roads' grading as the portal's base")
        self.assertAlmostEqual(z, ROAD_Z, delta=0.1)


class GraderTest(unittest.TestCase):
    """A road into a tunnel in a hillside, as the grader plans it from the map: the plan's last sample at the mouth is
    the road's level, and the mouth is found from the tunnel's own end."""

    def setUp(self):
        import roadz
        from unittest import mock
        p = mock.patch.object(roadz, "PROVIDER", None)
        p.start()
        self.addCleanup(p.stop)

    def test_a_road_ending_at_a_tunnel_leads_into_it(self):
        f = features([[(-300, 0), (0, 0)], [(0, 0), (400, 0)]], ["trunk", "trunk"],
                     flags=[(1, "is_tunnel", 0.0, 1.0)])

        def elev(la, lo):
            e, _, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            return hillside(e)
        g = grading.Grader(FRAME, FakeMD(f), elev)
        ground = roads.ground_parts(f, 0)
        self.assertEqual(len(ground), 1)
        la, lo = (np.array([v]) for v in (LAT, LON))
        pl = g.store.planners_of(la, lo)[0][0] if g.store is not None else g
        flags = pl._mouth_ends([(ground[0], None)])
        self.assertEqual(flags.tolist(), [[False, True]], "its end at the tunnel's start; its start nothing")
        plans = g._cell_plans(mapcells.cell_of(LAT, LON))
        mine = [p for fid, p in plans if str(fid) == str(f.ids[0])]
        self.assertEqual(len(mine), 1)
        at = int(np.argmin(np.abs(mine[0].xy[:, 0])))
        self.assertLess(abs(float(mine[0].xy[at, 0])), 3.0, "the plan reaches the mouth")
        self.assertAlmostEqual(float(mine[0].z[at]), ROAD_Z, delta=0.3, msg="the road's level, not the face over it")


class ArchFootprintTest(unittest.TestCase):
    """A road's cover over a low arch is asked over the tube's own footprint (its width and walls), not the soil beside
    it that the terrain gets (Zurich Brunau: the motorway 17 m from the tube's axis, its shoulder 0.7 m from the wall,
    lifted 5.8 m to the vault's cover)."""

    def _arch(self, **kw):
        xy = np.c_[np.arange(0.0, 101.0, 4.0), np.zeros(26)]
        spec = roads.Spec(14.0, "paved", False, "trunk")
        parts = [("t#1", spec, xy, np.full(len(xy), 100.0), None)]
        return roads.arch_parts(parts, {"t#1": 5.0}, {"t#1": (False, False)}, **kw)

    def test_beside_the_tube_is_not_over_it(self):
        x = np.full(4, 50.0)
        y = np.array([0.0, 8.0, 8.7, 10.5])         # (the tube: 7 m half width + 1.2 m wall = 8.2; the soil to 9.2)
        ground = np.full(4, 100.0)
        tube = roads.arch_raise_over(self._arch(side=0.0), x, y, ground)
        self.assertTrue((tube[:2] > 105.0).all(), "over the tube: the vault and its soil")
        self.assertTrue(np.allclose(tube[2:], ground[2:]), "beside it: nothing asked of a road there")
        soil = roads.arch_raise_over(self._arch(), x, y, ground)
        self.assertGreater(float(soil[2]), 105.0, "the terrain still gets its soil beside the tube")
        self.assertAlmostEqual(float(soil[3]), 100.0)

    def test_a_road_at_the_tubes_level_is_not_asked_to_cover_it(self):
        # Porte Maillot: slip roads 0.3 m over the floor, 9 m off the line of a 5 m tube on the edge of its footprint,
        # asked 5.9 m up onto the vault's cover (6 m over the road a deck held: bounds that cannot hold)
        x = np.full(3, 50.0)
        y = np.full(3, 4.0)
        ground = np.full(3, 100.0)
        got = roads.arch_cover_over(self._arch(side=0.0), x, y, np.array([100.3, 103.5, 103.7]))
        self.assertFalse(np.isfinite(got[0]) or np.isfinite(got[1]), "under TUNNEL_LOW_H over the floor: beside it")
        self.assertAlmostEqual(float(got[2]), 105.0 + roads.ARCH_SOIL_MIN, places=3)
        self.assertEqual(ground.shape, got.shape)


class BoreDipTest(unittest.TestCase):
    """A tunnel under anything goes down: the tube's floor leaves room for the lowest tube and the arch and soil under
    a road over it, not only where the lines cross (Porte Maillot's Peripherique tunnels 4.5-5 m under slip roads over
    their flanks, 5 m tubes through them)."""

    def setUp(self):
        self.s = np.arange(0.0, 101.0, 4.0)
        self.floor = np.full(len(self.s), 100.0)
        self.kinds = np.full(len(self.s), 2)
        self.cover = np.full(len(self.s), 9.0)

    def test_a_road_too_low_over_the_floor_dips_the_floor_under_it(self):
        dips = roads.bore_dips([48.0, 49.0], [104.6, 104.9], self.s, self.floor, 5.0)
        self.assertEqual([q for q, _z in dips], [12])
        self.assertAlmostEqual(dips[0][1], 104.6 - roads.BORE_FIT_M - 5.0)

    def test_a_road_with_room_leaves_the_floor_and_the_portals_are_not_dipped(self):
        self.assertEqual(roads.bore_dips([48.0], [106.0], self.s, self.floor, 5.0), [])
        got = roads.bore_dips([0.0, 100.0], [101.0, 101.0], self.s, self.floor, 5.0)
        self.assertEqual([q for q, _z in got], [1, len(self.s) - 2])

    def test_the_floor_goes_down_from_a_portal_no_steeper_than_the_roads_grade(self):
        # (a road 8 m from a portal wants the floor 1.1 m down there: 14 %, over what the road may hold)
        grade = gradelaw.road_grade("motorway")
        dips = roads.bore_dips([8.0, 50.0], [104.6, 104.6], self.s, self.floor, 5.0, (100.0, 100.0), grade)
        self.assertEqual([q for q, _z in dips], [2, 12])
        self.assertAlmostEqual(dips[0][1], 100.0 - grade * 8.0)
        self.assertAlmostEqual(dips[1][1], 104.6 - roads.BORE_FIT_M - 5.0, msg="far from both portals: the whole dip")
        self.assertLessEqual(abs(dips[0][1] - 100.0) / 8.0, grade + 1e-9)

    def test_only_a_road_over_a_covered_tube_counts(self):
        arc, zr = np.array([20.0, 40.0, 60.0]), np.array([104.0, 100.5, 104.0])
        kinds = self.kinds.copy()
        kinds[15:] = 0                                     # (open from 60 m)
        on = roads.roads_over_tube(arc, zr, self.s, self.floor, np.full(len(self.s), 1.0), kinds)
        self.assertEqual(on.tolist(), [True, False, False], "over it; at its level; where it is open")
        # (the ground over a thin cover: a road on an embankment the elevation does not show still stands over it)
        on = roads.roads_over_tube(arc, zr, self.s, self.floor, np.full(len(self.s), 1.0), self.kinds)
        self.assertEqual(on.tolist(), [True, False, True])
        on = roads.roads_over_tube(arc, zr, self.s, self.floor, self.cover, self.kinds)
        self.assertEqual(on.tolist(), [True, True, True], "under deep ground the tube is under it however low the road")


LIP_Z = 100.0                   # m: the road's level where the tunnel meets the bridge and where the bridge meets the ground
GORGE_M = 60.0                  # m the bridge spans (x = 0 .. 60)
GORGE_DEPTH = 15.0


def gorge(x):
    """The ground of a road in a tunnel under a hill (x < 0: 100 m at both ends, 140 m over the middle) that comes out
    on a bridge over a gorge (0 < x < 60: walls 6 m wide, the floor 15 m down) and goes on level on the other side."""
    x = np.asarray(x, np.float64)
    hill_ = np.where((x < 0.0) & (x > -300.0), 40.0 * (1.0 - ((x + 150.0) / 150.0) ** 2), 0.0)
    depth = GORGE_DEPTH * np.clip(np.minimum(x, GORGE_M - x) / 6.0, 0.0, 1.0) * ((x > 0.0) & (x < GORGE_M))
    return LIP_Z + hill_ - depth


class MouthOverDeckTest(unittest.TestCase):
    """The road's level at a mouth a bridge goes on from (roads.mouth_over_deck): the structure's own, not the ground
    under the deck, which over a gorge is the gorge (Galleria Casagranda: the line the ground read outward gave was
    the gorge's wall and floor, 58.0 where the elevation at the node was 72.2 and the deck's abutment stood)."""

    def test_a_deck_over_a_gorge_is_on_the_line_to_the_far_lip(self):
        s = np.arange(8.0, 52.1, STEP)
        z = roads.mouth_over_deck(LIP_Z, 300.0, [(LIP_Z, GORGE_M)], s, gorge(s))
        self.assertAlmostEqual(z, LIP_Z, delta=0.01, msg="the lip, level with the tunnel's other end and the far lip")
        d = np.arange(0.0, 41.0, STEP)
        self.assertLess(roads.fill_mouth(gorge(d), False, STEP)[0], LIP_Z - 10.0, "what the ground read outward gave")

    def test_a_deck_over_a_bank_stands_on_it(self):
        s = np.arange(8.0, 52.1, STEP)
        bank = np.full(len(s), 108.0)                   # (ground over the line to the far lip: the road must clear it)
        z = roads.mouth_over_deck(LIP_Z, 300.0, [(LIP_Z, GORGE_M)], s, bank)
        self.assertGreater(z, LIP_Z + 7.0)
        self.assertLess(z, 108.0 + roads.DECK_CLEAR + 1e-6, "no higher than clearing the ground asks")

    def test_a_deck_with_no_far_end_has_no_level(self):
        self.assertTrue(np.isnan(roads.mouth_over_deck(LIP_Z, 300.0, [])))


class BridgeContinuedPortalTest(unittest.TestCase):
    """A road in a tunnel that comes out on a bridge over a gorge, as TunnelProfiles reads it from the map: the portal
    where the bridge goes on is at the road's level, the lip, whatever lies under the deck."""

    def setUp(self):
        import roadz
        from unittest import mock
        p = mock.patch.object(roadz, "PROVIDER", None)
        p.start()
        self.addCleanup(p.stop)

    def _tunnel(self, bridge):
        f = features([[(-300, 0), (0, 0), (GORGE_M, 0), (160, 0)]], ["trunk"],
                     flags=[(0, "is_tunnel", 0.0, 300.0 / 460.0)] + (
                         [(0, "is_bridge", 300.0 / 460.0, 360.0 / 460.0)] if bridge else []))

        def elev(la, lo):
            e, _, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            return gorge(e)
        tp = roads.TunnelProfiles(FRAME, lambda key: f, elev)
        tp.raw = elev
        tp.ends = lambda la, lo, base=None: elev(la, lo) if base is None else base
        parts = [p for p in tp.parts([f], (-400.0, -200.0, 400.0, 200.0)) if p[0].startswith(str(f.ids[0]))]
        last = max(parts, key=lambda p: float(np.asarray(p[2])[-1, 0]))     # (the tunnel's end at the gorge)
        return float(np.asarray(last[2])[-1, 0]), float(np.asarray(last[3])[-1])

    def test_the_portal_a_bridge_goes_on_from_is_at_the_lip(self):
        x, z = self._tunnel(bridge=True)
        self.assertAlmostEqual(x, 0.0, delta=10.0)
        self.assertAlmostEqual(z, LIP_Z, delta=0.5, msg="the road's level at the lip (not the gorge, 15 m down)")

    def test_a_portal_no_bridge_goes_on_from_reads_the_ground_outward(self):
        # (the same ground, the gorge with no deck over it: it is not a road's: the old law, the ground)
        x, z = self._tunnel(bridge=False)
        self.assertLess(z, LIP_Z - 10.0)


if __name__ == "__main__":
    unittest.main()
