"""Distance-driven traffic targets, road-class multipliers and speed population rules."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import traffic  # noqa: E402

AREA_KM2 = math.pi * (traffic.SAMPLE_RADIUS_M / 1000.0) ** 2


def metres_for_density(density):
    return density * AREA_KM2 * 1000.0


def profile_at(distance_km, size="metropolitan", *, density=0.0, urban_share=0.0, divided=False, limit=15):
    total = metres_for_density(density)
    return traffic.calculate(total, total * urban_share, 0.0, 0.0,
                             settlement={"name": "Test", "distanceM": distance_km * 1000,
                                         "rank": 1, "sizeClass": size, "lat": -37.8, "lon": 145.0},
                             divided=divided, max_traffic=limit)


class SettlementDistanceTest(unittest.TestCase):
    def test_distance_anchors_match_the_requested_envelopes(self):
        self.assertEqual(profile_at(400)["target"], 0.0)
        self.assertAlmostEqual(profile_at(200)["target"], 1.6, places=2)
        self.assertAlmostEqual(profile_at(50)["target"], 4.0, places=2)
        self.assertAlmostEqual(profile_at(50, "regional-town")["target"], 2.5, places=2)

    def test_interpolation_is_continuous_around_50_200_and_400_km(self):
        for boundary in (50.0, 200.0, 400.0):
            before = traffic.settlement_target(boundary - 0.01, "metropolitan")
            at = traffic.settlement_target(boundary, "metropolitan")
            after = traffic.settlement_target(boundary + 0.01, "metropolitan")
            self.assertLess(abs(before - after), 0.01)
            self.assertLessEqual(abs(before - at), 0.01)
            self.assertLessEqual(abs(after - at), 0.01)
        self.assertGreater(traffic.settlement_target(125, "metropolitan"),
                           traffic.settlement_target(200, "metropolitan"))

    def test_settlement_size_changes_the_target_at_equal_distance(self):
        city = profile_at(0, "metropolitan")["target"]
        regional = profile_at(0, "regional-town")["target"]
        small = profile_at(0, "small-settlement")["target"]
        self.assertGreater(city, regional)
        self.assertGreater(regional, small)

    def test_city_centre_and_outskirts(self):
        city = profile_at(2, "metropolitan", density=11.0, urban_share=0.35, limit=15)
        outskirts = profile_at(15, "metropolitan", density=4.5, urban_share=0.22, limit=15)
        self.assertEqual(city["band"], "city")
        self.assertEqual(city["target"], 15.0)
        self.assertEqual(outskirts["band"], "suburban")
        self.assertGreaterEqual(outskirts["target"], 4.0)
        self.assertLessEqual(outskirts["target"], 5.0)
        self.assertGreater(city["target"], outskirts["target"])

    def test_freeway_and_dual_carriageway_double_the_target(self):
        single = profile_at(50, "metropolitan", density=0)
        freeway = profile_at(50, "metropolitan", density=0, divided=True)
        self.assertEqual(freeway["target"], min(15.0, single["target"] * 2.0))
        self.assertEqual(freeway["roadMultiplier"], 2.0)
        self.assertTrue(traffic.is_divided_road("motorway", link=False))
        self.assertTrue(traffic.is_divided_road("primary", median_m=18.0))
        self.assertFalse(traffic.is_divided_road("primary", median_m=0.0))
        self.assertFalse(traffic.is_divided_road("residential", median_m=20.0))
        self.assertFalse(traffic.is_divided_road("motorway", link=True), "a motorway link/ramp is not a mainline")
        self.assertFalse(traffic.is_divided_road("motorway", median_m=22.0, link=True),
                         "a mapped motorway ramp remains excluded even if its median is populated")

    def test_server_profile_can_reach_a_larger_configured_limit(self):
        p = profile_at(0, "metropolitan", density=11.0, urban_share=0.35, limit=64)
        self.assertEqual(p["target"], 64.0)
        self.assertEqual(traffic.SERVER_TARGET_MAX, 999)

    def test_configured_maximum_is_an_absolute_cap(self):
        self.assertEqual(profile_at(0, "metropolitan", density=15, limit=15)["target"], 15.0)
        self.assertEqual(profile_at(0, "metropolitan", density=15, divided=True, limit=15)["target"], 15.0)
        self.assertEqual(profile_at(0, "metropolitan", density=15, limit=7)["target"], 7.0)

    def test_one_vehicle_per_adjustment(self):
        current = 12
        results = []
        while current != 7:
            next_value = traffic.next_population_step(current, 7)
            self.assertEqual(abs(next_value - current), 1)
            current = next_value
            results.append(current)
        self.assertEqual(results, [11, 10, 9, 8, 7])
        self.assertEqual(traffic.next_population_step(3, 8), 4)

    def test_missing_settlement_uses_local_road_fallback(self):
        total = metres_for_density(5.0)
        profile = traffic.calculate(total, total * 0.2, total * 0.1, total * 0.6, settlement=None)
        self.assertEqual(profile["source"], "mapped-roads-fallback")
        self.assertIsNone(profile["distanceKm"])
        self.assertIsNone(profile["settlement"])
        self.assertGreater(profile["target"], 0.0)

    def test_missing_settlement_data_uses_road_network_fallback(self):
        total = metres_for_density(4.5)
        p = traffic.calculate(total, total * 0.2, 0, total * 0.7, settlement=None)
        self.assertIsNone(p["settlement"])
        self.assertIsNone(p["distanceKm"])
        self.assertEqual(p["source"], "mapped-roads-fallback")
        self.assertGreater(p["target"], 0)
        self.assertLessEqual(p["target"], 15)
        self.assertEqual(traffic.calculate(0, 0, 0, 0, settlement=None)["target"], 0)

    def test_profile_includes_settlement_coordinates_for_live_distance_updates(self):
        profile = profile_at(20)
        self.assertEqual(profile["settlementLat"], -37.8)
        self.assertEqual(profile["settlementLon"], 145.0)
        missing = traffic.calculate(0, 0, 0, 0, settlement=None)
        self.assertIsNone(missing["settlementLat"])
        self.assertIsNone(missing["settlementLon"])

    def test_target_does_not_jump_at_distance_anchors(self):
        values = [profile_at(d, density=0)["target"] for d in (199.0, 199.5, 200.0, 200.5, 201.0)]
        self.assertTrue(all(abs(b - a) < 0.02 for a, b in zip(values, values[1:])), values)

    def test_road_class_multipliers_scale_target(self):
        total = metres_for_density(4.5)
        kw = dict(settlement=None, sample_radius_m=traffic.SAMPLE_RADIUS_M, max_traffic=15)
        p_motorway = traffic.calculate(total, 0, total, 0, road_class="motorway", **kw)
        p_primary = traffic.calculate(total, 0, total, 0, road_class="primary", **kw)
        p_secondary = traffic.calculate(total, 0, 0, total, road_class="secondary", **kw)
        p_residential = traffic.calculate(total, 0, 0, total, road_class="residential", **kw)
        p_service = traffic.calculate(total, 0, 0, total, road_class="service", **kw)
        p_track = traffic.calculate(total, 0, 0, total, road_class="track", **kw)

        self.assertGreater(p_motorway["target"], p_primary["target"])
        self.assertGreater(p_primary["target"], p_secondary["target"])
        self.assertGreater(p_secondary["target"], p_residential["target"])
        self.assertGreater(p_residential["target"], p_service["target"])
        self.assertGreater(p_service["target"], p_track["target"])
        self.assertEqual(p_motorway["roadMultiplier"], 2.0)
        self.assertAlmostEqual(p_primary["roadMultiplier"], 1.3, places=2)
        self.assertAlmostEqual(p_residential["roadMultiplier"], 0.4, places=2)
        self.assertAlmostEqual(p_track["roadMultiplier"], 0.02, places=2)

    def test_lane_count_scales_target_with_diminishing_returns(self):
        total = metres_for_density(4.5)
        kw = dict(settlement=None, road_class="motorway", sample_radius_m=traffic.SAMPLE_RADIUS_M, max_traffic=100)
        p_2lane = traffic.calculate(total, 0, total, 0, lanes=2, **kw)
        p_4lane = traffic.calculate(total, 0, total, 0, lanes=4, **kw)
        p_8lane = traffic.calculate(total, 0, total, 0, lanes=8, **kw)

        self.assertGreater(p_8lane["target"], p_4lane["target"])
        self.assertGreater(p_4lane["target"], p_2lane["target"])
        self.assertEqual(p_4lane["roadLanes"], 4)
        self.assertEqual(p_8lane["roadLanes"], 8)
        # 8 lanes vs 4 lanes has sqrt(8/4) = sqrt(2) ~ 1.414 ratio
        ratio = p_8lane["roadMultiplier"] / p_4lane["roadMultiplier"]
        self.assertAlmostEqual(ratio, math.sqrt(2.0), places=2)

    def test_road_lanes_attribute_parsing(self):
        total = metres_for_density(2.0)
        p_valid = traffic.calculate(total, 0, 0, 0, settlement=None, lanes="4")
        self.assertEqual(p_valid["roadLanes"], 4)
        p_nan = traffic.calculate(total, 0, 0, 0, settlement=None, lanes=float("nan"))
        self.assertIsNone(p_nan["roadLanes"])
        p_none = traffic.calculate(total, 0, 0, 0, settlement=None, lanes=None)
        self.assertIsNone(p_none["roadLanes"])


if __name__ == "__main__":
    unittest.main()
