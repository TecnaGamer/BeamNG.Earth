"""Startup categories gate generation and isolate the production revisit caches."""
import itertools
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker
import cliffcuts
import cliffrocks
import cliffsettings
import geodesy
import revisit


CONFIGURATIONS = [cliffsettings.CliffSettings(*flags)
                  for flags in itertools.product((False, True), repeat=2)]


class Surface:
    def classes(self, lat, lon):
        return np.full(np.shape(lat), 60), True

    def urban(self, lat, lon):
        return np.zeros(np.shape(lat), bool)


class StartupGenerationTest(unittest.TestCase):
    def test_disabled_does_not_read_inputs(self):
        fail = Mock(side_effect=AssertionError('disabled cliff work'))
        surface = SimpleNamespace(classes=fail)
        with patch.object(cliffrocks, 'qualifying_cells', fail), \
             patch.object(cliffcuts, 'road_groups', fail):
            self.assertIsNone(cliffrocks.build(None, None, fail, surface,
                                              settings=cliffsettings.CliffSettings()))
        fail.assert_not_called()

    def test_each_category_only_invokes_its_own_selector(self):
        frame = geodesy.EnuFrame(46, 7)
        def elev(lat, lon):
            return 100 + (np.asarray(lon) - 7) * 111320 * np.cos(np.radians(46)) * 2
        def natural(h, classes, blocked, **kwargs):
            return np.zeros(blocked.shape, bool)
        def cuts(h, classes, xs, ys, scale, centers, vertices, groups, blocked, *args, **kwargs):
            return tuple(np.zeros(blocked.shape, bool) for _ in range(3))
        # Empty selector results stop before appearance/meshing. Supplying a group
        # proves that a disabled road-cut category skips selection, not just output.
        for settings in CONFIGURATIONS:
            with self.subTest(settings=settings), \
                 patch.object(cliffrocks, 'qualifying_cells', side_effect=natural) as n, \
                 patch.object(cliffcuts, 'road_groups', return_value=[object()]) as groups, \
                 patch.object(cliffcuts, 'select_roads', side_effect=cuts) as c:
                result = cliffrocks.build(frame, (0, 0, 80, 80), elev, Surface(), settings=settings)
                self.assertIsNone(result)
                self.assertEqual(n.call_count, int(settings.natural))
                self.assertEqual(groups.call_count, int(settings.road_cuts))
                self.assertEqual(c.call_count, int(settings.road_cuts))


class StartupCacheTest(unittest.TestCase):
    def test_geotile_cache_keys_separate_all_four_configurations(self):
        keys = []
        with patch.object(blockworker, '_surf_tags', return_value=()), \
             patch.object(revisit, 'code_key', return_value='fixed-test-code'):
            for settings in CONFIGURATIONS * 2:
                with patch.object(cliffsettings, 'SETTINGS', settings):
                    keys.append(blockworker._geo_content_key(
                        'g17_1_1', 15, False, False, True, False, 0, None, None,
                        md={}, rules=()))
        self.assertEqual(len(set(keys[:4])), 4)
        self.assertEqual(keys[:4], keys[4:])

    def test_anchor_cache_reuses_only_matching_configuration(self):
        # Exercise the actual disk-cache read/write path, replacing expensive tile
        # generation with a payload that records the configuration that built it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / 'out'
            out.mkdir()
            state = SimpleNamespace(cache_dir=str(root / 'cache'))
            generated = []
            def build(*args, **kwargs):
                flags = cliffsettings.SETTINGS.cache_key
                generated.append(flags)
                (out / 'roads_1_0_0.json').write_text(json.dumps({'cliffs': flags}), encoding='utf-8')
                return '/temp/test/roads_1_0_0.json', 0, []
            with patch.object(blockworker, '_s', state), \
                 patch.object(blockworker, '_md_versions', return_value={}), \
                 patch.object(blockworker, '_surf_tags', return_value=()), \
                 patch.object(blockworker, '_roads_tile', side_effect=build), \
                 patch.object(revisit, 'code_key', return_value='fixed-test-code'), \
                 patch.object(revisit, 'ON', True), \
                 patch.object(revisit, 'store', wraps=revisit.store) as store:
                for settings in CONFIGURATIONS * 2:
                    with patch.object(cliffsettings, 'SETTINGS', settings):
                        blockworker._roads_tile_cached(
                            1, 46, 7, 15, 0, 0, str(out), '/temp/test', True, .05,
                            False, False, True, None, False, 0, None)
                        payload = json.loads((out / 'roads_1_0_0.json').read_text())
                        self.assertEqual(payload['cliffs'], list(settings.cache_key))
                self.assertEqual(generated, [s.cache_key for s in CONFIGURATIONS])
                self.assertEqual(store.call_count, 4)
                for call, saved in zip(store.call_args_list, CONFIGURATIONS):
                    signature = call.kwargs['where'][-1]
                    self.assertEqual('cliffs=' in signature, saved.enabled)
                    if not saved.enabled:
                        continue
                    for current in CONFIGURATIONS:
                        # Keep unrelated settings from this same row identical.
                        cliff_part = next(p for p in signature.split(',') if p.startswith('cliffs='))
                        self.assertEqual(revisit.sig_current(cliff_part, {'cliffs': current.cache_key}),
                                         current == saved)


if __name__ == '__main__':
    unittest.main()
