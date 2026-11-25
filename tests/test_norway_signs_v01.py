"""Norwegian road-sign pilot — configuration contract (stdlib-only tests).

These checks run offline and don't require BeamNG.drive.
They cannot verify in-game sign positions or the native sign renderer.
"""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "server" / "data"


class NorwegianSignsPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.systems = json.loads((DATA / "sign_systems.json").read_text(encoding="utf-8"))
        cls.roles = json.loads((DATA / "sign_roles.json").read_text(encoding="utf-8"))
        cls.no = cls.systems["countries"]["NO"]

    def test_norway_enabled(self):
        self.assertIn("NO", self.systems["countries"])

    def test_existing_atlas_reused(self):
        self.assertEqual(self.no["atlas"], "vienna_w")
        self.assertIn("vienna_w", self.roles["systems"])

    def test_explicit_allowlist(self):
        self.assertEqual(set(self.no["only"]),
                         {"prio.give_way", "prio.stop", "reg.no_entry"})

    def test_no_guessed_warnings_or_priority_road_signs(self):
        self.assertTrue(all(not k.startswith("warn.") for k in self.no["only"]))
        self.assertNotIn("prio.main_road", self.no["only"])
        self.assertNotIn("prio.main_road_end", self.no["only"])

    def test_roles_and_shape_consistent(self):
        roles = {r["id"]: r for r in self.roles["roles"]}
        shapes = {"prio.give_way": "tri_down", "prio.stop": "octagon",
                  "reg.no_entry": "circle"}
        for name, shape in shapes.items():
            self.assertIn(name, self.systems["rules"])
            self.assertEqual(roles[name]["shape"], shape)

    def test_mount_values_and_limit(self):
        self.assertEqual(len(self.no["mount"]), 2)
        self.assertEqual(len(self.no["lateral"]), 2)
        self.assertLessEqual(self.no["max_on_pole"], 2)
        self.assertTrue(all(0 < x < 3 for x in self.no["mount"]))

    def test_other_countries_still_available(self):
        for code in ("DE", "PL", "CZ", "UA", "NL"):
            self.assertIn(code, self.systems["countries"])


if __name__ == "__main__":
    unittest.main()
