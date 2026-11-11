"""aurails: Australia's roadside crash barriers: where they stand, which side they face, where they stop."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aurails  # noqa: E402
import guardrails  # noqa: E402
import roads  # noqa: E402


class Frame:
    enu_to_geodetic = staticmethod(lambda x, y, z: (y, x, z))
    geodetic_to_enu = staticmethod(lambda la, lo, h: (lo, la, h))


BOX = (-3000.0, -3000.0, 3000.0, 3000.0)
SPEC = roads.Spec(9.0, "paved", False, "primary", False)._replace(kmh=100.0, sides=())
EDGE = 4.5 + aurails.OUT_M
FLAT = lambda la, lo: la * 0 + 5.0       # noqa: E731
MODULES = ("guardrail", "cable")


def line(x0, x1, step=10.0):
    xs = np.arange(x0, x1 + 1e-6, step)
    return np.c_[xs, np.zeros(len(xs)), np.zeros(len(xs))]


def fill_left(la, lo):
    """The ground falls away on the road's left (+Y) beyond 6 m: 0.5 m per metre."""
    return 5.0 - 0.5 * np.maximum(la - 6.0, 0.0)


def rows(near, elev=FLAT, country="AU", mapped=(), decks=(), box=BOX):
    return np.array(aurails.tile_rows(Frame, near, box, elev, country, mapped, decks)).reshape(-1, 17)


def names(r):
    return [guardrails.GROUPS[int(g)] for g in r[:, 0]]


def modules(r):
    return r[[n in MODULES for n in names(r)]]


def bend(radius, degrees, left=True):
    ang = np.radians(degrees)
    a = np.linspace(0.0, ang, max(int(radius * ang / 5.0), 4))
    sg = 1.0 if left else -1.0
    arc = np.c_[radius * np.sin(a), sg * radius * (1 - np.cos(a))]
    out = np.array([np.cos(sg * ang), np.sin(sg * ang)])
    xy = np.vstack([np.c_[np.arange(-400.0, 0.0, 5.0), np.zeros(80)], arc, arc[-1] + np.outer(np.arange(5.0, 400.0, 5.0), out)])
    return np.c_[xy, np.zeros(len(xy))]


