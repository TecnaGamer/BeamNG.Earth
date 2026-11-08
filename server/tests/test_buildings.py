"""buildings: footprints become closed walls and a roof, heights predicted where the data has none,
deterministic, every triangle facing out."""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildings as bld  # noqa: E402
import cdae  # noqa: E402
import threedmr  # noqa: E402


def rect(x, y, w, d):
    return np.array([(x, y), (x + w, y), (x + w, y + d), (x, y + d)], np.float64)


class PolygonTest(unittest.TestCase):
    def test_clean_ring_ccw_without_collinear(self):
        p = np.array([(0, 0), (0, 10), (5, 10.1), (10, 10), (10, 0), (0, 0)], np.float64)    # clockwise, closed
        r = bld.clean_ring(p)
        self.assertEqual(len(r), 4)
        self.assertGreater(bld._area2(r), 0)

    def test_triangulate_concave(self):
        L = np.array([(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)], np.float64)
        t = bld.triangulate(L)
        self.assertEqual(len(t), 4)
        area = sum(abs(bld._area2(L[[a, b, c]])) / 2 for a, b, c in t)
        self.assertAlmostEqual(area, bld._area2(L) / 2)


class RegionStyleTest(unittest.TestCase):
    """Building habits by region (2026-10-15: Swiss villages had come out as American clapboard houses)."""

    def houses(self, lat, lon, n=400):
        rng = np.random.default_rng(5)
        out = []
        for i in range(n):
            w, l = rng.uniform(7, 11), rng.uniform(9, 14)
            ring = np.array([[0, 0], [w, 0], [w, l], [0, l]], float) + rng.uniform(-900, 900, 2)
            out.append(bld.plan(ring, {"class": "house"}, lat=lat, lon=lon))
        return out

    def test_regions(self):
        self.assertEqual(bld.region_style(39.29, -76.61).name, "us_east")
        self.assertEqual(bld.region_style(37.36, -118.39).name, "us_west")
        self.assertEqual(bld.region_style(46.59, 7.91).name, "alpine")
        self.assertEqual(bld.region_style(52.37, 4.90).name, "europe_north")
        self.assertEqual(bld.region_style(40.42, -3.70).name, "mediterranean")
        self.assertEqual(bld.region_style(19.43, -99.13).name, "latin_america")
        self.assertEqual(bld.region_style(35.66, 139.70).name, "east_asia")
        self.assertEqual(bld.region_style(-33.87, 151.21).name, "oceania")
        self.assertEqual(bld.region_style(30.9, 75.8).name, "south_asia")
        self.assertEqual(bld.region_style(0.0, 25.0).name, "warm")
        self.assertEqual(bld.region_style(19.7, 96.1).name, "warm", "Myanmar: not South Asia's style")
        self.assertEqual(bld.brick_share(39.29, -76.61), 0.75)      # the US as before
        self.assertEqual(bld.brick_share(30.0, -90.0), 0.35)
        self.assertEqual(bld.brick_share(40.0, -110.0), 0.15)

    def test_south_asia_houses(self):
        hs = self.houses(28.61, 77.21)                  # Delhi, India
        self.assertFalse(any(h.porch for h in hs), "no American porches in South Asia")
        flat = [h for h in hs if h.roof == "flat"]
        self.assertGreater(len(flat), 0.85 * len(hs), "overwhelmingly flat terrace roofs")
        self.assertTrue(all(not str(h.wall).startswith(bld.SIDING) for h in hs), "no wooden clapboard siding")

    def test_alpine_houses(self):
        hs = self.houses(46.59, 7.91)
        self.assertFalse(any(h.porch for h in hs), "no porches in the Alps")
        self.assertTrue(any(str(h.wall).startswith(f"{bld.SIDING}|w") for h in hs), "wooden chalets")
        self.assertFalse(any(h.wall and str(h.wall).startswith(bld.SIDING) and "|w" not in str(h.wall) for h in hs))
        pitched = [h for h in hs if h.roof != "flat"]
        self.assertGreater(len(pitched), 0.8 * len(hs))
        self.assertTrue(all(h.pitch == 24.0 for h in hs))
        for h in pitched:
            mat, k = h.roof_mat.split("|")
            self.assertIn(int(k), (5, 7, 8, 9))     # dark or brown roofs

    def test_mediterranean_and_latin_houses(self):
        med = self.houses(40.42, -3.70)
        for h in med:
            if h.roof != "flat":
                self.assertIn(int(h.roof_mat.split("|")[1]), (4, 5, 6), "clay tile roofs")
        self.assertTrue(any(h.roof == "flat" for h in med))
        lat = self.houses(19.43, -99.13)
        self.assertGreater(sum(h.roof == "flat" for h in lat), 0.5 * len(lat))
        self.assertTrue(any(h.wall and h.wall.startswith(bld.STUCCO) and int(h.wall.split("|")[1]) >= 5 for h in lat),
                        "painted walls")

    def test_attached_old_town_roofs(self):
        """Terraced houses: flat in Baltimore, mostly gabled in Amsterdam."""
        rng = np.random.default_rng(9)

        def gabled(lat, lon):
            n = 0
            for i in range(200):
                ring = np.array([[0, 0], [6, 0], [6, 15], [0, 15]], float) + rng.uniform(-900, 900, 2)
                n += bld.plan(ring, {"class": "terrace"}, neighbours=2, lat=lat, lon=lon).roof != "flat"
            return n
        self.assertEqual(gabled(39.28, -76.59), 0)
        self.assertGreater(gabled(52.37, 4.89), 100)

    def test_us_houses_keep_their_tints(self):
        for h in self.houses(39.29, -76.61):
            if h.roof != "flat":
                self.assertLess(int(h.roof_mat.split("|")[1]), 4)
            if h.wall and h.wall.startswith(bld.STUCCO):
                self.assertLess(int(h.wall.split("|")[1]), 5)
            self.assertIsNone(h.pitch)


class PlanTest(unittest.TestCase):
    def test_hut_above_the_tree_line_is_stone(self):
        # the user, 2026-10-14: Mount Whitney's stone summit shelter was a plain grey box
        self.assertAlmostEqual(bld.tree_line_m(36.6), 3600 - 110 * 4.6, places=6)
        self.assertGreater(bld.tree_line_m(46.0), 1900)
        self.assertLess(bld.tree_line_m(46.0), 2200)
        self.assertEqual(bld.tree_line_m(75.0), 0.0)
        hut = bld.plan(rect(0, 0, 4, 6), {"class": ""}, 0, 36.58, -118.29, alpine=True)
        self.assertTrue(str(hut.wall).startswith(bld.STONE))
        self.assertTrue(str(hut.facade).startswith(bld.STONE))
        town = bld.plan(rect(0, 0, 4, 6), {"class": ""}, 0, 36.58, -118.29)
        self.assertFalse(str(town.wall).startswith(bld.STONE), "the same hut down in a town: unchanged")
        big = bld.plan(rect(0, 0, 20, 20), {"class": ""}, 0, 36.58, -118.29, alpine=True)
        self.assertFalse(str(big.wall).startswith(bld.STONE), "a big building up there: unchanged")

    def test_given_height_kept(self):
        s = bld.plan(rect(0, 0, 20, 20), {"class": "", "height": 18.0})
        self.assertAlmostEqual(s.height, 18.0)
        self.assertEqual(s.floors, 6)

    def test_row_house_three_storeys_or_two_flat(self):
        s = bld.plan(rect(0, 0, 5, 18), {"class": ""}, neighbours=2)
        self.assertIn(s.floors, (2, 3))
        self.assertEqual(s.roof, "flat")

    def test_west_town_one_storey_main_street(self):
        shops = [bld.plan(rect(x * 30, 0, 7, 25), {"class": ""}, 2, 37.36, -118.39, west_town=True)
                 for x in range(40)]
        self.assertTrue(all(s.floors in (1, 2) for s in shops), "attached shops, not 2-4 storey row houses")
        self.assertGreater(sum(s.floors == 1 for s in shops), 20)
        one = next(s for s in shops if s.floors == 1)
        self.assertGreater(one.height, bld.FLOOR_M + 0.6 + 1.0, "a false front's parapet")
        city = [bld.plan(rect(x * 30, 0, 5, 18), {"class": ""}, 2) for x in range(10)]
        self.assertTrue(all(s.floors >= 2 for s in city), "a city's row houses unchanged")
        given = bld.plan(rect(0, 0, 20, 20), {"class": "", "height": 18.0}, 0, west_town=True)
        self.assertEqual(given.floors, 6, "data heights win")

    def test_district_coverage(self):
        from test_roads import features
        f = features([[(0, 0), (10, 0)]], ["x"])
        lat, lon = f.lonlat[0, 1], f.lonlat[0, 0]
        # one 100 x 100 m square footprint at the centre, as a buildings layer
        sq = features([[(-50, -50), (50, -50), (50, 50), (-50, 50)]], ["x"])
        bld._cell_stats.clear()
        c = bld.district_coverage(lat, lon, [("k1", sq)])
        self.assertAlmostEqual(c, 10000.0 / (np.pi * bld.DISTRICT_M ** 2), delta=1e-4)
        self.assertAlmostEqual(bld.district_coverage(lat, lon, [("k1", None)]), c, msg="cached by cell")

    def test_parking_structure_not_a_barn(self):
        g = bld.plan(rect(0, 0, 60, 60), {"class": "parking"}, 0, 39.2, -76.86)
        self.assertGreaterEqual(g.floors, 4)
        self.assertFalse(g.facade.startswith(bld.SIDING), "garage panels, not barn siding")
        c = bld.plan(rect(0, 0, 8, 6), {"class": "parking"}, 0, 39.2, -76.86)
        self.assertEqual(c.floors, 1, "a carport")

    def test_small_house_pitched(self):
        roofs = {bld.plan(rect(x * 50, 0, 10, 8), {"class": "house"}).roof for x in range(20)}
        self.assertEqual(roofs, {"gabled", "hipped"})

    def test_big_box_blank_walls_one_storefront(self):
        s = bld.plan(rect(0, 0, 60, 40), {"class": "retail"}, 0, 38.57, -109.55, town=True)
        self.assertFalse(s.windows)
        self.assertTrue(s.wall.startswith(bld.PANEL))
        rows = bld.doors([(s, 0.0, 0.0)], [(np.array([(-50.0, -10.0), (200.0, -10.0)]), 6.0, "trunk", True)], 0.0, 0.0)
        self.assertEqual(rows, [])
        self.assertEqual(s.front, 0, "the south wall faces the road")
        P, UV, T, M, mats, C = bld.build([(s, 0.0, 0.0)])
        self.assertIn(bld.MATERIALS.index(bld.SHOP), set(M.tolist()))

    def test_named_kinds(self):
        big = rect(0, 0, 200, 60)
        t = bld.plan(big, {"class": "", "name": "Main Terminal"}, 0, 39.17, -76.67)
        self.assertEqual((t.floors, t.facade), (2, bld.OFFICE))
        g = bld.plan(big, {"class": "", "name": "Hourly Garage"}, 0, 39.17, -76.67)
        self.assertIn(g.floors, (4, 5))
        self.assertLess(g.height, 18.0)
        h = bld.plan(big, {"class": "", "name": "Hangar 4"}, 0, 39.17, -76.67)
        self.assertTrue(h.wall.startswith(bld.METALWALL))
        c = bld.plan(rect(0, 0, 30, 14), {"class": "church"}, 0, 37.36, -118.39)
        self.assertTrue(c.steeple)
        P = bld.build([(c, 0.0, 0.0)])[0]
        self.assertGreater(P[:, 2].max(), 25.0, "a spire over the nave")

    def test_dense_core_small_buildings_are_row_houses(self):
        s = bld.plan(rect(0, 0, 8, 12), {"class": "house"}, 0, 39.28, -76.59, dense=True)
        self.assertEqual(s.roof, "flat")
        self.assertIsNone(s.wall)

    def test_barn_gabled_metal(self):
        s = bld.plan(rect(0, 0, 30, 15), {"class": "barn"}, 0, 40.0, -76.1)
        self.assertEqual(s.roof, "gabled")
        self.assertTrue(s.roof_mat.startswith(bld.ROOF_METAL))
        self.assertGreater(s.height, 5.0)

    def test_deterministic(self):
        a = bld.plan(rect(3, 7, 12, 9), {"class": ""})
        b = bld.plan(rect(3, 7, 12, 9), {"class": ""})
        self.assertEqual((a.floors, a.roof, a.facade, a.band), (b.floors, b.roof, b.facade, b.band))


class TowerShopTest(unittest.TestCase):
    def test_city_towers_have_a_shop_ground_floor(self):
        # Midtown, 2026-10-14: classless towers had their curtain wall down to the pavement
        t = bld.plan(rect(0, 0, 40, 30), {"class": "", "height": 120.0}, 2, 40.758, -73.985, dense=True)
        self.assertTrue(t.shop)
        low = bld.plan(rect(0, 0, 40, 30), {"class": "", "height": 9.0}, 2, 40.758, -73.985, dense=True)
        self.assertFalse(low.shop, "a low building keeps its own rules")
        suburb = bld.plan(rect(0, 0, 40, 30), {"class": "", "height": 120.0}, 0, 40.758, -73.985)
        self.assertFalse(suburb.shop, "outside a dense core")


class MidriseTest(unittest.TestCase):
    def test_midrises_are_mostly_masonry_towers_glass(self):
        # Midtown's theatre district: 5-15 storey brick and stone, not glass (street photos 2026-10-14)
        mid = [bld.plan(rect(x * 40, 0, 25, 30), {"class": "", "height": 40.0}, 2, 40.76, -73.99, dense=True)
               for x in range(40)]
        glass = sum(1 for sp in mid if sp.facade.startswith(bld.OFFICE))
        self.assertEqual(glass, 0, "unclassed 12-storey buildings: masonry")
        towers = [bld.plan(rect(x * 40, 0, 40, 40), {"class": "", "height": 200.0}, 2, 40.76, -73.99, dense=True)
                  for x in range(20)]
        self.assertTrue(all(sp.facade.startswith(bld.OFFICE) for sp in towers), "60 floors: glass")
        old = [bld.plan(rect(x * 40, 0, 40, 40), {"class": "", "height": 90.0}, 2, 40.76, -73.99, dense=True)
               for x in range(60)]
        share = sum(1 for sp in old if sp.facade.startswith(bld.OFFICE)) / len(old)
        self.assertTrue(0.45 < share < 0.85, f"27 floors: mostly glass, some stone ({share:.2f})")
        tints = {sp.facade for sp in towers}
        self.assertGreaterEqual(len(tints), 4, "towers in several glass colours")


