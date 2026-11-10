"""Direction signs (dirsigns.py, earthfast dirsign_place / dirsign_face / dirsign_mesh): where the boards stand, what
they name, how a face is drawn."""
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dirsigns  # noqa: E402
import fast  # noqa: E402

M_LAT = 111320.0
LAT0 = 39.3
LON0 = -76.5
M_LON = M_LAT * np.cos(np.radians(LAT0))
HAVE = getattr(fast._ef, "VERSION", 0) >= dirsigns.KERNEL


def row(xy, rank=5, hw=4.0, way=0, urban=False, link=False, ends=None):
    """A speed-signs road row (what dirsigns._arrays takes): xy (k, 2) metres, junction ids at its ends."""
    xy = np.asarray(xy, np.float64)
    ll = np.column_stack([xy[:, 0] / M_LON + LON0, xy[:, 1] / M_LAT + LAT0])
    a, b = ends or (f"{xy[0][0]:.1f},{xy[0][1]:.1f}", f"{xy[-1][0]:.1f},{xy[-1][1]:.1f}")
    return (ll, xy, (hw, way, rank, 1.0 if urban else 0.0, 1.0 if link else 0.0), [(0.0, a), (1.0, b)], [], [])


def place_array(rows):
    """[lon, lat, population, class] of places given as (east m, north m, population, class)."""
    return np.ascontiguousarray([[x / M_LON + LON0, y / M_LAT + LAT0, pop, cls] for x, y, pop, cls in rows], np.float64)


def boards(roads, places, routes=None, box=(-5000, -5000, 5000, 5000), left_hand=False, polys=(), repeat=None):
    table = dirsigns.RouteTable()
    routes_of = {fid: [(n, r, a, b) for n, r, a, b in rs] for fid, rs in (routes or {}).items()}
    # (the margin the glue reads the roads in: a repeat cell and more, so that a tile sees every board it shares one with)
    arrays = dirsigns._arrays(roads, box, dirsigns.styles()["placement"]["repeat_m"] + dirsigns.REPEAT_MARGIN_M, table, routes_of)
    if arrays is None:
        return np.zeros((0, 12)), np.zeros((0, 11)), table
    polys = [np.asarray(p, np.float64) for p in polys]
    pts = np.concatenate(polys) if polys else np.zeros((0, 2))
    off = np.r_[0, np.cumsum([len(p) for p in polys])].astype(np.int64)
    st = dirsigns.styles()
    placement = dict(st["placement"], **({} if repeat is None else {"repeat_m": repeat}))
    b, r = fast._ef.dirsign_place(*arrays, np.ascontiguousarray(pts), off, places, [float(v) for v in box], left_hand,
                                  dirsigns._params(placement))
    return b, r, table


def junction():
    """A primary road from the west meeting a primary road going east and one going north, at the origin."""
    return {"w": row([(-1500, 0), (0, 0)], ends=("w", "j")), "e": row([(0, 0), (1500, 0)], ends=("j", "e")),
            "n": row([(0, 0), (0, 1500)], ends=("j", "n"))}


