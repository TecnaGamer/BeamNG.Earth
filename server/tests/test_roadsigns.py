"""server/roadsigns.py: the tables, the crossings it believes, the switch. The rules themselves: cargo test (roadsigns.rs)."""
import json
import os
import sys
import types
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


class Rules(unittest.TestCase):
    def test_the_rule_numbers_are_the_kernels(self):
        _, systems, by_id = roadsigns.tables()
        self.assertEqual(len(systems["rules"]), 27)
        self.assertEqual(len(set(systems["rules"])), 27)
        names = ["reg.no_entry", "info.dead_end", "info.crossing_right", "warn.curve_right", "warn.curve_left",
                 "warn.double_right", "warn.double_left", "warn.roundabout", "mand.roundabout", "warn.signals",
                 "info.motorway", "info.motorway_end", "info.living_street", "info.living_street_end", "info.oneway",
                 "warn.narrow_both", "warn.chevron_right", "warn.chevron_left", "warn.side_right", "warn.side_left",
                 "prio.give_way", "prio.stop", "info.tunnel", "info.pedestrian_zone", "mand.bicycle", "info.bus_stop",
                 "info.parking"]
        self.assertEqual(systems["rules"], names)

    def test_every_threshold_is_given_and_a_country_may_override(self):
        _, systems, _ = roadsigns.tables()
        self.assertEqual(set(systems["params"]), set(roadsigns.PARAM_KEYS))
        base = roadsigns.params({})
        self.assertEqual(len(base), 11)
        over = roadsigns.params({"params": {"warn_m": 200}})
        self.assertEqual(over[0], 200.0)
        self.assertEqual(over[1:], base[1:])

    def test_ukraine_has_no_living_street_signs(self):
        _, systems, by_id = roadsigns.tables()
        for code, row in systems["countries"].items():
            for role in row.get("off", ()):
                self.assertIn(role, by_id, code)
        self.assertEqual(roadsigns.system("UA")["off"], ["info.living_street", "info.living_street_end"])
        self.assertNotIn("off", roadsigns.system("CZ"))

    def row(self, pts, direction=0.0):
        pts = np.asarray(pts, np.float64)
        return (pts, pts, (4.0, direction, 3.0, 0.0, 0.0), [], [], [])

    def test_a_round_loop_is_a_ring_and_its_approach_is_not(self):
        t = np.radians(np.arange(0, 361, 10.0))
        circle = np.column_stack([30.0 * np.cos(t), 30.0 * np.sin(t)])
        quarter = lambda a, b: circle[(a // 10):(b // 10) + 1]       # noqa: E731
        rows = {"a": self.row(quarter(0, 90), 1.0), "b": self.row(quarter(90, 180), 1.0),
                "c": self.row(quarter(180, 270), 1.0), "d": self.row(quarter(270, 360), 1.0),
                "in": self.row([[-330.0, 0.0], [-30.0, 0.0]])}
        import islands
        ring = islands.ring_directions(
            [(fid, types.SimpleNamespace(oneway=bool(r[2][1]), dir=int(r[2][1])), r[1], 0.0) for fid, r in rows.items()], False)
        got = {id(r[0]): (m, f) for r, m, f in roadsigns.with_flags(rows, set(), ring)}
        flags = {fid: got[id(rows[fid][0])][1] for fid in rows}
        self.assertEqual({k for k, v in flags.items() if v & roadsigns.FLAG_RING}, {"a", "b", "c", "d"})
        self.assertEqual(flags["in"], 0)

    def test_a_living_street_ranks_as_a_street(self):
        rows = {"x": (np.zeros((2, 2)), np.array([[0.0, 0.0], [50.0, 0.0]]), (3.0, 0.0, 0.0, 1.0, 0.0), [], [], [])}
        (row, meta, flags), = roadsigns.with_flags(rows, {"x"}, {})
        self.assertEqual(flags, roadsigns.FLAG_LIVING)
        self.assertGreaterEqual(meta[2], 1.0)
        (row, meta, flags), = roadsigns.with_flags(rows, set(), {})
        self.assertEqual((flags, meta[2]), (0, 0.0))


class Poles(unittest.TestCase):
    def test_a_warning_stands_alone_in_germany_but_not_in_czechia(self):
        rules = roadsigns.tables()[1]["rules"]
        de, cz = roadsigns.pole_params(roadsigns.system("DE")), roadsigns.pole_params(roadsigns.system("CZ"))
        self.assertEqual(len(de), len(roadsigns.tables()[1]["rules"]) + 2)
        self.assertEqual(de[rules.index("warn.curve_left")], -1.0)
        self.assertGreaterEqual(cz[rules.index("warn.curve_left")], 0.0)
        self.assertEqual(cz[rules.index("warn.curve_left")], 0.0, "czech warnings on top")
        self.assertEqual(de[len(rules):], [3.0, 12.0])

    def test_the_country_order_ranks_the_groups_from_the_top(self):
        rules = roadsigns.tables()[1]["rules"]
        ua = roadsigns.pole_params(roadsigns.system("UA"))
        self.assertLess(ua[rules.index("warn.curve_left")], ua[rules.index("mand.roundabout")])
        self.assertLess(ua[rules.index("mand.roundabout")], ua[rules.index("reg.no_entry")])
        self.assertLess(ua[rules.index("reg.no_entry")], ua[rules.index("info.oneway")])

    def test_signs_on_a_pole_are_stacked_from_the_lowest_up(self):
        sys_row = roadsigns.system("CZ")
        rules = roadsigns.tables()[1]["rules"]
        rule = lambda name: float(rules.index(name))        # noqa: E731
        # [x, y, travel x, travel y, rule, size, road x, road y, urban, pole, slot]
        signs = np.array([[0, 0, 1, 0, rule("warn.signals"), 1, 0, 0, 0, 0, 0],
                          [0, 0, 1, 0, rule("info.crossing_right"), 1, 0, 0, 0, 0, 1],
                          [50, 0, 1, 0, rule("reg.no_entry"), 1, 50, 0, 0, 1, 0]], float)
        t = roadsigns.table(signs, np.zeros(3), np.zeros(3), sys_row)
        sizes = roadsigns.tables()[1]["sizes"]["default"]
        mount = sys_row["mount"][1]
        self.assertAlmostEqual(t[1, 9], mount)
        self.assertAlmostEqual(t[2, 9], mount)
        self.assertAlmostEqual(t[0, 9], mount + sizes["square"][1] + 0.1)

    def test_a_tile_of_roads_gives_signs_with_pole_and_slot(self):
        import fast
        if not hasattr(fast._ef, "road_sign_place"):
            self.skipTest("kernel without road signs")
        pts = np.array([[0.0, 0.0], [800.0, 0.0]])
        meta = np.array([[4.0, 0.0, 4.0, 0.0, 0.0, 0.0]])
        z2 = np.zeros((0, 2))
        got = fast._ef.road_sign_place(pts, pts, np.array([0, 2], np.int64), meta, np.array([0.0, 1.0]), np.array([0, 1], np.int64),
                                       np.array([0, 2], np.int64), z2, np.zeros(2, np.int64), np.array([[360.0, 0.5]]),
                                       np.array([[500.0, 0.5]]), z2, z2, np.zeros(1, np.int64), [-1e4, -1e4, 1e4, 1e4], False,
                                       [0.5, 1.2], 0.9, roadsigns.params({}), np.zeros((0, 4)),
                                       roadsigns.pole_params(roadsigns.system("CZ")), np.zeros((0, 5)))
        self.assertEqual(got.shape[1], 11)
        on = {int(p) for p in got[:, 9]}
        self.assertTrue(any(((got[:, 9] == p).sum() > 1) for p in on), got)


class Interest(unittest.TestCase):
    def test_give_way_and_stop_take_the_way_the_map_states(self):
        tags = lambda **kw: json.dumps(kw)         # noqa: E731
        self.assertEqual(roadsigns.poi_of("give_way", tags(highway="give_way", direction="forward")), (roadsigns.POI_GIVE_WAY, 1.0))
        self.assertEqual(roadsigns.poi_of("stop", tags(highway="stop", direction="backward")), (roadsigns.POI_STOP, -1.0))
        self.assertEqual(roadsigns.poi_of("stop", tags(highway="stop")), (roadsigns.POI_STOP, 0.0))
        self.assertIsNone(roadsigns.poi_of("bench", tags(amenity="bench")))
        self.assertEqual(roadsigns.poi_of("give_way", None), (roadsigns.POI_GIVE_WAY, 0.0))

    def test_a_bus_stop_is_a_highway_bus_stop_and_a_car_park_is_not_private(self):
        tags = lambda **kw: json.dumps(kw)         # noqa: E731
        self.assertEqual(roadsigns.poi_of("bus_stop", tags(highway="bus_stop"))[0], roadsigns.POI_BUS_STOP)
        self.assertIsNone(roadsigns.poi_of("bus_stop", tags(public_transport="platform")), "a platform alone is not a stop sign")
        self.assertEqual(roadsigns.poi_of("parking_entrance", tags(amenity="parking_entrance"))[0], roadsigns.POI_PARKING)
        self.assertIsNone(roadsigns.poi_of("parking_entrance", tags(amenity="parking_entrance", access="private")))
        self.assertIsNone(roadsigns.poi_of("bus_stop", "not json"))

    def test_a_country_may_have_its_own_sign_for_a_role(self):
        rules = roadsigns.tables()[1]["rules"]
        bus = rules.index("info.bus_stop")
        roles, _, by_id = roadsigns.tables()
        self.assertEqual(roadsigns.system("CZ")["roles"]["info.bus_stop"], "info.bus_stop_cz")
        self.assertIn("info.bus_stop_cz", by_id)
        row = np.array([[0.0, 0.0, 0.0, 1.0, bus, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
        cz = roadsigns.table(row, np.zeros(1), np.zeros(1), roadsigns.system("CZ"))
        ua = roadsigns.table(row, np.zeros(1), np.zeros(1), roadsigns.system("UA"))
        self.assertEqual(int(cz[0, 6]), by_id["info.bus_stop_cz"][0])
        self.assertEqual(int(ua[0, 6]), by_id["info.bus_stop"][0])
        local = roadsigns.pole_params(roadsigns.system("CZ"))
        self.assertEqual(local[bus], -1.0, "a local sign stands alone on its pole")

    def test_a_chevron_board_is_wide_and_stands_alone(self):
        rules = roadsigns.tables()[1]["rules"]
        _, _, by_id = roadsigns.tables()
        self.assertEqual(by_id["warn.chevron_right"][1]["shape"], "rect")
        for cc in ("UA", "CZ", "PL", "DE"):
            self.assertEqual(roadsigns.pole_params(roadsigns.system(cc))[rules.index("warn.chevron_left")], -1.0, cc)
        row = np.array([[0.0, 0.0, 0.0, 1.0, rules.index("warn.chevron_left"), 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
        t = roadsigns.table(row, np.zeros(1), np.zeros(1), roadsigns.system("CZ"))
        self.assertEqual(int(t[0, 7]), roadsigns.SHAPES["rect"])
        self.assertEqual(t[0, 8], roadsigns.tables()[1]["sizes"]["default"]["rect"][1])


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
