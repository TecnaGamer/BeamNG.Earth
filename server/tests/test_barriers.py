"""Mapped boundary geometry, conservative gate matching and streamed shape contracts; no live data."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import barriers
import mapgeom
import roads
from test_roads import FRAME, features


def infra(lines, classes, tags=None, **attrs):
    f = features(lines, classes)
    f.layer = "infrastructure"
    f.kind = np.array([mapgeom.POINT if len(p) == 1 else mapgeom.LINE for p in lines])
    f.attrs["tags"] = np.array([json.dumps(t) for t in (tags or [{} for _ in lines])])
    f.attrs.update({k: np.asarray(v) for k, v in attrs.items()})
    return f


def ground(xy):
    return np.full(len(xy), 10.0)


def runs(f, box=(0, 0, 256, 256), stats=None):
    return barriers.plan(FRAME, [f], box, stats)


class BarrierTest(unittest.TestCase):
    def test_regional_renderer_keeps_source_ownership(self):
        f = infra([[(10, 10), (90, 10)], [(10, 20), (90, 20)]], ["wall", "wall"])
        planned = runs(f)
        with patch.object(barriers, "load", return_value=planned), \
                patch.object(barriers, "tile_meshes", return_value={}) as build:
            barriers.tile(FRAME, 0, 0, None, ground, [], [], exclude_fids={planned[0].fid})
        self.assertEqual([r.fid for r in build.call_args.args[0]], [planned[1].fid])

    def test_classes_and_explicit_tag_precedence(self):
        f = infra([[(10, y), (90, y)] for y in (10, 20, 30, 40)], ["fence", "wall", "hedge", "fence"],
                  [{}, {}, {}, {"barrier": "guard_rail"}])
        self.assertEqual([r.kind for r in runs(f)], ["fence", "wall", "hedge"])

    def test_raw_barrier_fallback_and_construction(self):
        f = infra([[(10, 10), (90, 10)]], ["unknown"],
                  [{"barrier": "fence", "fence_type": "chain_link", "material": "steel"}])
        self.assertEqual(runs(f)[0].style, "mesh")

    def test_unavailable_optional_layer_keeps_roads_usable(self):
        class Missing:
            def cell(self, *a, **kw):
                raise OSError("offline")
        self.assertEqual(barriers.load(FRAME, 0, 0, Missing()), [])

    def test_bad_tags_use_class_and_invalid_geometry_is_ignored(self):
        f = infra([[(10, 10), (90, 10)]], ["fence"])
        f.attrs["tags"] = np.array(["not json"])
        self.assertEqual(runs(f)[0].kind, "fence")
        f.lonlat[0] = np.nan
        f.__dict__.pop("_xy_in", None)
        self.assertEqual(runs(f), [])

    def test_material_families(self):
        for material, expected in (("brick", "bne_wall_brick"), ("stone", "bne_wall_stone"),
                                   ("concrete", "bne_bridge_concrete")):
            f = infra([[(10, 10), (90, 10)]], ["wall"], [{"material": material}])
            self.assertEqual(runs(f)[0].material, expected)

    def test_load_order_does_not_change_geometry(self):
        a = infra([[(10, 10), (90, 10)]], ["wall"])
        b = infra([[(10, 20), (90, 20)]], ["hedge"])
        b.ids = np.array(["different"])
        box = (0, 0, 256, 256)
        m = [barriers.tile_meshes(barriers.plan(FRAME, cells, box), box, ground) for cells in ([a, b], [b, a, b])]
        for key in m[0]:
            for i in (0, 1, 2, 4):
                np.testing.assert_array_equal(m[0][key][0][i], m[1][key][0][i])

    def test_rebase_preserves_world_positions_within_centimetre(self):
        import geodesy
        f = infra([[(10, 50), (150, 50)]], ["wall"])
        other = geodesy.EnuFrame(FRAME.lat0 + 0.001, FRAME.lon0 + 0.001, 0)
        box = (-256, -256, 256, 256)
        meshes = []
        for frame in (FRAME, other):
            r = barriers.plan(frame, [f], box)
            p = barriers.tile_meshes(r, box, ground)["boundary"][0][0] + [-256, -256, 0]
            la, lo, _ = frame.enu_to_geodetic(p[:, 0], p[:, 1], p[:, 2])
            x, y, _ = FRAME.geodetic_to_enu(la, lo, np.zeros(len(p)))
            meshes.append(np.c_[x, y])
        np.testing.assert_allclose(meshes[0], meshes[1], atol=0.01)

    def test_long_source_stationing_is_independent_of_anchor(self):
        import geodesy
        f = infra([[(-10000, 50), (150, 50)]], ["fence"])
        frames = [FRAME, geodesy.EnuFrame(FRAME.lat0 + 0.1, FRAME.lon0 + 0.1, 0)]
        rr = [barriers.plan(frame, [f], (-20000, -20000, 20000, 20000))[0] for frame in frames]
        np.testing.assert_array_equal(rr[0].s, rr[1].s)
        positions = []
        at = np.array([2000., 5000., 10000.])
        for frame, r in zip(frames, rr):
            xy = barriers._at(r.xy, r.s, at)
            _, _, z = frame.geodetic_to_enu(f.lonlat[:, 1], f.lonlat[:, 0], 0)
            la, lo, h = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.interp(at, r.s, z))
            positions.append(np.column_stack(geodesy.geodetic_to_ecef(la, lo, h)))
        np.testing.assert_allclose(positions[0], positions[1], atol=1e-6)

    def test_dimensions_and_units(self):
        for s, expected in [("2 m", 2), ("150cm", 1.5), (".9", .9), ("6' 2\"", 1.8796), ("5 ft", 1.524)]:
            self.assertAlmostEqual(barriers.metres(s, 1), expected)
        for s in ("nan", "inf", "-1", "1;2", "300", "none", "bad"):
            self.assertEqual(barriers.metres(s, 1.3), 1.3)
        f = infra([[(10, 10), (90, 10)]], ["wall"], height=[2.0])
        self.assertEqual(runs(f)[0].height, 2)

    def test_point_gate_cuts_clear_physical_width_not_legal_maxwidth(self):
        f = infra([[(10, 50), (150, 50)], [(80, 50)]], ["fence", "gate"],
                  [{}, {"width": "4", "maxwidth": "1", "access": "private", "locked": "yes"}])
        r = runs(f)[0]
        self.assertAlmostEqual(r.gaps[0][1] - r.gaps[0][0], 4)
        meshes = barriers.tile_meshes([r], (0, 0, 256, 256), ground)
        p, tri, *_ = meshes["boundary"][0]
        # No visual or colliding triangle crosses the gate's centre; no post intrudes into the clear gap.
        centre = barriers._at(r.xy, barriers._arc(r.xy), [np.mean(r.gaps[0][:2])])[0]
        self.assertFalse(np.any((p[tri, 0].min(1) < centre[0]) & (p[tri, 0].max(1) > centre[0])))

    def test_line_gate_uses_mapped_span(self):
        f = infra([[(10, 50), (150, 50)], [(78, 50), (83, 50)]], ["wall", "gate"])
        gap = runs(f)[0].gaps[0]
        self.assertAlmostEqual(gap[1] - gap[0], np.linalg.norm(f.xy_in(FRAME)[-1] - f.xy_in(FRAME)[-2]))

    def test_overlapping_gate_openings_merge_without_internal_posts(self):
        f = infra([[(10, 50), (150, 50)], [(80, 50)], [(82, 50)]], ["fence", "gate", "gate"],
                  [{}, {"width": "4"}, {"width": "4"}])
        r = runs(f)[0]
        self.assertEqual(len(r.gaps), 1)
        self.assertAlmostEqual(r.gaps[0][1] - r.gaps[0][0], 6, places=2)

    def test_gate_at_closed_ring_start_cuts_both_sides(self):
        f = infra([[(10, 10), (80, 10), (80, 80), (10, 10)], [(10, 10)]], ["fence", "gate"])
        r = runs(f)[0]
        self.assertEqual(len(r.gaps), 2)
        length = barriers._arc(r.xy)[-1]
        self.assertLess(r.gaps[0][0], 0)
        self.assertGreater(r.gaps[1][1], length)

    def test_lift_gate_and_entrance_remain_open(self):
        for cls in ("lift_gate", "entrance"):
            f = infra([[(10, 50), (150, 50)], [(80, 50)]], ["hedge", cls])
            r = runs(f)[0]
            self.assertEqual(len(r.gaps), 1)
            self.assertAlmostEqual(r.gaps[0][1] - r.gaps[0][0], barriers.GATE_WIDTH_M)

    def test_ambiguous_gate_does_not_cut_two_different_boundaries(self):
        f = infra([[(10, 50), (150, 50)], [(80, 10), (80, 150)], [(80, 50)]], ["wall", "wall", "gate"])
        stats = {}
        self.assertTrue(all(not r.gaps for r in runs(f, stats=stats)))
        self.assertEqual(stats["ambiguous"], 1)

    def test_collinear_join_at_gate_cuts_both_pieces(self):
        f = infra([[(10, 50), (80, 50)], [(80, 50), (150, 50)], [(80, 50)]], ["fence", "fence", "gate"])
        self.assertTrue(all(r.gaps for r in runs(f)))

    def test_unmatched_gate_does_not_invent_a_boundary(self):
        f = infra([[(80, 50)]], ["gate"])
        stats = {}
        self.assertEqual(runs(f, stats=stats), [])
        self.assertEqual(stats["unmatched"], 1)

    def test_different_level_and_tunnel_do_not_match(self):
        f = infra([[(10, 50), (150, 50)], [(80, 50)]], ["fence", "gate"], level=[0, 1])
        self.assertFalse(runs(f)[0].gaps)
        f.attrs["tags"] = np.array([json.dumps({"tunnel": "yes"}), "{}"])
        self.assertFalse(runs(f))

    def test_explicit_bridge_and_retaining_walls_are_outside_scope(self):
        f = infra([[(10, 10), (90, 10)], [(10, 20), (90, 20)]], ["wall", "wall"],
                  [{"bridge": "yes"}, {"wall": "retaining_wall"}])
        self.assertFalse(runs(f))

    def test_polygon_and_point_boundaries_are_not_invented_fences(self):
        f = infra([[(10, 10), (80, 10), (80, 80), (10, 10)], [(50, 50)]], ["hedge", "fence"])
        f.kind[0] = mapgeom.POLYGON
        self.assertFalse(runs(f))

    def test_deduplicates_cells_and_keeps_closed_linear_ring(self):
        f = infra([[(10, 10), (80, 10), (80, 80), (10, 10)]], ["fence"])
        got = barriers.plan(FRAME, [f, f], (0, 0, 256, 256))
        self.assertEqual(len(got), 1)
        np.testing.assert_array_equal(got[0].xy[0], got[0].xy[-1])

    def test_far_and_curved_bbox_miss_make_no_elevation_queries(self):
        f = infra([[(500, 500), (1000, 500)], [(-30, -30), (300, -30), (300, 300)]], ["wall", "fence"])
        def fail(xy):
            self.fail("irrelevant geometry sampled")
        self.assertEqual(barriers.tile_meshes(runs(f), (0, 0, 256, 256), fail), {})

    def test_huge_crossing_piece_samples_only_local_geometry(self):
        f = infra([[(-10000, 50), (10000, 50)]], ["wall"])
        asked = []
        def sample(xy):
            asked.extend(xy.tolist())
            return ground(xy)
        self.assertIn("boundary", barriers.tile_meshes(runs(f), (0, 0, 256, 256), sample))
        self.assertLess(len(asked), 300)
        self.assertTrue(np.all((np.asarray(asked)[:, 0] >= -1e-6) & (np.asarray(asked)[:, 0] <= 256.000001)))

    def test_shared_tile_edge_follows_same_sloped_ground(self):
        f = infra([[(10, 50), (500, 50)]], ["wall"])
        def slope(xy):
            return xy[:, 0] * 0.05 + 10
        r = runs(f)
        left = barriers.tile_meshes(r, (0, 0, 256, 256), slope)["boundary"][0][0]
        right = barriers.tile_meshes(r, (256, 0, 512, 256), slope)["boundary"][0][0] + [256, 0, 0]
        a = np.unique(np.round(left[np.isclose(left[:, 0], 256)], 5), axis=0)
        b = np.unique(np.round(right[np.isclose(right[:, 0], 256)], 5), axis=0)
        np.testing.assert_array_equal(a, b)

    def test_no_rank_thinning_and_hedges_have_separate_meshes(self):
        f = infra([[(10, 10), (90, 10)], [(10, 20), (90, 20)]], ["fence", "hedge"])
        a = barriers.tile_meshes(runs(f), (0, 0, 256, 256), ground)
        b = barriers.tile_meshes(runs(f), (0, 0, 256, 256), ground)
        self.assertEqual(set(a), {"boundary", "hedge"})
        for key in a:
            np.testing.assert_array_equal(a[key][0][0], b[key][0][0])

    def test_procedural_fence_suppression_is_local_and_preserves_gate_gap(self):
        f = infra([[(10, 50), (150, 50)], [(80, 50)]], ["wall", "gate"])
        props = [["fence", 80, 50, 10, 0], ["fence", 80, 150, 10, 0], ["door", 80, 50, 10, 0]]
        barriers.suppress_fences(props, runs(f))
        self.assertEqual([r[0] for r in props], ["fence", "door"])
        self.assertEqual(props[0][2], 150)

    def test_mesh_batches_stay_under_16_bit_indices(self):
        f = infra([[(10, 10), (90, 10)]], ["wall"])
        with patch.object(barriers, "MAX_VERTS", 120):
            meshes = barriers.tile_meshes(runs(f), (0, 0, 256, 256), ground)
        self.assertGreater(len(meshes), 1)
        for mesh, _ in meshes.values():
            self.assertLessEqual(len(mesh[0]), 120)
            self.assertLess(mesh[1].max(), len(mesh[0]))

    def test_boundary_only_tile_roundtrip_and_collision_boxes(self):
        f = infra([[(10, 10), (90, 10)], [(10, 20), (90, 20)]], ["wall", "hedge"])
        bm = barriers.tile_meshes(runs(f), (0, 0, 256, 256), ground)
        with tempfile.TemporaryDirectory() as d:
            roads.write_tile(d, "/temp/test", 1, 0, 0, [], boundary_meshes=bm)
            with open(os.path.join(d, "roads_1_0_0.json")) as stream:
                data = json.load(stream)
            self.assertIn("boundary", data["colliders"])
            self.assertNotIn("hedge", data["colliders"])
            for key in ("boundary", "hedge"):
                self.assertTrue(os.path.isfile(os.path.join(d, roads.shape_files(1, 0, 0)[key])))

    def test_ground_carriageway_stays_clear_without_removing_verge_fences(self):
        f = infra([[(10, 50), (150, 50)], [(75, 10), (75, 150)]], ["wall", "fence"])
        road = [(np.array([[80., 0], [80., 200]]), 3, "residential", False)]
        mm = barriers.tile_meshes(runs(f), (0, 0, 256, 256), ground, road)["boundary"][0]
        p, tri = mm[:2]
        self.assertFalse(np.any((p[tri, 0].min(1) < 80) & (p[tri, 0].max(1) > 80)))
        self.assertTrue(np.any(p[:, 1] > 100), "parallel fence outside the carriageway remains")

    def test_entire_boundary_inside_carriageway_needs_no_dem(self):
        f = infra([[(10, 50), (150, 50)]], ["wall"])
        def fail(xy):
            self.fail("clashing geometry sampled")
        road = [(np.array([[0., 50], [200., 50]]), 3, "residential", False)]
        self.assertEqual(barriers.tile_meshes(runs(f), (0, 0, 256, 256), fail, road), {})




class CurrentPipelineTest(unittest.TestCase):
    def test_construction_styles_and_explicit_combinations(self):
        kinds = ["wire", "barbed_wire", "electric", "picket", "panel", "concrete"]
        f = infra([[(10, 10 + n * 10), (90, 10 + n * 10)] for n in range(len(kinds))],
                  ["fence"] * len(kinds), [{"fence_type": k} for k in kinds])
        planned = runs(f)
        self.assertEqual([r.style for r in planned], ["wire", "barbed", "electric", "picket", "panel", "panel"])
        mesh = barriers.tile_meshes(planned, (0, 0, 256, 256), ground)["boundary"][0]
        self.assertTrue(np.isfinite(mesh[0]).all())
        self.assertIn("m_gm_concrete_plain", mesh[3], "electric insulators use an existing material")
        plain = infra([[(10, 10), (90, 10)]], ["fence"], [{"fence_type": "wire"}])
        mixed = infra([[(10, 10), (90, 10)]], ["fence"],
                      [{"fence_type": "wire", "barrier:barbed_wire": "yes", "barrier:electric": "yes"}])
        mixed_runs = runs(mixed)
        self.assertTrue(mixed_runs[0].barbed and mixed_runs[0].electric)
        original = barriers.tile_meshes(runs(plain), (0, 0, 256, 256), ground)["boundary"][0]
        combined = barriers.tile_meshes(mixed_runs, (0, 0, 256, 256), ground)["boundary"][0]
        self.assertGreater(len(combined[1]), len(original[1]))

    def test_path_crossing_alone_does_not_invent_a_gate(self):
        f = infra([[(10, 50), (90, 50)]], ["fence"])
        planned = runs(f)
        a = barriers.tile_meshes(planned, (0, 0, 256, 256), ground)["boundary"][0]
        for cls in ("cycleway", "footway", "path", "steps", "bridleway", "pedestrian"):
            with self.subTest(cls=cls):
                path = [(np.array([[50., 0.], [50., 100.]]), 1.25, cls)]
                b = barriers.tile_meshes(planned, (0, 0, 256, 256), ground, path)["boundary"][0]
                for before, after in zip(a, b):
                    np.testing.assert_array_equal(before, after)

    def test_vehicle_classes_and_legacy_footprints_keep_clearance(self):
        f = infra([[(45, 20), (45, 80)]], ["fence"])
        planned = runs(f)
        xy = np.array([[50., 0.], [50., 100.]])
        for cls in (*roads.STYLE, None, ""):
            with self.subTest(cls=cls):
                elev = Mock(side_effect=AssertionError("cleared boundary needs no elevation"))
                self.assertEqual(barriers.tile_meshes(planned, (0, 0, 256, 256), elev, [(xy, 6., cls)]), {})
                elev.assert_not_called()
        self.assertEqual(barriers.tile_meshes(planned, (0, 0, 256, 256), ground, [(xy, 6.)]), {})

    def test_optional_mesh_failure_preserves_existing_props(self):
        f = infra([[(10, 10), (90, 10)]], ["fence"])
        props = [["fence", 50., 10., 10., 0., 1.]]
        original = [row[:] for row in props]
        with patch.object(barriers, "load", return_value=runs(f)), patch.object(
                barriers, "tile_meshes", side_effect=ValueError("budget")):
            meshes, clearance = barriers.tile(FRAME, 0, 0, None, ground, [], props)
        self.assertEqual(meshes, {})
        self.assertIsNone(clearance)
        self.assertEqual(props, original)

    def test_mapped_stock_width_uses_one_dimensioned_mesh_and_keeps_gates(self):
        import fences
        for cls, tags, material in (("wall", {"wall": "noise_barrier"}, barriers.WALL_NEUTRAL),
                                     ("fence", {"fence_type": "chain-link"}, "bne_barrier_mesh")):
            with self.subTest(cls=cls):
                f = infra([[(10, 50), (90, 50)], [(50, 50)]], [cls, "gate"],
                          [dict(tags, height="2.2", width="0.3"), {"width": "4"}])
                planned = runs(f)
                self.assertFalse(planned[0].existing)
                self.assertEqual((planned[0].height, planned[0].width), (2.2, .3))
                meshes = barriers.tile_meshes(planned, (0, 0, 100, 100), ground)
                self.assertTrue(meshes)
                vertices = np.concatenate([m[0][0] for m in meshes.values()])
                self.assertAlmostEqual(vertices[:, 2].max(), 12.2, places=6)
                self.assertGreaterEqual(vertices[:, 1].min(), planned[0].xy[:, 1].min() - .150001)
                self.assertLessEqual(vertices[:, 1].max(), planned[0].xy[:, 1].max() + .150001)
                self.assertTrue(any(material in m[0][3] for m in meshes.values()))
                rows = fences.rows(FRAME, lambda key: f, (0, 0, 100, 100),
                    lambda la, lo: np.full(np.shape(la), 10.), gate_clearance=barriers.stock_clearance(planned))
                self.assertEqual(rows, [], "stock and procedural owners must not overlap")
                self.assertTrue(barriers.stock_clearance(planned)(planned[0].fid, np.array([50., 50.]), 4., True))

    def test_invalid_stock_width_keeps_existing_renderer(self):
        for width in ("-1", "nan", "0", "garbage", "10"):
            f = infra([[(10, 50), (90, 50)]], ["wall"], [{"wall": "noise_barrier", "width": width}])
            self.assertTrue(runs(f)[0].existing)

    def test_existing_stock_fences_are_not_drawn_twice_and_mapped_gates_clear_panels(self):
        import fences
        f = infra([[(10, 10), (90, 10)], [(50, 10)]], ["fence", "gate"],
                  [{"fence_type": "chain_link"}, {}])
        planned = runs(f)
        self.assertTrue(planned[0].existing)
        self.assertEqual(barriers.tile_meshes(planned, (0, 0, 256, 256), ground), {})
        elev = lambda lat, lon: np.full(np.shape(lat), 10.)
        ordinary = fences.rows(FRAME, lambda key: f, (0, 0, 256, 256), elev)
        filtered = fences.rows(FRAME, lambda key: f, (0, 0, 256, 256), elev,
                               gate_clearance=barriers.stock_clearance(planned))
        self.assertLess(len(filtered), len(ordinary))
        for row in filtered:
            centre = row[1] - fences.CHAIN_OX
            self.assertGreaterEqual(abs(centre - 50), barriers.GATE_WIDTH_M / 2 + fences.CHAIN_LEN / 2 - .02)

    def test_no_gate_keeps_stock_output_and_deck_hints_are_spared(self):
        import fences
        f = infra([[(10, 10), (90, 10)]], ["wall"], [{"wall": "noise_barrier"}])
        planned = runs(f)
        self.assertTrue(planned[0].existing)
        self.assertIsNone(barriers.stock_clearance(planned))
        self.assertEqual(barriers.tile_meshes(planned, (0, 0, 256, 256), ground), {})
        planned[0].gaps = ((20., 30., "gate"),)
        self.assertTrue(barriers.stock_clearance(planned)(planned[0].fid, planned[0].xy[0], 4., True))

    def test_new_boundary_shapes_do_not_replace_highway_barriers(self):
        f = infra([[(10, 10), (90, 10)]], ["wall"])
        meshes = barriers.tile_meshes(runs(f), (0, 0, 256, 256), ground)
        with tempfile.TemporaryDirectory() as folder:
            roads.write_tile(folder, "/temp/test", 1, 0, 0, [],
                             barrier_mesh=meshes["boundary"][0], boundary_meshes=meshes)
            data = json.loads(Path(folder, "roads_1_0_0.json").read_text())
            self.assertNotEqual(data["barrier"]["shape"], data["boundary"]["shape"])
            self.assertIn("boundary", data["colliders"])
            self.assertIn("barrier", data["colliders"])


if __name__ == "__main__":
    unittest.main()