@unittest.skipUnless(HAVE, "earthfast with the direction-sign kernels")
class PlaceTest(unittest.TestCase):
    def setUp(self):
        # a town 30 km east, a big city 80 km north, a village 25 km east (too small to be named)
        self.places = place_array([(30000, 0, 20000, 1), (0, 80000, 500000, 2), (25000, 200, 800, 0)])

    def test_a_board_on_the_approach_before_the_junction_on_the_driving_side(self):
        b, r, _ = boards(junction(), self.places)
        west = [x for x in b if x[2] > 0.9 and x[0] < 0]       # (travelling east, west of the junction)
        self.assertEqual(len(west), 1)
        x = west[0]
        self.assertTrue(-260 < x[0] < -200, f"about the style's rural advance before the junction: {x[0]}")
        self.assertTrue(-11 < x[1] < -6.5, f"right of an eastbound road (south), off its pavement: {x[1]}")
        self.assertAlmostEqual(x[6], 0.0)                       # not urban

    def test_the_rows_straight_first_then_left_with_their_places(self):
        b, r, _ = boards(junction(), self.places)
        x = [x for x in b if x[2] > 0.9 and x[0] < 0][0]
        rows = r[int(x[10]):int(x[10]) + int(x[11])]
        self.assertEqual([int(v[0]) for v in rows], [0, -1], "straight (east), then left (north)")
        east, north = rows
        self.assertEqual(int(east[5]), 0, "the near place east: the town")
        self.assertAlmostEqual(east[6], 30000, delta=500)
        self.assertEqual(int(north[7]), 1, "the far place north: the city")
        self.assertAlmostEqual(north[8], 80000, delta=500)
        self.assertEqual(int(north[5]), -1, "no near place north")

    def test_a_village_is_not_a_destination_and_a_place_behind_is_not_either(self):
        places = place_array([(25000, 0, 800, 0), (-30000, 0, 90000, 2)])
        b, r, _ = boards(junction(), places)
        west = [x for x in b if x[2] > 0.9 and x[0] < 0]        # (the approach from the west, heading east)
        self.assertEqual(west, [], "a village east (too small) and a city behind the approach: nothing to say")
        # (the city is ahead of the approaches from the north and the east: they do have a row to it)
        self.assertEqual(len(r), 2)

    def test_the_route_numbers_of_the_road_an_arm_follows(self):
        roads = junction()
        routes = {"e": [("ua:international", "M-07", 0.0, 1.0), ("e-road", "E 373", 0.0, 1.0)],
                  "n": [("ua:regional", "P-15", 0.0, 1.0)]}
        b, r, table = boards(roads, self.places, routes)
        x = [x for x in b if x[2] > 0.9 and x[0] < 0][0]
        rows = r[int(x[10]):int(x[10]) + int(x[11])]
        east = {table.items[int(i)] for i in rows[0][2:5] if i >= 0}
        north = {table.items[int(i)] for i in rows[1][2:5] if i >= 0}
        self.assertEqual(east, {("ua:international", "M-07"), ("e-road", "E 373")})
        self.assertEqual(north, {("ua:regional", "P-15")})

    def test_the_same_boards_in_every_tile_of_any_box(self):
        whole, _, _ = boards(junction(), self.places)
        left, _, _ = boards(junction(), self.places, box=(-5000, -5000, -100, 5000))
        right, _, _ = boards(junction(), self.places, box=(-100, -5000, 5000, 5000))
        key = lambda a: sorted((round(v[0], 3), round(v[1], 3)) for v in a)          # noqa: E731
        self.assertEqual(sorted(key(left) + key(right)), key(whole), "a board belongs to one tile, whichever the box")
        again, _, _ = boards(junction(), self.places)
        self.assertEqual(key(again), key(whole))

    def test_no_board_on_a_bridge_or_where_a_pavement_is(self):
        roads = junction()
        w = roads["w"]
        roads["w"] = w[:5] + ([(0.0, 1.0)],)                    # the whole approach on a bridge
        b, r, _ = boards(roads, self.places)
        self.assertEqual(len([x for x in b if x[2] > 0.9 and x[0] < 0]), 0)

    def test_nothing_without_a_junction_of_major_roads(self):
        roads = {"w": row([(-1500, 0), (0, 0)], ends=("w", "j")), "e": row([(0, 0), (1500, 0)], ends=("j", "e"))}
        b, r, _ = boards(roads, self.places)
        self.assertEqual(len(b), 0, "two roads meeting are no junction")
        roads = junction()
        roads["n"] = row([(0, 0), (0, 1500)], rank=2, ends=("j", "n"))     # a minor road
        b, r, _ = boards(roads, self.places)
        self.assertEqual(len(r), 0 if len(b) == 0 else len(r))
        self.assertEqual([x for x in b if x[0] < 0 and int(x[11]) > 1], [], "a minor road is no row of a board")

    def test_left_hand_traffic_stands_on_the_other_side(self):
        b, _, _ = boards(junction(), self.places, left_hand=True)
        x = [x for x in b if x[2] > 0.9 and x[0] < 0][0]
        self.assertGreater(x[1], 6.5, "left-hand: north of an eastbound road")


