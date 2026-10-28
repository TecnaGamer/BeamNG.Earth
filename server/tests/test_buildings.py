"""buildings: footprints become closed walls and a roof, heights predicted where the data has none,
deterministic, every triangle facing out."""
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings as bld  # noqa: E402
import cdae  # noqa: E402


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
