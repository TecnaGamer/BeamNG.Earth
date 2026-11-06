"""furniture: street lights along sidewalk runs at fixed stations of their road, utility poles and their
wires along country roads; the same stations whichever tile draws them."""
import ast
import math
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import furniture  # noqa: E402
import roads  # noqa: E402


def road(fid, cls, urban, x0=-300.0, x1=600.0, y=50.0, width=8.0):
    pts = np.array([(x0, y, 0.0), (x1, y, 0.0)])
    return (fid, roads.Spec(width, "paved", urban, cls, False), pts, 0.0)


class ConstantDevelopment:
    def __init__(self, tier=2, limited=1., urban=1., commercial=1.):
        self.value = SimpleNamespace(tier=tier, limited=limited, urban=urban, commercial=commercial)

    def sample(self, point, outward, half_width):
        return self.value



class SignalSpotTest(unittest.TestCase):
    def test_off_a_deck(self):
        """A ramp's deck coming down beside the approach (Fallsway, Baltimore): the signal moves off it."""
        u, right = np.array([0.0, 1.0]), np.array([1.0, 0.0])
        road = (4.0, np.array([[0.0, -50.0], [0.0, 50.0]]))
        free = furniture.signal_spot(np.zeros(2), u, right, 4.0, 4.0, [road])
        deck = (3.0, np.array([[free[0], -50.0], [free[0], 60.0]]))      # a deck right where it stood
        q = furniture.signal_spot(np.zeros(2), u, right, 4.0, 4.0, [road, deck])
        self.assertTrue(q is None or abs(q[0] - free[0]) >= 3.0 + furniture.SIGN_CLEAR_M - 1e-9, (free, q))

class LampTest(unittest.TestCase):
    def walks(self, r, side):
        fid, spec, pts, _ = r
        line = roads.offset_polyline(pts, side * (spec.width / 2 + 1.25))
        return [{"fid": fid, "pts": line, "side": side, "w": 2.5, "caps": (False, False)}]

    def test_residential_one_side_spaced(self):
        r = road("a", "residential", True)
        rows = []
        for side in (1, -1):
            rows += furniture.place([r], self.walks(r, side), (0, 0, 256, 256))
        lamps = [x for x in rows if x[0] == furniture.G_LAMP]
        self.assertTrue(lamps)
        ys = {round(x[2]) for x in lamps}
        self.assertEqual(len(ys), 1, "one side of a residential street")
        xs = sorted(x[1] for x in lamps)
        self.assertTrue(np.allclose(np.diff(xs), furniture.LAMP_SPACING["residential"][0], atol=0.5))
        # the arm (local +x) points at the road
        lamp = lamps[0]
        d = np.array([np.cos(np.radians(lamp[4])), np.sin(np.radians(lamp[4]))])
        self.assertGreater(float(d @ np.array([0.0, 50.0 - lamp[2]])), 0)

    def test_same_stations_in_neighbouring_tiles(self):
        r = road("b", "residential", True)
        side = 1 if furniture._h01("b", "side") < 0.5 else -1
        a = furniture.place([r], self.walks(r, side), (0, 0, 256, 256))
        b = furniture.place([r], self.walks(r, side), (256, 0, 512, 256))
        xs = sorted(x[1] for x in a + b if x[0] == furniture.G_LAMP)
        self.assertTrue(np.allclose(np.diff(xs), furniture.LAMP_SPACING["residential"][0], atol=0.5))

    def test_town_streets_get_wired_poles(self):
        r = road("t", "residential", True)
        rows, wires = [], []
        for side in (1, -1):
            rows += furniture.place([r], self.walks(r, side), (0, 0, 256, 256), wires=wires, town=True)
        groups = [x[0] for x in rows]
        self.assertNotIn(furniture.G_LAMP, groups)
        self.assertGreaterEqual(groups.count(furniture.G_POLE), 5)
        self.assertGreaterEqual(groups.count(furniture.G_POLELAMP), 2)
        self.assertTrue(wires)

    def test_no_poles_along_a_mountain_pass_road(self):
        """A pass road (roads.Spec.mountain, the Stelvio): no utility poles nor wires, as a road in the hills
        without the flag has them."""
        fid, spec, pts, lon = road("m", "secondary", False)
        rows, wires = [], []
        for sp in (spec, spec._replace(mountain=True)):
            got, w = [], []
            for side in (1, -1):
                got += furniture.place([(fid, sp, pts, lon)], self.walks((fid, sp, pts, lon), side), (0, 0, 256, 256),
                                       wires=w, town=True)
            rows.append([x[0] for x in got].count(furniture.G_POLE))
            wires.append(len(w))
        self.assertGreater(rows[0], 0)
        self.assertEqual((rows[1], wires[1]), (0, 0))

    def test_hydrants_on_town_streets(self):
        r = road("h", "residential", True, x0=-300, x1=900)
        rows = []
        for side in (1, -1):
            rows += furniture.place([r], self.walks(r, side), (0, 0, 600, 256), development=ConstantDevelopment())
        hy = sorted(x[1] for x in rows if x[0] == furniture.G_HYDRANT)
        self.assertGreaterEqual(len(hy), 8)
        self.assertTrue(np.allclose(np.diff(hy), furniture.HYDRANT_SPACING / 2, atol=1.0), "alternating sides")