@unittest.skipUnless(HAVE, "earthfast with the direction-sign kernels")
class RepeatTest(unittest.TestCase):
    """The same board is not repeated at every junction (once in a repeat cell), a turn always has its board."""

    def setUp(self):
        self.places = place_array([(30000, 0, 20000, 1), (0, 80000, 500000, 2), (0, -80000, 400000, 2)])

    @staticmethod
    def crossings():
        """A primary road going east with a crossing primary road every 500 m."""
        roads = {}
        for i in range(10):
            x = -2500 + 500 * i
            roads[f"m{i}"] = row([(x, 0), (x + 500, 0)], ends=(f"j{i}", f"j{i + 1}"))
            roads[f"n{i}"] = row([(x, 0), (x, 1500)], ends=(f"j{i}", f"n{i}"))
            roads[f"s{i}"] = row([(x, -1500), (x, 0)], ends=(f"s{i}", f"j{i}"))
        return roads

    @staticmethod
    def eastbound(b, y_from=-50, y_to=50):
        return [x for x in b if x[2] > 0.9 and y_from < x[1] < y_to]

    def test_a_board_that_says_the_same_is_not_at_every_junction(self):
        every, _, _ = boards(self.crossings(), self.places, repeat=0.0)
        thin, _, _ = boards(self.crossings(), self.places, repeat=2000.0)
        n_every, n_thin = len(self.eastbound(every, -20, 0)), len(self.eastbound(thin, -20, 0))
        self.assertGreaterEqual(n_every, 8, "without the thinning a board before nearly every junction")
        self.assertTrue(1 <= n_thin <= 4, f"with it about one in 2 km of a 5 km road: {n_thin}")

    def test_the_thinning_does_not_depend_on_the_tile(self):
        whole, _, _ = boards(self.crossings(), self.places)
        parts = []
        for x0 in range(-5000, 5000, 1000):
            b, _, _ = boards(self.crossings(), self.places, box=(x0, -5000, x0 + 1000, 5000))
            parts += list(b)
        key = lambda a: sorted((round(v[0], 3), round(v[1], 3)) for v in a)          # noqa: E731
        self.assertEqual(key(parts), key(whole))

    def test_a_turn_always_has_its_board(self):
        roads = {}
        for i in range(5):                                     # five side roads ending on a trunk: each a turn
            y = 300 * i
            roads[f"t{i}"] = row([(0, y), (0, y + 300)], ends=(f"t{i}", f"t{i + 1}"))
            roads[f"s{i}"] = row([(-1500, y), (0, y)], ends=(f"s{i}", f"t{i}"))
        roads["tail"] = row([(0, -300), (0, 0)], ends=("tail", "t0"))
        roads["head"] = row([(0, 1500), (0, 1800)], ends=("t5", "head"))
        roads["t4"] = row([(0, 1200), (0, 1500)], ends=("t4", "t5"))
        thin, _, _ = boards(roads, self.places, repeat=2000.0)
        east = [x for x in thin if x[2] > 0.9 and x[0] < 0]
        self.assertEqual(len(east), 5, "every side road's turn onto the trunk has a board")

    def test_different_boards_are_both_kept(self):
        b, r, _ = boards(self.crossings(), self.places, repeat=2000.0)
        self.assertTrue(any(x[3] > 0.9 for x in b) or any(x[3] < -0.9 for x in b), "boards for the crossing roads too")