class ParkingDeckTest(unittest.TestCase):
    def deck(self, w, d, gmax=0.0):
        spec = bld.plan(rect(0, 0, w, d), {"class": "parking", "num_floors": 3, "height": 9.0}, 0, 39.28, -76.59)
        return spec, bld.build([(spec, 0.0, gmax)])

    def tops(self, P, T, z, tol=0.05):
        """Triangles lying flat at height z."""
        tri = P[T]
        return tri[(np.abs(tri[:, :, 2] - z) < tol).all(1)]

    def covered(self, tris, x, y):
        for a, b, c in tris[:, :, :2]:
            d = [(b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]),
                 (c[0] - b[0]) * (y - b[1]) - (c[1] - b[1]) * (x - b[0]),
                 (a[0] - c[0]) * (y - c[1]) - (a[1] - c[1]) * (x - c[0])]
            if min(d) >= -1e-9 or max(d) <= 1e-9:
                return True
        return False

    def test_skin_tower_and_entrance(self):
        # reports #51/#52 (2026-10-17): a brick or precast skin, a stair tower over the roof, a driveway from the road
        spec, (P, UV, T, M, mats, C) = self.deck(60, 40)
        used = {mats[k] for k in np.unique(M)}
        self.assertTrue(used & {bld.BRICK_WALL, bld.CONCRETE}, used)
        self.assertGreater(P[:, 2].max(), 9.0 + bld.STAIR_TOWER_UP - 0.2, "a stair tower over the top deck")
        # a road 8 m south of the garage's south edge (y = 0), running east-west
        road = (np.array([[-50.0, -12.0], [120.0, -12.0]]), 4.0)
        drives = bld.garage_drives([(spec, 0.0, 0.0)], [road], 0.0, 0.0)
        self.assertEqual(len(drives), 1)
        n = np.array(drives[0]["n"])[:, :2]
        self.assertGreater(n[:, 1].max(), 0.0, "into the ground deck")
        self.assertAlmostEqual(n[:, 1].min(), -12.0 + 4.1, places=1, msg="to the road's edge")

    def test_a_big_garage_is_open_decks_with_a_ramp_well(self):
        # the user, 2026-10-14: "garage buildings ... are solid"
        spec, (P, UV, T, M, mats, C) = self.deck(60, 40)
        self.assertTrue(spec.deck)
        zs = [0.03 + k * 3.0 for k in range(4)]
        for z in zs:
            self.assertGreaterEqual(len(self.tops(P, T, z)), 2, f"a deck at {z} m")
        # the longest edge is along x (y = 0): the well is cut from it on the upper decks, the ground deck is whole
        xs, ys = 30.0, bld.DECK_EDGE_M + 1.5 * bld.DECK_LANE_M        # the middle of the second lane
        self.assertTrue(self.covered(self.tops(P, T, zs[0]), xs, ys), "the ground deck under the ramps")
        self.assertFalse(self.covered(self.tops(P, T, zs[1]), xs, ys), "the well open on deck 1")
        self.assertTrue(self.covered(self.tops(P, T, zs[1]), 30.0, 30.0), "deck 1 beyond the well")
        self.assertEqual(bld.collider_boxes([(spec, 0.0, 0.0)], 0, 0), [], "driven into: no keep-out box")

    def test_deck_undersides_face_down(self):
        _, (P, UV, T, M, mats, C) = self.deck(60, 40)
        tri = P[T]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        flat_low = (np.abs(tri[:, :, 2] - (3.03 - bld.DECK_SLAB_M)) < 0.01).all(1)
        self.assertGreater(int(flat_low.sum()), 1, "deck 1 has an underside")
        self.assertTrue(np.all(n[flat_low][:, 2] < 0), "facing down")

    def test_ramps_are_drivable(self):
        _, (P, UV, T, M, mats, C) = self.deck(60, 40)
        tri = P[T]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        up = n[:, 2] / np.maximum(np.linalg.norm(n, axis=1), 1e-9)
        sloped = (up > 0.9) & (up < 0.9999)
        self.assertGreaterEqual(int(sloped.sum()), 6, "three ramps facing up")
        self.assertTrue(np.all(np.sqrt(1 - up[sloped] ** 2) / up[sloped] <= bld.DECK_SLOPE + 1e-6), "not too steep")

    def test_too_short_or_sloped_keeps_the_box(self):
        spec, (P, UV, T, M, mats, C) = self.deck(30, 30)
        self.assertNotEqual(bld.collider_boxes([(spec, 0.0, 0.0)], 0, 0), [], "falls back")
        self.assertFalse(len(self.tops(P, T, 3.03)), "no decks: the closed box")
        spec, (P, UV, T, M, mats, C) = self.deck(60, 40, gmax=4.0)
        self.assertFalse(len(self.tops(P, T, 7.03)), "on a slope: the closed box")


    def test_ramp_along_another_edge_when_the_longest_is_a_thin_leg(self):
        # an L whose longest edge runs along a 6 m leg: the ramp goes along the 50 m side (2026-10-17: 3 of
        # downtown Baltimore's 15 garages were boxes)
        ring = np.array([(0, 0), (80, 0), (80, 6), (20, 6), (20, 50), (0, 50)], np.float64)
        lay = bld._deck_layout(ring, 3.0)
        self.assertIsNotNone(lay)
        self.assertEqual(lay[0], 5)

    def test_mapped_multi_storey_car_park_is_a_garage(self):
        r = rect(0, 0, 60, 40)
        self.assertTrue(bld.plan(r, {"class": "commercial", "garage": True}, 0, 39.28, -76.59).deck)
        self.assertTrue(bld.plan(r, {"class": "garage", "garage": True, "num_floors": 4}, 0, 39.28, -76.59).deck)
        self.assertFalse(bld.plan(r, {"class": "commercial"}, 0, 39.28, -76.59).deck)
        # apartments over a car park podium stay apartments
        self.assertFalse(bld.plan(r, {"class": "apartments", "garage": True}, 0, 39.28, -76.59).deck)
        self.assertTrue(bld._covers_garage(r, [rect(-5, -5, 70, 50)]))
        self.assertTrue(bld._covers_garage(rect(0, 0, 100, 100), [rect(40, 40, 10, 10)]))
        self.assertFalse(bld._covers_garage(r, [rect(200, 0, 60, 40)]))

    def test_split_levels_counted_as_floors_still_make_decks(self):
        spec = bld.plan(rect(0, 0, 60, 40), {"class": "parking", "num_floors": 10, "height": 20.8}, 0, 39.28, -76.59)
        p = bld.deck_plan(spec, 0.0, 0.0)
        self.assertIsNotNone(p)
        self.assertEqual(p[1], 6)


