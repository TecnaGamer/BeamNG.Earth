"""Local mapped bridge levels survive merged road features (Taiwan interchange, #866)."""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import mapgeom
import roads
from test_roads import FRAME, features, rules


class BridgeLevelSpansTest(unittest.TestCase):
    def test_level_changes_split_parts_without_changing_ground_coverage(self):
        f = features([[(0., 0.), (1000., 0.)]], ['motorway'], flags=[(0, 'is_bridge')],
                     levels=[(0, 3., 0., .4), (0, 2., .4, .8), (0, 1., .8, 1.)])
        parts = roads.bridge_parts(f, 0)
        self.assertEqual(roads.bridge_levels(f, 0), [3., 2., 1.])
        self.assertEqual(len(parts), 3)
        self.assertEqual(roads.ground_parts(f, 0), [])
        np.testing.assert_array_equal(parts[0][-1], parts[1][0])
        np.testing.assert_array_equal(parts[1][-1], parts[2][0])
        spans = roads._bridge_level_spans(f, 0)
        self.assertAlmostEqual(sum(b - a for a, b, _ in spans), 1.)

    def test_same_level_boundaries_do_not_fragment_a_deck(self):
        f = features([[(0., 0.), (1000., 0.)]], ['motorway'], flags=[(0, 'is_bridge')],
                     levels=[(0, 2., 0., .5), (0, 2., .5, 1.)])
        self.assertEqual(roads.bridge_levels(f, 0), [2.])
        self.assertEqual(len(roads.bridge_parts(f, 0)), 1)

    def test_flagged_bridge_without_level_stays_at_default(self):
        f = features([[(0., 0.), (1000., 0.)]], ['motorway'], flags=[(0, 'is_bridge')])
        self.assertEqual(roads.bridge_levels(f, 0), [1.])
        self.assertEqual(len(roads.bridge_parts(f, 0)), 1)

    def test_tiny_level_interval_does_not_leave_a_hole(self):
        f = features([[(0., 0.), (1000., 0.)]], ['motorway'], flags=[(0, 'is_bridge')],
                     levels=[(0, 2., 0., .5), (0, 3., .5, .50001), (0, 2., .50001, 1.)])
        self.assertEqual(roads.bridge_levels(f, 0), [2.])
        self.assertEqual(len(roads.bridge_parts(f, 0)), 1)
        self.assertEqual(roads._bridge_level_spans(f, 0), [(0., 1., 2.)])

    def test_partial_bridge_does_not_eat_tunnel_or_ground(self):
        f = features([[(0., 0.), (1000., 0.)]], ['motorway'],
                     flags=[(0, 'is_bridge', .1, .45), (0, 'is_tunnel', .45, .55),
                            (0, 'is_bridge', .55, .9)],
                     levels=[(0, 2., .1, .3), (0, 3., .3, .9)])
        self.assertEqual(roads.bridge_levels(f, 0), [2., 3., 3.])
        self.assertEqual(len(roads.ground_parts(f, 0)), 2)
        self.assertEqual(len(roads.tunnel_parts(f, 0)), 1)

    def test_real_taiwan_ramp_crossings_keep_the_lower_local_level(self):
        data = json.loads((Path(__file__).parent / 'fixtures/taiwan_bridge_levels.json').read_text())
        rows = data['roads']
        coords, off, flags, levels = [], [0], [], []
        for i, row in enumerate(rows):
            coords.extend(row['parts'][0][0])
            off.append(len(coords))
            flags.extend((i, v, a, b) for a, b, v in row['rules']['flags'])
            levels.extend((i, float(v), a, b) for a, b, v in row['rules']['level'])
        n = len(rows)
        f = mapgeom.Features(layer='roads', ids=np.array([r['id'] for r in rows]),
            kind=np.full(n, mapgeom.LINE, np.uint8), part_off=np.arange(n + 1, dtype=np.int64),
            ring_off=np.arange(n + 1, dtype=np.int64), coord_off=np.array(off, np.int64),
            lonlat=np.asarray(coords, np.float64),
            attrs={'class': np.array(['motorway'] * n), 'subtype': np.array(['road'] * n)},
            rules={'flags': rules(flags), 'level': rules(levels, 'float')})
        frame = geodesy.EnuFrame(*data['anchor'])
        parts, local, owners = [], [], []
        for i in range(n):
            ps = roads.bridge_parts(f, i)
            ls = roads.bridge_levels(f, i)
            self.assertEqual(len(ps), len(ls))
            for p, level in zip(ps, ls):
                e, no, _ = frame.geodetic_to_enu(p[:, 1], p[:, 0], 0.)
                parts.append(np.c_[e, no]); local.append(level); owners.append(i)
        crossings = [(local[i], local[j]) for i, j, _, _ in roads._deck_crossings(parts)
                     if {owners[i], owners[j]} in ({0, 2}, {1, 2}) and local[i] != local[j]]
        self.assertEqual(len(crossings), 2, 'both mainline crossings over the ramp must retain level 3 over level 2')
        self.assertTrue(all(sorted(pair) == [2., 3.] for pair in crossings))


