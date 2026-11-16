"""server/signcodes.py: OSM traffic_sign values to roles."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roadsigns  # noqa: E402
import signcodes  # noqa: E402


class Parse(unittest.TestCase):
    def test_values_from_the_wild(self):
        # (value, country of the place, roles) from Overpass round Berlin, Krakow and Prague, 2026-11-07
        for value, country, want in [
            ("DE:206", "DE", ["prio.stop"]),
            ("DE:267", None, ["reg.no_entry"]),
            ("DE:250;DE:1020-30", "DE", ["reg.no_vehicles"]),
            ("DE:220-10", None, ["info.oneway"]),
            ("DE:350-10,1000-32", None, ["info.crossing_right"]),
            ("PL:B-36", None, ["reg.no_stopping"]),
            ("PL:D-6,T-27", None, ["info.crossing_right"]),
            ("PL:B-35;PL:B-36", None, ["reg.no_parking", "reg.no_stopping"]),
            ("CZ:P6", None, ["prio.stop"]),
            ("CZ:B2", None, ["reg.no_entry"]),
            ("stop", None, ["prio.stop"]),
            ("give_way", None, ["prio.give_way"]),
            ("yield", None, ["prio.give_way"]),
        ]:
            self.assertEqual(signcodes.roles(value, country), want, value)

    def test_plates_belong_to_the_sign_before_them(self):
        got = signcodes.parse("DE:325.1,1020-30,1022-10")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["plates"], ["1020-30", "1022-10"])
        got = signcodes.parse("PL:D-6,T-27")
        self.assertEqual(got[0]["plates"], ["T-27"])

    def test_a_value_is_read_with_its_number(self):
        got = signcodes.parse("DE:274.1[30]")
        self.assertEqual(got, [])           # (a speed zone: the speed module's, no role here)
        got = signcodes.parse("PL:B-25[x]")
        self.assertEqual(got[0]["value"], "x")

    def test_doubt_is_no_sign(self):
        for value in ["", None, "none", "yes", "city_limit", "maxspeed", "DE:9999", "PL:Z-1", "PL:D-6b", "UA:1.1", "garbage",
                      "DE:", ":", "[]", ";;,,", "NL:G11"]:
            self.assertEqual(signcodes.roles(value), [], value)

    def test_a_prefix_of_another_country_is_not_ours(self):
        self.assertEqual(signcodes.roles("DE:B-36"), [])
        self.assertEqual(signcodes.roles("PL:267"), [])
        self.assertEqual(signcodes.roles("267", None), [])

    def test_an_unprefixed_code_takes_the_places_own_numbering(self):
        self.assertEqual(signcodes.roles("267", "DE"), ["reg.no_entry"])
        self.assertEqual(signcodes.roles("B-36", "PL"), ["reg.no_stopping"])

    def test_the_prefix_of_the_first_sign_serves_the_group(self):
        self.assertEqual(signcodes.roles("DE:267,1020-30"), ["reg.no_entry"])


class Table(unittest.TestCase):
    def test_every_role_exists_in_the_catalogue(self):
        roles = {r["id"] for r in roadsigns.tables()[0]["roles"]}
        t = signcodes.table()
        for role in t["generic"].values():
            self.assertIn(role, roles, role)
        for country, row in t["countries"].items():
            for code, role in row["codes"].items():
                self.assertIn(role, roles, f"{country}:{code}")

    def test_verified_codes_are_in_the_table(self):
        for country, row in signcodes.table()["countries"].items():
            self.assertLessEqual(set(row["verified"]), set(row["codes"]), country)

    def test_the_file_is_plain_json(self):
        with open(signcodes.DATA, encoding="utf-8") as f:
            json.load(f)


if __name__ == "__main__":
    unittest.main()
