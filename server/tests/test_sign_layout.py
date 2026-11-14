"""Duplicate OSM records, paired exit destinations and ownership across road tiles."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exitsigns
import geodesy
import highwaysigns
import roads
import signlayout
import signroads
import throughsigns

FRAME = geodesy.EnuFrame(42.7, -84.6)
BOX = (-2000., -300., 300., 300.)


def ll(x, y):
    la, lo, _ = FRAME.enu_to_geodetic(x, y, 0.)
    return [float(lo), float(la)]


def flat(la, lo):
    return np.zeros_like(la)


def fixture():
    rows = [('osm:w100:0', signroads.Spec(12., 'motorway', True, 1, False),
             np.array([[-2000., 0., 0.], [500., 0., 0.]]), 12.)]
    objects = []
    for wid, side, names, routes in ((201, -1, ['Lansing', 'Grand Rapids'], ['I 69 North', 'I 96 West']),
                                     (202, 1, ['Detroit'], ['I 96 East'])):
        line = np.array([[0., 0., 0.], [100., -side*120., 0.], [200., -side*200., 0.]])
        rows.append((f'osm:w{wid}:0', signroads.Spec(6., 'motorway', True, 1, True), line, 6.))
        objects.append(dict(id=-wid, exit=dict(label=dict(ref='72', names=names, routes=routes),
            side=side, approach=[ll(-1900., 0.), ll(0., 0.)], ramp=[ll(*p[:2]) for p in line],
            main_ids=[100], ramp_id=wid)))
    return objects, rows, SimpleNamespace(pavements=lambda *a: rows)


class LayoutTest(unittest.TestCase):
    def test_photo_destinations_use_one_gantry_per_station_with_correct_arrows(self):
        objects, rows, context = fixture()
        roadside, overheads = highwaysigns.guide_layout(FRAME, objects, context, BOX, flat)
        self.assertEqual(len(overheads), 4)
        self.assertTrue(all(s['kind'] == 'ramp' for s in roadside))
        self.assertEqual({s['distance'] for s in overheads}, {'1 MILE', '1/2 MILE', '1/4 MILE', ''})
        for s in overheads:
            self.assertEqual([p['label']['names'] for p in s['panels']], [['Lansing', 'Grand Rapids'], ['Detroit']])
            left, right = s['panels']
            self.assertLess(left['offset']+left['width']/2, right['offset']-right['width']/2)
            self.assertGreaterEqual(s['bottom']-s['centre'][2], 6.)
            axis = np.array([s['travel'][1], -s['travel'][0]])
            for pole in s['poles']:
                self.assertTrue(exitsigns._clear(np.asarray(s['centre'][:2])+axis*pole, .65, exitsigns._outlines(rows)))
        decision = next(s for s in overheads if not s['distance'])
        mesh = throughsigns.Mesh((0., 0., 0.))
        with mock.patch.object(mesh, 'arrow', wraps=mesh.arrow) as arrows:
            mesh.overhead(decision)
        self.assertEqual([c.args[2] for c in arrows.call_args_list], [True, False])
        self.assertTrue(np.isfinite(mesh.result()[0]).all())

    def test_duplicate_ids_and_alternate_way_records_cannot_duplicate_assemblies(self):
        objects, _, context = fixture()
        duplicate = copy.deepcopy(objects[0])
        duplicate['id'] = -999
        duplicate['exit']['ramp_id'] = 999
        duplicate['exit']['label']['names'] = [' LANSING ', 'Grand Rapids']
        wanted = highwaysigns.guide_layout(FRAME, objects, context, BOX, flat)
        actual = highwaysigns.guide_layout(FRAME, [duplicate]+objects+objects[::-1], context, BOX, flat)
        def shape(result):
            return sorted((s['kind'], s['distance'], tuple(s['centre'])) for stream in result for s in stream)
        self.assertEqual(shape(wanted), shape(actual))
        for stream in actual:
            for i, sign in enumerate(stream):
                self.assertFalse(any(signlayout.overlap(sign, other) for other in stream[i+1:]))

    def test_adjacent_tiles_have_exactly_the_same_assemblies_as_whole_layout(self):
        objects, _, context = fixture()
        whole = highwaysigns.guide_layout(FRAME, objects, context, BOX, flat)
        tiled = [[], []]
        for tx in range(-8, 2):
            for ty in (-1, 0):
                for i, stream in enumerate(highwaysigns.guide_layout(FRAME, objects[::-1], context,
                                              roads.tile_bbox(tx, ty), flat)):
                    tiled[i].extend(stream)
        for a, b in zip(whole, tiled):
            key = lambda s: (s['id'], s['distance'], tuple(s['centre']))
            self.assertEqual(sorted(map(key, a)), sorted(map(key, b)))

    def test_same_exit_number_on_other_carriageway_or_later_junction_is_preserved(self):
        objects, _, _ = fixture()
        later, opposite = copy.deepcopy(objects[0]), copy.deepcopy(objects[0])
        later['id'], opposite['id'] = -301, -401
        later['exit']['approach'] = [ll(-1300., 0.), ll(600., 0.)]
        opposite['exit']['approach'] = [ll(1900., 20.), ll(0., 20.)]
        groups = signlayout.groups(FRAME, objects+[later, opposite])
        self.assertEqual(sorted(map(len, groups)), [1, 1, 2])

    def test_failed_gantry_retains_distinct_nonoverlapping_roadside_destinations(self):
        objects, _, context = fixture()
        objects[0]['exit']['side'] = 1  # both records would occupy the right shoulder
        with mock.patch.object(throughsigns, '_assembly', return_value=None):
            roadside, overheads = highwaysigns.guide_layout(FRAME, objects, context, BOX, flat)
        self.assertEqual(overheads, [])
        decision = [s for s in roadside if s['kind'] == 'decision']
        self.assertEqual(len(decision), 2)
        self.assertEqual({tuple(s['label']['names']) for s in decision}, {('Lansing', 'Grand Rapids'), ('Detroit',)})
        for i, s in enumerate(roadside):
            self.assertFalse(any(signlayout.overlap(s, b) for b in roadside[i+1:]))

    def test_overlapping_through_and_exit_gantries_combine_distinct_boards(self):
        objects, rows, context = fixture()
        objects = objects[:1]
        objects[0]['exit']['through_label'] = dict(ref='', names=['Flint'], routes=['I 69 North'])
        x = -804.672
        for i, name in enumerate(('Flint', 'Saginaw')):
            objects.append(dict(id=-800-i, through=dict(main_ids=[100], point=ll(x, 0.),
                approach=[ll(x-80., 0.), ll(x+80., 0.)],
                label=dict(ref='', names=[name], routes=['I 69 North']))))
        _, overheads = highwaysigns.guide_layout(FRAME, objects, context, BOX, flat)
        self.assertEqual(len(overheads), 1)
        names = [p['label']['names'] for p in overheads[0]['panels']]
        self.assertEqual(names.count(['Flint']), 1)
        self.assertIn(['Saginaw'], names)
        self.assertIn(['Lansing', 'Grand Rapids'], names)


if __name__ == '__main__':
    unittest.main()