class DevelopmentDecorationTest(unittest.TestCase):
    OPTIONAL = {furniture.G_STREET_TREE, furniture.G_HYDRANT, furniture.G_BENCH, furniture.G_BIN}

    def street(self, urban=True):
        # Select a street from the existing deterministic share that receives procedural trees.
        fid = next(f"dev{k}" for k in range(100) if furniture._h01(f"dev{k}", "trees") < furniture.STREET_TREE_SHARE)
        r = road(fid, "tertiary", urban, x1=2000.)
        walks = [wk for side in (1, -1) for wk in LampTest.walks(None, r, side)]
        return r, walks

    def optional(self, rows):
        return sorted(tuple(r) for r in rows if r[0] in self.OPTIONAL)

    def test_rural_or_missing_context_keeps_exact_lighting_and_wires(self):
        r, walks = self.street()
        for town, buried in ((False, 0.), (True, 0.), (True, 1.)):
            for lit in (None, "yes", "no"):
                settings = dict(town=town, buried=buried, region="na:sparse", lighting={r[0]: lit})
                rich_wires = []
                rich = furniture.place([r], walks, (0, 0, 512, 256), wires=rich_wires,
                                       development=ConstantDevelopment(), **settings)
                infrastructure = [row for row in rich if row[0] not in self.OPTIONAL]
                # (None: no context given or readable, the old town rule: tested below)
                for context in (ConstantDevelopment(tier=0),):
                    with self.subTest(town=town, buried=buried, lit=lit, context=context):
                        wires = []
                        rows = furniture.place([r], walks, (0, 0, 512, 256), wires=wires,
                                               development=context, **settings)
                        self.assertEqual(self.optional(rows), [])
                        self.assertEqual(rows, infrastructure)
                        self.assertEqual(len(wires), len(rich_wires))
                        for actual, expected in zip(wires, rich_wires):
                            np.testing.assert_array_equal(actual, expected)

    def test_without_a_context_the_town_rule(self):
        """development None (a caller without it, or the tile's cells unreadable): street trees, hydrants, benches and
        bins as before the context, on town streets only (merge of PR #670)."""
        r, walks = self.street()
        town = furniture.place([r], walks, (0, 0, 512, 256), development=None)
        self.assertTrue({row[0] for row in town} & self.OPTIONAL, "a town street keeps its decoration")
        r, walks = self.street(urban=False)
        rural = furniture.place([r], walks, (0, 0, 512, 256), development=None)
        self.assertEqual({row[0] for row in rural} & self.OPTIONAL, set(), "none on a country road")

    def test_existing_rural_sidewalk_can_follow_local_development(self):
        r, walks = self.street(urban=False)
        rows = furniture.place([r], walks, (0, 0, 1000, 256), development=ConstantDevelopment())
        self.assertEqual({row[0] for row in rows} & self.OPTIONAL, self.OPTIONAL)

    def test_context_cannot_decorate_without_walks_or_on_medians(self):
        class Unused:
            def sample(self, *args):
                raise AssertionError("no eligible sidewalk: context must not be sampled")
        r, walks = self.street()
        for runs in ([], [dict(wk, median=True) for wk in walks]):
            rows = furniture.place([r], runs, (0, 0, 512, 256), development=Unused())
            self.assertEqual(self.optional(rows), [])

    def test_limited_decoration_is_a_stable_subset_across_tile_boxes(self):
        r, walks = self.street()
        limited = ConstantDevelopment(tier=1, urban=0., commercial=0.)
        medium = furniture.place([r], walks, (0, 0, 1000, 256), development=limited)
        rich = furniture.place([r], walks, (0, 0, 1000, 256), development=ConstantDevelopment())
        self.assertTrue(self.optional(medium))
        self.assertLess(len(self.optional(medium)), len(self.optional(rich)))
        self.assertTrue(set(self.optional(medium)) <= set(self.optional(rich)))
        self.assertFalse({row[0] for row in medium} & {furniture.G_BENCH, furniture.G_BIN},
                         "residential context alone does not infer benches or bins")
        for context in (limited, ConstantDevelopment()):
            whole = furniture.place([r], walks, (0, 0, 512, 256), development=context)
            parts = furniture.place([r], walks, (0, 0, 256, 256), development=context)
            parts += furniture.place([r], walks, (256, 0, 512, 256), development=context)
            self.assertEqual(self.optional(whole), self.optional(parts))

    def test_procedural_trees_still_respect_bridge_headroom(self):
        r, walks = self.street()
        deck = (np.array([[-400., 50., 5.], [2100., 50., 5.]]), 20.)
        rows = furniture.place([r], walks, (0, 0, 512, 256), development=ConstantDevelopment(), decks=[deck])
        self.assertNotIn(furniture.G_STREET_TREE, [row[0] for row in rows])
        self.assertIn(furniture.G_HYDRANT, [row[0] for row in rows], "low decoration fits under the deck")

    def test_real_context_samples_shallow_storefronts_from_the_road_centre(self):
        import development
        from test_development import FRAME, buildings, ring

        # Close shallow shops, with dense development behind them. Adding the road half-width a second time
        # from the sidewalk's middle would jump over the streetfront and report no frontage.
        f = buildings([ring(-160, 8, 160, 12), ring(-160, -12, 160, -8),
                       ring(-160, 50, 160, 85), ring(-160, -85, 160, -50)], "retail", "commercial")

        class Observed(development.Context):
            def sample(self, point, out, half):
                got = super().sample(point, out, half)
                samples.append((np.asarray(point).copy(), got))
                return got

        samples = []
        context = Observed(FRAME, (-256, -256, 256, 256), {"buildings": [f]})
        r = road("storefront", "tertiary", True, y=0.)
        walks = [wk for side in (1, -1) for wk in LampTest.walks(None, r, side)]
        rows = furniture.place([r], walks, (-100, -20, 100, 20), development=context)
        self.assertTrue(samples)
        self.assertTrue(all(abs(point[1]) < 1e-9 for point, _ in samples))
        self.assertGreater(max(got.frontage for _, got in samples), .9)
        self.assertIn(2, [got.tier for _, got in samples])
        self.assertIn(furniture.G_BIN, [row[0] for row in rows])


