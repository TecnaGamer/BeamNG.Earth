"""gasstations: which Overture places are fuel stations (categories, two brand lists), their pins merged into one
station, the shop and the canopy found among the buildings, and the forecourt laid out clear of roads and buildings
(the user, 2026-10-23: "the refueling is the main part")."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gasstations as gs  # noqa: E402


def pin(pid, x, y, name="", brand="", wikidata="", gas=False):
    c = gs.classify("gas_station" if gas else "convenience_store", "", wikidata, "open")
    g, fb, cb = c if c is not None else (gas, False, False)
    return gs.Pin(pid, x, y, name, brand, wikidata, g, fb, cb)


class ClassifyTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(gs.classify("gas_station", "gas_station", "", "open"), (True, False, False))
        self.assertEqual(gs.classify("fueling_station", "", "", None), (True, False, False))
        # a Sheetz near Pittsburgh: a convenience store by its category, fuel by its brand (a fuel brand:
        # the user, 2026-10-23; one of its stores has no canopy in any building data)
        self.assertEqual(gs.classify("convenience_store", "convenience_store", "Q7492551", "open"), (False, True, False))
        self.assertEqual(gs.classify("convenience_store", "", "Q5936320", "open"), (False, False, True))   # Wawa
        # "7-ELEVEN/SPEEDWAY #40001": Western Union by its category, no brand
        self.assertIsNone(gs.classify("financial_service", "money_transfer_service", "", "open"))
        self.assertEqual(gs.classify("atm", "atm", "Q458363", "open"), (False, True, False))     # Marathon
        self.assertIsNone(gs.classify("gas_station", "gas_station", "", "closed"))
        self.assertIsNone(gs.classify("gas_station", "gas_station", "", "permanently_closed"))
        self.assertEqual(gs.classify("gas_station", "gas_station", "Q458363", "open"), (True, True, False))

    def test_brand_name_without_wikidata(self):
        # a Sheetz near Pittsburgh carries the brand's name but no Wikidata id (found 2026-10-23)
        self.assertEqual(gs.classify("convenience_store", "convenience_store", "", "open", "Sheetz"), (False, True, False))
        self.assertEqual(gs.classify("atm", "atm", None, "open", "MARATHON"), (False, True, False))
        self.assertIsNone(gs.classify("convenience_store", "", "", "open", "Sheetz Bakery"))

    def test_brand_lists_disjoint(self):
        self.assertFalse(set(gs.FUEL_BRANDS) & set(gs.CONV_BRANDS))
        self.assertIn("Q7492551", gs.FUEL_BRANDS)       # Sheetz (all 48 around Pittsburgh filed as gas stations)
        self.assertIn("Q5936320", gs.CONV_BRANDS)       # Wawa
        self.assertIn("Q565734", gs.FUEL_BRANDS)        # Aral


class MergeTest(unittest.TestCase):
    def test_merge_speedway_pins(self):
        # A Speedway near Pittsburgh: DAC's pin on the shop, AllThePlaces' beside the pumps, 55 m apart
        a = pin("dac", 0.0, 0.0, "Speedway", gas=True)
        b = gs.Pin("atp", 40.0, 38.0, "7-ELEVEN/SPEEDWAY #40001", "", "", False, False, False)
        st = gs.merge([a, b])
        self.assertEqual(len(st), 1)
        self.assertTrue(st[0].confirmed)
        self.assertEqual(set(st[0].pins), {"dac", "atp"})
        self.assertEqual(st[0].name, "Speedway")

    def test_different_brands_close_not_merged(self):
        a = pin("shell", 0.0, 0.0, "Shell", "Shell", "Q110716465", gas=True)
        b = pin("sunoco", 40.0, 0.0, "Sunoco", "Sunoco", "Q1423218", gas=True)
        self.assertEqual(len(gs.merge([a, b])), 2)

    def test_same_brand_pins_merged(self):
        a = pin("m1", 0.0, 0.0, "Sunoco", "Sunoco", "Q1423218", gas=True)
        b = pin("m2", 30.0, 10.0, "Sunoco Gas Station", "", "", gas=True)
        self.assertEqual(len(gs.merge([a, b])), 1)

    def test_unbranded_pin_does_not_chain_brands(self):
        # review I2: an unbranded gas pin between a Shell and a Sunoco across the street joined both into one station
        a = pin("shell", 0.0, 0.0, "Shell", "Shell", "Q110716465", gas=True)
        m = pin("mid", 20.0, 0.0, "", gas=True)
        b = pin("sunoco", 40.0, 0.0, "Sunoco", "Sunoco", "Q1423218", gas=True)
        self.assertEqual(len(gs.merge([a, m, b])), 2)

    def test_generic_name_words_do_not_merge(self):
        a = pin("joe", 0.0, 0.0, "Joe Gas Mart", gas=True)
        b = pin("main", 40.0, 0.0, "Main Street Gas", gas=True)
        self.assertEqual(len(gs.merge([a, b])), 2)

    def test_far_pins_not_merged(self):
        a = pin("a", 0.0, 0.0, "Sunoco", "Sunoco", "Q1423218", gas=True)
        b = pin("b", 200.0, 0.0, "Sunoco", "Sunoco", "Q1423218", gas=True)
        self.assertEqual(len(gs.merge([a, b])), 2)

    def test_conv_brand_alone_not_confirmed(self):
        st = gs.merge([pin("w", 0.0, 0.0, "Wawa", "Wawa", "Q5936320")])
        self.assertFalse(st[0].confirmed)

    def test_name_without_the_brand_shows_the_brand(self):
        # the Aral at Garmischer Str. 197, Munich: Overture's name is its operator's (the refuel prompt said "Wolfgang
        # Tulach", 2026-10-23); a name that has the brand in it stays
        self.assertEqual(gs.merge([pin("a", 0, 0, "Wolfgang Tulach", "Aral", gas=True)])[0].name, "Aral")
        self.assertEqual(gs.merge([pin("j", 0, 0, "Joby's Gulf", "Gulf", gas=True)])[0].name, "Joby's Gulf")
        self.assertEqual(gs.merge([pin("g", 0, 0, "GetGo Gas Station", "GetGo", gas=True)])[0].name, "GetGo Gas Station")
        self.assertEqual(gs.merge([pin("b", 0, 0, "BP Wolfratshausen", "BP", gas=True)])[0].name, "BP Wolfratshausen")
        self.assertEqual(gs.merge([pin("u", 0, 0, "Main Street Gas", gas=True)])[0].name, "Main Street Gas")
        # the Buc-ee's in New Braunfels: Overture's brand name is another store's ("Buc-ee's Katy Texas"); the brand
        # list's name for its Wikidata id
        st = gs.merge([pin("b", 0, 0, "Buccee s 22 New Braunfels Tx", "Buc-ee's Katy Texas", "Q4982335", gas=True)])[0]
        self.assertEqual(st.name, "Buc-ee's")
        st = gs.merge([pin("b", 0, 0, "Buccee s 22 New Braunfels Tx", "Buc-ee's Katy Texas", gas=True)])[0]    # (no id)
        self.assertEqual(st.name, "Buc-ee's")

    def test_station_id_stable(self):
        ps = [pin("dac", 0.0, 0.0, "Speedway", gas=True), pin("x", 300.0, 0.0, "Shell", "Shell", "Q110716465", gas=True),
              gs.Pin("atp", 40.0, 38.0, "7-ELEVEN/SPEEDWAY #40001", "", "", False, False, False)]
        self.assertEqual(sorted(s.id for s in gs.merge(ps)), sorted(s.id for s in gs.merge(list(reversed(ps)))))


def rect(bid, cx, cy, w, h, cls="", name="", src="OpenStreetMap", height=None):
    """A building: w along x, h along y, centred at (cx, cy)."""
    x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    ring = np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], np.float64)
    return gs.Bld(bid, ring, cls, name, src, height)


ROAD = [(np.array([(-300.0, 0.0), (300.0, 0.0)]), 5.0)]       # a street along y = 0, 10 m wide
STATION = gs.Station("st", 0.0, 45.0, "Speedway", "", ("p",), True)
SHOP = rect("shop", 0.0, 45.0, 20.0, 18.0, cls="retail")
ML = "Microsoft ML Buildings"


class CanopyTest(unittest.TestCase):
    def test_tagged_roof_is_canopy(self):
        roof = rect("roof", 0.0, 22.0, 30.0, 21.0, cls="roof")
        self.assertIs(gs.find_canopy(STATION, SHOP, [SHOP, roof], ROAD), roof)

    def test_single_ml_candidate_is_canopy(self):
        # a Sheetz near Pittsburgh: the shop named in OSM, the canopy an untagged ML outline 1.48 m high
        shop = rect("shop", 0.0, 45.0, 27.0, 20.3, name="Sheetz")
        can = rect("ml", 0.0, 20.0, 28.0, 18.0, src=ML, height=1.48)
        self.assertIs(gs.find_canopy(STATION, shop, [shop, can], ROAD), can)

    def test_two_ml_candidates_none(self):
        a = rect("ml1", -8.0, 20.0, 14.0, 14.0, src=ML)
        b = rect("ml2", 9.0, 21.0, 14.0, 14.0, src=ML)
        self.assertIsNone(gs.find_canopy(STATION, SHOP, [SHOP, a, b], ROAD))

    def test_ml_behind_shop_none(self):
        # a car wash behind the shop (the user, 2026-10-23: replacing one "I wouldn't like that")
        wash = rect("wash", 0.0, 70.0, 28.0, 12.0, src=ML)
        self.assertIsNone(gs.find_canopy(STATION, SHOP, [SHOP, wash], ROAD))

    def test_ml_toward_side_road_on_a_corner(self):
        # a Sheetz near Pittsburgh: the canopy toward the second-nearest road (59 m; the nearest 49 m the other way)
        side = (np.array([(-60.0, -300.0), (-60.0, 300.0)]), 5.0)          # 60 m west of the shop (front: 45 m)
        can = rect("ml", -30.0, 45.0, 18.0, 28.0, src=ML, height=1.48)
        self.assertIs(gs.find_canopy(STATION, SHOP, [SHOP, can], ROAD + [side]), can)

    def test_ml_toward_far_back_road_none(self):
        back = (np.array([(-300.0, 120.0), (300.0, 120.0)]), 5.0)          # 75 m behind the shop (front: 45 m)
        wash = rect("wash", 0.0, 70.0, 28.0, 12.0, src=ML)
        self.assertIsNone(gs.find_canopy(STATION, SHOP, [SHOP, wash], ROAD + [back]))

    def test_two_tagged_roofs_of_a_big_store(self):
        # Buc-ee's, 2760 N I-35, New Braunfels TX: a 155 x 55 m store, two mapped canopies of 157 x 21 m beside it,
        # their centres 52 and 90 m from the store's (the user, 2026-10-23: "canopys are buildings and we put two
        # pumps outside instead")
        shop = rect("shop", 0.0, 80.0, 155.0, 55.0, cls="retail", name="Buc-ee's")
        near = rect("roof1", 0.0, 32.0, 157.0, 21.0, cls="roof")     # 10 m from the store
        far = rect("roof2", 0.0, -5.0, 157.0, 20.0, cls="roof")      # 16.5 m from the first canopy
        road = [(np.array([(-300.0, -60.0), (300.0, -60.0)]), 5.0)]
        st = gs.Station("bucees", 0.0, 80.0, "Buc-ee's", "", ("p",), True)
        self.assertEqual([b.id for b in gs.find_canopies(st, shop, [shop, near, far], road)], ["roof1", "roof2"])
        site = gs.layout(st, [shop, near, far], road)
        self.assertEqual(set(site.drop_ids), {"roof1", "roof2"})
        self.assertEqual(len([p for p in site.props if p["shape"].endswith("s_fuel_structure_06.dae")]), 2)
        self.assertGreater(len(site.areas), 100)

    def test_chained_roofs_need_a_canopy_shape(self):
        # final review I1: a roofed hall 15 m past the canopy, a platform roof chained on from the hall, a 1 x 0.6 m
        # roof by the shop: none of them a canopy (real ones: short side <= 21 m, >= 118 m2, rectangles; 2026-10-23)
        can = rect("can", 0.0, 22.0, 30.0, 13.0, cls="roof")
        hall = rect("hall", 60.0, 22.0, 60.0, 30.0, cls="roof")
        plat = rect("plat", 100.0, 22.0, 10.0, 60.0, cls="roof")      # 5 m from the hall, 80 m from the canopy
        tiny = rect("tiny", 12.0, 34.0, 1.0, 0.6, cls="roof")
        found = gs.find_canopies(STATION, SHOP, [SHOP, can, hall, plat, tiny], ROAD)
        self.assertEqual([b.id for b in found], ["can"])

    def test_far_roof_not_a_canopy(self):
        far = rect("roof", 0.0, -5.0, 30.0, 10.0, cls="roof")        # 36 m from the shop's edge
        road = [(np.array([(-300.0, -100.0), (300.0, -100.0)]), 5.0)]
        self.assertEqual(gs.find_canopies(STATION, SHOP, [SHOP, far], road), [])

    def test_osm_untagged_not_canopy(self):
        b = rect("osm", 0.0, 20.0, 28.0, 18.0)
        self.assertIsNone(gs.find_canopy(STATION, SHOP, [SHOP, b], ROAD))

    def test_ml_wrong_shape_none(self):
        ring = np.array([(-14, 11), (14, 11), (14, 15), (-10, 15), (-10, 29), (-14, 29), (-14, 11)], np.float64)
        ell = gs.Bld("ell", ring, "", "", ML, None)
        self.assertIsNone(gs.find_canopy(STATION, SHOP, [SHOP, ell], ROAD))

    def test_ml_too_big_none(self):
        b = rect("big", 0.0, 22.0, 60.0, 20.0, src=ML)
        self.assertIsNone(gs.find_canopy(STATION, SHOP, [SHOP, b], ROAD))

    def test_shop_ignores_roof(self):
        roof = rect("roof", 0.0, 22.0, 30.0, 21.0, cls="roof")
        st = gs.Station("st", 0.0, 25.0, "Speedway", "", ("p",), True)     # the pin under the canopy
        self.assertIs(gs.find_shop(st, [roof, SHOP]), SHOP)

    def test_shop_prefers_name_match(self):
        other = rect("other", 0.0, 30.0, 10.0, 10.0, cls="retail")
        named = rect("named", 30.0, 45.0, 20.0, 18.0, name="Speedway")
        st = gs.Station("st", 0.0, 32.0, "Speedway", "", ("p",), True)
        self.assertIs(gs.find_shop(st, [other, named]), named)

    def test_shop_prefers_mapped_over_imagery(self):
        # a Sunoco near Pittsburgh: the pin on the road, the nearest outline an untagged imagery one (likely the
        # canopy), the store a mapper's building behind it
        ml = rect("ml", 17.0, 10.0, 13.0, 13.0, src=ML)
        osm = rect("osm", 39.0, 30.0, 14.0, 13.0)
        st = gs.Station("sun", 0.0, 0.0, "Sunoco", "", ("p",), True)
        self.assertIs(gs.find_shop(st, [ml, osm]), osm)
        self.assertIs(gs.find_shop(st, [ml]), ml)               # where imagery is all there is

    def test_shop_not_a_hospital_or_home(self):
        # the Shell at Plinganserstrasse 128, Munich: its kiosk is not mapped; the nearest building is a hospital
        hosp = rect("hosp", -25.0, 45.0, 50.0, 55.0, cls="hospital")
        flats = rect("flats", 20.0, 45.0, 20.0, 15.0, cls="apartments")
        self.assertIsNone(gs.find_shop(STATION, [hosp, flats]))

    def test_no_shop_far(self):
        self.assertIsNone(gs.find_shop(STATION, [rect("far", 300.0, 300.0, 20.0, 20.0)]))

    def test_home_shop_beside_a_canopy(self):
        # (a player's report, Nagambie, Victoria: United Petroleum's shop mapped as a residential building, its canopy
        # a mapped roof beside it; the shop never found, the roof drawn as a solid block, the pumps bare at the pin)
        home = rect("home", 0.0, 45.0, 20.0, 18.0, cls="residential", height=3.0)
        roof = rect("roof", 0.0, 24.0, 15.0, 10.0, cls="roof")
        self.assertIsNone(gs.find_shop(STATION, [home, roof]))
        self.assertIs(gs._home_shop(STATION, [home, roof], ROAD), home)
        for cls in ("house", "detached", "bungalow"):
            h = rect("h", 0.0, 45.0, 20.0, 18.0, cls=cls)
            self.assertIs(gs._home_shop(STATION, [h, roof], ROAD), h, cls)

    def test_home_shop_needs_a_canopy(self):
        home = rect("home", 0.0, 45.0, 20.0, 18.0, cls="residential")
        self.assertIsNone(gs._home_shop(STATION, [home], ROAD))
        # a carport is no canopy
        self.assertIsNone(gs._home_shop(STATION, [home, rect("carport", 0.0, 32.0, 7.0, 6.0, cls="roof")], ROAD))
        # nor a roof across the street
        self.assertIsNone(gs._home_shop(STATION, [home, rect("roof", 0.0, -12.0, 15.0, 10.0, cls="roof")], ROAD))
        # nor a platform roof
        self.assertIsNone(gs._home_shop(STATION, [home, rect("roof", 0.0, 24.0, 120.0, 30.0, cls="roof")], ROAD))

    def test_home_shop_only_a_low_home(self):
        roof = rect("roof", 0.0, 24.0, 15.0, 10.0, cls="roof")
        for b in (rect("flats", 0.0, 45.0, 20.0, 18.0, cls="apartments"),
                  rect("hosp", 0.0, 45.0, 20.0, 18.0, cls="hospital"),
                  rect("church", 0.0, 45.0, 20.0, 18.0, cls="church"),
                  rect("tall", 0.0, 45.0, 20.0, 18.0, cls="residential", height=12.0)):
            self.assertIsNone(gs._home_shop(STATION, [b, roof], ROAD), b.cls)
        # a convenience brand alone (no gas station pin) gets none
        wawa = gs.Station("wawa", 0.0, 45.0, "Wawa", "Wawa", ("p",), False)
        self.assertIsNone(gs._home_shop(wawa, [rect("home", 0.0, 45.0, 20.0, 18.0, cls="residential"), roof], ROAD))


def inside_box(bld, x, y, pad=0.5):
    c, u, hl, hw = gs._obox(bld.ring)
    v = np.array([x, y]) - c
    return abs(v @ u) <= hl + pad and abs(v[0] * u[1] - v[1] * u[0]) <= hw + pad


def road_gap(x, y):
    return abs(y) - 5.0         # from ROAD's carriageway edge


class LayoutTest(unittest.TestCase):
    def test_canopy_layout(self):
        # a mapped canopy gets the lanes' style (the user, 2026-10-23): one roof slab over its outline, islands across
        # its short side, one every ~7 m along its long side
        roof = rect("roof", 0.0, 22.0, 30.0, 21.0, cls="roof")
        site = gs.layout(STATION, [SHOP, roof], ROAD)
        self.assertEqual(site.drop_ids, ("roof",))
        self.assertEqual([p["shape"].rsplit("/", 1)[-1] for p in site.props if "s_fuel_structure_0" in p["shape"]
                          and not p["shape"].endswith("_07.dae")], ["s_fuel_structure_06.dae"])
        self.assertEqual(len(site.areas), 12)           # 4 islands (30 m / 7) of 3 pumps (19 m / 6.5)
        for a in site.areas:
            self.assertAlmostEqual(a["yaw"] % 180.0, 90.0, places=3)     # islands along y: across the short side
        for p in site.props + site.areas:
            self.assertTrue(inside_box(roof, p["x"], p["y"], pad=1.5), p)

    def test_platform_roof_beside_the_shop_is_no_canopy(self):
        # (GitHub #308, Rio de Janeiro: a train station's 1355 m2 platform roof beside a station's lot took its pumps,
        # on the tracks) a tagged roof only in a canopy's shape
        platform = rect("platform", 0.0, 80.0, 120.0, 30.0, cls="roof")
        self.assertIsNone(gs.find_canopy(STATION, SHOP, [SHOP, platform], ROAD))

    def test_lot_named_like_the_station_gets_a_canopy_in_it(self):
        # (GitHub #308: Rio's Posto Ipiranga, its whole 2323 m2 lot mapped as one building named "Ipiranga", was drawn as
        # a big block, its pumps elsewhere) the lot's box dropped, a canopy-sized roof in its middle, the pumps under it
        st = gs.Station("st", 0.0, 40.0, "Posto Ipiranga", "Ipiranga", ("p",), True)
        lot = rect("lot", 0.0, 40.0, 54.0, 43.0, name="Ipiranga")
        site = gs.layout(st, [lot], ROAD)
        self.assertIsNotNone(site)
        self.assertEqual(site.drop_ids, ("lot",))
        for p in site.areas:
            self.assertTrue(inside_box(lot, p["x"], p["y"]), p)
            self.assertLessEqual(abs(p["x"]), 0.5 * 54.0 * (gs.LOT_CANOPY_M2 / (54.0 * 43.0)) ** 0.5 + 1.5)
        # a big building not named like the station stays a building (a supermarket's pumps, a shop of its own)
        self.assertFalse(gs._is_lot(rect("mkt", 0.0, 40.0, 54.0, 43.0, name="Supermercado"), st))

    def test_lot_named_as_a_fuel_station(self):
        # (GitHub #308: a BR station's lot mapped as "Auto Posto Jari LTDA", 1661 m2, drawn four storeys high)
        st = gs.Station("st", 0.0, 40.0, "BR", "BR", ("p",), True)
        self.assertTrue(gs._is_lot(rect("lot", 0.0, 40.0, 41.0, 41.0, cls="commercial", name="Auto Posto Jari LTDA"), st))
        self.assertTrue(gs._is_lot(rect("lot", 0.0, 40.0, 41.0, 41.0, name="Tankstelle Meier"), st))
        self.assertFalse(gs._is_lot(rect("lot", 0.0, 40.0, 41.0, 41.0, name="Postal Office"), st))

    def test_canopy_never_across_the_street(self):
        # (GitHub #308: an imagery outline on the other corner took the station) the shop north of the street, a
        # canopy-sized imagery outline south of it: none
        across = rect("ml", 0.0, -20.0, 16.0, 10.0, src=ML)
        self.assertIsNone(gs.find_canopy(STATION, SHOP, [SHOP, across], ROAD))
        roof = rect("roof", 0.0, -22.0, 30.0, 21.0, cls="roof")
        self.assertIsNone(gs.find_canopy(STATION, SHOP, [SHOP, roof], ROAD))

    def test_canopy_across_the_street_from_a_pin_on_the_road(self):
        # (a Sunoco in Pittsburgh, BNGE_GAS_REAL: its pin 2.4 m from the road's centre line, the nearest building north
        # of the road, its two mapped canopies south of it) a pin on the carriageway does not say which side the
        # station is on: the canopy is taken, as before GitHub #308
        st = gs.Station("sun", 0.0, 2.4, "Sunoco", "", ("p",), True)
        shop = rect("shop", 0.0, 20.0, 16.0, 14.0)
        roof = rect("roof", 0.0, -20.0, 16.0, 10.0, cls="roof")
        self.assertIs(gs.find_canopy(st, shop, [shop, roof], ROAD), roof)
        self.assertEqual(gs.find_canopies(st, shop, [shop, roof], ROAD), [roof])
        site = gs.layout(st, [shop, roof], ROAD)
        self.assertIsNotNone(site)
        self.assertEqual(site.drop_ids, ("roof",))

    def test_irregular_roof_beside_the_shop_is_canopy(self):
        # (a BP in Pittsburgh, BNGE_GAS_REAL: its canopy a mapped roof of 26 x 25 m, 461 m2, not a rectangle, against the
        # shop) a tagged roof of a canopy's area is the shop's canopy whatever its outline; a bigger one never (#308)
        ring = np.array([(-13.0, 0.0), (13.0, 0.0), (13.0, 25.0), (0.0, 25.0), (0.0, 12.0), (-13.0, 12.0), (-13.0, 0.0)])
        roof = gs.Bld("roof", ring + np.array([0.0, 11.0]), "roof", "", "OpenStreetMap", 4.2)
        self.assertFalse(gs._canopy_shape(roof))
        st = gs.Station("bp", 0.0, 45.0, "BP", "bp", ("p",), True)
        self.assertIs(gs.find_canopy(st, SHOP, [SHOP, roof], ROAD), roof)
        hall = gs.Bld("hall", ring * 1.6 + np.array([0.0, 6.0]), "roof", "", "OpenStreetMap", None)
        self.assertIsNone(gs.find_canopy(st, SHOP, [SHOP, hall], ROAD))

    def test_long_canopy_one_roof(self):
        roof = rect("roof", 0.0, 22.0, 46.0, 18.0, cls="roof")
        site = gs.layout(STATION, [SHOP, roof], ROAD)
        self.assertEqual(len([p for p in site.props if p["shape"].endswith("s_fuel_structure_06.dae")]), 1)
        self.assertEqual(len([p for p in site.props if p["shape"].endswith("s_fuel_structure_07.dae")]), 12)

    def test_narrow_canopy_short_islands_across_it(self):
        # a Speedway near Pittsburgh: its mapped canopy is 73 x 8.6 m; the user's photo, 2026-10-23: short
        # islands across the canopy one after another, one dispenser and a pillar on each, bollards at their ends
        roof = rect("roof", 0.0, 22.0, 73.0, 8.6, cls="roof")
        site = gs.layout(STATION, [SHOP, roof], ROAD)
        self.assertEqual(len(site.areas), 10)                          # 72 m / 7: 10 islands, one pump each
        for a in site.areas:
            self.assertAlmostEqual(a["yaw"] % 180.0, 90.0, places=3)   # the islands across the canopy (along y)
            self.assertAlmostEqual(a["y"], 22.0, places=3)             # the pump in the island's middle
        pillars = [p for p in site.props if p["shape"].endswith("s_fuel_structure_07.dae")]
        self.assertEqual(len(pillars), 10)                             # one per island ...
        for p in pillars:
            self.assertGreater(p["y"], 22.5)                           # ... behind its pump (away from the road)
        self.assertEqual(len([p for p in site.props if "bollard" in p["shape"]]), 20)

    def test_long_lanes_split_into_islands(self):
        long_ladder = [lane(x, 12.0, x, 46.0) for x in (-14.0, -7.0, 0.0)]         # 34 m lanes
        shop = rect("shop", 0.0, 75.0, 20.0, 18.0, cls="retail")
        st = gs.Station("st", 0.0, 75.0, "Speedway", "", ("p",), True)
        site = gs.layout(st, [shop], ROAD, long_ladder)
        pillars = [p for p in site.props if p["shape"].endswith("s_fuel_structure_07.dae")]
        self.assertEqual(len(pillars), 8)                               # 2 islands of 32 m: each split in 2

    def test_narrow_canopy_pumps_clear_of_the_shop(self):
        # review I3: a kit scaled down no further than KIT_SCALE[0] put pumps past a narrow canopy, into the shop
        roof = rect("roof", 0.0, 32.0, 26.0, 6.0, cls="roof")
        site = gs.layout(STATION, [SHOP, roof], ROAD)
        self.assertTrue(site.areas)
        for a in site.areas:
            self.assertFalse(inside_box(SHOP, a["x"], a["y"], pad=gs.BLD_CLEAR_M + a["half"][0]), a)

    def test_no_canopy_pump_pair(self):
        site = gs.layout(STATION, [SHOP], ROAD)
        self.assertEqual(site.drop_ids, ())
        pumps = [p for p in site.props if "s_fuel_pump_" in p["shape"] and p["collide"]]
        self.assertEqual(len(pumps), 2)
        self.assertEqual(len(site.areas), 2)
        for p in pumps:
            self.assertTrue(5.0 < p["y"] < 36.0, p)                     # between the road and the shop's front
        self.assertAlmostEqual(pumps[0]["y"], pumps[1]["y"], places=6)  # a row parallel to the shop's front

    def test_pin_on_road_pumps_off_road(self):
        # a Sunoco near Pittsburgh: Microsoft's pin on the road beside the station
        st = gs.Station("sun", 3.0, 0.0, "Sunoco Gas Station", "", ("p",), True)
        shop = rect("shop", 0.0, 30.0, 12.0, 10.0)
        site = gs.layout(st, [shop], ROAD)
        self.assertIsNotNone(site)
        for p in site.props:
            self.assertGreater(road_gap(p["x"], p["y"]), gs.ROAD_CLEAR_M, p)
            self.assertFalse(inside_box(shop, p["x"], p["y"], pad=gs.BLD_CLEAR_M), p)

    def test_forecourt_beside_the_shop(self):
        # a Kwik Fill near Pittsburgh (2026-10-23): the shop's front close to the road, the forecourt and the
        # pin beside it; the pair at the pin, clear of both
        shop = rect("shop", 0.0, 12.0, 20.0, 10.0)
        st = gs.Station("kf", 22.0, 16.0, "Kwik Fill", "Kwik Fill", ("p",), True)
        site = gs.layout(st, [shop], ROAD)
        self.assertIsNotNone(site)
        for p in site.props:
            self.assertLess(np.hypot(p["x"] - st.x, p["y"] - st.y), 20.0, p)
            self.assertGreater(road_gap(p["x"], p["y"]), gs.ROAD_CLEAR_M, p)
            self.assertFalse(inside_box(shop, p["x"], p["y"], pad=gs.BLD_CLEAR_M), p)

    def test_lanes_away_from_the_canopy_ignored(self):
        # final review I2: three service lanes in the next lot, beside a mapped canopy: the canopy's islands, not the
        # lanes'
        roof = rect("roof", 0.0, 22.0, 30.0, 13.0, cls="roof")
        lanes = [lane(x, 12.0, x, 30.0) for x in (40.0, 47.0, 54.0)]
        site = gs.layout(STATION, [SHOP, roof], ROAD, service=lanes)
        self.assertEqual(site.drop_ids, ("roof",))
        for a in site.areas:
            self.assertTrue(inside_box(roof, a["x"], a["y"]), a)

    def test_no_room_no_site(self):
        shop = rect("shop", 0.0, 13.0, 20.0, 10.0)          # 3 m between the road's edge and the shop
        st = gs.Station("st", 0.0, 13.0, "Shell", "Shell", ("p",), True)
        self.assertIsNone(gs.layout(st, [shop], ROAD))

    def test_no_shop_pair_at_the_pin(self):
        # the Agip at Boschetsrieder Str. 162, Munich: the pin on the forecourt, no shop mapped, the road 22 m away
        st = gs.Station("agip", 0.0, 22.0, "Agip", "Agip", ("p",), True)
        flats = rect("flats", 0.0, 60.0, 20.0, 12.0, cls="apartments")
        site = gs.layout(st, [flats], ROAD)
        self.assertIsNotNone(site)
        self.assertEqual(site.drop_ids, ())
        for p in site.props:
            self.assertLess(np.hypot(p["x"] - st.x, p["y"] - st.y), 30.0, p)
            self.assertGreater(road_gap(p["x"], p["y"]), gs.ROAD_CLEAR_M, p)
            self.assertFalse(inside_box(flats, p["x"], p["y"], pad=gs.BLD_CLEAR_M), p)

    def test_no_shop_pin_near_the_road(self):
        # the Shell at Plinganserstrasse 128, Munich: its pin 8.5 m from the road's centre line, the forecourt behind it
        st = gs.Station("shell", 0.0, 8.5, "Shell", "Shell", ("p",), True)
        site = gs.layout(st, [], ROAD)
        self.assertIsNotNone(site)
        for p in site.props:
            self.assertLess(np.hypot(p["x"] - st.x, p["y"] - st.y), 30.0, p)
            self.assertGreater(road_gap(p["x"], p["y"]), gs.ROAD_CLEAR_M, p)

    def test_home_shop_canopy_layout(self):
        # (Nagambie) the canopy beside a residential shop is the canopy: left out of the buildings, the pumps under it
        home = rect("home", 0.0, 45.0, 20.0, 18.0, cls="residential", height=3.0)
        roof = rect("roof", 0.0, 24.0, 15.0, 10.0, cls="roof")
        site = gs.layout(STATION, [home, roof], ROAD)
        self.assertIsNotNone(site)
        self.assertEqual(site.drop_ids, ("roof",))
        self.assertEqual(site.shop_id, "home")
        for a in site.areas:
            self.assertTrue(inside_box(roof, a["x"], a["y"], pad=1.5), a)
        # without the roof: the pumps at the pin, as before
        self.assertEqual(gs.layout(gs.Station("st", 0.0, 22.0, "Speedway", "", ("p",), True), [home], ROAD).drop_ids, ())

    def test_no_shop_no_site(self):
        st = gs.Station("st", 0.0, 200.0, "Shell", "Shell", ("p",), True)
        self.assertIsNone(gs.layout(st, [SHOP], ROAD))

    def test_conv_brand_needs_canopy(self):
        st = gs.Station("wawa", 0.0, 45.0, "Wawa", "Wawa", ("p",), False)
        self.assertIsNone(gs.layout(st, [SHOP], ROAD))
        can = rect("ml", 0.0, 20.0, 28.0, 18.0, src=ML, height=1.48)
        self.assertIsNotNone(gs.layout(st, [SHOP, can], ROAD))

    def test_pumps_match_the_roof(self):
        # the roof slab (West Coast's s_fuel_structure_06) carries Trilobite's sign: every pump is Trilobite's too (the
        # user, 2026-10-23: "the pumps are changing brands but the canopy is not")
        roof = rect("roof", 0.0, 22.0, 30.0, 21.0, cls="roof")
        shapes = set()
        for i in range(50):
            st = gs.Station(f"s{i}", 0.0, 45.0, "x", "", ("p",), True)
            for site in (gs.layout(st, [SHOP, roof], ROAD), gs.layout(st, [SHOP], ROAD)):
                shapes |= {p["shape"].rsplit("/", 1)[-1] for p in site.props if "s_fuel_pump_" in p["shape"]}
        self.assertEqual(shapes, {"s_fuel_pump_2015.dae", "s_fuel_pump_2015_trilobite.dae"})


class RegionTest(unittest.TestCase):
    """Stations by region (the user, 2026-10-23: "I want to use them based on region so we should take advantage of
    the italy assets for europe")."""

    def test_style_of(self):
        self.assertEqual(gs.style_of(48.8566, 2.3522), "europe")        # Paris
        self.assertEqual(gs.style_of(41.9028, 12.4964), "europe")       # Rome
        self.assertEqual(gs.style_of(47.37, 8.54), "europe")            # Zurich (alpine)
        self.assertEqual(gs.style_of(40.403093, -80.041343), "us_east")   # the Pittsburgh area
        self.assertEqual(gs.style_of(33.75, -84.39), "us_east")         # Atlanta (us_south)
        self.assertEqual(gs.style_of(34.05, -118.24), "default")        # Los Angeles
        self.assertEqual(gs.style_of(35.68, 139.69), "default")         # Tokyo

    def test_europe_canopy_italy_sections(self):
        roof = rect("roof", 0.0, 22.0, 30.0, 13.0, cls="roof")
        site = gs.layout(STATION, [SHOP, roof], ROAD, style="europe")
        self.assertEqual(site.drop_ids, ("roof",))
        names = [p["shape"].rsplit("/", 1)[-1] for p in site.props]
        self.assertFalse([n for n in names if n.startswith("s_fuel_")], names)
        self.assertEqual(names.count("italy_petrolstation_awningSt.DAE"), 4)   # 30 m: 2 sections of two halves
        self.assertEqual(len(site.areas), 8)                                   # 2 pumps under each half
        for p in site.props:
            if "_pump_" in p["shape"]:
                self.assertAlmostEqual(p["yaw"] % 180.0, 0.0, places=3)        # along the canopy (x)
                self.assertTrue(inside_box(roof, p["x"], p["y"]), p)
        self.assertEqual(names.count("italy_petrolstation_brandsign.DAE"), 1)

    def test_europe_pair_italy_pumps(self):
        site = gs.layout(STATION, [SHOP], ROAD, style="europe")
        names = {p["shape"].rsplit("/", 1)[-1] for p in site.props if "pump" in p["shape"]}
        self.assertTrue(names and all(n.startswith("italy_petrolstation_pump_") for n in names), names)

    def eca(self, site):
        units = [p for p in site.props if p["shape"].endswith("eca_gastation_pumps.DAE")]
        self.assertFalse([p for p in site.props if "s_fuel_" in p["shape"]], site.props)
        return units

    def test_us_east_canopy_eca_units(self):
        # a 36 x 8 m canopy: islands across it in threes for East Coast's units (6, ~5.8 m apart), two units over them
        roof = rect("roof", 0.0, 22.0, 36.0, 8.0, cls="roof")
        site = gs.layout(STATION, [SHOP, roof], ROAD, style="us_east")
        self.assertEqual(site.drop_ids, ("roof",))
        units = self.eca(site)
        self.assertEqual(len(units), 2)
        for u in units:
            self.assertAlmostEqual(u["yaw"] % 180.0, 0.0, places=3)            # its x (across its islands) along x
            a = np.radians(u["yaw"])
            ex, ey = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
            c = np.array([u["x"], u["y"]]) + ex * (gs.ECA_ROOF_XY[0] * u["scale"][0]) + ey * (gs.ECA_ROOF_XY[1] * u["scale"][1])
            self.assertTrue(inside_box(roof, *c), (u, c))                      # its roof over the canopy
        self.assertEqual(len(site.areas), 6)
        lanes = gs._outline_lanes(roof, 3, gs.ECA_ROOF_STEP, gs.ECA_MIN_GAP_M)
        mids = sorted((o0 + o1) / 2 for o0, o1 in zip(lanes.offsets[:-1], lanes.offsets[1:]))
        for x, want in zip(sorted(a["x"] for a in site.areas), mids):
            self.assertAlmostEqual(x, want, delta=0.75)                       # (between the lanes)
        self.on_islands(site)
        for a in site.areas:
            self.assertAlmostEqual(a["yaw"] % 180.0, 90.0, places=3)           # along the island, across the canopy
            self.assertTrue(inside_box(roof, a["x"], a["y"]), a)

    def test_us_east_lanes_islands_between_them(self):
        # the four mapped lanes 7 m apart and 18 m long: three islands between them, three rows of units along them
        site = gs.layout(STATION, [SHOP], ROAD, service=LADDER, style="us_east")
        units = self.eca(site)
        self.assertEqual(len(units), 3)
        self.assertEqual(len(site.areas), 9)
        for x, want in zip(sorted({round(a["x"], 2) for a in site.areas}), (-10.5, -3.5, 3.5)):
            self.assertAlmostEqual(x, want, delta=0.75)
        self.on_islands(site)
        for a in site.areas:
            self.assertAlmostEqual(a["yaw"] % 180.0, 90.0, places=3)
            self.assertTrue(12.0 < a["y"] < 30.0, a)

    def on_islands(self, site):
        """Every refuel area at one of the units' own islands (its dispenser's place in the model, stretched)."""
        units = self.eca(site)
        for a in site.areas:
            best = min(abs((a["x"] - u["x"]) - ix * u["scale"][0]) for u in units for ix in gs.ECA_ISLAND_X)
            self.assertLess(best, 0.01, a)

    def test_us_east_units_one_roof(self):
        # the user's screenshot, 2026-10-23: a gap between the units' roofs ("can we do just one big canopy"; option 1:
        # the East Coast units, joined up): six islands, two units, the first's roof ending where the second's begins
        roof = rect("roof", 0.0, 22.0, 43.0, 8.0, cls="roof")
        units = sorted(self.eca(gs.layout(STATION, [SHOP, roof], ROAD, style="us_east")), key=lambda u: u["x"])
        self.assertEqual(len(units), 2)
        right = units[0]["x"] + gs.ECA_ROOF_X[1] * units[0]["scale"][0]
        left = units[1]["x"] + gs.ECA_ROOF_X[0] * units[1]["scale"][0]
        self.assertAlmostEqual(right, left, delta=0.05)

    def test_us_east_mapped_five_islands(self):
        # six mapped lanes (a Sheetz near Pittsburgh): five islands, the second unit back over the first, its shared
        # island the first's (one island, not two side by side), every island between its lanes
        six = [lane(x, 12.0, x, 30.0) for x in (-21.0, -14.0, -7.0, 0.0, 7.0, 14.0)]
        site = gs.layout(STATION, [SHOP], ROAD, service=six, style="us_east")
        units = self.eca(site)
        row = [u for u in units if abs(u["y"] - min(v["y"] for v in units)) < 0.01]
        self.assertEqual(len(row), 2)
        isl = sorted(u["x"] + ix * u["scale"][0] for u in row for ix in gs.ECA_ISLAND_X)
        distinct = [x for k, x in enumerate(isl) if k == 0 or x - isl[k - 1] > 0.05]
        self.assertEqual(len(distinct), 5, isl)
        for x, want in zip(distinct, (-17.5, -10.5, -3.5, 3.5, 10.5)):
            self.assertAlmostEqual(x, want, delta=0.75)
        self.on_islands(site)

    def test_us_east_small_canopy_west_coast_islands(self):
        # final review I3: a canopy with room for two islands: East Coast's three-island unit would overhang it by 5 m;
        # West Coast's islands under the roof slab instead
        roof = rect("roof", 0.0, 22.0, 14.0, 8.0, cls="roof")
        site = gs.layout(STATION, [SHOP, roof], ROAD, style="us_east")
        self.assertFalse([p for p in site.props if "eca_gastation" in p["shape"]], site.props)
        self.assertTrue(site.areas)
        for a in site.areas:
            self.assertTrue(inside_box(roof, a["x"], a["y"]), a)

    def test_us_east_overlapping_unit_lifted(self):
        # final review: five islands, the second unit back over the first's last island: its roof a little higher, not
        # in the same plane (z-fighting)
        roof = rect("roof", 0.0, 22.0, 36.0, 8.0, cls="roof")
        units = self.eca(gs.layout(STATION, [SHOP, roof], ROAD, style="us_east"))
        self.assertEqual(sorted(u["dz"] > 0.0 for u in units), [False, True])

    def test_us_east_pair_west_coast_pumps(self):
        site = gs.layout(STATION, [SHOP], ROAD, style="us_east")
        self.assertTrue(any(p["shape"].endswith("s_fuel_pump_2015.dae") for p in site.props))

    def test_style_shapes(self):
        # the shapes the mod preloads for the anchor's region (earthd anchor_ready fuelShapes): only that region's (a
        # Steam Deck's memory: the user, 2026-10-23), and every shape that region's layouts can use
        names = {k: {x.rsplit("/", 1)[-1] for x in gs.style_shapes(k)} for k in ("default", "europe", "us_east")}
        self.assertFalse([n for n in names["default"] if n.startswith(("italy_", "eca_"))], names["default"])
        self.assertFalse([n for n in names["europe"] if n.startswith(("s_fuel_", "eca_"))], names["europe"])
        self.assertIn("eca_gastation_pumps.DAE", names["us_east"])
        self.assertTrue(names["default"] <= names["us_east"], "East Coast's fall back to West Coast's islands")
        for style, roof in (("default", self.ROOF_EAST), ("europe", self.ROOF_EAST), ("us_east", self.ROOF_EAST),
                            ("us_east", rect("small", 0.0, 22.0, 14.0, 8.0, cls="roof"))):
            for site in (gs.layout(STATION, [SHOP, roof], ROAD, style=style), gs.layout(STATION, [SHOP], ROAD, style=style)):
                used = {p["shape"] for p in site.props}
                self.assertTrue(used <= set(gs.style_shapes(style)), (style, used - set(gs.style_shapes(style))))

    ROOF_EAST = rect("roof", 0.0, 22.0, 36.0, 8.0, cls="roof")

    def test_tile_sites_by_region(self):
        md = station_md()
        orig = gs.style_of
        gs.style_of = lambda lat, lon: "europe"
        try:
            recs, skip = gs.tile_sites(FRAME, 0, 0, md, flat)
        finally:
            gs.style_of = orig
        self.assertIn("b-roof", skip)
        self.assertTrue(any(p["shape"].endswith("italy_petrolstation_awningSt.DAE") for p in recs[0]["props"]))


def in_pad(site, x, y):
    import polygon
    pt = np.array([[x, y]])
    return any(bool(polygon.contains_points(np.vstack([r, r[:1]]), pt)[0]) for r in site.pad)


class PadTest(unittest.TestCase):
    """The forecourt's asphalt (the user, 2026-10-23: "some stations spawn on dirt because we do not paint ashpat
    there"; "I'd start with the pad and driveway, some driveways are there if caught by OSM but some do not")."""

    ROOF = rect("roof", 0.0, 22.0, 30.0, 13.0, cls="roof")      # y 15.5 .. 28.5; the road's edge at y = 5

    def test_pad_canopy_margin_shop_and_driveway(self):
        site = gs.layout(STATION, [SHOP, self.ROOF], ROAD)
        self.assertTrue(in_pad(site, 0.0, 22.0))
        self.assertTrue(in_pad(site, 20.0, 22.0), "5 m past the canopy's end")
        self.assertFalse(in_pad(site, 23.0, 22.0), "not more than the margin past it")
        self.assertTrue(in_pad(site, 0.0, 35.0), "up to the shop's front (y = 36)")
        self.assertTrue(in_pad(site, 0.0, 7.0), "the driveway to the road")
        self.assertFalse(in_pad(site, 0.0, 3.0), "not across the road")
        self.assertFalse(in_pad(site, 30.0, 7.0), "the driveway a strip, not the whole frontage")

    def test_mapped_driveway_no_strip(self):
        drive = [lane(0.0, 14.0, 0.0, 2.0)]        # a service road from the forecourt into the street
        with_drive = gs.layout(STATION, [SHOP, self.ROOF], ROAD, service=drive)
        without = gs.layout(STATION, [SHOP, self.ROOF], ROAD)
        self.assertEqual(len(with_drive.pad), len(without.pad) - 1)
        self.assertFalse(in_pad(with_drive, 0.0, 7.0), "the mapped driveway is painted as a road already")

    def test_pad_stops_short_of_a_neighbour(self):
        house = rect("house", 23.0, 22.0, 6.0, 10.0, cls="house")       # its west wall at x = 20, 5 m off the canopy
        site = gs.layout(STATION, [SHOP, self.ROOF, house], ROAD)
        self.assertTrue(in_pad(site, 18.0, 22.0))
        self.assertFalse(in_pad(site, 19.8, 22.0), "half a metre short of the house")

    def test_pump_pair_pad(self):
        site = gs.layout(STATION, [SHOP], ROAD)
        for a in site.areas:
            self.assertTrue(in_pad(site, a["x"], a["y"]), a)

    SIDE = (np.array([(-40.0, -300.0), (-40.0, 300.0)]), 4.0)        # a side street 15 m west of the pad

    def test_corner_lot_two_driveways(self):
        # the Speedway (2026-10-23, the user's screenshot): an entrance on the side street too, as in real life
        site = gs.layout(STATION, [SHOP, self.ROOF], ROAD + [self.SIDE])
        self.assertEqual(len(site.drives), 2)
        self.assertTrue(in_pad(site, -30.0, 22.0), "the driveway to the side street")
        self.assertTrue(in_pad(site, 0.0, 7.0), "and the one to the main road")

    def test_back_street_no_second_driveway(self):
        back = (np.array([(-300.0, 75.0), (300.0, 75.0)]), 4.0)        # parallel to the main road, behind the shop
        site = gs.layout(STATION, [SHOP, self.ROOF], ROAD + [back])
        self.assertEqual(len(site.drives), 1)

    def test_mapped_access_no_made_up_driveways(self):
        # a Sheetz near Pittsburgh (the user, 2026-10-23: "can we skip drawing drivewas if there is already a service
        # road there for access"): mapped into the main road, so none made up to the side street either
        drive = [lane(0.0, 14.0, 0.0, 2.0)]
        site = gs.layout(STATION, [SHOP, self.ROOF], ROAD + [self.SIDE], service=drive)
        self.assertEqual(site.drives, [])
        self.assertFalse(in_pad(site, -30.0, 22.0))

    def test_mapped_access_through_two_service_roads(self):
        drive = [lane(0.0, 14.0, 25.0, 12.0), lane(25.0, 12.0, 30.0, 2.0)]     # off the forecourt, then to the road
        site = gs.layout(STATION, [SHOP, self.ROOF], ROAD, service=drive)
        self.assertEqual(site.drives, [])

    def test_mapped_access_alongside_the_forecourt(self):
        # a service road running past the forecourt's side, 7 m off the pad's edge, into the street
        drive = [lane(28.0, 40.0, 28.0, 2.0)]
        site = gs.layout(STATION, [SHOP, self.ROOF], ROAD, service=drive)
        self.assertEqual(site.drives, [])

    def test_service_road_elsewhere_still_a_driveway(self):
        far = [lane(80.0, 40.0, 80.0, 2.0)]         # another lot's driveway
        site = gs.layout(STATION, [SHOP, self.ROOF], ROAD, service=far)
        self.assertEqual(len(site.drives), 1)

    def test_drives_as_walk_cuts(self):
        site = gs.layout(STATION, [SHOP, self.ROOF], ROAD)
        (line, half), = site.drives
        self.assertAlmostEqual(half, gs.DRIVEWAY_W_M / 2)
        self.assertLess(abs(float(line[-1][1]) - (5.0 - gs.PAD_BLD_GAP_M)), 0.6, "to the road's edge")

    def test_driveway_blocked_by_a_building_left_out(self):
        wall = rect("wall", 0.0, 7.0, 60.0, 3.0, cls="retail")          # a building between the forecourt and the road
        site = gs.layout(STATION, [SHOP, self.ROOF, wall], ROAD)
        self.assertFalse(in_pad(site, 0.0, 4.8))
        self.assertFalse(any(in_pad(site, x, 7.0) for x in (-20.0, 0.0, 20.0)), "nothing painted through it")


def lane(x0, y0, x1, y1):
    return np.array([(x0, y0), (x1, y1)], np.float64)


# a Sheetz near Pittsburgh: its forecourt's drive lanes mapped as six short parallel service roads, 7 m apart
# (the user, 2026-10-23: "you see the rows thats the roads for the pumps")
LADDER = [lane(-14.0, 12.0, -14.0, 30.0), lane(-7.0, 12.0, -7.0, 30.0), lane(0.0, 12.0, 0.0, 30.0),
          lane(7.0, 12.0, 7.0, 30.0)]


class LanesTest(unittest.TestCase):
    def test_ladder_found(self):
        ln = gs.find_lanes(SHOP, LADDER + [lane(-60.0, 5.0, 60.0, 8.0)])     # (and a long service road: not a lane)
        self.assertIsNotNone(ln)
        self.assertEqual(len(ln.offsets), 4)
        self.assertAlmostEqual(abs(float(ln.u[1])), 1.0, places=6)          # along y
        self.assertAlmostEqual(ln.a1 - ln.a0, 18.0, delta=0.5)

    def test_real_sheetz_ladder(self):
        # the six lanes as Overture has them (m from the pin), with the station's other service roads
        rows = [lane(-14, -12, 6, -11), lane(-15, 2, 6, 3), lane(-13, -20, 6, -19), lane(-16, 17, 5, 18),
                lane(-14, -5, 6, -4), lane(-16, 9, 5, 10), lane(-46, -45, -49, -7), lane(47, -32, 5, 7),
                lane(-21, 58, -19, 41), lane(-23, -24, -13, -24)]
        shop = rect("shop", 28.0, -3.0, 30.0, 20.4)
        ln = gs.find_lanes(shop, rows)
        self.assertEqual(len(ln.offsets), 6)

    def test_two_lanes_not_enough(self):
        self.assertIsNone(gs.find_lanes(SHOP, LADDER[:2]))

    def test_parking_aisles_not_lanes(self):
        aisles = [lane(-40.0, y, 40.0, y) for y in (60.0, 78.0, 96.0)]       # long, 18 m apart
        self.assertIsNone(gs.find_lanes(SHOP, aisles))

    def test_lanes_far_from_the_shop_ignored(self):
        far = [lane(x, 200.0, x, 218.0) for x in (-14.0, -7.0, 0.0, 7.0)]
        self.assertIsNone(gs.find_lanes(SHOP, far))


class LaneLayoutTest(unittest.TestCase):
    def test_lane_site(self):
        site = gs.layout(STATION, [SHOP], ROAD, LADDER)
        self.assertEqual(site.drop_ids, ())
        self.assertEqual(len([p for p in site.props if p["shape"].endswith("s_fuel_structure_06.dae")]), 1)
        self.assertEqual(len([p for p in site.props if p["shape"].endswith("s_fuel_structure_07.dae")]), 6)
        self.assertEqual(len(site.areas), 6)                    # 3 islands of 16 m, 2 pumps each
        for a in site.areas:
            self.assertTrue(12.0 < a["y"] < 30.0 and -14.0 < a["x"] < 7.0, a)
            self.assertNotIn(round(a["x"], 3), (-14.0, -7.0, 0.0, 7.0))   # on the islands, not in the lanes
        roof = next(p for p in site.props if p["shape"].endswith("s_fuel_structure_06.dae"))
        self.assertGreater(roof["dz"], 4.0)

    def test_lane_pumps_face_the_lanes(self):
        # a pump model's x runs along its island, its screens face the cars either side (West Coast USA's fs_4: pumps
        # at yaw 0 in rows along x, areas 0.6 along x, 2.2 across); the user, 2026-10-23, at a Sheetz near Pittsburgh:
        # "pumps are facing the wrong way"
        site = gs.layout(STATION, [SHOP], ROAD, LADDER)                 # lanes along y
        for p in site.props:
            if "s_fuel_pump_" in p["shape"] or p["shape"].endswith("s_fuel_structure_07.dae"):
                self.assertAlmostEqual(p["yaw"] % 180.0, 90.0, places=3, msg=p)
        for a in site.areas:
            self.assertAlmostEqual(a["yaw"] % 180.0, 90.0, places=3, msg=a)
            self.assertLess(a["half"][0], a["half"][1])                 # short along the island, long across it

    def test_pair_pumps_face_the_lanes(self):
        site = gs.layout(STATION, [SHOP], ROAD)                         # the row parallel to the road: along x
        for p in site.props:
            if "s_fuel_pump_" in p["shape"]:
                self.assertAlmostEqual(p["yaw"] % 180.0, 0.0, places=3, msg=p)

    def test_lanes_and_outline_drop_the_outline(self):
        roof = rect("roof", -3.5, 21.0, 26.0, 22.0, cls="roof")
        site = gs.layout(STATION, [SHOP, roof], ROAD, LADDER)
        self.assertEqual(site.drop_ids, ("roof",))
        self.assertEqual(len(site.areas), 6)

    def test_lanes_are_evidence_for_a_convenience_brand(self):
        st = gs.Station("wawa", 0.0, 45.0, "Wawa", "Wawa", ("p",), False)
        self.assertIsNone(gs.layout(st, [SHOP], ROAD))
        self.assertIsNotNone(gs.layout(st, [SHOP], ROAD, LADDER))


# --- the road tile: map data in, station records and the canopies to leave out ------------------------------------
import json  # noqa: E402
import tempfile  # noqa: E402

import geodesy  # noqa: E402
import mapgeom as mg  # noqa: E402
import roads  # noqa: E402

FRAME = geodesy.EnuFrame(40.44, -80.00)


def _ll(xy):
    xy = np.asarray(xy, np.float64)
    la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    return np.stack([lo, la], 1)


def feats(layer, rows, kind):
    """mapgeom.Features of rows [(id, xy (k, 2) or (x, y), {attr: value})], sorted by id."""
    rows = sorted(rows, key=lambda r: r[0])
    geoms = []
    for _, xy, _ in rows:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        geoms.append((kind, [[_ll(xy)]]))
    f = mg.build(layer, np.array([r[0] for r in rows]), geoms)
    keys = sorted({k for r in rows for k in r[2]})
    for k in keys:
        vals = [r[2].get(k) for r in rows]
        if any(isinstance(v, str) for v in vals):
            f.attrs[k] = np.array([v if v is not None else "" for v in vals], object)
        else:
            f.attrs[k] = np.array([np.nan if v is None else v for v in vals], np.float64)
    return f


def box_ring(cx, cy, w, h):
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2),
            (cx - w / 2, cy - h / 2)]


