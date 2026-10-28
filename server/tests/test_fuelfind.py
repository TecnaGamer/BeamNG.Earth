"""fuelfind: the nearest real gas station to a point, and a spot on the public road beside it (the world map's "Nearest
gas station" button)."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fuelfind  # noqa: E402
import mapgeom as mg  # noqa: E402
from test_gasstations import FRAME, FakeMD, feats  # noqa: E402

LAT0, LON0 = 40.44, -80.00      # FRAME's origin: the player


def enu(lat, lon):
    e, n, _ = FRAME.geodetic_to_enu(lat, lon, 0.0)
    return float(e), float(n)


def md_with(places, roads):
    return FakeMD({"places": feats("places", places, mg.POINT), "roads": feats("roads", roads, mg.LINE)})


def gas(pid, xy, name="Speedway", **kw):
    a = {"basic_category": "gas_station", "name": name, "operating_status": "open"}
    a.update(kw)
    return (pid, xy, a)


def road_x(rid, x, cls="secondary"):
    return (rid, [(x, -20000.0), (x, 20000.0)], {"class": cls})


class NearestTest(unittest.TestCase):
    def test_spot_on_the_road_beside_the_station(self):
        md = md_with([gas("p1", (240.0, 100.0))], [road_x("r1", 320.0)])
        got = fuelfind.nearest(md, LAT0, LON0)
        self.assertIsNotNone(got)
        self.assertEqual(got["name"], "Speedway")
        self.assertAlmostEqual(got["dist"], math.hypot(240.0, 100.0), delta=1.0)
        x, y = enu(got["lat"], got["lon"])
        self.assertAlmostEqual(x, 320.0, delta=1.0)      # on the road, level with the station
        self.assertAlmostEqual(y, 100.0, delta=2.0)

    def test_the_nearest_of_two(self):
        md = md_with([gas("far", (900.0, 0.0), name="Far Gas"), gas("near", (-300.0, 50.0), name="Near Gas")],
                     [road_x("r1", 905.0), road_x("r2", -310.0)])
        self.assertEqual(fuelfind.nearest(md, LAT0, LON0)["name"], "Near Gas")

    def test_a_fuel_brand_counts_without_the_category(self):
        shell = ("p1", (200.0, 0.0), {"basic_category": "convenience_store", "brand_wikidata": "Q110716465",
                                      "brand_name": "Shell", "name": "Shell", "operating_status": "open"})
        md = md_with([shell], [road_x("r1", 210.0)])
        got = fuelfind.nearest(md, LAT0, LON0)
        self.assertIsNotNone(got, "Shell (a fuel brand) is a station")

    def test_a_convenience_brand_alone_is_no_station(self):
        wawa = ("p1", (100.0, 0.0), {"basic_category": "convenience_store", "brand_name": "Wawa", "name": "Wawa",
                                     "operating_status": "open"})
        md = md_with([wawa, gas("p2", (800.0, 0.0), name="Real Gas")], [road_x("r1", 110.0), road_x("r2", 810.0)])
        self.assertEqual(fuelfind.nearest(md, LAT0, LON0)["name"], "Real Gas")

    def test_closed_station_skipped(self):
        md = md_with([gas("p1", (100.0, 0.0), name="Closed Gas", operating_status="permanently_closed"),
                      gas("p2", (700.0, 0.0), name="Open Gas")], [road_x("r1", 110.0), road_x("r2", 710.0)])
        self.assertEqual(fuelfind.nearest(md, LAT0, LON0)["name"], "Open Gas")

    def test_station_without_a_public_road_skipped(self):
        md = md_with([gas("p1", (100.0, 0.0), name="Service Area"), gas("p2", (1200.0, 0.0), name="Town Gas")],
                     [road_x("r1", 100.0 + fuelfind.SPOT_M + 400.0, cls="motorway"), road_x("r2", 1210.0)])
        self.assertEqual(fuelfind.nearest(md, LAT0, LON0)["name"], "Town Gas")

    def test_a_corner_station_is_not_taken_over_a_nearer_one_further_out(self):
        """The first square's corner (2687 m away) is farther than a station just past its side (2500 m): the search
        looks one ring further before taking the corner one."""
        md = md_with([gas("corner", (1900.0, 1900.0), name="Corner Gas"), gas("side", (2500.0, 0.0), name="Side Gas")],
                     [road_x("r1", 1910.0), road_x("r2", 2510.0)])
        self.assertEqual(fuelfind.nearest(md, LAT0, LON0)["name"], "Side Gas")

    def test_none_near(self):
        self.assertIsNone(fuelfind.nearest(md_with([gas("p1", (40000.0, 0.0))], [road_x("r1", 40010.0)]), LAT0, LON0))

    def test_failed_cells_do_not_stop_the_search(self):
        md = FakeMD({"places": feats("places", [gas("p1", (300.0, 0.0))], mg.POINT),
                     "roads": feats("roads", [road_x("r1", 310.0)], mg.LINE)})
        calls = {"n": 0}
        real = md.cell

        def flaky(key, layers=None, kind="demand"):
            calls["n"] += 1
            if calls["n"] == 1 and layers == ("places",):
                raise IOError("one cell down")
            return real(key, layers, kind)
        md.cell = flaky
        self.assertIsNotNone(fuelfind.nearest(md, LAT0, LON0))


class HandlerTest(unittest.TestCase):
    """earthd's nearest_fuel message: the answer the mod's settings.uiNearestFuelResult reads."""

    def run_handler(self, msg, md):
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        import earthd
        sent = []
        conn = object.__new__(earthd.Connection)
        conn.s = type("S", (), {"mapdata": md, "ui_pool": ThreadPoolExecutor(1)})()
        conn.send = sent.append
        asyncio.run(conn.nearest_fuel(msg))
        conn.s.ui_pool.shutdown()
        self.assertEqual(len(sent), 1)
        return sent[0]

    def test_found(self):
        md = md_with([gas("p1", (240.0, 100.0))], [road_x("r1", 320.0)])
        out = self.run_handler({"id": 7, "lat": LAT0, "lon": LON0}, md)
        self.assertEqual((out["type"], out["id"], out["name"]), ("nearest_fuel_result", 7, "Speedway"))
        self.assertTrue(isinstance(out["lat"], float) and isinstance(out["lon"], float) and out["dist"] > 0)
        self.assertNotIn("error", out)

    def test_none_near_says_so(self):
        out = self.run_handler({"id": 8, "lat": LAT0, "lon": LON0}, md_with([], [road_x("r1", 10.0)]))
        self.assertEqual(out["id"], 8)
        self.assertIn("no gas station", out["error"])
        self.assertNotIn("lat", out)

    def test_bad_position_and_no_map_data(self):
        self.assertEqual(self.run_handler({"id": 9}, None)["error"], "no position")
        self.assertEqual(self.run_handler({"id": 10, "lat": LAT0, "lon": LON0}, None)["error"], "no map data")


if __name__ == "__main__":
    unittest.main()
