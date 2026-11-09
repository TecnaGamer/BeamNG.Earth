"""Mapped chain-link fences and noise walls (fences.py)."""
import json
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fences
import geodesy
import mapgeom


class KindTest(unittest.TestCase):
    def features(self, cls, tags=None):
        attrs = {"subtype": np.array(["barrier"]), "class": np.array([cls])}
        if tags is not None:
            attrs["tags"] = np.array([json.dumps(tags)])
        return type("F", (), {"attrs": attrs})()

    def test_chain_link_and_noise_wall_only(self):
        self.assertEqual(fences.kind_of(self.features("fence", {"fence_type": "chain_link"}), 0), "chainlink")
        self.assertEqual(fences.kind_of(self.features("fence", {"barrier": "fence", "fence_type": "chainlink"}), 0),
                         "chainlink")
        self.assertIsNone(fences.kind_of(self.features("fence", {"fence_type": "wood"}), 0))
        self.assertIsNone(fences.kind_of(self.features("fence"), 0))
        self.assertEqual(fences.kind_of(self.features("wall", {"wall": "noise_barrier"}), 0), "noisewall")
        self.assertEqual(fences.kind_of(self.features("noise_barrier"), 0), "noisewall")
        self.assertIsNone(fences.kind_of(self.features("wall", {"wall": "brick"}), 0))
        self.assertIsNone(fences.kind_of(self.features("barrier", {"tags": "nope"}), 0))
        self.assertIsNone(fences.kind_of(self.features("wall", {"tags": "null"}), 0))


class PlaceTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(39.2834, -76.5934, 0.0)

    def line(self, cls, tags, xy, fid="line-1", level=None):
        xy = np.asarray(xy, np.float64)
        lat, lon, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        attrs = {"subtype": np.array(["barrier"]), "class": np.array([cls]),
                 "tags": np.array([json.dumps(tags)])}
        if level is not None:
            attrs["level"] = np.array([level], np.float64)
        return mapgeom.Features(
            layer="infrastructure", ids=np.array([fid]), kind=np.array([mapgeom.LINE], np.uint8),
            part_off=np.array([0, 1], np.int64), ring_off=np.array([0, 1], np.int64),
            coord_off=np.array([0, len(xy)], np.int64), lonlat=np.column_stack((lon, lat)),
            attrs=attrs)

    def rows(self, feats, box=(0.0, 0.0, 100.0, 100.0)):
        return fences.rows(self.frame, lambda key: feats, box, lambda la, lo: np.full(np.shape(la), 5.0))

    def test_chain_link_panels_follow_the_line(self):
        got = self.rows(self.line("fence", {"fence_type": "chain_link"}, [[10.0, 50.0], [90.0, 50.0]]))
        self.assertGreaterEqual(len(got), 30)
        self.assertLessEqual(len(got), 45)
        self.assertTrue(all(r[0] == "chainlink" and r[6] == fences.CHAIN_H for r in got))
        xs = np.array([r[1] for r in got])
        self.assertAlmostEqual(float(np.median(np.diff(np.sort(xs)))), fences.CHAIN_LEN, delta=0.05)
        self.assertTrue(all(abs(r[2] - 50.0) < 0.05 and abs(r[4]) < 0.5 and abs(r[3] - 5.16) < 0.02 for r in got))

    def test_a_plain_fence_is_not_chain_link(self):
        self.assertEqual(self.rows(self.line("fence", {"barrier": "fence"}, [[10.0, 50.0], [90.0, 50.0]])), [])

    def test_noise_wall_panels(self):
        got = self.rows(self.line("wall", {"wall": "noise_barrier"}, [[10.0, 20.0], [90.0, 20.0]]))
        self.assertGreaterEqual(len(got), 16)
        self.assertLessEqual(len(got), 22)
        self.assertTrue(all(r[0] == "noisewall" and r[6] == fences.WALL_H and abs(r[3] - 5.0) < 0.02 for r in got))
        xs = np.sort([r[1] for r in got])
        self.assertAlmostEqual(float(np.median(np.diff(xs))), fences.WALL_LEN, delta=0.05)
        self.assertTrue(all(abs(r[4] - 270.0) < 0.5 for r in got))

    # GitHub #775: the B 29's noise wall on its bridge (layer=1) stood on the road under the bridge
    DECK = [{"n": np.array([[0.0, 22.0, 13.0], [100.0, 22.0, 13.0]]), "w": 8.0}]     # 8 m over the ground (5 m)

    def deck_rows(self, tags, y=20.0):
        feats = self.line("wall", dict({"wall": "noise_barrier"}, **tags), [[10.0, y], [90.0, y]])
        return fences.rows(self.frame, lambda key: feats, (0.0, 0.0, 100.0, 100.0),
                           lambda la, lo: np.full(np.shape(la), 5.0), decks=self.DECK)

    def test_a_noise_wall_on_a_bridge_stands_on_its_deck(self):
        for tags in ({"layer": "1"}, {"bridge": "yes"}):
            got = self.deck_rows(tags)
            self.assertTrue(got)
            self.assertTrue(all(abs(r[3] - 13.0) < 0.02 for r in got), (tags, [r[3] for r in got]))

    def test_a_noise_wall_under_or_beside_a_bridge_stays_on_the_ground(self):
        for tags, y in (({}, 20.0), ({"layer": "-1"}, 20.0), ({"layer": "1"}, 60.0)):
            got = self.deck_rows(tags, y)
            self.assertTrue(got)
            self.assertTrue(all(abs(r[3] - 5.0) < 0.02 for r in got), (tags, y, [r[3] for r in got]))

    def test_a_noise_wall_with_overture_level_but_no_layer_tag_stands_on_its_deck(self):
        # Rail-viaduct noise walls: Overture keeps some ways' OSM layer only in the feature's own level
        # (tags without layer, level=2.0): that counts as on a bridge too.
        feats = self.line("wall", {"wall": "noise_barrier"}, [[10.0, 20.0], [90.0, 20.0]], level=2.0)
        got = fences.rows(self.frame, lambda key: feats, (0.0, 0.0, 100.0, 100.0),
                          lambda la, lo: np.full(np.shape(la), 5.0), decks=self.DECK)
        self.assertTrue(got)
        self.assertTrue(all(abs(r[3] - 13.0) < 0.02 for r in got), [r[3] for r in got])

    def test_a_noise_wall_on_a_rail_viaduct_stands_on_its_deck(self):
        # A rail viaduct's noise walls (layer=2) stood across the street under it instead of on it. Rail decks
        # come from railways.tile_mesh's return_decks, in the same format as road decks.
        rail = [{"n": np.array([[-70.0, 3.0, 225.0], [70.0, 27.0, 225.0]]), "w": 4.8}]
        feats = self.line("wall", {"wall": "noise_barrier", "layer": "2"}, [[-60.0, 5.0], [60.0, 25.0]])
        got = fences.rows(self.frame, lambda key: feats, (-100.0, -100.0, 100.0, 100.0),
                          lambda la, lo: np.full(np.shape(la), 210.0), decks=rail)
        self.assertTrue(got)
        self.assertTrue(all(abs(r[3] - 225.0) < 0.1 for r in got), [r[3] for r in got])

    # The Stelling Road Overcrossing, Cupertino (OSM way 921088131, man_made=bridge, layer=1): its noise wall and
    # chain-link fence (ways 921088133 / 921088134, layer=1) run along the outline's long edges, on its nodes, about
    # 10 m either side of the 2-lane road deck's centreline (way 25796371: 6.6 m wide); the sidewalks between are
    # footways (no deck). Past the road deck's reach (half its width + 2 m) both stood on the road under the bridge.
    # As Overture gives them: the layer as the feature's level, not in its source_tags.
    # Metres from the road's centreline (x) along it (y), shifted into the tile.
    STELLING_OUTLINE = [[-10.0, -33.8], [-9.0, -35.0], [0.2, -44.5], [9.8, -54.5], [10.3, -55.1], [10.1, 38.4],
                        [9.5, 39.0], [-0.2, 49.4], [-9.1, 59.0], [-10.5, 60.4], [-10.0, -33.8]]
    STELLING_WALL = [[10.3, -55.1], [10.1, 38.4], [10.2, 51.2]]
    STELLING_FENCE = [[-10.2, -42.9], [-10.0, -33.8], [-10.5, 60.4], [-10.5, 66.6]]
    STELLING_DECK = [{"n": np.array([[49.8, 109.4, 13.0], [50.2, 15.5, 13.0]]), "w": 6.6}]   # 8 m over the ground

    def features(self, items):
        """One infrastructure Features of (fid, kind, subtype, class, tags, level, xy) items, a part and a ring
        each."""
        rings = []
        for _fid, _kind, _sub, _cls, _tags, _level, xy in items:
            xy = np.asarray(xy, np.float64) + (50.0, 60.0)
            lat, lon, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
            rings.append(np.column_stack((lon, lat)))
        n = len(items)
        return mapgeom.Features(
            layer="infrastructure", ids=np.array([it[0] for it in items]),
            kind=np.array([it[1] for it in items], np.uint8), part_off=np.arange(n + 1, dtype=np.int64),
            ring_off=np.arange(n + 1, dtype=np.int64),
            coord_off=np.concatenate(([0], np.cumsum([len(r) for r in rings]))).astype(np.int64),
            lonlat=np.concatenate(rings), attrs={"subtype": np.array([it[2] for it in items]),
                                                 "class": np.array([it[3] for it in items]),
                                                 "tags": np.array([json.dumps(it[4]) for it in items]),
                                                 "level": np.array([it[5] for it in items], np.float64)})

    def stelling_rows(self, level=1.0, outline=True, barrier_tags=None):
        tags = barrier_tags or {}
        items = [("921088133", mapgeom.LINE, "barrier", "wall",
                  dict({"barrier": "wall", "wall": "noise_barrier"}, **tags), level, self.STELLING_WALL),
                 ("921088134", mapgeom.LINE, "barrier", "fence",
                  dict({"barrier": "fence", "fence_type": "chain_link"}, **tags), level, self.STELLING_FENCE)]
        if outline:
            items.append(("921088131", mapgeom.POLYGON, "bridge", "bridge",
                          {"bridge:structure": "beam", "man_made": "bridge", "ref": "37 0388"}, 1.0,
                          self.STELLING_OUTLINE))
        feats = self.features(items)
        return fences.rows(self.frame, lambda key: feats, (0.0, 0.0, 100.0, 140.0),
                           lambda la, lo: np.full(np.shape(la), 5.0), decks=self.STELLING_DECK)

    @staticmethod
    def base(r):
        """A row's ground or deck height: its z less the chain-link panel's lift."""
        return r[3] - (fences.CHAIN_LIFT if r[0] == fences.G_CHAIN else 0.0)

    def test_barriers_on_a_bridge_outline_past_the_road_deck_stand_on_the_deck(self):
        got = self.stelling_rows()
        self.assertEqual(got, self.stelling_rows(level=float("nan"), barrier_tags={"layer": "1"}))   # (or tagged)
        for group in (fences.G_WALL, fences.G_CHAIN):
            mine = [r for r in got if r[0] == group]
            over = [r for r in mine if 30.0 <= r[2] <= 95.0]        # along the road deck, inside the outline
            self.assertGreater(len(over), 10, group)
            self.assertTrue(all(abs(self.base(r) - 13.0) < 0.02 for r in over), (group, [r[3] for r in over]))
            self.assertTrue(all(abs(self.base(r) - 13.0) < 0.02 or abs(self.base(r) - 5.0) < 0.02 for r in mine))
        # off the outline (the fence's approach south of it, the wall's north end past its corner): the ground
        off = [r for r in got if (r[0] == fences.G_CHAIN and (r[2] < 20.0 or r[2] > 123.0))
               or (r[0] == fences.G_WALL and r[2] > 103.0)]
        self.assertTrue(off)
        self.assertTrue(all(abs(self.base(r) - 5.0) < 0.02 for r in off), [(r[0], r[2], r[3]) for r in off])

    def test_barriers_by_a_bridge_outline_not_tagged_on_it_stay_on_the_ground(self):
        # no level, under the bridge (level -1 or layer=-1), or on it with no outline mapped: the road deck's reach only
        for level, outline, tags in ((float("nan"), True, None), (-1.0, True, None),
                                     (float("nan"), True, {"layer": "-1"}), (1.0, False, None)):
            got = self.stelling_rows(level, outline, tags)
            self.assertTrue(got)
            self.assertTrue(all(abs(self.base(r) - 5.0) < 0.02 for r in got), (level, outline, tags))
    def test_an_untagged_wall_keeps_its_panels_over_an_underpass(self):
        # (OSM often leaves layer / bridge off a noise wall running along a bridge. Under the bridge the
        # ground is the road below, and the wall broke into it mid-span; between stretches standing at the
        # deck's height a wall keeps to the deck it runs onto)
        feats = self.line("wall", {"wall": "noise_barrier"}, [[5.0, 20.0], [95.0, 20.0]])

        def ground(la, lo):
            e, n, _ = self.frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
            return np.where(np.abs(e - 50.0) < 20.0, 5.0, 13.0)

        got = fences.rows(self.frame, lambda key: feats, (0.0, 0.0, 100.0, 100.0), ground, decks=self.DECK)
        self.assertTrue(got)
        self.assertTrue(all(abs(r[3] - 13.0) < 0.05 for r in got), sorted(r[3] for r in got))
        # (a wall really under the bridge, standing on the ground the whole way, is left there)
        low = fences.rows(self.frame, lambda key: feats, (0.0, 0.0, 100.0, 100.0),
                          lambda la, lo: np.full(np.shape(la), 5.0), decks=self.DECK)
        self.assertTrue(low)
        self.assertTrue(all(abs(r[3] - 5.0) < 0.05 for r in low), sorted(r[3] for r in low))

    def test_a_noise_wall_stands_off_the_carriageway_edge(self):
        # A wall is laid along the carriageway it runs along with its face WALL_EDGE_M beyond the painted edge,
        # not on the mapped line (that follows the real deck's outer edge, a shoulder further out).
        wall = self.line("wall", {"wall": "noise_barrier"}, [[10.0, 9.0], [90.0, 9.0]])
        spec = type("S", (), {"width": 14.0})()
        road = [("road", spec, np.array([[0.0, 0.0, 5.0], [100.0, 0.0, 5.0]]), None)]
        got = fences.rows(self.frame, lambda key: wall, (0.0, 0.0, 100.0, 100.0),
                          lambda la, lo: np.full(np.shape(la), 5.0), roads=road)
        self.assertTrue(got)
        for r in got:
            a = math.radians(r[4])
            cy = r[2] + 1.25 * math.sin(a) + 2.0 * math.cos(a)
            cx = r[1] + 1.25 * math.cos(a) - 2.0 * math.sin(a)
            self.assertAlmostEqual(cy, 7.0 + fences.WALL_EDGE_M + fences.WALL_ACROSS, delta=0.05)
            self.assertTrue(10.0 - 0.1 <= cx <= 90.0 + 0.1)       # (along the road, unmoved)
            self.assertAlmostEqual(r[4], 270.0, delta=0.5)      # (flat face south, toward the road)

    def test_a_noise_wall_is_never_drawn_across_the_carriageway(self):
        # A way mapped across a road (a data error) put its panels over the asphalt; those panels are left out.
        wall = self.line("wall", {"wall": "noise_barrier"}, [[50.0, -30.0], [50.0, 30.0]])
        spec = type("S", (), {"width": 14.0})()
        road = [("road", spec, np.array([[0.0, 0.0, 5.0], [100.0, 0.0, 5.0]]), None)]
        got = fences.rows(self.frame, lambda key: wall, (0.0, 0.0, 100.0, 100.0),
                          lambda la, lo: np.full(np.shape(la), 5.0), roads=road)
        self.assertTrue(got)                    # (the panels away from the carriageway stay)
        for r in got:
            a = math.radians(r[4])
            cy = r[2] + 1.25 * math.sin(a) + 2.0 * math.cos(a)
            self.assertGreaterEqual(abs(cy), 7.0 - 1e-6, "a panel over the carriageway")

    def test_a_wall_on_a_bridge_ignores_the_road_under_it(self):
        # A rail viaduct's wall crosses a road below: the road is nearer in XY, but at another level, and must
        # not take the wall (it stood on the road below, or turned across it).
        wall = self.line("wall", {"wall": "noise_barrier", "layer": "2"}, [[10.0, 14.0], [90.0, 14.0]])
        deck = [{"n": np.array([[0.0, 20.0, 13.0], [100.0, 20.0, 13.0]]), "w": 8.0}]
        spec = type("S", (), {"width": 7.0})()
        under = [("road", spec, np.array([[0.0, 14.0, 5.0], [100.0, 14.0, 5.0]]), None)]
        got = fences.rows(self.frame, lambda key: wall, (0.0, 0.0, 100.0, 100.0),
                          lambda la, lo: np.full(np.shape(la), 5.0), decks=deck, roads=under)
        self.assertTrue(got)
        self.assertTrue(all(abs(r[4] - 90.0) < 0.5 for r in got), [r[4] for r in got])
        self.assertTrue(all(abs(r[3] - 13.0) < 0.05 for r in got), [r[3] for r in got])

    def test_a_panel_past_the_tile_is_left_out(self):
        feats = self.line("fence", {"fence_type": "chain_link"}, [[-30.0, 10.0], [40.0, 10.0]])
        got = self.rows(feats, (0.0, 0.0, 100.0, 100.0))
        self.assertTrue(got)
        self.assertTrue(all(r[1] >= 0.0 for r in got))
        self.assertLess(min(r[1] for r in got), 5.0)

    def test_catalog_names_the_stock_shapes(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("M.CATALOG.chainlink", source)
        self.assertIn("s_chainlink_old", source)
        self.assertIn("M.CATALOG.noisewall", source)
        self.assertIn("s_concrete_noiseblocker", source)
        self.assertNotIn("M.NO_COLLIDE.chainlink", source)
        self.assertNotIn("M.NO_COLLIDE.noisewall", source)