class SplitTest(unittest.TestCase):
    def test_split_under_the_index_limit(self):
        items = [(bld.plan(rect(x * 30, 0, 20, 12), {"class": ""}), 0.0, 0.0) for x in range(30)]
        one = bld.build(items)
        parts = bld.build_parts(items, max_verts=len(one[0]) // 3)
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(all(len(p[0]) <= len(one[0]) // 3 for p in parts))
        self.assertEqual(sum(len(p[2]) for p in parts), len(one[2]), "every triangle in exactly one part")
        self.assertEqual(len(bld.build_parts(items)), 1, "a small tile stays one mesh")

    def test_parts_carry_their_lite_mesh(self):
        # a near tile's shape draws the far tiles' lite mesh while it is small on screen (camera LOD)
        items = [(bld.plan(rect(x * 30, 0, 20, 12), {"class": ""}), 0.0, 0.0) for x in range(4)]
        (part,) = bld.build_parts(items, with_lod=True)
        lite = bld.build(items, lite=True)
        self.assertEqual(len(part[6][2]), len(lite[2]))
        with tempfile.TemporaryDirectory() as d:
            bld.write(os.path.join(d, "t.cdae"), part)
            shape = cdae.read(os.path.join(d, "t.cdae"))
            self.assertEqual(shape["names"][1:], [f"detail{int(bld.BLD_LOD_PX)}", "detail2"])
            self.assertEqual(len(shape["meshes"]), 2)


class MeshTest(unittest.TestCase):
    def check_outward(self, ring, attrs):
        spec = bld.plan(ring, attrs)
        P, UV, T, M, mats, C = bld.build([(spec, 0.0, 0.0)])
        c = np.append(ring.mean(0), spec.height / 2)
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        mid = P[T].mean(1)
        good = ((mid - c) * n).sum(1) > 0
        nz = n[:, 2] / np.maximum(np.linalg.norm(n, axis=1), 1e-12)
        good |= (nz < -0.99) & (mid[:, 2] > spec.height - 1.0)     # a cornice's underside faces down
        good |= mid[:, 2] > spec.height + bld.CORNICE_UP_M + 0.01   # roof boxes: their own outsides
        lip = (mid[:, 2] > spec.height) & (mid[:, 2] < spec.height + bld.CORNICE_UP_M + 0.01) & (np.abs(nz) < 0.01)
        good |= lip                                                   # the cornice lip's inner face, above the roof
        self.assertTrue(good.all(), f"{(~good).sum()} of {len(T)} triangles face in ({spec.roof})")
        self.assertLess(M.max(), len(mats))
        return spec, P

    def test_flat_box(self):
        spec, P = self.check_outward(rect(0, 0, 30, 20), {"class": "retail", "height": 9.0})
        self.assertAlmostEqual(P[:, 2][P[:, 2] < 9.5].max(), 9.0 + bld.CORNICE_UP_M)
        self.assertGreater(P[:, 2].max(), 10.0, "boxes on a 600 m2 flat roof")
        # the walls reach SKIRT_M under the ground (hidden; fills the gap where the far field's coarse ground runs lower)
        self.assertAlmostEqual(P[:, 2].min(), -bld.SKIRT_M)
        self.assertAlmostEqual(bld.build([(spec, 0.0, 0.0)], lite=True)[0][:, 2].min(), -bld.SKIRT_LITE_M)

    def test_gabled_and_hipped(self):
        for shape in ("gabled", "hipped"):
            spec, P = self.check_outward(rect(0, 0, 12, 8), {"class": "house", "roof_shape": shape})
            self.assertEqual(spec.roof, shape)
            self.assertGreater(P[:, 2].max(), spec.height + 1.5, "a ridge above the eaves")


    # domes from the map's roof:shape (#593's thread: landmarks from the map data, not hand-made); a gable before
    @staticmethod
    def circle(r, n=24):
        a = np.linspace(0.0, 2 * math.pi, n, endpoint=False)
        return np.column_stack((r * np.cos(a), r * np.sin(a)))

    def test_a_mapped_dome_is_a_dome(self):
        ring = self.circle(10.0)
        spec, P = self.check_outward(ring, {"class": "church", "height": 30.0, "roof_shape": "dome",
                                            "roof_height": 9.0})
        self.assertEqual(spec.roof, "dome")
        self.assertAlmostEqual(spec.height, 21.0, delta=0.01)            # the drum: the mapped height less the roof's
        self.assertAlmostEqual(P[:, 2].max(), 30.0, delta=0.05)          # its top where the map puts it
        top = P[P[:, 2] > spec.height + 0.01]
        r = np.hypot(top[:, 0], top[:, 1])
        self.assertLessEqual(r.max(), 10.0 + 0.01)                       # never wider than its drum
        # a sphere's quarter: at half the rise its radius is ~cos(asin(0.5)) of the drum's
        mid = top[np.abs(top[:, 2] - (spec.height + 4.5)) < 1.2]
        self.assertTrue(len(mid))
        far = bld.build([(spec, 0.0, 0.0)], lite=True)[0]
        self.assertAlmostEqual(far[:, 2].max(), 30.0, delta=0.05)        # far away the same dome

    def test_an_onion_dome_bulges_and_comes_to_a_point(self):
        ring = self.circle(4.0)
        spec, P = self.check_outward(ring, {"class": "church", "height": 20.0, "roof_shape": "onion"})
        self.assertEqual(spec.roof, "onion")
        rise = 20.0 - spec.height
        self.assertAlmostEqual(rise, bld.DOME_RISE["onion"] * 4.0, delta=0.4)   # no roof:height: 1.6 x its radius
        top = P[P[:, 2] > spec.height + 0.01]
        self.assertGreater(np.hypot(top[:, 0], top[:, 1]).max(), 4.0 * 1.1)     # the bulge over the drum
        self.assertAlmostEqual(P[:, 2].max(), 20.0, delta=0.05)

    def test_a_dome_in_the_skyline(self):
        spec = bld.plan(self.circle(12.0), {"class": "church", "height": 40.0, "roof_shape": "dome"})
        P = bld._sky_mesh([(spec, 0.0, 0.0)])[0]
        self.assertAlmostEqual(P[:, 2].max(), 40.0, delta=0.05)

    def test_a_round_roof_is_still_a_gable(self):
        spec = bld.plan(rect(0, 0, 12, 8), {"class": "house", "roof_shape": "round"})
        self.assertEqual(spec.roof, "gabled")


class DoorTest(unittest.TestCase):
    def test_door_faces_the_road(self):
        spec = bld.plan(rect(0, 0, 10, 8), {"class": "house"})
        road = (np.array([(-50.0, -6.0), (50.0, -6.0)]), 4.0)        # south of the house
        rows = bld.doors([(spec, 0.0, 0.0)], [road], 0.0, 0.0)
        self.assertEqual(len(rows), 1)
        g, x, y, z, yaw, sc, h, r = rows[0]
        self.assertEqual(g, "door")
        self.assertAlmostEqual(y, -bld.DOOR_OUT_M, places=2)            # on the south wall (its panel just out of it)
        self.assertAlmostEqual(yaw, 0.0)                               # the wall runs +x

    def test_main_street_shopfronts(self):
        shops = signed = 0
        for k in range(20):
            spec = bld.plan(rect(k * 30, 0, 10, 14), {"class": ""}, 2, 39.4, -77.4)
            street = (np.array([(-50.0, -7.0), (900.0, -7.0)]), 6.0, "tertiary", True)
            rows = bld.doors([(spec, 0.0, 0.0)], [street], 0.0, 0.0)
            if spec.shop:
                shops += 1
                self.assertEqual(rows, [], "a shopfront, no house door")
                if not spec.plain:
                    self.assertEqual(spec.sign, 0, "the sign band over the street (south) wall")
                    m = bld.Mesh()
                    bld._signs(m, spec, spec.ring, 4.2, bld._Looks(spec.seed))
                    if m.n:                     # a few shops have none (hashed)
                        signed += 1
                        ys = m.arrays()[0][:, 1]
                        self.assertTrue((ys < 0.01).all() and (ys > -bld.SIGN_OUT - 0.01).all(),
                                        "boxes just out from the south wall")
        self.assertGreater(shops, 8)
        self.assertGreater(signed, shops // 2)

    def test_driveway_to_the_road(self):
        for k in range(40):                                  # a house with a siding wall (hashed)
            spec = bld.plan(rect(k * 40, 20, 12, 9), {"class": "house"}, 0, 39.2, -76.9)
            if spec.wall:
                break
        self.assertIsNotNone(spec.wall)
        drives = []
        bld.doors([(spec, 0.0, 0.0)], [(np.array([(-50.0, 6.0), (2000.0, 6.0)]), 4.0, "residential", True)], 0.0, 0.0,
                  drives)
        self.assertEqual(len(drives), 1)
        n = np.array(drives[0]["n"])
        self.assertAlmostEqual(n[0, 1], 19.8, delta=0.3, msg="from the front wall")
        self.assertAlmostEqual(n[-1, 1], 10.1, delta=0.3, msg="to the road's edge")

    def test_porches_on_the_front_of_some_houses(self):
        with_porch = 0
        for k in range(40):
            spec = bld.plan(rect(k * 40, 0, 10, 8), {"class": "house"}, 0, 39.4, -77.4)
            bld.doors([(spec, 0.0, 0.0)], [(np.array([(-50.0, -6.0), (2000.0, -6.0)]), 4.0, "residential", True)], 0.0, 0.0)
            if not spec.porch or not spec.door:
                continue
            with_porch += 1
            m = bld.Mesh()
            bld._porch(m, spec, 1.0, bld._Looks(spec.seed))
            P = m.arrays()[0]
            self.assertTrue(m.n > 0)
            self.assertAlmostEqual(P[:, 1].min(), -(bld.PORCH_DEPTH_M + 0.2), delta=0.01, msg="out in front of the south wall")
            self.assertGreaterEqual(P[:, 1].max(), -0.01)
            self.assertAlmostEqual(P[:, 2].max(), 1.0 + bld.PORCH_DECK_M + bld.PORCH_H_M + 0.18, places=3)
        self.assertTrue(10 < with_porch < 35, with_porch)

    def test_front_fence_with_a_gate(self):
        a, d = np.array([0.0, 20.0]), np.array([1.0, 0.0])         # a 12 m house front 20 m from a road at y = 0
        rows = bld._front_fence(type("S", (), {"seed": 0.3})(), a, d, 12.0, 0.5, np.array([-100.0, 0.0]),
                                np.array([300.0, 0.0]), 300.0 ** 2, 4.0, 1.5, False)
        self.assertTrue(rows)
        for g, x, y, z, yaw, sc, h, r in rows:
            self.assertEqual(g, bld.G_FENCE)
            self.assertAlmostEqual(y, 4.0 + bld.FENCE_BACK_M, places=2)
            self.assertEqual(yaw, 0.0)
            self.assertGreater(abs(x - 6.0), bld.FENCE_SEG_M / 2, "a gap in front of the door")
        self.assertEqual(bld._front_fence(type("S", (), {"seed": 0.3})(), np.array([0.0, 8.0]), d, 12.0, 0.5,
                                          np.array([-100.0, 0.0]), np.array([300.0, 0.0]), 300.0 ** 2, 4.0, 1.5,
                                          False), [], "no fence without a front yard")

    def test_front_fence_keeps_off_other_roads(self):
        """A corner house: its fence along the front road stops short of the side street (GitHub #105)."""
        a, d = np.array([0.0, 20.0]), np.array([1.0, 0.0])
        args = (type("S", (), {"seed": 0.3})(), a, d, 12.0, 0.5, np.array([-100.0, 0.0]), np.array([300.0, 0.0]),
                300.0 ** 2, 4.0, 1.5, False)
        front = (np.array([[-100.0, 0.0]]), np.array([[300.0, 0.0]]), np.array([4.0]))
        side = (np.array([[-100.0, 0.0], [-4.0, -50.0]]), np.array([[300.0, 0.0], [-4.0, 50.0]]), np.array([4.0, 4.0]))
        alone = bld._front_fence(*args, front)
        self.assertEqual(alone, bld._front_fence(*args), "its own road: as before")
        cut = bld._front_fence(*args, side)
        self.assertLess(len(cut), len(alone))
        for row in cut:
            self.assertGreater(row[1] - bld.FENCE_SEG_M / 2, -4.0 + 4.0 + bld.FENCE_CLEAR_M - 1e-9)

    def test_company_sign_on_a_warehouse_front(self):
        seen = 0
        for k in range(30):
            spec = bld.plan(rect(k * 200, 0, 60, 40), {"class": "industrial"}, 0, 39.4, -77.4)
            if spec.windows is not False:
                continue
            rows = bld.doors([(spec, 0.0, 0.0)], [(np.array([(-50.0, -10.0), (9000.0, -10.0)]), 6.0, "primary", True)],
                             0.0, 0.0)
            for g, x, y, z, yaw, sc, h, r in rows:
                if g.startswith("wallsign"):
                    seen += 1
                    self.assertAlmostEqual(y, -0.35, places=2, msg="on the south (street) wall")
                    self.assertLess(z + h / 2, spec.height + 0.01)
        self.assertGreater(seen, 3)

    def test_no_door_without_a_road(self):
        spec = bld.plan(rect(0, 0, 10, 8), {"class": "house"})
        self.assertEqual(bld.doors([(spec, 0.0, 0.0)], [(np.array([(500.0, 0), (600.0, 0)]), 4.0)], 0, 0), [])


class Feats:
    def __init__(self, ids, rings, **attrs):
        self.ids = np.array(ids, object)
        self.kind = np.full(len(ids), 3, np.uint8)
        self.rings = rings
        self.attrs = {k: np.array(v, object if isinstance(v[0], str) else np.float64) for k, v in attrs.items()}

    def parts(self, i):
        return [[np.asarray(self.rings[i], np.float64)]]


class PartsTest(unittest.TestCase):
    def test_tower_on_podium(self):
        import geodesy
        f = geodesy.EnuFrame(39.28, -76.59)

        def ll(x, y, w, d):
            e = np.array([x, x + w, x + w, x, x]); n = np.array([y, y, y + d, y + d, y])
            la, lo, _ = f.enu_to_geodetic(e, n, 0.0)
            return np.stack([lo, la], 1)
        b = Feats(["B"], [ll(10, 10, 40, 40)], **{"class": ["commercial"], "height": [90.0],
                                                   "has_parts": [1.0], "min_height": [np.nan]})
        parts = Feats(["p1", "p2", "p3"], [ll(10, 10, 40, 40), ll(20, 20, 20, 20), ll(10, 10, 40, 8)],
                      building_id=["B", "B", "B"], height=[15.0, np.nan, 30.0], min_height=[np.nan, np.nan, 20.0],
                      num_floors=[np.nan, np.nan, np.nan], min_floor=[np.nan, np.nan, np.nan])
        items = bld.tile_buildings(f, 0, 0, 256, 256, [b], lambda la, lo: np.zeros(np.shape(la)), part_layers=[parts])
        self.assertEqual(len(items), 3, "the parts, not the footprint")
        tops = sorted(round(s.height + s.min_h) for s, _, _ in items)
        self.assertEqual(tops, [15, 30, 90], "the podium, a floating part, the tower at the building's height")
        self.assertEqual(len({s.seed for s, _, _ in items}), 1, "one look")
        P = bld.build(items)[0]
        self.assertAlmostEqual(P[:, 2][P[:, 2] < 90.5].max(), 90.0, delta=0.01)

    def _items(self, part_rect, part_bottom=np.nan, height=10.0):
        import geodesy
        f = geodesy.EnuFrame(59.42, 24.79)

        def ll(x, y, w, d):
            e = np.array([x, x + w, x + w, x, x]); n = np.array([y, y, y + d, y + d, y])
            la, lo, _ = f.enu_to_geodetic(e, n, 0.0)
            return np.stack([lo, la], 1)
        b = Feats(["B"], [ll(-100, -125, 200, 250)], **{"class": ["retail"], "height": [height], "has_parts": [1.0],
                                                         "min_height": [np.nan]})
        parts = Feats(["p1"], [ll(*part_rect)], building_id=["B"], height=[5.0], min_height=[part_bottom],
                      num_floors=[np.nan], min_floor=[np.nan])
        return bld.tile_buildings(f, -256, -256, 256, 256, [b], lambda la, lo: np.zeros(np.shape(la)),
                                  part_layers=[parts])

    def test_a_stray_part_does_not_erase_its_building(self):
        """Ülemiste keskus, Tallinn (GitHub #608): a 51,000 m² mall mapped with height 10 and one 452 m² building
        part (height 5) was drawn as that part alone, the rest of it bare ground. Parts covering under PARTS_COVER_MIN
        of the outline: the outline is drawn too, the parts on it."""
        items = self._items((60, 60, 21, 22))
        tops = sorted(round(s.height + (s.min_h or 0.0)) for s, _, _ in items)
        self.assertEqual(tops, [5, 10], "the part and the building")
        whole = max(items, key=lambda it: abs(bld._area2(it[0].ring)))
        self.assertAlmostEqual(abs(bld._area2(whole[0].ring)) / 2, 50000.0, delta=500.0)
        with unittest.mock.patch.object(bld, "PARTS_COVER_ON", False):
            self.assertEqual([round(s.height) for s, _, _ in self._items((60, 60, 21, 22))], [5], "(off: as before)")

    def test_parts_covering_most_of_it_still_draw_it(self):
        items = self._items((-100, -125, 200, 150))         # (60 % of the outline)
        self.assertEqual([round(s.height) for s, _, _ in items], [5], "the parts, not the footprint")

    def test_a_structure_in_parts_off_the_ground_keeps_its_gaps(self):
        """A part off the ground (an arcade's roof, a tower's platform): the building is modelled in parts, its
        outline its overall box: not drawn, however little the parts cover."""
        items = self._items((60, 60, 21, 22), part_bottom=3.0)
        self.assertEqual(len(items), 1, "the part alone")
        self.assertEqual(len(self._items((60, 60, 21, 22), height=1.0)), 1, "(an outline mapped as a base: its part)")


class OverRoadTest(unittest.TestCase):
    def setUp(self):
        import geodesy
        self.f = geodesy.EnuFrame(39.28, -76.59)

    def ll(self, x, y, w, d):
        e = np.array([x, x + w, x + w, x, x]); n = np.array([y, y, y + d, y + d, y])
        la, lo, _ = self.f.enu_to_geodetic(e, n, 0.0)
        return np.stack([lo, la], 1)

    def test_underground_buildings_are_left_out(self):
        # Shot Tower Metro (Baltimore): level -2, drawn as a box across the street the AI then drove into
        b = Feats(["S", "T"], [self.ll(10, 10, 60, 40), self.ll(100, 10, 30, 30)],
                  **{"class": ["train_station", ""], "height": [np.nan, 9.0], "level": [-2.0, np.nan]})
        items = bld.tile_buildings(self.f, 0, 0, 256, 256, [b], lambda la, lo: np.zeros(np.shape(la)))
        self.assertEqual(len(items), 1)

    def test_a_footprint_over_a_street_is_lifted_over_it(self):
        b = Feats(["O", "H"], [self.ll(10, 10, 80, 60), self.ll(150, 10, 30, 30)],
                  **{"class": ["", ""], "height": [16.0, 9.0]})
        street = np.array([[0.0, 40.0], [256.0, 40.0]])         # through the first, past the second
        items = bld.tile_buildings(self.f, 0, 0, 256, 256, [b], lambda la, lo: np.zeros(np.shape(la)),
                                            roads_xy=[street])
        by_area = sorted(items, key=lambda it: -bld._area2(it[0].ring))
        over, house = by_area[0][0], by_area[1][0]
        self.assertEqual(over.min_h, bld.OVER_ROAD_CLEAR_M)
        self.assertAlmostEqual(over.min_h + over.height, 16.0, delta=0.01, msg="its top where it was")
        self.assertEqual(house.min_h, 0.0)
        self.assertAlmostEqual(bld.road_through(over.ring, [street - 0]), 80.0, delta=2.5)

    def test_a_huge_footprint_with_streets_in_it_is_a_site_left_out(self):
        # OSM's 1.35 km2 "Blueberry Commons" by I-94 (Huntley MT, 2026-11-05): a planned development tagged building,
        # its streets inside, lifted over them as a 15 m wall over the prairie. A footprint over SITE_M2 that public
        # roads run through is left out; the same size without a road in it (a real warehouse) stays
        side = 400.0                                            # 16 ha
        b = Feats(["S"], [self.ll(-72, -72, side, side)], **{"class": ["retail"], "height": [15.0]})
        street = np.array([[-100.0, 128.0], [400.0, 128.0]])
        ground = lambda la, lo: np.zeros(np.shape(la))
        self.assertGreater(side * side, bld.SITE_M2)
        self.assertEqual(bld.tile_buildings(self.f, 0, 0, 256, 256, [b], ground, roads_xy=[street]), [])
        far = np.array([[-100.0, 600.0], [400.0, 600.0]])
        self.assertEqual(len(bld.tile_buildings(self.f, 0, 0, 256, 256, [b], ground, roads_xy=[far])), 1)

    def test_a_geosearch_hit_is_returned_as_custom(self):
        # a 3dmr.eu model found by geo-search (road_tile_items, threedmr.search_ids/model_info): the footprints
        # stay procedural, the model is returned alongside them as custom
        b = Feats(["M", "H"], [self.ll(10, 10, 20, 15), self.ll(100, 100, 12, 10)],
                  **{"class": ["", "house"], "height": [8.0, 6.0]})

        class MD:
            def cell(self, key, layers=None, kind=None):
                return type("C", (), {"layers": {"buildings": b}})()

        mx, my = 50.0, 60.0
        mlat, mlon, _ = self.f.enu_to_geodetic(mx, my, 0.0)

        def model_position(mid, cache_dir, iface=None, timeout=30.0):
            return (float(mlat), float(mlon), False)

        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("threedmr.region_ids", return_value=[24]), \
             mock.patch("threedmr.model_position", side_effect=model_position):
            items, custom = bld.road_tile_items(self.f, 0, 0, MD(), ground, cache_dir="/tmp/whatever")
            self.assertEqual(len(items), 2, "both footprints stay procedural")
            self.assertEqual(len(custom), 1)
            self.assertEqual(custom[0]["id"], 24)
            x, y = custom[0]["xy"]
            self.assertAlmostEqual(x, mx, places=4)
            self.assertAlmostEqual(y, my, places=4)
            self.assertTrue(0 <= x < 256 and 0 <= y < 256, "inside the tile bbox")

            _items, custom_none = bld.road_tile_items(self.f, 0, 0, MD(), ground, cache_dir=None)
            self.assertEqual(custom_none, [], "no cache_dir: geo-search skipped")

    def test_geotile_box_owns_its_buildings(self):
        # (tile_buildings' own=: a geotile's quad overlaps its neighbours' bounding boxes; without its exact ownership a
        # footprint in the overlap was built by both tiles - lost in our rebuild, restored at the 1.7.3 merge)
        class Owned(tuple):
            def owns(self, x, y):
                return x < 50.0

        class MD:
            def cell(self, key, layers=None, kind=None):
                return type("C", (), {"layers": {}})()

        got = {}

        def tile_buildings(*a, **kw):
            got.update(kw)
            return []

        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("roads.tile_bbox", return_value=Owned((0.0, 0.0, 256.0, 256.0))), \
             mock.patch("roads.cells_of_tile", return_value=[]), \
             mock.patch.object(bld, "tile_buildings", side_effect=tile_buildings), \
             mock.patch.object(bld, "district_town", return_value=False):
            bld.road_tile_items(self.f, 0, 0, MD(), ground)
            self.assertTrue(hasattr(got.get("own"), "owns"), "a geotile's box is passed as own")
        with mock.patch("roads.cells_of_tile", return_value=[]), \
             mock.patch.object(bld, "tile_buildings", side_effect=tile_buildings), \
             mock.patch.object(bld, "district_town", return_value=False):
            bld.road_tile_items(self.f, 0, 0, MD(), ground)
            self.assertIsNone(got.get("own"), "an anchor tile's square: its half-open box")

    def test_geosearch_uses_corrected_desc_coords(self):
        # threedmr.model_position (id 22's fix): when it returns corrected coordinates (its api point is far
        # from its desc's own osmgo.org link), both xy and custom["latlon"] must come from those corrected
        # coordinates, not the api's - latlon in particular feeds osm_yaw's OSM lookup, which must look
        # around the model's real position, not wherever the stale api point sits
        b = Feats(["M"], [self.ll(10, 10, 20, 15)], **{"class": [""], "height": [8.0]})

        class MD:
            def cell(self, key, layers=None, kind=None):
                return type("C", (), {"layers": {"buildings": b}})()

        mx, my = 50.0, 60.0
        mlat, mlon, _ = self.f.enu_to_geodetic(mx, my, 0.0)
        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("threedmr.region_ids", return_value=[22]), \
             mock.patch("threedmr.model_position", return_value=(float(mlat), float(mlon), True)):
            _items, custom = bld.road_tile_items(self.f, 0, 0, MD(), ground, cache_dir="/tmp/whatever")
            self.assertEqual(len(custom), 1)
            x, y = custom[0]["xy"]
            self.assertAlmostEqual(x, mx, places=4)
            self.assertAlmostEqual(y, my, places=4)
            self.assertAlmostEqual(custom[0]["latlon"][0], float(mlat))
            self.assertAlmostEqual(custom[0]["latlon"][1], float(mlon))

    def test_geosearch_falls_back_to_the_wide_corrected_search(self):
        # id 22 (Dragon Bridge, Da Nang): its api point sits ~26 km from the real bridge, outside any tile's
        # own small search_ids radius, so the first (tile-radius) search never finds it at all - only the
        # second, threedmr.SEARCH_CORRECT_R_M-radius one does, and only because model_position corrects it
        b = Feats(["H"], [self.ll(100, 100, 12, 10)], **{"class": ["house"], "height": [6.0]})

        class MD:
            def cell(self, key, layers=None, kind=None):
                return type("C", (), {"layers": {"buildings": b}})()

        mx, my = 50.0, 60.0
        mlat, mlon, _ = self.f.enu_to_geodetic(mx, my, 0.0)
        ground = lambda la, lo: np.zeros(np.shape(la))

        # (the region's search reaches SEARCH_CORRECT_R_M past its corners: id 22 is in it, placed where corrected)
        with mock.patch("threedmr.region_ids", return_value=[22]), \
             mock.patch("threedmr.model_position", return_value=(float(mlat), float(mlon), True)):
            _items, custom = bld.road_tile_items(self.f, 0, 0, MD(), ground, cache_dir="/tmp/whatever")
            self.assertEqual(len(custom), 1)
            self.assertEqual(custom[0]["id"], 22)
            x, y = custom[0]["xy"]
            self.assertAlmostEqual(x, mx, places=4)
            self.assertAlmostEqual(y, my, places=4)

    def test_wide_search_hit_without_correction_is_dropped(self):
        # a model only found by the wide search whose model_position did NOT correct it (corrected False) is
        # not trusted that far out - an api point 20+ km away is not "this tile's" model
        b = Feats(["H"], [self.ll(100, 100, 12, 10)], **{"class": ["house"], "height": [6.0]})

        class MD:
            def cell(self, key, layers=None, kind=None):
                return type("C", (), {"layers": {"buildings": b}})()

        mx, my = 50.0, 60.0
        mlat, mlon, _ = self.f.enu_to_geodetic(mx, my, 0.0)
        ground = lambda la, lo: np.zeros(np.shape(la))

        # (one region search: a model counts where model_position puts it; its api point 20 km off, uncorrected,
        # is not in this tile)
        flat, flon, _ = self.f.enu_to_geodetic(20000.0, 0.0, 0.0)
        with mock.patch("threedmr.region_ids", return_value=[22]), \
             mock.patch("threedmr.model_position", return_value=(float(flat), float(flon), False)):
            _items, custom = bld.road_tile_items(self.f, 0, 0, MD(), ground, cache_dir="/tmp/whatever")
            self.assertEqual(custom, [], "a region model far from the tile is not this tile's")

    def test_one_region_search_a_tile(self):
        # one cached search for the tile's region (threedmr.region_ids), not two per tile; its model added once
        b = Feats(["H"], [self.ll(100, 100, 12, 10)], **{"class": ["house"], "height": [6.0]})

        class MD:
            def cell(self, key, layers=None, kind=None):
                return type("C", (), {"layers": {"buildings": b}})()

        mx, my = 50.0, 60.0
        mlat, mlon, _ = self.f.enu_to_geodetic(mx, my, 0.0)
        ground = lambda la, lo: np.zeros(np.shape(la))
        seen_ranges = []

        def region_ids(lat, lon, cache_dir):
            seen_ranges.append((lat, lon))
            return [24]

        with mock.patch("threedmr.region_ids", side_effect=region_ids), \
             mock.patch("threedmr.model_position", return_value=(float(mlat), float(mlon), False)):
            _items, custom = bld.road_tile_items(self.f, 0, 0, MD(), ground, cache_dir="/tmp/whatever")
            self.assertEqual(len(custom), 1, "added once")
            self.assertEqual(len(seen_ranges), 1, "one region search")

    def test_local_model_in_bbox_is_returned_as_custom(self):
        # a local (no-3dmr-base) model (threedmr.local_models) whose own lat/lon falls in the tile is folded
        # into custom exactly like a geo-search hit, but with a string id (never a numeric 3dmr one) and a
        # "local" key (_custom_tile_entries' signal to skip fetch_model/gltf_mesh for it)
        b = Feats(["H"], [self.ll(100, 100, 12, 10)], **{"class": ["house"], "height": [6.0]})

        class MD:
            def cell(self, key, layers=None, kind=None):
                return type("C", (), {"layers": {"buildings": b}})()

        mx, my = 50.0, 60.0
        mlat, mlon, _ = self.f.enu_to_geodetic(mx, my, 0.0)
        entry = {"id": "local_fire_tower_20m", "file": "fire_tower_20m.stl",
                  "lat": float(mlat), "lon": float(mlon), "factor": [0.784, 0.063, 0.18]}
        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("threedmr.region_ids", return_value=[]), \
             mock.patch("threedmr.local_models", return_value=[entry]):
            _items, custom = bld.road_tile_items(self.f, 0, 0, MD(), ground, cache_dir="/tmp/whatever")
            self.assertEqual(len(custom), 1)
            self.assertEqual(custom[0]["id"], "local_fire_tower_20m")
            self.assertIsInstance(custom[0]["id"], str)
            self.assertIs(custom[0]["local"], entry)
            x, y = custom[0]["xy"]
            self.assertAlmostEqual(x, mx, places=4)
            self.assertAlmostEqual(y, my, places=4)

            _items, custom_none = bld.road_tile_items(self.f, 0, 0, MD(), ground, cache_dir=None)
            self.assertEqual(custom_none, [], "no cache_dir: local catalog skipped too")


class CustomModelSuppressionTest(unittest.TestCase):
    """A baked 3dmr.eu model suppresses the procedural footprints under it (_suppress_under_models) - the
    user's report at Baghdad's Al-Shaheed Monument: two OSM "sector" footprints (the domes' lower tiers)
    stayed and intersected model 24's real mesh once it loaded."""

    def setUp(self):
        import geodesy
        self.f = geodesy.EnuFrame(39.28, -76.59)

    @staticmethod
    def _gltf_mesh(pos_bbox):
        """A fake threedmr.gltf_mesh returning a one-triangle mesh whose horizontal bbox is pos_bbox (dx, dy)."""
        dx, dy = pos_bbox
        pos = np.array([[0.0, 0.0, 0.0], [dx, 0.0, 0.0], [0.0, dy, 1.0]])
        return (pos, np.zeros((3, 2)), np.array([[0, 1, 2]]), None, [(None, None, None, False, None)],
               np.zeros(1, np.int64))

    def test_custom_model_suppresses_procedural(self):
        # the model stands at (10, 10); one footprint's centre is 4.2 m away (under it), the other 127 m away
        near = bld.Spec(ring=rect(5, 5, 4, 4), height=6.0, min_h=0.0, cls="")
        far = bld.Spec(ring=rect(100, 100, 4, 4), height=6.0, min_h=0.0, cls="")
        items = [(near, 0.0, 6.0), (far, 0.0, 6.0)]
        radii = [(24, 10.0, 10.0, 20.0)]
        out = bld._suppress_under_models(items, radii, 0.0, 0.0)
        self.assertEqual([s for s, _, _ in out], [far], "only the footprint under the model is dropped")

    def test_toll_booths_are_never_suppressed(self):
        booth = bld.Spec(ring=rect(5, 5, 4, 4), height=4.0, min_h=0.0, cls="toll_booth")
        items = [(booth, 0.0, 4.0)]
        out = bld._suppress_under_models(items, [(24, 7.0, 7.0, 20.0)], 0.0, 0.0)
        self.assertEqual(out, items, "a toll island's booth is kept even under a model's radius")

    def test_no_suppression_without_a_model(self):
        # a failed geo-search / download leaves radii empty (_custom_tile_entries): items untouched
        house = bld.Spec(ring=rect(5, 5, 4, 4), height=6.0, min_h=0.0, cls="")
        items = [(house, 0.0, 6.0)]
        out = bld._suppress_under_models(items, [], 0.0, 0.0)
        self.assertEqual(out, items)

    def test_suppress_radius_clamped(self):
        custom = [{"id": 41, "xy": (10.0, 10.0)}, {"id": 3, "xy": (20.0, 20.0)}]

        def gltf_mesh(glb, model_id=None):
            return self._gltf_mesh((0.5, 0.5) if model_id == 41 else (900.0, 900.0))

        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("threedmr.fetch_model", return_value=b"glb"), \
             mock.patch("threedmr.gltf_mesh", side_effect=gltf_mesh), \
             mock.patch("threedmr.write_model",
                       return_value={"shape": "threedmr_x.cdae", "texture": None, "material": "threedmr_x",
                                    "has_alpha": False, "alpha_cutoff": None}), \
             mock.patch("threedmr.update_materials", return_value=None):
            _entries, radii, _boxes = bld._custom_tile_entries(self.f, custom, ground, "/tmp/s1", "/temp/beamngEarth/s1")
        by_id = {cid: r for cid, _cx, _cy, r in radii}
        self.assertEqual(by_id[41], bld.SUPPRESS_MIN_M, "a 0.5 m model: radius clamped up to the minimum")
        self.assertEqual(by_id[3], bld.SUPPRESS_MAX_M, "a 900 m model: radius clamped down to the maximum")

    def test_model_wider_than_a_km_not_placed(self):
        # 3dmr.eu id 29 (a district, 61 km) and ids 6 / 7 (stray geometry 1.4-1.9 km round a building): not placed,
        # nothing suppressed; a model up to MODEL_MAX_M (the Dragon Bridge, 842 m) is
        custom = [{"id": 29, "xy": (10.0, 10.0)}, {"id": 22, "xy": (20.0, 20.0)}]

        def gltf_mesh(glb, model_id=None):
            return self._gltf_mesh((61000.0, 37000.0) if model_id == 29 else (842.0, 83.0))

        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("threedmr.fetch_model", return_value=b"glb"), \
             mock.patch("threedmr.gltf_mesh", side_effect=gltf_mesh), \
             mock.patch("threedmr.bridge_deck_surface", return_value=None), \
             mock.patch("threedmr.write_model",
                       return_value={"shape": "threedmr_x.cdae", "texture": None, "material": "threedmr_x",
                                    "has_alpha": False, "alpha_cutoff": None}), \
             mock.patch("threedmr.update_materials", return_value=None):
            _entries, radii, _boxes = bld._custom_tile_entries(self.f, custom, ground, "/tmp/s1", "/temp/beamngEarth/s1")
        self.assertEqual([cid for cid, _cx, _cy, _r in radii], [22])

    def test_failed_model_yields_no_radius(self):
        custom = [{"id": 24, "xy": (10.0, 10.0)}]
        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("threedmr.fetch_model", side_effect=RuntimeError("network down")), \
             mock.patch("revisit.fault") as fault:
            entries, radii, _b = bld._custom_tile_entries(self.f, custom, ground, "/tmp/s1", "/temp/beamngEarth/s1")
        self.assertEqual(entries, [])
        self.assertEqual(radii, [], "no mesh downloaded: no suppression circle, the procedural stand-in stays")
        fault.assert_called_once()

    def test_split_sections_each_get_an_entry_and_their_materials_updated(self):
        # a model too big for Torque's 16-bit IBO comes back from write_model with "sections": every section
        # must land its own {"shape", "pos"} entry (same pos) and have its own materials registered, while
        # the suppression radius stays a single one, measured off the model's own full (pre-split) bbox
        custom = [{"id": 8, "xy": (10.0, 10.0)}]
        ground = lambda la, lo: np.zeros(np.shape(la))
        section0 = {"shape": "threedmr_8.cdae", "texture": "threedmr_8.png", "material": "threedmr_8",
                   "has_alpha": False, "alpha_cutoff": None}
        section1 = {"shape": "threedmr_8_s1.cdae", "textures": ["threedmr_8_s1_0.png"],
                   "materials": ["threedmr_8_s1_0"], "has_alpha": [False], "alpha_cutoff": [None]}
        write_result = dict(section0, sections=[section0, section1])
        updated = []
        with mock.patch("threedmr.fetch_model", return_value=b"glb"), \
             mock.patch("threedmr.gltf_mesh", side_effect=lambda glb, model_id=None: self._gltf_mesh((569.0, 81.0))), \
             mock.patch("threedmr.write_model", return_value=write_result), \
             mock.patch("threedmr.update_materials", side_effect=lambda *a, **k: updated.append(a[1])):
            entries, radii, _b = bld._custom_tile_entries(self.f, custom, ground, "/tmp/s1", "/temp/beamngEarth/s1")
        self.assertEqual([e["shape"] for e in entries],
                        ["/temp/beamngEarth/s1/threedmr_8.cdae", "/temp/beamngEarth/s1/threedmr_8_s1.cdae"])
        self.assertEqual(entries[0]["pos"], entries[1]["pos"], "every section placed at the same pos")
        self.assertEqual(updated, ["threedmr_8", "threedmr_8_s1_0"], "both sections' materials registered")
        self.assertEqual(len(radii), 1, "one suppression radius for the whole model, not per section")
        self.assertAlmostEqual(radii[0][3], bld.SUPPRESS_MAX_M, delta=0.01)

    def test_override_yaw_forces_rotation_without_osm_lookup(self):
        # id 22 (Dragon Bridge)'s POSITION_OVERRIDES entry carries a forced yaw - _custom_tile_entries must
        # use it as-is and never call osm_yaw's OSM lookup at all (temporarily overridden to a non-zero yaw
        # here so the rotation itself is observable, rather than the real 0.0 which would look like "no
        # rotation happened" either way)
        custom = [{"id": 22, "xy": (10.0, 10.0), "latlon": (16.06120, 108.22790)}]
        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch.dict(threedmr.POSITION_OVERRIDES, {22: (16.06120, 108.22790, 30.0)}), \
             mock.patch("threedmr.fetch_model", return_value=b"glb"), \
             mock.patch("threedmr.gltf_mesh", side_effect=lambda glb, model_id=None: self._gltf_mesh((2.0, 2.0))), \
             mock.patch("threedmr.osm_yaw") as osm_yaw, \
             mock.patch("threedmr.write_model",
                       return_value={"shape": "threedmr_22.cdae", "texture": None, "material": "threedmr_22",
                                    "has_alpha": False, "alpha_cutoff": None}) as write_model, \
             mock.patch("threedmr.update_materials", return_value=None):
            bld._custom_tile_entries(self.f, custom, ground, "/tmp/s1", "/temp/beamngEarth/s1")
        osm_yaw.assert_not_called()
        rotated_pos = write_model.call_args[0][2]
        self.assertFalse(np.allclose(rotated_pos[1], [2.0, 0.0, 0.0]), "the forced 30 degree yaw was applied")

    def test_bridge_shaped_model_is_not_carved(self):
        # FV=18: an is_bridge model (BRIDGE_ASPECT, BRIDGE_MIN_SPAN_M) no longer has its deck carved out
        # (threedmr.carve_bridge_deck_mask) - it is baked exactly like any other model, deck triangles and
        # all, and the stub road underneath is left to carry its own collision
        pos = np.array([[0.0, 0.0, 0.0], [150.0, 0.0, 0.0], [0.0, 10.0, 0.0], [150.0, 10.0, 0.0]])
        tris = np.array([[0, 1, 2], [1, 3, 2]])  # a flat, low deck: exactly what the old carve dropped
        mesh = (pos, np.zeros((4, 2)), tris, None, [(None, None, None, False, None)], np.zeros(2, np.int64))
        custom = [{"id": 22, "xy": (10.0, 10.0)}]
        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("threedmr.fetch_model", return_value=b"glb"), \
             mock.patch("threedmr.gltf_mesh", return_value=mesh), \
             mock.patch("threedmr.carve_bridge_deck_mask") as carve, \
             mock.patch("threedmr.write_model",
                       return_value={"shape": "threedmr_22.cdae", "texture": None, "material": "threedmr_22",
                                    "has_alpha": False, "alpha_cutoff": None}) as write_model, \
             mock.patch("threedmr.update_materials", return_value=None):
            bld._custom_tile_entries(self.f, custom, ground, "/tmp/s1", "/temp/beamngEarth/s1")
        carve.assert_not_called()
        self.assertEqual(write_model.call_args[0][3].tolist(), tris.tolist(),
                         "every tri (including the deck) reaches write_model unchanged")

    def test_local_model_is_baked_without_fetch(self):
        # a "local" custom entry (road_tile_items') skips fetch_model/gltf_mesh entirely - the mesh comes
        # from threedmr.load_local_model instead, and its material/shape names come straight from the
        # string id ("local_fire_tower_20m"), the same naming scheme write_model always uses for a numeric
        # 3dmr one
        factor = (0.784, 0.063, 0.18)
        pos = np.array([[0.0, 0.0, 0.0], [1.24, 0.0, 0.0], [0.0, 1.24, 20.22]])
        local_mesh = (pos, np.zeros((3, 2)), np.array([[0, 1, 2]]), None,
                      [(None, None, factor, False, None)], np.zeros(1, np.int64))
        entry = {"id": "local_fire_tower_20m", "file": "fire_tower_20m.stl", "factor": list(factor)}
        custom = [{"id": "local_fire_tower_20m", "xy": (10.0, 10.0), "local": entry}]
        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("threedmr.fetch_model") as fetch_model, \
             mock.patch("threedmr.load_local_model", return_value=local_mesh) as load_local_model, \
             mock.patch("threedmr.write_model",
                       return_value={"shape": "threedmr_local_fire_tower_20m.cdae", "texture": None,
                                    "material": "threedmr_local_fire_tower_20m",
                                    "has_alpha": False, "alpha_cutoff": None}) as write_model, \
             mock.patch("threedmr.update_materials", return_value=None) as update_materials:
            entries, radii, _b = bld._custom_tile_entries(self.f, custom, ground, "/tmp/s1", "/temp/beamngEarth/s1")
        fetch_model.assert_not_called()
        load_local_model.assert_called_once_with("/tmp", entry)
        self.assertEqual(write_model.call_args[0][1], "local_fire_tower_20m", "shape/material name from the id")
        self.assertEqual(entries[0]["shape"], "/temp/beamngEarth/s1/threedmr_local_fire_tower_20m.cdae")
        update_materials.assert_called_once()
        call_args = update_materials.call_args[0]
        self.assertEqual(call_args[1], "threedmr_local_fire_tower_20m")
        self.assertEqual(call_args[3], factor, "the catalog's own factor reaches the material")
        self.assertEqual(len(radii), 1)




class CustomModelTextureSurvivesSweepTest(unittest.TestCase):
    """The "NO MATERIAL" bug: a session's out_dir (<user_dir>/temp/beamngEarth/<sid>) is wiped whenever the
    anchor changes (sweep_sessions), and a tile that later comes back from the revisit cache restores only
    its .cdae (revisit.shapes_in matches a tile JSON's own "shape" field, never a materials.json colorMap) -
    so a texture _custom_tile_entries wrote beside the .cdae in that same out_dir goes dead, and every
    material using it falls back to the engine's own "NO MATERIAL" stand-in. write_model's tex_cache_dir
    (threedmr._persistent_png_dir) keeps the texture in cache_dir/threedmr_cache instead, which a session
    sweep never touches - this exercises the real (non-mocked) threedmr.write_model/update_materials through
    _custom_tile_entries to prove the file the materials.json entry points at survives the session's own
    out_dir being removed."""

    @staticmethod
    def _gltf_mesh(glb, model_id=None):
        pos = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 1.0]])
        uvs = np.zeros((3, 2))
        tris = np.array([[0, 1, 2]])
        textures = [(b"\x89PNGfake", "png", None, False, None)]
        return pos, uvs, tris, None, textures, np.zeros(1, np.int64)

    def test_texture_outlives_its_session_dir(self):
        import geodesy
        f = geodesy.EnuFrame(39.28, -76.59)
        ground = lambda la, lo: np.zeros(np.shape(la))
        custom = [{"id": 4, "xy": (10.0, 10.0)}]
        with tempfile.TemporaryDirectory() as user_dir:
            cache_dir = os.path.join(user_dir, "temp", "beamngEarth")
            out_dir_a = os.path.join(cache_dir, "AAA")
            os.makedirs(out_dir_a)
            os.makedirs(os.path.join(user_dir, *threedmr.MOD_MATERIALS_REL[:3]))

            with mock.patch("threedmr.fetch_model", return_value=b"glb"), \
                 mock.patch("threedmr.gltf_mesh", side_effect=self._gltf_mesh):
                entries, _radii, _b = bld._custom_tile_entries(f, custom, ground, out_dir_a,
                                                            "/temp/beamngEarth/AAA")

            materials_path = os.path.join(user_dir, *threedmr.MOD_MATERIALS_REL, threedmr.MATERIALS_FILE)
            with open(materials_path) as fh:
                data = json.load(fh)
            color_map = data["threedmr_4"]["Stages"][0]["colorMap"]
            self.assertEqual(color_map, "/temp/beamngEarth/threedmr_cache/threedmr_4.png",
                             "the texture is registered under the persistent cache, not the session dir")
            self.assertEqual(entries[0]["shape"], "/temp/beamngEarth/AAA/threedmr_4.cdae")

            shutil.rmtree(out_dir_a)       # sweep_sessions: an anchor change wipes the session folder

            self.assertTrue(os.path.exists(os.path.join(user_dir, color_map.lstrip("/"))),
                            "the texture the materials.json entry names must still be on disk")
            self.assertFalse(os.path.exists(os.path.join(user_dir, entries[0]["shape"].lstrip("/"))),
                             "only the .cdae lived in the swept session dir - a revisit cache hit re-creates "
                             "that one from its own stored blob, not from here")


class PhotoFillTest(unittest.TestCase):
    def test_big_footprint_fills_from_the_ground_around(self):
        rgb = np.zeros((512, 512, 3), np.uint8)
        rgb[:] = (40, 160, 60)
        mask = np.zeros((512, 512), bool)
        mask[10:500, 10:500] = True         # far wider than the first blur
        rgb[mask] = (200, 200, 200)
        out = bld.fill_photo(rgb, mask, 256.0, 512)
        self.assertTrue(np.allclose(out[mask].astype(int), (40, 160, 60), atol=4), "no black holes, no roof colour")


class StoopStreetTest(unittest.TestCase):
    def test_steps_reach_the_street(self):
        # the house's floor 1 m over the sidewalk (its highest ground): steps all the way down, no white block
        ring = np.array([[0.0, 0.0], [6.0, 0.0], [6.0, 12.0], [0.0, 12.0]])
        spec = bld.plan(ring, {"class": "house"})
        spec.stoop = (0, 3.0)
        m = bld.Mesh()
        bld._stoop(m, spec, ring, 10.0, 11.0, bld._Looks(spec.seed))
        P = m.arrays()[0]
        tops = sorted({round(float(z), 3) for z in P[:, 2] if z > 10.0})
        self.assertAlmostEqual(tops[-1], 11.0 + bld.STOOP_H)
        self.assertLessEqual(tops[0] - 10.0, bld.STOOP_RISE_MAX + 1e-6, "the bottom step one riser over the street")


class BridgePassageTest(unittest.TestCase):
    """A road bridge through a bridge's tower (Tower Bridge): the tower splits round a passage at its bottom."""

    def test_passage(self):
        ring = np.array([[-10, -10], [10, -10], [10, 10], [-10, 10], [-10, -10]], float)
        road = [np.array([[0.0, -50.0], [0.0, 50.0]])]
        R, A, L, M = bld._passages([ring], [{"bridge_masonry": "stone", "height": 40.0}], [None], [9.0], road)
        self.assertEqual(len(R), 3)
        sides = [r for r, m in zip(R, M) if m == 9.0]
        self.assertEqual(len(sides), 2)
        for r in sides:                 # clear of the road's PASSAGE_W
            self.assertGreaterEqual(np.abs(r[:, 0]).min(), bld.PASSAGE_W / 2 - 1e-6)
        self.assertIn(9.0 + bld.PASSAGE_H, M)
        # another building: only with the deck's height, where the deck passes through it (Southern Gate, 22 m, the
        # deck 9 m up: solid under it, the sides, the top); under a viaduct (8 m tall, deck 12 m up) it stays whole
        deck = [(road[0], np.array([12.0, 12.0]))]
        R2, A2, L2, M2 = bld._passages([ring], [{"height": 22.0}], [None], [0.0], road, deck, lambda c: 3.0)
        self.assertEqual(len(R2), 4)
        self.assertIn(9.0 + bld.PASSAGE_CLEAR, M2)
        R4, *_ = bld._passages([ring], [{"height": 8.0}], [None], [0.0], road, deck, lambda c: 3.0)
        self.assertEqual(len(R4), 1)
        R5, *_ = bld._passages([ring], [{"height": 22.0}], [None], [0.0], road)      # (no decks: left whole)
        self.assertEqual(len(R5), 1)
        R3, *_ = bld._passages([ring], [{"bridge_masonry": "stone", "height": 10.0}], ["k"], [47.0], road)
        self.assertEqual(len(R3), 1)


class MachineLearnedFootprintTest(unittest.TestCase):
    """Microsoft's machine-learned footprints on a road bridge or on water are its deck or a boat (Tower Bridge's
    river, 2026-10-18); on an island in the river, a building."""

    def test_deck_boat_island(self):
        sq = lambda cx, cy: np.array([[cx - 5, cy - 5], [cx + 5, cy - 5], [cx + 5, cy + 5], [cx - 5, cy + 5]], float)
        bridge = [np.array([[0.0, 50.0], [100.0, 50.0]])]
        river = [(np.array([[0, 0], [100, 0], [100, 100], [0, 100]], float),
                  [np.array([[60, 60], [90, 60], [90, 90], [60, 90]], float)])]
        self.assertTrue(bld._ml_false(sq(20, 50), bridge, None))
        self.assertTrue(bld._ml_false(sq(20, 20), None, river))
        self.assertFalse(bld._ml_false(sq(75, 75), bridge, river))
        self.assertFalse(bld._ml_false(sq(150, 20), bridge, river))

    def test_vehicle_on_a_road(self):
        """GitHub #2: a lorry on a Norwegian trunk road (Microsoft's footprint 4 x 17 m) stood as a white building
        over the lanes; a house beside the road stays."""
        road = [np.array([[0.0, 0.0], [200.0, 0.0]])]
        lorry = np.array([[50, 0.5], [67, 0.5], [67, 4.5], [50, 4.5]], float)      # in the outer lane
        house = np.array([[50, 9], [62, 9], [62, 19], [50, 19]], float)
        self.assertTrue(bld._ml_false(lorry, None, None, road))
        self.assertFalse(bld._ml_false(house, None, None, road))
        self.assertFalse(bld._ml_false(lorry, None, None, None))


    def test_in_a_median(self):
        """GitHub #90: a 10 x 9 m footprint between the carriageways of Moscow's Bolshaya Tulskaya (7.6 m from each)
        stood as a house in the road; a house between two streets of a block stays, as does one beside a single road."""
        sq = lambda cx, cy: np.array([[cx - 5, cy - 4.5], [cx + 5, cy - 4.5], [cx + 5, cy + 4.5], [cx - 5, cy + 4.5]],
                                     float)
        dual = [np.array([[0.0, 7.6], [200.0, 7.6]]), np.array([[200.0, -7.6], [0.0, -7.6]])]
        self.assertTrue(bld._ml_false(sq(100, 0), None, None, dual))
        block = [np.array([[0.0, 30.0], [200.0, 30.0]]), np.array([[0.0, -30.0], [200.0, -30.0]])]
        self.assertFalse(bld._ml_false(sq(100, 0), None, None, block))
        self.assertFalse(bld._ml_false(sq(100, 0), None, None, dual[:1]))
        crossing = [np.array([[0.0, 7.6], [200.0, 7.6]]), np.array([[100.0 - 7.6, -100.0], [100.0 - 7.6, -50.0]])]
        self.assertFalse(bld._ml_false(sq(100, 0), None, None, crossing), "(a corner between two crossing streets)")


class BridgeStructureTest(unittest.TestCase):
    """A bridge's tower (Overture bridge_structure: the Queensboro Bridge's, 33 x 4.6 x 110 m) is an open steel frame
    the decks run through, not a glass tower (2026-10-17)."""

    def test_part_on_a_bridge_support_is_painted_steel(self):
        """A building part over a mapped bridge support (the Golden Gate Bridge's towers): a plain box in the
        support's paint, not a glass tower; it builds (its roof look too: a KeyError dropped the whole road tile)."""
        ring = np.array([[0.0, 0.0], [6.0, 0.0], [6.0, 6.0], [0.0, 6.0]])
        sp = bld.plan(ring, {"class": "", "height": 225.0, "bridge_part": "orange"})
        self.assertEqual(sp.facade, f"{bld.STEEL}|orange")
        self.assertFalse(sp.windows)
        self.assertIsNotNone(bld.build([(sp, 0.0, 0.0)]))
        sup = [(ring * 2 - 3, "orange")]
        self.assertEqual(bld._on_support(ring, sup), "orange")
        self.assertIsNone(bld._on_support(ring + 50, sup))

    def ring(self, hl, hw):
        return np.array([[-hl, -hw], [hl, -hw], [hl, hw], [-hl, hw]], np.float64)

    def test_thin_tower_is_two_legs_and_top_beams(self):
        spec = bld.plan(self.ring(16.7, 2.3), {"class": "bridge_structure", "height": 110.0})
        self.assertEqual(spec.cls, "bridge_structure")
        self.assertFalse(spec.door)
        P, UV, T, M, mats, C = bld.build([(spec, 5.0, 6.0)])
        c = P[T].mean(1)
        low = c[:, 2] < 5.0 + bld.STEEL_TOP_SHARE * 110.0 - 1.0
        self.assertTrue((np.abs(c[low, 0]) > 16.7 - 2 * 2.3 - 0.01).all(), "below the top only the legs at the ends")
        self.assertAlmostEqual(P[:, 2].max(), 115.0, places=3)
        self.assertEqual(bld.collider_boxes([(spec, 5.0, 6.0)], 0.0, 0.0), [], "driven through")
        Pc = bld.collision_mesh([(spec, 5.0, 6.0)])[0]
        self.assertTrue((np.abs(Pc[:, 0]) > 16.7 - 2 * 2.3 - 0.01).all(), "it collides at its legs only")

    def test_small_footprint_is_a_pier(self):
        spec = bld.plan(self.ring(3.0, 2.0), {"class": "bridge_structure", "height": 12.0})
        P = bld.build([(spec, 0.0, 0.0)])[0]
        self.assertAlmostEqual(P[:, 2].max(), 12.0, places=3)



class StoopTest(unittest.TestCase):
    def test_steps_reach_the_lowest_ground(self):
        # a row house on a street falling 1.5 m along it: its front steps go down to the lowest ground (they floated)
        spec = bld.plan(rect(0, 0, 6, 14), {"class": "house"}, 1, 39.28, -76.59)
        spec.roof, spec.band, spec.stoop = "flat", bld.BAND_BRICK, (0, 3.0)
        P, UV, T, M, mats, C = bld.build([(spec, 10.0, 11.5)])
        front = (P[:, 1] < 0.0) & (P[:, 1] > -2.0)          # out from the wall y = 0 (edge 0 runs along +x)
        self.assertTrue(front.any())
        self.assertLessEqual(P[front, 2].min(), 10.0 - 0.3 + 1e-6)


class PortalSkipTest(unittest.TestCase):
    def test_a_service_tunnel_lifts_no_building(self):
        # GitHub #60: a 15 m drive-through (a service road tunnel) under an Oklahoma City church floated it 10 m up
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_roads import features, FRAME
        f = features([[(-100, 0), (100, 0)], [(-100, 50), (100, 50)]], ["service", "secondary"],
                     flags=[(0, "is_tunnel", 0.4, 0.6), (1, "is_tunnel", 0.4, 0.6)], ids=["dock", "street"])
        lines = bld.tunnel_lines(FRAME, [f], -128.0, -128.0)
        self.assertEqual(len(lines), 1, "the street's tunnel only")
        self.assertAlmostEqual(float(lines[0][:, 1].mean()), 50.0 + 128.0, delta=0.5)


class TollBoothTest(unittest.TestCase):
    """A toll plaza's islands (low mapped "buildings") with booths on them are West Coast USA's toll booth (the user's
    report at the Golden Gate, 2026-10-21: bare boxes on slabs)."""

    @staticmethod
    def box(cx, cy, length, width, angle_deg):
        u = np.array([math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))])
        v = np.array([-u[1], u[0]])
        c = np.array([cx, cy])
        return np.array([c - u * length / 2 - v * width / 2, c + u * length / 2 - v * width / 2,
                         c + u * length / 2 + v * width / 2, c - u * length / 2 + v * width / 2])

    def test_island_with_booth_becomes_the_stock_booth(self):
        island = bld.Spec(ring=self.box(50, 50, 16, 1.5, 40), height=0.2, min_h=0.0, cls="")
        booth = bld.Spec(ring=self.box(50.5, 50.4, 3, 1.5, 40), height=4.75, min_h=0.0, cls="")
        plaza = bld.Spec(ring=self.box(50, 50, 34, 8, 40), height=0.01, min_h=0.0, cls="")
        house = bld.Spec(ring=self.box(90, 90, 10, 8, 0), height=6.0, min_h=0.0, cls="")
        isl = [bld.oriented_box(island.ring)]
        items = bld._toll_mark([(island, 1.0, 1.2), (booth, 1.0, 1.2), (plaza, 1.0, 1.2), (house, 1.0, 1.5)],
                                     isl)
        self.assertEqual([s for s, _, _ in items], [island, plaza, house], "the booth on the island left out")
        self.assertEqual(island.cls, "toll_booth")
        self.assertEqual(plaza.cls, "", "a plaza's flat footprint round the islands stays as it is")
        rows = bld.toll_booth_rows(items, 100.0, 200.0)
        self.assertEqual(len(rows), 1)
        g, x, y, z, yaw = rows[0][:5]
        self.assertEqual(g, bld.G_TOLLBOOTH)
        self.assertAlmostEqual(x, 150.0, places=1)
        self.assertAlmostEqual(y, 250.0, places=1)
        self.assertAlmostEqual(z, 1.0)
        self.assertAlmostEqual(yaw, 310.0, places=0, msg="its local y along the island (40 deg): yaw 40 - 90")
        self.assertIsNone(bld.build([(island, 1.0, 1.2)]), "the island itself not built")



