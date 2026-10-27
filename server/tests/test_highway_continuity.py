"""Highway counts agree across cached copies and unambiguous through-road gaps."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
from test_roads import features, rules, FRAME, flat
import grading, mapcells, mapdata, mapgeom, navgraph, netfetch, osmtags, roadlanes, roads, revisit


def chain(counts=(3, None, None, 3), reverse=False, **kwargs):
    lines = [[(i * 80., 20.), ((i + 1) * 80., 20.)] for i in range(len(counts))]
    if reverse:
        lines[1].reverse()
    f = features(lines, ['motorway'] * len(lines), **kwargs)
    f.rules['oneway'] = rules([(i, -1 if reverse and i == 1 else 1) for i in range(len(lines))], 'float')
    f.attrs['lane_count'] = np.array([np.nan if c is None else c for c in counts])
    return f


def fixture():
    rows = json.loads((Path(__file__).parent / 'fixtures/plum_springs_lanes.json').read_text())['roads']
    out = []
    for r in rows:
        f = mapgeom.build('roads', np.array([r['id']]), [(mapgeom.LINE, [[np.array(r['line'])]])])
        f.attrs = {k: np.array([np.nan if v is None else v]) for k, v in r['attrs'].items()}
        f.rules = {k: mapgeom.rule_table([(0, v, (a, b)) for v, a, b in entries],
                                       'float' if isinstance(entries[0][0], (float, int)) else 'text')
                   for k, entries in r['rules'].items()}
        out.append((mapcells.parse(r['cell']), f))
    return out


class HighwayContinuityTest(unittest.TestCase):
    def test_missing_chain_and_old_connected_guess_keep_three_lanes(self):
        for reverse in (False, True):
            for middle in (None, 2):
                f = chain((3, middle, middle, 3), reverse)
                f.attrs['lane_source'] = np.array(['osm-way', 'connected', 'connected', 'osm-way'])
                self.assertEqual(roadlanes.generated_counts(f), [3] * 4)
                self.assertTrue(all(sp.lanes == 3 for sp in roads.road_specs(f)))
                self.assertTrue(all(roadlanes.count(c) == middle for c in f.attrs['lane_count'][1:3]))

    def test_real_drops_explicit_counts_and_open_ends_are_preserved(self):
        for counts in ((3, 2, 3), (3, None, 2), (3, None), (None, 3), (2, None, 2)):
            self.assertEqual(roadlanes.generated_counts(chain(counts)), list(counts))

    def test_no_borrowing_from_ramps_wrong_direction_forks_or_different_connectors(self):
        for mode in ('ramp', 'opposing', 'connector', 'class', 'roundabout', 'two-way', 'fork'):
            f = chain((3, None, 3))
            if mode == 'ramp':
                f.rules['subclass'] = rules([(2, 'link')])
            elif mode == 'opposing':
                f.rules['oneway'] = rules([(0, 1), (1, 1), (2, -1)], 'float')
            elif mode == 'connector':
                f.rules['connectors'] = rules([(1, 'a', 1, 1), (2, 'b', 0, 0)])
            elif mode == 'class':
                f.attrs['class'][2] = 'trunk'
            elif mode == 'roundabout':
                f.attrs['osm_junction'] = np.array(['', 'roundabout', ''])
            elif mode == 'two-way':
                f.rules['oneway'] = rules([(0, 1), (2, 1)], 'float')
            else:
                f = chain((3, None, 3, 3))
                f.lonlat[-2:] = features([[(160, 20), (240, 25)]], ['motorway']).lonlat
            self.assertIsNone(roadlanes.generated_counts(f)[1], mode)

    def test_bridge_tunnel_markings_width_grading_and_ai_agree(self):
        for flag, specs in (('is_bridge', roads.bridge_specs), ('is_tunnel', roads.tunnel_specs)):
            f = chain((3, None, 3), flags=[(1, flag)])
            sp = specs(f)[1]
            self.assertEqual(sp.lanes, 3)
            self.assertEqual(sp.width, roads.road_specs(f)[0].width)
        f = chain((3, None, 3))
        sp = roads.road_specs(f)[1]
        p = np.array([[80., 20., 10.], [160., 20., 10.]])
        paint = roads.extras(sp, p, -86.)
        self.assertEqual(sum('dash' in look for look, _, _ in paint), 2)
        self.assertAlmostEqual(grading._plan(p[:, :2], p[:, 2], sp).road_half, sp.width / 2)
        graph = navgraph.build(FRAME, [f], flat, (120., 20.))
        self.assertTrue(graph['edges'])
        self.assertTrue(all((e[3], e[4]) == (3, 0) for e in graph['edges']))

    def test_plum_springs_copies_match_using_read_only_cached_evidence(self):
        (key, f), (_, tagged) = fixture()
        original = f.attrs['lane_count'].copy()
        table = {str(tagged.ids[0]): (str(tagged.attrs['src_record'][0]), 3)}
        f._cached_lane_counts, pending = roadlanes.cached_counts(f, key, lambda _: table)
        self.assertFalse(pending)
        self.assertEqual(roads.road_specs(f)[0].lanes, 3)
        self.assertEqual(roads.road_specs(f)[0].width, roads.road_specs(tagged)[0].width)
        np.testing.assert_array_equal(f.attrs['lane_count'], original)

    def test_context_requires_exact_id_revision_and_direct_tags(self):
        for mode in ('id', 'revision', 'conflict', 'explicit'):
            (key, f), (_, tagged) = fixture()
            fid, record = str(tagged.ids[0]), str(tagged.attrs['src_record'][0])
            if mode == 'explicit':
                f.attrs['lane_count'][0] = 2
                f.attrs['lane_source'][0] = 'osm-way'
            calls = []
            def read(other):
                calls.append(other)
                return {fid + ('x' if mode == 'id' else ''):
                        (record + ('x' if mode == 'revision' else ''),
                         2 if mode == 'conflict' and len(calls) == 1 else 3)}
            overrides, _ = roadlanes.cached_counts(f, key, read)
            self.assertEqual(overrides, {}, mode)

    def test_mapdata_integration_retries_context_without_network_or_raw_rewrite(self):
        (key, f), (owner, tagged) = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            md = mapdata.MapData(tmp, fetcher=netfetch.Fetcher(offline=True))
            self.addCleanup(md.close)
            def save(k, features):
                arrays = features.to_arrays('')
                arrays['provenance'] = np.array('null')
                md._store(md.path(k, 'roads'), arrays)
            save(key, f)
            original = Path(md.path(key, 'roads')).read_bytes()
            with patch.object(md, '_build', side_effect=AssertionError('network/build')):
                before = revisit.faults()
                first = md.cell(key, layers=('roads',)).layers['roads']
                self.assertTrue(first._lane_context_pending)
                self.assertGreater(revisit.faults(), before)
                self.assertNotIn('_bnge_cell', first.__dict__)
                save(owner, tagged)
                md._mem_retry[str(key), 'roads'] = time.time() - 1
                after = md.cell(key, layers=('roads',)).layers['roads']
                self.assertFalse(after._lane_context_pending)
                self.assertEqual(roads.road_specs(after)[0].lanes, 3)
                self.assertIn('_bnge_cell', after.__dict__)
                self.assertEqual(Path(md.path(key, 'roads')).read_bytes(), original)

    def test_fresh_osm_tags_clear_old_context(self):
        f = chain((3, None, 3))
        f.attrs['src_record'] = np.array(['w1@1', 'w2@1', 'w3@1'])
        f._cached_lane_counts = {1: 3}
        self.assertEqual(roadlanes.generated_counts(f)[1], 3)
        osmtags.join(f, {1: {'lanes': 3}, 2: {'lanes': 2}, 3: {'lanes': 3}})
        self.assertEqual(roadlanes.generated_counts(f)[1], 2)

    def test_cached_table_ignores_inferred_incomplete_and_corrupt_data(self):
        (key, f), _ = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            md = mapdata.MapData(tmp, fetcher=netfetch.Fetcher(offline=True))
            self.addCleanup(md.close)
            f.attrs['lane_count'][0] = 3
            f.attrs['lane_source'] = np.array(['connected'])
            arrays = f.to_arrays('')
            md._store(md.path(key, 'roads'), arrays)
            self.assertEqual(md._cached_road_lanes(key), {})
            arrays['_osm_retry_at'] = np.array(time.time() + 30)
            md._store(md.path(key, 'roads'), arrays)
            self.assertIsNone(md._cached_road_lanes(key))
            Path(md.path(key, 'roads')).write_bytes(b'not an archive')
            self.assertIsNone(md._cached_road_lanes(key))

    def test_context_read_budget_is_bounded_for_large_cells(self):
        n = 50
        f = chain([None] * n)
        f.attrs['src_record'] = np.array([f'w{i}@1' for i in range(n)])
        # Long features with separate owner/end cells, all read through a spy.
        for i in range(n):
            f.lonlat[2*i:2*i+2] = [(-100 + i*.1, 40), (-99.95 + i*.1, 40)]
        calls = []
        def read(key):
            calls.append(key)
            return {}
        _, pending = roadlanes.cached_counts(f, mapcells.cell_of(40, -100), read)
        self.assertLessEqual(len(calls), 16)
        self.assertTrue(pending)


if __name__ == '__main__':
    unittest.main()
