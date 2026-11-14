"""Synthetic divided I-80 corridor: pavement is built-up, countryside is not.

No downloaded OSM/WorldCover fixture is available; these exercise the classification
failure independently of network data and preserve explicit OSM way lighting.
"""
import unittest

import numpy as np

from test_roads import FRAME, features
import furniture
import osmtags
import roads


class RuralLightingTest(unittest.TestCase):
    def corridor(self, urban=False, tag=None, link=False):
        f = features([[(-300, 50), (900, 50)], [(-300, 80), (900, 80)]],
                     ["motorway", "motorway"])
        f.attrs["src_record"] = np.array(["w80@1", "w81@1"])
        if tag is not None:
            osmtags.join(f, {80: {"lit": tag}, 81: {"lit": tag}})
        if link:
            from test_roads import rules
            f.rules["subclass"] = rules([(0, "link"), (1, "link")])

        def cover(lat, lon):
            _, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.0)
            return np.ones(np.shape(n), bool) if urban else ((abs(n - 50) < 6) | (abs(n - 80) < 6))

        return f, cover

    def tile(self, f, cover, tx=0):
        props = []
        roads.build_tile(FRAME, tx, 0, [f], lambda la, lo: np.zeros(np.shape(la)),
                         cover, walks=[], props=props, lamp_region=furniture.lamp_region("US", "US-WY"))
        return [r for r in props if r[0] in (furniture.G_LAMP, furniture.G_POLELAMP)]

    def test_rural_i80_parallel_pavement_is_not_urban(self):
        f, cover = self.corridor()
        self.assertTrue(all(not sp.urban for sp in roads.road_specs(f, cover)))
        self.assertEqual(self.tile(f, cover), [])
        self.assertEqual(self.tile(f, cover, 1), [])

    def test_wide_pavement_fringe_does_not_light_a_rural_freeway(self):
        f, _ = self.corridor()
        def cover(lat, lon):
            _, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.0)
            # I-25 at the CO/WY border: WorldCover's built-up classification
            # extends beyond the pavement and reaches the old 30 m probes.
            return (abs(n - 50) < 45) | (abs(n - 80) < 45)
        self.assertTrue(all(not sp.urban for sp in roads.road_specs(f, cover)))
        self.assertEqual(self.tile(f, cover), [])
        self.assertEqual(self.tile(f, cover, 1), [])

    def test_explicit_lighting_survives_rural_suppression(self):
        f, cover = self.corridor(tag="yes")
        self.assertTrue(self.tile(f, cover))
        self.assertTrue(self.tile(f, cover, 1))

    def test_urban_lighting_remains_but_rural_ramps_need_evidence(self):
        self.assertTrue(self.tile(*self.corridor(urban=True)))
        f, cover = self.corridor(link=True)
        self.assertEqual(self.tile(f, cover, -2), [])
        f, cover = self.corridor(link=True, tag="yes")
        self.assertTrue(self.tile(f, cover, -2))

    def test_explicit_unlit_overrides_urban_and_ramps(self):
        for kw in ({"urban": True}, {"link": True}):
            with self.subTest(**kw):
                self.assertEqual(self.tile(*self.corridor(tag="no", **kw)), [])

    def test_unknown_lighting_keeps_inference(self):
        self.assertEqual(self.tile(*self.corridor(tag="unknown")), [])
        self.assertTrue(self.tile(*self.corridor(urban=True, tag="unknown")))


if __name__ == "__main__":
    unittest.main()
