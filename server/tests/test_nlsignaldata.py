import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nlsignaldata as data


class ContextReader(unittest.TestCase):
    def test_missing_published_shard_can_recover_without_a_new_build(self):
        import railtags
        row=dict(kind='signal',id='1',geometry=[[5.,52.]],tags={})
        bounds=(4.99999,51.99999,5.00001,52.00001)
        with tempfile.TemporaryDirectory() as folder:
            source=data.Source(folder,None,enabled=True)
            bodies=[gzip.compress(b'{}'),gzip.compress(json.dumps(dict(format=1,records=[row])).encode())]
            with patch.object(source,'version',return_value='same-build'), \
                 patch.object(source,'known',return_value='same-build'), \
                 patch.object(source,'get',side_effect=bodies) as fetch:
                self.assertFalse(source.snapshot(bounds)['complete'])
                self.assertEqual(source.snapshot(bounds)['records'],[])
                self.assertEqual(fetch.call_count,1)
                later=time.time()+railtags.SHARD_RETRY_S+1
                with patch('railtags.time.time',return_value=later):
                    result=source.snapshot(bounds)
                self.assertTrue(result['complete']);self.assertEqual(result['records'],[row])
                self.assertEqual(fetch.call_count,2)

    def test_active_context_survives_disk_pressure_and_reader_recreation(self):
        import cache
        row=dict(kind='signal',id='1',geometry=[[5.,52.]],tags={})
        x,y=data.tile(5.,52.)
        class Fetcher:
            def get(self,url,**kw):raise AssertionError('cached context must work offline: '+url)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'pagesdata/nl-signal-context'
            shard=root/'cached'/f'{x}_{y}.json.gz';shard.parent.mkdir(parents=True)
            shard.write_bytes(gzip.compress(json.dumps(dict(format=1,records=[row])).encode()))
            manifest=root/'manifest.json'
            manifest.write_text(json.dumps(dict(built='cached',format=1,zoom=14,
                tiles={f'{x}/{y}.json.gz':{}})))
            # The global cache has already indexed old downloads. Touching only
            # the filesystem mtime is insufficient: its live LRU needs the hit.
            os.utime(shard,(1,1));os.utime(manifest,(1,1))
            disk=cache.Cache(folder,0,start=False);disk.scan()
            source=data.Source(folder,Fetcher(),enabled=True,disk=disk)
            bounds=(4.99999,51.99999,5.00001,52.00001)
            self.assertEqual(source.query(bounds),[row])
            disk.evict(target=0)
            self.assertTrue(shard.is_file());self.assertTrue(manifest.is_file())
            reloaded=data.Source(folder,Fetcher(),enabled=True,disk=disk)
            self.assertEqual(reloaded.query(bounds),[row])

    def test_disabled_or_unpublished_does_not_fetch(self):
        source = data.Source(None, None)
        with patch.object(source, 'version', side_effect=AssertionError('off must not fetch')):
            self.assertEqual(source.query((4., 51., 5., 52.)), [])
        source.enabled = True
        with patch.object(source, 'version', return_value=None):
            self.assertEqual(source.query((5., 52., 5.001, 52.001)), [])

    def test_source_schema_rejects_bad_geometry(self):
        row = dict(kind='signal', id='12345678901', geometry=[[5., 52.]])
        self.assertTrue(data.valid(row))
        for bad in (dict(row, id='1.0'), dict(row, geometry=[[float('nan'), 52.]]),
                    dict(row, geometry=[[5., 52.], [5., 52.]]), dict(row, kind='unknown')):
            self.assertFalse(data.valid(bad))

    def test_cached_public_tile_round_trip(self):
        row = dict(kind='signal', id='12345678901', geometry=[[5., 52.]], tags={})
        x, y = data.tile(5., 52.)
        class Fetcher:
            def __init__(self):
                self.urls = []
            def get(self, url, **kw):
                self.urls.append(url)
                if url.endswith('/manifest.json'):
                    return SimpleNamespace(body=json.dumps(dict(built='test-context', format=1, zoom=14)).encode())
                if url.endswith(f'/{x}/{y}.json.gz'):
                    return SimpleNamespace(body=gzip.compress(json.dumps(dict(format=1, records=[row])).encode()))
                raise AssertionError(url)
        bounds = (4.99999, 51.99999, 5.00001, 52.00001)
        with tempfile.TemporaryDirectory() as folder:
            source = data.Source(folder, Fetcher(), enabled=True)
            self.assertEqual(source.query(bounds), [row])
            self.assertEqual(source.query(bounds), [row])
            self.assertEqual(len(source.fetcher.urls), 2)
            reloaded = data.Source(folder, Fetcher(), enabled=True)
            self.assertEqual(reloaded.query(bounds), [row])
            self.assertEqual(reloaded.fetcher.urls, [])

    def test_duplicate_halo_records_collapse_and_conflicts_are_not_used(self):
        a = dict(kind='signal', id='1', geometry=[[5., 52.]], tags={})
        b = dict(a, geometry=[[5.1, 52.]])
        source = data.Source(None, None, enabled=True)
        source.built = 'test'
        # A rectangle crossing a tile edge reads two source tiles.
        bounds = (5.00976, 52., 5.00978, 52.)
        with patch.object(source, 'version', return_value='test'), \
             patch.object(source, 'shard', side_effect=[dict(format=1, records=[a]), dict(format=1, records=[a])]):
            self.assertEqual(source.query(bounds), [a])
        with patch.object(source, 'version', return_value='test'), \
             patch.object(source, 'shard', side_effect=[dict(format=1, records=[a]), dict(format=1, records=[b])]):
            self.assertEqual(source.query(bounds), [])

    def test_missing_tile_and_mid_query_build_change_never_prove_bubeko(self):
        source=data.Source(None,None,enabled=True);source.built='one'
        bounds=(5.,52.,5.001,52.001)
        with patch.object(source,'version',return_value='one'),patch.object(source,'shard',return_value=None):
            got=source.snapshot(bounds)
            self.assertFalse(got['complete']);self.assertEqual(got['records'],[])
        row=dict(kind='signal',id='1',geometry=[[5.,52.]])
        with patch.object(source,'version',return_value='one'),patch.object(source,'known',return_value='two'), \
             patch.object(source,'shard',return_value=dict(format=1,records=[row])):
            got=source.snapshot(bounds)
            self.assertFalse(got['complete']);self.assertEqual(got['records'],[])

    def test_query_neighbourhood_is_bounded(self):
        source = data.Source(None, None, enabled=True)
        with self.assertRaises(ValueError):
            source.query((3., 50., 8., 54.))
        with self.assertRaises(ValueError):
            source.query((5., 52., 4., 51.))

    def test_manifest_proven_empty_tile_is_complete_without_download(self):
        source=data.Source(None,None,enabled=True);source.built='complete'
        source.manifest.manifest=dict(format=1,zoom=14,built='complete',tiles={})
        with patch.object(source,'version',return_value='complete'), \
             patch.object(source,'shard',side_effect=AssertionError('known empty tile must not fetch')):
            got=source.snapshot((5.,52.,5.001,52.001))
        self.assertTrue(got['complete']);self.assertEqual(got['records'],[])


if __name__ == '__main__':
    unittest.main()
