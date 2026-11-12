"""Spatial discovery, exact-node hydration and shared offline welcome-board cache."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from concurrent.futures import CancelledError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapcells
import osmwelcomes
import rangeread


def node(n, welcome=True):
    return dict(type='node', id=n, lat=40.9994853, lon=-104.9062861,
                tags=dict(board_type='welcome_sign' if welcome else 'map', inscription='Welcome to Wyoming'))


class WelcomeSourceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fetcher = SimpleNamespace(get=Mock(return_value=SimpleNamespace(
            body=json.dumps({'elements': [node(1), node(2, False)]}).encode())))
        self.md = SimpleNamespace(cache_dir=self.directory.name, raw_dir=self.directory.name, fetcher=self.fetcher)
        self.source = osmwelcomes.Source(self.md)
        self.source.info = SimpleNamespace(cell=Mock(return_value=[1, 2]))
        self.key = mapcells.cell_of(41., -104.906)

    def test_exact_node_tags_disk_cache_and_offline_reuse(self):
        result = self.source.cell(self.key)
        self.assertEqual([n['id'] for n in result], [1])
        self.assertEqual(result[0]['lon'], -104.9062861)
        self.assertEqual(result[0]['tags']['inscription'], 'Welcome to Wyoming')
        self.assertIn('/nodes.json?nodes=1,2', self.fetcher.get.call_args.args[0])
        self.assertEqual(self.source.cell(self.key), result)
        self.fetcher.get.assert_called_once()
        other = osmwelcomes.Source(self.md)
        other.info = SimpleNamespace(cell=Mock(side_effect=OSError('offline')))
        self.assertEqual(other.cell(self.key), result)
        other.info.cell.assert_not_called()
        target = other.directory / str(self.key) / 'nodes.json'
        os.utime(target, (0, 0))
        self.assertEqual(other.cell(self.key), result, 'stale good data survives an outage')

    def test_empty_cells_are_cached_without_api_calls(self):
        self.source.info.cell.return_value = []
        self.assertEqual(self.source.cell(self.key), [])
        self.assertEqual(self.source.cell(self.key), [])
        self.source.info.cell.assert_called_once()
        self.fetcher.get.assert_not_called()

    def test_failure_and_partial_response_never_cache_false_empty(self):
        self.fetcher.get.return_value.body = json.dumps({'elements': [node(1)]}).encode()
        with self.assertRaises(ValueError):
            self.source.cell(self.key)
        self.assertFalse((self.source.directory / str(self.key) / 'nodes.json').exists())
        with self.assertRaises(OSError):
            self.source.cell(self.key)
        self.fetcher.get.assert_called_once()
        self.source.failed.clear()
        self.fetcher.get.return_value.body = json.dumps({'elements': [node(1), node(2)]}).encode()
        self.assertEqual(len(self.source.cell(self.key)), 2)

    def test_requests_are_batched_and_cancellation_propagates(self):
        ids = list(range(130))
        self.source.info.cell.return_value = ids
        def get(url, **kwargs):
            batch = [int(n) for n in url.split('nodes=')[1].split(',')]
            self.assertLessEqual(len(batch), 64)
            return SimpleNamespace(body=json.dumps({'elements': [node(n) for n in batch]}).encode())
        self.fetcher.get.side_effect = get
        self.assertEqual(len(self.source.cell(self.key)), 130)
        self.assertEqual(self.fetcher.get.call_count, 3)
        other = mapcells.cell_of(41.1, -104.906)
        self.fetcher.get.side_effect = CancelledError()
        with self.assertRaises(CancelledError):
            self.source.cell(other)
        self.assertNotIn(other, self.source.failed)

    def test_spatial_poi_discovery_reads_only_information_nodes(self):
        import duckdb
        target = Path(self.directory.name) / 'pois.parquet'
        con = duckdb.connect()
        try:
            con.execute("""COPY (SELECT * FROM (VALUES
                (1, 'node', 'information', {'xmin': -104.91, 'ymin': 40.99, 'xmax': -104.91, 'ymax': 40.99}),
                (2, 'way', 'information', {'xmin': -104.91, 'ymin': 40.99, 'xmax': -104.91, 'ymax': 40.99}),
                (3, 'node', 'museum', {'xmin': -104.91, 'ymin': 40.99, 'xmax': -104.91, 'ymax': 40.99}),
                (4, 'node', 'information', {'xmin': -100., 'ymin': 40., 'xmax': -100., 'ymax': 40.})
                ) t(id, type, tourism, bbox)) TO '""" + target.as_posix() + "' (FORMAT parquet)")
        finally:
            con.close()
        remote = rangeread.MemoryRange(target.read_bytes())
        remote.cache_dir = self.directory.name
        info = osmwelcomes.Information(self.directory.name, None)
        self.assertEqual(info._information(remote, (-104.92, 40.98, -104.90, 41.), 'demand'), [1])
        self.assertEqual(info._information(remote, (-90., 30., -89., 31.), 'demand'), [])


class OffByDefaultTest(unittest.TestCase):
    def test_no_osm_api_or_layercake_parks_unless_switched_on(self):
        """Players' machines make no api.openstreetmap.org requests for the boards (BNE_OSM_API_SIGNS=1 turns the
        mapped welcomes and exit-tag reads on) and read no Layercake parks file (BNE_OSM_PARKS=1)."""
        import travelboards
        self.assertFalse(travelboards.OSM_API_ON)
        self.assertFalse(travelboards.OSM_PARKS_ON)
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = SimpleNamespace(get=Mock(side_effect=AssertionError('network')))
            source = travelboards.Source(SimpleNamespace(cache_dir=tmp, raw_dir=tmp, fetcher=fetcher, cell=Mock()))
        self.assertIsNone(source.mapped)
        self.assertIsNone(source.exits)
        self.assertIsNone(source.parks)
