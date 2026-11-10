"""French motorway decision gantries, honest labels and safe tile ownership."""
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exitsigns
import frmotorway_layout
import frroadmesh
import highwaysigns
import roads
import throughsigns
from test_sign_layout import FRAME, BOX, fixture, flat


class MainlineMetadataTest(unittest.TestCase):
    def test_exact_way_ids_from_real_parquet_cannot_take_a_neighbouring_route(self):
        import duckdb
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'roads.parquet'
            con = duckdb.connect()
            con.execute('CREATE TABLE roads(id BIGINT, highway VARCHAR, ref VARCHAR, '
                        'bbox STRUCT(xmin FLOAT,ymin FLOAT,xmax FLOAT,ymax FLOAT))')
            for wid, route in ((100, 'A4'), (101, 'A35')):
                con.execute('INSERT INTO roads VALUES (?,?,?,?)', [wid, 'motorway', route,
                    dict(xmin=3., ymin=48., xmax=3.1, ymax=48.1)])
            con.execute(f"COPY roads TO '{path.as_posix()}' (FORMAT PARQUET)")
            con.close()
            class Connection:
                def __init__(self): self.con = duckdb.connect()
                def execute(self, sql):
                    return self.con.execute(sql.replace("'test://osm/highways.parquet'", repr(path.as_posix())))
                def close(self): self.con.close()
            fetch = mock.Mock()
            tags = SimpleNamespace(_reader=SimpleNamespace(
                _connect=lambda *a: (Connection(), 'test', []), _fetch=fetch),
                _groups=lambda *a: {0: dict(cols={c: [] for c in ('id', 'bbox', 'highway', 'ref')})},
                _candidates=lambda *a: [0])
            got = frmotorway_layout.read_main_tags(tags, None, (2.9, 47.9, 3.2, 48.2), (100, 999))
            self.assertEqual(got, {100: dict(highway='motorway', ref='A4')})
            self.assertEqual(frmotorway_layout.read_main_tags(tags, None, (4., 48., 4.1, 48.1), (100,)), {})

    def test_old_shards_are_enriched_once_without_mutating_the_shared_source(self):
        source = frmotorway_layout.MainlineTags('unused', None)
        source.tags.version = lambda: ('test', None)
        objects, _, _ = french_fixture({})
        with mock.patch.object(frmotorway_layout, 'read_main_tags',
                return_value={100: dict(highway='motorway', ref='A4')}) as read:
            enriched = source.enrich(objects)
            again = source.enrich(objects)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(objects[0]['exit']['main_tags'], {})
        self.assertEqual(enriched, again)
        self.assertEqual(frmotorway_layout.through_content(enriched[0]['exit'])['names'], ['A4'])

    def test_conflicting_mainline_references_are_not_guessed(self):
        source = frmotorway_layout.MainlineTags('unused', None)
        source.tags.version = lambda: ('test', None)
        objects, _, _ = french_fixture({})
        with mock.patch.object(frmotorway_layout, 'read_main_tags', return_value={
                100: dict(highway='motorway', ref='A4'), 101: dict(highway='motorway', ref='A35')}):
            self.assertEqual(source.enrich(objects), objects)

    def test_offline_metadata_preserves_existing_signs_and_throttles_retries(self):
        source = frmotorway_layout.MainlineTags('unused', None)
        source.tags.version = mock.Mock(side_effect=OSError('offline'))
        objects, _, _ = french_fixture({})
        with self.assertLogs('earthd.highwaysigns', level='WARNING'):
            self.assertEqual(source.enrich(objects), objects)
            self.assertEqual(source.enrich(objects), objects)
        self.assertEqual(source.tags.version.call_count, 1)

    def test_changed_snapshot_is_retried_against_the_new_version(self):
        import osmtags
        source = frmotorway_layout.MainlineTags('unused', None)
        source.tags.version = mock.Mock(side_effect=[('old', None), ('new', None)])
        source.tags._forget = mock.Mock()
        objects, _, _ = french_fixture({})
        with mock.patch.object(frmotorway_layout, 'read_main_tags', side_effect=[
                osmtags.rangeread.Changed(), {100: dict(highway='motorway', ref='A4')}]):
            self.assertEqual(source.enrich(objects)[0]['exit']['main_tags']['ref'], 'A4')
        source.tags._forget.assert_called_once()


def french_fixture(tags=None):
    objects, rows, context = fixture()
    objects = objects[1:]
    objects[0]['exit'].update(main_tags=tags if tags is not None else
        dict(highway='motorway', oneway='yes', ref='A4', destination='Paris;Meaux-Centre'))
    objects[0]['exit']['label'] = dict(ref='18', names=['Meaux-Est', 'Trilport'], routes=[])
    return objects, rows, context


