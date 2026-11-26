"""Two decks meeting at a joint that lie over each other: the shorter one takes the longer one's top over the overlap.
A climbing 24 m deck ends at the joint; a flat 18 m deck leaves it at an angle, back across the climbing one's side. Its
flat slab stood up to 0.2 m over the other's top along their seam (they are nearer than TRIM_DZ[0] in height, so nothing
trimmed them)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402

GRADE = 0.024


def chunk(fid, nodes, width):
    return {"m": "road_asphalt_2lane", "w": width, "tl": 10.0, "p": 20, "deck": True, "fixed": True, "fid": fid,
            "n": [list(map(float, p)) for p in nodes]}


def deck_chunks():
    long_ = chunk("long", [(x, 0.0, 20.0 + GRADE * x) for x in (-50.0, -25.0, 0.0)], 24.0)
    d = np.array([-0.3, -0.954])
    short = chunk("short", [(*(d * s), 20.0) for s in (0.0, 14.0)], 18.0)
    return long_, short


def tops(chunks, xy):
    """The heights of the decks' top triangles over the plan point xy."""
    p, t, _uv, mats, mi = roadmesh.deck(chunks, (0.0, 0.0, 0.0))
    t = t[mi == mats.index(roadmesh.DECK_TOP)]
    a, b, c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
    det = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
    keep = np.abs(det) > 1e-9
    a, b, c, det = a[keep], b[keep], c[keep], det[keep]
    u = ((b[:, 1] - c[:, 1]) * (xy[0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (xy[1] - c[:, 1])) / det
    v = ((c[:, 1] - a[:, 1]) * (xy[0] - c[:, 0]) + (a[:, 0] - c[:, 0]) * (xy[1] - c[:, 1])) / det
    hit = (u >= -1e-8) & (v >= -1e-8) & (1 - u - v >= -1e-8)
    return np.unique(np.round(u[hit] * a[hit, 2] + v[hit] * b[hit, 2] + (1 - u - v)[hit] * c[hit, 2], 6))


@unittest.skipUnless(fast.AVAILABLE, "earthfast")
class JointCamberTest(unittest.TestCase):
    def test_the_short_deck_takes_the_long_decks_top_over_the_overlap(self):
        long_, short = deck_chunks()
        (lr, _), (sr, _) = roadmesh.ribbon_pairs([long_, short])
        for xy in ((-5.0, -10.0), (-3.0, -4.0), (-6.0, -8.0)):
            p = np.array([xy])
            self.assertTrue(roadmesh._in_slab(lr[0], lr[1], p)[0] and roadmesh._in_slab(sr[0], sr[1], p)[0],
                            "both decks lie over the point")
            heights = tops([long_, short], xy)
            self.assertLessEqual(float(np.abs(heights - (20.0 + GRADE * xy[0])).max()), 0.02,
                                 "the short deck's top on the long deck's, the long deck unchanged")

    def test_a_sidewalk_or_marking_on_the_short_deck_moves_with_it(self):
        long_, short = deck_chunks()
        d = np.array([-0.3, -0.954])
        side = -10.25 * np.array([-d[1], d[0]])         # (the short deck's right edge, over the long deck's slab)
        walk = {**chunk("short", [(*(d * s + side), 20.03) for s in (0.0, 14.0)], 2.5), "deck": False, "m": "sidewalk_concrete"}
        del walk["deck"]
        pairs = roadmesh.ribbon_pairs([long_, short, walk])
        got = roadmesh.camber_joints(pairs)
        (left, right, _v, _m, _layer, z), _c = got[2]
        mid = (left + right) / 2
        np.testing.assert_allclose(z, 20.0 + GRADE * mid[:, 0] + 0.03, atol=0.02)
        self.assertGreater(float(np.abs(z - 20.03).max()), 0.1, "it moved")

    def test_the_long_deck_is_left_as_it_is(self):
        long_, short = deck_chunks()
        for xy in ((-30.0, 5.0), (-30.0, -9.0), (-12.0, 11.0)):       # (clear of the short deck's slab)
            alone = tops([long_], xy)
            both = tops([long_, short], xy)
            np.testing.assert_allclose(both, alone, atol=1e-6)

    def test_decks_not_meeting_at_a_joint_are_left_alone(self):
        long_, short = deck_chunks()
        far = chunk("far", [(s[0] + 30.0, s[1], s[2]) for s in short["n"]], 18.0)
        got = roadmesh.camber_joints([(r, c) for r, c in roadmesh.ribbon_pairs([long_, far])])
        for (r, _c), (r0, _c0) in zip(got, roadmesh.ribbon_pairs([long_, far])):
            np.testing.assert_array_equal(r[5], r0[5])


@unittest.skipUnless(fast.AVAILABLE, "earthfast")
class WholeNetJointsTest(unittest.TestCase):
    """Which decks a deck joins is a fact of the road net, not of a tile's window (seamprobe: a deck cut by a tile's edge
    was moved on one side of the cut and not on the other, a 0.14 m step across it)."""

    def test_the_joints_are_read_from_the_whole_parts(self):
        spec = type("S", (), {"width": 24.0})()
        d = np.array([-0.3, -0.954])
        long_ = ("long", spec, np.array([(x, 0.0, 20.0 + GRADE * x) for x in (-50.0, -25.0, 0.0)]), 0.0)
        short = ("short", type("S", (), {"width": 18.0})(), np.array([(*(d * s), 20.0) for s in (0.0, 14.0)]), 0.0)
        far = ("far", spec, np.array([(x + 30.0, 40.0, 20.0) for x in (-10.0, 0.0)]), 0.0)
        got = roads._camber_partners([long_, short, far])
        self.assertEqual(got, [[], ["long"], []], "the short deck joins the long one at its end; nothing joins far")

    def test_a_deck_cut_by_a_window_still_takes_its_partners_top(self):
        long_, short = deck_chunks()
        # (a window that cuts the long deck to 12 m of it, shorter than the short deck's 14 m: judged by the pieces,
        # the long one would be the one that moves)
        cut = chunk("long", [(x, 0.0, 20.0 + GRADE * x) for x in (-12.0, 0.0)], 24.0)
        cut["cj"], short["cj"] = [], ["long"]
        for xy in ((-5.0, -10.0), (-3.0, -4.0)):
            heights = tops([cut, short], xy)
            self.assertLessEqual(float(np.abs(heights - (20.0 + GRADE * xy[0])).max()), 0.02)

    def test_decks_with_cut_ends_side_by_side_do_not_join(self):
        """Two decks side by side whose cut ends (a tile's edge) meet are no joint: a chunk that knows its joints (none)
        is not moved by what the window's pieces look like."""
        a = chunk("a", [(-30.0, 0.0, 20.0), (0.0, 0.0, 20.0)], 10.0)
        b = chunk("b", [(-20.0, 0.0, 20.1), (0.0, 0.0, 20.1)], 10.0)
        b["n"] = [[-10.0, 3.0, 20.1], [0.0, 3.0, 20.1]]
        a["cj"], b["cj"] = [], []
        pairs = roadmesh.ribbon_pairs([a, b])
        for (r, _c), (r0, _c0) in zip(roadmesh.camber_joints(pairs), pairs):
            np.testing.assert_array_equal(r[5], r0[5])


if __name__ == "__main__":
    unittest.main()
