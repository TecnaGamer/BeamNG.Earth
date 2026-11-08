"""CBS fleet mix: Amsterdam is not Leeuwarden, and an unknown point keeps today's mix."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fleetmix  # noqa: E402


class FleetTest(unittest.TestCase):
    def test_amsterdam_is_not_the_friesland_gemeente(self):
        city = fleetmix.shares(52.373, 4.892)
        rural = fleetmix.shares(53.200, 5.800)
        self.assertIsNotNone(city)
        self.assertIsNotNone(rural)
        self.assertGreater(city["car"], rural["car"])
        self.assertNotEqual(round(city["van"], 4), round(rural["van"], 4))

    def test_an_unknown_point_keeps_todays_mix(self):
        self.assertIsNone(fleetmix.shares(50.938, 6.960))
        self.assertIsNone(fleetmix.shares(51.922, 4.479))
        self.assertIsNone(fleetmix.shares(None, None))