class FakeCell:
    def __init__(self, layers):
        self.layers = layers


class FakeMD:
    """Every map cell holds the same layers (the code takes a feature in two cells once, by id)."""

    def __init__(self, layers, fail=()):
        self.all, self.fail = layers, set(fail)

    def cell(self, key, layers=None, kind="demand"):
        for name in layers or ():
            if name in self.fail:
                raise IOError(f"no {name}")
        return FakeCell({k: self.all.get(k) for k in (layers or ())})


def station_md(fail=()):
    """A station on tile (0, 0)'s east edge: pin and shop in tile (0, 0), the canopy's centroid in tile (1, 0), the
    road beyond it (x = 320)."""
    x0, _, x1, _ = roads.tile_bbox(0, 0)
    assert (x0, x1) == (0.0, roads.TILE)
    places = feats("places", [("pla-1", (240.0, 100.0), {"basic_category": "gas_station", "name": "Speedway",
                                                         "operating_status": "open"})], mg.POINT)
    blds = feats("buildings", [("b-shop", box_ring(230.0, 100.0, 18.0, 20.0), {"class": "retail", "src_dataset": "OpenStreetMap"}),
                               ("b-roof", box_ring(275.0, 100.0, 21.0, 30.0), {"class": "roof", "src_dataset": "OpenStreetMap"})],
                 mg.POLYGON)
    rds = feats("roads", [("r-1", [(320.0, -200.0), (320.0, 400.0)], {"class": "secondary"})], mg.LINE)
    return FakeMD({"places": places, "buildings": blds, "roads": rds}, fail)


