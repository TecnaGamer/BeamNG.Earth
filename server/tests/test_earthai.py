"""The standalone OSM lane graph and conservative Earth AI controller math."""
import os
import math
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import earthai


def way(wid, points, **tags):
    return {"id": wid, "highway": tags.pop("highway", "residential"), "points": points, **tags}


class EarthAiGraph(unittest.TestCase):
    def test_two_way_lanes_keep_to_the_correct_side(self):
        graph = earthai.build_lane_graph([way("a", [(0, 0), (10, 0)], lanes="2")])
        forward = graph["a:f:0"]["points"]
        backward = graph["a:b:0"]["points"]
        self.assertAlmostEqual(forward[0, 1], -earthai.LANE_M / 2)
        self.assertAlmostEqual(backward[0, 1], earthai.LANE_M / 2)
        left = earthai.build_lane_graph([way("a", [(0, 0), (10, 0)], lanes="2")], left_hand=True)
        self.assertGreater(left["a:f:0"]["points"][0, 1], 0)
        self.assertLess(left["a:b:0"]["points"][0, 1], 0)

    def test_oneway_reverse_and_mapped_speed(self):
        graph = earthai.build_lane_graph([
            way("a", [(0, 0), (10, 0)], oneway="-1", lanes="1", maxspeed="50 mph")
        ])
        lane = graph["a:b:0"]
        self.assertEqual(lane["direction"], -1)
        self.assertTrue(np.allclose(lane["points"][0], [10, 0]))
        self.assertAlmostEqual(lane["speed"], 50 * 0.44704)
        self.assertNotIn("a:f:0", graph)

    def test_intersection_selects_straight_road_with_same_name(self):
        graph = earthai.build_lane_graph([
            way("west", [(0, 0), (10, 0)], lanes="4", name="Main Street"),
            way("east", [(10, 0), (20, 0)], lanes="4", name="Main Street"),
            way("north", [(10, 0), (10, 10)], lanes="4", name="Side Street"),
        ])
        self.assertEqual(graph["west:f:0"]["next"], "east:f:0")
        self.assertEqual(graph["west:f:1"]["next"], "east:f:1", "lane continuation preserves the lane position")
        route = earthai.follow_route(graph, "west:f:0", 15)
        self.assertEqual(len(route), 3)
        self.assertTrue(np.allclose(route[0], [0, -1.65]))
        self.assertTrue(np.allclose(route[-1], [15, -1.65]))

    def test_dead_end_stops_route(self):
        graph = earthai.build_lane_graph([way("end", [(0, 0), (10, 0)], lanes="2")])
        route = earthai.follow_route(graph, "end:f:0", 500)
        self.assertEqual(route, [(0.0, -1.65), (10.0, -1.65)])
        self.assertIsNone(graph["end:f:0"]["next"])

    def test_route_can_start_mid_lane_and_spawns_are_spread(self):
        graph = earthai.build_lane_graph([
            way("long", [(0, 0), (500, 0)], oneway="yes", lanes="1")
        ])
        route = earthai.follow_route(graph, "long:f:0", distance_m=80, start_m=25)
        self.assertTrue(np.allclose(route[0], [25, 0]))
        self.assertTrue(np.allclose(route[-1], [105, 0]))
        plan = earthai.spawn_plan(graph, (250, 30), 2, min_distance=50, max_distance=200,
                                  min_separation=35, samples_per_lane=7)
        self.assertEqual(len(plan), 2)
        self.assertGreaterEqual(math.dist((plan[0]["x"], plan[0]["y"]),
                                          (plan[1]["x"], plan[1]["y"])), 35)
        self.assertTrue(all(len(item["route"]) >= 2 for item in plan))

    def test_speed_target_and_steering_are_bounded(self):
        self.assertEqual(earthai.speed_target(10, 20), (0.65, 0.0))
        self.assertEqual(earthai.speed_target(20, 10), (0.0, 0.8))
        self.assertEqual(earthai.pure_pursuit_steering((0, 0), 0, (0, 10)), 0.7)
        self.assertLess(earthai.pure_pursuit_steering((0, 0), 0, (10, -2)), 0)

    def test_private_and_driveway_roads_are_excluded(self):
        graph = earthai.build_lane_graph([
            way("public", [(0, 0), (100, 0)], access="yes"),
            way("private", [(0, 10), (100, 10)], access="private"),
            way("driveway", [(0, 20), (100, 20)], highway="service", service="driveway"),
            way("motor-permitted", [(0, 30), (100, 30)], access="private", motor_vehicle="permissive"),
        ])
        roads = {lane["way"] for lane in graph.values()}
        self.assertIn("public", roads)
        self.assertIn("motor-permitted", roads)
        self.assertNotIn("private", roads)
        self.assertNotIn("driveway", roads)


class EarthAiTracker(unittest.TestCase):
    def test_pool_deficit_rebuilds_after_cooldown(self):
        class Future:
            def __init__(self, value):
                self.value = value

            def add_done_callback(self, callback):
                callback(self)

            def result(self):
                return self.value

        class Loop:
            def run_in_executor(self, pool, fn, *args):
                return Future(fn(*args))

        class Connection:
            def __init__(self):
                self.messages = []

            def send(self, message):
                self.messages.append(message)

        tracker = earthai.Tracker()
        tracker.pool = object()
        conn, loop, calls = Connection(), Loop(), []

        def build(aid, lat, lon, amount):
            calls.append((aid, lat, lon, amount))
            return [{"route": []}]

        tracker.on_pose(conn, 1, 10, 20, True, 2, 0, 0, loop, build)
        self.assertEqual(len(calls), 1)
        tracker.on_pose(conn, 1, 10, 20, True, 2, 0, 2, loop, build)
        self.assertEqual(len(calls), 1, "a complete queued population does not rebuild")
        tracker.last_build -= tracker.REPLENISH_S + 1
        tracker.on_pose(conn, 1, 10, 20, True, 2, 0, 0, loop, build)
        self.assertEqual(len(calls), 2, "a depleted population is replenished while stationary")
        self.assertEqual(len(conn.messages), 2)


if __name__ == "__main__":
    unittest.main()
