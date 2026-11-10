"""roads.part_keys: a bridge / tunnel / ground piece's key is the same in every map cell's copy of a long road, whatever
other ranges a copy has (gradesep spans found by the cells around it: the A4 at Glaireuse, #1020) or how far it
lengthened a deck (_skew_decks over the roads its own cell has crossing under it: PR #1013's A11)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roads  # noqa: E402
from test_roads import features  # noqa: E402


def keys(raw, final, want):
    f = features([[(0.0, 0.0), (5000.0, 0.0)]], ["motorway"])
    with mock.patch.object(roads, "raw_level_ranges", lambda feats, i: list(raw)), \
            mock.patch.object(roads, "level_ranges", lambda feats, i, _home=True: list(final)):
        return roads._part_keys_new(f, 0, want)


class PartKeysTest(unittest.TestCase):
    RAW = [(0.4, 0.5, "bridge"), (0.8, 0.9, "bridge")]

    def test_a_lengthened_deck_keeps_its_key(self):
        a = keys(self.RAW, [(0.38, 0.52, "bridge"), (0.8, 0.9, "bridge")], "bridge")
        b = keys(self.RAW, self.RAW, "bridge")
        self.assertEqual(a, b)
        self.assertEqual(len(set(a)), 2)

    def test_another_copy_s_extra_bridge_shifts_nothing(self):
        far = [(0.1, 0.12, "bridge")] + self.RAW
        a = keys(far, far, "bridge")
        b = keys(self.RAW, self.RAW, "bridge")
        self.assertEqual(a[1:], b)

    def test_ground_pieces_between_them_too(self):
        a = keys(self.RAW, [(0.38, 0.52, "bridge"), (0.8, 0.9, "bridge")], "ground")
        b = keys(self.RAW, self.RAW, "ground")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 3)

    def test_a_piece_over_no_mapped_range_keys_by_its_own_start(self):
        a = keys(self.RAW, self.RAW + [(0.95, 0.97, "bridge")], "bridge")
        self.assertEqual(len(set(a)), 3)
        self.assertGreater(a[2], roads.PART_KEY_OWN * roads.PART_KEY_SUB)


if __name__ == "__main__":
    unittest.main()