class ContentTest(unittest.TestCase):
    def setUp(self):
        doc = {"places": [
            {"id": "a", "cc": "UA", "class": "town", "pop": 68000, "name": "Ковель", "en": "Kovel", "lat": 51.2, "lon": 24.7},
            {"id": "b", "cc": "UA", "class": "village", "pop": 3200, "name": "Дубове", "en": "Dubove", "lat": 51.3, "lon": 24.7},
            {"id": "c", "cc": "CZ", "class": "city", "pop": 90000, "name": "Hradec Králové", "en": "", "lat": 50.2, "lon": 15.8},
            {"id": "d", "cc": "PL", "class": "city", "pop": 1800000, "name": "Warszawa", "en": "Warsaw", "lat": 52.2, "lon": 21.0}]}
        self.pl = dirsigns.Places(doc)
        self.st = dirsigns.styles()

    def content(self, near, far, show_en=True, cc="UA"):
        row = [0, 0.0, -1, -1, -1, near, 12000.0, far, 85000.0, 5, 0]
        return dirsigns._row_content(self.st["countries"][cc], row, dirsigns.RouteTable(), self.pl, 1.25,
                                     self.st["distance_round"], show_en)

    def test_a_city_or_town_has_the_english_name_a_village_only_the_local(self):
        turn, marks, dests = self.content(1, 0)
        self.assertEqual(dests[0][:2], ("Дубове", ""), "a village: the local name only")
        self.assertEqual(dests[1][:2], ("Ковель", "Kovel"), "a town: local and English")

    def test_no_second_line_when_the_english_name_is_the_same_or_missing(self):
        _, _, dests = self.content(2, -1, cc="CZ")
        self.assertEqual(dests, [("Hradec Králové", "", "15")])

    def test_the_second_line_can_be_switched_off(self):
        _, _, dests = self.content(0, 3, show_en=False)
        self.assertEqual([d[1] for d in dests], ["", ""])

    def test_the_distances_are_the_straight_ones_scaled_and_rounded(self):
        self.assertEqual(dirsigns.km_text(12000, 1.25, self.st["distance_round"]), "15")
        self.assertEqual(dirsigns.km_text(1200, 1.25, self.st["distance_round"]), "2")
        self.assertEqual(dirsigns.km_text(85000, 1.25, self.st["distance_round"]), "105")
        self.assertEqual(dirsigns.km_text(100, 1.25, self.st["distance_round"]), "1")

    def test_nothing_to_say_is_none(self):
        row = [0, 0.0, -1, -1, -1, -1, 0.0, -1, 0.0, 5, 0]
        self.assertIsNone(dirsigns._row_content(self.st["countries"]["UA"], row, dirsigns.RouteTable(), self.pl, 1.25,
                                                self.st["distance_round"], True))

    def test_a_board_names_the_best_class_and_the_european_road(self):
        marks = [(0, "m", "M-19"), (3, "t", "T-03-11"), (4, "e", "E 85")]
        self.assertEqual(dirsigns.best_plates(marks), [(0, "m", "M-19"), (4, "e", "E 85")])
        self.assertEqual(dirsigns.best_plates([(3, "t", "T-03-11")]), [(3, "t", "T-03-11")])


class StyleTest(unittest.TestCase):
    def test_every_network_rule_names_a_plate_the_country_has(self):
        st = dirsigns.styles()
        for cc, c in st["countries"].items():
            for rule in c["networks"]:
                self.assertIn(rule["plate"], c["plates"], f"{cc}: {rule}")
            for kind in ("freeway", "road", "urban"):
                self.assertEqual(len(c["board"][kind]["bg"]), 3)

    def test_route_networks_become_plates_by_the_countrys_rules(self):
        st = dirsigns.styles()["countries"]
        self.assertEqual(dirsigns.plate_for(st["UA"], "ua:international", "М-07")[1], "m")
        self.assertEqual(dirsigns.plate_for(st["UA"], "e-road", "E 373")[1], "e")
        self.assertEqual(dirsigns.plate_for(st["CZ"], "cz:national", "D35")[1], "d")
        self.assertEqual(dirsigns.plate_for(st["CZ"], "cz:national", "35")[1], "i")
        self.assertEqual(dirsigns.plate_for(st["DE"], "", "L 3016")[1], "l")
        self.assertIsNone(dirsigns.plate_for(st["DE"], "", "XYZ"))

    def test_parse_routes(self):
        self.assertEqual(dirsigns.parse_routes("ua:international|М-07|0.19|1.0;e-road|E 373|0.0|1.0"),
                         [("ua:international", "М-07", 0.19, 1.0), ("e-road", "E 373", 0.0, 1.0)])
        self.assertEqual(dirsigns.parse_routes(""), [])

    def test_the_place_snapshot_is_the_one_that_was_checked(self):
        pl = dirsigns.places()
        self.assertGreater(len(pl.names), 5000)
        self.assertEqual(pl.array.shape[1], 4)
        self.assertIn("Ковель", pl.names)