class RoadLinesTest(unittest.TestCase):
    def test_a_road_with_a_bridge_in_it_keeps_its_ground_pieces(self):
        """A motorway segment with a short overpass in it (the A7, GitHub #239): its pieces on the ground are public
        road lines (they were left out whole, and lorries machine-learned as buildings stood on its lanes)."""
        from test_roads import FRAME, features
        f = features([[(-100, 128), (356, 128)]], ["motorway"], levels=[(0, 1.0, 0.45, 0.5)],
                     flags=[(0, "is_bridge", 0.45, 0.5)])
        lines = bld.road_lines(FRAME, [f], 0.0, 0.0)
        self.assertEqual(len(lines), 2, "the two ground pieces either side of the bridge")
        xs = np.concatenate([ln[:, 0] for ln in lines])
        self.assertFalse(((xs > 110) & (xs < 120)).any(), "nothing on the bridge's stretch")
        ring = np.array([[20, 126.8], [35, 126.8], [35, 129.2], [20, 129.2], [20, 126.8]], np.float64)
        self.assertTrue(bld._ml_false(ring, [], [], lines), "a lorry-sized outline on its lanes: not a building")



class SteepMachineLearnedTest(unittest.TestCase):
    """GitHub #242: machine-learned outlines on a volcano's bare slopes (rocks, shadows) are left out; huts are not."""
    def ring(self, x=100.0, y=100.0, s=6.0):
        return np.array([[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]], np.float64)

    def test_slope_and_access(self):
        r = self.ring()
        flat = np.full(5, 10.0)
        steep30 = 10.0 + np.tan(np.radians(30)) * r[:, 0]       # 30 deg rising east
        steep40 = 10.0 + np.tan(np.radians(40)) * r[:, 0]
        far = [np.array([[0.0, 0.0], [0.0, 10.0]])]             # a path ~140 m away
        near = [np.array([[90.0, 0.0], [90.0, 300.0]])]          # a path 13 m away
        self.assertFalse(bld._ml_steep(r, flat, far), "flat: kept")
        self.assertTrue(bld._ml_steep(r, steep30, far), "30 deg, no way near: out")
        self.assertFalse(bld._ml_steep(r, steep30, near), "30 deg beside a path: kept (a hut on the trail)")
        self.assertTrue(bld._ml_steep(r, steep40, near), "40 deg: out wherever it is")
        self.assertTrue(bld._ml_steep(r, steep30, []), "no ways at all")




