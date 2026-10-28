"""A delayed or failed visual job must not block terrain or corrupt a later block's state."""
import asyncio
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor
import multiprocessing
import itertools
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker
import earthd
import surface
from test_pavement_corners import crossing
from test_roads import FRAME, LAT, LON


def photo_worker_init():
    """Real spawned process, deterministic road inputs and no network."""
    feats = crossing()
    blockworker._s = SimpleNamespace(md=None, widths=None,
        surface=SimpleNamespace(urban=None, roads=lambda *_: ([feats], True),
                                lots=lambda *_: [], fuel_pads=lambda *_: []),
        dem=SimpleNamespace(sample=lambda la, lo, zoom: np.full(np.shape(la), 10.)))


class TerrainPhotoTest(unittest.TestCase):
    def test_overlapping_rebuilds_get_distinct_texture_files(self):
        names = []
        def write(anchor, cx, cy, rev, **kwargs):
            name = f'block_base_r{rev}.png'
            names.append(name)
            return name
        s = SimpleNamespace(base_rev=itertools.count(1), imagery=None, write_base=write)
        earthd.Server._base_photo(s, None, 0., 0.)
        earthd.Server._base_photo(s, None, 0., 0.)
        self.assertEqual(len(set(names)), 2, 'old jobs cannot overwrite a replacement texture')

    def server(self):
        s = SimpleNamespace(hm=OrderedDict(), hm_inflight={}, base_stale=set(), base_pending={},
                            layout=SimpleNamespace(block=512), vfs_dir='/test', connections=[],
                            evict_heightmaps=Mock())
        s._base_later = lambda *a: earthd.Server._base_later(s, *a)
        return s

    def info(self):
        return dict(stages={}, zBase=0., maxHeight=1024., q=1/64)

    def test_pending_photo_returns_at_all_speeds_and_completion_before_publish_is_not_lost(self):
        async def run():
            s, base, info = self.server(), Future(), self.info()
            key = (1, 128., 0.)
            anchor = SimpleNamespace(id=1, hm_zoom=16)
            earthd.Server._finish_heightmap(s, anchor, *key[1:], info, time.perf_counter(), base)
            self.assertFalse(base.done(), 'terrain returned while the photo is still blocked')
            self.assertIsNone(info['base'])
            self.assertLess(info['stages']['baseWait'], .8)
            base.set_result(('full.png', [], 99.))  # completes in the old callback-registration race
            built = asyncio.get_running_loop().create_future()
            built.set_result(info)
            earthd.Server._hm_done(s, key, built)
            await asyncio.sleep(0)
            self.assertEqual(s.hm[key]['base'], 'full.png', 'updated even with no connection')
            self.assertNotIn('_base_future', info)
        asyncio.run(run())

    def test_failed_ready_and_late_photos_retain_drivable_ground(self):
        s, anchor = self.server(), SimpleNamespace(id=1, hm_zoom=16)
        for late in (False, True):
            with self.subTest(late=late):
                base, info = Future(), self.info()
                if not late:
                    base.set_exception(OSError('photo worker failed'))
                earthd.Server._finish_heightmap(s, anchor, 0., 0., info, time.perf_counter(), base)
                s.hm[(1, 0., 0.)] = info
                if late:
                    base.set_exception(OSError('late photo worker failed'))
                    s._base_later((1, 0., 0.), info, base)
                self.assertIsNone(info['base'])
                self.assertIn((1, 0., 0.), s.base_stale)
                self.assertNotIn('_base_future', info)

    def test_late_photo_cannot_replace_refreshed_or_rebuilt_block(self):
        s, key, old = self.server(), (1, 0., 0.), self.info()
        conn = SimpleNamespace(send=Mock())
        s.connections = [conn]
        f = Future(); f.set_result(('old.png', [], 20.))
        newer = dict(base='new.png')
        s.hm[key] = newer
        s._base_later(key, old, f)
        self.assertEqual(newer['base'], 'new.png')
        conn.send.assert_not_called()
        old['base'] = 'refreshed.png'
        s.hm[key] = old
        s._base_later(key, old, f)
        self.assertEqual(old['base'], 'refreshed.png')
        conn.send.assert_not_called()

    def test_evicted_job_does_not_leak_late_files(self):
        s, key, old = self.server(), (1, 0., 0.), self.info()
        s.out_dir, s.user_dir, s._remove = '/out', '/user', Mock()
        f = Future(); f.set_result(('old.png', [], 20.))
        s.base_stale.add(key); s.base_pending[key] = frozenset()
        s._base_later(key, old, f)
        self.assertEqual(s._remove.call_count, 2)
        self.assertNotIn(key, s.base_stale)
        self.assertNotIn(key, s.base_pending)

    def test_ready_photo_keeps_lawn_correction_before_publication(self):
        s, info = self.server(), self.info()
        s.out_dir = '/out'
        info.update(ter='block.ter', layers=['layer.png'])
        f = Future(); f.set_result(('photo.png', ['corrected pixels'], 1.))
        with patch.object(surface, 'photo_lawns') as lawns:
            earthd.Server._finish_heightmap(s, SimpleNamespace(id=1, hm_zoom=16), 0., 0., info,
                                            time.perf_counter(), f)
        lawns.assert_called_once_with('/out', 'block.ter', ['layer.png'], 'corrected pixels')
        self.assertEqual(info['base'], 'photo.png')

    def test_spawned_photo_worker_preserves_corrected_pixels_and_full_road_paint(self):
        import terraincache
        # Odd, nonuniform colours stand in for corrected imagery; workers must never render a new source.
        rgb = np.random.default_rng(15).integers(70, 220, (128, 128, 3), dtype=np.uint8)
        original_state = blockworker._s
        self.addCleanup(setattr, blockworker, '_s', original_state)
        photo_worker_init()
        args = (rgb, LAT, LON, -32., -32., 64., 128, False, 15)
        with tempfile.TemporaryDirectory() as temp:
            a, b = os.path.join(temp, 'here.png'), os.path.join(temp, 'worker.png')
            cache_a = (temp, '/here.png', '/ao.png', 251)
            cache_b = (temp, '/worker.png', '/ao.png', 251)
            expected = blockworker.finish_base_photo(*args, a, cache_a)
            with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context('spawn'),
                                     initializer=photo_worker_init) as pool:
                got = pool.submit(blockworker.finish_base_photo, *args, b, cache_b).result(30)
            np.testing.assert_array_equal(got, expected)
            self.assertEqual(Path(a).read_bytes(), Path(b).read_bytes())
            self.assertTrue(np.any(got != rgb), 'roads were actually painted')
            self.assertTrue(np.any(np.all(got == rgb, axis=2)), 'source pixels outside roads retained')
            dds = Path(terraincache.cache_dir(temp), terraincache.base_key('/worker.png', '/ao.png') + '.dds')
            self.assertTrue(dds.is_file(), 'game receives a prepacked texture')

    def test_server_sends_corrected_pixels_to_photo_pool_without_using_physics_pool(self):
        import contextlib
        rgb = np.full((32, 32, 3), (90, 120, 70), np.uint8)
        result = Future(); result.set_result(rgb + 1)
        pool = SimpleNamespace(submit=Mock(return_value=result))
        s = SimpleNamespace(layout=SimpleNamespace(block=512),
            imagery=SimpleNamespace(render=Mock(return_value=(rgb, {'usgs': 1024}))),
            deshadow=SimpleNamespace(no_wait=contextlib.nullcontext),
            colormatch=object(), photo_pool=pool, buildings=True, mapdata=object(),
            out_dir='/out', user_dir='/user', vfs_dir='/vfs', base_name=lambda key, rev: 'base.png',
            block_fuel_pads=lambda *a: [], _base_kept=Mock())
        anchor = SimpleNamespace(id=2, frame=FRAME, lat=LAT, lon=LON, zooms=[15])
        photo = []
        name = earthd.Server.write_base(s, anchor, 0., 0., photo=photo)
        self.assertEqual(name, 'base.png')
        args = pool.submit.call_args.args
        self.assertIs(args[0], blockworker.finish_base_photo)
        np.testing.assert_array_equal(args[1], rgb)
        np.testing.assert_array_equal(photo[0], rgb + 1)
        s._base_kept.assert_called_once()
