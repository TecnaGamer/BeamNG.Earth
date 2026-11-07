"""warnsigns (fork): Australia's curve and turn warning signs: where they stand."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guardrails  # noqa: E402
import roads  # noqa: E402
import warnsigns  # noqa: E402


class Frame:
    enu_to_geodetic = staticmethod(lambda x, y, z: (y, x, z))
    geodetic_to_enu = staticmethod(lambda la, lo, h: (lo, la, h))


BOX = (-5000.0, -5000.0, 5000.0, 5000.0)
SPEC = roads.Spec(9.0, "paved", False, "secondary", False)._replace(kmh=100.0, sides=())


def straight(n, step=10.0):
    return np.c_[np.arange(0.0, n, step), np.zeros(int(np.ceil(n / step))), np.zeros(int(np.ceil(n / step)))]


def bend_road(radius, degrees, lead=400.0, tail=400.0, left=True):
    """A straight, an arc of the radius turning the given degrees (left: counter-clockwise), a straight, heading +X first."""
    ang = np.radians(degrees)
    a = np.linspace(0.0, ang, max(int(radius * ang / 5.0), 4))
    sg = 1.0 if left else -1.0
    arc = np.c_[radius * np.sin(a), sg * radius * (1 - np.cos(a))]
    head = sg * ang
    first = np.c_[np.arange(-lead, 0.0, 5.0), np.zeros(int(lead / 5.0))]
    out = np.array([np.cos(head), np.sin(head)])
    last = arc[-1] + np.outer(np.arange(5.0, tail, 5.0), out)
    xy = np.vstack([first, arc, last])
    return np.c_[xy, np.zeros(len(xy))]


def rows(near, country="AU", mapped=()):
    got = warnsigns.tile_rows(Frame, near, BOX, lambda la, lo: la * 0 + 5.0, country, mapped)
    return np.array(got).reshape(-1, 17)


def names(r):
    return [guardrails.GROUPS[int(g)] for g in r[:, 0]]


class BendTest(unittest.TestCase):
    def test_a_straight_and_a_gentle_bend_have_none(self):
        self.assertEqual(len(rows([("a", SPEC, straight(1500.0), 12.0)])), 0)
        self.assertEqual(len(rows([("a", SPEC, bend_road(400.0, 60.0), 12.0)])), 0, "400 m radius is not sharp")

    def test_a_kink_at_one_node_is_not_a_curve(self):
        xy = np.vstack([straight(600.0), [[600.0 + 20.0, 4.0, 0.0]], [[640.0 + k * 10.0, 4.0 + 2.0 * k, 0.0] for k in range(1, 4)],
                        [[660.0 + k * 10.0, 10.0 + k * 0.0, 0.0] for k in range(1, 40)]])
        self.assertEqual(len(rows([("a", roads.Spec(9.0, "paved", False, "secondary", True)._replace(
            kmh=100.0, dir=1, sides=()), xy, 12.0)])), 0)

    def test_a_sharp_curve_left_has_a_sign_each_way_on_the_left_of_its_traffic(self):
        r = rows([("a", SPEC, bend_road(100.0, 70.0), 12.0)])
        self.assertEqual(len(r), 2)
        by = {n: row for n, row in zip(names(r), r)}
        self.assertEqual(set(by), {"au_warn_curve_l", "au_warn_curve_r"}, "the other way it bends right")
        fwd, back = by["au_warn_curve_l"], by["au_warn_curve_r"]
        # going +X the bend begins at x = 0: 150 m before it at 100 km/h, on the driver's left (+Y)
        self.assertAlmostEqual(fwd[1], -150.0, delta=6.0)
        self.assertAlmostEqual(fwd[2], 4.5 + warnsigns.OUT_M, places=2)
        self.assertEqual(np.round(fwd[8:10], 3).tolist(), [0.0, -1.0], "faces the traffic going +X")
        # coming back along the far straight (heading 70 degrees from its end A) the driver's left is its right side
        ang = np.radians(70.0)
        end, head = np.array([100.0 * np.sin(ang), 100.0 * (1 - np.cos(ang))]), np.array([np.cos(ang), np.sin(ang)])
        d = back[1:3] - end
        self.assertAlmostEqual(head[0] * d[1] - head[1] * d[0], -(4.5 + warnsigns.OUT_M), places=1)
        self.assertGreater(back[11] * head[0] + back[12] * head[1], -1.01)
        self.assertAlmostEqual(back[11] * head[0] + back[12] * head[1], -1.0, places=2)      # (travelling against it)

    def test_a_square_turn_is_a_turn_sign(self):
        r = rows([("a", SPEC, bend_road(40.0, 90.0, left=False), 12.0)])
        self.assertEqual(sorted(names(r)), ["au_warn_turn_l", "au_warn_turn_r"])
        r = rows([("a", SPEC, bend_road(120.0, 60.0), 12.0)])
        self.assertTrue(all(n.startswith("au_warn_curve") for n in names(r)))

    def test_advance_by_speed(self):
        a = rows([("a", SPEC._replace(kmh=100.0), bend_road(100.0, 70.0), 12.0)])
        b = rows([("a", SPEC._replace(kmh=70.0), bend_road(100.0, 70.0), 12.0)])
        xa = a[[n == "au_warn_curve_l" for n in names(a)], 1][0]
        xb = b[[n == "au_warn_curve_l" for n in names(b)], 1][0]
        self.assertAlmostEqual(xb - xa, 50.0, delta=6.0)
        self.assertEqual(warnsigns.advance_m(None), 150.0)

    def test_one_way_roads_only_their_own_way(self):
        spec = SPEC._replace(oneway=True, dir=1)
        self.assertEqual(names(rows([("a", spec, bend_road(100.0, 70.0), 12.0)])), ["au_warn_curve_l"])
        spec = SPEC._replace(oneway=True, dir=-1)
        self.assertEqual(names(rows([("a", spec, bend_road(100.0, 70.0), 12.0)])), ["au_warn_curve_r"])

    def test_only_rural_sealed_numbered_roads_in_australia(self):
        road = [("a", SPEC, bend_road(100.0, 70.0), 12.0)]
        self.assertEqual(len(rows(road, "NZ")), 0)
        self.assertEqual(len(rows(road, "")), 0)
        for sp in (SPEC._replace(urban=True), SPEC._replace(kmh=60.0), SPEC._replace(kind="gravel"),
                   SPEC._replace(sides=(1,)), SPEC._replace(cls="motorway"), SPEC._replace(cls="residential"),
                   SPEC._replace(cls="unclassified", kmh=None)):
            self.assertEqual(len(rows([("a", sp, bend_road(100.0, 70.0), 12.0)])), 0, str(sp))
        self.assertEqual(len(rows([("a", SPEC._replace(cls="unclassified", kmh=80.0), bend_road(100.0, 70.0), 12.0)])), 2)

    def test_none_at_a_junction_or_with_no_room_before_the_road_starts(self):
        side = roads.Spec(6.0, "paved", False, "tertiary", False)
        cross = np.c_[np.full(2, -150.0), [-200.0, 200.0], [0.0, 0.0]]
        got = rows([("a", SPEC, bend_road(100.0, 70.0), 12.0), ("b", side, cross, 12.0)])
        self.assertEqual(names(got), ["au_warn_curve_r"], "the sign by the side road is dropped, the other way's stands")
        self.assertEqual(len(rows([("a", SPEC, bend_road(100.0, 70.0, lead=100.0), 12.0)])), 1, "the forward one lacks room")

    def test_a_winding_road_is_not_littered(self):
        xs = np.arange(0.0, 1500.0, 5.0)
        wind = np.c_[xs, 40.0 * np.sin(xs / 40.0), np.zeros(len(xs))]       # an S every 250 m, radius ~40 m
        n = len(rows([("a", SPEC._replace(oneway=True, dir=1), wind, 12.0)]))
        self.assertLessEqual(n, 1500 // int(warnsigns.MIN_GAP_M) + 1)
        self.assertGreater(n, 0)

    def test_a_tile_keeps_the_signs_in_its_box(self):
        road = [("a", SPEC, bend_road(100.0, 70.0), 12.0)]
        inside = warnsigns.tile_rows(Frame, road, (-300.0, -1000.0, 0.0, 1000.0), lambda la, lo: la * 0 + 5.0, "AU")
        outside = warnsigns.tile_rows(Frame, road, (0.0, -1000.0, 300.0, 1000.0), lambda la, lo: la * 0 + 5.0, "AU")
        every = rows(road)
        self.assertEqual(len(inside), 1)
        self.assertEqual(len(outside), 1)
        self.assertEqual(len(inside) + len(outside) + len([r for r in every if r[1] < -300 or r[1] >= 300]), len(every))

    def test_a_mapped_barrier_beside_it_moves_nothing_but_removes_it(self):
        road = [("a", SPEC, bend_road(100.0, 70.0), 12.0)]
        full = rows(road)
        fwd = full[[n == "au_warn_curve_l" for n in names(full)]][0]
        guard = [[0, fwd[1] - 1.0, fwd[2], 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                 [0, fwd[1] + 1.0, fwd[2], 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]]
        self.assertEqual(len(rows(road, mapped=guard)), len(full) - 1)


class ModTablesTest(unittest.TestCase):
    """The mod's catalog has every group the server names (trees.lua), and the models and materials exist."""

    def test_groups_models_and_materials(self):
        root = Path(__file__).resolve().parents[2] / "mod" / "beamng_earth"
        text = (root / "lua" / "ge" / "extensions" / "beamngEarth" / "trees.lua").read_text(encoding="utf-8")
        shapes = root / "levels" / "beamng_earth" / "art" / "shapes" / "beamngEarth"
        for g in warnsigns.GROUPS:
            self.assertIn(g, guardrails.GROUPS)
            self.assertIn(f"'{g}'", text, g)
            self.assertTrue((shapes / f"bne_{g}.dae").exists(), g)
        self.assertIn("au_warnsigns.materials.json", text)
        self.assertTrue((shapes / "au_warnsigns.materials.json").exists())


if __name__ == "__main__":
    unittest.main()
