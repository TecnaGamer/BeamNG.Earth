"""Verified Vedauwoo utility correction reaches normal road-tile generation."""
import unittest
import numpy as np

from test_roads import features, FRAME, flat
import furniture
import roadutilities
import roads


class RoadUtilitiesTest(unittest.TestCase):
    def tile(self, way, tx=0):
        f = features([[(-300., 50.), (900., 50.)]], ['unclassified'])
        f.attrs['src_record'] = np.array([f'w{way}@9'])
        props, wires = [], []
        chunks = roads.build_tile(FRAME, tx, 0, [f], flat, walks=[], props=props, wires=wires)
        return props, wires, chunks

    def test_no_inferred_poles_or_wires_on_vedauwoo_access_road(self):
        for way in (15674422, 1524224932):
            for tile in (0, 1):
                with self.subTest(way=way, tile=tile):
                    props, wires, _ = self.tile(way, tile)
                    self.assertFalse(any(p[0] in (furniture.G_POLE, furniture.G_POLELAMP) for p in props))
                    self.assertEqual(wires, [])

    def test_other_rural_roads_keep_their_poles_and_wires(self):
        props, wires, _ = self.tile(999999)
        self.assertTrue(any(p[0] == furniture.G_POLE for p in props))
        self.assertTrue(wires)

    def test_rule_is_by_osm_identity_and_not_a_name_or_geographic_guess(self):
        f = features([[(0, 0), (100, 0)]], ['unclassified'])
        f.attrs['name'] = np.array(['Vedauwoo Picnic Ground Complex Road'])
        self.assertIsNone(roadutilities.override(f, 0))
        f.attrs['src_record'] = np.array(['w999999@1'])
        self.assertIsNone(roadutilities.override(f, 0))
        f.attrs['src_record'] = np.array(['w15674422@99'])
        self.assertIs(roadutilities.override(f, 0), False)

    def test_road_surfaces_do_not_change(self):
        # Furniture corrections must not alter the generated road geometry.
        f = features([[(-300., 50.), (900., 50.)]], ['unclassified'])
        f.attrs['src_record'] = np.array(['w15674422@1'])
        without = roads.build_tile(FRAME, 0, 0, [f], flat, walks=[])
        with_props = roads.build_tile(FRAME, 0, 0, [f], flat, walks=[], props=[], wires=[])
        self.assertEqual(without, with_props)