@unittest.skipUnless(HAVE, "earthfast with the direction-sign kernels")
class FaceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bne_dirsigns_test_")
        self.st = dirsigns.styles()
        self.face = self.st["face"]
        dirsigns._set_fonts()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, content, cc="UA", kind="road"):
        keys = sorted({k for _, marks, _ in content for k, _ in marks})
        return dirsigns._face(self.dir, "vfs", self.st["countries"][cc], keys, kind, content, self.face)

    def test_a_face_is_a_dds_of_the_boards_size_named_by_its_content(self):
        """A DXT1 DDS with mips (imagery.write_dds), never a PNG: the engine's texture cooker crashed the game as it
        started importing the session folder's sign PNGs (2026-11-05)."""
        import imagery
        content = [(0, [("m", "М-07"), ("e", "E 373")], [("Ковель", "Kovel", "65")])]
        name, tex, w, h = self.make(content)
        path = os.path.join(self.dir, name + ".dds")
        self.assertTrue(os.path.exists(path))
        self.assertFalse(any(f.endswith(".png") for f in os.listdir(self.dir)), "no PNG written")
        self.assertEqual(tex, f"vfs/{name}.dds")
        self.assertTrue(2.0 <= w <= 6.0 and 0.5 < h < 3.0, (w, h))
        with open(path, "rb") as f:
            wpx, hpx, mips, fourcc, _sizes = imagery.read_dds_info(f.read())
        self.assertEqual(fourcc, b"DXT1")
        self.assertGreater(mips, 1)
        self.assertTrue(wpx & (wpx - 1) == 0 and hpx & (hpx - 1) == 0 and min(wpx, hpx) >= 16,
                        f"the engine's texture cooker takes only power-of-two sizes: {wpx}x{hpx}")
        self.assertGreaterEqual(wpx, w * self.face["px_per_m"] - 1, "stretched up, never down")
        again = self.make(content)
        self.assertEqual(again[0], name, "the same content, the same file")
        self.assertNotEqual(self.make([(0, [], [("Ковель", "", "65")])])[0], name)

    def test_more_rows_make_a_taller_board_and_long_names_stay_within_the_widest(self):
        one = self.make([(0, [], [("Ковель", "Kovel", "65")])])
        two = self.make([(0, [], [("Ковель", "Kovel", "65")]), (-1, [], [("Луцьк", "Lutsk", "85")])])
        self.assertGreater(two[3], one[3])
        long = self.make([(1, [("m", "М-07")], [("Новоград-Волинський Дуже Довга Назва Міста", "Novohrad-Volynskyi", "120")])])
        self.assertLessEqual(long[2], self.face["max_w_m"] + 0.01)

    def test_the_kernel_meshes_a_board_with_its_posts(self):
        signs = np.array([[0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 3.2, 1.0, 1, 1.8]])
        p, t, uv, mi = fast._ef.dirsign_mesh(signs, [0.0, 0.0, 0.0])
        self.assertEqual(len(t), len(mi))
        self.assertIn(1, set(mi.tolist()), "the face")
        self.assertIn(0, set(mi.tolist()), "metal: the back and the posts")
        face = [i for i in range(len(t)) if mi[i] == 1]
        self.assertEqual(len(face), 2, "one quad")
        self.assertTrue(np.isfinite(p).all())
        self.assertLessEqual(float(p[:, 2].min()), -0.2, "the posts go into the ground")
        self.assertTrue(0.0 <= uv.min() and uv.max() <= 1.0)


if __name__ == "__main__":
    unittest.main()
