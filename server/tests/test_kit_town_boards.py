"""PR #505 follow-up: with the regional kit on, townentry's French town signs are the kit's boards (signmesh, the
author's EB10 / EB20 model and lettering) at townentry's places; with it off, townentry's own faces as before."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poleprofiles  # noqa: E402
import roads  # noqa: E402
import signmesh  # noqa: E402
import townentry  # noqa: E402

STREET = roads.Spec(6.0, "paved", True, "secondary")
ROADS = [("r", STREET, np.array([[-200.0, 0.0, 0.0], [200.0, 0.0, 0.0]]), 7.78)]


def sign_rows(country="FR", x=10.0, left_hand=False, road=ROADS[0]):
    """townentry._sign's two rows of a commune's sign on the kerb side of traffic going east (into the town)."""
    commune = type("C", (), {"name": "Haguenau", "local": "", "country": country, "sub": ""})()
    line = road[2]
    return townentry._sign(commune, line, x - float(line[0, 0]), 1, road[1].width, left_hand)


class KitTownBoardsTest(unittest.TestCase):
    def test_one_board_per_sign_clear_of_the_road_with_posts(self):
        signs, props, records = [], [], []
        roads.kit_town_boards(sign_rows(), ROADS, (0.0, -128.0, 256.0, 128.0), signs, props, None, False, records)
        self.assertEqual(len(signs), 1)
        mesh, recs = signs[0]
        self.assertIs(recs, records)
        self.assertTrue(mesh.t)
        self.assertEqual([r["name"] for r in records], ["Haguenau"])
        self.assertEqual(records[0]["lettering"], "l1")
        plan = signmesh.town_layout("Haguenau")
        y = records[0]["pos"][1]
        # (driving on the right, going east: the board on the right, south of the road, its near edge off the lanes)
        self.assertLess(y, -(STREET.width / 2 + plan.width / 2 + 0.55))
        self.assertTrue(props and all(p[0] == "fr_signpost" for p in props))
        P = np.asarray(mesh.p) + mesh.origin
        self.assertLess(P[:, 1].max(), -STREET.width / 2)     # (nothing of it over the carriageway)

    def test_only_the_tile_that_owns_it_and_left_hand_side(self):
        signs, props = [], []
        roads.kit_town_boards(sign_rows(x=-100.0), ROADS, (0.0, -128.0, 256.0, 128.0), signs, props)
        self.assertEqual((signs, props), ([], []))
        signs, props, records = [], [], []
        roads.kit_town_boards(sign_rows(left_hand=True), ROADS, (0.0, -128.0, 256.0, 128.0), signs, props, None, True,
                              records)
        self.assertGreater(records[0]["pos"][1], STREET.width / 2)

    def test_build_tile_hands_french_rows_to_the_kit_only_with_it_on(self):
        calls = []

        def towns(roads_, box, left_hand, rings=(), meshed=None):
            calls.append(meshed is not None)
            road = next(r for r in roads_ if r[1].cls == "secondary")
            rows = sign_rows(x=60.0, road=road)
            if meshed is not None:
                meshed.extend(rows)
                return []
            return [[townentry.G_ENTRY] + rows[0][1:8]]

        from test_roads import features, flat, FRAME
        feats = features([[(-200, 100), (400, 100)]], ["secondary"])
        for kit in (None, poleprofiles.FR_KIT):
            signs = [] if kit else None
            props, walks = [], []
            roads.build_tile(FRAME, 0, 0, [feats], flat, walks=walks, props=props, signs=signs, towns=towns,
                             pole_profile=kit)
            if kit:
                self.assertTrue(any(p[0] == "fr_signpost" for p in props))
            else:
                self.assertFalse(any(p[0] == "fr_signpost" for p in props))
        self.assertEqual(calls[0], False)


if __name__ == "__main__":
    unittest.main()
