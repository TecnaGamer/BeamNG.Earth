"""bgsigns: Bulgaria's С7 bend boards and Д11 / Д12 town signs (Наредба № РД-02-21-1 от 2023), the Transliteration
Act's Latin names."""
import json
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bgsigns  # noqa: E402
import roads  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def bend(radius, turn_deg, lead=200.0, tail=200.0, step=2.0):
    """A straight, a left bend of radius turning turn_deg, a straight (x east, y north)."""
    pts = [(x, 0.0) for x in np.arange(-lead, 0.0, step)]
    a = np.linspace(0.0, math.radians(turn_deg), max(3, int(radius * math.radians(turn_deg) / step)))
    pts += [(radius * math.sin(t), radius - radius * math.cos(t)) for t in a]
    end, h = np.array(pts[-1]), a[-1]
    pts += [tuple(end + s * np.array([math.cos(h), math.sin(h)])) for s in np.arange(step, tail, step)]
    return np.array(pts)


class TransliterationTest(unittest.TestCase):
    def test_the_acts_table_and_its_rules(self):
        for bg, latin in (("София", "Sofia"), ("Стара Загора", "Stara Zagora"), ("Шумен", "Shumen"),
                          ("Щръклево", "Shtraklevo"), ("Вършец", "Varshets"), ("Горна Оряховица", "Gorna Oryahovitsa"),
                          ("Кюстендил", "Kyustendil"), ("Козлодуй", "Kozloduy"), ("Ихтиман", "Ihtiman"),
                          ("Цар Калоян", "Tsar Kaloyan"), ("Джебел", "Dzhebel"), ("Дъбрава", "Dabrava")):
            self.assertEqual(bgsigns.translit(bg), latin)
        self.assertEqual(bgsigns.translit("България"), "Bulgaria", "Art. 6")
        self.assertEqual(bgsigns.translit("Ия"), "Ia", "final ия: ia (Art. 5 (2))")
        self.assertEqual(bgsigns.translit("Ияново"), "Iyanovo", "not at the end: и-я")
        self.assertEqual(bgsigns.translit("ЖАБА"), "ZHABA")

    def test_every_letter_of_a_name_is_in_the_atlas(self):
        glyphs = bgsigns.lettering()["glyphs"]
        for name in ("Стара Загора", "Велико Търново", "Гоце Делчев", "Ѝ"):
            self.assertTrue(all(c in glyphs for c in name + bgsigns.translit(name)), name)


class C7Test(unittest.TestCase):
    def test_a_dangerous_bend(self):
        got = bgsigns.bends(bend(100.0, 90.0))
        self.assertEqual(len(got), 1)
        s0, s1, r, sign = got[0]
        self.assertAlmostEqual(r, 100.0, delta=8.0)
        self.assertEqual(sign, 1)
        self.assertAlmostEqual(s0, 200.0, delta=10.0)
        self.assertAlmostEqual(s1 - s0, 100.0 * math.pi / 2, delta=12.0)

    def test_not_dangerous(self):
        self.assertEqual(bgsigns.bends(bend(300.0, 90.0)), [], "R > 200 m")
        self.assertEqual(bgsigns.bends(bend(150.0, 25.0)), [], "turns less than 30°")
        self.assertEqual(bgsigns.bends(np.array([[0.0, 0.0], [500.0, 0.0]])), [])

    def test_spacing_is_annex_23s(self):
        self.assertEqual(bgsigns.sc_of(100.0), 20.0)
        self.assertEqual(bgsigns.sc_of(150.0), 25.0, "interpolated")
        self.assertEqual(bgsigns.sc_of(20.0), 6.0)
        posts = bgsigns.c7_posts(bend(100.0, 90.0), 7.0)
        gaps = np.hypot(*np.diff(np.array([q for q, _, _ in posts]), axis=0).T)
        self.assertTrue(7 <= len(posts) <= 9, len(posts))
        self.assertTrue(np.allclose(gaps, gaps.mean(), rtol=0.05) and 17.0 < gaps.mean() < 23.0, gaps)

    def test_posts_stand_outside_the_bend_facing_each_way(self):
        xy = bend(100.0, 90.0)
        centre = np.array([0.0, 100.0])
        for q, boards, outside in bgsigns.c7_posts(xy, 7.0):
            self.assertAlmostEqual(np.hypot(*(q - centre)), 100.0 + 3.5 + bgsigns.C7_SIDE_M + 0.05, delta=1.5,
                                   msg="0.50 m past the edge line, on the outside")
            self.assertEqual(len(boards), 2, "a two-way road: one board each way")
            (fa, aa), (fb, ab) = boards
            self.assertAlmostEqual(float(np.dot(fa, fb)), 0.0, places=6, msg="at a right angle")
            self.assertEqual({aa, ab}, {-1, 1}, "the road turns left one way, right the other")
            self.assertLess(float(np.dot(fa, outside)), 0.0, "towards the road")
        one = bgsigns.c7_posts(xy, 7.0, oneway=True, direction=1)
        self.assertTrue(all(len(b) == 1 and b[0][1] == -1 for _, b, _ in one), "one-way: one board, pointing left")


