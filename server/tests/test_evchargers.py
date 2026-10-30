"""EV chargers (the user, 2026-10-29: "real life ev stations mapped like the gas stations are but using beam assets to
have real recharging in the game"): charger sites from Overture's places and OpenStreetMap's charging stations, their
layout, and their records in the road tiles."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import mapgeom as mg  # noqa: E402

import evchargers as ev  # noqa: E402
import gasstations as gs  # noqa: E402
import roads  # noqa: E402
from test_gasstations import FRAME, FakeMD, feats, flat, station_md  # noqa: E402


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


def bld(bid, cx, cy, w, h):
    ring = np.array([(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2),
                     (cx - w / 2, cy + h / 2), (cx - w / 2, cy - h / 2)], np.float64)
    return gs.Bld(bid, ring, "retail", "", "OpenStreetMap", None)


def charger(big=False, tesla=False, records=1, x=0.0, y=0.0):
    return ev.Charger("evtest", x, y, "Test", "", records, big, tesla)


ROAD = (np.array([(-300.0, 60.0), (300.0, 60.0)]), 4.0)        # a public road 60 m north, 8 m wide
AISLE = np.array([(-100.0, 15.0), (100.0, 15.0)])               # a parking aisle 15 m north of the pin


class LayoutTest(unittest.TestCase):
    def _clear(self, site, blds, roads):
        pts = [(p["x"], p["y"]) for p in site.props] + [(a["x"], a["y"]) for a in site.areas]
        return gs._clear(np.array(pts, np.float64), blds, roads)

    def test_big_row_along_aisle(self):
        site = ev.layout(charger(big=True, records=2), [], [ROAD], [AISLE], "default")
        self.assertEqual((len(site.props), len(site.areas)), (4, 4))
        ys = {round(p["y"], 3) for p in site.props}
        self.assertEqual(len(ys), 1, "a straight row parallel to the aisle")
        self.assertTrue(all(abs(a["y"] - 15.0) < abs(p["y"] - 15.0) for a, p in zip(site.areas, site.props)),
                        "each stall between its charger and the aisle")
        self.assertTrue(all(p["shape"] == ev.CHARGER_SHAPES["default"][0] for p in site.props), "the big model")
        import polygon
        pad = site.pad[0]
        pts = np.array([(p["x"], p["y"]) for p in site.props] + [(a["x"], a["y"]) for a in site.areas])
        self.assertTrue(polygon.contains_points(pad, pts).all(), "the pad covers chargers and stalls")
        self.assertTrue(self._clear(site, [], [ROAD]))

    def test_chargers_face_their_stalls(self):
        site = ev.layout(charger(big=True, records=2), [], [ROAD], [AISLE], "default")
        p, a = site.props[0], site.areas[0]
        f = np.array([a["x"] - p["x"], a["y"] - p["y"]])
        self.assertAlmostEqual(p["yaw"] % 360.0, (gs._yaw(f) + ev.CHARGER_DYAW) % 360.0, places=3)
        self.assertAlmostEqual(a["yaw"] % 360.0, gs._yaw(f) % 360.0, places=3, msg="the area's depth along the stall")

    def test_tesla_row_of_eight(self):
        site = ev.layout(charger(big=True, tesla=True), [], [ROAD], [AISLE], "default")
        self.assertEqual(len(site.props), 8)

    def test_count_of(self):
        self.assertEqual(ev.count_of(charger()), 2)
        self.assertEqual(ev.count_of(charger(big=True, records=2)), 4)
        self.assertEqual(ev.count_of(charger(big=True, records=3)), 6)
        self.assertEqual(ev.count_of(charger(big=True, records=9)), 8)
        self.assertEqual(ev.count_of(charger(big=True, tesla=True)), 8)

    def test_small_pair_at_pin(self):
        site = ev.layout(charger(), [], [ROAD], [], "default")
        self.assertEqual(len(site.props), 2)
        self.assertTrue(all(p["shape"] == ev.CHARGER_SHAPES["default"][1] for p in site.props), "the small model")
        self.assertTrue(all(np.hypot(p["x"], p["y"]) < 10.0 for p in site.props), "at the pin")
        self.assertTrue(self._clear(site, [], [ROAD]))

    def test_shortened_when_tight(self):
        # walls 3 x 3 m apart east and west of the aisle's stall row leave room for 3 chargers
        blds = [bld("w", -6.6, 7.0, 4.0, 14.0), bld("e", 6.6, 7.0, 4.0, 14.0)]
        site = ev.layout(charger(big=True, records=2), blds, [ROAD], [np.array([(-100.0, 15.0), (100.0, 15.0)])],
                         "default")
        self.assertIsNotNone(site)
        self.assertLess(len(site.props), 4)
        self.assertTrue(self._clear(site, blds, [ROAD]))

    def test_charger_in_garage_skipped(self):
        garage = bld("garage", 0.0, 0.0, 120.0, 120.0)
        self.assertIsNone(ev.layout(charger(), [garage], [ROAD], [], "default"))

    def test_never_on_a_road(self):
        road = (np.array([(-300.0, 0.0), (300.0, 0.0)]), 6.0)    # the pin on a road's centreline
        site = ev.layout(charger(), [], [road], [], "default")
        if site is not None:
            self.assertTrue(self._clear(site, [], [road]))

    def test_style_shapes_have_chargers(self):
        for style in ("default", "us_east", "europe"):
            self.assertTrue(set(ev.CHARGER_SHAPES[style]) <= set(gs.style_shapes(style)), style)

    def test_reach(self):
        self.assertGreaterEqual(ev.reach_of(charger()), ev.AISLE_REACH_M + ev.STALL_M)


def ev_md(rows, osm=None, buildings=None):
    """A FakeMD with these Overture places, a public road at y = -100 and OSM cells (osm_fuel_cached) of osm rows
    (None: not fetched)."""
    rds = feats("roads", [("r-1", [(-400.0, -100.0), (800.0, -100.0)], {"class": "secondary"})], mg.LINE)
    md = FakeMD({"places": places(rows) if rows else None, "roads": rds, "buildings": buildings})
    md.osm_fuel_cached = lambda key: osm
    return md


class TileTest(unittest.TestCase):
    def test_tile_has_chargers(self):
        pads = []
        md = ev_md([("pla-ev", (100.0, 100.0), {"basic_category": "ev_charging_station", "name": "ChargePoint"})], [])
        recs, _ = gs.tile_sites(FRAME, 0, 0, md, flat, pads=pads)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["energy"], ["electricEnergy"])
        self.assertEqual(recs[0]["name"], "ChargePoint")
        self.assertEqual(len(recs[0]["areas"]), len(recs[0]["props"]))
        self.assertEqual([p[0] for p in pads], [recs[0]["id"]], "its pad for the ground's painters")

    def test_osm_only_charger(self):
        md = ev_md([], [osm_row(5, 100.0, 100.0)])
        recs, _ = gs.tile_sites(FRAME, 0, 0, md, flat)
        self.assertEqual([r["name"] for r in recs], ["EV charger"])

    def test_charger_owned_by_one_tile(self):
        x = roads.TILE - 2.0
        md = ev_md([("pla-ev", (x, 100.0), {"basic_category": "ev_charging_station"})], [])
        a = gs.tile_sites(FRAME, 0, 0, md, flat)[0]
        b = gs.tile_sites(FRAME, 1, 0, md, flat)[0]
        self.assertEqual((len(a), len(b)), (1, 0), "a record in the tile that owns its position only")

    def test_charger_beside_station(self):
        import polygon
        md = station_md()
        md.all["places"] = feats("places", [
            ("pla-1", (240.0, 100.0), {"basic_category": "gas_station", "name": "Speedway", "operating_status": "open"}),
            ("pla-ev", (250.0, 130.0), {"basic_category": "ev_charging_station", "operating_status": "open"})],
            mg.POINT)
        md.osm_fuel_cached = lambda key: []
        pads = []
        recs, _ = gs.tile_sites(FRAME, 0, 0, md, flat, pads=pads)
        fuel = [r for r in recs if "energy" not in r]
        evs = [r for r in recs if r.get("energy") == ["electricEnergy"]]
        self.assertEqual((len(fuel), len(evs)), (1, 1))
        fuel_pad = dict(pads)[fuel[0]["id"]]
        ring = np.stack(roads_xy(fuel_pad[0]), 1)
        pts = np.array([p["pos"][:2] for p in evs[0]["props"]])
        self.assertFalse(polygon.contains_points(ring, pts).any(), "no charger on the forecourt")
        d = gs._seg_dist(pts, ring[:-1], ring[1:])
        self.assertTrue((d <= 12.0).all(), d)

    def test_chargers_without_data(self):
        import revisit
        f0 = revisit.faults()
        recs, _ = gs.tile_sites(FRAME, 0, 0, ev_md([], None), flat)
        self.assertEqual(recs, [])
        self.assertGreater(revisit.faults(), f0, "an unfetched OSM cell: the tile not kept")

    def test_no_fuel_station_lost(self):
        md = station_md()
        md.osm_fuel_cached = lambda key: []
        before = gs.tile_sites(FRAME, 0, 0, md, flat)[0]
        md.all["places"] = feats("places", [
            ("pla-1", (240.0, 100.0), {"basic_category": "gas_station", "name": "Speedway", "operating_status": "open"}),
            ("pla-ev", (100.0, 900.0), {"basic_category": "ev_charging_station", "operating_status": "open"})],
            mg.POINT)
        after = gs.tile_sites(FRAME, 0, 0, md, flat)[0]
        self.assertEqual(before, after)
        self.assertEqual([r["name"] for r in after], ["Speedway"])


def roads_xy(ll):
    """(x, y) in FRAME of a pad's (lat, lon) arrays."""
    la, lo = ll
    e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
    return np.asarray(e, np.float64), np.asarray(n, np.float64)


if __name__ == "__main__":
    unittest.main()
