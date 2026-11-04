"""Moored ships drawn as hulls (ships.py), and the quay beside one kept flat."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildings as bld  # noqa: E402
import ships  # noqa: E402
from test_buildings import Feats  # noqa: E402

# a 200 x 30 m hull, its bow pointed over the last 50 m (x 20..220, y 100..130)
HULL = [(20, 100), (170, 100), (220, 115), (170, 130), (20, 130)]
BOX = [(20, 160), (220, 160), (220, 190), (20, 190)]           # a plain block of the same size
PIER = [(30, 92), (180, 92), (180, 100), (30, 100)]           # a landing stage along the hull's side
WATER = [(np.array([[0.0, 60.0], [256.0, 60.0], [256.0, 256.0], [0.0, 256.0]]), [])]


class ShipTest(unittest.TestCase):
    def setUp(self):
        import geodesy
        self.f = geodesy.EnuFrame(33.742, -118.2775)

    def ll(self, pts):
        p = np.array(pts + pts[:1], np.float64)
        la, lo, _ = self.f.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
        return np.stack([lo, la], 1)

    def items(self, water_xy=WATER, names=("", "", "")):
        b = Feats(["H", "B", "P"], [self.ll(HULL), self.ll(BOX), self.ll(PIER)],
                  **{"class": ["", "", ""], "height": [17.0, 17.0, 22.7], "name": list(names),
                     "src_dataset": ["OpenStreetMap"] * 3})
        items = bld.tile_buildings(self.f, 0, 0, 256, 256, [b], lambda la, lo: np.zeros(np.shape(la)),
                                   water_xy=water_xy)
        return {round(float(s.ring.mean(0)[1]) / 10): (s, g0, g1) for s, g0, g1 in items}

    def test_a_ship_footprint_on_water_is_a_hull_not_a_prism(self):
        got = self.items()
        ship, box = got[11][0], got[17][0]
        self.assertEqual(ship.cls, "ship")
        self.assertNotEqual(box.cls, "ship", "a square-ended block on the water stays a building")
        P = bld.build([got[11]])[0]
        self.assertLess(P[:, 2].min(), -1.0, "the hull reaches under the water")
        # lofted: far more than a prism's corners, and narrower at the waterline than at the deck
        self.assertGreater(len(P), 400)
        xs, tc, b, w, zd = ships.hull_sections(ship, 0.0, ships.STATIONS)
        self.assertTrue((w <= b + 1e-6).all())
        self.assertGreater(zd[0], zd[len(zd) // 2], "the deck rises to the bow")
        # the bow is the pointed end (+x here)
        c, ub, hl, hw = ships.axis(np.asarray(ship.ring))
        self.assertGreater(ub[0], 0.9)
        # its collision is the hull to the deck, not the masts
        C = bld.collision_mesh([got[11]])[0]
        self.assertLess(C[:, 2].max(), ship.floor_h + 0.01)

    def test_on_land_unnamed_it_stays_a_building(self):
        got = self.items(water_xy=None)
        self.assertNotEqual(got[11][0].cls, "ship")

    def test_a_ships_name_makes_it_one_off_mapped_water(self):
        got = self.items(water_xy=None, names=("USS Iowa (BB-61)", "", ""))
        self.assertEqual(got[11][0].cls, "ship")
        self.assertIs(got[11][0].ship["profile"], ships.IOWA_CLASS)

    def test_a_pier_alongside_stays_flat(self):
        got = self.items()
        pier = got[10][0]
        self.assertEqual(pier.cls, "pier")
        self.assertLessEqual(pier.height, ships.PIER_DECK_M)

    def test_a_pier_beside_the_next_tiles_ship_stays_flat(self):
        b = Feats(["H", "P"], [self.ll(HULL), self.ll(PIER)], **{"class": ["", ""], "height": [17.0, 22.7],
                  "name": ["", ""], "src_dataset": ["OpenStreetMap"] * 2})
        items = bld.tile_buildings(self.f, 0, 0, 256, 104, [b], lambda la, lo: np.zeros(np.shape(la)),
                                   water_xy=WATER)
        self.assertEqual([s.cls for s, _, _ in items], ["pier"], "the ship is the next tile's; the pier is flat")

    def test_the_water_test_is_only_asked_for_ship_shapes(self):
        asked = []
        ring = np.array(BOX[::-1], np.float64)
        ring = ring if bld._area2(ring) > 0 else ring[::-1]
        self.assertFalse(ships.is_ship(ring, {"class": ""}, lambda: asked.append(1) or True))
        self.assertEqual(asked, [])

    def land(self, ring, name="", subtype="entertainment", cls="", height=None, parts=None):
        """A footprint on land (no water at all) through tile_buildings; [(Spec, gmin, gmax)]."""
        h = [np.nan if height is None else height]
        b = Feats(["S"], [self.ll(ring)], **{"class": [cls], "subtype": [subtype], "name": [name], "height": h,
                                             "src_dataset": ["OpenStreetMap"]})
        return bld.tile_buildings(self.f, 0, 0, 256, 256, [b], lambda la, lo: np.zeros(np.shape(la)), water_xy=None,
                                  part_layers=parts or ())

    def test_a_ship_on_land_named_in_any_language_is_a_hull(self):
        # (Mikasa: building=ship, historic=ship in OSM; Overture keeps only subtype entertainment, and a Japanese name)
        got = self.land(HULL, name="\u8a18\u5ff5\u8266\u307f\u304b\u3055")
        self.assertEqual([s.cls for s, _, _ in got], ["ship"])
        self.assertFalse(bld.build(got) is None)

    def test_a_ship_shape_on_land_without_evidence_stays_a_building(self):
        for kw in (dict(subtype=""), dict(subtype="commercial"), dict(cls="theatre"),
                   dict(name="SS Great Britain Museum")):
            self.assertNotEqual(self.land(HULL, **kw)[0][0].cls, "ship", kw)

    def test_a_squat_arts_centre_is_not_a_ship(self):
        # (Bristol's Folk House: an attraction's subtype, a pointed end, but 3.2 times as long as wide)
        wide = [(20, 100), (120, 100), (140, 118), (120, 136), (20, 136)]
        self.assertNotEqual(self.land(wide)[0][0].cls, "ship")

    def test_a_lopsided_museum_wing_is_not_a_ship(self):
        # (an attraction's subtype, slender and pointed, but its middle line wanders: M24, Le Mans; Museu do Tesouro Real)
        wing = [(20, 100), (120, 100), (220, 100), (220, 110), (120, 110), (60, 140), (20, 140)]
        self.assertNotEqual(self.land(wing)[0][0].cls, "ship")

    def test_a_tagged_ship_with_both_ends_bluff_is_a_hull(self):
        # (SS Great Britain: both ends about 0.67 of the beam)
        bluff = [(20, 109), (70, 100), (170, 100), (220, 109), (220, 121), (170, 130), (70, 130), (20, 121)]
        self.assertEqual(self.land(bluff, name="SS Great Britain")[0][0].cls, "ship")
        self.assertNotEqual(self.land(bluff, subtype="")[0][0].cls, "ship", "no evidence: a building")

    def part_feats(self, rings, heights):
        return Feats([f"p{i}" for i in range(len(rings))], [self.ll(r) for r in rings],
                     building_id=["S"] * len(rings), height=list(heights), min_height=[0.0] * len(rings),
                     src_dataset=["OpenStreetMap"] * len(rings))

    def test_mapped_parts_keep_their_heights_no_leftover_prism(self):
        # (HMS Belfast: a hull-long part 7.5 m high stood to 85 % of the ship's 41 m as a giant "funnel")
        long_part = [(30, 105), (190, 105), (190, 125), (30, 125)]
        gun = [(100, 114), (110, 114), (110, 116), (100, 116)]
        got = self.land(HULL, name="HMS Example", height=41.0, parts=[self.part_feats([long_part, gun], [7.5, 14.0])])
        self.assertEqual([s.cls for s, _, _ in got], ["ship"])
        P, _, T, *_ = bld.build(got)
        tri = np.asarray(P)[np.asarray(T).reshape(-1, 3)]
        self.assertLessEqual(tri[..., 2].max(), 14.01, "nothing stands over the tallest mapped part")
        xy = tri[..., :2]
        wide = (xy.max(1) - xy.min(1)).max(1) > 12          # anything longer than the gun: the hull and its deck alone
        self.assertLessEqual(tri[wide][..., 2].max(), 12.2, "the long part stays at the deck")

    def test_a_second_outline_on_the_hull_is_not_a_building_too(self):
        dup = [(40, 104), (180, 104), (180, 126), (40, 126)]
        canopy = [(0, 80), (240, 80), (240, 150), (0, 150)]        # a canopy the ship stands in stays
        b = Feats(["H", "D", "C"], [self.ll(HULL), self.ll(dup), self.ll(canopy)],
                  **{"class": ["", "", ""], "subtype": ["entertainment", "", ""], "name": ["", "", ""],
                     "height": [np.nan, 17.0, 3.0], "src_dataset": ["OpenStreetMap"] * 3})
        got = bld.tile_buildings(self.f, 0, 0, 256, 256, [b], lambda la, lo: np.zeros(np.shape(la)), water_xy=None)
        self.assertEqual(sorted(str(s.cls) for s, _, _ in got if s.cls == "ship"), ["ship"])
        self.assertEqual(len(got), 2, "the hull and the canopy; the second outline is gone")

    def test_profiles(self):
        self.assertIs(ships.profile("Iowa Street Apartments"), ships.GENERIC_LINER)
        self.assertIs(ships.profile("RMS Queen Mary"), ships.QUEEN_MARY)
        self.assertIs(ships.profile("Battleship Missouri Memorial"), ships.IOWA_CLASS)
        self.assertIs(ships.profile("", "Essex-class"), ships.ESSEX_CLASS)
        self.assertIs(ships.profile("USS Kidd"), ships.GENERIC_WARSHIP)


if __name__ == "__main__":
    unittest.main()
