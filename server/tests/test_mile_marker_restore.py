"""Integration contracts for restored mile markers and median scenery in 1.7.1."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd
import blockworker
import highwaysigns
import medianbarriers
import roads
import signstream
import geodesy
import milemarkers
import railtags
import signroads
import medians
from test_highway_signs import fixture, server
from test_ky_medians import FRAME, BOX, pair, marker, flat
from test_blockworker import FakeDem
from test_roads import LAT, LON
from test_highway_signs import FakeTags
from test_sign_tags import NotPublished
from test_medians import pair as native_pair


class MigrationTest(unittest.TestCase):
    def test_empty_or_unpublished_osm_dataset_still_builds_bundled_markers(self):
        ref = next(o for o in milemarkers.references()[0] if o['mile_marker']['state'] == 'MI'
                   and o['mile_marker']['route'] == 69 and o['mile_marker']['mile'] == 116
                   and o['mile_marker']['direction'] == 'W')
        frame = geodesy.EnuFrame(ref['lat']-.001, ref['lon']-.001)
        point = highwaysigns.exitsigns._xy(frame, [[ref['lon'], ref['lat']]])[0]
        axis = np.asarray(ref['mile_marker']['axis'])
        p = np.c_[np.array([point-axis*500, point+axis*500]), np.zeros(2)]
        pavements = [('real-mile', signroads.Spec(11.8, 'motorway', True, 1, False), p, 11.8)]
        with tempfile.TemporaryDirectory() as tmp:
            unavailable = mock.Mock()
            unavailable.query.side_effect = OSError('offline')
            for tags in (FakeTags(), railtags.SignTags(tmp, NotPublished()), unavailable):
                if isinstance(tags, railtags.SignTags):
                    tags.local = None
                source = highwaysigns.Source(SimpleNamespace(), tags=tags)
                source.context = lambda *a: SimpleNamespace(pavements=lambda *a: pavements)
                with mock.patch.object(highwaysigns.log, 'warning'):
                    _, mesh = highwaysigns.build_tile(SimpleNamespace(highway_signs=source), frame, 0, 0, flat)
                self.assertIsNotNone(mesh)
                self.assertGreater(len(mesh[1]), 0)

    def test_custom_wall_replaces_generic_median_without_removing_deck_or_crossing_rails(self):
        chunks = [dict(n=[[0., -400., 0.], [0., 400., 0.]])]
        native = medians.modules(native_pair(8.), BOX, country='US')
        self.assertGreater(len(native), 0)
        self.assertEqual(len(medianbarriers.native_modules(native, chunks)), 0)
        # A cable treatment sits beside an inner shoulder, away from the generic centreline.
        cable = [dict(n=[[9., -400., 0.], [9., 400., 0.]])]
        self.assertEqual(len(medianbarriers.native_modules(native, cable)), 0)
        deck = native.copy()
        deck[:, 9:11] = 8.
        np.testing.assert_array_equal(medianbarriers.native_modules(deck, chunks), deck)
        rows = medians.tile_rows(FRAME, native_pair(2.), BOX, flat, country='US')
        self.assertTrue(rows)
        self.assertEqual(medianbarriers.mapped_rows(rows, chunks), [])
        outside = [dict(n=[[40., -400., 0.], [40., 400., 0.]])]
        self.assertEqual(medianbarriers.mapped_rows(rows, outside), rows)
        crossing = [dict(n=[[-400., 0., 0.], [400., 0., 0.]])]
        self.assertEqual(medianbarriers.mapped_rows(rows, crossing), rows)
        raised = [r[:3] + [r[3]+8.] + r[4:] for r in rows]
        self.assertEqual(medianbarriers.mapped_rows(raised, chunks), raised)
        self.assertEqual(medians.tile_rows(FRAME, native_pair(8.), BOX, flat, country='US', custom=chunks), [])

    def test_bundled_kentucky_corridor_builds_actual_barrier_and_blue_marker(self):
        obj = next(o for o in milemarkers.references()[0] if o['mile_marker']['state'] == 'KY'
                   and o['mile_marker']['route'] == 65 and o['mile_marker']['mile'] == 46.2)
        frame = geodesy.EnuFrame(obj['lat'], obj['lon'])
        box = (-256., -256., 256., 256.)
        paths = medianbarriers.corridor(frame, box)
        pavements = [(str(i), signroads.Spec(11.8, 'motorway', True, 1, False),
                      np.c_[p, np.zeros(len(p))], 11.8) for i, (route, p) in enumerate(paths)]
        walls = medianbarriers.runs(frame, pavements, box, flat)
        signs = milemarkers.place(frame, [obj], pavements, box, flat)
        self.assertTrue(walls)
        self.assertEqual(len(signs), 1)
        self.assertEqual(signs[0]['style'], 'ky_blue')
        self.assertEqual(signs[0]['mile'], 46.2)

    def test_dedicated_barrier_does_not_change_existing_meshes_or_sound(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = fixture(tmp)
            self.addCleanup(md.highway_signs.close)
            base, port = Path(tmp, 'base'), Path(tmp, 'port')
            base.mkdir()
            port.mkdir()
            # Use real Jersey geometry; the road pipeline is otherwise the same 1.7 fixture.
            with mock.patch.object(medianbarriers, 'corridor', return_value=[(65, np.array([[0., -500.], [0., 500.]]))]), \
                 mock.patch.object(medianbarriers, 'identify', side_effect=lambda f,p,d,c: (65, (p[-1,1]-p[0,1])*d > 0)):
                chunks = medianbarriers.runs(FRAME, pair(), BOX, flat)
            mesh = medianbarriers.build(chunks, (0., 0., 0.))
            self.assertIsNotNone(mesh)
            with mock.patch.object(medianbarriers, 'tile', return_value=(None, ())):
                earthd.Server.build_roads_tile(server(md, str(base)), 1, 0, 0)
            with mock.patch.object(medianbarriers, 'tile', return_value=(mesh, chunks)):
                earthd.Server.build_roads_tile(server(md, str(port)), 1, 0, 0)
            for p in base.iterdir():
                if p.suffix != '.json':
                    self.assertEqual(p.read_bytes(), (port / p.name).read_bytes(), p.name)
            before = json.loads((base / 'roads_1_0_0.json').read_text())
            after = json.loads((port / 'roads_1_0_0.json').read_text())
            self.assertIn('barrier', after)
            self.assertIn('barrier', after['colliders'])
            self.assertNotIn('sound', after)   # a ground median is not an overhead bridge
            del after['barrier']
            del after['colliders']['barrier']
            if not after['colliders']:
                del after['colliders']
            self.assertEqual(before, after)
            self.assertEqual(roads.shape_files(1, 0, 0)['barrier'], 'barrier_1_0_0.cdae')
            worker = Path(tmp, 'worker')
            worker.mkdir()
            state = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None,
                                    sea=None, lock=threading.Lock(), bridges={}, anchor=lambda *a: (None, None))
            with mock.patch.object(blockworker, '_s', state), \
                 mock.patch.object(medianbarriers, 'tile', return_value=(mesh, chunks)):
                blockworker._roads_tile(1, LAT, LON, 15, 0, 0, str(worker), '/temp/test', False, .05,
                                       False, False, True, None, False)
            self.assertEqual((worker/'barrier_1_0_0.cdae').read_bytes(), (port/'barrier_1_0_0.cdae').read_bytes())
            data = json.loads((worker/'roads_1_0_0.json').read_text())
            self.assertEqual(data['colliders']['barrier'], medianbarriers.collider_boxes(chunks))

    def test_transient_median_failure_keeps_roads_available_without_caching_omission(self):
        import revisit
        source = mock.Mock()
        source.context.side_effect = OSError('temporary source failure')
        before = revisit.faults()
        with mock.patch.object(medianbarriers, 'corridor', return_value=[(65, None)]), \
             self.assertLogs('earthd.medians', level='WARNING'):
            self.assertEqual(medianbarriers.tile(SimpleNamespace(highway_signs=source), FRAME, 0, 0, flat), (None, ()))
        self.assertEqual(revisit.faults(), before+1)

    def test_tile_and_marker_mount_share_the_banked_ground(self):
        source = SimpleNamespace(context=lambda *a: SimpleNamespace(pavements=lambda *a: pair()))
        def bank(la, lo):
            x, y, _ = FRAME.geodetic_to_enu(la, lo, 0.)
            return .08*x + .025*y
        with mock.patch.object(medianbarriers, 'corridor', return_value=[(65, np.array([[0., -500.], [0., 500.]]))]), \
             mock.patch.object(medianbarriers, 'identify', side_effect=lambda f,p,d,c: (65, (p[-1,1]-p[0,1])*d > 0)):
            mesh, chunks = medianbarriers.tile(SimpleNamespace(highway_signs=source), FRAME, 0, 0, bank)
            mount = medianbarriers.mount(FRAME, marker(y=50.), pair(), bank)
        self.assertIsNotNone(mesh)
        self.assertIsNotNone(mount)
        line = np.asarray(chunks[0]['n'])
        top = np.asarray(chunks[0]['top'])
        self.assertGreater(np.ptp(top), 1.)
        expected = np.interp(mount[0][1], line[:, 1], top)
        self.assertAlmostEqual(mount[0][2], expected, places=6)
        boxes = medianbarriers.collider_boxes(chunks)
        self.assertTrue(any(b[0] <= mount[0][0] <= b[3] and b[1] <= mount[0][1] <= b[4]
                            and b[2] <= mount[0][2] <= b[5] for b in boxes))

    def test_outside_corridor_does_not_load_extra_context_or_elevation(self):
        from test_roads import FRAME as michigan
        source, elev = mock.Mock(), mock.Mock(side_effect=AssertionError('unneeded elevation'))
        self.assertEqual(medianbarriers.tile(SimpleNamespace(highway_signs=source), michigan, 0, 0, elev), (None, ()))
        source.context.assert_not_called()
        elev.assert_not_called()


if __name__ == '__main__':
    unittest.main()
