"""Country-specific markings on the ground, bridges and tunnel decks."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roads
from test_roads import FRAME, flat


LINE = np.array([[0., 0., 10.], [48., 0., 10.]])
BOX = (-1., -20., 49., 20.)
TWO_WAY = roads.Spec(8.5, "paved", False, "trunk", False, lanes=2)


def centre_runs(chunks):
    return [(c["n"][0][0], c["n"][-1][0]) for c in chunks if c["m"] == "line_yellow"]


class NorwegianFixedPaint(unittest.TestCase):
    def test_build_tile_passes_country_to_covered_and_open_tunnel_parts(self):
        opened = LINE + [0., 10., 0.]
        tunnel = SimpleNamespace(
            parts=lambda cells, box: [("road#1", TWO_WAY, LINE[:, :2], LINE[:, 2], 8.)],
            open_parts=lambda cells, box: [("road#0", TWO_WAY, opened[:, :2], opened[:, 2], 8.)],
        )
        chunks = roads.build_tile(FRAME, 0, 0, [], flat, country="NO", tunnels=tunnel)
        fixed = [c for c in chunks if c.get("fixed") and c["m"] == "line_yellow"]
        open_ = [c for c in chunks if not c.get("fixed") and c["m"] == "line_yellow"]
        self.assertEqual(centre_runs(fixed), [(0., 9.), (12., 21.), (24., 33.), (36., 45.)])
        self.assertEqual(centre_runs(open_), [(0., 3.), (12., 15.), (24., 27.), (36., 39.)])
        self.assertTrue(all(n[2] == 10. for c in fixed for n in c["n"]))

    def test_warning_dash_spans_a_tile_edge(self):
        tunnel = SimpleNamespace(parts=lambda cells, box: [("road#1", TWO_WAY, LINE[:, :2], LINE[:, 2], 8.)])
        left = roads._tunnel_chunks(tunnel, [], (-1., -20., 5., 20.), country="NO")
        right = roads._tunnel_chunks(tunnel, [], (5., -20., 49., 20.), country="NO")
        self.assertEqual(centre_runs(left), [(0., 5.)])
        self.assertEqual(centre_runs(right), [(5., 9.), (12., 21.), (24., 33.), (36., 45.)])

    def test_bridge_keeps_the_open_road_pattern(self):
        profiles = SimpleNamespace(
            parts=lambda cells, box: [("road", TWO_WAY, LINE[:, :2], LINE[:, 2], 8.)],
            _ground=lambda points: np.zeros(len(points)),
        )
        bridge = roads._bridge_chunks(FRAME, {}, flat, BOX, bridges=profiles, cells=[], country="NO")
        self.assertEqual(centre_runs(bridge), [(0., 3.), (12., 15.), (24., 27.), (36., 39.)])
        self.assertTrue(all(c.get("fixed") for c in bridge))


if __name__ == "__main__":
    unittest.main()
