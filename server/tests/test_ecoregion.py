"""ecoregion.py: RESOLVE's Ecoregions 2017 at a point, and the vegetation rules' ECO_* lists by name."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ecoregion as er  # noqa: E402
import vegetation as veg  # noqa: E402


class EcoregionTest(unittest.TestCase):
    def name_at(self, lat, lon):
        return er.name(int(er.eco_many(np.array([lat]), np.array([lon]))[0]))

    def test_places(self):
        self.assertEqual(self.name_at(52.23, 21.0), "Central European mixed forests")
        self.assertEqual(self.name_at(66.5, 25.7), "Scandinavian and Russian taiga")
        self.assertEqual(self.name_at(-15.8, -47.9), "Cerrado")
        self.assertEqual(self.name_at(35.0, -116.0), "Mojave desert")
        self.assertEqual(self.name_at(0.0, -30.0), "", "the open sea")

    def test_coast_takes_the_nearest_land(self):
        self.assertNotEqual(self.name_at(43.296, 5.36), "", "Marseille's quay")

    def test_biomes(self):
        e = er.eco_many(np.array([52.23, 41.9, -3.1]), np.array([21.0, 12.5, -60.0]))
        self.assertEqual(er.biome_of(e).tolist(), [er.B_TEMP_BROAD, er.B_MEDITERRANEAN, er.B_TROP_MOIST])

    def test_rule_lists_by_name(self):
        """The ECO_* lists hold the ecoregions their comments name (ECO_IDs are RESOLVE's; a rebuilt raster from
        another release would renumber them)."""
        self.assertEqual(set(veg.ECO_CREOSOTE), set(er.ids(
            "Baja California desert", "Chihuahuan desert", "Gulf of California xeric scrub", "Mojave desert",
            "Sonoran desert")))
        self.assertEqual(set(veg.ECO_BROAD_SHARES), set(er.ids("Cantabrian mixed forests",
                                                               "Northwest Iberian montane forests")))
        self.assertIn(er.ids("Iberian conifer forests")[0], veg.ECO_JUNIPER)
        self.assertIn(er.ids("Mediterranean High Atlas juniper steppe")[0], veg.ECO_JUNIPER)
        for e in veg.ECO_MED:
            self.assertEqual(er._meta[e][1], er.B_MEDITERRANEAN, er.name(e))
            self.assertEqual(er._meta[e][2], "Palearctic", er.name(e))


if __name__ == "__main__":
    unittest.main()
