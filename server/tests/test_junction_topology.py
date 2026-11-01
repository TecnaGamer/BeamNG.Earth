"""Only mapped joins may change a road profile; nearby roads are not junction constraints.

Synthetic and captured Overture geometry replay through the production snap and terrain grading kernels.
The source fixture deliberately uses controlled heights: it is not a claim about the local DEM or a drive.
"""
import itertools
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy
import grading
import mapgeom
import roads
import plancache
from test_grading import FakeMD
from test_roads import features, rules, FRAME

PRIMARY = roads.Spec(8.0, "paved", False, "primary", False)
SERVICE = roads.Spec(4.0, "paved", False, "service", False)


def plan(xy, z, rid, abut=(False, False)):
    xy = np.asarray(xy, np.float64)
    p = grading.Plan(xy, np.broadcast_to(z, len(xy)).copy(), 3.0, abut, road_half=2.0)
    p.rid = rid
    return p


def surface(plans):
    """Terrain/road consumers' common height field at centres and both carriageway edges."""
    x = np.tile(np.arange(0.0, 80.1, 2.0), 3)
    y = np.repeat([4.0, 6.0, 8.0], 41)
    return grading.grade_points(x, y, np.full(len(x), 12.0), grading.PlanPack(plans), grading.ROAD_LIFT)


def original_sources(feats):
    return [(i, source, lo, hi) for i in range(len(feats))
            for _part, source, lo, hi in roads.ground_parts_with_source(feats, i)]


def captured_case():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "dundonald_junction_topology.json").read_text())
    rows = fixture["roads"]
    frame = geodesy.EnuFrame(*fixture["provenance"]["location"])
    f = mapgeom.build("roads", [r["fid"] for r in rows],
                      [(mapgeom.LINE, [[np.asarray(r["line"], np.float64)]]) for r in rows])
    f.attrs["class"] = np.array([r["class"] for r in rows])
    f.attrs["subtype"] = np.array(["road"] * len(rows))
    f.rules["connectors"] = rules([(i, name, at, at) for i, r in enumerate(rows)
                                   for name, at in r["connectors"]])
    specs = [SERVICE, roads.Spec(8.0, "paved", False, "trunk", False),
             roads.Spec(6.0, "paved", False, "residential", False)]
    source = [(r["fid"], sp, np.asarray(r["line"], np.float64)) for r, sp in zip(rows, specs)]
    plans = []
    for i, (fid, _spec, line) in enumerate(source):
        e, n, _ = frame.geodetic_to_enu(line[:, 1], line[:, 0], 0.0)
        xy = grading.resample(np.c_[e, n], 2.0)[0]
        plans.append(plan(xy, 12.0 if i == 0 else 10.0, grading.road_id(fid)))
    return frame, f, source, plans, specs


