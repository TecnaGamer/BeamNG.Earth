"""Decks across tile seams and at their joints: one road's pieces not trimmed as decks beside each other
(roadmesh._chain_owners), a deck's surface past the tile (roads._beyond_box, DECK_SEAM), a slab's reach cut back at
another deck's lanes (roadmesh._clamp_reach), long parts in a crossing's common surface (roads._elevated_joint), and a
road filled onto a bridge built after the survey (grading.stale_end_fill)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402


def _ribbon(x0, x1, half, z0, grade, fid, y=0.0, step=2.0):
    x = np.arange(x0, x1 + 1e-9, step)
    mid = np.c_[x, np.full(len(x), y)]
    return ((mid + [0, half], mid - [0, half], x / 10, "m", "base", z0 + grade * (x - x0)), {"fid": fid})


class ChainTrimTest(unittest.TestCase):
    """I-75 over Nancy Creek: one carriageway's deck in two pieces (its lanes change), climbing 2 %."""

    def test_pieces_of_one_climbing_road_not_trimmed(self):
        a = _ribbon(0.0, 80.0, 13.0, 246.0, 0.02, "sb~lanes0")
        b = _ribbon(80.0, 160.0, 13.0, 246.0 + 0.02 * 80.0, 0.02, "sb~lanes1")
        out = roadmesh.trim_overlaps([a, b])
        for (r, _c), (r0, _c0) in zip(out, [a, b]):
            np.testing.assert_allclose(r[0], r0[0])
            np.testing.assert_allclose(r[1], r0[1])

    def test_off_switch_trims_them(self):
        a = _ribbon(0.0, 80.0, 13.0, 246.0, 0.02, "sb~lanes0")
        b = _ribbon(80.0, 160.0, 13.0, 246.0 + 0.02 * 80.0, 0.02, "sb~lanes1")
        on = roadmesh.TRIM_CHAIN_ON
        try:
            roadmesh.TRIM_CHAIN_ON = False
            (ra, _), _rb = roadmesh.trim_overlaps([a, b])
        finally:
            roadmesh.TRIM_CHAIN_ON = on
        self.assertFalse(np.allclose(ra[0], a[0][0]) and np.allclose(ra[1], a[0][1]))   # (the old notches)

    def test_a_ramp_leaving_at_the_joint_keeps_its_own(self):
        a = _ribbon(0.0, 50.0, 6.0, 100.0, 0.0, "main0")
        b = _ribbon(50.0, 100.0, 6.0, 100.0, 0.0, "main1")
        x = np.arange(50.0, 100.01, 2.0)
        mid = np.c_[x, -(x - 50.0) * 0.25]                        # (a ramp parting at 14 degrees, going down)
        z = 100.0 - 0.04 * (x - 50.0)
        u = np.array([0.25, 1.0]) / np.hypot(0.25, 1.0)
        ramp = ((mid + u * 4.0, mid - u * 4.0, x / 10, "m", "base", z), {"fid": "ramp"})
        lefts = [np.asarray(r[0][0]) for r in (a, b, ramp)]
        rights = [np.asarray(r[0][1]) for r in (a, b, ramp)]
        zs = [np.asarray(r[0][5]) for r in (a, b, ramp)]
        own = roadmesh._chain_owners(lefts, rights, zs, [0, 1, 2])
        self.assertEqual(own[0], own[1])
        self.assertNotEqual(own[2], own[0])


class BeyondBoxTest(unittest.TestCase):
    def test_the_part_just_outside(self):
        line = np.array([[-10.0, 50.0, 1.0], [30.0, 50.0, 2.0]])       # (crossing the box's west edge x = 0)
        got = roads._beyond_box(line, (0.0, 0.0, 100.0, 100.0), 5.0)
        self.assertEqual(len(got), 1)
        np.testing.assert_allclose(got[0][0], [-5.0, 50.0, 1.125])
        np.testing.assert_allclose(got[0][-1], [0.0, 50.0, 1.25])

    def test_inside_or_far_none(self):
        self.assertEqual(roads._beyond_box(np.array([[10.0, 10.0], [20.0, 20.0]]), (0, 0, 100, 100), 5.0), [])
        self.assertEqual(roads._beyond_box(np.array([[-50.0, 10.0], [-40.0, 20.0]]), (0, 0, 100, 100), 5.0), [])

    def test_along_the_edge_outside(self):
        line = np.array([[-3.0, 10.0], [-3.0, 60.0]])                  # (parallel to the seam, 3 m off it)
        got = roads._beyond_box(line, (0.0, 0.0, 100.0, 100.0), 13.0)
        self.assertEqual(len(got), 1)
        np.testing.assert_allclose(got[0], line)


