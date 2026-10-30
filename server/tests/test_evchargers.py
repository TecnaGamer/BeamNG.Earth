"""EV chargers (the user, 2026-10-29: "real life ev stations mapped like the gas stations are but using beam assets to
have real recharging in the game"): charger sites from Overture's places and OpenStreetMap's charging stations, their
layout, and their records in the road tiles."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mapgeom as mg  # noqa: E402

import evchargers as ev  # noqa: E402
import gasstations as gs  # noqa: E402
from test_gasstations import FRAME, feats  # noqa: E402


def places(rows):
    """Overture places [(id, (x, y), {attrs})], open unless said."""
    return feats("places", [(i, xy, {"operating_status": "open", **a}) for i, xy, a in rows], mg.POINT)


def osm_row(oid, x, y, name="", brand=""):
    """An osmfuel row of an OSM charging station at (x, y) in FRAME."""
    la, lo, _ = FRAME.enu_to_geodetic(x, y, 0.0)
    return {"id": oid, "amenity": "charging_station", "name": name, "brand": brand,
            "bbox": (float(lo), float(la), float(lo), float(la))}


class ChargerSitesTest(unittest.TestCase):
    def test_overture_charger_pins(self):
        L = places([("pla-ev", (100.0, 100.0), {"basic_category": "ev_charging_station", "name": "ChargePoint"}),
                    ("pla-gas", (300.0, 100.0), {"basic_category": "gas_station", "name": "Sunoco"}),
                    ("pla-shut", (500.0, 100.0), {"basic_category": "ev_charging_station", "name": "Gone",
                                                  "operating_status": "permanently_closed"})])
        pins = ev.charger_pins(FRAME, [L])
        self.assertEqual([p[0] for p in pins], ["pla-ev"])
        self.assertAlmostEqual(pins[0][1], 100.0, places=1)

    def test_ev_gas_listing_is_a_charger(self):
        # a charger Overture filed as a gas station (Shell Recharge) is a charger site, never a fuel station
        L = places([("pla-sr", (100.0, 100.0), {"basic_category": "gas_station", "name": "Shell Recharge"})])
        self.assertEqual([p[0] for p in ev.charger_pins(FRAME, [L])], ["pla-sr"])
        self.assertEqual(gs.pins(FRAME, [L]), [])

    def test_merge_across_sources(self):
        pins = [("pla-ev", 100.0, 100.0, "ChargePoint", "")]
        chs = ev.merge(pins, gs._osm_points(FRAME, [[osm_row(7, 130.0, 100.0)]], amenity="charging_station"))
        self.assertEqual(len(chs), 1)
        self.assertEqual(chs[0].records, 2)
        self.assertTrue(chs[0].big, "two records: a big site")
        self.assertEqual(chs[0].name, "ChargePoint")

    def test_network_is_big(self):
        chs = ev.merge([("pla-t", 0.0, 0.0, "Tesla Supercharger", "Tesla")], [])
        self.assertTrue(chs[0].big and chs[0].tesla)
        chs = ev.merge([], gs._osm_points(FRAME, [[osm_row(9, 0.0, 0.0)]], amenity="charging_station"))
        self.assertFalse(chs[0].big, "one unnamed charger: small")
        self.assertFalse(chs[0].tesla)

    def test_far_records_stay_apart(self):
        chs = ev.merge([("a", 0.0, 0.0, "", ""), ("b", 200.0, 0.0, "", "")], [])
        self.assertEqual(len(chs), 2)

    def test_merge_ids_stable(self):
        pins = [("pla-b", 10.0, 0.0, "", ""), ("pla-a", 0.0, 0.0, "", "")]
        osm = gs._osm_points(FRAME, [[osm_row(3, 20.0, 0.0)]], amenity="charging_station")
        a = [(c.id, c.records) for c in ev.merge(pins, osm)]
        b = [(c.id, c.records) for c in ev.merge(list(reversed(pins)), osm)]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