class TownTest(unittest.TestCase):
    LINE = np.array([[0.0, 0.0], [1000.0, 0.0]])

    def built(self, start, end):
        p, s = bgsigns._resample(self.LINE, bgsigns.TOWN_STEP_M)
        return (s >= start) & (s < end)

    def test_town_edges(self):
        self.assertEqual(bgsigns.town_edges(self.LINE, self.built(400.0, 900.0)), [(400.0, 1)])
        self.assertEqual(bgsigns.town_edges(self.LINE, self.built(0.0, 600.0)), [(600.0, -1)], "leaving: in against")
        self.assertEqual(bgsigns.town_edges(self.LINE, self.built(400.0, 460.0)), [], "a short run is not a town")
        flicker = self.built(400.0, 900.0)
        flicker[50:53] = False
        self.assertEqual(bgsigns.town_edges(self.LINE, flicker), [(400.0, 1)], "a gap in the land cover is bridged")
        self.assertEqual(bgsigns.town_edges(self.LINE, self.built(60.0, 900.0)), [], "too little road outside it")
        speckle = self.built(400.0, 900.0)
        speckle[20:26] = [True, False, True, False, True, False]
        speckle[60:66] = [False, True, False, True, False, True]
        self.assertEqual(bgsigns.town_edges(self.LINE, speckle), [(400.0, 1)], "speckled land cover: one edge")

    def test_d11_and_d12(self):
        got = bgsigns.town_signs(self.LINE, 7.0, "primary", False, 1, self.built(400.0, 900.0),
                                 lambda q: "Стара Загора" if q[0] > 400 else "")
        self.assertEqual(sorted(g["kind"] for g in got), ["d11", "d12"])
        d11 = next(g for g in got if g["kind"] == "d11")
        np.testing.assert_allclose(d11["travel"], [1.0, 0.0])
        np.testing.assert_allclose(d11["point"], [400.0 - bgsigns.TOWN_BEFORE_M, -3.5 - bgsigns.TOWN_SIDE_M],
                                   err_msg="before the built-up area, on the right")
        d12 = next(g for g in got if g["kind"] == "d12")
        self.assertGreater(d12["point"][1], 0.0, "the traffic coming out: its right")
        self.assertEqual(d11["latin"], "Stara Zagora", "a republican road: in Latin too")
        local = bgsigns.town_signs(self.LINE, 5.0, "unclassified", False, 1, self.built(400.0, 900.0),
                                   lambda q: "Ряхово")
        self.assertEqual({g["latin"] for g in local}, {""}, "a municipal road: Cyrillic only")
        self.assertEqual(bgsigns.town_signs(self.LINE, 7.0, "primary", False, 1, self.built(400.0, 900.0),
                                            lambda q: ""), [], "no place known: no sign")
        oneway = bgsigns.town_signs(self.LINE, 7.0, "primary", True, 1, self.built(400.0, 900.0), lambda q: "Х")
        self.assertEqual([g["kind"] for g in oneway], ["d11"])