def flat(la, lo):
    return np.zeros(np.shape(la))


class TileTest(unittest.TestCase):
    def test_canopy_dropped_by_owning_tile_only(self):
        md = station_md()
        recs, skip = gs.tile_sites(FRAME, 0, 0, md, flat)
        self.assertEqual(len(recs), 1)
        self.assertIn("b-roof", skip)
        r = recs[0]
        self.assertEqual(r["name"], "Speedway")
        self.assertTrue(r["areas"] and all(len(a["pos"]) == 3 and len(a["icon"]) == 3 for a in r["areas"]))
        self.assertTrue(all(len(p["pos"]) == 3 for p in r["props"]))
        for a in r["areas"]:                                    # under the roof (x 264.5..285.5)
            self.assertTrue(262.0 < a["pos"][0] < 288.0, a)
            self.assertGreater(a["icon"][2], a["pos"][2])
        recs1, skip1 = gs.tile_sites(FRAME, 1, 0, md, flat)
        self.assertEqual(recs1, [])
        self.assertIn("b-roof", skip1)
        json.dumps(recs)                                        # plain JSON

    def test_tile_sites_walk_cuts(self):
        md = station_md()
        cuts = []
        gs.tile_sites(FRAME, 0, 0, md, flat, cuts=cuts)
        self.assertEqual(len(cuts), 1, "the made-up driveway to x = 320")
        line, half = cuts[0]
        self.assertGreater(float(line[-1][0]), 300.0)
        cuts = []
        gs.tile_sites(FRAME, 1, 0, md, flat, cuts=cuts)
        self.assertEqual(len(cuts), 1, "a neighbour tile's station's driveway cuts this tile's sidewalks too")

    def test_tile_sites_pads(self):
        # the road tiles' own pads, for the ground's painters (surface.Surface.add_fuel_pads): no other process lays
        # the stations out again for them (a Steam Deck's memory: the user, 2026-10-23)
        import polygon
        md = station_md()
        pads = []
        gs.tile_sites(FRAME, 0, 0, md, flat, pads=pads)
        self.assertEqual(len(pads), 1)
        sid, rings = pads[0]
        la, lo, _ = FRAME.enu_to_geodetic(275.0, 100.0, 0.0)          # the canopy's centre
        self.assertTrue(any(polygon.contains_points(np.vstack([np.c_[lo_, la_], np.c_[lo_, la_][:1]]),
                                                    np.array([[float(lo), float(la)]]))[0] for la_, lo_ in rings))
        pads = []
        gs.tile_sites(FRAME, 1, 0, md, flat, pads=pads)
        self.assertEqual(pads, [], "a station's pads come with its own tile only")

    def test_mapped_lanes_reach_the_record(self):
        md = station_md()
        rows = [("r-1", [(320.0, -200.0), (320.0, 400.0)], {"class": "secondary"})]
        rows += [(f"s-{k}", [(250.0, y), (270.0, y)], {"class": "service"}) for k, y in enumerate((86.0, 93.0, 100.0, 107.0))]
        md.all["roads"] = feats("roads", rows, mg.LINE)
        recs, skip = gs.tile_sites(FRAME, 0, 0, md, flat)
        self.assertEqual(len(recs), 1)
        self.assertIn("b-roof", skip)
        self.assertTrue(any(p["shape"].endswith("eca_gastation_pumps.DAE") for p in recs[0]["props"]))     # (us_east)
        self.assertEqual(len(gs.service_lines(FRAME, [md.all["roads"]], 240.0, 100.0, 100.0)), 4)

    def test_no_places_layer_no_stations(self):
        self.assertEqual(gs.tile_sites(FRAME, 0, 0, station_md(fail=("places",)), flat), ([], frozenset()))

    def test_buildings_near_and_drive_lines(self):
        md = station_md()
        blds = gs.buildings_near(FRAME, [md.all["buildings"], md.all["buildings"]], 240.0, 100.0, 80.0)
        self.assertEqual(sorted(b.id for b in blds), ["b-roof", "b-shop"])     # once each
        roof = next(b for b in blds if b.id == "b-roof")
        self.assertEqual((roof.cls, roof.src), ("roof", "OpenStreetMap"))
        self.assertAlmostEqual(float(roof.ring[:-1, 0].mean()), 275.0, delta=0.05)
        lines = gs.drive_lines(FRAME, [md.all["roads"]], 240.0, 100.0, 200.0)
        self.assertEqual(len(lines), 1)
        self.assertGreater(lines[0][1], 3.0)

    def test_one_station_per_shop(self):
        # unmerged pins of one station (a brand pin and the shop's own name) lay out the same forecourt once
        md = station_md()
        extra = feats("places", [("pla-1", (240.0, 100.0), {"basic_category": "gas_station", "name": "Speedway",
                                                           "operating_status": "open"}),
                                 ("pla-2", (232.0, 108.0), {"basic_category": "gas_station", "name": "A-Plus Mini Mart",
                                                           "operating_status": "open"})], mg.POINT)
        md.all["places"] = extra
        recs, _ = gs.tile_sites(FRAME, 0, 0, md, flat)
        self.assertEqual(len(recs), 1)

    def test_drive_lines_use_lanes(self):
        # review I5: a six-lane arterial is wider than its class's default
        rds = feats("roads", [("r-1", [(0.0, 0.0), (300.0, 0.0)], {"class": "primary"}),
                              ("r-2", [(0.0, 50.0), (300.0, 50.0)], {"class": "primary", "osm_lanes": 6.0})], mg.LINE)   # (osmtags.join)
        half = {round(float(l[0, 1])): h for l, h in gs.drive_lines(FRAME, [rds], 150.0, 25.0, 100.0)}
        self.assertGreater(half[50], half[0] + 2.0)

    def test_one_station_per_canopy(self):
        # two stations of different shops both reaching one canopy: one forecourt
        md = station_md()
        md.all["buildings"] = feats("buildings", [
            ("b-shop", box_ring(230.0, 100.0, 18.0, 20.0), {"class": "retail", "src_dataset": "OpenStreetMap"}),
            ("b-shop2", box_ring(245.0, 135.0, 18.0, 16.0), {"class": "retail", "src_dataset": "OpenStreetMap"}),
            ("b-roof", box_ring(275.0, 100.0, 21.0, 30.0), {"class": "roof", "src_dataset": "OpenStreetMap"})],
            mg.POLYGON)
        md.all["places"] = feats("places", [
            ("pla-1", (240.0, 100.0), {"basic_category": "gas_station", "name": "Speedway", "operating_status": "open"}),
            ("pla-2", (245.0, 140.0), {"basic_category": "gas_station", "name": "Valero", "operating_status": "open"})],
            mg.POINT)
        recs, _ = gs.tile_sites(FRAME, 0, 0, md, flat)
        self.assertEqual(len(recs), 1)

    def test_tile_buildings_skip(self):
        import buildings
        md = station_md()
        a = buildings.tile_buildings(FRAME, 0, 0, 512, 256, [md.all["buildings"]], flat)
        b = buildings.tile_buildings(FRAME, 0, 0, 512, 256, [md.all["buildings"]], flat, skip=frozenset({"b-roof"}))
        self.assertEqual(len(a) - len(b), 1)

    def test_write_tile_fuel(self):
        rec = {"id": "x", "props": [], "areas": []}
        chunk = {"m": "x", "w": 1, "tl": 1, "p": 1, "n": [[0, 0, 0], [1, 0, 0]]}
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = roads.write_tile(tmp, "/vfs", 1, 0, 0, [chunk], fuel=[rec])
            data = json.loads(Path(tmp, Path(path).name).read_text())
            self.assertEqual(data["fuel"], [rec])
            path, _ = roads.write_tile(tmp, "/vfs", 1, 0, 1, [chunk], fuel=[])
            self.assertNotIn("fuel", json.loads(Path(tmp, Path(path).name).read_text()))

    def test_clear_park(self):
        rec = {"props": [{"pos": [100.0, 0.0, 0.0]}], "areas": [{"pos": [50.0, 0.0, 0.0], "half": [1.6, 2.2, 2.75]}]}
        park = [[52.0, 1.0, 0.0, 0.0], [101.5, 0.0, 0.0, 0.0], [70.0, 0.0, 0.0, 0.0]]
        self.assertEqual(gs.clear_park(park, [rec]), [[70.0, 0.0, 0.0, 0.0]])
        self.assertEqual(gs.clear_park(park, []), park)

    def test_flag_default_off_recommended_on(self):
        import earthd
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        try:
            import launcher
            import start
        finally:
            sys.path.pop(0)
        import inspect
        self.assertIs(inspect.signature(earthd.Server).parameters["gas_stations"].default, False)
        self.assertIn('ap.add_argument("--gas-stations", choices=("on", "off"), default="off"',
                      Path(earthd.__file__).read_text(encoding="utf-8"))
        self.assertIn(("--gas-stations", "on"), start.RECOMMENDED)
        # the app starts the server through launcher.py, with its own copy of the list
        self.assertEqual([x for pair in start.RECOMMENDED for x in pair], launcher.RECOMMENDED)


