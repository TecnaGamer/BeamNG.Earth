"""maplabels.py: the 3D map's labels by zoom (Natural Earth far out, towns, streets, places)."""
import sys
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fast  # noqa: E402
import maplabels  # noqa: E402

BALTIMORE = (39.2847, -76.5925)


class WorldTest(unittest.TestCase):
    def test_more_appears_as_the_view_zooms_in(self):
        far = maplabels.world_labels(*BALTIMORE, 3.0, 3e6)
        kinds = {r[0] for r in far}
        self.assertIn("continent", kinds)
        self.assertIn("country", kinds)
        names = {r[1] for r in far}
        self.assertIn("North America", names)
        mid = maplabels.world_labels(*BALTIMORE, 6.5, 6e5)
        mid_names = {r[1] for r in mid if r[0] == "state"}
        self.assertIn("Maryland", mid_names)
        self.assertNotIn("North America", {r[1] for r in mid})       # (past its max zoom)
        self.assertIn("Chesapeake Bay", {r[1] for r in mid})

    def test_rows_carry_zoom_rank_and_extra(self):
        md = [r for r in maplabels.world_labels(*BALTIMORE, 6.5, 6e5) if r[1] == "Maryland"][0]
        self.assertEqual(md[0], "state")
        self.assertEqual(md[7], "MD")                   # (the postal code, shown far out)
        self.assertLess(md[4], md[5])
        self.assertEqual(len(md), 9)

    def test_text_spaces_are_cleaned(self):
        self.assertEqual(maplabels.clean("Washington,  D.C."), "Washington, D.C.")


class TownTest(unittest.TestCase):
    def test_a_town_natural_earth_has_is_left_out(self):
        world = [["city", "Baltimore", 39.30, -76.62, 5.6, 99, 33.6, "", None]]
        towns = [("Baltimore", 39.29, -76.61, "city", 600000.0), ("Towson", 39.40, -76.60, "town", 55000.0),
                 ("Fells Point", 39.28, -76.59, "", 0.0)]
        rows = maplabels.town_labels(towns, world)
        self.assertEqual([r[1] for r in rows], ["Towson", "Fells Point"])
        self.assertEqual(rows[1][0], "district")
        self.assertGreater(rows[1][4], rows[0][4])      # (a district only close in)


class StreetTest(unittest.TestCase):
    def test_names_written_as_on_the_photo(self):
        self.assertEqual(maplabels.street_text("East Fayette Street"), "E FAYETTE ST")
        self.assertEqual(maplabels.street_text("South Broadway"), "S BROADWAY")
        self.assertEqual(maplabels.street_text("Martin Luther King Jr Boulevard"), "MARTIN LUTHER KING JR BLVD")
        self.assertEqual(maplabels.street_text("Rue de Rivoli"), "RUE DE RIVOLI")
        self.assertEqual(maplabels.street_text("North"), "NORTH")

    @unittest.skipUnless(fast.available() if hasattr(fast, "available") else fast._ef is not None, "earthfast")
    def test_points_along_named_roads_once_per_spacing(self):
        # two segments of the same street end to end, due east, and an unnamed footway
        lonlat = np.array([[-76.600, 39.28], [-76.590, 39.28], [-76.590, 39.28], [-76.580, 39.28],
                           [-76.600, 39.281], [-76.580, 39.281]], np.float64)
        f = types.SimpleNamespace(
            ids=np.array(["a", "b", "c"]), lonlat=lonlat, part_off=np.array([0, 1, 2, 3]),
            ring_off=np.array([0, 1, 2, 3]), coord_off=np.array([0, 2, 4, 6]),
            attrs={"name": np.array(["Fleet Street", "Fleet Street", ""]),
                   "class": np.array(["residential", "residential", "footway"])})
        rows = maplabels.street_labels([f], 39.28, -76.59, 16.0, 3000.0)
        self.assertTrue(rows)
        self.assertEqual({r[1] for r in rows}, {"FLEET ST"})
        self.assertTrue(all(abs(r[8] - 90.0) < 1.0 for r in rows))      # (bearing: east)
        sp = maplabels.street_spacing(16.0) * 0.6
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                self.assertGreater(abs(a[3] - b[3]) * 111320 * np.cos(np.radians(39.28)), sp - 1)


class HoodTest(unittest.TestCase):
    def test_districts_without_residents_associations(self):
        f = types.SimpleNamespace(
            ids=np.array(["1", "2", "3"]),
            attrs={"name": np.array(["Fell's Point", "McHenry Pointe Homeowners Association", "South Baltimore"]),
                   "subtype": np.array(["neighborhood", "neighborhood", "macrohood"]),
                   "cx": np.array([-76.594, -76.589, -76.614]), "cy": np.array([39.283, 39.268, 39.271])})
        rows = maplabels.hood_labels([f], *BALTIMORE)
        self.assertEqual([r[1] for r in rows], ["Fell's Point", "South Baltimore"])
        self.assertEqual([r[0] for r in rows], ["district", "district"])
        self.assertLess(rows[1][4], rows[0][4])          # (a macrohood from farther out)


class PoiTest(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(maplabels.poi_group("restaurant"), "food")
        self.assertEqual(maplabels.poi_group("pizza_restaurant"), "food")
        self.assertEqual(maplabels.poi_group("fashion_and_apparel_store"), "shop")
        self.assertEqual(maplabels.poi_group("museum"), "culture")
        self.assertEqual(maplabels.poi_group("gas_station"), "fuel")
        self.assertIsNone(maplabels.poi_group("historic_site"))       # (Overture's: mostly apartment blocks)
        self.assertIsNone(maplabels.poi_group("legal_service"))
        self.assertIsNone(maplabels.poi_group("fire_station"))
        self.assertIsNone(maplabels.poi_group(""))

    def test_sure_open_named_ones_only(self):
        n = 4
        f = types.SimpleNamespace(
            ids=np.array(["1", "2", "3", "4"]),
            attrs={"name": np.array(["Joe's", "", "Closed Cafe", "Unsure Bar"]),
                   "basic_category": np.array(["restaurant", "restaurant", "cafe", "bar"]),
                   "confidence": np.array([0.95, 0.95, 0.95, 0.3]),
                   "operating_status": np.array(["open", "open", "permanently_closed", "open"]),
                   "brand_name": np.array([""] * n),
                   "cx": np.full(n, -76.59), "cy": np.full(n, 39.28)})
        rows = maplabels.poi_labels([f], 39.28, -76.59)
        self.assertEqual([(r[1], r[7]) for r in rows], [("Joe's", "food")])


if __name__ == "__main__":
    unittest.main()
