"""Mapped guardrail geometry, stock Italy model rows, and Overture tag filtering."""
import json
import os
import sys
import unittest
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy
import guardrails
import mapgeom
import roads
import roadmesh


class FakeFeatures:
    def __init__(self, attrs):
        self.attrs = attrs


class GuardrailTagTest(unittest.TestCase):
    def test_normalized_overture_guardrail(self):
        f = FakeFeatures({"class": np.array(["guard_rail"]), "subtype": np.array(["barrier"])})
        self.assertTrue(guardrails._is_guardrail(f, 0))

    def test_osm_tag_fallback(self):
        f = FakeFeatures({"class": np.array(["barrier"]), "subtype": np.array(["barrier"]),
                          "tags": np.array([json.dumps({"barrier": "guard_rail"})])})
        self.assertTrue(guardrails._is_guardrail(f, 0))

    def test_other_barriers_are_not_guardrails(self):
        f = FakeFeatures({"class": np.array(["fence"]), "subtype": np.array(["barrier"]),
                          "tags": np.array([json.dumps({"barrier": "fence"})])})
        self.assertFalse(guardrails._is_guardrail(f, 0))

    def test_jersey_and_cable_barriers_are_kinds(self):
        for cls, tag in (("jersey_barrier", None), ("barrier", "jersey_barrier"), ("cable_barrier", None)):
            tags = {"barrier": tag} if tag else {}
            f = FakeFeatures({"class": np.array([cls]), "subtype": np.array(["barrier"]),
                              "tags": np.array([json.dumps(tags)])})
            self.assertEqual(guardrails._barrier_kind(f, 0), tag or cls)
            self.assertFalse(guardrails._is_guardrail(f, 0))

    def test_invalid_tags_are_ignored(self):
        f = FakeFeatures({"class": np.array(["barrier"]), "subtype": np.array(["barrier"]),
                          "tags": np.array(["not json"])})
        self.assertFalse(guardrails._is_guardrail(f, 0))

    def test_non_object_json_tags_are_ignored(self):
        # Overture source_tags is optional; malformed / non-object JSON must not abort
        # guardrail generation for the whole road tile.
        for raw in ('null', '[]', '42', '"text"'):
            with self.subTest(raw=raw):
                f = FakeFeatures({"class": np.array(["barrier"]), "subtype": np.array(["barrier"]),
                                  "tags": np.array([raw])})
                self.assertFalse(guardrails._is_guardrail(f, 0))


class GuardrailStockAssetTest(unittest.TestCase):
    def test_stock_italy_guardrail_model_is_registered_as_forest_type(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "mod", "beamng_earth", "lua", "ge", "extensions",
                            "beamngEarth", "trees.lua")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("M.CATALOG.guardrail", source)
        self.assertIn("/art/shapes/objects/italy_guardrails_basic", source)
        self.assertIn("/art/shapes/objects/italy_guardrails_basic_end_cw", source)
        self.assertIn("/art/shapes/objects/italy_guardrails_basic_end_ccw", source)
        self.assertNotIn("M.NO_COLLIDE.guardrail = true", source)
        self.assertIn("M.GROUP_MAX_M.guardrail = 4000", source)
        # (its basis: one reused vec3, column by column; a road geotile's turned by its placement)
        self.assertIn("itemVec:set(vx, vy, vz); m:setColumn(c, itemVec)", source)
        self.assertIn("local isGuardrail = M.isBarrier(g)", source)
        # the median barriers' and mapped jersey / cable barriers' groups (guardrails.GROUPS)
        for g in guardrails.GROUPS:
            self.assertIn("M.CATALOG." + g + " = ", source)
        self.assertIn("/art/shapes/objects/jerseybarrier_3m", source)
        self.assertTrue(os.path.isfile(os.path.join(root, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes",
                                                    "beamngEarth", "bne_cable_barrier.dae")))
        self.assertIn("elseif not rowCap or t.n <= rowCap then", source)


class GuardrailForestTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(39.2834, -76.5934, 0.0)

    def features(self, cls, tags=None, xy=None):
        xy = np.array(xy if xy is not None else [[10.0, 50.0], [90.0, 50.0]], np.float64)
        lat, lon, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(2))
        attrs = {"subtype": np.array(["barrier"]), "class": np.array([cls])}
        if tags is not None:
            attrs["tags"] = np.array([json.dumps(tags)])
        return mapgeom.Features(
            layer="infrastructure",
            ids=np.array(["guard-1"]),
            kind=np.array([mapgeom.LINE], np.uint8),
            part_off=np.array([0, 1], np.int64),
            ring_off=np.array([0, 1], np.int64),
            coord_off=np.array([0, 2], np.int64),
            lonlat=np.column_stack((lon, lat)),
            attrs=attrs,
        )

    def test_mapped_jersey_and_cable_rows(self):
        flat = lambda la, lo: np.zeros_like(np.asarray(la, np.float64))
        box = (0.0, 0.0, 256.0, 256.0)
        for cls, group in (("jersey_barrier", guardrails.G_JERSEY), ("cable_barrier", guardrails.G_CABLE)):
            feats = self.features(cls, tags={"two_sided": "yes"})
            rows = guardrails.forest_rows(self.frame, lambda key: feats, box, flat)
            self.assertGreater(len(rows), 20)
            self.assertEqual({int(r[0]) for r in rows}, {group})     # no caps, no reverse-facing twin
            r = rows[0]
            c0, c1, c2 = np.array(r[8:11]), np.array(r[11:14]), np.array(r[14:17])
            self.assertAlmostEqual(float(np.dot(np.cross(c0, c1), c2)), 1.0, places=6)
            along = c1 if group == guardrails.G_JERSEY else c0         # (the jersey model runs along its y)
            self.assertAlmostEqual(abs(float(along[0])), 1.0, places=6)
            self.assertAlmostEqual(r[6], guardrails.HEIGHTS[group])

    def test_two_sided_osm_tag(self):
        feats = self.features("guard_rail", tags={"two_sided": "yes"})
        self.assertTrue(guardrails._is_two_sided(feats, 0))
        for value in ("no", "false", ""):
            one = self.features("guard_rail", tags={"two_sided": value})
            self.assertFalse(guardrails._is_two_sided(one, 0))

    def test_stock_guardrail_rows_are_exported_in_road_tile_json(self):
        rows = [[0, 300.0, 600.0, 12.0, 0.0, 0.0, 0.9, 0.0]]
        with __import__("tempfile").TemporaryDirectory() as tmp:
            path, count = roads.write_tile(tmp, "vfs", 7, 1, 2, [], guardrail_rows=rows)
            with open(os.path.join(tmp, "roads_7_1_2.json"), encoding="utf-8") as f:
                data = json.load(f)
        self.assertEqual(count, 0)
        self.assertEqual(path, "vfs/roads_7_1_2.json")
        self.assertEqual(data["guardrails"], {"items": rows,
                                               "groups": ["guardrail", "guardrail_start", "guardrail_end",
                                                          "guardrail_back", "guardrail_back_start", "guardrail_back_end",
                                                          "jersey", "jersey_end", "cable"]})
        self.assertNotIn("guardrail", roads.shape_files(7, 1, 2))

    def test_stock_guardrail_rows_follow_ground_and_have_full_orientation(self):
        feats = self.features("guard_rail", xy=[[10.0, 50.0], [90.0, 50.0]])
        rows = guardrails.forest_rows(self.frame, lambda _key: feats, (0.0, 0.0, 100.0, 100.0),
                                      lambda lat, lon: np.full(np.shape(lat), 12.0, np.float64))
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row[0], (0, 1, 2))
            self.assertAlmostEqual(row[3], 12.0, delta=0.05)
            self.assertAlmostEqual(row[4], 0.0, delta=0.1)
            self.assertEqual(row[6], guardrails.MODULE_HEIGHT_M)
            self.assertGreaterEqual(row[7], 0.0)
            self.assertLess(row[7], 1.0)
            self.assertEqual(len(row), 17)
            np.testing.assert_allclose(row[8:17], [1,0,0, 0,1,0, 0,0,1], atol=1e-3)
        groups = [row[0] for row in rows]
        self.assertIn(1, groups)
        self.assertIn(2, groups)
        centres = [row[1] for row in rows if row[0] == 0]
        self.assertGreaterEqual(len(centres), 2)
        for a, b in zip(centres, centres[1:]):
            self.assertAlmostEqual(b - a, guardrails.MODULE_STEP_M, places=4)

    def test_two_sided_guardrails_create_reversed_instances(self):
        feats = self.features("guard_rail", tags={"two_sided": "yes"},
                              xy=[[10.0, 50.0], [90.0, 50.0]])
        rows = guardrails.forest_rows(self.frame, lambda _key: feats, (0.0, 0.0, 100.0, 100.0),
                                      lambda lat, lon: np.full(np.shape(lat), 12.0, np.float64))
        groups = [row[0] for row in rows]
        self.assertEqual(groups.count(0), groups.count(3))
        self.assertEqual(groups.count(1), groups.count(4))
        self.assertEqual(groups.count(2), groups.count(5))
        one = next(row for row in rows if row[0] == 0)
        back = next(row for row in rows if row[0] == 3)
        # The two models must be parallel, separated, and face away from the centre gap.
        np.testing.assert_allclose(back[8:14], [-v for v in one[8:14]], atol=1e-4)
        np.testing.assert_allclose(back[14:17], one[14:17], atol=1e-4)
        self.assertAlmostEqual(abs(one[2] - back[2]), guardrails.TWO_SIDED_SPACING_M, places=3)
        self.assertAlmostEqual(0.5 * (one[2] + back[2]), 50.0, places=2)
        self.assertLess((one[2] - 50.0) * one[12], 0.0)
        self.assertLess((back[2] - 50.0) * back[12], 0.0)

    def test_guardrails_pitch_to_the_ground_grade(self):
        feats = self.features("guard_rail", xy=[[10.0, 50.0], [90.0, 50.0]])
        # A stable east-west grade; longitude is nearly proportional to east distance here.
        elev = lambda lat, lon: 12.0 + (np.asarray(lon) + 76.5934) * 8600.0
        rows = guardrails.forest_rows(self.frame, lambda _key: feats, (0.0, 0.0, 100.0, 100.0), elev)
        segment = next(row for row in rows if row[0] == 0)
        self.assertGreater(segment[10], 0.0)
        self.assertLess(segment[10], 0.5)


    def deck_fixture(self):
        deck = {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 20,
                "deck": True, "fixed": True, "fid": "bridge",
                "n": [[0.0, 0.0, 20.0], [100.0, 0.0, 20.0]]}
        decks = [{"n": deck["n"], "w": deck["w"]}]
        return decks, roadmesh.parapet_runs([deck])

    BOX = (0.0, -20.0, 200.0, 20.0)

    def rows_for(self, xy, ground, decks, parapets=(), tags=None, bridge_rails=False,
                 supported_decks_out=None):
        f = self.features("guard_rail", tags=tags, xy=xy)
        return guardrails.forest_rows(self.frame, lambda _k: f, self.BOX,
                                      lambda lat, lon: np.full(np.shape(lat), float(ground), np.float64),
                                      decks=decks, parapets=parapets, bridge_rails=bridge_rails,
                                      supported_decks_out=supported_decks_out)

    def test_mapped_rails_on_the_deck_edge_are_dropped(self):
        decks, parapets = self.deck_fixture()
        for line_y in (3.5, 4.5, 5.4):        # road edge at 4 m: from 1 m inside it to 1.5 m outside it
            with self.subTest(y=line_y):
                self.assertEqual(self.rows_for([[10.0, line_y], [90.0, line_y]], 20.0, decks, parapets), [])

    def test_explicit_bridge_centre_rail_stays_at_deck_height(self):
        decks, parapets = self.deck_fixture()
        for tags in ({"bridge": "yes"}, {"bridge": "yes", "two_sided": "yes"}):
            with self.subTest(tags=tags):
                rows = self.rows_for([[10.0, 0.0], [90.0, 0.0]], 12.0, decks, parapets, tags=tags)
                self.assertTrue(rows, "an explicitly mapped bridge-level rail must be drawn")
                for row in rows:
                    self.assertAlmostEqual(row[3], 20.0, delta=0.05)

    def test_untagged_centre_rail_stays_at_deck_height(self):
        decks, parapets = self.deck_fixture()
        rows = self.rows_for([[10.0, 0.0], [90.0, 0.0]], 12.0, decks, parapets)
        self.assertTrue(rows)
        for row in rows:
            self.assertAlmostEqual(row[2], 0.0, delta=0.05)
            self.assertAlmostEqual(row[3], 20.0, delta=0.05)

    def test_guardrail_crossing_under_bridge_is_not_lifted_onto_deck(self):
        decks, parapets = self.deck_fixture()
        # This lower road crosses the bridge at right angles. A point-only deck lookup used
        # to mistake the module at the crossing for a centre rail on the bridge.
        rows = self.rows_for([[50.0, -15.0], [50.0, 15.0]], 12.0, decks, parapets)
        self.assertTrue(rows)
        for row in rows:
            self.assertAlmostEqual(row[3], 12.0, delta=0.05)

    def test_explicit_bridge_median_between_two_decks_stays_at_deck_height(self):
        decks = [{"n": [[0.0, -6.0, 20.0], [100.0, -6.0, 20.0]], "w": 8.0},
                 {"n": [[0.0, 6.0, 20.0], [100.0, 6.0, 20.0]], "w": 8.0}]
        # An explicit bridge-level tag disambiguates the median from a lower road under both decks.
        rows = self.rows_for([[10.0, 0.0], [90.0, 0.0]], 12.0, decks, tags={"bridge": "yes"})
        self.assertTrue(rows)
        for row in rows:
            self.assertAlmostEqual(row[3], 20.0, delta=0.05)

    def test_untagged_median_stays_at_deck_height(self):
        decks = [{"n": [[0.0, -6.0, 20.0], [100.0, -6.0, 20.0]], "w": 8.0},
                 {"n": [[0.0, 6.0, 20.0], [100.0, 6.0, 20.0]], "w": 8.0}]
        rows = self.rows_for([[10.0, 0.0], [90.0, 0.0]], 12.0, decks)
        self.assertTrue(rows)
        for row in rows:
            self.assertAlmostEqual(row[2], 0.0, delta=0.05)
            self.assertAlmostEqual(row[3], 20.0, delta=0.05)

    def test_rail_hanging_beside_the_deck_is_dropped_but_a_fill_rail_stays(self):
        decks, parapets = self.deck_fixture()
        # 2.5 m past the edge with the ground 8 m under the deck: the bridge's own rail, mapped off the edge.
        self.assertEqual(self.rows_for([[10.0, 6.5], [90.0, 6.5]], 12.0, decks, parapets), [])
        # The same place on an embankment whose ground is at the road's level: an ordinary rail.
        rows = self.rows_for([[10.0, 6.5], [90.0, 6.5]], 19.5, decks, parapets)
        self.assertTrue(rows)
        for row in rows:
            self.assertAlmostEqual(row[3], 19.5, delta=0.1)
        # Well away from the bridge, on low ground: untouched.
        far = self.rows_for([[10.0, 15.0], [90.0, 15.0]], 12.0, decks, parapets)
        self.assertTrue(far)
        for row in far:
            self.assertAlmostEqual(row[3], 12.0, delta=0.1)

    def test_edge_approach_rail_is_joined_to_the_bridge_rail_and_leaves_it_smoothly(self):
        decks, parapets = self.deck_fixture()
        line = guardrails.BRIDGE_EDGE_OFFSET_M + 4.0
        # An approach rail mapped 1.8 m outside the bridge rail's line, on an embankment 6 m under the deck.
        rows = self.rows_for([[100.0, 6.0], [160.0, 6.0]], 14.0, decks, parapets, bridge_rails=True)
        approach = sorted((r for r in rows if r[1] > 100.5 and r[2] > 0), key=lambda r: r[1])
        self.assertTrue(approach)
        first, last = approach[0], approach[-1]
        self.assertAlmostEqual(first[2], line, delta=0.15)          # on the bridge rail's line at the end
        self.assertAlmostEqual(first[3], 20.0, delta=0.4)           # at deck height
        self.assertAlmostEqual(last[2], 6.0, delta=0.01)            # back on its mapped line ...
        self.assertAlmostEqual(last[3], 14.0, delta=0.01)           # ... and on the ground
        for a, b in zip(approach, approach[1:]):                    # no step anywhere on the way
            self.assertLess(abs(b[2] - a[2]), 0.6)
            self.assertLess(abs(b[3] - a[3]), 1.2)
        # The rail the bridge built ends at the deck end and the approach starts right behind it.
        built = [r for r in rows if r[1] < 100.0 and r[2] > 0]
        self.assertAlmostEqual(max(r[1] for r in built) + guardrails.MODULE_LENGTH_M / 2, 100.0, delta=0.6)
        self.assertLess(first[1] - 1.5 - 100.0, 1.5)

    def test_approach_rail_on_a_fill_stands_at_the_road_level_not_on_the_slope(self):
        deck = {"n": [[0.0, 0.0, 20.0], [100.0, 0.0, 26.0]], "w": 8.0}
        frame = self.frame

        def ground(lat, lon):               # the graded road corridor at 26 m, the verge slope at 14 m
            east, north, _ = frame.geodetic_to_enu(lat, lon, np.zeros(np.shape(lat)))
            return np.where(np.abs(north) <= 3.5, 26.0, 14.0)
        f = self.features("guard_rail", xy=[[100.0, 6.0], [150.0, 6.0]])
        rows = guardrails.forest_rows(frame, lambda _k: f, self.BOX, ground, decks=[deck])
        approach = sorted((r for r in rows if r[1] > 100.5), key=lambda r: r[1])
        self.assertTrue(approach)
        for r in approach:
            s = r[1] - 100.0
            w = float(guardrails._smooth_weight(s))
            self.assertAlmostEqual(r[3], 14.0 + (26.0 - 14.0) * w, delta=0.15)
        self.assertGreater(approach[2][3], 24.0)        # close to the bridge: at the road's level
        self.assertAlmostEqual(approach[-1][3], 14.0, delta=0.01)

    def test_centre_approach_rail_keeps_its_place_and_takes_the_deck_height(self):
        decks, parapets = self.deck_fixture()
        rows = self.rows_for([[100.0, 0.5], [160.0, 0.5]], 14.0, decks, parapets)
        approach = sorted((r for r in rows if r[1] > 100.5), key=lambda r: r[1])
        self.assertTrue(approach)
        for r in approach:
            self.assertAlmostEqual(r[2], 0.5, delta=1e-6)
        self.assertAlmostEqual(approach[0][3], 20.0, delta=0.4)
        self.assertAlmostEqual(approach[-1][3], 14.0, delta=0.01)

    def test_deck_joint_is_not_an_end(self):
        decks = [{"n": [[0.0, 0.0, 20.0], [50.0, 0.0, 20.0]], "w": 8.0},
                 {"n": [[50.0, 0.0, 20.0], [100.0, 0.0, 20.0]], "w": 8.0}]
        ends = guardrails._deck_ends(decks)
        self.assertEqual(len(ends["z"]), 2)
        self.assertEqual(sorted(np.round(ends["e"][:, 0])), [0.0, 100.0])
        for e, t in zip(ends["e"], ends["t"]):          # pointing away from the bridge
            self.assertGreater((e[0] - 50.0) * t[0], 0.0)

    def test_rails_on_a_rail_the_bridge_built_are_doubles(self):
        decks, parapets = self.deck_fixture()
        built = guardrails.bridge_rows(parapets)
        self.assertEqual(len(guardrails._self_doubles(built + built)), len(built))
        moved = [list(r) for r in built]
        for r in moved:
            r[3] += 5.0                                  # another level: not a double
        self.assertEqual(len(guardrails._drop_doubles(moved, built)), len(moved))
        self.assertEqual(guardrails._drop_doubles([list(r) for r in built], built), [])

    def test_bridge_rails_are_not_generated_without_mapped_data(self):
        decks, parapets = self.deck_fixture()
        rows = guardrails.forest_rows(self.frame, None, self.BOX, lambda *_: 0.0,
                                      decks=decks, parapets=parapets, bridge_rails=True)
        self.assertEqual(rows, [])
        self.assertEqual(guardrails.forest_rows(self.frame, None, self.BOX, lambda *_: 0.0,
                                                decks=decks, parapets=parapets), [])

    def test_mapped_edge_rail_enables_both_edges_for_its_deck(self):
        decks, parapets = self.deck_fixture()
        supported = set()
        rows = self.rows_for([[10.0, 4.5], [90.0, 4.5]], 20.0, decks, parapets,
                             bridge_rails=True, supported_decks_out=supported)
        self.assertEqual(supported, {0})
        self.assertTrue(rows, "a mapped edge guardrail supports generation for its bridge deck")
        expected_y = 4.0 + roadmesh.PARAPET_W / 2.0
        ys = {round(row[2], 2) for row in rows}
        self.assertIn(round(expected_y, 2), ys)
        self.assertIn(round(-expected_y, 2), ys,
                      "the bridge deck's opposite edge should be generated too")
        for row in rows:
            self.assertAlmostEqual(abs(row[2]), expected_y, delta=0.15)

    def test_rails_elsewhere_near_a_bridge_do_not_trigger_bridge_generation(self):
        decks, parapets = self.deck_fixture()
        supported = set()
        rows = self.rows_for([[10.0, 15.0], [90.0, 15.0]], 12.0, decks, parapets,
                             bridge_rails=True, supported_decks_out=supported)
        self.assertEqual(supported, set())
        self.assertTrue(rows, "the mapped roadside rail should still be kept")
        self.assertTrue(all(abs(abs(row[2]) - (4.0 + roadmesh.PARAPET_W / 2.0)) > 0.5
                            for row in rows),
                        "a distant rail must not cause either bridge edge to be generated")

    def test_only_the_deck_with_mapped_edge_evidence_is_reported(self):
        chunks = [
            {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 20,
             "deck": True, "fixed": True, "fid": "lower",
             "n": [[0.0, -10.0, 20.0], [100.0, -10.0, 20.0]]},
            {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 30,
             "deck": True, "fixed": True, "fid": "upper",
             "n": [[0.0, 10.0, 30.0], [100.0, 10.0, 30.0]]},
        ]
        decks = [{"n": chunk["n"], "w": chunk["w"]} for chunk in chunks]
        parapets = roadmesh.parapet_runs(chunks)
        supported = set()
        self.rows_for([[10.0, -5.5], [90.0, -5.5]], 20.0, decks, parapets,
                      bridge_rails=True, supported_decks_out=supported)
        self.assertEqual(supported, {0})

    def test_edge_offset_matches_the_parapet_strip_centre(self):
        self.assertAlmostEqual(guardrails.BRIDGE_EDGE_OFFSET_M, roadmesh.PARAPET_W / 2)

    def test_an_explicit_lower_level_rail_under_the_bridge_is_kept(self):
        decks, parapets = self.deck_fixture()
        f = self.features("guard_rail", tags={"layer": "-1"}, xy=[[10.0, 4.5], [90.0, 4.5]])
        rows = guardrails.forest_rows(self.frame, lambda _k: f, (0.0, -20.0, 100.0, 20.0),
                                      lambda lat, lon: np.full(np.shape(lat), 12.0, np.float64),
                                      decks=decks, parapets=parapets)
        self.assertTrue(rows)
        for row in rows:
            self.assertAlmostEqual(row[3], 12.0, delta=0.1)


