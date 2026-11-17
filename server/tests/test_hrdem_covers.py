"""hrdem.covers: Canada's lidar mosaic is asked in Canada and up to ~17 km past its border, not in the US south of it
(a tester's drive in Massachusetts asked it for every lidar tile, 2026-11-17)."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hrdem  # noqa: E402


def tile(lat, lon, z=14):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return z, x, y


CANADA = {"Victoria": (48.42, -123.37), "Middle Island": (41.685, -82.68), "Windsor": (42.30, -83.03),
          "Fort Erie": (42.90, -78.93), "Wolfe Island": (44.17, -76.35), "Cornwall": (45.02, -74.73),
          "Campobello": (44.88, -66.93), "Sault Ste. Marie": (46.52, -84.35), "Osoyoos": (49.03, -119.47),
          "Fort Frances": (48.61, -93.40), "Stanstead": (45.01, -72.10), "Pohenegamook": (47.46, -69.23),
          "St. Stephen": (45.19, -67.28), "Haines Junction": (60.75, -137.51), "Stewart": (55.94, -129.99),
          "Toronto": (43.65, -79.38), "Halifax": (44.65, -63.57), "St. John's": (47.56, -52.71)}
US = {"Mohawk Trail": (42.64, -72.99), "Boston": (42.36, -71.06), "Seattle": (47.6, -122.3),
      "Burlington": (44.48, -73.21), "Minneapolis": (44.98, -93.27), "Bangor": (44.8, -68.77),
      "Duluth": (46.79, -92.1), "Spokane": (47.66, -117.43), "Juneau": (58.30, -134.42)}


class CoversTest(unittest.TestCase):
    def test_canada_asked(self):
        for name, (lat, lon) in CANADA.items():
            for z in (13, 16):
                self.assertTrue(hrdem.covers(*tile(lat, lon, z)), (name, z))

    def test_us_south_of_the_border_not_asked(self):
        for name, (lat, lon) in US.items():
            self.assertFalse(hrdem.covers(*tile(lat, lon)), name)

    def test_coarse_rule_keeps_the_rectangle(self):
        """dem.coarse_at reads in_bounds, the rectangle as before: the US south of the border still counts as where the
        lidar reads for the road plans' coarse rules (no plan changes), though it is no longer asked."""
        self.assertTrue(hrdem.in_bounds(*tile(42.64, -72.99)))
        self.assertFalse(hrdem.covers(*tile(42.64, -72.99)))

    def test_outside_bounds(self):
        self.assertFalse(hrdem.covers(*tile(40.71, -74.0)))         # (south of BOUNDS)
        self.assertFalse(hrdem.covers(*tile(64.84, -147.72)))       # (Fairbanks: west of BOUNDS)


if __name__ == "__main__":
    unittest.main()
