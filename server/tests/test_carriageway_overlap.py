"""Adjacent street pieces are not the two carriageways of a divided road."""
import math
import os
from pathlib import Path
import sys
import unittest

import numpy as np

HERE = Path(__file__).resolve().parent
os.environ.setdefault("BNE_REGIONAL_DIR", str(HERE / "fixtures/regional"))
sys.path[:0] = [str(HERE.parent), str(HERE)]
import fast
import navgraph
import roads
from test_roads import FRAME, features, flat, rules


def street():
    # Relative geometry of the Chicago junction: two short pieces connect long
    # stretches of the same two-way street, with a small bend at each join.
    points = [(-192.79, -8.81), (0., 0.), (7.57, .44), (12.47, .89),
              (19.59, .88), (136.69, 3.51)]
    f = features([[a, b] for a, b in zip(points, points[1:])], ["secondary"] * 5)
    f.attrs["name"] = np.array(["Connected Street"] * 5)
    f.rules["connectors"] = rules([(i, f"joint{i}", 0., 0.) for i in range(5)] +
                                  [(i, f"joint{i + 1}", 1., 1.) for i in range(5)])
    return f


class CarriagewayOverlapTest(unittest.TestCase):
    def side(self, a, b):
        return fast._ef.dual_side(np.asarray(a, np.float64), np.asarray(b, np.float64),
                                  4., 45., math.cos(math.radians(25.)), require_overlap=True)

    def test_endpoint_only_neighbours_are_not_beside_each_other(self):
        a = [(0., 0.), (7.57, .44)]
        b = [(12.47, .89), (19.59, .88)]
        for x, y in ((a, b), (a[::-1], b), (a, b[::-1]), (b, a)):
            with self.subTest(a=x, b=y):
                self.assertEqual(self.side(x, y), (0., 0.))

    def test_parallel_carriageways_keep_their_overlap(self):
        for reverse in (False, True):
            b = [(0., 12.), (100., 12.)]
            if reverse:
                b.reverse()
            share, side = self.side([(0., 0.), (100., 0.)], b)
            self.assertEqual((share, side), (1., 1.))
        share, side = self.side([(0., 0.), (20., 0.)], [(10., 8.), (40., 8.)])
        self.assertAlmostEqual(share, 2. / 3.)
        self.assertEqual(side, 1.)

    def test_parallel_bends_keep_the_nearest_interior_vertex(self):
        angle = math.radians(20.)
        a = [(0., 0.), (50., 0.), (50. + 50. * math.cos(angle), 50. * math.sin(angle))]
        b = [(0., 8.), (50. - 8. * math.tan(angle / 2.), 8.),
             (a[-1][0] - 8. * math.sin(angle), a[-1][1] + 8. * math.cos(angle))]
        # A sample at a convex bend can project past both adjoining segments.
        # The interior vertex is still beside it; only the open ends are excluded.
        for reverse_a in (False, True):
            for reverse_b in (False, True):
                with self.subTest(reverse_a=reverse_a, reverse_b=reverse_b):
                    self.assertEqual(self.side(a[::-1] if reverse_a else a, b[::-1] if reverse_b else b),
                                     (1., -1. if reverse_a else 1.))

    def test_short_connected_street_pieces_keep_two_way_access(self):
        for left_hand in (False, True):
            with self.subTest(left_hand=left_hand):
                f = street()
                urban = lambda la, lo: np.ones(np.shape(la), bool)
                physical = roads.road_specs(f, urban, left_hand=left_hand)
                specs = roads.navigation_specs(f, physical, urban, left_hand)
                self.assertTrue(all(sp is not None and not sp.oneway for sp in specs))
                # Reading navigation directions must not change the physical specs or their cache.
                self.assertEqual(roads.road_specs(f, urban, left_hand=left_hand), physical)
                self.assertTrue(any(sp.oneway for sp in physical))
                for nav, drawn in zip(specs, physical):
                    self.assertEqual(nav._replace(oneway=drawn.oneway, dir=drawn.dir), drawn)

    def test_real_divided_streets_keep_their_inferred_directions(self):
        for left_hand in (False, True):
            f = features([[(0., 0.), (100., 0.)], [(100., 12.), (0., 12.)]],
                         ["secondary"] * 2)
            f.attrs["name"] = np.array(["Divided Street"] * 2)
            urban = lambda la, lo: np.ones(np.shape(la), bool)
            physical = roads.road_specs(f, urban, left_hand=left_hand)
            specs = roads.navigation_specs(f, physical, urban, left_hand)
            self.assertTrue(all(sp.oneway for sp in specs))
            self.assertEqual([sp.dir for sp in specs], [-1, -1] if left_hand else [1, 1])

    def test_graph_can_cross_the_loaded_junction_both_ways(self):
        graph = navgraph.build(FRAME, [street()], flat, (0., 0.),
                               urban_fn=lambda la, lo: np.ones(np.shape(la), bool),
                               signals=[], country="US", window=300.)
        links = {name: set() for name in graph["nodes"]}
        for a, b, one_way, *_ in graph["edges"]:
            links[a].add(b)
            if not one_way:
                links[b].add(a)
        for start, end in (("cjoint0", "cjoint5"), ("cjoint5", "cjoint0")):
            pending, seen = [start], set()
            while pending:
                name = pending.pop()
                if name in seen:
                    continue
                seen.add(name)
                pending.extend(links[name] - seen)
            self.assertIn(end, seen)


if __name__ == "__main__":
    unittest.main()
