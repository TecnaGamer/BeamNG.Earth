import gzip
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nlsignaldata as data


class ContextReader(unittest.TestCase):
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
        # A rectangle crossing a tile edge reads two source tiles.
        bounds = (5.00976, 52., 5.00978, 52.)
        with patch.object(source, 'version', return_value='test'), \
             patch.object(source, 'shard', side_effect=[dict(format=1, records=[a]), dict(format=1, records=[a])]):
            self.assertEqual(source.query(bounds), [a])
        with patch.object(source, 'version', return_value='test'), \
             patch.object(source, 'shard', side_effect=[dict(format=1, records=[a]), dict(format=1, records=[b])]):
            self.assertEqual(source.query(bounds), [])

    def test_query_neighbourhood_is_bounded(self):
        source = data.Source(None, None, enabled=True)
        with self.assertRaises(ValueError):
            source.query((3., 50., 8., 54.))
        with self.assertRaises(ValueError):
            source.query((5., 52., 4., 51.))


if __name__ == '__main__':
    unittest.main()