class TopologySnapTest(unittest.TestCase):
    def test_disconnected_dead_end_keeps_its_profile_and_full_width_surface(self):
        x = np.arange(0.0, 201.0, 4.0)
        main = plan(np.c_[x - 100, np.zeros(len(x))], 10.0, 1)
        side = plan(np.c_[x, np.full(len(x), 6.0)], 12.0 + 0.03 * x, 2)
        own = side.z.copy()
        before = surface([main, side])
        # Both roads have topology. Their nearest endpoints/centrelines are not a shared node.
        nodes = [[("main", (-100.0, 0.0), 0)], [("dead-end", (0.0, 6.0), 0)]]
        grading.snap_junctions([main, side], [PRIMARY, SERVICE], nodes)
        np.testing.assert_array_equal(side.z, own)
        np.testing.assert_array_equal(surface([main, side]), before)
        self.assertAlmostEqual((side.z[-1] - side.z[0]) / 200.0, 0.03)

    def test_unknown_topology_does_not_guess_a_join(self):
        a = plan([[0, 0], [100, 0]], 10.0, 1)
        b = plan([[50, 0], [50, 100]], 12.0, 2)
        grading.snap_junctions([a, b], [PRIMARY, SERVICE], [[], []])
        np.testing.assert_array_equal(b.z, [12.0, 12.0])

    def test_shared_node_uses_its_own_target_not_a_closer_hairpin_leg(self):
        # The joined road doubles back past the side road's displaced map endpoint.
        # Its connector is at (0, 0), not on its unrelated upper leg at (0, 4).
        main = plan([[0, 0], [100, 0], [100, 4], [0, 4]], [10, 10, 14, 14], 1)
        side = plan([[0, 3], [-40, 3], [-80, 3]], 12.0, 2)
        nodes = [[("join", (0.0, 0.0), 0)], [("join", (0.0, 3.0), 0)]]
        grading.snap_junctions([main, side], [PRIMARY, SERVICE], nodes)
        self.assertEqual(side.z[0], 10.0)
        self.assertEqual(side.z[-1], 12.0)
        np.testing.assert_array_equal(main.z, [10, 10, 14, 14])

    def test_one_features_nearby_parts_do_not_snap_to_each_other(self):
        a = plan([[0, 0], [100, 0]], 10.0, 7)
        b = plan([[0, 4], [100, 4]], 13.0, 7)
        grading.snap_junctions([a, b], [PRIMARY, SERVICE],
                              [[("loop", (0.0, 0.0), 0)], [("loop", (0.0, 4.0), 0)]])
        np.testing.assert_array_equal(b.z, [13.0, 13.0])

    def test_crossings_with_distinct_nodes_keep_their_vertical_layer(self):
        x = np.arange(-100.0, 101.0, 4.0)
        a = plan(np.c_[x, np.zeros(len(x))], 10.0, 1)
        b = plan(np.c_[np.zeros(len(x)), x], 14.0, 2)
        with mock.patch.object(grading, "COARSE_EASE_ON", True):
            grading.snap_junctions([a, b], [PRIMARY, SERVICE],
                                  [[("surface", (0.0, 0.0), -1)], [("lower", (0.0, 0.0), -1)]])
        np.testing.assert_array_equal(b.z, np.full(len(x), 14.0))

    def test_height_guard_and_abutment_remain_constraints(self):
        for height, abut in ((18.0, (False, False)), (12.0, (True, False))):
            with self.subTest(height=height, abut=abut):
                a = plan([[0, 0], [100, 0]], 10.0, 1)
                b = plan([[0, 0], [0, 100]], height, 2, abut)
                grading.snap_junctions([a, b], [PRIMARY, SERVICE],
                                      [[("join", (0.0, 0.0), 0)], [("join", (0.0, 0.0), 0)]])
                np.testing.assert_array_equal(b.z, [height, height])

    def test_input_order_does_not_choose_the_junction_height(self):
        expected = None
        for order in itertools.permutations(range(3)):
            ps = [plan([[0, 0], [100, 0]], 10.0, 10),
                  plan([[0, 0], [-100, 0]], 11.0, 20),
                  plan([[0, 0], [0, 100]], 12.0, 30)]
            specs = [PRIMARY, PRIMARY, SERVICE]
            grading.snap_junctions([ps[i] for i in order], [specs[i] for i in order],
                                  [[("join", (0.0, 0.0), 0)] for _i in order])
            result = [p.z.tolist() for p in ps]
            if expected is None:
                expected = result
            self.assertEqual(result, expected)
        self.assertEqual(expected[2][0], 10.0)


