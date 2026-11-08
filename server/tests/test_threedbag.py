"""3DBAG LoD 2.2: a canned feature sets the roof; a footprint with no match keeps today's roof."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import buildings as bld  # noqa: E402
import threedbag  # noqa: E402


def _feature():
    return {"type": "CityJSONFeature", "vertices": [[121000, 487000, 1], [121010, 487000, 1], [121010, 487008, 1],
                                                    [121000, 487008, 1]],
            "CityObjects": {"NL.IMBAG.Pand.0363100012170876": {
                "type": "Building",
                "attributes": {"identificatie": "NL.IMBAG.Pand.0363100012170876", "b3_dak_type": "slanted",
                               "b3_h_maaiveld": 1.0, "b3_h_dak_min": 12.0, "b3_h_dak_max": 18.0, "b3_h_nok": 18.5,
                               "b3_opp_grond": 80.0, "oorspronkelijkbouwjaar": 1825, "gebruiksdoel": "woonfunctie"},
                "geometry": [{"type": "MultiSurface", "lod": "0", "boundaries": [[[0, 1, 2, 3]]]}]}}}


class RecordTest(unittest.TestCase):
    def test_a_canned_feature_sets_the_roof_height(self):
        rec = threedbag.from_cityjson(_feature())
        self.assertAlmostEqual(rec["height"], 17.5)
        self.assertAlmostEqual(rec["roof_height"], 6.0)
        self.assertEqual(rec["roof_shape"], "gabled")
        self.assertEqual(rec["year"], 1825)
        self.assertEqual(rec["use"], "woonfunctie")

    def test_no_match_keeps_todays_roof(self):
        self.assertIsNone(threedbag.match_records([], "no-such-bag", 52.37, 4.89, 80))
        ring = np.array([[0, 0], [10, 0], [10, 8], [0, 8]], float)
        plain = bld.plan(ring, {"class": "house", "height": 9.0, "roof_shape": "gabled", "roof_height": 2.0},
                         lat=52.37, lon=4.89)
        self.assertEqual(plain.roof, "gabled")
        self.assertAlmostEqual(plain.pitch or 0.0, bld.region_style(52.37, 4.89).pitch)

    def test_credit(self):
        self.assertEqual(threedbag.CREDIT, "© 3DBAG by tudelft3d and 3DGI")

    def test_present_heights_replace_the_guess(self):
        ring = np.array([[0, 0], [10, 0], [10, 8], [0, 8]], float)
        rec = threedbag.from_cityjson(_feature())
        spec = bld.plan(ring, {"class": "house", "b3": rec}, lat=52.37, lon=4.89)
        self.assertEqual(spec.roof, "gabled")
        self.assertGreater(spec.pitch, 30.0)
        self.assertNotAlmostEqual(spec.pitch, bld.region_style(52.37, 4.89).pitch, places=3)
