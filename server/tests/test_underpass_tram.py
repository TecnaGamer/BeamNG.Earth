"""A street tram's bridge piece that lies on a road deck is no overpass for the roads it crosses (grading.tram_rides_deck).
Prague, Rašínovo nábřeží (50.07267 N 14.41415 E, 2026-11-06): a 25 m tram piece at the end of Palackého most stood over
the quay, and the underpass pass dug the quay up to 4.9 m under it: humps all along the road. A tram viaduct that stands
clear of every road deck still takes the road under it down."""
import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grading  # noqa: E402
from test_grading import FakeMD  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def line(*pts):
    return np.array(pts, np.float64)


class RidesDeckTest(unittest.TestCase):
    def test_a_piece_along_a_deck_rides_it(self):
        tram = line((0, 0), (30, 0))
        deck = line((-5, 2), (40, 2))
        self.assertTrue(grading.tram_rides_deck(tram, [deck]))

    def test_a_piece_half_on_a_deck_rides_it(self):
        """Prague: only the end of the piece lies on the busway's deck."""
        tram = line((0, 0), (25, 0))
        deck = line((12, 1), (40, 1))
        self.assertTrue(grading.tram_rides_deck(tram, [deck]))

    def test_a_viaduct_clear_of_decks_does_not(self):
        tram = line((0, 0), (80, 0))
        deck = line((0, 40), (80, 40))
        self.assertFalse(grading.tram_rides_deck(tram, [deck]))

    def test_a_piece_that_only_touches_a_deck_end_does_not(self):
        tram = line((0, 0), (200, 0))
        deck = line((200, 0), (200, 80))
        self.assertFalse(grading.tram_rides_deck(tram, [deck]))

    def test_no_decks_or_a_degenerate_piece(self):
        self.assertFalse(grading.tram_rides_deck(line((0, 0), (30, 0)), []))
        self.assertFalse(grading.tram_rides_deck(line((0, 0), (0, 0)), [line((0, 0), (10, 0))]))


class UnderpassTest(unittest.TestCase):
    def setUp(self):
        import roadz
        p = unittest.mock.patch.object(roadz, "PROVIDER", None)
        p.start()
        self.addCleanup(p.stop)

    def dig(self, with_deck, over=130.0):
        """The lowest the N-S residential road at x = over gets under a tram bridge along y = 80, x 92..168 (flat ground
        at 10 m); with_deck: a road bridge along the tram's second half, x 137..168."""
        lines = [[(-60, 80), (320, 80)], [(over, -60), (over, 320)]]
        classes = ["tram", "residential"]
        subtypes = ["rail", "road"]
        flags = [(0, "is_bridge", 0.4, 0.6)]
        levels = [(0, 1, 0.4, 0.6)]
        if with_deck:
            lines.append([(-60, 80), (320, 80)])
            classes.append("primary")
            subtypes.append("road")
            flags.append((2, "is_bridge", 0.52, 0.6))
            levels.append((2, 1, 0.52, 0.6))
        f = features(lines, classes, subtypes=subtypes, flags=flags, levels=levels)
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([over]), np.array([80.0]), 0.0)
        low = 10.0
        for p in g.plans(la, lo, roads_only=True):
            xy = np.asarray(p.xy)
            if abs(float(np.median(xy[:, 0])) - over) < 5.0:
                low = min(low, float(np.min(p.z)))
        return low

    def test_a_road_dips_under_a_tram_viaduct_of_its_own(self):
        self.assertLess(self.dig(False), 9.0, "the dip under a free tram bridge stays")

    def test_a_road_under_a_tram_piece_on_a_road_deck_stays_level(self):
        self.assertGreater(self.dig(True, over=110.0), 9.8, "the crossing is at the road deck's junction, not under a deck")


if __name__ == "__main__":
    unittest.main()
