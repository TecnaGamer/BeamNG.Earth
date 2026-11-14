import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import build_nl_signal_context as producer


class SignalContextData(unittest.TestCase):
    def test_motor_vehicle_parent_required_and_64_bit_id_kept(self):
        node = dict(type='node', id=12345678901, lon=5., lat=52., tags=dict(highway='traffic_signals'))
        bike = dict(type='way', id=2, nodes=[node['id']], tags=dict(highway='cycleway'))
        car = dict(type='way', id=3, nodes=[node['id']], tags=dict(highway='primary'))
        self.assertEqual(list(producer.osm_records({}, dict(elements=[node, bike]))), [])
        got = list(producer.osm_records({}, dict(elements=[node, bike, car])))
        self.assertEqual(got[0]['id'], '12345678901')
        self.assertEqual(got[0]['road_class'], 'primary')
        node['tags']['traffic_signals'] = 'bicycle'
        self.assertEqual(list(producer.osm_records({}, dict(elements=[node, car]))), [])

    def test_turn_direction_and_lane_count_preserved(self):
        tags = {'highway': 'primary', 'oneway': '-1', 'lanes': '3', 'turn:lanes': 'left|through|right'}
        way = dict(type='way', id=4, geometry=[dict(lon=5., lat=52.), dict(lon=5.001, lat=52.)], tags=tags)
        got = list(producer.osm_records(dict(elements=[way]), {}))
        self.assertEqual(got[0]['tags'], tags)
        self.assertEqual(got[0]['geometry'][0], [5., 52.])
        tags['motor_vehicle'] = 'no'
        self.assertEqual(list(producer.osm_records(dict(elements=[way]), {})), [])

    def test_tile_halo_includes_both_sides_of_boundary(self):
        # x tile boundary at longitude 5.009765625.
        slots = producer.tile_slots([[5.009765625, 52.]])
        self.assertGreaterEqual(len({x for x, y in slots}), 2)

    def test_reproducible_order_independent_public_tiles(self):
        a = dict(kind='signal', id='12345678901', geometry=[[5., 52.]], tags={})
        b = dict(kind='road', id='5', geometry=[[5., 52.], [5.001, 52.]], tags={'highway': 'primary'})
        with tempfile.TemporaryDirectory() as folder:
            x, y = Path(folder) / 'a', Path(folder) / 'b'
            first = producer.write_tiles([a, b, a], x, {'test': {'sha256': 'a' * 64}})
            second = producer.write_tiles([b, a], y, {'test': {'sha256': 'a' * 64}})
            self.assertEqual(first, second)
            self.assertEqual(first['records'], 2)
            self.assertEqual(first['license'], 'ODbL-1.0')
            for name in first['tiles']:
                self.assertEqual((x / name).read_bytes(), (y / name).read_bytes())
                rows = json.loads(gzip.decompress((x / name).read_bytes()))['records']
                self.assertEqual(len(rows), 2)
            with self.assertRaises(ValueError):
                producer.write_tiles([a], x, {})

    def test_conflicting_sources_and_unbounded_geometry_rejected(self):
        a = dict(kind='signal', id='1', geometry=[[5., 52.]], tags={})
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                producer.write_tiles([a, dict(a, geometry=[[5.1, 52.]])], Path(folder) / 'out', {})
        with self.assertRaises(ValueError):
            producer.tile_slots([[3., 50.], [8., 54.]])


if __name__ == '__main__':
    unittest.main()