class SourceConnectorTest(unittest.TestCase):
    def test_clipped_piece_keeps_only_actual_source_connector_endpoints(self):
        f = features([[(-4000, 0), (0, 0), (4000, 0)]], ["primary"], ids=["long"])
        f.rules["connectors"] = rules([(0, "west", 0.0, 0.0), (0, "east", 1.0, 1.0)])
        ring = f.parts(0)[0][0]
        g = grading._Planner(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        clipped = g._near_cell(ring, (-100, -20, 100, 20), ring, 0.0, 1.0)
        self.assertTrue(clipped)
        self.assertEqual(grading.junction_nodes(FRAME, f, [(0, ring, a, b) for _part, a, b in clipped]), [[]])
        retained, a, b = g._near_cell(ring, (-5000, -20, 100, 20), ring, 0.0, 1.0)[0]
        np.testing.assert_array_equal(retained[0], ring[0])
        nodes = grading.junction_nodes(FRAME, f, [(0, ring, a, b)])[0]
        self.assertEqual([(name, end) for name, _xy, end in nodes], [("west", 0)])

    def test_long_clipped_join_survives_enu_roundtrip_in_terrain_consumer(self):
        frame = geodesy.EnuFrame(0.0, 0.0)
        lines = [np.array([[0.1, -0.05], [0.1, 0.05]]), np.array([[0.1, 0.0], [0.102, 0.0]])]
        f = mapgeom.build("roads", ["main", "side"], [(mapgeom.LINE, [[line]]) for line in lines])
        f.attrs["class"] = np.array(["primary", "service"])
        f.attrs["subtype"] = np.array(["road", "road"])
        f.rules["connectors"] = rules([(0, "join", 0.5, 0.5), (1, "join", 0.0, 0.0)])
        g = grading._Planner(frame, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        main, original, lo, hi = roads.ground_parts_with_source(f, 0)[0]
        piece, start, end = g._near_cell(main, (11000, -100, 11200, 100), original, lo, hi)[0]
        # The planning geometry really is displaced by the u=0/h=0 roundtrip, as in the report.
        self.assertGreater(float(np.max(np.abs(piece[:, 0] - 0.1))), 1e-7)
        self.assertLess(start, 0.5)
        self.assertGreater(end, 0.5)
        side, side_source, a, b = roads.ground_parts_with_source(f, 1)[0]
        nodes = grading.junction_nodes(frame, f, [(0, original, start, end), (1, side_source, a, b)])
        self.assertEqual([[(name, end) for name, _point, end in row] for row in nodes],
                         [[("join", -1)], [("join", 0)]])
        plans = grading.plan_roads(frame, [(piece, PRIMARY), (side, SERVICE)],
                                   lambda la, lo: np.full(np.shape(la), 10.0), cuts=False)
        plans[0].rid, plans[1].rid = 1, 2
        plans[1].z = np.full(len(plans[1].z), 12.0)
        q = plans[1].xy[4:15]
        q = np.concatenate([q + [0.0, lateral] for lateral in (-1.0, 0.0, 1.0)])
        before = grading.grade_points(q[:, 0], q[:, 1], np.full(len(q), 12.0), plans)
        grading.snap_junctions(plans, [PRIMARY, SERVICE], nodes)
        self.assertAlmostEqual(plans[1].z[0], 10.0, places=6)
        self.assertEqual(plans[1].z[-1], 12.0)
        after = grading.grade_points(q[:, 0], q[:, 1], np.full(len(q), 12.0), plans)
        self.assertGreater(float(np.max(before - after)), 0.25)

    def test_original_corner_connector_survives_resampling_chord(self):
        # The original node is at station102: halfway between the plan's retained100/104m
        # vertices. The 90-degree corner is sqrt(2)m from their chord, not a missing join.
        f = features([[(-102, 0), (0, 0), (0, 98)], [(0, 0), (0, -200)]],
                     ["primary", "service"], ids=["corner", "side"])
        f.rules["connectors"] = rules([(0, "join", 0.51, 0.51), (1, "join", 0.0, 0.0)])
        specs = [PRIMARY, SERVICE]
        parts = [(f.parts(i)[0][0], specs[i]) for i in range(2)]
        ps = grading.plan_roads(FRAME, parts, lambda la, lo: np.full(np.shape(la), 10.0), cuts=False)
        ps[0].rid, ps[1].rid = 1, 2
        ps[1].z = np.full(len(ps[1].z), 12.0)
        source = [(i, parts[i][0], 0.0, 1.0) for i in range(2)]
        nodes = grading.junction_nodes(FRAME, f, source)
        self.assertEqual([len(row) for row in nodes], [1, 1])
        point = np.asarray(nodes[0][0][1])
        a, delta = ps[0].xy[:-1], np.diff(ps[0].xy, axis=0)
        t = np.clip(((point - a) * delta).sum(axis=1) / (delta * delta).sum(axis=1), 0.0, 1.0)
        self.assertGreater(np.linalg.norm(a + t[:, None] * delta - point, axis=1).min(), 1.0)
        q = ps[1].xy[:4]
        q = np.concatenate([q + [lateral, 0.0] for lateral in (-1.0, 0.0, 1.0)])
        before = grading.grade_points(q[:, 0], q[:, 1], np.full(len(q), 12.0), ps)
        grading.snap_junctions(ps, specs, nodes)
        self.assertAlmostEqual(ps[1].z[0], 10.0, places=6)
        self.assertEqual(ps[1].z[-1], 12.0)
        after = grading.grade_points(q[:, 0], q[:, 1], np.full(len(q), 12.0), ps)
        self.assertGreater(float(np.max(before - after)), 0.25)

    def test_clipped_nearby_hairpin_leg_does_not_gain_another_legs_connector(self):
        # The two clipped legs are 2 cm apart, but the connector is only on the outgoing leg.
        f = features([[(-4000, 0), (4000, 0), (4000, 0.02), (-4000, 0.02)]],
                     ["primary"], ids=["hairpin"])
        f.rules["connectors"] = rules([(0, "join", 0.25, 0.25)])
        ground, source, lo, hi = roads.ground_parts_with_source(f, 0)[0]
        g = grading._Planner(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        clipped = g._near_cell(ground, (-100, -5, 100, 5), source, lo, hi)
        self.assertEqual(len(clipped), 2)
        nodes = grading.junction_nodes(FRAME, f, [(0, source, a, b) for _part, a, b in clipped])
        self.assertEqual([[name for name, _xy, _end in row] for row in nodes], [["join"], []])

    def test_clipping_preserves_stations_after_a_bridge_or_tunnel(self):
        for flag in ("is_bridge", "is_tunnel"):
            f = features([[(-6000, 0), (-2000, 0), (0, 0), (2000, 0), (6000, 0)]],
                         ["primary"], ids=["long-layer"], flags=[(0, flag, 0.2, 0.4)])
            f.rules["connectors"] = rules([(0, "under", 0.3, 0.3), (0, "ground", 0.5, 0.5),
                                           (0, "outside", 0.8, 0.8)])
            ground, source, lo, hi = roads.ground_parts_with_source(f, 0)[-1]
            g = grading._Planner(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
            clipped = g._near_cell(ground, (-100, -20, 100, 20), source, lo, hi)
            self.assertEqual(len(clipped), 1)
            nodes = grading.junction_nodes(FRAME, f, [(0, source, a, b) for _part, a, b in clipped])
            self.assertEqual([[name for name, _xy, _end in row] for row in nodes], [["ground"]])

    def test_tunnel_and_bridge_ranges_do_not_supply_surface_connectors(self):
        for flag in ("is_bridge", "is_tunnel"):
            f = features([[(-100, 0), (100, 0)]], ["primary"], ids=["layers"],
                         flags=[(0, flag, 0.4, 0.6)])
            f.rules["connectors"] = rules([(0, "west", 0.0, 0.0), (0, "under", 0.5, 0.5),
                                           (0, "east", 1.0, 1.0)])
            bare = roads.ground_parts(f, 0)
            annotated = roads.ground_parts_with_source(f, 0)
            self.assertEqual(len(bare), len(annotated))
            for piece, (from_source, _ring, _lo, _hi) in zip(bare, annotated):
                np.testing.assert_array_equal(piece, from_source)
            parts = [(0, ring, lo, hi) for _piece, ring, lo, hi in annotated]
            nodes = grading.junction_nodes(FRAME, f, parts)
            self.assertEqual({name for row in nodes for name, _xy, _end in row}, {"west", "east"})

    def test_captured_dundonald_dead_end_does_not_join_nearby_trunk(self):
        frame, f, source, plans, specs = captured_case()
        before = [p.z.copy() for p in plans]
        # Use both road edges and centre, as the terrain and road mesh consumers do.
        xy = plans[0].xy
        normal = grading._normals(xy)
        q = np.concatenate([xy + lateral * normal for lateral in (-2.0, 0.0, 2.0)])
        z0 = grading.grade_points(q[:, 0], q[:, 1], np.full(len(q), 12.0), plans)
        nodes = grading.junction_nodes(frame, f, original_sources(f))
        grading.snap_junctions(plans, specs, nodes)
        np.testing.assert_array_equal(plans[0].z, before[0])
        np.testing.assert_array_equal(grading.grade_points(q[:, 0], q[:, 1], np.full(len(q), 12.0), plans), z0)
        # Castleview Road DOES share the trunk's connector: that true T remains eligible.
        self.assertTrue({name for name, _xy, _end in nodes[1]} & {name for name, _xy, _end in nodes[2]})
        self.assertFalse({name for name, _xy, _end in nodes[0]} & {name for name, _xy, _end in nodes[1]})

    def test_solved_source_geometry_roundtrips_for_workers_and_restarts(self):
        frame, f, source, plans, specs = captured_case()
        grading.snap_junctions(plans, specs, grading.junction_nodes(frame, f, original_sources(f)))
        q = plans[0].xy
        expected = grading.grade_points(q[:, 0], q[:, 1], np.full(len(q), 12.0), plans)
        named = [(fid, p) for (fid, _spec, _line), p in zip(source, plans)]
        key = (14, 7926, 5214)
        with tempfile.TemporaryDirectory() as root:
            writer = plancache.PlanCache(root, "topology")
            self.assertTrue(writer.store(key, named, [], [], []))
            for _ in range(2):  # independent consumers, including a fresh cache object after restart
                reader = plancache.PlanCache(root, "topology")
                restored = reader.load(key)
                self.assertIsNotNone(restored)
                copied = [p for _fid, p in restored[0]]
                np.testing.assert_array_equal(
                    grading.grade_points(q[:, 0], q[:, 1], np.full(len(q), 12.0), copied), expected)


if __name__ == "__main__":
    unittest.main()