class FrenchMotorwayTest(unittest.TestCase):
    def layout(self, objects, context, **kw):
        return highwaysigns.guide_layout(FRAME, objects, context, BOX, flat, mph=False, french=True, **kw)

    def test_advance_and_decision_share_mainline_and_exit_boards_with_clear_columns(self):
        objects, rows, context = french_fixture()
        roadside, overheads = self.layout(objects, context)
        self.assertEqual({s['distance'] for s in overheads}, {'500 m', ''})
        self.assertTrue(all(s['kind'] == 'ramp' for s in roadside))
        for sign in overheads:
            main, exit_panel = sign['panels']
            self.assertFalse(main['exit'])
            self.assertEqual(main['label']['names'], ['Paris', 'Meaux-Centre'])
            self.assertTrue(exit_panel['exit'])
            self.assertEqual(exit_panel['label']['names'], ['Meaux-Est', 'Trilport'])
            self.assertGreaterEqual(sign['bottom'] - sign['centre'][2], 6.)
            right = np.array([sign['travel'][1], -sign['travel'][0]])
            for pole in sign['poles']:
                self.assertTrue(exitsigns._clear(np.asarray(sign['centre'][:2])+right*pole,
                                                .65, exitsigns._outlines(rows)))
        mesh = frroadmesh.build_highways(roadside, overheads, (0., 0., 0.))
        p, t, uv, materials, mi = mesh
        self.assertTrue(np.isfinite(p).all())
        self.assertEqual(uv.shape, (len(p), 2))
        self.assertLess(t.max(), len(p))
        self.assertLess(mi.max(), len(materials))
        self.assertIn('bne_sign_blue', materials)
        blue = materials.index('bne_sign_blue')
        self.assertTrue(np.any(mi == blue))
        self.assertGreater(p[t[mi == blue]].min(axis=(0, 1))[2], 5.9)

    def test_missing_mainline_destinations_use_only_current_motorway_reference(self):
        objects, _, context = french_fixture(dict(highway='motorway', oneway='yes', ref='A4'))
        roadside, overheads = self.layout(objects, context)
        self.assertEqual(len(overheads), 2)
        self.assertTrue(all(s['kind'] == 'ramp' for s in roadside))
        for s in overheads:
            self.assertEqual(s['panels'][0]['label']['names'], ['A4'])
            self.assertTrue(s['panels'][0]['label']['route_only'])
            self.assertNotIn('Meaux-Est', s['panels'][0]['label']['names'])
        # Neither the ramp nor its destinations describe the continuing road.
        self.assertIsNone(frmotorway_layout.through_content(dict(
            main_tags={'destination:ref': 'A35'}, ramp_tags={'ref': 'A355'},
            label={'names': ['Paris'], 'routes': ['A4']})))

    def test_no_mainline_information_retains_existing_roadside_signs(self):
        objects, _, context = french_fixture({})
        roadside, overheads = self.layout(objects, context)
        self.assertFalse(overheads)
        self.assertEqual({s['kind'] for s in roadside}, {'advance', 'decision', 'ramp'})

    def test_blue_exit_can_be_overhead_without_fabricating_a_mainline_board(self):
        objects, _, context = french_fixture({})
        objects[0]['exit']['label']['routes'] = ['A35']
        _, overheads = self.layout(objects, context)
        self.assertEqual(len(overheads), 2)
        self.assertTrue(all(len(s['panels']) == 1 and s['panels'][0]['exit'] for s in overheads))

    def test_failed_support_planning_keeps_the_roadside_information(self):
        objects, _, context = french_fixture()
        with mock.patch.object(throughsigns, '_assembly', return_value=None):
            roadside, overheads = self.layout(objects, context)
        self.assertFalse(overheads)
        self.assertEqual({s['kind'] for s in roadside}, {'advance', 'decision', 'ramp'})

    def test_decks_are_not_given_ground_based_gantry_columns(self):
        objects, _, context = french_fixture()
        deck = dict(deck=True, n=[[-2000., 0., 8.], [2000., 0., 8.]], w=20.)
        _, overheads = self.layout(objects, context, chunks=[deck])
        self.assertFalse(overheads)

    def test_other_country_layout_and_non_motorway_roads_are_unchanged(self):
        objects, _, context = french_fixture()
        roadside, overheads = highwaysigns.guide_layout(FRAME, objects, context, BOX, flat, mph=False)
        self.assertEqual(len(overheads), 1)
        self.assertTrue(any(s['kind'] == 'decision' for s in roadside))
        objects[0]['exit']['main_tags']['highway'] = 'trunk'
        roadside, overheads = self.layout(objects, context)
        self.assertEqual(len(overheads), 1)
        self.assertTrue(any(s['kind'] == 'decision' for s in roadside))

    def test_duplicates_and_adjacent_tiles_own_each_assembly_once(self):
        objects, _, context = french_fixture()
        whole = self.layout(objects, context)
        tiled = [[], []]
        for tx in range(-8, 2):
            for ty in (-1, 0):
                result = highwaysigns.guide_layout(FRAME, copy.deepcopy(objects)*2, context,
                    roads.tile_bbox(tx, ty), flat, mph=False, french=True)
                for a, b in zip(tiled, result):
                    a.extend(b)
        key = lambda s: (s['id'], s['kind'], s['distance'], tuple(s['centre']))
        for a, b in zip(whole, tiled):
            self.assertEqual(sorted(map(key, a)), sorted(map(key, b)))

    def test_two_branches_keep_one_mainline_board_and_both_exit_directions(self):
        objects, _, context = fixture()
        for obj in objects:
            obj['exit']['main_tags'] = dict(highway='motorway', oneway='yes', ref='A4', destination='Paris')
            obj['exit']['label']['routes'] = []
        roadside, overheads = self.layout(objects, context)
        self.assertEqual(len(overheads), 2)
        self.assertTrue(all(s['kind'] == 'ramp' for s in roadside))
        for sign in overheads:
            self.assertEqual(len([p for p in sign['panels'] if not p['exit']]), 1)
            self.assertEqual({p['side'] for p in sign['panels'] if p['exit']}, {-1, 1})
        sign = next(s for s in overheads if not s['distance'])
        with mock.patch.object(frroadmesh, 'guide_panel', wraps=frroadmesh.guide_panel) as draw:
            frroadmesh.build_highways([], [sign], (0., 0., 0.))
        self.assertEqual({c.args[-1] for c in draw.call_args_list if c.args[5]}, {-1, 1})


if __name__ == '__main__':
    unittest.main()
