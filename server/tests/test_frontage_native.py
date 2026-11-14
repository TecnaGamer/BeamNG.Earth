"""frontage_geometry.Coverage in Rust (earthfast.FrontageCoverage) against the numpy reference (_PyCoverage): the same
pieces, in the same order, for random strips, convex and concave rings, slivers and touching shapes."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import frontage_geometry as fg  # noqa: E402


def shapes(rng, n):
    out = []
    for _ in range(n):
        kind = rng.integers(4)
        c = rng.uniform(-30, 60, 2)
        if kind == 0:               # a strip, as streetfront's bands
            d = rng.normal(size=2)
            d /= np.linalg.norm(d)
            nrm = np.array([-d[1], d[0]])
            L, W = rng.uniform(.5, 9), rng.uniform(.2, 4)
            out.append(np.array([c, c + d * L, c + d * L + nrm * W, c + nrm * W]))
        elif kind == 1:             # a convex polygon
            a = np.sort(rng.uniform(0, 2 * np.pi, rng.integers(3, 9)))
            out.append(c + np.c_[np.cos(a), np.sin(a)] * rng.uniform(.5, 8))
        elif kind == 2:             # a concave (star) ring, either way round
            a = np.linspace(0, 2 * np.pi, 10, endpoint=False)
            r = np.where(np.arange(10) % 2, 2.0, 6.0) * rng.uniform(.5, 1.5)
            p = c + np.c_[np.cos(a), np.sin(a)] * r[:, None]
            out.append(p if rng.random() < .5 else p[::-1])
        else:                       # a sliver, repeated vertices, and an exact copy of an earlier shape
            if out and rng.random() < .5:
                out.append(out[rng.integers(len(out))].copy())
            else:
                out.append(np.array([c, c, c + [5., 0.], c + [5., 1e-5]]))
    return out


class FrontageNativeTest(unittest.TestCase):
    def check(self, a, b):
        self.assertEqual(len(a), len(b))
        for p, q in zip(a, b):
            np.testing.assert_allclose(np.asarray(p, np.float64), np.asarray(q, np.float64), atol=1e-9)

    def test_matches_the_python_reference(self):
        for seed in range(25):
            rng = np.random.default_rng(seed)
            bounds = (-40., -40., 80., 80.)
            nat, ref = fg.Coverage(bounds), fg._PyCoverage(bounds)
            for i, ring in enumerate(shapes(rng, 60)):
                op = i % 3
                if op == 0:
                    nat.reserve(ring)
                    ref.reserve(ring)
                elif op == 1:
                    self.check(nat.overlaps(ring), list(ref.overlaps(ring)))
                    self.assertEqual(nat.overlaps_any(ring), ref.overlaps_any(ring))
                else:
                    self.check(nat.claim(ring), list(ref.claim(ring)))


if __name__ == "__main__":
    unittest.main()
