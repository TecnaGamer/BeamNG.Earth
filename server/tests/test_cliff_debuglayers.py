"""Cliff map previews follow startup opt-ins and the production material selector."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliffsettings
import debuglayers
import clifftypes


class CliffDebugLayersTest(unittest.TestCase):
    BOX = dict(s=35., w=-115., n=37., e=-112.)

    def test_disabled_categories_do_no_sampling_or_tile_reads(self):
        for natural in (False, True):
            for cuts in (False, True):
                disabled = []
                if not natural:
                    disabled.append('cliff_regions')
                if not cuts:
                    disabled.extend(('cliff_cuts', 'cliff_confirmed_cuts'))
                if not (natural or cuts):
                    disabled.extend(('cliff_generated', 'cliff_rocks'))
                with patch.object(cliffsettings, 'SETTINGS', cliffsettings.CliffSettings(natural, cuts)), \
                     patch.object(debuglayers, '_sampled', side_effect=AssertionError('selector called')), \
                     patch.object(debuglayers, '_generated', side_effect=AssertionError('tiles read')):
                    for layer in disabled:
                        result = debuglayers.query(object(), dict(self.BOX, layer=layer))
                        self.assertEqual(result['features'], [])
                        self.assertEqual(result['enabled'], dict(natural=natural, road_cuts=cuts))
                        self.assertIn('disabled', result['note'])

    def test_material_preview_calls_production_selector_for_both_categories(self):
        with patch.object(cliffsettings, 'SETTINGS', cliffsettings.CliffSettings(True, True)), \
             patch.object(clifftypes, 'select', wraps=clifftypes.select) as select:
            natural = debuglayers.query(object(), dict(self.BOX, layer='cliff_regions'))
            self.assertTrue(natural['features'])
            self.assertEqual(select.call_count, 1)
            self.assertNotIn('context', select.call_args.kwargs)
            cuts = debuglayers.query(object(), dict(self.BOX, layer='cliff_cuts'))
            self.assertTrue(cuts['features'])
            self.assertEqual(select.call_count, 2)
            self.assertEqual(select.call_args.kwargs['context'], 'road_cut')
            self.assertEqual(select.call_args.args[0].shape, (960,))

    def test_loaded_cut_preview_does_not_depend_on_removed_client_graphics_flags(self):
        conn = SimpleNamespace(roads=SimpleNamespace(sent=[]))
        with patch.object(cliffsettings, 'SETTINGS', cliffsettings.CliffSettings(False, True)):
            for layer in ('cliff_generated', 'cliff_confirmed_cuts', 'cliff_rocks'):
                result = debuglayers.query(conn, dict(self.BOX, layer=layer))
                self.assertEqual(result['features'], [])
                self.assertNotIn('disabled', result['note'])

    def test_loaded_geotile_metadata_uses_production_filename_and_no_unsent_tiles(self):
        import json
        import tempfile
        import blockworker
        import geotiles
        import roads
        gx, gy = geotiles.tile_of(36., -114.)
        lat, lon = geotiles.centre(gx, gy)
        bounds = dict(s=lat-.01, w=lon-.01, n=lat+.01, e=lon+.01)
        with tempfile.TemporaryDirectory() as directory:
            tracker = roads.GeoTracker()
            tracker.sent.add((gx, gy))
            conn = SimpleNamespace(roads=tracker, s=SimpleNamespace(out_dir=directory))
            name = blockworker.geo_names(geotiles.key(gx, gy))['json']
            footprint = [0, lon-.001, lat-.001, lon+.001, lat+.001]
            data = dict(debugCliffs=[footprint], debugCliffCuts=[footprint],
                        debugCliffRocks=[[1, lon, lat, 3.]])
            (Path(directory)/name).write_text(json.dumps(data))
            # A present but unsent neighbour must not enter this connection's preview.
            other = blockworker.geo_names(geotiles.key(gx+1, gy))['json']
            (Path(directory)/other).write_text(json.dumps(data))
            with patch.object(cliffsettings, 'SETTINGS', cliffsettings.CliffSettings(True, True)):
                for layer in ('cliff_generated', 'cliff_confirmed_cuts', 'cliff_rocks'):
                    result = debuglayers.query(conn, dict(bounds, layer=layer))
                    self.assertEqual(len(result['features']), 1)
                    self.assertNotIn('predate', result['note'])
                with patch.object(debuglayers, 'MAX_QUERY_BYTES', 1):
                    result = debuglayers.query(conn, dict(bounds, layer='cliff_generated'))
                    self.assertEqual(result['features'], [])
                    self.assertIn('limited', result['note'])

    def test_invalid_bounds_rejected_before_selectors(self):
        with patch.object(debuglayers, '_sampled', side_effect=AssertionError('selector called')):
            for bounds in (dict(self.BOX, s=np.nan), dict(self.BOX, n=34.), dict(self.BOX, e=-116.)):
                with self.assertRaises(ValueError):
                    debuglayers.query(object(), dict(bounds, layer='cliff_regions'))


class CliffDebugRequestTest(unittest.TestCase):
    def run_request(self, conn, msg):
        import earthd
        class Ready:
            def __init__(self, value): self.value = value
            def __await__(self):
                if False: yield
                return self.value
        loop = SimpleNamespace(run_in_executor=lambda pool, fn, *args: Ready(fn(*args)))
        with patch.object(earthd.asyncio, 'get_running_loop', return_value=loop):
            request = earthd.Connection.debug_map(conn, msg)
            with self.assertRaises(StopIteration):
                request.send(None)


    def check_contract(self, message):
        from test_protocol_agreement import SERVER_TO_MOD, OPT
        spec = SERVER_TO_MOD['debug_map_result']
        self.assertEqual(message['type'], 'debug_map_result')
        self.assertLessEqual(set(message)-{'type'}, set(spec))
        for field,(types,flags) in spec.items():
            if field in message:
                self.assertIsInstance(message[field], types)
            else:
                self.assertIn(OPT, flags)

    def test_response_and_error_release_busy_guard(self):
        import earthd
        sent = []
        conn = SimpleNamespace(s=SimpleNamespace(place_pool=None), send=sent.append)
        msg = dict(CliffDebugLayersTest.BOX, layer='cliff_regions', id=12)
        with patch.object(cliffsettings, 'SETTINGS', cliffsettings.CliffSettings()), \
                patch.object(cliffsettings, 'DEBUG', True):   # (the overlay: BNE_CLIFFS or BNE_CLIFFS_DEBUG only)
            self.run_request(conn, msg)
        self.check_contract(sent[-1])
        self.assertEqual(sent[-1]['id'], 12)
        self.assertEqual(sent[-1]['features'], [])
        self.assertFalse(conn._debug_map_busy)
        with patch.object(cliffsettings, 'DEBUG', True):
            self.run_request(conn, dict(msg, s='invalid'))
        self.check_contract(sent[-1])
        self.assertIn('error', sent[-1])
        self.assertFalse(conn._debug_map_busy)

    def test_busy_request_never_queues_another_provider(self):
        import earthd
        sent = []
        conn = SimpleNamespace(_debug_map_busy=True, send=sent.append)
        with patch.object(debuglayers, 'query', side_effect=AssertionError('duplicate work')), \
                patch.object(cliffsettings, 'DEBUG', True):
            self.run_request(conn, dict(id=3))
        self.check_contract(sent[-1])
        self.assertIn('busy', sent[-1]['error'])
        self.assertTrue(conn._debug_map_busy, 'the active request still owns the guard')

    def test_off_without_cliffs_or_debug(self):
        """Neither BNE_CLIFFS nor BNE_CLIFFS_DEBUG: an error at once, no provider asked (PR #1412 review R3)."""
        sent = []
        conn = SimpleNamespace(s=SimpleNamespace(place_pool=None), send=sent.append)
        with patch.object(debuglayers, 'query', side_effect=AssertionError('no provider when off')), \
                patch.object(cliffsettings, 'DEBUG', False), patch.dict('os.environ', {'BNE_CLIFFS': 'off'}):
            self.run_request(conn, dict(CliffDebugLayersTest.BOX, layer='cliff_regions', id=4))
        self.check_contract(sent[-1])
        self.assertIn('off', sent[-1]['error'])


if __name__ == '__main__':
    unittest.main()
