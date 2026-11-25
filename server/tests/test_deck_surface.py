"""roads._deck_look: a bridge tagged surface=concrete takes the road's asphalt (Roswell Road over I-285, Akers Mill
Road over I-75, Atlanta: plain concrete read as "missing road texture", 2026-11-21); BNE_DECK_SURFACE_CONCRETE=on as
before. A concrete street on the ground keeps its concrete (look_of)."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402


def spec(cls="primary", surf="concrete"):
    return SimpleNamespace(cls=cls, kind="paved", surf=surf)


class DeckSurfaceTest(unittest.TestCase):
    def test_concrete_deck_drawn_in_asphalt(self):
        with mock.patch.object(roads, "DECK_SURFACE_CONCRETE_ON", False):
            self.assertIs(roads._deck_look(spec()), roads.DECK)

    def test_concrete_street_on_the_ground_stays_concrete(self):
        self.assertIs(roads.look_of(spec()), roads.CONCRETE)

    def test_switch_keeps_the_old_concrete(self):
        with mock.patch.object(roads, "DECK_SURFACE_CONCRETE_ON", True):
            self.assertIs(roads._deck_look(spec()), roads.CONCRETE)

    def test_asphalt_deck_unchanged(self):
        self.assertIs(roads._deck_look(spec(surf="asphalt")), roads.DECK)


if __name__ == "__main__":
    unittest.main()