class _Writer:
    def __init__(self):
        import json
        self.json, self.msgs = json, []

    def write(self, data):
        self.msgs.append(self.json.loads(data[4:].decode()))

    def is_closing(self):
        return False

    def get_extra_info(self, name):
        return ("127.0.0.1", 0)


class SwitchTest(unittest.TestCase):
    """The settings page's "Gas stations" switch (the user, 2026-10-23: "build that switch!"; off saves their cost on a
    small machine): the mod's gas_stations message turns the server's stations and pads off and on again."""

    def server(self, **kw):
        import os
        import tempfile
        import types
        import earthd
        import surface as sf
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sur = sf.Surface(None)
        self.addCleanup(sur.close)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, cells=64, block=512, surface=sur,
                          mapdata=types.SimpleNamespace(), **kw)
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(s.tile_pool.shutdown)
        return s, sur

    def run_msgs(self, s, msgs):
        import asyncio
        import earthd

        async def go():
            c = earthd.Connection(s, None, _Writer())
            seen = [c.roads]
            for m in msgs:
                c.dispatch(m)
                seen.append(c.roads)
            return seen
        return asyncio.run(go())

    def test_off_and_on(self):
        s, sur = self.server(gas_stations=True)
        ring = (np.array([40.0, 40.0, 40.001]), np.array([-80.0, -79.999, -80.0]))
        sur.add_fuel_pads([("a", [ring])])
        seen = self.run_msgs(s, [{"type": "gas_stations", "on": False}])
        self.assertFalse(s.gas_stations)
        self.assertFalse(sur.gas_pads)
        self.assertIsNot(seen[1], seen[0], "the road tiles sent again (canopies back, no pumps)")
        seen = self.run_msgs(s, [{"type": "gas_stations", "on": True}, {"type": "gas_stations", "on": True}])
        self.assertTrue(s.gas_stations)
        self.assertTrue(sur.gas_pads)
        self.assertEqual(sur.fuel_pads(np.array([40.0]), np.array([-80.0])), [], "the old pads dropped with the switch")
        self.assertIsNot(seen[1], seen[0])
        self.assertIs(seen[2], seen[1], "no change, nothing sent again")

    def test_new_pads_refresh_the_block_photo(self):
        # the ground under the car is built before its road tiles (and their pads) arrive: a teleport to a station
        # showed its forecourt bare (the user, 2026-10-24: "No asphalt at the station"); a new station's pad marks the
        # blocks it lies on stale, and base_refresh renders their photo again with it
        s, sur = self.server(gas_stations=True, near_visual="detail")
        a = s.new_anchor(40.0, -80.0)
        s.anchors[a.id] = a                 # (a connection's anchor: registered as prepare_anchor does)
        size = s.layout.block * 2.0
        s.hm[(a.id, 0.0, 0.0)] = {}
        s.hm[(a.id, 5 * size, 0.0)] = {}                                   # a block far away
        la, lo, _ = a.frame.enu_to_geodetic(np.array([-10.0, 10.0, 10.0, -10.0]), np.array([-5.0, -5.0, 5.0, 5.0]),
                                            np.zeros(4))
        pads = [("st", [(np.asarray(la), np.asarray(lo))])]
        s.keep_fuel_pads(pads)
        self.assertEqual(s.base_stale, {(a.id, 0.0, 0.0)})
        s.base_stale.clear()
        s.keep_fuel_pads(pads)                                                # (a re-sent tile: the same station)
        self.assertEqual(s.base_stale, set())
        s.set_gas_stations(False)                                             # the switch: every block's photo again
        self.assertEqual(s.base_stale, {(a.id, 0.0, 0.0), (a.id, 5 * size, 0.0)})

    def test_new_pads_rebuild_the_block_when_slow(self):
        # the photo alone left the forecourt grass and dirt underneath (the user, 2026-10-24: "looks like we painted it
        # but it is still dirt"): the surface layers come with the block, so the block the car is on is built again
        # (terrain_refresh) once its pads settle, only when the car is slow (driving on, the next blocks have them)
        import asyncio
        import earthd
        s, sur = self.server(gas_stations=True, near_visual="detail")
        a = s.new_anchor(40.0, -80.0)
        s.anchors[a.id] = a
        key = (a.id, 0.0, 0.0)
        s.hm[key] = {"ter": "old.ter"}
        s.hm_committed.append(key)
        built = []

        async def terrain(anchor, cx, cy, *args, **kw):
            built.append((cx, cy))
            s.hm[(anchor.id, cx, cy)] = info = {"ter": "new.ter"}
            return info
        s.terrain = terrain
        la, lo, _ = a.frame.enu_to_geodetic(np.array([-10.0, 10.0, 10.0, -10.0]), np.array([-5.0, -5.0, 5.0, 5.0]),
                                            np.zeros(4))
        s.keep_fuel_pads([("st", [(np.asarray(la), np.asarray(lo))])])
        self.assertIn(key, s.pads_rebuild)
        t = s.pads_changed_at

        async def go():
            w = _Writer()
            c = earthd.Connection(s, None, w)
            c.terrain_msg = lambda k, info: {"cx": k[1], "cy": k[2], "size": 512}
            c.vel = (20.0, 0.0)
            c.pads_refresh(a.id, now=t + 10.0)
            await asyncio.sleep(0.05)
            fast = list(built)
            c.vel = (0.0, 0.0)
            c.pads_refresh(a.id, now=t + 1.0)                     # (more road tiles may still bring pads)
            await asyncio.sleep(0.05)
            early = list(built)
            c.pads_refresh(a.id, now=t + 10.0)
            await asyncio.sleep(0.05)
            c.pads_refresh(a.id, now=t + 20.0)                    # (once)
            await asyncio.sleep(0.05)
            return fast, early, list(built), w.msgs
        fast, early, done, msgs = asyncio.run(go())
        self.assertEqual((fast, early), ([], []))
        self.assertEqual(done, [(0.0, 0.0)])
        self.assertEqual([m["type"] for m in msgs if m["type"] == "terrain_refresh"], ["terrain_refresh"])
        self.assertNotIn(key, s.pads_rebuild)

    def test_never_on_without_the_flag(self):
        s, sur = self.server()
        self.run_msgs(s, [{"type": "gas_stations", "on": True}])
        self.assertFalse(s.gas_stations)
        self.assertFalse(sur.gas_pads)


