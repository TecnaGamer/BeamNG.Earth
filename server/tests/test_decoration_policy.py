"""Contrasting synthetic contexts; real geographic evidence lives in test_i25_decorations.

Synthetic feature IDs describe test geometry, never purported OSM records.
"""
import json
import unittest
from unittest import mock

import numpy as np

from test_roads import FRAME, features, rules
import furniture
import revisit
import roads


class DecorationPolicyTest(unittest.TestCase):
    def tile(self, feats, cover=None, tx=0, ty=0):
        walks, props = [], []
        roads.build_tile(FRAME, tx, ty, [feats], lambda lat, lon: np.zeros(np.shape(lat)),
                         cover, walks=walks, props=props, town=True, lamp_region="na:sparse")
        return walks, props

    def tagged_street(self, urban=False, sidewalk="both", lit=""):
        f = features([[(-300, 50), (900, 50)]], ["tertiary"], ids=["synthetic-frontage"])
        f.attrs["osm_sidewalk"] = np.array([sidewalk])
        f.attrs["osm_lit"] = np.array([lit])
        cover = lambda lat, lon: np.full(np.shape(lat), urban, bool)
        return f, cover

    def lamps(self, rows):
        return [r for r in rows if r[0] in (furniture.G_LAMP, furniture.G_POLELAMP)]

    def test_rural_explicit_sidewalk_does_not_imply_lights_or_decorations(self):
        for tag in ("both", "yes", "left", "right"):
            with self.subTest(sidewalk=tag):
                walks, rows = self.tile(*self.tagged_street(sidewalk=tag))
                self.assertTrue(walks)
                self.assertFalse(self.lamps(rows))
                self.assertFalse([r for r in rows if r[0] in
                                  (furniture.G_STREET_TREE, furniture.G_BENCH, furniture.G_BIN)])

    def test_urban_frontage_retains_sidewalks_and_lighting(self):
        walks, rows = self.tile(*self.tagged_street(urban=True))
        self.assertTrue(walks)
        self.assertTrue(self.lamps(rows))

    def test_explicit_absent_or_separate_sidewalk_overrides_development(self):
        for tag in ("no", "none", "separate"):
            with self.subTest(sidewalk=tag):
                walks, rows = self.tile(*self.tagged_street(urban=True, sidewalk=tag))
                self.assertFalse(walks)
                self.assertTrue(self.lamps(rows))

    def test_rural_explicit_lighting_preserved_with_or_without_walks(self):
        for walk in ("both", "no"):
            with self.subTest(sidewalk=walk):
                self.assertTrue(self.lamps(self.tile(*self.tagged_street(sidewalk=walk, lit="yes"))[1]))

    def test_rural_county_and_missing_metadata_do_not_default_to_urban(self):
        for cover in (None, lambda lat, lon: np.zeros(np.shape(lat), bool)):
            f = features([[(-300, 50), (900, 50)]], ["unclassified"], ids=["synthetic-county"])
            walks, rows = self.tile(f, cover)
            self.assertFalse(walks)
            self.assertFalse(self.lamps(rows))
            self.assertTrue([r for r in rows if r[0] == furniture.G_POLE])

    def test_urban_grids_with_parallel_streets_remain_developed(self):
        for spacing in (40, 50, 60, 80):
            with self.subTest(spacing=spacing):
                f = features([[(-300, y), (900, y)] for y in range(-400, 401, spacing)],
                             ["residential"] * len(range(-400, 401, spacing)))
                specs = roads.road_specs(f, lambda lat, lon: np.ones(np.shape(lat), bool))
                self.assertTrue(all(sp.urban and roads.has_sidewalks(sp) for sp in specs))

    def test_explicit_fully_lit_rural_ramp_is_not_limited_to_inferred_end_lamps(self):
        f = features([[(-300, 50), (900, 50)]], ["motorway"], ids=["synthetic-ramp"])
        f.rules["subclass"] = rules([(0, "link")])
        f.attrs["osm_lit"] = np.array(["yes"])
        self.assertTrue(self.lamps(self.tile(f)[1]), "mid-ramp explicitly lit infrastructure")
        f.attrs["osm_lit"][:] = "no"
        self.assertFalse(self.lamps(self.tile(f)[1]))

    def test_parallel_frontage_tags_do_not_light_the_motorway(self):
        f = features([[(-300, 50), (900, 50)], [(-300, 100), (900, 100)]],
                     ["motorway", "tertiary"], ids=["synthetic-highway", "synthetic-frontage"])
        f.attrs["osm_lit"] = np.array(["no", "yes"])
        f.attrs["osm_sidewalk"] = np.array(["", "both"])
        walks, rows = self.tile(f)
        self.assertTrue(walks)
        self.assertTrue(self.lamps(rows))
        self.assertTrue(all(abs(r[2] - 100) < 12 for r in self.lamps(rows)))

    def test_tile_boundary_has_no_duplicate_lamps_and_no_eligibility_change(self):
        f, cover = self.tagged_street(urban=True, lit="yes")
        a, b = self.tile(f, cover), self.tile(f, cover, tx=1)
        self.assertTrue(a[0] and b[0] and self.lamps(a[1]) and self.lamps(b[1]))
        poses = [tuple(row[1:4]) for row in self.lamps(a[1]) + self.lamps(b[1])]
        self.assertEqual(len(poses), len(set(poses)))

    def test_missing_pavement_context_is_conservative_and_not_memoized(self):
        f, cover = self.tagged_street(sidewalk="")
        def arriving(points):
            if not getattr(arriving, "ready", False):
                raise OSError("pending test context")
            return [f]
        f.__dict__["_bnge_pavement"] = arriving
        before = revisit.thread_faults()
        self.assertFalse(roads.road_specs(f, lambda lat, lon: np.ones(np.shape(lat), bool))[0].urban)
        self.assertGreater(revisit.thread_faults(), before)
        arriving.ready = True
        self.assertTrue(roads.road_specs(f, lambda lat, lon: np.ones(np.shape(lat), bool))[0].urban)

    def test_generation_hook_emits_only_selected_segment(self):
        import roaddecor_debug
        f, cover = self.tagged_street(urban=True)
        with mock.patch.object(roaddecor_debug, "ENABLED", True), \
             mock.patch.object(roaddecor_debug, "SELECTORS", frozenset([str(f.ids[0])])), \
             mock.patch.object(roaddecor_debug.log, "info") as logged:
            self.tile(f, cover)
        self.assertTrue(logged.called)
        record = json.loads(logged.call_args.args[1])
        self.assertGreater(record["placement"]["count"], 0)
        self.assertTrue(all(obj["object_source"] == "procedural" and obj["rule"]
                            for obj in record["placement"]["objects"]))


if __name__ == "__main__":
    unittest.main()