class FarLampTest(unittest.TestCase):
    """The far and skyline tiles' street lamps (buildings.far_lamps; the user, 2026-10-27: "extend the street and
    window lights as far as the buildings render"): town streets in built-up squares, at the region's spacing; every
    ramp; nothing in empty country."""

    def test_far_lamps(self):
        import furniture
        from test_roads import features, rules, FRAME
        f = features([[(-100, 50), (400, 50)], [(-100, 150), (400, 150)]], ["residential", "motorway"])
        f.rules["subclass"] = rules([(1, "link")])
        ground = lambda la, lo: np.zeros(np.shape(la))          # noqa: E731
        lamps = bld.far_lamps(FRAME, [f], 0.0, 0.0, 256.0, {(0, 0)}, ground)
        res = [p for p in lamps if abs(p[1] - 50) < 10]
        ramp = [p for p in lamps if abs(p[1] - 150) < 10]
        gaps = np.diff(sorted(p[0] for p in res))
        self.assertTrue(np.allclose(gaps, furniture.LAMP_SPACING["residential"][0], atol=0.5), gaps)
        self.assertTrue(all(p[2] == bld.FAR_LAMP_Z for p in lamps))
        self.assertGreater(len(ramp), 3)
        na = [p for p in bld.far_lamps(FRAME, [f], 0.0, 0.0, 256.0, {(0, 0)}, ground, "na") if abs(p[1] - 50) < 10]
        self.assertLess(len(na), len(res), "North America: sparser")
        empty = bld.far_lamps(FRAME, [f], 0.0, 0.0, 256.0, set(), ground)
        self.assertEqual(empty, [], "no built-up square: none, the ramp's either (nothing to light past)")
        country = bld.far_lamps(FRAME, [f], 0.0, 0.0, 256.0, {(5, 5)}, ground)
        self.assertTrue(country and all(abs(p[1] - 150) < 10 for p in country), "elsewhere built-up: the ramp only")

    def test_skyline_windows(self):
        spec = bld.plan(rect(0, 0, 30, 20), {"class": "", "height": 30.0}, 0)
        P, UV, T, M, mats, C = bld._sky_mesh([(spec, 0.0, 0.0)])
        wall = mats.index(bld.SKY_WALL)
        self.assertIn(wall, set(M.tolist()), "the walls take the night window map")
        self.assertGreater(float(np.ptp(UV[:, 0])), 0.1, "spread along the walls")




