"""Regional furniture through the 1.7.2 retained-content, prebuild and native sidewalk paths."""
import json
import pickle
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import blockworker as bw
import cdae
import directionsigns
import geotiles
import poleprofiles
import revisit
import roads
import sidewalks
import townsigns
from test_blockworker import FakeDem, FakeMapData
from test_roads import features


class RegionalGeoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gx, self.gy = geotiles.tile_of(49.026, 7.816)
        self.tile = geotiles.geobox(self.gx, self.gy)
        xy = np.array([[-350., 0.], [350., 0.]])
        f = features([xy], ['residential'])
        la, lo, _ = self.tile.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
        f.lonlat[:] = np.c_[lo, la]
        md = FakeMapData(f, roads.cells_of_tile(self.tile.frame, self.tile, None, 200)[0])
        surface = SimpleNamespace(urban=lambda lat, lon: np.ones(np.shape(lat), dtype=bool),
                                  lots=lambda lat, lon: [])
        state = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=surface, airports=None,
                                sea=None, lock=threading.Lock(), bridges={}, grading=False,
                                cache_dir=str(self.root / 'cache'), anchor=lambda *args: (None, None))
        la, lo, _ = self.tile.frame.enu_to_geodetic(0., 0., 0.)
        cla, clo, _ = self.tile.frame.enu_to_geodetic(200., 0., 0.)
        signs = townsigns.SignData({'signs': [{'id': 1, 'lat': float(la), 'lon': float(lo),
                                              'name': 'Village-test', 'kind': 'both',
                                              'centre': [float(clo), float(cla)]}], 'places': []})
        for mocker in (patch.object(bw, '_s', state), patch.object(revisit, 'ON', True),
                       patch.object(bw, '_geo_inputs', {}),
                       patch.object(townsigns, 'bundled_europe', return_value=signs),
                       patch.object(directionsigns, 'bundled', return_value=SimpleNamespace(nearby=lambda box: []))):
            mocker.start()
            self.addCleanup(mocker.stop)
        self.profile = poleprofiles.geographic_profile('europe')

    def content(self, folder, profile):
        folder.mkdir(exist_ok=True)
        return bw._geo_content(self.gx, self.gy, 15, str(folder), '/test', True,
                               False, False, False, 0., None, 'FR', profile)

    def test_live_content_keeps_signs_and_palette_across_rebase(self):
        out = self.root / 'live'
        _, n, info, _ = self.content(out, self.profile)
        self.assertGreater(n, 0)
        self.assertIn('signs', info['parts'])
        self.assertFalse(info['parts']['signs']['col'])
        names = bw.geo_names(self.tile.key)
        data = json.loads((out / names['json']).read_text())
        self.assertTrue((out / Path(data['signs']['shape']).name).is_file())
        inputs = pickle.loads((out / names['inputs']).read_bytes())
        self.assertTrue(inputs['walks'])
        self.assertTrue(all(w.get('europe') for w in inputs['walks']))
        sign_bytes = (out / names['signs']).read_bytes()
        for aid, lon in [(1, 7.816), (2, 8.02)]:
            bw._geo_skin(aid, 49.026, lon, 15, self.gx, self.gy, str(out), '/test',
                         True, 0., 1 / 32, lambda: self.fail('content was already built'))
            skin = json.loads((out / bw.geo_names(self.tile.key, aid)['json']).read_text())
            self.assertIn('walkcol', skin)
            shape = cdae.read(out / Path(skin['walk']['shape']).name)
            self.assertTrue({'bne_eu_curb', 'bne_eu_asphalt_walk'} <=
                            {m['name'] for m in shape['materials']})
        self.assertEqual((out / names['signs']).read_bytes(), sign_bytes)

    def test_prebuilt_content_is_reused_and_off_has_separate_cache(self):
        scratch = self.root / 'prebuild'
        key = bw.prebuild_geo(self.gx, self.gy, 15, str(scratch), True, False, False,
                             False, 0., None, 'FR', self.profile)
        self.assertIsNotNone(key)
        self.assertFalse(list(scratch.iterdir()), 'prebuilding cleans session artifacts')
        with patch.object(bw, '_tile_parts', side_effect=AssertionError('must hit prebuilt content')):
            _, _, info, _ = self.content(self.root / 'live', self.profile)
        self.assertIn('signs', info['parts'])
        self.assertNotEqual(key, bw._geo_content_key(self.tile.key, 15, True, False, False,
                                                    False, 0., None, 'FR', pole_profile=None))
        _, _, off, _ = self.content(self.root / 'off', None)
        self.assertNotIn('signs', off['parts'])
        inputs = pickle.loads((self.root / 'off' / bw.geo_names(self.tile.key)['inputs']).read_bytes())
        self.assertFalse(any(w.get('europe') for w in inputs['walks']))

    def test_server_prebuild_live_and_map_cache_use_the_same_profile(self):
        from earthd import Server
        out = self.root / 'session'
        out.mkdir()
        with ThreadPoolExecutor(max_workers=1) as pool:
            server = SimpleNamespace(
                anchors={1: SimpleNamespace(lat=49.026, lon=7.816, zooms=[15])},
                mapdata=bw._s.md, road_pool=pool, out_dir=str(out), vfs_dir='/test',
                cache_dir=bw._s.cache_dir, regional_poles='europe', sidewalks=True,
                buildings=True, gas_stations=False, near_visual='off',
                geo_rules=lambda gx, gy: (False, False, 0., None, 'FR', 15),
                keep_fuel_pads=lambda pads: None, imagery=None, layout=None, tone=1.,
                road_widths=None, sea=None)
            expected = Server.geo_content_key(server, self.gx, self.gy)
            self.assertEqual(Server.prebuild_geo(server, self.gx, self.gy), expected)
            with patch.object(bw, '_tile_parts', side_effect=AssertionError('must reuse prebuild')):
                result = Server.build_roads_geo(server, 1, self.gx, self.gy, skin=False)
            self.assertIn('signs', result['content'][2]['parts'])
            self.assertEqual(Server.world_signature(server)['pole_profile'], self.profile)
            server.regional_poles = 'off'
            self.assertNotEqual(Server.geo_content_key(server, self.gx, self.gy), expected)
            self.assertIsNone(Server.world_signature(server)['pole_profile'])


class NativePaletteTest(unittest.TestCase):
    def test_mixed_palettes_preserve_native_geometry_and_collision(self):
        plain, mixed = [sidewalks.Builder((0, 0, 0), collision=True) for _ in range(2)]
        for i, eu in enumerate([False, True, False, True]):
            curb = np.array([[0., i * 20., 0.], [60., i * 20., 1.]])
            dirs = np.array([[0., -1.], [0., -1.]])
            slab = sidewalks.BRICK if i == 3 else 1
            for builder, enabled in [(plain, False), (mixed, eu)]:
                builder.add_run(curb, dirs, 2.5, ramps=[(20, 23)], slab=slab, europe=enabled)
        a, b = plain.arrays(), mixed.arrays()
        np.testing.assert_array_equal(a[0], b[0])
        np.testing.assert_array_equal(a[1], b[1])
        self.assertEqual(set(b[3]), {0, 1, 2, 3, 4, 5, 6})
        for aa, bb in zip(plain.col.arrays(), mixed.col.arrays()):
            np.testing.assert_array_equal(aa, bb)
        # The native tactile pads and brick slabs keep their UVs, including shared vertices.
        keep = np.unique(a[1][np.isin(a[3], [2, 3])])
        np.testing.assert_array_equal(a[2][keep], b[2][keep])


if __name__ == '__main__':
    unittest.main()