class PoleTest(unittest.TestCase):
    def test_poles_and_wires_on_country_roads(self):
        wires = []
        rows = furniture.place([road("c", "tertiary", False)], [], (0, 0, 256, 256), wires=wires)
        poles = [x for x in rows if x[0] == furniture.G_POLE]
        self.assertGreaterEqual(len(poles), 5)
        self.assertTrue(all(abs(abs(x[2] - 50.0) - (4.0 + furniture.POLE_OFF_M)) < 0.01 for x in poles))
        self.assertEqual(len(wires), 3 * len(poles), "three wires per span starting in the tile")
        m = furniture.wire_mesh(wires, (0, 0, 0))
        self.assertEqual(len(m[1]) % 4, 0)

    def test_town_street_without_sidewalk_is_lit(self):
        """A town street with no sidewalk run: lamps from its edge (the user, 2026-10-27: lamps only near the
        camera; most streets had none), on its poles where the lines run overhead, on posts where buried; a main
        road's every pole, a side street's every other; a service road: nothing."""
        rows = furniture.place([road("d", "secondary", True)], [], (0, 0, 256, 256))
        groups = [x[0] for x in rows]
        self.assertEqual(groups.count(furniture.G_POLELAMP), groups.count(furniture.G_POLE))
        self.assertIn(furniture.G_LENS, groups)
        # (North America: a side street's every other pole; elsewhere every pole: OpenStreetMap's lamps 25-30 m apart)
        rows = furniture.place([road("d", "residential", True)], [], (0, 0, 256, 256), region="na")
        groups = [x[0] for x in rows]
        self.assertLess(groups.count(furniture.G_POLELAMP), 0.6 * groups.count(furniture.G_POLE))
        self.assertGreater(groups.count(furniture.G_POLELAMP), 0)
        groups = [x[0] for x in furniture.place([road("d", "residential", True)], [], (0, 0, 256, 256))]
        self.assertEqual(groups.count(furniture.G_POLELAMP), groups.count(furniture.G_POLE))
        rows = furniture.place([road("d", "residential", True)], [], (0, 0, 256, 256), buried=1.0)
        lamps = [x for x in rows if x[0] == furniture.G_LAMP]
        self.assertGreaterEqual(len(lamps), 5)
        self.assertNotIn(furniture.G_POLE, [x[0] for x in rows])
        self.assertTrue(all(abs(abs(x[2] - 50.0) - (4.0 + furniture.LAMP_EDGE_M)) < 0.01 for x in lamps))
        self.assertEqual(furniture.place([road("d", "service", True)], [], (0, 0, 256, 256)), [])
        # highways: urban ones and every ramp lit along the outer edge (one-way: the drivers' right), rural ones dark
        hw = road("m", "motorway", True)
        hw = (hw[0], hw[1]._replace(dir=1, oneway=True), hw[2], hw[3])
        lamps = [x for x in furniture.place([hw], [], (0, 0, 256, 256)) if x[0] == furniture.G_LAMP]
        self.assertGreaterEqual(len(lamps), 3)
        self.assertTrue(all(x[2] < 50.0 for x in lamps), "right of travel along +x: south of the line")
        self.assertTrue(all(x[6] == furniture.HIGHWAY_LAMP_H[None] for x in lamps), "the taller highway post")
        lens = [x for x in furniture.place([hw], [], (0, 0, 256, 256)) if x[0] == furniture.G_LENS]
        k = furniture.HIGHWAY_LAMP_H[None] / furniture.LAMP_H[furniture.G_LAMP]
        self.assertAlmostEqual(lens[0][3] - lamps[0][3], furniture.LAMP_HEADS[furniture.G_LAMP][2] * k
                               - furniture.LENS_DROP_M, places=1)
        self.assertEqual([x for x in furniture.place([road("m", "motorway", False)], [], (0, 0, 256, 256))
                          if x[0] == furniture.G_LAMP], [])
        ramp = road("r", "motorway", False, 20.0, 230.0)
        ramp = (ramp[0], ramp[1]._replace(link=True), ramp[2], ramp[3])
        self.assertTrue(any(x[0] == furniture.G_LAMP for x in furniture.place([ramp], [], (0, 0, 256, 256))))
        # a rural ramp: lamps at its ends only (partial interchange lighting), an urban one along it
        rural = [x for x in furniture.place([ramp], [], (0, 0, 256, 256)) if x[0] == furniture.G_LAMP]
        urban = road("u", "motorway", True, 20.0, 230.0)
        urban = (urban[0], urban[1]._replace(link=True), urban[2], urban[3])
        self.assertEqual(len(rural), 2)
        self.assertGreater(sum(x[0] == furniture.G_LAMP for x in furniture.place([urban], [], (0, 0, 256, 256))), 2)
        # the states' practice: California lights its interchanges only, Utah its side streets sparsely
        self.assertEqual(furniture.lamp_region("US", "US-CA"), "na:ramps")
        self.assertEqual(furniture.lamp_region("US", "US-UT"), "na:sparse")
        self.assertEqual(furniture.lamp_region("US", "US-MD"), "na")
        self.assertEqual(furniture.lamp_region("CA", "CA-ON"), "na")
        self.assertIsNone(furniture.lamp_region("DE", "DE-BY"))
        self.assertEqual([x for x in furniture.place([hw], [], (0, 0, 256, 256), region="na:ramps")
                          if x[0] == furniture.G_LAMP], [], "an urban freeway in California: dark between interchanges")
        self.assertTrue(any(x[0] == furniture.G_LAMP for x in furniture.place([ramp], [], (0, 0, 256, 256),
                                                                              region="na:ramps")), "its ramps lit")
        n = {r: sum(x[0] == furniture.G_LAMP for x in furniture.place([road("d", "residential", True)], [],
                                                                        (0, 0, 256, 256), buried=1.0, town=True,
                                                                        region=r)) for r in ("na", "na:sparse")}
        self.assertLess(n["na:sparse"], n["na"])
        # a country road in a tile of few buildings (town: not a dense core): poles, no lamps
        rows = furniture.place([road("c", "residential", False)], [], (0, 0, 256, 256), town=True)
        self.assertIn(furniture.G_POLE, [x[0] for x in rows])
        self.assertNotIn(furniture.G_POLELAMP, [x[0] for x in rows])

    def test_buried_lines_no_poles(self):
        """The country's low-voltage lines underground (furniture.BURIED_LV, the user 2026-10-26: poles "almost
        everywhere"): that share of the roads (hashed) have no poles; all of them in the Netherlands."""
        self.assertEqual(furniture.buried_share("nl"), 1.0)
        self.assertEqual(furniture.buried_share("US"), 0.0)
        self.assertEqual(furniture.buried_share(None), 0.0)
        rows = furniture.place([road("c", "tertiary", False)], [], (0, 0, 256, 256), wires=[], buried=1.0)
        self.assertNotIn(furniture.G_POLE, [x[0] for x in rows])
        roads_ = [road(f"r{k}", "tertiary", False) for k in range(200)]
        with_poles = sum(1 for r in roads_
                         if any(x[0] == furniture.G_POLE for x in furniture.place([r], [], (0, 0, 256, 256), buried=0.5)))
        self.assertTrue(70 <= with_poles <= 130, with_poles)

    def test_buried_town_street_gets_lamps(self):
        r = road("t", "residential", True)
        rows = []
        for side in (1, -1):
            rows += furniture.place([r], LampTest.walks(None, r, side), (0, 0, 256, 256), wires=[], town=True, buried=1.0)
        groups = [x[0] for x in rows]
        self.assertNotIn(furniture.G_POLE, groups)
        self.assertIn(furniture.G_LAMP, groups)
        self.assertIn(furniture.G_LENS, groups)        # (the lamps' lenses: night lighting)
        # and a far glow at every lens, at the same place
        lens = sorted(tuple(x[1:4]) for x in rows if x[0] == furniture.G_LENS)
        self.assertEqual(lens, sorted(tuple(x[1:4]) for x in rows if x[0] == furniture.G_GLOW))