class MappedRoofShapesTest(unittest.TestCase):
    """roof:shape forms drawn as what they are, not bent to a gable or a hip."""
    SHAPES = ("skillion", "half-hipped", "pyramidal", "mansard", "gambrel")

    def spec(self, shape, seed=None):
        ring = np.array([[0.0, 0.0], [14.0, 0.0], [14.0, 9.0], [0.0, 9.0]])
        return bld.plan(ring, {"class": "house", "roof_shape": shape, "height": 11.0}, lat=50.0, lon=9.0, look=seed)

    def faces(self, spec):
        P, _, T, *_ = bld.build([(spec, 0.0, 0.0)])
        return P[T]

    def test_the_mapped_shape_is_kept(self):
        for shape in self.SHAPES:
            self.assertEqual(self.spec(shape).roof, shape)

    def test_the_mapped_height_is_the_ridge(self):
        for shape in self.SHAPES + ("gabled",):
            s = self.spec(shape)
            rise = bld.roof_rise(s.roof, bld.oriented_box(s.ring)[3], bld.roof_pitch(s))
            self.assertAlmostEqual(s.height + rise, 11.0, delta=0.01, msg=shape)
            self.assertAlmostEqual(self.faces(s)[:, :, 2].max(), 11.0, delta=0.15, msg=shape)

    def test_every_face_looks_outwards(self):
        for shape in self.SHAPES:
            for seed in (0.2, 0.8):
                s = self.spec(shape, seed)
                f = self.faces(s)
                f = f[f[:, :, 2].min(1) >= s.height - 0.5]          # the roof and the walls under it
                n = np.cross(f[:, 1] - f[:, 0], f[:, 2] - f[:, 0])
                f, n = f[np.linalg.norm(n, axis=1) > 1e-6], n[np.linalg.norm(n, axis=1) > 1e-6]
                out = (f.mean(1) - np.array([7.0, 4.5, 0.0]))[:, :2]
                ok = (n[:, 2] > 1e-6) | ((n[:, :2] * out).sum(1) > 0)
                self.assertTrue(ok.all(), f"{shape}: {int((~ok).sum())} of {len(ok)} faces look inwards")

    def test_each_form_is_its_own_shape(self):
        tops = {}
        for shape in self.SHAPES + ("gabled", "hipped"):
            f = self.faces(self.spec(shape, 0.2))
            v = np.unique(np.round(f.reshape(-1, 3), 2), axis=0)
            tops[shape] = len(v)
        self.assertEqual(len({tops[s] for s in ("pyramidal", "mansard", "half-hipped")}), 3, tops)

    def test_a_lean_to_rises_from_one_wall_to_the_other(self):
        lows = []
        for seed in (0.2, 0.8):
            s = self.spec("skillion", seed)
            f = self.faces(s).reshape(-1, 3)
            top = f[np.argmax(f[:, 2])]
            lows.append(top[1] > 4.5)
        self.assertEqual(set(lows), {True, False}, "the high side differs from building to building")

    def test_a_nearly_square_house_can_be_a_pyramid(self):
        ring = np.array([[0.0, 0.0], [9.0, 0.0], [9.0, 9.0], [0.0, 9.0]])
        s = bld.plan(ring, {"class": "house", "roof_shape": "pyramidal"}, lat=50.0, lon=9.0)
        f = self.faces(s).reshape(-1, 3)
        top = f[np.argmax(f[:, 2])]
        self.assertAlmostEqual(top[0], 4.5, delta=0.01)
        self.assertAlmostEqual(top[1], 4.5, delta=0.01)