class BridgeRailTest(unittest.TestCase):
    def runs(self, z1=20.0):
        deck = {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 20,
                "deck": True, "fixed": True, "fid": "bridge",
                "n": [[0.0, 0.0, 20.0], [100.0, 0.0, z1]]}
        return roadmesh.parapet_runs([deck])

    def test_one_rail_line_per_deck_edge_on_the_deck_edge_at_deck_height(self):
        runs = self.runs()
        rows = guardrails.bridge_rows(runs)
        self.assertTrue(rows)
        ys = sorted({round(r[2], 1) for r in rows})
        self.assertEqual(len(ys), 2)
        # the strip centre: half the road width plus half the 0.45 m parapet strip
        for y in ys:
            self.assertAlmostEqual(abs(y), 4.0 + roadmesh.PARAPET_W / 2, delta=0.1)
        for r in rows:
            self.assertEqual(len(r), 17)
            self.assertEqual(r[0], 0)
            self.assertAlmostEqual(r[3], 20.0, delta=1e-6)
            self.assertEqual(r[6], guardrails.MODULE_HEIGHT_M)

    def test_rails_cover_the_whole_run_without_gaps(self):
        rows = [r for r in guardrails.bridge_rows(self.runs()) if r[2] > 0]
        rows.sort(key=lambda r: r[1])
        xs = [r[1] for r in rows]
        half = guardrails.MODULE_LENGTH_M / 2
        self.assertAlmostEqual(xs[0] - half, 0.0, delta=0.6)
        self.assertAlmostEqual(xs[-1] + half, 100.0, delta=0.6)
        for a, b in zip(xs, xs[1:]):
            self.assertLessEqual(b - a, guardrails.MODULE_STEP_M + 1e-6)
            self.assertGreater(b - a, 1.0)

    def test_visible_face_looks_at_the_road_on_both_edges(self):
        # The model's visible side is opposite its back axis: the back must point out of the bridge.
        for r in guardrails.bridge_rows(self.runs()):
            outward = 1.0 if r[2] > 0 else -1.0
            self.assertGreater(r[12] * outward, 0.9, "back axis must point out of the bridge")

    def test_rails_pitch_with_the_deck_grade(self):
        rows = guardrails.bridge_rows(self.runs(z1=30.0))
        self.assertTrue(rows)
        for r in rows:
            self.assertAlmostEqual(r[10], 0.0995 * np.sign(r[8]), delta=0.01)   # a 10 % grade, either way round
            self.assertAlmostEqual(r[3], 20.0 + 0.1 * r[1], delta=0.1)

    def test_no_parapet_runs_no_rows(self):
        self.assertEqual(guardrails.bridge_rows([]), [])
        self.assertEqual(guardrails.bridge_rows(None), [])
        self.assertEqual(guardrails.bridge_rows([{"line": [[0, 0]], "z": [1], "out": [[0, 1]]}]), [])


class BridgeCurbTest(unittest.TestCase):
    def test_curb_height_replaces_the_wall_in_the_deck_mesh(self):
        deck = {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 20,
                "deck": True, "fixed": True, "fid": "bridge",
                "n": [[0.0, 0.0, 20.0], [100.0, 0.0, 20.0]]}
        wall = roadmesh.deck([deck], (0.0, 0.0, 0.0))
        curb = roadmesh.deck([deck], (0.0, 0.0, 0.0), parapet_h=roadmesh.BRIDGE_CURB_H)
        self.assertAlmostEqual(wall[0][:, 2].max() - 20.0, roadmesh.PARAPET_H, delta=1e-6)
        self.assertAlmostEqual(curb[0][:, 2].max() - 20.0, roadmesh.BRIDGE_CURB_H, delta=1e-6)



if __name__ == "__main__":
    unittest.main()
