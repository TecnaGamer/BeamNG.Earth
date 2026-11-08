"""NDW flow: a high count raises the weight, and no detector leaves it alone."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ndwflow  # noqa: E402

SITE = '<measurementSiteRecord id="A10"><latitude>52.340</latitude><longitude>4.830</longitude>'
QUIET = '<measurementSiteRecord id="side"><latitude>52.374</latitude><longitude>4.882</longitude>'
FLOW = '<roa:siteMeasurements><roa:measurementSiteReference id="A10"/><com:vehicleFlowRate>2200</com:vehicleFlowRate><com:vehicleFlowRate>400</com:vehicleFlowRate>'


class FlowTest(unittest.TestCase):
    def test_a_high_count_raises_the_weight(self):
        self.assertGreater(ndwflow.weight_of(2200), 1.0)
        self.assertGreater(ndwflow.weight_of(2200), ndwflow.weight_of(200))
        sites = ndwflow.join([ndwflow.sites_from_record(SITE), ndwflow.sites_from_record(QUIET)],
                             [ndwflow.flow_from_record(FLOW)])
        self.assertEqual(len(sites), 1)

        class Frame:
            lat0, lon0 = 52.34, 4.83

            def enu_to_geodetic(self, x, y, z):
                return 52.340, 4.830, 0.0

        path = {"id": "ring", "points": [[0, 0, 0], [100, 0, 0]], "speed": 25}
        ndwflow.apply([path], Frame(), sites)
        self.assertLess(path["spacing"], 180.0)

    def test_no_detector_leaves_the_weight_alone(self):
        class Frame:
            lat0, lon0 = 52.37, 4.88

            def enu_to_geodetic(self, x, y, z):
                return 52.374, 4.882, 0.0

        path = {"id": "street", "points": [[0, 0, 0], [40, 0, 0]], "speed": 10}
        ndwflow.apply([path], Frame(), sites=[])
        self.assertNotIn("spacing", path)
        self.assertEqual(ndwflow.weight_of(200), 1.0)
