"""Conservative automatic sidewalks, with mapped sidewalk sides preserved."""
import unittest
import numpy as np
from test_roads import FRAME, features
from test_roundabouts import ring_features
import roads


class SidewalkPolicyTest(unittest.TestCase):
    def test_isolated_farm_vertices_do_not_create_a_rural_sidewalk(self):
        f = features([[(0, 0), (5, 0), (10, 0), (15, 0), (20, 0), (400, 0)]], ['primary'])

        def farm(lat, lon):
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.)
            return (e < 50.) & (np.abs(n) < 60.)

        sp = roads.road_specs(f, farm)[0]
        town = roads.road_specs(f, lambda la, lo: np.ones(np.shape(la), bool))[0]
        self.assertTrue(sp.urban, 'retain the upstream width classification')
        self.assertEqual(sp.width, town.width)
        self.assertFalse(roads.has_sidewalks(sp))
        self.assertTrue(roads.has_sidewalks(town))

    def test_main_roads_need_development_on_both_sides(self):
        f = features([[(0, 0), (400, 0)]] * 3, ['primary', 'secondary', 'residential'])

        def one_side(lat, lon):
            _, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.)
            return np.asarray(n) > -6.

        specs = roads.road_specs(f, one_side)
        self.assertEqual([roads.has_sidewalks(sp) for sp in specs], [False, False, True])

    def test_explicit_rural_sides_override_inference(self):
        f = features([[(0, 0), (400, 0)]] * 5, ['primary'] * 5)
        f.attrs['osm_sidewalk'] = np.array(['both', 'left', 'right', 'separate', ''])
        specs = roads.road_specs(f, lambda la, lo: np.zeros(np.shape(la), bool))
        self.assertEqual([roads.has_sidewalks(sp) for sp in specs], [True, True, True, False, False])
        self.assertEqual(roads.walk_sides(specs[1]), (1,))
        self.assertEqual(roads.walk_sides(specs[2]), (-1,))

    def test_roundabouts_require_mapped_sidewalks_even_in_town(self):
        urban = lambda la, lo: np.ones(np.shape(la), bool)
        untagged = roads.road_specs(ring_features(mapped=False), urban)
        self.assertTrue(all(sp.roundabout or sp.roundabout_entry for sp in untagged))
        self.assertFalse(any(roads.has_sidewalks(sp) for sp in untagged))
        mapped = roads.road_specs(ring_features(), urban)
        self.assertTrue(all(roads.has_sidewalks(sp) for sp in mapped[:6]))
        self.assertFalse(roads.has_sidewalks(mapped[-1]))


if __name__ == '__main__':
    unittest.main()
