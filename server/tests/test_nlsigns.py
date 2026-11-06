"""Local protocol and archive tests, without game, NDW or photo requests."""
import asyncio
from concurrent.futures import Future
import json
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nlsigns
import protocol


def center(x, y):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + .5) / 2**17)))), (x + .5) / 2**17 * 360 - 180


class Tiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.x, self.y = nlsigns.tile_of(51.36711, 6.14178)
        self.key = f'g17_{self.x}_{self.y}'
        lat, lon = center(self.x, self.y)
        self.record = dict(id='source-id', lat=lat, lon=lon, asset='B6', bearing_deg=0,
                           supplementary_assets=['OB504'])
        self.tile = dict(schema_version=1, kind='george_v5_render_tile', key=self.key, records=[self.record])
        self.manifest = dict(kind='george_v5_tile_store', zoom=17,
                             runtime_file_pattern='render/g17_{x}_{y}.json', totals=dict(render_tiles=1))
        self.write_zip()
        self.store = nlsigns.Store(self.root)

    def write_zip(self):
        with zipfile.ZipFile(self.root / 'BeamNG_Earth_Nederland_v9_data_01.zip', 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr(nlsigns.PREFIX + 'manifest.json', json.dumps(self.manifest))
            z.writestr(nlsigns.PREFIX + 'render/' + self.key + '.json', json.dumps(self.tile))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def request(self, **extra):
        return dict(type='nl_sign_tiles', id=1, x=self.x, y=self.y, keys=[self.key], **extra)

    def test_direct_archive_roundtrip_and_empty_cache(self):
        q = self.request()
        q['keys'].append(f'g17_{self.x+1}_{self.y}')
        msg = self.store.response(q)
        self.assertEqual(msg['assets'], ['B6', 'OB504'])
        self.assertEqual(msg['tiles'][0][1], [['source-id', self.record['lat'], self.record['lon'], 1, 0, [2]]])
        self.assertEqual(msg['tiles'][1][1], [])
        self.assertFalse((self.root / 'render').exists(), 'Never extract source tiles')
        with mock.patch.object(self.store.archives[0], 'read', side_effect=AssertionError('cached')):
            self.assertEqual(self.store.response(q), msg)
        decoder = protocol.Decoder()
        frame = protocol.encode(msg)
        self.assertEqual(decoder.feed(frame[:7]), [])
        self.assertEqual(decoder.feed(frame[7:]), [msg])

    def test_request_bounds_and_paths(self):
        for change in [dict(id=True), dict(x=True), dict(x=0), dict(keys=[]), dict(keys=['../../x']),
                       dict(keys=[self.key, self.key]), dict(keys=[f'g17_{self.x+2}_{self.y}'])]:
            q = self.request();q.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.response(q)

    def test_negative_cache_is_bounded(self):
        for x in range(self.x, self.x + 140):
            self.store.response(dict(id=1, x=x, y=self.y, keys=[f'g17_{x}_{self.y}']))
        self.assertEqual(len(self.store.cache), 128)

    def test_bad_tile_is_not_cached(self):
        self.store.close()
        self.tile['records'][0]['lon'] = 4
        self.write_zip()
        self.store = nlsigns.Store(self.root)
        with self.assertRaisesRegex(ValueError, 'invalid_source_record'):
            self.store.response(self.request())
        self.assertNotIn(self.key, self.store.cache)

    def test_incomplete_archive_set(self):
        self.store.close()
        self.manifest['totals']['render_tiles'] = 2
        self.write_zip()
        with self.assertRaisesRegex(ValueError, 'Missing data ZIP'):
            nlsigns.Store(self.root)

    def test_loose_store_matches_archive(self):
        expected = self.store.response(self.request())
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))
        (self.root / 'render').mkdir()
        (self.root / 'render' / (self.key + '.json')).write_text(json.dumps(self.tile))
        loose = nlsigns.Store(self.root)
        try:
            actual = loose.response(self.request())
            self.assertEqual(actual, expected)
        finally:
            loose.close()

    def test_cancelled_job_retains_global_capacity(self):
        async def check():
            future = Future();future.set_running_or_notify_cancel()
            with mock.patch.object(self.store.executor, 'submit', return_value=future):
                task = asyncio.create_task(self.store.query(self.request()))
                await asyncio.sleep(0)
                self.assertEqual(self.store.jobs, 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.store.jobs, 1)
                future.set_result({})
                self.assertEqual(self.store.jobs, 0)
        asyncio.run(check())

    def test_real_connection_dispatch(self):
        import earthd
        class Writer:
            frames = []
            def is_closing(self):return False
            def write(self, data):self.frames.append(data)
            async def drain(self):pass
        async def check():
            conn = object.__new__(earthd.Connection)
            conn.s = types.SimpleNamespace(nl_signs=self.store)
            conn.writer = Writer();conn.tasks = set()
            conn.nl_signs_busy = False;conn.nl_signs_last_request = -float('inf')
            conn.dispatch(self.request());conn.dispatch(self.request())
            self.assertEqual(len(conn.tasks), 1, 'One in-flight job per connection')
            await asyncio.gather(*tuple(conn.tasks))
            self.assertFalse(conn.nl_signs_busy)
            response = protocol.Decoder().feed(conn.writer.frames[-1])[0]
            self.assertEqual(response, self.store.response(self.request()))
            conn.dispatch(self.request())
            await asyncio.gather(*tuple(conn.tasks))
            self.assertIn('error', protocol.Decoder().feed(conn.writer.frames[-1])[0])
        asyncio.run(check())

    def test_rvv_only_closes_old_jobs_and_drops_visible_tiles(self):
        import earthd
        import signstream
        conn=object.__new__(earthd.Connection)
        conn.rvv_only=False;conn.signs=signstream.Tracker()
        conn.signs.aid=7;conn.signs.sent={(2,3),(4,5)}
        old=conn.signs;messages=[];conn.send=messages.append
        conn.wake=types.SimpleNamespace(set=lambda:None)
        self.assertTrue(conn.set_rvv_only(True))
        self.assertTrue(old.closed)
        self.assertEqual([m['key'] for m in messages],['s2_3','s4_5'])
        self.assertTrue(all(m['type']=='roads_drop' and m['anchorId']==7 for m in messages))
        self.assertFalse(conn.set_rvv_only(True))
        conn.signs_on=True
        conn.dispatch({'type':'highway_signs','on':True})
        self.assertTrue(conn.rvv_only,'ordinary highway sign toggle must not disable RVV-only')
        conn.dispatch({'type':'rvv_only','on':False})
        self.assertFalse(conn.rvv_only)
        conn.signs.close()


if __name__ == '__main__':
    unittest.main()