class ClampReachTest(unittest.TestCase):
    """A slab's reach 6 m past its edge, another deck's lanes from 3 m past it."""

    def setUp(self):
        x = np.arange(0.0, 40.01, 2.0)
        self.base = np.c_[x, np.zeros(len(x))]
        self.out = np.tile([0.0, 1.0], (len(x), 1))
        self.z = np.full(len(x), 100.0)
        mid = np.c_[x, np.full(len(x), 8.0)]
        self.other = [("other", mid, np.full(len(x), 5.0), np.full(len(x), 100.4))]

    def test_cut_back_to_the_other_lanes(self):
        got = roadmesh._clamp_reach(np.full(len(self.z), 6.0), self.base, self.out, self.z, "me", self.other, 0.5)
        np.testing.assert_allclose(got, 3.0, atol=0.02)

    def test_level_with_it_or_far_under_kept(self):
        for oz in (100.0, 94.0):                         # (one surface; a road 6 m under it)
            o = [(f, m, h, np.full(len(z), oz)) for f, m, h, z in self.other]
            got = roadmesh._clamp_reach(np.full(len(self.z), 6.0), self.base, self.out, self.z, "me", o, 0.5)
            np.testing.assert_allclose(got, 6.0)

    def test_its_own_deck_ignored(self):
        o = [("me",) + tuple(self.other[0][1:])]
        got = roadmesh._clamp_reach(np.full(len(self.z), 6.0), self.base, self.out, self.z, "me", o, 0.5)
        np.testing.assert_allclose(got, 6.0)


class ElevatedJointTest(unittest.TestCase):
    def ends(self, n):
        q = np.array([0.0, 0.0])
        return [(j, 0, q + [0.1 * j, 0.0]) for j in range(n)]

    def test_crossing_on_a_deck(self):
        pts = np.array([[0.0, 0.0, 110.0], [100.0, 0.0, 105.0]])
        self.assertTrue(roads._elevated_joint(0, pts, self.ends(4), lambda xy: np.full(len(xy), 100.0)))

    def test_a_ramp_joining_or_on_the_ground(self):
        pts = np.array([[0.0, 0.0, 110.0], [100.0, 0.0, 105.0]])
        self.assertFalse(roads._elevated_joint(0, pts, self.ends(3), lambda xy: np.full(len(xy), 100.0)))
        self.assertFalse(roads._elevated_joint(0, pts, self.ends(4), lambda xy: np.full(len(xy), 109.0)))
        self.assertFalse(roads._elevated_joint(0, pts, self.ends(4), None))


class StaleEndFillTest(unittest.TestCase):
    """Roswell Rd's exit ramp off I-285: a road 3 % down toward a bridge, its last 30 m 9 % down (the old ground)."""

    def road(self):
        s = np.arange(0.0, 140.01, 4.0)                 # (s from the far end; the bridge at its last point)
        z = 291.6 - 0.03 * s
        dip = s > 110.0
        z[dip] -= 0.06 * (s[dip] - 110.0)
        return np.c_[s, np.zeros(len(s))], z

    def test_filled_to_its_line(self):
        xy, z = self.road()
        add = grading.stale_end_fill(xy, z, at_end=True)
        self.assertIsNotNone(add)
        got = z + add
        np.testing.assert_allclose(got[-1], 291.6 - 0.03 * 140.0, atol=0.05)
        self.assertTrue((add >= 0).all())
        self.assertLess(np.abs(np.diff(got) / 4.0 + 0.03).max(), 0.005)     # (one even slope)

    def test_from_the_start_too(self):
        xy, z = self.road()
        add = grading.stale_end_fill(xy[::-1], z[::-1], at_end=False)
        self.assertIsNotNone(add)
        np.testing.assert_allclose((z[::-1] + add)[0], 291.6 - 0.03 * 140.0, atol=0.05)

    def test_no_dip_none(self):
        xy, _z = self.road()
        self.assertIsNone(grading.stale_end_fill(xy, 291.6 - 0.03 * xy[:, 0], at_end=True))

    def test_at_most_stale_max(self):
        xy, z = self.road()
        z[-3:] -= 10.0
        add = grading.stale_end_fill(xy, z, at_end=True)
        self.assertLessEqual(add.max(), grading.STALE_MAX_M + 1e-9)

    def test_steep_line_none(self):
        xy, _z = self.road()
        z = 300.0 - 0.12 * xy[:, 0]
        z[-5:] -= 3.0
        self.assertIsNone(grading.stale_end_fill(xy, z, at_end=True))

    def test_over_a_brow_to_the_bridge_none(self):
        # (GA-400 at I-285: up over a hill and 3 m down to the bridge's end; its line from further out rises toward it)
        xy, _z = self.road()
        s = xy[:, 0]
        z = 327.0 + 0.03 * s                            # (rising toward the bridge at the last point) ...
        z[s > 100.0] = z[s > 100.0][0] - 0.08 * (s[s > 100.0] - 100.0)   # (... then down off the brow to it)
        self.assertIsNone(grading.stale_end_fill(xy, z, at_end=True))

    def test_short_road_none(self):
        xy, z = self.road()
        self.assertIsNone(grading.stale_end_fill(xy[-10:], z[-10:], at_end=True))


if __name__ == "__main__":
    unittest.main()
