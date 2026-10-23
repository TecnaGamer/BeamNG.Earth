"""Cell-independent highway curves, lane profiles and ramp approach ownership."""
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import mapgeom
import roadcontext
import roads
from test_roads import FRAME, features, rules
from test_auxiliary_markings import interchange


def subset(feats, indices):
    out = mapgeom.build('roads', feats.ids[indices], [(int(feats.kind[i]), feats.parts(i)) for i in indices])
    out.attrs = {k: v[indices].copy() for k, v in feats.attrs.items()}
    for name, table in feats.rules.items():
        rows = np.concatenate([table.rows(i) for i in indices])
        owner = np.concatenate([np.full(len(table.rows(i)), j, np.int32) for j, i in enumerate(indices)])
        out.rules[name] = mapgeom.RuleTable(owner, table.value[rows].copy(),
                                           table.start[rows].copy(), table.end[rows].copy())
    return out


def neighbourhood():
    return features([[(-160, -35), (0, 0)], [(0, 0), (140, 0), (270, 35), (430, 35)],
                     [(430, 35), (600, 70)]], ['motorway']*3, ids=['before', 'shared', 'after'])


class RoadContextTest(unittest.TestCase):
    def test_same_source_has_one_curve_and_parameterisation_across_partial_cells(self):
        full = neighbourhood()
        a, b = subset(full, [0, 1]), subset(full, [1, 2])
        raw_a, raw_b = roads.geometry_parts(a, 1)[0][0], roads.geometry_parts(b, 0)[0][0]
        self.assertFalse(raw_a.shape == raw_b.shape and np.allclose(raw_a, raw_b, rtol=0., atol=1e-12),
                         'fixture must exercise the old cell-dependent curve')
        a, b = subset(full, [0, 1]), subset(full, [1, 2])
        context = roadcontext.Context(lambda key: full)
        a._road_context = b._road_context = context
        # Concurrent builds and opposite loading order must produce the same source samples.
        with ThreadPoolExecutor(2) as pool:
            ca = pool.submit(roads.geometry_parts, a, 1)
            cb = pool.submit(roads.geometry_parts, b, 0)
            np.testing.assert_array_equal(ca.result()[0][0], cb.result()[0][0])
        np.testing.assert_array_equal(a._geometry_params[1, 0, 0], b._geometry_params[0, 0, 0])
        for points in (roads.ground_parts(a, 1)[0], roads.ground_parts(b, 0)[0]):
            self.assertTrue(np.isfinite(points).all())
        np.testing.assert_array_equal(roads.ground_parts(a, 1)[0], roads.ground_parts(b, 0)[0])

    def test_lane_change_profile_does_not_restart_at_a_map_cell_boundary(self):
        full = neighbourhood()
        full.rules['oneway'] = rules([(i, 1.) for i in range(3)], 'float')
        full.rules['lanes_forward'] = rules([(0, 2), (1, 2), (2, 3)], 'float')
        full.rules['lanes_backward'] = rules([(i, 0) for i in range(3)], 'float')
        a, b = subset(full, [0, 1]), subset(full, [1, 2])
        self.assertNotEqual(roads.road_specs(a)[1].lane_model, roads.road_specs(b)[0].lane_model)
        a, b = subset(full, [0, 1]), subset(full, [1, 2])
        a._road_context = b._road_context = roadcontext.Context(lambda key: full)
        self.assertEqual(roads.road_specs(a)[1], roads.road_specs(b)[0])

    def test_ramp_approach_is_identical_when_upstream_features_are_missing_from_one_cell(self):
        full = interchange()
        a, b = subset(full, [0, 1, 2, 3]), subset(full, [1, 2, 3])
        a._road_context = b._road_context = roadcontext.Context(lambda key: full)
        results = [roads.projected_ramp_layout(FRAME, [f]) for f in (a, b)]
        for field in ('pts', 'outer', 'guide', 'widths'):
            np.testing.assert_allclose(results[0][3][0][field], results[1][3][0][field], atol=1e-8)
        np.testing.assert_allclose(roads.auxiliary_parts(a, 3)[0][0],
                                   roads.auxiliary_parts(b, 2)[0][0], atol=1e-12)

    def test_merge_preserves_directional_rules_and_deduplicates_whole_features(self):
        full = neighbourhood()
        full.rules['oneway'] = rules([(0, -1.), (1, 1.), (2, 1.)], 'float')
        full.attrs['optional'] = np.array(['a', 'b', 'c'])
        a, b = subset(full, [0, 1]), subset(full, [1, 2])
        del b.attrs['optional']
        merged = roadcontext.merge([a, b])
        self.assertEqual(list(merged.ids), ['after', 'before', 'shared'])
        self.assertEqual(merged.attrs['optional'].tolist(), ['', 'a', 'b'])
        for fid in merged.ids:
            old, new = list(full.ids).index(fid), merged.index(fid)
            np.testing.assert_array_equal(merged.parts(new)[0][0], full.parts(old)[0][0])
            self.assertEqual(merged.rules['oneway'].for_feature(new), full.rules['oneway'].for_feature(old))
        self.assertNotIn('_road_context', merged.__dict__)

    def test_long_sparse_source_queries_its_vertices_instead_of_a_large_bbox(self):
        full = features([[(0, 0), (27000, 27000)]], ['track'])
        calls = []
        def cell(key):
            calls.append(key)
            return full
        context = roadcontext.Context(cell)
        context.feature(full, 0)
        self.assertLessEqual(len(calls), 8)
        n = len(calls)
        context.feature(full, 0)
        self.assertEqual(len(calls), n, 'reuse a complete neighbourhood')

    def test_failed_neighbour_fetch_is_retried_without_caching_an_incomplete_fit(self):
        full = neighbourhood()
        attempts = []
        def cell(key):
            attempts.append(key)
            if len(attempts) == 1:
                raise OSError('temporary cell read failure')
            return full
        context = roadcontext.Context(cell)
        with self.assertRaises(OSError):
            context.feature(full, 1)
        self.assertFalse(context.cache)
        source, row = context.feature(full, 1)
        self.assertEqual(source.ids[row], 'shared')

    def test_neighbourhood_cache_evicts_old_entries(self):
        full = neighbourhood()
        context = roadcontext.Context(lambda key: full)
        with patch.object(roadcontext, 'MAX_CONTEXTS', 1):
            for dx in (0., 5.):
                context.around(full.lonlat+[dx, 0.])
        self.assertEqual(len(context.cache), 1)


if __name__ == '__main__':
    unittest.main()
