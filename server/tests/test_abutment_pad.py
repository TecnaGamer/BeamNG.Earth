"""Parapets and sidewalks along a bridge's whole deck. The Juri overpass over Tallinna ringtee (59.35833 N, 24.8906 E)
had a gap near each end, a sloped end section either side of it and dark deck where the sidewalk strip should be:
- its abutment's junction pad (two one-way approach roads, 3.1 m each, meet the deck's end on the ground) reached 12 m
  along the deck (PAD_REACH_M) and the parapets stop inside a pad: a 29 m run of the 57 m deck;
- roads._off_wall_tops cut the deck's sidewalk strips and lines where the walls of the cutting under it pass."""
import os
import sys
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import roads  # noqa: E402
import roadmesh  # noqa: E402

GROUND_Z = 20.0


def spec(width):
    return SimpleNamespace(width=width)


def arms(deck_z=GROUND_Z):
    """A 57 m deck east along +x from the joint at the origin (6.2 m wide, on the ground) and the two one-way roads
    (3.1 m each) leaving the joint west, 17 degrees either side of the deck's axis."""
    xy = np.c_[np.linspace(0.0, 57.0, 29), np.zeros(29)]
    deck = ("bridge", spec(6.2), xy, np.full(29, deck_z))
    ground = []
    for name, deg in (("north", 17.0), ("south", -17.0)):
        a = np.radians(180.0 - deg)
        t = np.linspace(0.0, 40.0, 21)
        ground.append((name, spec(3.1), np.c_[t * np.cos(a), t * np.sin(a)], None))
    return deck, ground


def pads(deck, ground, ground_z=GROUND_Z):
    return roads.deck_pads([deck], ground, (-80.0, -80.0, 80.0, 80.0), lambda p: np.full(len(p), ground_z))


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 122, "earthfast 122 (deck_edges_covered)")
class AbutmentPadTest(unittest.TestCase):
    def test_pad_stays_off_the_deck_at_an_abutment(self):
        deck, ground = arms()
        got = pads(deck, ground)
        self.assertEqual(len(got), 1, "the approach roads still meet in a pad")
        ring = np.asarray(got[0][0])
        # (the deck's end, and the diverging roads' own flanks at the joint: 4 m out from lines 17 degrees off the axis)
        self.assertLessEqual(float(ring[:, 0].max()), 2.0, "none of the deck's length (12 m before)")
        self.assertLessEqual(float(ring[:, 0].min()), -10.0, "the approach roads' meeting is paved as before")

    def test_parapets_run_to_the_abutment(self):
        deck, ground = arms()
        got = pads(deck, ground)
        chunk = {"m": "road_asphalt_2lane", "w": 6.2, "tl": 10.0, "p": 20, "deck": True, "fixed": True, "fid": "bridge",
                 "n": [[float(x), 0.0, GROUND_Z] for x in np.linspace(0.0, 57.0, 29)]}
        runs = roadmesh.parapet_runs([chunk], pads=got)
        self.assertEqual(len(runs), 2, "one run per side, no gap")
        for run in runs:
            length = float(np.hypot(*np.diff(run["edge"], axis=0).T).sum())
            self.assertGreater(length, 57.0 - 2 * (roads.PAD_ABUT_REACH_M + 0.3) - 2.1, "the whole deck but its pad's end")

    def test_two_decks_at_a_joint_keep_their_reach(self):
        """Only a deck alone at its joint is an abutment: a ramp leaving a viaduct's deck is paved as before."""
        deck, ground = arms()
        ramp = ("ramp", spec(3.5), np.c_[np.linspace(0.0, 24.0, 13), np.linspace(0.0, 24.0, 13)], np.full(13, GROUND_Z))
        got = roads.deck_pads([deck, ramp], ground, (-80.0, -80.0, 80.0, 80.0), lambda p: np.full(len(p), GROUND_Z))
        self.assertEqual(len(got), 1)
        self.assertGreater(float(np.asarray(got[0][0])[:, 0].max()), 5.0)

    def test_switch_off_restores_the_reach(self):
        deck, ground = arms()
        old = roads.PAD_ABUT_ON
        roads.PAD_ABUT_ON = False
        try:
            got = pads(deck, ground)
        finally:
            roads.PAD_ABUT_ON = old
        self.assertGreaterEqual(float(np.asarray(got[0][0])[:, 0].max()), roads.PAD_REACH_M - 1.0)


class WallTopCutTest(unittest.TestCase):
    """The walls of a cutting cut the ground roads' surfaces over their tops (roads._off_wall_tops), not a bridge's
    sidewalks and lines high over the cutting: the Juri overpass's sidewalk strips had gaps where the walls of the
    cutting under it pass (3.8 and 2.8 m, both sides), dark deck showing in them."""

    @staticmethod
    def chunks():
        wall = {"base": np.c_[np.full(5, 20.0), np.linspace(-20.0, 20.0, 5)], "thick": np.full(5, 2.4),
                "toward": np.tile([1.0, 0.0], (5, 1))}
        road = {"m": "sidewalk_concrete", "w": 2.5, "n": [[0.0, 0.0, 10.0], [40.0, 0.0, 10.0]]}
        deck = {"m": "sidewalk_concrete", "w": 2.5, "fixed": True, "fid": "bridge",
                "n": [[0.0, 3.0, 25.0], [40.0, 3.0, 25.0]]}
        return [wall], [road, deck]

    @staticmethod
    def span(chunks):
        return sum(abs(c["n"][-1][0] - c["n"][0][0]) for c in chunks if c["n"][0][1] == 3.0)

    def test_ground_surface_is_cut_and_the_bridge_strip_is_not(self):
        walls, chunks = self.chunks()
        got = roads._off_wall_tops(chunks, walls)
        self.assertEqual(self.span([c for c in got if c["n"][0][2] == 25.0]), 40.0, "the deck's strip whole")
        ground = [c for c in got if c["n"][0][2] == 10.0]
        self.assertGreater(len(ground), 0)
        self.assertLess(sum(abs(c["n"][-1][0] - c["n"][0][0]) for c in ground), 40.0, "the ground road's cut")

    def test_switch_off_cuts_the_strip_as_before(self):
        walls, chunks = self.chunks()
        old = roads.WALL_TOP_FIXED
        roads.WALL_TOP_FIXED = False
        try:
            got = roads._off_wall_tops(chunks, walls)
        finally:
            roads.WALL_TOP_FIXED = old
        self.assertLess(self.span([c for c in got if c["n"][0][2] == 25.0]), 40.0)


if __name__ == "__main__":
    unittest.main()
