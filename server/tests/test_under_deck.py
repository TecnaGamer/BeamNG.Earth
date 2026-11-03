"""GitHub #685: Järve tee under Tartu mnt at Assaku, Estonia (59.3788 N 24.8441 E). The lidar keeps the trunk's two
short decks as ground, so the road under them has a 6 m hump 40 m long in its elevation. Its plan climbed it, and
the underpass dip, measured from the hump, lowered it and the streets joined to it 5.5 m under their ground for about
560 m. A road's ground where a deck crosses over it is read from beside the deck (grading.deck_capped)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402
from test_roads import features, FRAME  # noqa: E402
from test_grading import FakeMD  # noqa: E402

ROAD = 43.0         # the road under, level
BANK = 5.7          # the trunk's embankment over it (its deck kept as ground: no opening)


def bank_profile(y):
    """The ground across the trunk's embankment along y = 0: its top 12 m either side, its toe 20 m."""
    return ROAD + BANK * np.clip((20.0 - np.abs(y)) / 8.0, 0.0, 1.0)


def hump(la, lo):
    return bank_profile(np.asarray(FRAME.geodetic_to_enu(la, lo, 0.0)[1], np.float64))


class DeckCappedTest(unittest.TestCase):
    pts = np.stack([np.zeros(101), np.arange(-100.0, 101.0, 2.0)], axis=1)      # a road north-south
    deck = (np.array([[-10.0, 0.0], [10.0, 0.0]]), 6.0)                         # a 20 m deck east-west over it

    def test_hump_capped_at_the_ground_beside_the_deck(self):
        got = grading.deck_capped(self.pts, bank_profile(self.pts[:, 1]), [self.deck])
        np.testing.assert_allclose(got, ROAD, atol=0.01)

    def test_never_raised(self):
        dip = ROAD - 4.0 * (np.abs(self.pts[:, 1]) < 8.0)          # a road that does dip under its bridge
        np.testing.assert_allclose(grading.deck_capped(self.pts, dip, [self.deck]), dip)

    def test_left_by_a_deck_end_and_without_decks(self):
        z = bank_profile(self.pts[:, 1])
        end = (np.array([[-3.0, 0.0], [17.0, 0.0]]), 6.0)          # 3 m from the deck's end: a junction at grade
        self.assertIs(grading.deck_capped(self.pts, z, [end]), z)
        self.assertIs(grading.deck_capped(self.pts, z, []), z)


class UnderDeckTest(unittest.TestCase):
    def test_road_under_keeps_its_level(self):
        f = features([[(-400, 0), (400, 0)], [(0, -400), (0, 400)]], ["trunk", "tertiary"],
                      flags=[(0, "is_bridge", 390 / 800, 410 / 800)], ids=["tartu", "jarve"])
        g = grading.Grader(FRAME, FakeMD(f), hump)
        ys = np.arange(-150.0, 151.0, 2.0)
        la, lo, _ = FRAME.enu_to_geodetic(np.zeros(len(ys)), ys, 0.0)
        z = np.asarray(g.apply(la, lo, hump(la, lo)), np.float64)
        far = np.abs(ys) >= 60.0
        self.assertLess(np.abs(z[far] - ROAD).max(), 0.3, f"no trench beside the bridge: {z[far].min():.2f}")
        depth = roads.deck_depth(roads.geom_span([np.array([[-10.0, 0.0], [10.0, 0.0]])]))
        need = ROAD + BANK - roads.deck_road_clear(depth) - grading.ROAD_LIFT     # (the deck on the embankment's top)
        under = z[np.abs(ys) <= 4.0]
        self.assertLess(under.max(), need + 0.2, "the road clears the deck")
        self.assertGreater(z.min(), need - 0.6, "dug only as deep as the clearance needs")


if __name__ == "__main__":
    unittest.main()
