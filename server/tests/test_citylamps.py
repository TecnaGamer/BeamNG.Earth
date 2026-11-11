"""Local street lamp posts (citylamps.py, poleprofiles.lamp_country_selector, tools/build_city_lamps.py)."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import citylamps  # noqa: E402
import furniture  # noqa: E402
import poleprofiles as pp  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")


def lamp(x=1.0, y=2.0, z=3.0, h=9.0):
    return ["streetlight", x, y, z, 90.0, 1.0, h, 0.0]


class LocalizeTest(unittest.TestCase):
    def test_a_lamp_in_a_europe_country_becomes_a_local_post_the_rest_keep_the_stock_one(self):
        rows = [lamp(), ["pole", 5.0, 5.0, 3.0, 0.0, 1.0, 9.2, 0.0], ["bench", 1.0, 1.0, 0.0, 0.0, 1.0, 0.9, 0.0]]
        got = citylamps.localize(rows, lambda x, y, z: "UA")
        self.assertIn(got[0][0], citylamps.GROUPS)
        self.assertEqual(got[0][1:], rows[0][1:], "position, yaw and height kept")
        self.assertEqual(got[1:], rows[1:], "poles and benches untouched")
        self.assertEqual(citylamps.localize(rows, lambda x, y, z: None), rows, "the stock post outside the set")

    def test_the_choice_is_the_same_whatever_the_order_or_the_tile(self):
        rows = [lamp(x=float(i), y=float(i * 3)) for i in range(60)]
        a = citylamps.localize(rows, lambda x, y, z: "PL")
        b = citylamps.localize(list(reversed(rows)), lambda x, y, z: "PL")
        self.assertEqual(a, list(reversed(b)))
        self.assertEqual({r[0] for r in a}, set(citylamps.GROUPS), "both kinds in a country with both")

    def test_germany_is_mostly_steel_and_switzerland_all_steel(self):
        n = 400
        share = lambda c: sum(citylamps.group_for(c, float(i), float(i * 7)) == citylamps.G_CONCRETE for i in range(n)) / n  # noqa: E731
        self.assertLess(share("DE"), 0.25)
        self.assertEqual(share("CH"), 0.0)
        self.assertGreater(share("UA"), 0.4)

    def test_the_lens_and_the_collider_follow_the_new_groups(self):
        for g in citylamps.GROUPS:
            self.assertEqual(furniture.LAMP_HEADS[g], citylamps.HEAD)
            self.assertEqual(furniture.LAMP_H[g], 9.0)
            self.assertIn(g, furniture.COLLISION_BOXES)
        lens = furniture.lamp_lenses([[citylamps.G_STEEL, 0.0, 0.0, 10.0, 0.0, 1.0, 9.0, 0.0]])
        self.assertAlmostEqual(lens[0][1], citylamps.HEAD[0], places=2)
        self.assertAlmostEqual(lens[0][3], 10.0 + citylamps.HEAD[2] - furniture.LENS_DROP_M, places=2)

    def test_the_selector_is_off_without_a_country_profile(self):
        for profile in (None, pp.FR_ALL, "x"):
            self.assertIsNone(pp.lamp_country_selector(profile, None)(0.0, 0.0, 0.0))


class ModTest(unittest.TestCase):
    def test_the_mod_has_the_groups_the_server_makes_and_the_models_exist(self):
        lua = open(os.path.join(ROOT, "mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua"), encoding="utf-8").read()
        shapes = os.path.join(ROOT, "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/city_lamps")
        for g, name in ((citylamps.G_STEEL, "bne_lamp_eu_steel"), (citylamps.G_CONCRETE, "bne_lamp_eu_concrete")):
            self.assertRegex(lua, rf"{g} = \{{ \{{ '[^']*city_lamps/{name}', 9\.0 \}} \}}")
            self.assertTrue(os.path.exists(os.path.join(shapes, name + ".dae")), name)
        self.assertIn("beamngEarth/city_lamps'", lua, "its materials directory is loaded")
        self.assertTrue(os.path.exists(os.path.join(shapes, "main.materials.json")))

    def test_the_model_is_9_m_tall_with_its_luminaire_where_the_lens_is(self):
        for name in ("bne_lamp_eu_steel", "bne_lamp_eu_concrete"):
            path = os.path.join(ROOT, "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/city_lamps", name + ".dae")
            xml = open(path, encoding="utf-8").read()
            m = re.search(r'id="%s96-g-p-a" count="\d+">([^<]+)<' % name, xml)
            v = [float(s) for s in m.group(1).split()]
            xs, zs = v[0::3], v[2::3]
            self.assertLessEqual(max(zs), 9.0)
            self.assertGreater(max(zs), 8.7)
            self.assertAlmostEqual(max(xs), citylamps.HEAD[0] + 0.52 + 0.12, delta=0.05)


if __name__ == "__main__":
    unittest.main()
