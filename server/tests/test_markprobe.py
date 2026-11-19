"""markprobe.py's metric functions (pure: plain arrays in, numbers out) on small cases."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import markprobe  # noqa: E402


class LookupTest(unittest.TestCase):
    def test_points_in_triangles_and_their_plane(self):
        tri = np.array([[[0, 0, 0], [4, 0, 4], [0, 4, 0]], [[4, 0, 1], [4, 4, 1], [0, 4, 1]]], np.float64)
        pts = np.array([[1, 1], [3.5, 3.5], [9, 9]], np.float64)
        pi, ti, z = markprobe.lookup(tri, pts)
        self.assertEqual(pi.tolist(), [0, 1])
        self.assertEqual(ti.tolist(), [0, 1])
        np.testing.assert_allclose(z, [1.0, 1.0])


class TileMetricsTest(unittest.TestCase):
    def test_dz_and_coverage_over_a_flat_ground(self):
        box = (0.0, 0.0, 8.0, 8.0)
        ground = lambda xy: np.zeros(len(xy))      # noqa: E731
        spec = type("S", (), {"kind": "paved", "width": 4.0})()
        roads = [("r", spec, np.array([[0, 4, 0], [8, 4, 0]], np.float64), 0.0)]
        # a marking 3 mm up over the road's left half, an edge blend 1 mm under the ground over its right half
        P = np.array([[0, 4, .003], [8, 4, .003], [8, 6, .003], [0, 6, .003],
                      [0, 2, -.001], [8, 2, -.001], [8, 4, -.001], [0, 4, -.001]], np.float64)
        T = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]])
        r = markprobe.tile_metrics(P, T, ["line_white", "ECA_road_edge_dirt"], np.array([0, 0, 1, 1]),
                                   {"line_white": "mark", "ECA_road_edge_dirt": "edge"}, box, ground, roads)
        s = {L: markprobe.summarize(v) for L, v in r["dz"].items()}
        self.assertAlmostEqual(s["mark"]["min_mm"], 3.0, places=3)
        self.assertEqual(s["mark"]["neg"], 0)
        self.assertAlmostEqual(s["edge"]["min_mm"], -1.0, places=3)
        self.assertEqual(s["edge"]["neg"], s["edge"]["n"])
        self.assertGreater(r["cov"]["asphalt"], 0)
        self.assertGreater(r["cov"]["any"] / r["cov"]["asphalt"], 0.9)     # (the mask's rasterized edge rows aside)
        # a deck's chunk over the marking: left out of dz
        r2 = markprobe.tile_metrics(P, T, ["line_white", "ECA_road_edge_dirt"], np.array([0, 0, 1, 1]),
                                    {"line_white": "mark", "ECA_road_edge_dirt": "edge"}, box, ground, roads,
                                    decks=[(np.array([[0, 5], [8, 5]], np.float64), 1.5)])
        self.assertEqual(len(r2["dz"]["mark"]), 0)

    def test_a_dip_where_an_edge_crosses_a_grid_line(self):
        """A ridge of the ground exactly on a grid line, between the raster's samples and away from the lattice
        nodes: found where the overlay's edge crosses that line."""
        box = (0.0, 0.0, 8.0, 8.0)
        ground = lambda xy: np.where(np.abs(xy[:, 0] - 4.0) < 1e-9, 0.01, 0.0)     # noqa: E731
        P = np.array([[1, 2.2, .003], [7, 2.3, .003], [7, 2.8, .003]], np.float64)   # (no node inside: y 2.2-2.8)
        r = markprobe.tile_metrics(P, np.array([[0, 1, 2]]), ["line_white"], np.array([0]), {"line_white": "mark"},
                                   box, ground, [])
        self.assertAlmostEqual(markprobe.summarize(r["dz"]["mark"])["min_mm"], -7.0, places=3)


if __name__ == "__main__":
    unittest.main()
