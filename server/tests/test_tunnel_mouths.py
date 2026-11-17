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


if __name__ == "__main__":
    unittest.main()
