"""fork: Australia's streetscape (boundary fences, kerb and channel, power poles and lamps, roundabout centres). Every other
country must come out exactly as before: test_nature_strips holds the sidewalk / paint digests, here the furniture, fence and
roundabout rows are compared with the rows the unchanged rules give."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # (test_roads, also run as tests.test_au_streetscape)
import buildings  # noqa: E402
import furniture  # noqa: E402
import islands  # noqa: E402
import roads  # noqa: E402
import surface as sf  # noqa: E402
from test_roads import FRAME, features, flat  # noqa: E402

S = roads.Spec
URBAN = lambda la, lo: np.ones(np.shape(la), bool)      # noqa: E731
STRAIGHT = np.array([[0.0, 0, 0], [118, 0, 0]])


class FenceTest(unittest.TestCase):
    def test_the_front_fence_swaps_its_group_in_australia_only(self):
        d = np.array([1.0, 0.0])
        a = np.array([-6.0, 22.0])
        args = (a, d, 12.0, 0.5, np.array([-40.0, 0.0]), np.array([80.0, 0.0]), 6400.0, 3.5, 10.0, False)
        for au, want in ((None, {buildings.G_FENCE}), (True, {g for g, _ in buildings.AU_FENCE_COLORBOND
                                                              + buildings.AU_FENCE_PALING})):
            spec = buildings.Spec(seed=("fence", 3), au=au)
            rows = buildings._front_fence(spec, *args)
            self.assertTrue(rows, "a fence")
            self.assertTrue({r[0] for r in rows} <= want, (au, {r[0] for r in rows}))
            if au:
                self.assertEqual(len({r[0] for r in rows}), 1, "one kind and colour a house")
            else:
                self.assertTrue(all(r[6] == 1.3 for r in rows), "the stock fence is untouched")
    def test_side_and_rear_fences_start_at_the_front_building_line(self):
        """The user, in game: a lone paling panel in a front yard looked wrong; the tall fences are on the side and rear boundaries."""
        ring = np.array([(-6.0, 22.0), (6.0, 22.0), (6.0, 31.0), (-6.0, 31.0)])
        spec = buildings.Spec(seed=("fence", 3), au=True)
        roads_ = (np.array([[-40.0, 0.0]]), np.array([[80.0, 0.0]]), np.array([3.5]))
        args = (spec, ring, ring[0], np.array([1.0, 0.0]), 12.0, np.array([0.0, -1.0]), 10.0)
        rows = buildings._au_side_fences(*args, [(spec, ring)], ring.mean(0)[None], roads_)
        self.assertGreater(len(rows), 6)
        self.assertEqual(len({r[0] for r in rows}), 1, "one kind and colour a house")
        self.assertTrue(all(r[0].startswith("au_fence") and r[6] == buildings.AU_FENCE_H for r in rows))
        self.assertTrue(all(r[2] >= 22.0 for r in rows), "none in the front yard")
        sides = [r for r in rows if abs(abs(r[1]) - 6.0 - buildings.AU_SIDE_GAP_M) < 0.01]
        self.assertGreaterEqual(len(sides), 4, "both sides")
        self.assertTrue(any(abs(r[2] - (31.0 + buildings.AU_REAR_M)) < 0.01 for r in rows), "and the rear")
        # a neighbour's house in the way takes that piece out
        other = np.array([(7.0, 24.0), (19.0, 24.0), (19.0, 33.0), (7.0, 33.0)])
        some = buildings._au_side_fences(*args, [(spec, ring), (spec, other)], np.array([ring.mean(0), other.mean(0)]), roads_)
        self.assertLess(len(some), len(rows))
        self.assertTrue(all(not (7.0 < r[1] < 19.0 and 24.0 < r[2] < 33.0) for r in some))

    def test_a_house_in_australia_has_tall_fences_behind_its_front_wall_only(self):
        road = [(np.array([(-80.0, -14.0), (80.0, -14.0)]), 4.0)]
        tall = low = houses = 0
        for lat, lon, au in ((-27.47, 153.03, True), (40.0, -75.0, False)):
            for i in range(60):
                ring = np.array([[0, 0], [10.0, 0], [10.0, 12.0], [0, 12.0]], float)
                spec = buildings.plan(ring, {"class": "house", "seed_id": i}, lat=lat, lon=lon)
                if spec.roof == "flat":
                    continue
                rows = buildings.doors([(spec, 0.0, 0.0)], road, 0.0, 0.0)
                fences = [r for r in rows if str(r[0]).startswith("au_fence")]
                if not au:
                    self.assertFalse(fences)
                    continue
                houses += 1
                self.assertTrue(all(r[2] >= -0.01 or r[6] == buildings.AU_FRONT_H for r in fences),
                                "in front of the house: only the low fence")
                tall += sum(r[6] == buildings.AU_FENCE_H for r in fences)
                low += sum(r[6] == buildings.AU_FRONT_H for r in fences)
        self.assertGreater(houses, 10)
        self.assertGreater(tall, 3 * houses)
        self.assertLess(low, tall)

    def test_plan_marks_australian_footprints(self):
        ring = np.array([(0.0, 0.0), (12.0, 0.0), (12.0, 9.0), (0.0, 9.0)])
        for lat, lon, want in ((-27.47, 153.03, True), (40.0, -75.0, None), (51.5, -0.1, None), (-36.85, 174.76, None)):
            self.assertEqual(buildings.plan(ring, {"class": "house"}, 0, lat, lon).au, want, (lat, lon))


class ChannelTest(unittest.TestCase):
    def ex(self, spec, country, lon=153.0):
        return roads.extras(spec, STRAIGHT, lon, country=country, fid="x")

    def gutters(self, ex):
        return [e for e in ex if e[0]["m"] == roads.GUTTER["m"]]

    def test_australian_channel_is_concrete_and_wider_than_the_us_gutter(self):
        for cls, w in (("residential", 7.0), ("tertiary", 8.0), ("primary", 10.0)):
            sp = S(w, "paved", True, cls, False)
            g = self.gutters(self.ex(sp, "AU"))
            self.assertEqual(len(g), 2, cls)
            for look, width, line in g:
                self.assertEqual(width, roads.AU_CHANNEL_W)
                chunk = roads._chunk(look, width, line)
                self.assertEqual((chunk["m"], chunk["layer"]), (roads.AU_CHANNEL_M, "gutter"))

    def test_other_countries_keep_the_decal(self):
        sp = S(8.0, "paved", True, "tertiary", False)
        for country in ("US", "GB", "NZ", None):
            for look, width, line in self.gutters(self.ex(sp, country)):
                self.assertNotIn("au_m", look)
                self.assertEqual(width, roads.GUTTER["w"])
                chunk = roads._chunk(look, width, line)
                self.assertEqual(chunk["m"], roads.GUTTER["m"])
                self.assertNotIn("layer", chunk)

class PoleTest(unittest.TestCase):
    def test_australia_has_its_own_lamp_region(self):
        self.assertEqual(furniture.lamp_region("AU", "AU-QLD"), "au")
        self.assertEqual(furniture.lamp_region("US", "US-TX"), "na")
        self.assertIsNone(furniture.lamp_region("DE"))
        self.assertLess(furniture.buried_share("AU"), 0.5, "mostly overhead")

    def test_concrete_poles_on_main_roads_timber_in_the_suburbs(self):
        for cls in ("trunk", "primary", "secondary"):
            self.assertEqual(furniture.pole_group("au", cls), furniture.G_POLE_CONC)
        for cls in ("residential", "tertiary", "unclassified", "living_street"):
            self.assertEqual(furniture.pole_group("au", cls), furniture.G_POLE)
        for region in (None, "na", "na:sparse"):
            for cls in ("primary", "residential"):
                self.assertEqual(furniture.pole_group(region, cls), furniture.G_POLE, (region, cls))

    def test_a_lamp_on_every_second_local_pole(self):
        self.assertEqual([furniture.pole_lamp("residential", k, "au") for k in range(4)], [True, False, True, False])
        self.assertTrue(all(furniture.pole_lamp("primary", k, "au") for k in range(4)))
        # the others' rules are as they were
        self.assertTrue(all(furniture.pole_lamp("residential", k, None) for k in range(4)))
        self.assertEqual([furniture.pole_lamp("residential", k, "na") for k in range(4)], [True, False, True, False])

    def place(self, cls, region, country):
        line = np.array([[0.0, 0, 0], [60, 0, 0], [118, 0, 0]])
        walk = {"fid": "a", "side": 1, "w": 2.5, "pts": np.array([[x, 4.9, 0.0] for x in np.arange(0.0, 119.0, 3.0)])}
        sp = S(7.0, "paved", True, cls, False)
        return furniture.place([("a", sp, line, 153.0)], [walk], (-10.0, -10.0, 130.0, 20.0), town=True, region=region,
                               country=country, buried=0.0)

    def test_place_uses_the_regions_poles(self):
        au = self.place("primary", "au", "AU")
        self.assertIn(furniture.G_POLE_CONC, {r[0] for r in au})
        self.assertNotIn(furniture.G_POLE, {r[0] for r in au})
        sub = self.place("residential", "au", "AU")
        self.assertIn(furniture.G_POLE, {r[0] for r in sub})
        self.assertNotIn(furniture.G_POLE_CONC, {r[0] for r in sub})
        self.assertIn(furniture.G_POLELAMP, {r[0] for r in sub}, "cobra-head lamps on the poles")
        us = self.place("primary", "na", "US")
        self.assertEqual({r[0] for r in us} & {furniture.G_POLE_CONC}, set())


def ring_features(r=16.0):
    n = 24
    th = np.linspace(0, 2 * math.pi, n + 1)
    half = n // 2
    a = [(r * math.cos(t), r * math.sin(t)) for t in th[:half + 1]]
    b = [(r * math.cos(t), r * math.sin(t)) for t in th[half:]]
    app = [(r, 0.0), (r + 60.0, 0.0)]
    f = features([a, b, app], ["residential"] * 3, ids=["r1", "r2", "app"])
    return f


class RoundaboutTest(unittest.TestCase):
    def found(self, r=16.0):
        ow = S(6.0, "paved", True, "residential", True)
        two = S(9.0, "paved", True, "residential", False)
        t1 = np.linspace(0, math.pi, 14)
        t2 = np.linspace(math.pi, 2 * math.pi, 14)
        arc = lambda t: np.stack([r * np.cos(t), r * np.sin(t), np.zeros(len(t))], 1)      # noqa: E731
        return islands.find([("r1", ow, arc(t1), 0.0), ("r2", ow, arc(t2), 0.0),
                             ("app", two, np.array([(r, 0.0, 0.0), (r + 60.0, 0.0, 0.0)]), 0.0)])

    def test_a_garden_inside_the_curb(self):
        f = self.found()
        self.assertEqual(len(f), 1)
        rows = islands.au_garden_rows(f, (-40.0, -40.0, 40.0, 40.0))
        self.assertTrue({"deciduous", "shrub"} <= {r[0] for r in rows})
        ring = f[0]["ring"]
        c = ring[:, :2].mean(0)
        r_in = float(np.min(np.hypot(*(ring[:, :2] - c).T)))
        for g, x, y, z, yaw, sc, h, rank in rows:
            self.assertLess(math.hypot(x - c[0], y - c[1]), r_in - islands.GARDEN_EDGE_M + 1e-6, "inside, off the curb")

    def test_the_same_plants_from_every_tile_each_made_once(self):
        f = self.found()
        whole = islands.au_garden_rows(f, (-40.0, -40.0, 40.0, 40.0))
        halves = islands.au_garden_rows(f, (-40.0, -40.0, 0.0, 40.0)) + islands.au_garden_rows(f, (0.0, -40.0, 40.0, 40.0))
        self.assertEqual(sorted(map(tuple, whole)), sorted(map(tuple, halves)))
        self.assertEqual(whole, islands.au_garden_rows(self.found(), (-40.0, -40.0, 40.0, 40.0)), "deterministic")

    def test_a_tiny_island_has_no_garden_and_ground_sets_the_height(self):
        self.assertEqual(islands.au_garden_rows(self.found(5.0), (-40.0, -40.0, 40.0, 40.0)), [])
        rows = islands.au_garden_rows(self.found(), (-40.0, -40.0, 40.0, 40.0), ground=lambda x, y: x * 0.0 + 42.0)
        self.assertTrue(all(r[3] == 42.0 for r in rows))

    def test_build_tile_plants_them_in_australia_only(self):
        f = ring_features()
        for country, au in (("AU", True), ("US", False), ("GB", False), (None, False)):
            props, walks = [], []
            roads.build_tile(FRAME, 0, 0, [f], flat, URBAN, walks=walks, props=props, country=country)
            plants = [p for p in props if p[0] in ("shrub", "deciduous")]
            self.assertEqual(bool(plants), au, country)

    def test_photo_fill_is_mown_grass_in_australia_whatever_the_photo(self):
        rgb = np.full((40, 40, 3), (150, 120, 90), np.uint8)       # a bare, brown island photo
        poly = [[(8.0, 8.0), (32.0, 8.0), (32.0, 32.0), (8.0, 32.0)]]
        au = sf._fill_island_photos(rgb, poly, au=True)
        self.assertEqual(tuple(au[20, 20]), sf.ISLAND_GRASS_AU)
        self.assertEqual(tuple(au[2, 2]), (150, 120, 90), "outside the footprint untouched")
        r, g, b = (int(v) for v in sf.ISLAND_GRASS_AU)
        self.assertGreater(2 * g - r - b, sf.LAWN_GREEN, "green enough for photo_lawns to keep it lawn")
        other = sf._fill_island_photos(rgb, poly)
        self.assertEqual(tuple(other[20, 20]), (150, 120, 90), "elsewhere: as upstream's (too few greens: a paved island, its photo as it is)")


if __name__ == "__main__":
    unittest.main()