class EdgeTest(unittest.TestCase):
    def test_a_fill_on_the_left_gets_a_rail_on_that_side_facing_the_road(self):
        r = rows([("a", SPEC, line(-500.0, 500.0), 12.0)], elev=fill_left)
        mods = modules(r)
        self.assertGreater(len(mods), 100)
        self.assertTrue(np.allclose(mods[:, 2], EDGE, atol=0.05))
        self.assertTrue(np.allclose(mods[:, 12], 1.0), "its back to the outside (+Y), its face to the road")
        self.assertEqual(len(rows([("a", SPEC, line(-500.0, 500.0), 12.0)])), 0, "a flat straight road: none")

    def test_a_fill_on_the_right_runs_the_other_way(self):
        r = rows([("a", SPEC, line(-500.0, 500.0), 12.0)], elev=lambda la, lo: fill_left(-la, lo))
        mods = modules(r)
        self.assertTrue(len(mods) and np.allclose(mods[:, 2], -EDGE, atol=0.05))
        self.assertTrue(np.allclose(mods[:, 12], -1.0))

    def test_steel_rails_have_a_terminal_at_each_end_of_a_run(self):
        steel = SPEC._replace(kmh=80.0)          # (80 km/h: never a wire rope road)
        r = rows([("a", steel, line(-500.0, 500.0), 12.0)], elev=lambda la, lo: 5.0 - 0.8 * np.maximum(la - 6.0, 0.0))
        self.assertEqual(sorted(set(names(r))), ["guardrail", "guardrail_end", "guardrail_start"])
        self.assertEqual((names(r).count("guardrail_start"), names(r).count("guardrail_end")), (1, 1))

    def test_the_outside_of_a_tight_bend_only(self):
        mods = modules(rows([("a", SPEC, bend(150.0, 70.0), 12.0)]))
        self.assertGreater(len(mods), 10)
        d = np.hypot(mods[:, 1] - 0.0, mods[:, 2] - 150.0)       # (a left bend about (0, 150))
        self.assertTrue((d > 150.0).all(), "all on the outside of the bend")
        self.assertEqual(len(rows([("a", SPEC, bend(600.0, 40.0), 12.0)])), 0, "a gentle bend on flat ground: none")

    def test_only_fast_rural_roads_outside_towns_in_australia(self):
        road = [("a", SPEC, line(-500.0, 500.0), 12.0)]
        self.assertEqual(len(rows(road, fill_left, "NZ")), 0)
        for sp in (SPEC._replace(urban=True), SPEC._replace(kmh=60.0), SPEC._replace(kind="gravel"),
                   SPEC._replace(cls="residential"), SPEC._replace(link=True)):
            self.assertEqual(len(rows([("a", sp, line(-500.0, 500.0), 12.0)], fill_left)), 0, str(sp))
        for sp in (SPEC._replace(cls="motorway", urban=True), SPEC._replace(cls="secondary", kmh=80.0)):
            self.assertGreater(len(rows([("a", sp, line(-500.0, 500.0), 12.0)], fill_left)), 0, str(sp))

    def test_none_across_a_junction_but_carried_on_past_the_next_way(self):
        side = roads.Spec(6.0, "paved", False, "tertiary", False)
        cross = np.c_[np.zeros(2), [-300.0, 300.0], [0.0, 0.0]]
        r = rows([("a", SPEC, line(-500.0, 500.0), 12.0), ("b", side, cross, 12.0)], fill_left)
        mods = modules(r)
        self.assertFalse(((mods[:, 1] > -7.0) & (mods[:, 1] < 7.0)).any())
        self.assertTrue((mods[:, 1] < -10.0).any() and (mods[:, 1] > 10.0).any())
        # the same road's next way, carrying on from the end of this one: no gap
        r = rows([("a", SPEC, line(-500.0, 0.0), 12.0), ("b", SPEC, line(0.0, 500.0), 12.0)], fill_left)
        self.assertTrue(((modules(r)[:, 1] > -4.0) & (modules(r)[:, 1] < 4.0)).any())

    def test_a_mapped_barrier_stands_for_ours(self):
        road = [("a", SPEC, line(-500.0, 500.0), 12.0)]
        full = rows(road, fill_left)
        guard = [[0, x, EDGE, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
                 for x in np.arange(-100.0, 100.0, 3.0)]
        part = rows(road, fill_left, mapped=guard)
        mods = modules(part)
        self.assertFalse(((mods[:, 1] > -95.0) & (mods[:, 1] < 95.0)).any())
        self.assertLess(len(part), len(full))
        self.assertTrue((mods[:, 1] > 100.0).any() and (mods[:, 1] < -100.0).any())

    def test_a_bridge_approach_has_a_rail_on_both_sides(self):
        deck = [("d", SPEC, np.c_[np.arange(0.0, 101.0, 10.0), np.zeros(11)], np.zeros(11), 12.0)]
        mods = modules(rows([("a", SPEC, line(-500.0, 0.0), 12.0)], decks=deck))
        self.assertTrue((mods[:, 1] > -aurails.APPROACH_M - 3.0).all() and (mods[:, 1] < -aurails.APPROACH_NEAR_M + 3.0).all())
        self.assertEqual({int(np.sign(y)) for y in mods[:, 2]}, {-1, 1})
        # an overpass crossing over the road is no approach
        over = [("o", SPEC, np.c_[np.zeros(11), np.arange(-50.0, 51.0, 10.0)], np.zeros(11), 12.0)]
        self.assertEqual(len(rows([("a", SPEC, line(-500.0, 500.0), 12.0)], decks=over)), 0)

    def test_the_median_side_of_a_divided_road_is_left_to_the_median_barriers(self):
        spec = SPEC._replace(oneway=True, dir=1)
        a = line(-500.0, 500.0)
        b = a[::-1] + [0.0, 20.0, 0.0]
        r = rows([("a", spec, a, 12.0), ("b", spec._replace(dir=-1), b, 12.0)], fill_left)
        # the fall is on the +Y side: of carriageway a (y = 0) that is the median side, of b (y = 20) the outside
        self.assertTrue((modules(r)[:, 2] > 20.0).all(), "only b's outer edge")

    def test_a_tile_keeps_the_rails_in_its_box(self):
        road = [("a", SPEC, line(-500.0, 500.0), 12.0)]
        left = rows(road, fill_left, box=(-3000.0, -3000.0, 0.0, 3000.0))
        right = rows(road, fill_left, box=(0.0, -3000.0, 3000.0, 3000.0))
        self.assertEqual(len(left) + len(right), len(rows(road, fill_left)))


class ModTablesTest(unittest.TestCase):
    def test_it_uses_groups_the_mod_already_has(self):
        text = (Path(__file__).resolve().parents[2] / "mod" / "beamng_earth" / "lua" / "ge" / "extensions"
                / "beamngEarth" / "trees.lua").read_text(encoding="utf-8")
        for g in ("guardrail", "guardrail_start", "guardrail_end", "cable"):
            self.assertIn(g, guardrails.GROUPS)
            self.assertIn(f"M.CATALOG.{g} ", text)


if __name__ == "__main__":
    unittest.main()
