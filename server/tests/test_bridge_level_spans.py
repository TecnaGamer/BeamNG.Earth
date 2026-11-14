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
from test_roads import features, rules


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


if __name__ == '__main__':
    unittest.main()
