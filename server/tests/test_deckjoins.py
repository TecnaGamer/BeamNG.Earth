"""Deck parts of one carriageway that the map does not join (roads._end_joins, earthfast.deck_end_joins) and short
ground gaps between decks (roads._deck_gaps, earthfast.deck_gaps): the surface a car drives on has no step where a
branch leaves a viaduct a few metres off the joint (I-83, the Jones Falls Expressway in Baltimore: the AI drove off an
8.8 m cliff, 2026-10-29) or where the map leaves a short stretch of a viaduct untagged; with and without measured deck
heights (roadz)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import roads  # noqa: E402
import roadz  # noqa: E402
from test_approaches import flat_elev, FLAT  # noqa: E402
from test_roads import features, FRAME  # noqa: E402
from test_roadz import StubProvider  # noqa: E402

NATIVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 105
CROSS = [[(-200, -150), (200, -150)], [(-200, 150), (200, 150)]]       # streets under the viaduct
BRIDGE = (None, "is_bridge", 0.0, 1.0)


def _feats(lines, bridged, extra=()):
    lines = list(lines) + CROSS + [ln for ln, _ in extra]
    classes = ["motorway"] * (len(lines) - len(CROSS) - len(extra)) + ["residential"] * len(CROSS) + \
        [c for _, c in extra]
    flags = [(i, "is_bridge", 0.0, 1.0) for i in bridged]
    return features(lines, classes, flags=flags, ids=[f"p{i}" for i in range(len(lines))])


def _solve(f):
    bp = roads.BridgeProfiles(FRAME, lambda key: f, flat_elev)
    out = {}
    for fid, _spec, xy, z, _lon in bp.parts([f], (-400, -400, 400, 400)):
        out[fid] = (np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64))
    return out


def _grade(xy, z):
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    t = np.arange(0.0, s[-1], 2.0)
    zz = np.interp(t, s, z)
    return float(np.abs(np.diff(zz)).max() / 2.0) if len(zz) > 1 else 0.0


def _measured(xy, s):
    """A viaduct measured 8 m over the ground all along (geodetic heights)."""
    return np.full(len(xy), FLAT + 8.0)


@unittest.skipUnless(NATIVE, "earthfast 105 (deck_end_joins, deck_gaps)")
class EndJoinTest(unittest.TestCase):
    """A viaduct (p0 then p1, joined at (0, 0)) and its right-hand branch p2 starting 3.3 m off the joint."""

    def setUp(self):
        self.old = roadz.PROVIDER, roads.END_JOIN_ON
        self.f = _feats([[(0, -300), (0, 0)], [(0, 0), (0, 300)], [(3.3, 0), (12, 300)]], [0, 1, 2])

    def tearDown(self):
        roadz.PROVIDER, roads.END_JOIN_ON = self.old

    def check(self):
        d = _solve(self.f)
        joint = d["p0"][1][-1]
        self.assertLess(abs(d["p1"][1][0] - joint), 0.1)
        self.assertLess(abs(d["p2"][1][0] - joint), 0.1, "the branch starts at the viaduct's height, no cliff")
        self.assertGreater(joint, FLAT + 2.5, "(an elevated joint: the streets under it)")
        for k in ("p0", "p1", "p2"):
            self.assertLessEqual(_grade(*d[k]), roads.OBS_MAX_GRADE, k)
        return d

    def test_branch_joins_the_viaduct(self):
        roadz.PROVIDER = None
        self.check()

    def test_with_measured_heights(self):
        roadz.PROVIDER = StubProvider(_measured)
        d = self.check()
        self.assertGreater(d["p2"][1][0], FLAT + 7.0)

    def test_was_a_cliff_without_it(self):
        roadz.PROVIDER = None
        roads.END_JOIN_ON = False
        d = _solve(self.f)
        self.assertGreater(abs(d["p2"][1][0] - d["p0"][1][-1]), 3.0, "(the case the join is for)")

    def test_a_crossing_or_a_ground_road_keeps_the_abutment(self):
        roadz.PROVIDER = None
        # a deck ending on a road going on on the ground there: an abutment, whatever is near
        f = _feats([[(0, -300), (0, 0)], [(0, 0), (0, 300)], [(3.3, 0), (12, 300)]], [0, 1, 2],
                   extra=[([(3.3, 0), (60, -80)], "primary")])
        d = _solve(f)
        self.assertLess(abs(d["p2"][1][0] - FLAT), 0.2)
        # a deck crossing the joint's road at right angles: no join
        joins = roads._end_joins([np.array([[0.0, -100], [0, 0]]), np.array([[0.0, 0], [0, 100]]),
                                  np.array([[3.0, 0], [100, 0]])], [5.0, 5.0, 5.0], [])
        self.assertEqual(joins, [])

    def test_kernel_order_and_reach(self):
        lines = [np.array([[0.0, 0], [0, 50]]), np.array([[0.0, 53], [0, 100]]), np.array([[30.0, 0], [30, 50]])]
        joins = roads._end_joins(lines, [5.0, 5.0, 5.0], [])
        self.assertEqual(joins, [(1, 0, (0.0, 50.0))], "a gap of 3 m in one deck: the later end meets the earlier")
        self.assertEqual(roads._end_joins(lines, [1.0, 1.0, 1.0], []), [], "(beyond the half width)")


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 107, "earthfast 107 (deck_end_on)")
class EndOnDeckTest(unittest.TestCase):
    """A cross street's bridge (p0, x -150..150 over two streets) and a ramp's deck (p1) starting on it 3 m off its
    line, going north 150 m down to a street (p2 goes on from its end on the ground)."""

    def setUp(self):
        self.old = roadz.PROVIDER, roads.END_JOIN_ON
        roadz.PROVIDER = None

    def tearDown(self):
        roadz.PROVIDER, roads.END_JOIN_ON = self.old

    def solve(self, extra=()):
        lines = [[(-150, 0), (150, 0)], [(0, 3), (0, 150)], [(0, 150), (0, 300)],
                 [(-100, -200), (-100, 200)], [(100, -200), (100, 200)]] + [ln for ln, _ in extra]
        classes = ["primary", "motorway", "motorway", "residential", "residential"] + [c for _, c in extra]
        f = features(lines, classes, flags=[(0, "is_bridge", 0.0, 1.0), (1, "is_bridge", 0.0, 1.0)],
                     ids=[f"p{i}" for i in range(len(lines))])
        return _solve(f)

    def test_ramp_starts_at_the_bridge_height(self):
        d = self.solve()
        xy0, z0 = d["p0"]
        s0 = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy0, axis=0).T))]
        z_at = float(np.interp(150.0, s0, z0))
        self.assertGreater(z_at, FLAT + 2.0, "(the bridge stands over the streets)")
        self.assertLess(abs(d["p1"][1][0] - z_at), 0.1, "the ramp's start on the bridge, not on the ground under it")
        self.assertLessEqual(_grade(*d["p1"]), roads.DECK_STEEP_GRADE)

    def test_ground_road_there_keeps_the_abutment(self):
        d = self.solve(extra=[([(0, 3), (-80, 60)], "residential")])
        self.assertLess(abs(d["p1"][1][0] - FLAT), 0.2)

    def test_off(self):
        roads.END_JOIN_ON = False
        self.assertLess(abs(self.solve()["p1"][1][0] - FLAT), 0.2, "(the case it is for)")


@unittest.skipUnless(NATIVE, "earthfast 105 (deck_end_joins, deck_gaps)")
class GroundGapTest(unittest.TestCase):
    """A viaduct p0 - a 40 m stretch p1 the map has on the ground - a viaduct p2."""

    def setUp(self):
        self.old = roadz.PROVIDER, roads.GAP_DECK_ON
        self.lines = [[(0, -300), (0, -20)], [(0, -20), (0, 20)], [(0, 20), (0, 300)]]

    def tearDown(self):
        roadz.PROVIDER, roads.GAP_DECK_ON = self.old

    def check(self, max_grade=roads.DECK_MAX_GRADE + 0.02):
        f = _feats(self.lines, [0, 2])
        self.assertEqual(roads.level_ranges(f, 1), [(0.0, 1.0, "bridge")])
        d = _solve(f)
        self.assertIn("p1", d)
        self.assertLess(abs(d["p0"][1][-1] - d["p1"][1][0]), 0.1)
        self.assertLess(abs(d["p1"][1][-1] - d["p2"][1][0]), 0.1)
        self.assertGreater(d["p1"][1].min(), FLAT + 2.0, "no dip to the ground between the decks")
        for k in ("p0", "p1", "p2"):
            self.assertLessEqual(_grade(*d[k]), max_grade, k)

    def test_gap_is_deck(self):
        roadz.PROVIDER = None
        self.check()

    def test_gap_is_deck_with_measured_heights(self):
        roadz.PROVIDER = StubProvider(_measured)
        self.check(roads.OBS_MAX_GRADE)     # (eased from 8 m onto the abutments at the far ends)

    def test_long_gap_or_junction_stays_ground(self):
        f = _feats([[(0, -300), (0, -50)], [(0, -50), (0, 50)], [(0, 50), (0, 300)]], [0, 2])
        self.assertEqual(roads.level_ranges(f, 1), [], "100 m: a road on the ground between two bridges")
        f = _feats(self.lines, [0, 2], extra=[([(0, 0), (80, 0)], "residential")])
        self.assertEqual(roads.level_ranges(f, 1), [], "a street joins it: a junction at grade")

    def test_within_one_road(self):
        f = features([[(0, -300), (0, 300)]] + CROSS, ["motorway", "residential", "residential"],
                     flags=[(0, "is_bridge", 0.0, 280 / 600), (0, "is_bridge", 320 / 600, 1.0)])
        self.assertEqual(roads.level_ranges(f, 0), [(0.0, 1.0, "bridge")])

    def test_off(self):
        roads.GAP_DECK_ON = False
        f = _feats(self.lines, [0, 2])
        self.assertEqual(roads.level_ranges(f, 1), [])



@unittest.skipUnless(NATIVE, "earthfast 105")
class PinnedJointTest(unittest.TestCase):
    """Two decks leaving a joint a street goes on from on the ground (Charles Street over I-83: the decks' joint is
    pinned, _ground_pins), one over a street that wants headroom: the joint stays at the street's height in the curve
    solve too (it stood 8.5 m over it) and with measured decks (eased to their measured mean, 2.5 m under it)."""

    def setUp(self):
        self.old = roadz.PROVIDER
        roadz.PROVIDER = None

    def tearDown(self):
        roadz.PROVIDER = self.old

    def check(self):
        lines = [[(0, -200), (0, 0)], [(0, 0), (0, 120)], [(0, 0), (60, 100)], [(-80, 60), (80, 60)],
                 [(-200, 0), (0, 0)]]
        f = features(lines, ["secondary", "secondary", "secondary", "residential", "residential"],
                     flags=[(1, "is_bridge", 0.0, 1.0), (2, "is_bridge", 0.0, 1.0)],
                     ids=["street", "deck1", "deck2", "under", "side"])
        d = _solve(f)
        for k in ("deck1", "deck2"):
            self.assertLess(abs(d[k][1][0] - FLAT), 0.05, k)
        return d

    def test_curve_solve_keeps_the_pin(self):
        d = self.check()
        self.assertGreater(float(np.max(d["deck1"][1])), FLAT + 2.0, "(the deck still rises over the street under it)")

    def test_measured_keeps_the_pin(self):
        roadz.PROVIDER = StubProvider(lambda xy, s: np.full(len(xy), FLAT + 6.0))
        self.check()

    def test_unpinned_parts(self):
        lines = [np.array([[0.0, 0], [0, 50]]), np.array([[0.0, 0], [50, 50]]), np.array([[0.0, -50], [0, 0]])]
        out, gfn = roads._unpinned(lines, lambda xy: np.asarray(xy, np.float64)[:, 0], [(0.0, 0.0)])
        ends = [tuple(np.round(out[0][0], 2)), tuple(np.round(out[1][0], 2)), tuple(np.round(out[2][-1], 2))]
        self.assertEqual(len(set(ends)), 3, "each part's own end")
        np.testing.assert_allclose(gfn(np.array(ends)), 0.0, atol=1e-9, err_msg="the ground at the joint itself")



@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 110, "earthfast 110 (deck_pad)")
class JunctionPadTest(unittest.TestCase):
    """A street (ground, half 5.2 m) going north onto a bridge: a deck north (half 6.85) and a one-lane deck leaving
    north-east (half 1.75) from the joint (North Charles Street over I-83: the AI cut the corner onto the station's
    road over the motorway's cut)."""

    def pads(self, ne=True, box=(-50.0, -50.0, 50.0, 50.0), ground_z=22.0):
        Spec = type("S", (), {})
        def spec(w):
            s = Spec()
            s.width = w
            return s
        decks = [("north", spec(13.7), np.c_[np.zeros(11), np.linspace(0, 40, 11)], np.full(11, 22.0))]
        if ne:
            decks.append(("ne", spec(3.5), np.c_[np.linspace(0, 24, 11), np.linspace(0, 36, 11)], np.full(11, 22.0)))
        ground = [("street", spec(10.4), np.c_[np.zeros(11), np.linspace(-60, 0, 11)], None)]
        return roads.deck_pads(decks, ground, box, lambda xy: np.full(len(xy), ground_z))

    @staticmethod
    def inside(ring, x, y):
        k = len(ring)
        return all((ring[(i + 1) % k][0] - ring[i][0]) * (y - ring[i][1]) -
                   (ring[(i + 1) % k][1] - ring[i][1]) * (x - ring[i][0]) >= -1e-9 for i in range(k))

    def test_corner_paved(self):
        pads = self.pads()
        self.assertEqual(len(pads), 1)
        ring, centre = pads[0][:2]
        self.assertTrue(self.inside(ring, 6.0, 3.0), "the right turn's corner, outside both carriageways")
        self.assertTrue(self.inside(ring, 5.0, 8.0), "the wedge where the ramp leaves")
        self.assertFalse(self.inside(ring, 20.0, 0.0))
        np.testing.assert_allclose(centre, [0.0, 0.0, 22.0], atol=1e-9)

    def test_straight_joint_none(self):
        self.assertEqual(self.pads(ne=False), [], "a deck going on straight from the street: its abutment")

    def test_owned_by_one_tile(self):
        self.assertEqual(self.pads(box=(10.0, 10.0, 60.0, 60.0)), [])

    def test_mesh_faces_up_and_collides(self):
        import roadmesh
        ring, centre = self.pads()[0][:2]
        got = roadmesh.deck([], (0.0, 0.0, 0.0), pads=[(ring, centre)])
        P, T = got[0], got[1]
        top = T[: len(ring)]
        nrm = np.cross(P[top[:, 1]] - P[top[:, 0]], P[top[:, 2]] - P[top[:, 0]])
        self.assertTrue((nrm[:, 2] > 0).all(), "the top faces up")
        self.assertAlmostEqual(float(P[top[0, 0], 2]), 22.0 - roadmesh.PAD_DROP, places=6)

    def test_no_parapet_in_the_turn(self):
        """The decks' edges inside the pad get no parapet (the station road's began at the joint, in the turn's path,
        and the AI clipped its end); the pad reaches PAD_MARGIN_M past every carriageway."""
        pad = self.pads()[0]
        ne_right = np.c_[np.linspace(0, 6, 4), np.linspace(0, 9, 4)] + [1.75 * 0.832, -1.75 * 0.555]
        self.assertTrue(roads.pad_inside([pad], ne_right).all(), "the ramp's outer edge near the joint")
        self.assertFalse(roads.pad_inside([pad], np.array([[0.0, 40.0]])).any())
        ring = np.asarray(pad[0])[:, :2]
        d_street = np.abs(ring[:, 0])[ring[:, 1] < -2.0]       # (the street's arm: x from its line)
        self.assertGreaterEqual(float(d_street.min()), 5.2 + roads.PAD_MARGIN_M - 0.01)

    def test_rim_railing_over_a_drop_only(self):
        walls = self.pads(ground_z=15.0)[0][2]
        self.assertTrue(walls, "over the cut: a railing round the rim")
        pts = np.concatenate([w[0] for w in walls])
        # never across a carriageway leaving the pad
        for x, y in pts[:, :2]:
            self.assertFalse(abs(x) < 6.85 and y > 0 and y < 12, "not across the deck going north")
            self.assertFalse(abs(x) < 5.2 and y < 0, "not across the street")
        self.assertEqual(self.pads(ground_z=22.0)[0][2], [], "on level ground: none")


if __name__ == "__main__":
    unittest.main()