REAL = __import__("os").environ.get("BNGE_GAS_REAL") == "1"


@unittest.skipUnless(REAL, "set BNGE_GAS_REAL=1 to lay out three Pittsburgh-area stations from the real Overture data")
class RealSpotsTest(unittest.TestCase):
    """Real stations around Pittsburgh, one per kind of canopy data (picked 2026-10-23 from the stations laid out in
    and around the city)."""

    @classmethod
    def setUpClass(cls):
        import mapdata
        import netfetch
        import overture
        cache = mapdata.DEFAULT_CACHE
        cls.md = mapdata.MapData(cache, netfetch.Fetcher(stats_dir=__import__("os").path.join(cache, "netstats")),
                                 overture.RELEASE)

    def site(self, lat, lon):
        frame = geodesy.EnuFrame(lat, lon)
        la, lo, _ = frame.enu_to_geodetic(-128.0, -128.0, 0.0)     # the pin in the middle of tile (0, 0)
        frame = geodesy.EnuFrame(float(la), float(lo))
        recs, skip = gs.tile_sites(frame, 0, 0, self.md, flat)
        self.assertEqual(len(recs), 1, recs)
        return recs[0], skip

    def test_tagged_canopy(self):
        # a BP in Pittsburgh: its canopy a mapped building=roof, East Coast's units over it (us_east)
        rec, skip = self.site(40.403093, -80.041343)
        self.assertEqual(len(skip), 1, "the OSM roof left out")
        self.assertTrue(any("eca_gastation_pumps" in p["shape"] for p in rec["props"]))
        self.assertGreaterEqual(len(rec["areas"]), 4)

    def test_imagery_canopy(self):
        # a Sunoco in Butler: no mapped canopy; the untagged imagery outline between the shop and the road is the canopy
        rec, skip = self.site(40.861281, -79.900648)
        self.assertEqual(len(skip), 1, "the imagery outline left out")
        self.assertTrue(any("eca_gastation_pumps" in p["shape"] for p in rec["props"]))

    def test_pin_on_road(self):
        # a Sunoco in Pittsburgh: its pin 2.4 m from the road; its two mapped canopies (two islands: West Coast's)
        rec, skip = self.site(40.465627, -79.955447)
        self.assertEqual(len(skip), 2, "both mapped canopies left out")
        self.assertTrue(any("s_fuel_structure_06" in p["shape"] for p in rec["props"]))

    def test_home_shop(self):
        # United Petroleum in Nagambie, Victoria (a player's report): its shop mapped as a residential building, its
        # canopy a mapped roof beside it (_home_shop); the Astron pin 40 m away is the same station (one per shop)
        rec, skip = self.site(-36.79432, 145.15094)
        self.assertEqual(len(skip), 1, "the OSM roof left out")
        self.assertTrue(any("s_fuel_structure_06" in p["shape"] for p in rec["props"]))


if __name__ == "__main__":
    unittest.main()