if __name__ == "__main__":
    unittest.main()


class PyramidTest(unittest.TestCase):
    def test_a_mapped_pyramid_is_a_pyramid(self):
        # GitHub #47: Menkaure's pyramid (66 m, roof pyramidal 66 m high) was a 66 m box with windows
        s = bld.plan(rect(0, 0, 100, 100), {"class": "", "height": 66.0, "roof_shape": "pyramidal",
                                            "roof_height": 66.0}, 0, 29.97, 31.13)
        self.assertEqual((s.cls, s.roof), ("pyramid", "hipped"))
        P = bld.build([(s, 0.0, 0.0)])[0]
        self.assertAlmostEqual(float(np.max(P[:, 2])), 66.0, delta=2.0)
        house = bld.plan(rect(0, 0, 8, 8), {"class": "house", "roof_shape": "pyramidal", "height": 8.0,
                                            "roof_height": 3.0}, 0, 29.97, 31.13)
        self.assertNotEqual(house.cls, "pyramid", "a house's pyramidal roof: a house")


class ElevatorTest(unittest.TestCase):
    def test_a_tall_slender_footprint_in_a_plains_town_is_a_grain_elevator(self):
        # Strasburg, CO: two 25 m tall footprints by the railroad came out as 8-storey brick apartments
        s = bld.plan(rect(0, 0, 11, 11), {"class": "", "height": 25.0}, 0, 39.73, -104.35, town=True)
        self.assertEqual(s.cls, "elevator")
        self.assertTrue(s.plain)
        self.assertFalse(s.windows)
        P = bld.build([(s, 0.0, 0.0)])[0]          # (a roof look without its tint failed the whole road tile)
        self.assertAlmostEqual(float(np.max(P[:, 2])), 25.0, delta=0.5)
        city = bld.plan(rect(0, 0, 11, 11), {"class": "", "height": 25.0}, 0, 39.73, -104.35)
        self.assertNotEqual(city.cls, "elevator", "in a city: a tower")
        motel = bld.plan(rect(0, 0, 50, 30), {"class": "", "height": 15.0}, 0, 39.73, -104.35, town=True)
        self.assertNotEqual(motel.cls, "elevator", "a long low block")
        floors = bld.plan(rect(0, 0, 11, 11), {"class": "", "height": 25.0, "num_floors": 8}, 0, 39.73, -104.35,
                          town=True)
        self.assertNotEqual(floors.cls, "elevator", "mapped with floors: a building of floors")


class RoadAcrossTest(unittest.TestCase):
    def test_a_narrow_footprint_across_a_road_is_over_it(self):
        # GitHub #48: the 69th Street station's 3 m wide shelters across the Dan Ryan (a road runs only their width
        # inside them, under ROAD_THROUGH_M)
        shelter = rect(-18, -1.5, 36, 3)
        across = [np.array([(0.0, -50.0), (0.0, 50.0)])]
        self.assertLess(bld.road_through(shelter, across), bld.ROAD_THROUGH_M)
        self.assertTrue(bld.road_across(shelter, across))
        self.assertFalse(bld.road_across(shelter, [np.array([(-50.0, 10.0), (50.0, 10.0)])]), "a road beside it")
        self.assertFalse(bld.road_across(shelter, [np.array([(25.0, -50.0), (25.0, 50.0)])]), "a road past its end")
        self.assertFalse(bld.road_across(rect(-18, -10, 36, 20), across), "a wide one: road_through decides")

    def test_a_long_shelter_is_a_canopy_not_a_barn(self):
        s = bld.plan(rect(0, 0, 11, 140), {"class": "shelter"}, 0, 41.77, -87.63)
        self.assertEqual(s.floors, 1)
        self.assertLess(s.height, 5.0)


class CurvatureTest(unittest.TestCase):
    def test_ground_in_the_anchor_frame(self):
        # 8 km from the anchor's origin the ground (h = 100 m everywhere) lies d^2 / 2R = ~5 m lower in the frame, as the
        # terrain and the roads have it: the building stands on that (it floated 5 m, Santa Maria Val Muestair 2026-10-18)
        import geodesy
        f = geodesy.EnuFrame(46.53, 10.45)
        x0, y0 = 0.0, 8000.0

        def ll(x, y, w, d):
            e = np.array([x, x + w, x + w, x, x]) + x0; n = np.array([y, y, y + d, y + d, y]) + y0
            la, lo, _ = f.enu_to_geodetic(e, n, 0.0)
            return np.stack([lo, la], 1)
        b = Feats(["B"], [ll(10, 10, 20, 15)], **{"class": ["house"], "height": [8.0]})
        items = bld.tile_buildings(f, x0, y0, x0 + 256, y0 + 256, [b], lambda la, lo: np.full(np.shape(la), 100.0))
        (spec, gmin, gmax), = items
        r = np.hypot(x0 + 20, y0 + 17)
        self.assertAlmostEqual(gmin, 100.0 - r * r / (2 * 6371000.0), delta=0.2)

    def test_a_flat_area_is_not_a_building(self):
        # the Golden Gate toll plaza's 0.01 m footprint drew a slab with a rim round the booths (2026-10-21)
        import geodesy
        f = geodesy.EnuFrame(37.807, -122.476)

        def ll(x, y, w, d):
            e = np.array([x, x + w, x + w, x, x]); n = np.array([y, y, y + d, y + d, y])
            la, lo, _ = f.enu_to_geodetic(e, n, 0.0)
            return np.stack([lo, la], 1)
        b = Feats(["P", "K"], [ll(10, 10, 30, 30), ll(100, 100, 10, 10)], **{"class": ["", ""], "height": [0.01, 0.2]})
        items = bld.tile_buildings(f, 0.0, 0.0, 256.0, 256.0, [b], lambda la, lo: np.full(np.shape(la), 5.0))
        self.assertEqual([round(bld._area2(s.ring) / 2) for s, _, _ in items], [100], "the plaza left out, the 0.2 m one kept")


class PhotoMaskTest(unittest.TestCase):
    """photo_mask: the buildings over a ground photo's square, grown; one piece per id across the cells."""

    def setUp(self):
        import geodesy
        self.f = geodesy.EnuFrame(39.28, -76.59)

    def cell(self, ids, boxes, heights):
        import mapgeom
        coords, coord_off = [], [0]
        for x, y, w, d in boxes:
            e = np.array([x, x + w, x + w, x, x], float); n = np.array([y, y, y + d, y + d, y], float)
            la, lo, _ = self.f.enu_to_geodetic(e, n, 0.0)
            coords.extend(zip(lo, la))
            coord_off.append(len(coords))
        k = len(ids)
        return mapgeom.Features(layer="buildings", ids=np.array(ids), kind=np.full(k, mapgeom.POLYGON, np.uint8),
                                part_off=np.arange(k + 1, dtype=np.int64), ring_off=np.arange(k + 1, dtype=np.int64),
                                coord_off=np.array(coord_off, np.int64), lonlat=np.array(coords, np.float64),
                                attrs={"height": np.array(heights, np.float64)})

    def mask(self, cells):
        class MD:
            calls = 0

            def cell(self, key, layers=None, kind=None):
                c = cells[min(MD.calls, len(cells) - 1)]
                MD.calls += 1
                return type("C", (), {"layers": {"buildings": c}})()
        return bld.photo_mask(self.f, 0.0, 0.0, 512.0, 512, MD())

    def at(self, m, e, n):
        return bool(m[int(512 - n), int(e)])      # (row 0 = north, 1 m a pixel)

    def test_inside_drawn_outside_not(self):
        a = self.cell(["A", "B", "C"], [(100, 100, 20, 20), (2000, 2000, 20, 20), (505, 300, 30, 10)], [8, 8, np.nan])
        m = self.mask([a])
        self.assertTrue(self.at(m, 110, 110), "inside")
        self.assertTrue(self.at(m, 98, 110), "grown by its edge and lean (1.5 m + 12 % of 8 m)")
        self.assertFalse(self.at(m, 95, 110))
        self.assertTrue(self.at(m, 508, 305), "one across the square's edge")
        self.assertIsNone(self.mask([self.cell(["B"], [(2000, 2000, 20, 20)], [8])]), "none over the square")

    def test_one_piece_per_id(self):
        first = self.cell(["A", "A"], [(100, 100, 20, 20), (300, 300, 20, 20)], [8, 8])
        second = self.cell(["A", "D"], [(200, 200, 20, 20), (400, 100, 20, 20)], [8, 8])
        m = self.mask([first, second])
        self.assertTrue(self.at(m, 110, 110), "the first cell's first piece")
        self.assertFalse(self.at(m, 310, 310), "not a second row of the same id")
        self.assertFalse(self.at(m, 210, 210), "nor another cell's piece of it")
        self.assertTrue(self.at(m, 410, 110), "the next building")


