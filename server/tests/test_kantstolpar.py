"""kantstolpar: Sweden's roadside posts: where they stand."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kantstolpar  # noqa: E402
import roads  # noqa: E402


class Frame:
    enu_to_geodetic = staticmethod(lambda x, y, z: (y, x, z))
    geodetic_to_enu = staticmethod(lambda la, lo, h: (lo, la, h))


BOX = (-2000.0, -2000.0, 2000.0, 2000.0)
SPEC = roads.Spec(9.0, "paved", False, "primary", False)._replace(kmh=80.0)


def line(n=1000.0):
    return np.c_[np.linspace(0.0, n, 50), np.zeros(50), np.zeros(50)]


def rows(near, country="SE", mapped=()):
    got = kantstolpar.tile_rows(Frame, near, BOX, lambda la, lo: la * 0 + 5.0, country, mapped)
    return np.array(got).reshape(-1, 17)


class PlaceTest(unittest.TestCase):
    def test_both_sides_every_100_m_a_metre_off_the_edge(self):
        r = rows([("a", SPEC, line(), 12.0)])
        self.assertEqual(len(r), 20)
        self.assertEqual(sorted(set(r[:, 2])), [-5.5, 5.5])
        self.assertEqual(sorted(set(np.round(np.diff(np.sort(r[r[:, 2] > 0, 1])), 3))), [100.0])
        self.assertEqual(set(r[:, 3]), {5.0}, "on the ground")

    def test_each_faces_the_traffic_on_its_right(self):
        r = rows([("a", SPEC, line(), 12.0)])
        self.assertTrue((r[r[:, 2] < 0, 8] > 0.99).all(), "right of the way: traffic along it")
        self.assertTrue((r[r[:, 2] > 0, 8] < -0.99).all(), "left of the way: traffic against it")

    def test_only_sweden_and_only_fast_rural_main_roads(self):
        self.assertEqual(len(rows([("a", SPEC, line(), 12.0)], "NO")), 0)
        for sp in (SPEC._replace(kmh=70.0), SPEC._replace(kmh=None), SPEC._replace(urban=True),
                   SPEC._replace(cls="tertiary"), SPEC._replace(kind="gravel")):
            self.assertEqual(len(rows([("a", sp, line(), 12.0)])), 0, str(sp))

    def test_closer_in_a_tight_bend(self):
        a = np.linspace(0.0, 1.0, 60)                   # a 300 m radius
        bend = np.c_[300 * np.sin(a), 300 * (1 - np.cos(a)), a * 0]
        wide = np.c_[3000 * np.sin(a / 10), 3000 * (1 - np.cos(a / 10)), a * 0]     # the same length at 3000 m
        self.assertGreater(len(rows([("a", SPEC, bend, 12.0)])), 2.5 * len(rows([("a", SPEC, wide, 12.0)])))

    def test_none_at_a_junction(self):
        side = roads.Spec(6.0, "paved", False, "tertiary", False)
        cross = np.c_[np.full(2, 500.0), [-200.0, 200.0], [0.0, 0.0]]
        got = rows([("a", SPEC, line(), 12.0), ("b", side, cross, 12.0)])
        self.assertEqual(len(got), 18)
        self.assertGreater(np.abs(got[:, 1] - 500.0).min(), 50.0)

    def test_same_posts_from_any_tile(self):
        whole = rows([("a", SPEC, line(), 12.0)])
        parts = []
        for box in ((-10.0, -50.0, 333.0, 50.0), (333.0, -50.0, 1100.0, 50.0)):
            parts += kantstolpar.tile_rows(Frame, [("a", SPEC, line(), 12.0)], box, lambda la, lo: la * 0 + 5.0, "SE")
        self.assertEqual(sorted(map(tuple, whole[:, 1:3].tolist())), sorted(tuple(r[1:3]) for r in parts))


class CarriagewayTest(unittest.TestCase):
    def test_no_post_on_the_other_carriageway(self):
        sp = roads.Spec(4.5, "paved", False, "trunk", True, 2.5, 1, 100.0)
        a = np.array([[0.0, -500.0, 0.0], [0.0, 500.0, 0.0]])
        b = np.array([[6.0, 500.0, 0.0], [6.0, -500.0, 0.0]])
        rows = kantstolpar.tile_rows(Frame(), [("a", sp, a), ("b", sp._replace(dir=1), b)], (-128, -128, 128, 128),
                                     lambda la, lo: 0.0 * la, "SE")
        xs = sorted({round(float(r[1]), 2) for r in rows})
        self.assertEqual(xs, [-3.25, 9.25], "outside the two carriageways alone")


# fork: Australia's guide posts
AU = roads.Spec(9.0, "paved", False, "primary", False)
RED, WHITE = kantstolpar.AU_RED, kantstolpar.AU_WHITE


def au_rows(near, mapped=()):
    return rows(near, "AU", mapped)


class AustraliaTest(unittest.TestCase):
    def test_both_sides_150_m_apart_one_metre_off_the_edge(self):
        r = au_rows([("a", AU, line(), 12.0)])
        self.assertEqual(sorted(set(r[:, 2])), [-5.5, 5.5])
        self.assertEqual(len(r), 14)
        self.assertEqual(sorted(set(np.round(np.diff(np.sort(r[r[:, 2] > 0, 1])), 3))), [150.0])
        self.assertEqual(set(r[:, 3]), {5.0}, "on the ground")
        self.assertEqual(set(r[:, 6]), {kantstolpar.HEIGHT_M})

    def test_red_on_the_left_white_on_the_right_the_reflector_faces_the_traffic(self):
        r = au_rows([("a", AU, line(), 12.0)])
        left, right = r[r[:, 2] > 0], r[r[:, 2] < 0]        # (left of a way along +X: +Y)
        self.assertTrue((left[:, 0] == RED).all() and (right[:, 0] == WHITE).all())
        # the model's front (-X) face shows the traffic along the way; its back face the colour of the other lane's
        self.assertTrue((r[:, 8] > 0.99).all(), "facing the traffic along +X")

    def test_a_one_way_road_runs_against_its_geometry(self):
        r = au_rows([("a", AU._replace(oneway=True, dir=-1), line(), 12.0)])
        self.assertTrue((r[:, 8] < -0.99).all(), "its traffic comes along -X")
        self.assertTrue((r[r[:, 2] > 0, 0] == WHITE).all(), "+Y is the driver's right going -X")
        self.assertTrue((r[r[:, 2] < 0, 0] == RED).all())

    def test_closer_in_a_bend(self):
        a = np.linspace(0.0, 1.0, 60)                   # a 300 m radius, 3000 m (the same length) and 40 m
        bend = np.c_[300 * np.sin(a), 300 * (1 - np.cos(a)), a * 0]
        wide = np.c_[3000 * np.sin(a / 10), 3000 * (1 - np.cos(a / 10)), a * 0]
        tight = np.c_[40 * np.sin(a * 7.5), 40 * (1 - np.cos(a * 7.5)), a * 0]
        counts = [len(au_rows([("a", AU, g, 12.0)])) for g in (wide, bend, tight)]
        self.assertGreater(counts[1], 2.5 * counts[0])
        self.assertGreater(counts[2], 3 * counts[1])
        gaps = np.diff(np.sort(au_rows([("a", AU, bend, 12.0)])[:, 1]))
        self.assertLess(np.median(gaps), 60.0, "tens of metres on a bend, not 150")

    def test_only_rural_sealed_roads_in_australia(self):
        self.assertEqual(len(au_rows([("a", AU._replace(urban=True), line(), 12.0)])), 0, "an urban kerbed street")
        for sp in (AU._replace(kmh=60.0), AU._replace(kind="gravel"), AU._replace(cls="residential"),
                   AU._replace(cls="service"), AU._replace(sides=(1, -1))):
            self.assertEqual(len(au_rows([("a", sp, line(), 12.0)])), 0, str(sp))
        for sp in (AU._replace(cls="tertiary"), AU._replace(kmh=100.0), AU._replace(cls="unclassified")):
            self.assertGreater(len(au_rows([("a", sp, line(), 12.0)])), 0, str(sp))
        self.assertEqual(len(rows([("a", AU, line(), 12.0)], "NZ")), 0)

    def test_none_at_a_junction(self):
        side = roads.Spec(6.0, "paved", False, "residential", False)
        cross = np.c_[np.full(2, 450.0), [-200.0, 200.0], [0.0, 0.0]]
        got = au_rows([("a", AU, line(), 12.0), ("b", side, cross, 12.0)])
        self.assertEqual(len(got), 12)
        self.assertGreater(np.abs(got[:, 1] - 450.0).min(), 50.0)

    def test_same_posts_from_any_tile(self):
        whole = au_rows([("a", AU, line(), 12.0)])
        parts = []
        for box in ((-10.0, -50.0, 333.0, 50.0), (333.0, -50.0, 1100.0, 50.0)):
            parts += kantstolpar.tile_rows(Frame, [("a", AU, line(), 12.0)], box, lambda la, lo: la * 0 + 5.0, "AU")
        self.assertEqual(sorted(map(tuple, whole[:, 1:3].tolist())), sorted(tuple(r[1:3]) for r in parts))

    def test_the_groups_agree_with_the_mod_and_the_old_ones_did_not_move(self):
        import guardrails
        self.assertEqual(guardrails.GROUPS[RED], "au_guidepost_red")
        self.assertEqual(guardrails.GROUPS[WHITE], "au_guidepost_white")
        self.assertEqual(guardrails.GROUPS[kantstolpar.GROUP], "kantstolpe")
        self.assertEqual(guardrails.GROUPS.index("se_a19"), 22)


class SwedenUnchangedTest(unittest.TestCase):
    """fork: the Australian rules change nothing for Sweden: a sample (a 600 m straight into a 500 m bend, a two-way road
    and a one-way one beside it) as the module made it before they were added: 72 rows, their x + y summing to 43905.192."""

    def test_a_swedish_sample_road(self):
        a = np.linspace(0.0, 0.6, 40)
        pts = np.r_[np.c_[np.linspace(0.0, 600.0, 30), np.zeros(30), np.zeros(30)],
                    (np.c_[500 * np.sin(a), 500 * (1 - np.cos(a)), a * 0] + [600.0, 0.0, 0.0])[1:]]
        got = rows([("a", SPEC, pts, 12.0), ("b", SPEC._replace(oneway=True, dir=-1), pts + [0.0, 40.0, 0.0], 12.0)])
        self.assertEqual(len(got), 72)
        self.assertEqual(round(float(got[:, 1:3].sum()), 3), 43905.192)
        self.assertEqual(set(got[:, 0]), {float(kantstolpar.GROUP)})
        self.assertEqual(len(rows([("a", SPEC, pts, 12.0)], "AT")), 0)


if __name__ == "__main__":
    unittest.main()
