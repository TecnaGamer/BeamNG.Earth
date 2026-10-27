"""Two-lane through roads must not acquire isolated shared-lane sections."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from test_roads import features, rules, FRAME, flat
from test_rural_roads import rural, urban
import roads, roadlanes, osmtags, navgraph, grading, surface, mapgeom


def chain(counts=(2, 1, 1, 2), reverse=False, **kwargs):
    lines = [[(i * 80., 20.), ((i + 1) * 80., 20.)] for i in range(len(counts))]
    if reverse:
        lines[1].reverse()
    f = features(lines, ['secondary'] * len(lines), **kwargs)
    f.attrs['lane_count'] = np.array([np.nan if c is None else c for c in counts])
    return f


class RuralContinuityTest(unittest.TestCase):
    def test_two_ended_chain_is_wide_and_keeps_source_evidence(self):
        for reverse in (False, True):
            for cls in sorted(roads.RURAL_LANE_CLASSES):
                f = chain(reverse=reverse)
                f.attrs['class'] = np.full(4, cls)
                for left in (False, True):
                    specs = roads.road_specs(f, rural, left_hand=left)
                    self.assertTrue(all((s.width, s.lanes) == (11., 2) for s in specs), cls)
                np.testing.assert_array_equal(f.attrs['lane_count'], [2, 1, 1, 2])

    def test_missing_count_narrow_section_is_repaired_only_with_both_ends(self):
        f = chain((2, None, 2), widths=[(1, 4.)])
        sp = roads.road_specs(f, rural)[1]
        self.assertEqual((sp.width, sp.lanes), (11., 2))
        f = chain((2, None), widths=[(1, 4.)])
        sp = roads.road_specs(f, rural)[1]
        self.assertEqual((sp.width, sp.lanes), (4., None))

    def test_dead_end_conflicting_count_and_loop_remain_single_lane(self):
        for counts in ((2, 1), (1, 2), (2, 1, 3), (1, 1, 1)):
            specs = roads.road_specs(chain(counts), rural)
            self.assertEqual([sp.lanes for sp in specs], list(counts))
        f = features([[(0, 0), (80, 0), (80, 80), (0, 80), (0, 0)]], ['secondary'])
        f.attrs['lane_count'] = np.array([1.])
        self.assertEqual(roads.road_specs(f, rural)[0].lanes, 1)

    def test_side_roads_do_not_supply_evidence_and_ambiguous_forks_are_rejected(self):
        for mode in ('side', 'fork', 'connector', 'class'):
            f = chain((2, 1, 2))
            if mode == 'side':
                f.lonlat[-2:] = features([[(160, 20), (160, 100)]], ['secondary']).lonlat
            elif mode == 'fork':
                f = features([[(-80, 20), (0, 20)], [(0, 20), (80, 20)],
                              [(80, 20), (160, 20)], [(80, 20), (160, 25)]], ['secondary'] * 4)
                f.attrs['lane_count'] = np.array([2, 1, 2, 2])
            elif mode == 'connector':
                f.rules['connectors'] = rules([(0, 'a', 1, 1), (1, 'b', 0, 0)])
            else:
                f.attrs['class'][2] = 'primary'
            self.assertEqual(roads.road_specs(f, rural)[1].lanes, 1, mode)

    def test_crossroad_does_not_interrupt_unique_through_continuation(self):
        f = features([[(-80, 20), (0, 20)], [(0, 20), (80, 20)], [(80, 20), (160, 20)],
                      [(0, -80), (0, 20)], [(80, 20), (80, 100)]], ['secondary'] * 5)
        f.attrs['lane_count'] = np.array([2, 1, 2, 1, 1])
        self.assertEqual([sp.lanes for sp in roads.road_specs(f, rural)], [2, 2, 2, 1, 1])

    def test_urban_oneway_ramp_and_unpaved_sections_are_unchanged(self):
        for mode in ('urban', 'mapped-urban', 'oneway', 'ramp', 'dirt', 'roundabout'):
            f = chain((2, 1, 2), widths=[(1, 6.)] if mode == 'mapped-urban' else [])
            if mode == 'oneway':
                f.rules['oneway'] = rules([(1, 1)], 'float')
            if mode == 'ramp':
                f.rules['subclass'] = rules([(1, 'link')])
            if mode == 'dirt':
                f.rules['surface'] = rules([(1, 'dirt')])
            if mode == 'roundabout':
                f.attrs['osm_junction'] = np.array(['', 'roundabout', ''])
            self.assertEqual(roads.road_specs(f, urban if 'urban' in mode else rural)[1].lanes, 1, mode)

    def test_new_enrichment_requires_both_ends_before_inheriting_one_lane(self):
        for ends, expected in (((1, None, None), [1, None, None]),
                               ((1, None, 1), [1, 1, 1]), ((2, None, 2), [2, 2, 2])):
            f = chain(ends)
            f.attrs['src_record'] = np.array(['w1@1', 'w2@1', 'w3@1'])
            osmtags.join(f, {i + 1: {'lanes': c} for i, c in enumerate(ends) if c is not None})
            actual = [roadlanes.count(c) for c in f.attrs['lane_count']]
            self.assertEqual(actual, expected)

    def test_old_cached_inference_is_rechecked_without_erasing_explicit_tags(self):
        for both in (False, True):
            f = chain((1, 1, 1) if both else (1, 1, 1, 1))
            f.attrs['lane_source'] = np.array(['osm-way', 'connected', 'osm-way'] if both else
                                            ['osm-way', 'connected', 'connected', 'connected'])
            self.assertEqual(roadlanes.generated_counts(f), [1, 1, 1] if both else [1, None, None, None])
            np.testing.assert_array_equal(f.attrs['lane_count'], np.ones(len(f)))
        f.attrs['src_record'] = np.array([f'w{i + 1}@1' for i in range(len(f))])
        osmtags.join(f, {i + 1: {'lanes': 2} for i in range(len(f))})
        self.assertEqual(roadlanes.generated_counts(f), [2] * len(f))

    def test_paint_mesh_grading_surface_and_ai_use_repaired_profile(self):
        f = chain((2, 1, 2))
        sp = roads.road_specs(f, rural, widths=lambda *_: (6., 1.))[1]
        self.assertEqual((sp.width, sp.lanes), (11., 2))
        p = np.array([[80., 20., 10.], [160., 20., 10.]])
        paint = roads.extras(sp, p, -86.)
        self.assertEqual(sum(look['m'] == roads.centre_colour(-86.) for look, _, _ in paint), 2)
        for look, _, line in paint:
            if 'edge_side' in look:
                np.testing.assert_allclose(np.abs(line[:, 1] - 20), 4.25)
        with patch.object(roads, 'ROADS_IN_GROUND', False):
            chunks = roads.build_tile(FRAME, 0, 0, [f], flat, urban_fn=rural)
        self.assertTrue(all(c['w'] == 11. for c in chunks if c['m'] == roads.LOOK['paved']['m']))
        graph = navgraph.build(FRAME, [f], flat, (120., 20.), urban_fn=rural)
        self.assertTrue(graph['edges'])
        self.assertTrue(all((e[3], e[4], e[7]) == (1, 1, 4.25) for e in graph['edges']))
        self.assertGreaterEqual(grading._plan(p[:, :2], p[:, 2], sp).road_half, 5.5)
        east, north = np.meshgrid(np.arange(100., 132., .25), np.arange(4., 36., .25))
        lat, lon, _ = FRAME.enu_to_geodetic(east, north, 0.)
        painted = surface.paint_roads(np.full(east.shape, surface.GRASS, np.uint8), lat, lon, [f], .25, urban_fn=rural)
        self.assertEqual(int(painted[84, 64]), surface.ASPHALT)
        self.assertNotEqual(int(painted[96, 64]), surface.ASPHALT)

    def test_single_lane_deck_between_two_lane_approaches_is_repaired(self):
        for flag, get_specs in (('is_bridge', roads.bridge_specs), ('is_tunnel', roads.tunnel_specs)):
            f = chain((2, 1, 2), flags=[(1, flag)])
            sp = get_specs(f)[1]
            self.assertEqual((sp.width, sp.lanes), (11., 2), flag)
        f = chain((2, 1, 2), flags=[(1, 'is_bridge', .2, .8)])
        self.assertEqual(roads.bridge_specs(f)[1], roads.road_specs(f)[1])

    def test_cached_chandlers_road_does_not_spread_distant_single_lane_tag_backwards(self):
        data = json.loads((Path(__file__).parent / 'fixtures/chandlers_road.json').read_text(encoding='utf-8'))
        rows = data['roads']
        f = mapgeom.build('roads', np.array([r['id'] for r in rows]),
                         [(mapgeom.LINE, [[np.array(r['line'])]]) for r in rows])
        for name in rows[0]['attrs']:
            f.attrs[name] = np.array([np.nan if r['attrs'][name] is None else r['attrs'][name] for r in rows])
        for name in sorted({name for r in rows for name in r['rules']}):
            entries = [(i, entry[0], entry[1:]) for i, r in enumerate(rows) for entry in r['rules'].get(name, ())]
            f.rules[name] = mapgeom.rule_table(entries, 'float' if isinstance(entries[0][1], (float, int)) else 'text')
        specs = roads.road_specs(f, rural)
        for i, source in enumerate(f.attrs['lane_source']):
            if source == 'connected':
                self.assertEqual((specs[i].width, specs[i].lanes), (11., 2))
            elif f.attrs['osm_lanes'][i] == 1:
                self.assertEqual(specs[i].lanes, 1, 'no second two-lane end in this source cell')


if __name__ == '__main__':
    unittest.main()