class MeshTest(unittest.TestCase):
    def test_signs_are_drawn(self):
        m = bgsigns.Mesh((0.0, 0.0, 0.0))
        m.town({"kind": "d12", "point": np.zeros(2), "travel": np.array([0.0, 1.0]), "name": "Стара Загора",
                "latin": "Stara Zagora"}, 0.0, 0.0)
        p, t, uv, mats, mi = m.result()
        self.assertEqual(mats, bgsigns.MATERIALS)
        self.assertEqual(set(mi.tolist()), {bgsigns.WHITE, bgsigns.RED, bgsigns.METAL, bgsigns.LETTERS, bgsigns.BLACK})
        face = [k for k in range(len(t)) if mi[k] == bgsigns.WHITE]
        self.assertGreater(p[t[face].ravel(), 2].min(), bgsigns.TOWN_BOTTOM_M - 0.01, "its lower edge 1.5 m up")
        m = bgsigns.Mesh((0.0, 0.0, 0.0))
        m.c7(np.zeros(2), [(np.array([-0.7071, -0.7071]), -1), (np.array([0.7071, -0.7071]), 1)],
             np.array([0.0, 1.0]), 0.0, 0.0)
        p, t, uv, mats, mi = m.result()
        for k in np.flatnonzero(mi == bgsigns.RED):
            a, b, c = p[t[k]]
            n = np.cross(b - a, c - a)
            self.assertGreater(abs(n[0]) + abs(n[1]), 0.0)
        red = p[t[mi == bgsigns.RED].ravel()]
        self.assertTrue(1.2 - 1e-6 <= red[:, 2].min() and red[:, 2].max() <= 1.8 + 1e-6, "1.2 m up, 0.6 m board")

    def test_merge(self):
        a = (np.zeros((3, 3)), np.array([[0, 1, 2]]), np.zeros((3, 2)), ["x", "y"], np.array([1]))
        b = (np.ones((3, 3)), np.array([[0, 1, 2]]), np.zeros((3, 2)), ["y", "z"], np.array([1]))
        p, t, uv, mats, mi = bgsigns.merge(a, b)
        self.assertEqual(mats, ["x", "y", "z"])
        self.assertEqual(t.tolist(), [[0, 1, 2], [3, 4, 5]])
        self.assertEqual(mi.tolist(), [1, 2])
        self.assertIs(bgsigns.merge(None, b), b)

    def test_materials_are_in_the_mod(self):
        path = ROOT / "mod" / "beamng_earth" / "levels" / "beamng_earth" / "art" / "shapes" / "beamngEarth" / \
            "roadside" / "exit.materials.json"
        with open(path, encoding="utf-8") as f:
            mats = json.load(f)
        self.assertLessEqual(set(bgsigns.MATERIALS), set(mats))
        atlas = mats["bne_bg_letters_black"]["Stages"][0]["opacityMap"]
        self.assertTrue(atlas.endswith(".dds"), "the game loads only DDS (tools/mod_textures_dds.py)")
        # (made at install from its lossless source: modtextures' manifest and mod/texture_sources)
        import sys
        sys.path.insert(0, str(ROOT))
        import modtextures
        rel = atlas.lstrip("/")
        self.assertIn(rel, modtextures.manifest(), atlas)
        self.assertTrue((ROOT / "mod" / "texture_sources" / rel).with_suffix(".webp").exists(), atlas)


class TileTest(unittest.TestCase):
    def test_rows_of_a_rural_bend_and_a_town(self):
        class Frame:
            enu_to_geodetic = staticmethod(lambda x, y, z: (np.asarray(y) * 1e-5, np.asarray(x) * 1e-5, z))
        sp = roads.Spec(7.0, "paved", False, "primary", False)
        line = np.array([[0.0, 0.0], [1000.0, 0.0]])
        urban = lambda la, lo: np.asarray(lo) * 1e5 >= 400.0     # noqa: E731
        posts, towns = bgsigns.rows([("a", sp, bend(100.0, 90.0)), ("b", sp, line)], Frame, urban,
                                    lambda la, lo: "Ихтиман")
        self.assertTrue(posts)
        self.assertEqual(sorted(t["kind"] for t in towns), ["d11", "d12"])
        self.assertEqual(towns[0]["latin"], "Ihtiman")
        town_road = roads.Spec(7.0, "paved", True, "primary", False)
        posts, _ = bgsigns.rows([("a", town_road, bend(100.0, 90.0))], Frame, None, None)
        self.assertEqual(posts, [], "no С7 in a town")


if __name__ == "__main__":
    unittest.main()
