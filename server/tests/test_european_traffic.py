"""Roundabout circulation/priority, narrow streets and curved AI routes, independent of a specific location."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import furniture
import islands
import navgraph
import roads
import roadwidth
from test_islands import arc
from test_roads import FRAME, features, flat, rules


def roundabout(reverse_alternate=False, mapped=False):
    lines, connectors, oneway = [], [], []
    for k in range(6):
        p = arc(18, k * math.pi / 3, (k + 1) * math.pi / 3, 9)[:, :2]
        reverse = reverse_alternate and k % 2 == 1
        lines.append(p[::-1] if reverse else p)
        a, b = ((k + 1) % 6, k) if reverse else (k, (k + 1) % 6)
        connectors.extend([(k, f"R{a}", 0, 0), (k, f"R{b}", 1, 1)])
        if mapped:
            oneway.append((k, -1.0 if reverse else 1.0))
    lines.append(np.array([(18.0, 0.0), (120.0, 0.0)]))
    connectors.extend([(6, "R0", 0, 0), (6, "entry", 1, 1)])
    f = features(lines, ["primary"] * 7, widths=[(i, 5.0 if i < 6 else 6.0) for i in range(7)])
    f.rules["connectors"] = rules(connectors)
    f.rules["oneway"] = rules(oneway, "float")
    return f


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


class RoundaboutTraffic(unittest.TestCase):
    def test_unmapped_ring_follows_the_driving_side_in_either_geometry_direction(self):
        f = roundabout(reverse_alternate=True)
        for left_hand in (False, True):
            with self.subTest(left_hand=left_hand):
                g = navgraph.build(FRAME, [f], flat, (0, 0), left_hand=left_hand, mph=False)
                ring = [e for e in g["edges"] if not any("id006" in str(n) or n == "centry" for n in e[:2])]
                self.assertTrue(ring)
                for e in ring:
                    self.assertEqual(e[2], 1, "the whole ring is one-way, including previously unmapped arcs")
                    self.assertEqual(e[4], 0)
                    turn = cross(g["nodes"][e[0]], g["nodes"][e[1]])
                    self.assertLess(turn if left_hand else -turn, 0)
                    self.assertLessEqual(e[5], navgraph.RING_MS + 0.01)

    def test_ring_has_priority_over_a_same_class_entry_and_no_stop_controller(self):
        g = navgraph.build(FRAME, [roundabout()], flat, (0, 0), mph=False)
        incident = [e for e in g["edges"] if "cR0" in e[:2]]
        entry = [e for e in incident if "id006" in e[0] or "id006" in e[1]]
        ring = [e for e in incident if e not in entry]
        self.assertEqual((len(entry), len(ring)), (1, 2))
        self.assertTrue(all(e[6] == 1.0 for e in ring))
        self.assertLess(entry[0][6], min(e[6] for e in ring), "stock AI uses this difference to give way")
        self.assertEqual(g["signals"], [], "a ring never acquires a stop-sign controller or traffic light")

    def test_mapped_circulation_is_preserved_even_when_country_fallback_differs(self):
        g = navgraph.build(FRAME, [roundabout(True, True)], flat, (0, 0), left_hand=True)
        ring = [e for e in g["edges"] if e[2] == 1]
        self.assertTrue(ring)
        self.assertTrue(all(cross(g["nodes"][e[0]], g["nodes"][e[1]]) > 0 for e in ring))

    def test_source_cell_order_does_not_change_the_graph(self):
        ring = roundabout(True)
        other = features([[(-100, 150), (100, 150)]], ["residential"], ids=["other"])
        a = navgraph.build(FRAME, [ring, other], flat, (0, 0))
        b = navgraph.build(FRAME, [other, ring], flat, (0, 0))
        self.assertEqual(a["nodes"], b["nodes"])
        self.assertEqual(sorted(a["edges"]), sorted(b["edges"]))

    def test_loop_crossed_by_a_road_keeps_its_two_way_connections(self):
        spec = roads.Spec(5.0, "paved", False, "residential", False)
        ring = [(f"r{k}", spec, arc(18, k * math.pi / 3, (k + 1) * math.pi / 3, 9), None) for k in range(6)]
        ring.append(("through", spec, np.array([(-30, 0, 0), (30, 0, 0)], float), None))
        self.assertEqual(islands.ring_directions(ring), {})

    def test_give_way_signs_only_face_external_incoming_arms(self):
        sp = roads.Spec(5.0, "paved", False, "primary", True, 1.0, 1)
        p = np.zeros(3)
        arms = [("r1", sp, np.array([0.0, 1.0]), p, True, 40),
                ("r2", sp, np.array([0.0, -1.0]), p, False, 40),
                ("in", sp, np.array([1.0, 0.0]), p, False, 60),
                ("out", sp, np.array([-1.0, 0.0]), p, True, 60)]
        signs = furniture.junction_controls(arms, None, ring={"r1", "r2"})
        self.assertEqual([(a[0], kind) for a, kind in signs], [("in", furniture.G_YIELD)])

    def test_rural_roundabout_has_visible_give_way_signs(self):
        sp = roads.Spec(5.0, "paved", False, "primary", True, 1.0, 1)
        entry = roads.Spec(6.0, "paved", False, "primary", False)
        rs = [(f"r{k}", sp, arc(18, k * math.pi / 3, (k + 1) * math.pi / 3, 9), None) for k in range(6)]
        rs.append(("entry", entry, np.array([(18, 0, 0), (120, 0, 0)], float), None))
        props = furniture.junction_props(rs, (-150, -150, 150, 150))
        self.assertEqual([r[0] for r in props], [furniture.G_YIELD])


class NarrowAndCurvedStreets(unittest.TestCase):
    def test_close_buildings_produce_a_narrow_street_without_overlapping_sidewalks(self):
        for row in (4.2, 6.0, 8.0, 18.0):
            with self.subTest(row=row):
                cw, walk = roadwidth.split(row)
                self.assertGreaterEqual(cw, roadwidth.CARRIAGE_MIN - 1e-8)
                self.assertGreaterEqual(walk, roadwidth.WALK_NARROW_MIN)
                self.assertAlmostEqual(cw + 2 * walk, row)
        f = features([[(0, 0), (100, 0)]], ["residential"])
        urban = lambda la, lo: np.ones(np.shape(la), bool)
        spec = roads.road_specs(f, urban, lambda fid, ll: roadwidth.split(6.0))[0]
        self.assertEqual((spec.width, spec.walk), (3.0, 1.5))
        g = navgraph.build(FRAME, [f], flat, (0, 0), urban_fn=urban,
                           widths=lambda fid, ll: roadwidth.split(6.0))
        self.assertTrue(g["edges"])
        self.assertTrue(all(e[7] == 1.5 and e[5] <= navgraph.NARROW_MS + 0.01 for e in g["edges"]))

    def test_shallow_s_bends_in_a_three_metre_street_are_not_straightened(self):
        f = features([[(0, 0), (20, 0.4), (40, -0.4), (60, 0)]], ["residential"], widths=[(0, 3.0)])
        g = navgraph.build(FRAME, [f], flat, (0, 0))
        ys = [n[1] for n in g["nodes"].values()]
        self.assertGreater(max(ys), 0.3)
        self.assertLess(min(ys), -0.3)

    def test_rdp_does_not_erase_a_collinear_hairpin_reversal(self):
        p = np.array([(0, 0), (10, 0), (20, 0), (5, 0)], float)
        keep = navgraph._rdp(p, 0.2)
        self.assertIn(2, keep)

    def test_bends_slow_traffic_before_the_turn_in_both_directions(self):
        xy = np.array([(0, 0), (40, 0), (60, 0), (60, 10), (60, 30), (60, 70)], float)
        speeds = navgraph._speed_profile(xy, 6.0, False, 80 / 3.6)
        self.assertLess(speeds[2], 30 / 3.6)
        self.assertLess(speeds[1], 80 / 3.6, "braking starts on the straight approach")
        self.assertLess(speeds[4], 80 / 3.6, "the opposite direction has braking room too")
        self.assertTrue(np.isfinite(speeds).all())
        self.assertTrue(np.allclose(speeds, navgraph._speed_profile(xy[::-1], 6.0, False, 80 / 3.6)[::-1]))

    def test_straight_motorways_keep_the_mapped_limit(self):
        f = features([[(0, 0), (500, 0)]], ["motorway"])
        f.rules["max_speed_kmh"] = rules([(0, 120)], "float")
        g = navgraph.build(FRAME, [f], flat, (0, 0), mph=False)
        self.assertTrue(all(abs(e[5] - 120 / 3.6) < 0.01 for e in g["edges"]))

    def test_a_long_road_crossing_the_window_is_not_lost_between_far_endpoints(self):
        f = features([[(-3000, 0), (3000, 0)]], ["primary"])
        g = navgraph.build(FRAME, [f], flat, (0, 0), window=500)
        self.assertTrue(g["edges"])
        self.assertTrue(any(n[0] < -400 for n in g["nodes"].values()))
        self.assertTrue(any(n[0] > 400 for n in g["nodes"].values()))

    def test_limit_changes_along_a_segment_and_posted_limits_stay_separate(self):
        f = features([[(0, 0), (300, 0)]], ["primary"])
        f.rules["max_speed_kmh"] = rules([(0, 30, 0, 0.25), (0, 80, 0.25, 0.75), (0, 50, 0.75, 1)], "float")
        g = navgraph.build(FRAME, [f], flat, (0, 0), mph=False)
        for e in g["edges"]:
            x = (g["nodes"][e[0]][0] + g["nodes"][e[1]][0]) / 2
            limit = 30 if x < 74 else 80 if x < 224 else 50
            self.assertAlmostEqual(e[5], limit / 3.6, delta=0.02)
        sp = roads.road_specs(f)[0]
        self.assertAlmostEqual(navgraph.road_mph(f, 0, sp, mph=False) * 1.609344, 80)


    def test_indian_coordinates_default_to_left_hand_and_kmh(self):
        c = navgraph.country_near(None, 28.6139, 77.2090)
        self.assertEqual(c, "IN")
        rules = navgraph.road_rules(c)
        self.assertTrue(rules["rightHandDrive"], "India drives on the left")
        self.assertFalse(rules["mph"], "India speed limits in km/h")


if __name__ == "__main__":
    unittest.main()
