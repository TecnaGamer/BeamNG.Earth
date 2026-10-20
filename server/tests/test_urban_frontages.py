"""Reported floating building fragments and gaps between city facades and sidewalks."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings as bld
import polygon
import roads
import sidewalks
from test_building_fidelity import FRAME, features, flat, rect


def area(cells):
    return sum(abs(bld._area2(c)) / 2 for c in cells)


class PartsAndFootprintsTest(unittest.TestCase):
    def test_overlapping_cutters_are_a_union_not_an_even_odd_fill(self):
        cells = polygon.subtract_cells(rect(0, 0, 10, 10), [rect(2, 0, 4, 10), rect(4, 0, 4, 10)])
        self.assertAlmostEqual(area(cells), 40)
        self.assertTrue(all(not 2 < p[:, 0].mean() < 8 for p in cells))

    def test_crossing_edges_and_external_cutters_keep_exact_area(self):
        triangle = np.array([[-5, 0], [15, 0], [5, 10]], float)
        cells = polygon.subtract_cells(rect(0, 0, 10, 10), [triangle])
        self.assertAlmostEqual(area(cells), 25)
        for c in cells:
            self.assertFalse(polygon.contains_points(triangle, c.mean(0)[None])[0])

    def test_roof_part_does_not_replace_the_parent_body(self):
        parent = features([[rect(20, 20, 40, 40)]], height=12.0)
        part = features([[rect(30, 30, 10, 10)]], height=12.0, min_height=8.0,
                        building_id='stable-building-id')
        items = bld.tile_buildings(FRAME, 0, 0, 256, 256, [parent], flat, part_layers=[part])
        bases = [s for s, _, _ in items if s.min_h == 0]
        self.assertAlmostEqual(area([s.ring for s in bases]), 1600, places=3)
        self.assertTrue(all(s.height == 8 for s in bases))
        self.assertEqual(max(s.height + s.min_h for s, _, _ in items), 12)

    def test_partial_ground_parts_keep_the_uncovered_wing(self):
        parent = features([[rect(20, 20, 40, 40)]], height=12.0)
        part = features([[rect(20, 20, 20, 40)]], height=8.0, building_id='stable-building-id')
        items = bld.tile_buildings(FRAME, 0, 0, 256, 256, [parent], flat, part_layers=[part])
        self.assertAlmostEqual(area([s.ring for s, _, _ in items if s.min_h == 0]), 1600, places=3)

    def test_explicit_elevated_parent_remains_elevated(self):
        parent = features([[rect(20, 20, 40, 40)]], height=12.0, min_height=8.0)
        part = features([[rect(30, 30, 10, 10)]], height=12.0, min_height=8.0,
                        building_id='stable-building-id')
        items = bld.tile_buildings(FRAME, 0, 0, 256, 256, [parent], flat, part_layers=[part])
        self.assertTrue(all(s.min_h == 8 for s, _, _ in items))

    def test_disconnected_parent_wing_is_not_lost_when_only_one_wing_has_parts(self):
        parent = features([[rect(20, 20, 20, 20)], [rect(60, 20, 20, 20)]], height=12.0)
        part = features([[rect(20, 20, 20, 20)]], height=8.0, building_id='stable-building-id')
        items = bld.tile_buildings(FRAME, 0, 0, 256, 256, [parent], flat, part_layers=[part])
        self.assertAlmostEqual(area([s.ring for s, _, _ in items]), 800, places=3)

    def test_parts_share_one_foundation_datum_on_sloped_ground(self):
        parent = features([[rect(20, 20, 40, 40)]], height=12.0)
        part = features([[rect(20, 20, 20, 40)], [rect(40, 20, 20, 40)]], height=8.0,
                        building_id='stable-building-id')
        def slope(la, lo):
            east, _, _ = FRAME.geodetic_to_enu(la, lo, 0)
            return 10.0 + east * 0.1
        items = bld.tile_buildings(FRAME, 0, 0, 256, 256, [parent], slope, part_layers=[part])
        levels = [bld.base_z(s, low, high) for s, low, high in items]
        np.testing.assert_allclose(levels, levels[0], atol=1e-8)

    def test_skewed_house_walls_keep_the_mapped_outline(self):
        ring = np.array([[0, 0], [20, 0], [20, 10], [0.3, 10]], float)
        spec = bld.plan(ring, {'class': 'house', 'roof_shape': 'gabled', 'height': 9.0})
        np.testing.assert_array_equal(bld.drawn_ring(spec), ring)
        collision = bld.collision_mesh([(spec, 0, 0)])
        p = collision[0]
        walls = p[p[:, 2] < 0]
        self.assertTrue(all(np.linalg.norm(ring - xy, axis=1).min() < 1e-8 for xy in walls[:, :2]))

    def test_narrow_notched_roofs_do_not_have_unsupported_equipment(self):
        ring = np.array([[0, 0], [100, 0], [100, 8], [8, 8], [8, 100], [0, 100]], float)
        checked = 0
        for seed in np.linspace(0.01, 0.99, 30):
            spec = bld.plan(ring, {'roof_shape': 'flat', 'height': 10.0}, look=float(seed))
            with patch.object(bld, '_box') as box:
                bld._clutter(bld.Mesh(), spec, ring, 10, bld._Looks(spec.seed))
                for args, _ in box.call_args_list:
                    _, c, u, hx, hy, *_ = args
                    v = np.array([-u[1], u[0]])
                    corners = np.array([c + u * x * hx + v * y * hy for x, y in
                                        [(-1, -1), (1, -1), (1, 1), (-1, 1)]])
                    self.assertTrue(polygon.contains_points(ring, corners).all())
                    checked += 1
        self.assertGreater(checked, 0)

    def test_a_street_in_a_courtyard_does_not_raise_the_building(self):
        spec = bld.plan(rect(0, 0, 60, 60), {'height': 12, '_holes': [rect(10, 10, 40, 40)]})
        items = [(spec, 0, 0)]
        road = np.array([[15, 30], [45, 30]], float)
        self.assertIs(bld.street_passages(items, [road])[0][0], spec)

    def test_a_street_grazing_a_facade_does_not_remove_the_wall(self):
        spec = bld.plan(rect(0, 0, 60, 40), {'height': 12})
        items = [(spec, 0, 0)]
        road = np.array([[-10, 1], [70, 1]], float)
        self.assertIs(bld.street_passages(items, [road])[0][0], spec)

    def test_explicit_bridge_structure_keeps_its_existing_passage_generation(self):
        spec = bld.plan(rect(0, 0, 60, 40), {'class': 'bridge_structure', 'height': 60})
        items = [(spec, 0, 0)]
        road = np.array([[-10, 20], [70, 20]], float)
        self.assertIs(bld.street_passages(items, [road])[0][0], spec)


class SidewalkContinuityTest(unittest.TestCase):
    def test_reversed_middle_run_keeps_a_joined_chain_in_order(self):
        def run(start, end, side, fid):
            return dict(pts=np.array([[start, 0, 0], [end, 0, 0]], float), side=side,
                        w=2.5, fid=fid, j=(False, False), open=(True, True))
        joined = roads.join_walk_ends([run(0, 10, 1, 'a'), run(20, 10, -1, 'b'),
                                      run(20, 30, 1, 'c')], [])
        self.assertEqual(len(joined), 1)
        self.assertAlmostEqual(np.linalg.norm(np.diff(joined[0]['pts'], axis=0), axis=1).sum(), 30)
        self.assertEqual(set(joined[0]['pts'][[0, -1], 0]), {0, 30})

    def test_walks_on_different_levels_are_not_joined(self):
        def run(points, fid):
            return dict(pts=np.array(points, float), side=1, w=2.5, fid=fid,
                        j=(False, False), open=(True, True))
        joined = roads.join_walk_ends([run([[0, 0, 0], [10, 0, 0]], 'a'),
                                      run([[10, 0, 5], [20, 0, 5]], 'b')], [])
        self.assertEqual(len(joined), 2)

    def test_short_sidewalk_piece_survives_a_nearby_crossing(self):
        points = np.array([[0, 0, 0], [2, 0, 0]], float)
        blockers = [(np.array([[0, 20], [2, 20]], float), 1.0)]
        self.assertEqual(roads.cut_outside(points, blockers), [])
        kept = roads.cut_outside(points, blockers, 0.5, min_piece=0.05)
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(np.linalg.norm(kept[0][-1] - kept[0][0]), 2)

    def test_coincident_and_small_gaps_join_without_duplicate_vertices(self):
        for gap in (0, 0.1, 0.29):
            def run(points, fid):
                return dict(pts=np.array(points, float), side=1, w=2.5, fid=fid,
                            j=(False, False), open=(True, True))
            joined = roads.join_walk_ends([run([[0, 0, 0], [10, 0, 0]], 'a'),
                                          run([[10 + gap, 0, 0], [20, 0, 0]], 'b')], [])
            self.assertEqual(len(joined), 1, gap)
            self.assertTrue((np.linalg.norm(np.diff(joined[0]['pts'], axis=0), axis=1) > 0).all())


class PavedFrontageTest(unittest.TestCase):
    def items(self, raised=False):
        spec = bld.plan(rect(20, 20, 10, 10), {'class': 'house', 'roof_shape': 'flat', 'height': 9})
        spec.gring = np.full(4, 10.0)
        spec.min_h = 6 if raised else 0
        return [(spec, 10, 10)]

    def walk(self, y=17, side=1):
        return dict(pts=np.array([[15, y, 10], [35, y, 10]], float), side=side, w=2.0)

    def test_paving_reaches_existing_sidewalk_without_crossing_its_curb(self):
        items = self.items()
        bld.pave_frontages(items, [self.walk()], 0, 0)
        apron = items[0][0].aprons
        self.assertTrue(apron)
        self.assertAlmostEqual(area([p[:, :2] for p in apron]), 20)
        points = np.concatenate(apron)
        self.assertGreaterEqual(points[:, 1].min(), 18)
        self.assertLessEqual(points[:, 1].max(), 20)
        self.assertTrue(np.allclose(points[:, 2], 10 + sidewalks.CURB_H + 2 * sidewalks.CROSS_SLOPE))
        for mesh in (bld.build(items), bld.build(items, lite=True), bld.collision_mesh(items)):
            p, _, t = mesh[:3]
            tri = p[t]
            middle = tri.mean(1)
            top = ((middle[:, 1] > 18) & (middle[:, 1] < 20) &
                   np.isclose(middle[:, 2], points[0, 2]))
            self.assertTrue(top.any())
            self.assertTrue((np.cross(tri[top, 1] - tri[top, 0], tri[top, 2] - tri[top, 0])[:, 2] > 0).all())

    def test_paving_does_not_fill_a_garden_opposite_side_or_elevated_part(self):
        for walks, raised in (([self.walk(y=10)], False), ([self.walk(side=-1)], False),
                              ([self.walk()], True), ([], False)):
            items = self.items(raised)
            bld.pave_frontages(items, walks, 0, 0)
            self.assertFalse(items[0][0].aprons)


class BuildingGroundPipelineTest(unittest.TestCase):
    """Production building paths must sample the graded ground the tyres use."""
    def state(self):
        raw = lambda la, lo, zoom: np.full(np.shape(la), 100.0)
        grader = SimpleNamespace(apply=lambda la, lo, z, lift: z - 7.0 + lift)
        return SimpleNamespace(md=object(), dem=SimpleNamespace(sample=raw), airports=None, sea=None,
                               anchor=lambda *args: (grader, None)), grader

    def generate(self, frame, tx, ty, md, elev, *args):
        return elev(np.array([0.0]), np.array([0.0]))

    def test_worker_far_buildings_use_graded_ground(self):
        import blockworker
        import grading
        state, _ = self.state()
        with patch.object(blockworker, '_s', state), patch.object(bld, 'far_tile', side_effect=self.generate):
            z = blockworker.far_buildings_tile(1, 0, 0, 15, 0, 0, 'unused', '/unused')
        np.testing.assert_allclose(z, [93 + grading.ROAD_LIFT])

    def test_main_process_far_buildings_use_the_same_graded_ground(self):
        import earthd
        import grading
        state, grader = self.state()
        server = earthd.Server.__new__(earthd.Server)
        server.anchors = {1: SimpleNamespace(zooms={0: 15}, frame=FRAME)}
        server.buildings, server.mapdata, server.far_bld_pool = True, state.md, None
        server.dem, server.airports, server.sea = state.dem, None, None
        server.grader_for = lambda anchor: grader
        server.out_dir = server.vfs_dir = 'unused'
        with patch.object(bld, 'far_tile', side_effect=self.generate):
            z = server.build_far_bld_tile(1, 0, 0)
        np.testing.assert_allclose(z, [93 + grading.ROAD_LIFT])


if __name__ == '__main__':
    unittest.main()
