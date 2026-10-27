"""rumble: shoulder rumble strips on rural motorways and trunks, bars on the ground beside both edges, the same from
every tile; their boxes in the roadside mesh face outward."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guidesigns as gs  # noqa: E402
import roads  # noqa: E402
import rumble  # noqa: E402

BIG = (-1e5, -1e5, 1e5, 1e5)


def ground(x, y):
    return 5.0 + 0.01 * np.asarray(x)


def road(cls="motorway", urban=False, width=11.0, L=400.0, fid="r1", a=(0.0, 0.0), b=None, lift=0.0):
    b = b if b is not None else (a[0] + L, a[1])
    line = np.array([(a[0], a[1], float(ground(a[0], a[1])) + lift), (b[0], b[1], float(ground(b[0], b[1])) + lift)])
    return (fid, roads.Spec(width, "paved", urban, cls, True, dir=1, lanes=3), line, 0.0)


class StripTest(unittest.TestCase):
    def test_both_shoulders_spaced_on_the_ground(self):
        g = rumble.strips([road()], BIG, ground)
        c = g["c"]
        ys = sorted({round(float(v), 3) for v in c[:, 1]})
        off = rumble.offset(road()[1])
        self.assertEqual(ys, [round(-off, 3), round(off, 3)])
        xs = np.sort(c[c[:, 1] > 0][:, 0])
        self.assertTrue(np.allclose(np.diff(xs), rumble.PERIOD_M))
        self.assertAlmostEqual(xs[0], rumble.END_CLEAR_M)
        self.assertLessEqual(xs[-1], 400.0 - rumble.END_CLEAR_M)
        self.assertTrue(np.allclose(c[:, 2], ground(c[:, 0], c[:, 1])))

    def test_not_in_towns_nor_on_small_roads(self):
        self.assertIsNone(rumble.strips([road(urban=True)], BIG, ground))
        self.assertIsNone(rumble.strips([road("primary")], BIG, ground))
        self.assertIsNone(rumble.strips([road(L=40.0)], BIG, ground))

    def test_same_bars_from_neighbouring_tiles(self):
        whole = rumble.strips([road()], BIG, ground)["c"]
        parts = [rumble.strips([road()], (x0, -50, x0 + 64, 50), ground) for x0 in range(-64, 512, 64)]
        tiled = np.concatenate([p["c"] for p in parts if p is not None])
        key = lambda a: sorted(map(tuple, np.round(a, 4)))
        self.assertEqual(key(whole), key(tiled))

    def test_mesh_boxes_face_outward(self):
        g = rumble.strips([road(L=60.0)], BIG, ground)
        P, T, UV, M = gs.mesh([g], (0.0, 0.0, 0.0))
        self.assertTrue((M == gs.CONCRETE).all())
        self.assertEqual(len(T), 6 * len(g["c"]))
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        cen = P[T].mean(axis=1)
        bar = np.repeat(g["c"], 6, axis=0) + np.array([0.0, 0.0, rumble.BAR_H_M / 2])
        self.assertTrue(((n * (cen - bar)).sum(axis=1) > 0).all(), "every face outward")
        self.assertAlmostEqual(float(P[:, 2].max() - P[:, 2].min()), rumble.BAR_H_M + 0.01 * 60.0, delta=0.6)

    def test_signs_kept_before_bars_at_the_vertex_limit(self):
        g = rumble.strips([road(L=4000.0)], BIG, ground)
        sign = {"kind": "route", "x": 10.0, "y": 0.0, "z": 0.0, "d": [1.0, 0.0], "width": 11.0, "lanes": 2,
                "left_hand": False, "overhead": False, "rows": [[("text", "EAST", gs.SMALL_M)]], "id": "a"}
        P, T, UV, M = gs.mesh([g, sign], (0.0, 0.0, 0.0))
        self.assertIn(gs.ATLAS, set(M.tolist()), "the sign is there")
        self.assertLessEqual(len(P), gs.MAX_VERTS)


if __name__ == "__main__":
    unittest.main()


class PlaceTest(unittest.TestCase):
    """The user, 2026-10-26: strips on the outermost edge instead of beside the white line, and across other roads."""

    def test_beside_the_edge_line_on_the_shoulder(self):
        spec = road()[1]
        edge = spec.width / 2 - roads.motorway_shoulder(spec)             # the edge line's centre
        off = rumble.offset(spec)
        self.assertAlmostEqual(off - rumble.BAR_ACROSS_M / 2, edge + roads.EDGE_LINE["w"] / 2 + rumble.EDGE_GAP_M)
        self.assertLess(off + rumble.BAR_ACROSS_M / 2, spec.width / 2, "inside the pavement, not at its outer edge")

    def test_narrow_edge_strip_on_the_line(self):
        spec = road("trunk", width=8.0)[1]
        self.assertAlmostEqual(rumble.offset(spec), spec.width / 2 - roads.EDGE_LINE["inset"])

    def test_none_on_a_crossing_road(self):
        cross = road("primary", width=7.0, fid="x", a=(200.0, -50.0), b=(200.0, 50.0))
        c = rumble.strips([road(), cross], BIG, ground)["c"]
        r = 3.5 + rumble.OTHER_CLEAR_M + rumble.BAR_ACROSS_M / 2
        self.assertFalse((np.abs(c[:, 0] - 200.0) < r - 1e-6).any())
        self.assertTrue((np.abs(c[:, 0] - 200.0) < r + 1.0).any(), "right up to it")

    def test_none_on_a_bridge(self):
        self.assertIsNone(rumble.strips([road(lift=6.0)], BIG, ground))
