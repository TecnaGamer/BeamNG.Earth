"""Missing source tags must not invent a lane at an urban/bridge boundary."""
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import osmtags
import roads
import navgraph
from test_roads import features, rules


def chain(counts=(2, None, 2), reverse_bridge=False):
    lines = [[(0, 0), (100, 0)], [(100, 0), (200, 0)], [(200, 0), (300, 0)]]
    if reverse_bridge:
        lines[1].reverse()
    f = features(lines, ['motorway']*3, flags=[(1, 'is_bridge')])
    f.attrs['src_record'] = np.array(['w1@1', 'w2@1', 'w3@1'])
    f.rules['oneway'] = rules([(i, -1 if i == 1 and reverse_bridge else 1) for i in range(3)], 'float')
    return osmtags.join(f, {i+1: {'lanes': c} for i, c in enumerate(counts) if c is not None})


class LaneResolutionTest(unittest.TestCase):
    def test_bridge_inherits_connected_osm_count_in_both_geometry_orders(self):
        for reverse in (False, True):
            f = chain(reverse_bridge=reverse)
            self.assertTrue(np.isnan(f.attrs['osm_lanes'][1]))
            np.testing.assert_array_equal(f.attrs['lane_count'], [2, 2, 2])
            self.assertEqual(f.attrs['lane_source'][1], 'connected')
            bridge = roads.bridge_specs(f)[1]
            ground = roads.road_specs(f, lambda lat, lon: np.ones(np.shape(lat), bool))
            self.assertEqual(bridge.lanes, 2)
            self.assertEqual(bridge.width, ground[0].width)
            for sp in (bridge, ground[0], ground[2]):
                self.assertEqual(navgraph._lanes(navgraph.driving_width(sp), True, sp.lanes), (2, 0))
                paint = roads.extras(sp, np.array([[0., 0., 0.], [100., 0., 0.]]), -85.)
                self.assertEqual(sum('dash' in lk for lk, _, _ in paint), 1)

    def test_conflicting_counts_and_explicit_bridge_tags_are_preserved(self):
        f = chain((2, None, 3))
        self.assertTrue(np.isnan(f.attrs['lane_count'][1]))
        f = chain((2, 3, 2))
        self.assertEqual(roads.bridge_specs(f)[1].lanes, 3)
        self.assertEqual(f.attrs['lane_source'][1], 'osm-way')

    def test_no_transfer_from_ramps_opposing_traffic_or_crossing_roads(self):
        for kind in ('ramp', 'opposing', 'crossing'):
            f = features([[(0, 0), (100, 0)], [(100, 0), (200, 0)]], ['motorway']*2,
                         flags=[(1, 'is_bridge')])
            f.attrs['src_record'] = np.array(['w1@1', 'w2@1'])
            f.rules['oneway'] = rules([(0, -1 if kind == 'opposing' else 1), (1, 1)], 'float')
            if kind == 'ramp':
                f.rules['subclass'] = rules([(0, 'link')])
            if kind == 'crossing':
                f.lonlat[:2] = features([[(100, -100), (100, 0)]], ['motorway']).lonlat
            osmtags.join(f, {1: {'lanes': 3}})
            self.assertTrue(np.isnan(f.attrs['lane_count'][1]), kind)

    def test_current_geometry_recovers_replaced_way_ids_and_reversed_geometry(self):
        for reverse in (False, True):
            f = features([[(20, 0), (80, 0)]], ['motorway'])
            f.attrs['src_record'] = np.array(['w999@1'])
            f.rules['oneway'] = rules([(0, -1 if reverse else 1)], 'float')
            if reverse:
                f.lonlat[:] = f.lonlat[::-1]
            geom = features([[(0, 0), (100, 0)]], ['motorway']).lonlat
            osmtags.join(f, {123: {'lanes': 2, '_coords': geom, '_highway': 'motorway', 'osm_oneway': 'yes'}})
            self.assertEqual(roads.road_specs(f)[0].lanes, 2)
            self.assertEqual(f.attrs['lane_source'][0], 'osm-geometry')
            self.assertEqual(f.attrs['lane_osm_way'][0], 123)

    def test_geometry_matching_rejects_wrong_carriageway_and_ambiguity(self):
        for mode in ('opposing', 'far', 'ambiguous', 'ramp'):
            f = features([[(0, 0), (100, 0)]], ['motorway'])
            f.attrs['src_record'] = np.array(['r99@1'])
            f.rules['oneway'] = rules([(0, 1)], 'float')
            geom = features([[(0, 10 if mode == 'far' else 0), (100, 10 if mode == 'far' else 0)]], ['motorway']).lonlat
            tags = {1: {'lanes': 2, '_coords': geom, '_highway': 'motorway_link' if mode == 'ramp' else 'motorway',
                        'osm_oneway': '-1' if mode == 'opposing' else 'yes'}}
            if mode == 'ambiguous':
                tags[2] = dict(tags[1], lanes=3)
            osmtags.join(f, tags)
            self.assertIsNone(roads.road_specs(f)[0].lanes, mode)

    def test_refresh_invalidates_cached_specs(self):
        f = chain((3, 3, 3))
        self.assertEqual(roads.bridge_specs(f)[1].lanes, 3)
        self.assertEqual(roads.road_specs(f)[0].lanes, 3)
        osmtags.join(f, {1: {'lanes': 2}, 3: {'lanes': 2}})
        self.assertEqual(roads.bridge_specs(f)[1].lanes, 2)
        self.assertEqual(roads.road_specs(f)[0].lanes, 2)


if __name__ == '__main__':
    unittest.main()
