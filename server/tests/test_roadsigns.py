"""server/roadsigns.py: the tables, the crossings it believes, the switch. The rules themselves: cargo test (roadsigns.rs)."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roadsigns  # noqa: E402
import signstream  # noqa: E402

TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools")


class Tables(unittest.TestCase):
    def test_every_role_fits_the_sheet_once(self):
        roles, _, by_id = roadsigns.tables()
        self.assertEqual(len(by_id), len(roles["roles"]))
        self.assertLessEqual(len(by_id), roles["cols"] * roles["rows"])

    def test_every_shape_is_known(self):
        for role in roadsigns.tables()[0]["roles"]:
            self.assertIn(role["shape"], roadsigns.SHAPES)

    def test_every_rule_has_a_role(self):
        _, systems, by_id = roadsigns.tables()
        for rule in systems["rules"]:
            self.assertIn(rule, by_id)

    def test_every_country_has_the_sign_numbers(self):
        roles, systems, _ = roadsigns.tables()
        groups = {r["group"] for r in roles["roles"]}
        for code, row in systems["countries"].items():
            self.assertIn(row["atlas"], roles["systems"], code)
            self.assertEqual(len(row["lateral"]), 2, code)
            self.assertEqual(len(row["mount"]), 2, code)
            self.assertLessEqual(set(row["order"]), groups, code)

    def test_the_first_wave_and_nowhere_else(self):
        for code in ("UA", "cz", "PL", "DE"):
            self.assertIsNotNone(roadsigns.system(code), code)
        self.assertIsNone(roadsigns.system("NL"))
        self.assertIsNone(roadsigns.system(None))

    def test_atlas_drawings_match_the_roles(self):
        sys.path.insert(0, TOOLS)
        import build_sign_atlas
        ids = {r["id"] for r in build_sign_atlas.roles()["roles"]}
        self.assertEqual(ids, set(build_sign_atlas.DRAW))


class Crossings(unittest.TestCase):
    def tags(self, **kw):
        return json.dumps({"highway": "crossing", **kw})

    def test_marked_without_signals(self):
        for kind in ("zebra", "marked", "uncontrolled"):
            self.assertTrue(roadsigns.is_marked_crossing(self.tags(crossing=kind)), kind)
        self.assertTrue(roadsigns.is_marked_crossing(self.tags(crossing_ref="zebra")))

    def test_doubt_is_no_sign(self):
        for tags in (self.tags(crossing="traffic_signals"), self.tags(crossing="unmarked"), self.tags(),
                     self.tags(crossing="marked", **{"crossing:markings": "no"}),
                     self.tags(crossing="marked", foot="no"), json.dumps({"crossing": "marked"}), "", "not json"):
            self.assertFalse(roadsigns.is_marked_crossing(tags), tags)


class Switch(unittest.TestCase):
    def test_off_without_the_flag(self):
        class S:
            rsigns_on = True
        on = roadsigns.ON
        try:
            roadsigns.ON = False
            self.assertFalse(roadsigns.enabled(S()))
            roadsigns.ON = True
            self.assertTrue(roadsigns.enabled(S()))
            S.rsigns_on = False
            self.assertFalse(roadsigns.enabled(S()))
        finally:
            roadsigns.ON = on

    def test_the_tile_has_the_field(self):
        self.assertIn('rsigns', signstream.KINDS)
        self.assertEqual(signstream.KINDS[:5], ('exits', 'mileposts', 'merges', 'speeds', 'dirs'))


class Mesh(unittest.TestCase):
    def test_a_sign_is_mesh_on_two_materials(self):
        import fast
        if not hasattr(fast._ef, "road_sign_mesh"):
            self.skipTest("kernel without road signs")
        row = np.array([[10.0, 20.0, 0.0, 0.1, 1.0, 0.0, 6, 0, 0.75, 2.0]])
        p, t, uv, mi = fast._ef.road_sign_mesh(np.ascontiguousarray(row), 8, 8, [0.0, 0.0, 0.0])
        self.assertTrue(len(t) > 40)
        self.assertEqual(set(mi.tolist()), {0, 1})
        self.assertAlmostEqual(float(p[:, 2].max()), 2.1 + 0.75 / 2 * 2, delta=0.02)
        face = uv[t[mi == 0].ravel()]
        self.assertTrue(((face[:, 0] >= 6 / 8) & (face[:, 0] <= 7 / 8)).all())
        self.assertTrue(((face[:, 1] >= 7 / 8) & (face[:, 1] <= 1.0)).all())


if __name__ == "__main__":
    unittest.main()