def covered(f, i):
    """Share of feature i's length that its ground, bridge and tunnel parts cover."""
    def length(ps):
        return sum(float(np.hypot(*np.diff(np.c_[p[:, 0] * 111320 * np.cos(np.radians(p[:, 1])), p[:, 1] * 110540],
                                           axis=0).T).sum()) for p in ps)
    whole = length([r for part in f.parts(i) for r in part])
    return (length(roads.ground_parts(f, i)) + length(roads.bridge_parts(f, i)) + length(roads.tunnel_parts(f, i))) / whole


class ShortBridgeOnLongRoadTest(unittest.TestCase):
    """A piece is dropped under MIN_PART of a feature's length, at most MIN_PART_M metres of it: a share of a 24 km
    road was 24 m (the Fuji Subaru Line's 23 m bridge, OSM way 988616594: no deck, and its range cut out of the ground)."""
    L = 24289.0

    def road(self, *ranges):
        flags = [(0, kind, a / self.L, b / self.L) for kind, a, b in ranges]
        return features([[(0., 0.), (self.L, 0.)]], ['secondary'], flags=flags)

    def test_23_m_bridge_on_a_24_km_road_is_kept_with_no_hole(self):
        f = self.road(('is_bridge', 1012., 1035.))
        self.assertEqual(len(roads.bridge_parts(f, 0)), 1)
        self.assertEqual(len(roads.ground_parts(f, 0)), 2)
        self.assertGreater(covered(f, 0), 0.9999)
        keys = roads.part_keys(f, 0, 'bridge')
        self.assertEqual(len(keys), len(roads.bridge_parts(f, 0)))

    def test_every_short_bridge_and_tunnel_of_the_road_is_kept(self):
        f = self.road(('is_bridge', 1012., 1035.), ('is_bridge', 5000., 5016.), ('is_bridge', 9000., 9014.2),
                      ('is_tunnel', 15000., 15020.))
        self.assertEqual(len(roads.bridge_parts(f, 0)), 3)
        self.assertEqual(len(roads.tunnel_parts(f, 0)), 1)
        self.assertEqual(len(roads.ground_parts(f, 0)), 5)
        self.assertGreater(covered(f, 0), 0.9999)
        for want in ('bridge', 'tunnel', 'ground'):
            self.assertEqual(len(roads.part_keys(f, 0, want)), len(roads._parts_by_level_new(f, 0, want)))

    def test_floor_is_a_metre_of_a_long_road_and_a_share_of_a_short_one(self):
        self.assertAlmostEqual(roads.part_floor(self.road(), 0), 1.0 / self.L, delta=1e-7)
        short = features([[(0., 0.), (300., 0.)]], ['secondary'])
        self.assertEqual(roads.part_floor(short, 0), roads.MIN_PART)

    def test_a_sliver_under_a_metre_is_still_dropped(self):
        f = self.road(('is_bridge', 1012., 1012.5))
        self.assertEqual(roads.bridge_parts(f, 0), [])


if __name__ == '__main__':
    unittest.main()