class JunctionTest(unittest.TestCase):
    def arm(self, fid, cls, dx, dy, c=(100.0, 100.0)):
        pts = np.array([(c[0], c[1], 0.0), (c[0] + dx * 80, c[1] + dy * 80, 0.0)])
        return (fid, roads.Spec(10.0, "paved", True, cls, False), pts, 0.0)

    def test_signals_where_major_roads_cross(self):
        rs = [self.arm(f"a{i}", "tertiary", dx, dy) for i, (dx, dy) in enumerate([(1, 0), (-1, 0), (0, 1), (0, -1)])]
        rows = furniture.junction_props(rs, (0, 0, 256, 256))
        self.assertEqual(sorted(r[0] for r in rows), [furniture.G_SIGNAL] * 4)
        for r in rows:              # on the approach's right, outside the carriageways
            self.assertGreater(max(abs(r[1] - 100), abs(r[2] - 100)), 5.0)

    def test_signals_only_where_the_map_has_them(self):
        """With OpenStreetMap's signals (mapped_signals' points): lights at a junction with one near it, none at a
        major crossing without (stop signs there are left to the minor arms: none here), and lights at a mapped rural
        junction too (the user, 2026-10-25: lights where there are none)."""
        rs = [self.arm(f"a{i}", "tertiary", dx, dy) for i, (dx, dy) in enumerate([(1, 0), (-1, 0), (0, 1), (0, -1)])]
        none = furniture.junction_props(rs, (0, 0, 256, 256), signals=np.zeros((0, 2)))
        self.assertNotIn(furniture.G_SIGNAL, [r[0] for r in none], "no mapped signal: no lights")
        far = furniture.junction_props(rs, (0, 0, 256, 256), signals=np.array([[200.0, 200.0]]))
        self.assertNotIn(furniture.G_SIGNAL, [r[0] for r in far], "a signal at the next junction is not this one's")
        at = furniture.junction_props(rs, (0, 0, 256, 256), signals=np.array([[112.0, 96.0]]))
        self.assertEqual(sorted(r[0] for r in at), [furniture.G_SIGNAL] * 4, "mapped at its stop line: lights")
        rural = [(f, s._replace(urban=False), p, l) for f, s, p, l in rs]
        self.assertEqual(furniture.junction_props(rural, (0, 0, 256, 256), signals=np.zeros((0, 2))), [])
        self.assertEqual(len(furniture.junction_props(rural, (0, 0, 256, 256), signals=np.array([[100.0, 100.0]]))), 4,
                         "a mapped signal at a rural junction")

    def test_mapped_signals_from_the_infrastructure_layer(self):
        import geodesy
        import mapgeom
        frame = geodesy.EnuFrame(62.6, 29.76, 0.0)
        ll = np.array([[29.7601, 62.6001], [29.7602, 62.6002], [29.7603, 62.6003]])
        f = mapgeom.Features(layer="infrastructure", ids=np.array(["s1", "b1", "s2"]),
                             kind=np.full(3, mapgeom.POINT, np.uint8), part_off=np.arange(4, dtype=np.int64),
                             ring_off=np.arange(4, dtype=np.int64), coord_off=np.arange(4, dtype=np.int64), lonlat=ll,
                             attrs={"subtype": np.array(["transportation", "pedestrian", "transportation"]),
                                    "class": np.array(["traffic_signals", "bench", "traffic_signals"])}, rules={})
        got = furniture.mapped_signals(frame, [f, f])
        self.assertEqual(len(got), 2, "the two signals, once each, not the bench")
        self.assertIsNone(furniture.mapped_signals(frame, [None]), "no layer read: guessed")

    def test_divided_road_crossing_one_signal_per_way_in(self):
        # Boston St: a divided road (two one-way carriageways 20 m apart) crossing a street is two junctions; the
        # user saw ten heads there (2026-10-14). One per way in: none on the arms leaving, none across the median.
        def seg(fid, cls, a, b, oneway=False):
            sp = roads.Spec(10.0, "paved", True, cls, oneway, 2.5, 1 if oneway else 0)
            return (fid, sp, np.array([(a[0], a[1], 0.0), (b[0], b[1], 0.0)]), 0.0)
        rs = [seg("eb_w", "primary", (20, 100), (100, 100), True), seg("eb_e", "primary", (100, 100), (180, 100), True),
              seg("wb_e", "primary", (180, 120), (100, 120), True), seg("wb_w", "primary", (100, 120), (20, 120), True),
              seg("s", "tertiary", (100, 100), (100, 20)), seg("link", "tertiary", (100, 100), (100, 120)),
              seg("n", "tertiary", (100, 120), (100, 200))]
        rows = furniture.junction_props(rs, (0, 0, 256, 256))
        self.assertEqual([r[0] for r in rows], [furniture.G_SIGNAL] * 4, "the two carriageways' and the street's ways in")

    def test_left_hand_traffic_stands_them_on_the_left(self):
        rs = [self.arm("e", "residential", 1, 0), self.arm("w", "residential", -1, 0), self.arm("s", "residential", 0, -1)]
        rows = furniture.junction_props(rs, (0, 0, 256, 256), left_hand=True)
        self.assertEqual(len(rows), 1)
        self.assertLess(rows[0][1], 100.0, "on the left of northbound drivers (west side)")

    @staticmethod
    def front(yaw):
        """The way a junction prop's face looks after its yaw (the stock boom's and the signs' local -y; _yaw turns local
        +x toward (cos, sin) of the yaw, as trees.lua and navgraph.lua turn them): the way right-hand traffic's props have
        always faced their drivers."""
        a = math.radians(yaw)
        return np.array([math.sin(a), -math.cos(a)])

    def test_left_hand_traffic_faces_the_drivers(self):
        """GitHub #595 (the UK: "every time you pass a traffic light, it's facing away from the driver"): the yaw was
        turned with the kerb side for left-hand traffic, which stood every head and sign with its back to the drivers.
        Each faces the drivers coming in (out along the arm the approach comes from) on either side of the road."""
        cross = [self.arm(f"a{i}", "tertiary", dx, dy) for i, (dx, dy) in enumerate([(1, 0), (-1, 0), (0, 1), (0, -1)])]
        tee = [self.arm("e", "residential", 1, 0), self.arm("w", "residential", -1, 0),
               self.arm("s", "residential", 0, -1)]
        for rs, what in ((cross, furniture.G_SIGNAL), (tee, furniture.G_STOP)):
            for lh in (False, True):
                rows = furniture.junction_props(rs, (0, 0, 256, 256), left_hand=lh)
                self.assertTrue(rows and all(r[0] == what for r in rows), (what, lh))
                for r in rows:
                    # the arm it stands at: the one whose direction from the junction is nearest the prop's
                    away = np.array([r[1] - 100.0, r[2] - 100.0])
                    u = max(([1, 0], [-1, 0], [0, 1], [0, -1]), key=lambda v: float(np.dot(v, away)))
                    self.assertGreater(float(np.dot(self.front(r[4]), u)), 0.99,
                                       f"{what} at {r[1:3]} (left-hand: {lh}) faces the drivers coming in")

    def test_left_hand_signals_keep_their_boom_over_the_road(self):
        """The stock boom's arm reaches to the drivers' left from its pole (a turn cannot mirror it): facing them, its
        pole stands on their right in left-hand traffic too, so the arm hangs over the road, not the pavement. The signs
        stand on the left (test_left_hand_traffic_stands_them_on_the_left)."""
        rs = [self.arm(f"a{i}", "tertiary", dx, dy) for i, (dx, dy) in enumerate([(1, 0), (-1, 0), (0, 1), (0, -1)])]
        key = lambda r: (r[1], r[2], r[4])
        self.assertEqual(sorted(map(key, furniture.junction_props(rs, (0, 0, 256, 256), left_hand=True))),
                         sorted(map(key, furniture.junction_props(rs, (0, 0, 256, 256)))))

    def test_approach_pose(self):
        u = np.array([0.0, -1.0])           # the arm to the south: northbound drivers come in
        for what in (furniture.G_SIGNAL, furniture.G_STOP, furniture.G_YIELD):
            rh, lh = furniture.approach_pose(u, what), furniture.approach_pose(u, what, left_hand=True)
            self.assertEqual(rh[1], lh[1], f"{what}: one yaw on either side of the road")
            np.testing.assert_allclose(rh[0], [1.0, 0.0], err_msg="right-hand: the east kerb")
            np.testing.assert_allclose(lh[0], [1.0, 0.0] if what == furniture.G_SIGNAL else [-1.0, 0.0])

    def test_stop_on_the_stem_of_a_minor_t(self):
        rs = [self.arm("e", "residential", 1, 0), self.arm("w", "residential", -1, 0), self.arm("s", "residential", 0, -1)]
        rows = furniture.junction_props(rs, (0, 0, 256, 256))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], furniture.G_STOP)
        self.assertLess(rows[0][2], 100.0, "on the southern stem")
        self.assertGreater(rows[0][1], 100.0, "on its right for northbound drivers (east side)")

    def test_sweden_has_give_way_where_a_street_meets_a_main_road(self):
        rs = [self.arm("e", "primary", 1, 0), self.arm("w", "primary", -1, 0), self.arm("s", "residential", 0, -1)]
        self.assertEqual([r[0] for r in furniture.junction_props(rs, (0, 0, 256, 256))], [furniture.G_STOP])
        rows = furniture.junction_props(rs, (0, 0, 256, 256), country="SE")
        self.assertEqual([(r[0], r[6]) for r in rows], [(furniture.G_B1, furniture.B1_TOP)])
        self.assertLess(rows[0][2], 100.0, "on the side road")

    def test_sweden_keeps_the_right_hand_rule_between_minor_roads(self):
        t = [self.arm("e", "residential", 1, 0), self.arm("w", "residential", -1, 0), self.arm("s", "residential", 0, -1)]
        self.assertEqual(furniture.junction_props(t, (0, 0, 256, 256), country="SE"), [])

    def test_sweden_has_give_way_in_the_country_too(self):
        rs = [self.arm("e", "primary", 1, 0), self.arm("w", "primary", -1, 0), self.arm("s", "residential", 0, -1)]
        rs = [(fid, spec._replace(urban=False), pts, lon) for fid, spec, pts, lon in rs]
        self.assertEqual(furniture.junction_props(rs, (0, 0, 256, 256)), [], "elsewhere: no signs in the country")
        self.assertEqual([r[0] for r in furniture.junction_props(rs, (0, 0, 256, 256), country="SE")], [furniture.G_B1])

    def test_sweden_a_lesser_main_road_gives_way_too(self):
        arms = [self.arm(f, c, x, y) for f, c, x, y in (("e", "primary", 1, 0), ("w", "secondary", -1, 0), ("s", "tertiary", 0, -1))]
        arms = [(fid, spec, np.array([x, y], float), pts[0]) for (fid, spec, pts, _), (x, y) in zip(arms, ((1, 0), (-1, 0), (0, -1)))]
        got = furniture.junction_controls(arms, None, signals=np.zeros((0, 2)), country="SE")
        self.assertEqual([(a[0], w) for a, w in got], [("s", furniture.G_YIELD)])

    def test_b1_height_is_the_models(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import build_median_props as props
        self.assertAlmostEqual(furniture.B1_TOP, props.B1_TOP, 2)
        edge = props.b1_outline(8)
        self.assertAlmostEqual(max(x for x, _ in edge) - min(x for x, _ in edge), props.B1_W, 3,
                               "S1 is measured across the rounded corners")
        self.assertAlmostEqual(max(z for _, z in edge) - min(z for _, z in edge), props.B1_TOP - props.B1_LOW, 3)
        self.assertGreaterEqual(props.B1_LOW, 1.0)      # TSFS 2019:74 1 kap. 13 §: 1.0-2.0 m up
        self.assertLessEqual(props.B1_LOW, 2.0)

    def test_b1_face_is_on_the_sheet(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import build_median_props as props
        from PIL import Image
        sheet = np.asarray(Image.open(Path(props.OUT) / "bne_se_signs.png").convert("RGB")).astype(int)
        px = lambda x, z: sheet[int(props.B1_CELL[1] - z * props.B1_PX), int(props.B1_CELL[0] + x * props.B1_PX)]  # noqa: E731
        r, g, b = px(0.0, 0.05)
        self.assertTrue(r > 200 and g > 170 and b < 60, "the middle is yellow")
        for x, z in props.b1_outline(2):
            r, g, b = px(x * 0.97, z * 0.97)
            self.assertTrue(r > 200 and g < 110, "the edge is the border's red")
        self.assertEqual(max(abs(z) for _, z in props.b1_outline(6)) * props.B1_PX // 1, 107.0, "405 px of 458 at 244")



class SignAssetsTest(unittest.TestCase):
    """The km/h sign set is written in three places (furniture.SIGN_KMH, the mod's vienna_convention_speed_signs.lua
    LIMITS, the generator's defaults); a number in one and not another is a sign that is never placed or a model that
    is missing."""
    ROOT = Path(__file__).resolve().parents[2]

    def test_server_catalog_and_generator_agree(self):
        lua = (self.ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/vienna_convention_speed_signs.lua"
               ).read_text(encoding="utf-8")
        limits = re.search(r"^M\.LIMITS = \{([^}]*)\}", lua, re.M)
        catalog = {int(n) for n in re.findall(r"\d+", limits.group(1))}
        self.assertRegex(lua, r"M\.NONE = 'speedknone'")
        tree = ast.parse((self.ROOT / "tools/generate_kmh_speed_signs.py").read_text(encoding="utf-8"))
        defaults = next(ast.literal_eval(s.value) for s in tree.body
                        if isinstance(s, ast.Assign) and any(getattr(t, "id", "") == "DEFAULT_SPEEDS" for t in s.targets))
        generated = {s for s in defaults if isinstance(s, int)}
        self.assertEqual(set(furniture.SIGN_KMH), catalog)
        self.assertEqual(set(furniture.SIGN_KMH), generated)
        self.assertIn("none", defaults)
        art = self.ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth"
        for n in [str(k) + "kph" for k in furniture.SIGN_KMH] + ["_none"]:
            self.assertTrue((art / f"sign_speed{n}.dae").exists() and (art / f"sign_speed{n}.png").exists(), n)


class RoundaboutTest(unittest.TestCase):
    def test_no_signals_on_a_ring_entry(self):
        import numpy as np
        ring = roads.Spec(7.0, "paved", True, "primary", True, 2.5, 1)
        app = roads.Spec(9.0, "paved", True, "primary", False)
        u = lambda x, y: np.array([x, y], float)
        arms = [("r1", ring, u(0, 1), u(0, 0)), ("r2", ring, u(0, -1), u(0, 0)), ("a", app, u(1, 0), u(0, 0))]
        self.assertTrue(furniture.junction_controls(arms, None), "three major arms: signals without the ring")
        self.assertEqual(furniture.junction_controls(arms, None, ring={"r1", "r2"}),
                         [(arms[2], furniture.G_YIELD)], "give-way on the entry, no signals on the ring")


class UnderDeckTest(unittest.TestCase):
    def test_tall_props_and_wires_under_a_deck_go(self):
        import furniture
        deck = (np.array([[-50.0, 0.0, 8.0], [50.0, 0.0, 8.0]]), 5.5)          # a bridge 8 m up, 11 m wide
        rows = [["streetlight", 0.0, 2.0, 0.0, 0, 1, 9.0, 0],      # under it, 9 m tall: through the deck
                ["hydrant", 0.0, -2.0, 0.0, 0, 1, 0.9, 0],         # under it, low: fits
                ["streetlight", 0.0, 20.0, 0.0, 0, 1, 9.0, 0],     # beside it
                ["pole", 0.0, 6.0, 0.0, 0, 1, 9.2, 0]]             # within DECK_SIDE_M of its edge
        wires = [np.c_[np.zeros(13), np.linspace(-30, 30, 13), np.full(13, 7.9)],   # across it (5 m points)
                 np.array([[30.0, 10.0, 7.9], [60.0, 10.0, 7.9]])]  # clear of it
        out = furniture.under_decks(rows, [deck], wires)
        self.assertEqual([r[0] for r in out], ["hydrant", "streetlight"])
        self.assertEqual(out[1][2], 20.0)
        self.assertEqual(len(wires), 1)
        self.assertEqual(float(wires[0][0, 0]), 30.0)

    def test_props_on_the_deck_stay(self):
        import furniture
        deck = (np.array([[-50.0, 0.0, 8.0], [50.0, 0.0, 8.0]]), 5.5)
        rows = [["streetlight", 0.0, 4.0, 8.2, 0, 1, 9.0, 0]]       # on the deck itself (its own lamp)
        self.assertEqual(furniture.under_decks(rows, [deck]), rows)



if __name__ == "__main__":
    unittest.main()


class SignClearance(unittest.TestCase):
    def test_moved_off_a_wide_road_beside_the_junction(self):
        # the spot 'past the junction, right of the approach' lies on a 16 m road running beside it (Boston Street,
        # Baltimore, 2026-10-17): the sign moves along / out until clear, never onto a carriageway
        wide = (3.0, np.array([[-50.0, -4.0], [50.0, -4.0]]))     # crossing the approach just past the junction
        own = (3.0, np.array([[0.0, 0.0], [0.0, -60.0]]))         # the approach, away from the junction along -y
        q = furniture._off_carriageways(np.array([0.0, 0.0]), np.array([0.0, -1.0]), np.array([1.0, 0.0]), 2.0, 4.0,
                                        [wide, own])
        self.assertIsNotNone(q)
        for h, ln in (wide, own):
            self.assertGreaterEqual(furniture._seg_dist_min(q, ln), h + furniture.SIGN_CLEAR_M - 1e-9)

    def test_nowhere_clear_no_sign(self):
        big = (40.0, np.array([[-100.0, 0.0], [100.0, 0.0]]))
        self.assertIsNone(furniture._off_carriageways(np.zeros(2), np.array([0.0, 1.0]), np.array([1.0, 0.0]),
                                                      2.0, 4.0, [big]))


class StreetTreesOffBuildings(unittest.TestCase):
    def test_tree_in_a_rowhouse_goes_others_stay(self):
        """A sidewalk run a metre into a rowhouse row (Canton, Baltimore): its street tree goes, one clear of the
        walls stays, and a lamp in the same footprint is not this rule's."""
        from types import SimpleNamespace
        house = SimpleNamespace(ring=np.array([[10.0, 10.0], [30.0, 10.0], [30.0, 25.0], [10.0, 25.0]]))   # (tile-local)
        rows = [[furniture.G_STREET_TREE, 120.0, 211.0, 0.0, 0.0, 1.0, 7.0, 0.5],     # 1 m into the house
                [furniture.G_STREET_TREE, 120.0, 209.5, 0.0, 0.0, 1.0, 7.0, 0.5],     # 0.5 m from its wall
                [furniture.G_STREET_TREE, 145.0, 205.0, 0.0, 0.0, 1.0, 7.0, 0.5],     # clear
                [furniture.G_LAMP, 115.0, 215.0, 0.0, 0.0, 1.0, 9.0, 0.0]]
        out = furniture.off_buildings(rows, [house.ring + [100.0, 200.0]])
        self.assertEqual([(r[0], r[1], r[2]) for r in out],
                         [(furniture.G_STREET_TREE, 145.0, 205.0), (furniture.G_LAMP, 115.0, 215.0)])