class RealTagsTest(unittest.TestCase):
    def test_parse_color(self):
        self.assertEqual(bld.parse_color("#fff"), (1.0, 1.0, 1.0))
        self.assertEqual(bld.parse_color("#00ff00"), (0.0, 1.0, 0.0))
        self.assertEqual(bld.parse_color("red"), (0.75, 0.15, 0.15))
        self.assertEqual(bld.parse_color("terracotta"), (0.78, 0.40, 0.26))
        self.assertEqual(bld.parse_color("copper"), (0.55, 0.75, 0.65))
        self.assertEqual(bld.parse_color("steel-blue"), (0.28, 0.45, 0.60))
        self.assertEqual(bld.parse_color("Dark Green"), (0.10, 0.35, 0.12))
        self.assertIsNone(bld.parse_color("invalid_color_xyz"))
        self.assertIsNone(bld.parse_color(None))
        self.assertIsNone(bld.parse_color(""))

    def test_facade_materials(self):
        # wood
        sw = bld.plan(rect(0, 0, 10, 10), {"class": "house", "facade_material": "wood"}, 0, 39.2, -76.6)
        self.assertTrue(str(sw.wall).startswith(f"{bld.SIDING}|w"), "wood facade maps to stained siding boards")
        # concrete
        sc = bld.plan(rect(0, 0, 10, 10), {"class": "house", "facade_material": "concrete"}, 0, 39.2, -76.6)
        self.assertTrue(str(sc.wall).startswith(bld.CONCRETE), "concrete facade maps to concrete")
        # plaster
        sp = bld.plan(rect(0, 0, 10, 10), {"class": "house", "facade_material": "plaster"}, 0, 39.2, -76.6)
        self.assertTrue(str(sp.wall).startswith(bld.STUCCO), "plaster facade maps to stucco")
        # metal
        sm = bld.plan(rect(0, 0, 10, 10), {"class": "house", "facade_material": "metal"}, 0, 39.2, -76.6)
        self.assertTrue(str(sm.wall).startswith(bld.METALWALL), "metal facade maps to metalwall")
        # stone
        ss = bld.plan(rect(0, 0, 10, 10), {"class": "house", "facade_material": "stone"}, 0, 39.2, -76.6)
        self.assertTrue(str(ss.wall).startswith(bld.STONE), "stone facade maps to stone")
        # glass
        sg = bld.plan(rect(0, 0, 20, 20), {"class": "office", "facade_material": "glass"}, 0, 39.2, -76.6)
        self.assertTrue(str(sg.facade).startswith(bld.OFFICE), "glass facade maps to office curtain wall")

    def test_facade_color(self):
        s = bld.plan(rect(0, 0, 10, 10), {"class": "house", "facade_color": "red"}, 0, 39.2, -76.6)
        mat_idx = bld._Looks(s.seed)
        _, rgba = mat_idx[s.facade]
        # Red component should be significantly higher than green or blue
        self.assertGreater(rgba[0], 150)
        self.assertLess(rgba[1], 80)
        self.assertLess(rgba[2], 80)

        # Hex color
        s2 = bld.plan(rect(0, 0, 10, 10), {"class": "house", "facade_color": "#00ff00"}, 0, 39.2, -76.6)
        mat_idx2 = bld._Looks(s2.seed)
        _, rgba2 = mat_idx2[s2.facade]
        self.assertLess(rgba2[0], 50)
        self.assertGreater(rgba2[1], 200)
        self.assertLess(rgba2[2], 50)

    def test_roof_material_and_color(self):
        # Metal roof
        sm = bld.plan(rect(0, 0, 10, 10), {"class": "house", "roof_material": "metal"}, 0, 39.2, -76.6)
        self.assertTrue(str(sm.roof_mat).startswith(bld.ROOF_METAL))

        # Copper roof has copper patina tint
        sc = bld.plan(rect(0, 0, 10, 10), {"class": "house", "roof_material": "copper"}, 0, 39.2, -76.6)
        self.assertTrue(str(sc.roof_mat).startswith(bld.ROOF_METAL))
        mat_idx = bld._Looks(sc.seed)
        _, rgba = mat_idx[sc.roof_mat]
        self.assertGreater(rgba[1], rgba[0])  # green tint from copper patina

        # Tile roof
        st = bld.plan(rect(0, 0, 10, 10), {"class": "house", "roof_material": "tile"}, 0, 39.2, -76.6)
        self.assertTrue(str(st.roof_mat).startswith(bld.ROOF_SLATE))
        k = int(st.roof_mat.split("|")[1])
        self.assertGreaterEqual(k, 4)

        # Slate roof
        ss = bld.plan(rect(0, 0, 10, 10), {"class": "house", "roof_material": "slate"}, 0, 39.2, -76.6)
        self.assertTrue(str(ss.roof_mat).startswith(bld.ROOF_SLATE))

        # Shingle roof
        ssh = bld.plan(rect(0, 0, 10, 10), {"class": "house", "roof_material": "shingle"}, 0, 39.2, -76.6)
        self.assertTrue(str(ssh.roof_mat).startswith(bld.ROOF_SHINGLE))

        # Roof color hex
        s_rc = bld.plan(rect(0, 0, 10, 10), {"class": "house", "roof_material": "metal", "roof_color": "#1a2b3c"}, 0, 39.2, -76.6)
        mat_idx = bld._Looks(s_rc.seed)
        _, rgba = mat_idx[s_rc.roof_mat]
        self.assertAlmostEqual(rgba[0] / 255.0, 0x1a / 255.0, delta=0.08)
        self.assertAlmostEqual(rgba[1] / 255.0, 0x2b / 255.0, delta=0.08)
        self.assertAlmostEqual(rgba[2] / 255.0, 0x3c / 255.0, delta=0.08)

    def test_building_part_color_and_roof(self):
        import geodesy
        f = geodesy.EnuFrame(37.8, -122.4)
        def ll(x, y, w, d):
            e = np.array([x, x + w, x + w, x, x]); n = np.array([y, y, y + d, y + d, y])
            la, lo, _ = f.enu_to_geodetic(e, n, 0.0)
            return np.stack([lo, la], 1)
        # Parent building with two parts
        b = Feats(["B1"], [ll(10, 10, 20, 20)], **{"class": ["commercial"], "height": [30.0],
                                                    "facade_color": ["#ff0000"], "roof_color": ["#0000ff"]})
        parts = Feats(["P1", "P2"], [ll(10, 10, 20, 20), ll(12, 12, 16, 16)],
                      **{"building_id": ["B1", "B1"], "height": [10.0, 30.0], "min_height": [0.0, 10.0],
                         "roof_shape": ["flat", "hipped"], "facade_color": ["#ff0000", "#00ff00"],
                         "roof_color": ["#0000ff", "#ffff00"]})
        items = bld.tile_buildings(f, 0.0, 0.0, 256.0, 256.0, [b], lambda la, lo: np.zeros(np.shape(la)),
                                   part_layers=[parts])
        self.assertEqual(len(items), 2)
        p1_spec, p2_spec = items[0][0], items[1][0]
        # P1 has red facade and blue roof
        _, p1_rgba = bld._Looks(p1_spec.seed)[p1_spec.facade]
        self.assertGreater(p1_rgba[0], 200)
        self.assertLess(p1_rgba[1], 50)
        # P2 has green facade (overridden on the part) and yellow roof
        _, p2_rgba = bld._Looks(p2_spec.seed)[p2_spec.facade]
        self.assertLess(p2_rgba[0], 50)
        self.assertGreater(p2_rgba[1], 200)
        self.assertEqual(p2_spec.roof, "hipped")


class ModelBridgeTest(unittest.TestCase):
    """A 3dmr model bridge carries the road (FV=24): _custom_tile_entries bakes it leaned onto the mapped bridge's
    ends, drawn without collision ("col": False) beside its deck slab (threedmr.write_bridge_deck); the procedural
    bridge under it is left out of every tile (bridge_stub_*), wherever its footprint reaches."""

    FP = {"bridge": True, "ax": 0, "u0": -100.0, "du": 2.0, "cols": [[-10.0, 10.0]] * 101}

    def setUp(self):
        import geodesy
        self.f = geodesy.EnuFrame(16.0612, 108.2279)

    @staticmethod
    def _slab(length=200.0, width=20.0):
        P, T = [], []
        for z in (5.0, 3.0):        # a flat two-sided deck slab: top and its lower skin, each face doubled
            n = len(P)
            P += [[-length / 2, -width / 2, z], [length / 2, -width / 2, z], [length / 2, width / 2, z],
                  [-length / 2, width / 2, z]]
            T += [[n, n + 1, n + 2], [n, n + 2, n + 3], [n, n + 2, n + 1], [n, n + 3, n + 2]]
        P = np.array(P, np.float64)
        return (P, np.zeros((len(P), 2)), np.array(T, np.int64), None, [(None, None, None, False, None)],
                np.zeros(len(T), np.int64))

    def _bake(self, **patches):
        custom = [{"id": 22, "xy": (10.0, 10.0)}]
        ground = lambda la, lo: np.zeros(np.shape(la))
        res = {"shape": "threedmr_22.cdae", "texture": None, "material": "threedmr_22", "has_alpha": False,
               "alpha_cutoff": None}
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s1")
            os.makedirs(out)
            with mock.patch("threedmr.fetch_model", return_value=b"glb"), \
                 mock.patch("threedmr.gltf_mesh", return_value=self._slab()), \
                 mock.patch("threedmr.write_model", return_value=res) as write_model, \
                 mock.patch("threedmr.update_materials", return_value=None), \
                 mock.patch("revisit.fault") as fault:
                with mock.patch.multiple("threedmr", **patches) if patches else mock.patch("os.sep", os.sep):
                    entries, radii, _b = bld._custom_tile_entries(
                        self.f, custom, ground, out, "/temp/beamngEarth/s1",
                        road_ends={22: [(-80.0, 10.0, 1.0), (100.0, 10.0, 2.0)]})
                files = sorted(os.listdir(out))
        return entries, write_model, fault, files

    def test_bridge_drawn_without_collision_beside_its_deck_slab(self):
        entries, write_model, fault, files = self._bake()
        self.assertEqual([(e["shape"].rsplit("/", 1)[1], e.get("col", True)) for e in entries],
                         [("threedmr_22.cdae", False), ("threedmr_22_deck.cdae", True)])
        self.assertIn("threedmr_22_deck.cdae", files)
        tilt = write_model.call_args.kwargs["tilt"]
        self.assertEqual(tilt[4], (-90.0, 90.0), "the deck's span: the mapped bridge's ends (model frame)")
        top = 5.0 - threedmr._support_z(np.array([5.0] * 4 + [3.0] * 4)) - 0.2     # (the deck's top as bedded)
        self.assertAlmostEqual(threedmr.tilt_dz(tilt, [[-90.0, 0.0]])[0] + top, 1.0 + threedmr.BRIDGE_END_EPS_M,
                               delta=0.01)
        fault.assert_not_called()

    def test_a_failed_slab_keeps_the_models_own_collision(self):
        entries, _w, fault, _f = self._bake(write_bridge_deck=mock.Mock(side_effect=OSError("disk")))
        self.assertEqual([(e["shape"].rsplit("/", 1)[1], e.get("col", True)) for e in entries],
                         [("threedmr_22.cdae", True)])
        fault.assert_called()

    def test_stub_chunks_under_the_model_left_out(self):
        fps = [(self.FP, (0.0, 0.0), 22)]
        line = np.c_[np.linspace(-50, 50, 11), np.zeros(11), np.full(11, 9.0)]
        chunks = [{"deck": True, "fixed": True, "n": line}, {"fixed": True, "m": "line_white", "n": line},
                  {"m": "road", "n": line}, {"fixed": True, "tunnel": True, "deck": True, "n": line},
                  {"deck": True, "fixed": True, "n": line + [0.0, 60.0, 0.0]}]
        kept = bld.bridge_stub_chunks(fps, chunks)
        self.assertEqual([i for i, c in enumerate(chunks) if any(c is k for k in kept)], [2, 3, 4],
                         "the deck and its line under the model go; a ground road, a tunnel, a deck beside stay")
        self.assertIs(bld.bridge_stub_chunks([], chunks), chunks)

    def test_stub_parts_piers_props_left_out(self):
        fps = [(self.FP, (0.0, 0.0), 22)]
        parts = [("a", None, np.array([[-50.0, 0.0], [50.0, 0.0]]), np.array([9.0, 9.0]), None),
                 ("b", None, np.array([[-50.0, 60.0], [50.0, 60.0]]), np.array([1.0, 1.0]), None)]
        self.assertEqual([p[0] for p in bld.bridge_stub_parts(fps, parts)], ["b"])
        piers, beams = bld.bridge_stub_piers(fps, [(0.0, 0.0, 1.0), (0.0, 50.0, 1.0)],
                                             [((-5.0, 0.0, 9.0), (5.0, 0.0, 9.0)), ((-5.0, 50.0, 9.0), (5.0, 50.0, 9.0))])
        self.assertEqual((len(piers), piers[0][1], len(beams), beams[0][0][1]), (1, 50.0, 1, 50.0))
        props = [[0, 10.0, 0.0, 9.0], [0, 10.0, 40.0, 1.0]]
        self.assertEqual(bld.bridge_stub_props(fps, props), [[0, 10.0, 40.0, 1.0]])

    def test_road_ends_of_the_mapped_bridge_under_the_model(self):
        class Bridges:
            def parts(self, cells, box):
                self.box = box
                return [("a", None, np.array([[-80.0, 2.0], [0.0, 2.0], [90.0, 2.0]]), np.array([1.0, 7.0, 2.0]), None),
                        ("b", None, np.array([[-50.0, 60.0], [50.0, 60.0]]), np.array([1.0, 1.0]), None)]
        b = Bridges()
        got = bld.bridge_road_ends([(self.FP, (1000.0, 0.0), 22)], b, [])
        self.assertEqual(b.box, (900.0, -10.0, 1100.0, 10.0))
        self.assertEqual(got, {})       # (the parts are near the origin, the model at x 1000: none under it)
        got = bld.bridge_road_ends([(self.FP, (0.0, 0.0), 22)], b, [])
        self.assertEqual(got, {22: [(-80.0, 2.0, 1.0), (90.0, 2.0, 2.0)]})
