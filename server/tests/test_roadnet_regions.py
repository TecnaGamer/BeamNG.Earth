"""Where a road is (earthfast 385, roadnet::regions: the road revamp's R2a): sweden.contains and signstates.states against
the numpy they replaced, value for value: random points over Scandinavia and the US (and the whole world), points
exactly on outline vertices, on the horizontal edges' latitudes and on box edges, NaN / inf points, scalar and
broadcast inputs, float32 input; never a panic.

The numpy versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import signstates  # noqa: E402
import sweden  # noqa: E402
from sweden import OUTLINE  # noqa: E402


# ---- the numpy versions (before earthfast 385), verbatim ----

def ref_contains(lon, lat):
    x, y = np.atleast_1d(np.asarray(lon, np.float64)), np.atleast_1d(np.asarray(lat, np.float64))
    a, b = OUTLINE, np.roll(OUTLINE, -1, axis=0)
    crosses = (a[:, 1, None] > y) != (b[:, 1, None] > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        at = a[:, 0, None] + (y - a[:, 1, None]) * (b[:, 0, None] - a[:, 0, None]) / (b[:, 1, None] - a[:, 1, None])
    return (crosses & (x < at)).sum(0) % 2 == 1


def ref_states(lat, lon):
    lat, lon = np.broadcast_arrays(lat, lon)
    x, y = lon.ravel(), lat.ravel()
    found = np.full(len(x), '', dtype='U2')
    if not len(x):
        return found.reshape(lat.shape)
    polygons = signstates._polygons()
    finite = np.isfinite(x) & np.isfinite(y)
    if not finite.any():
        return found.reshape(lat.shape)
    low = np.array([x[finite].min(), y[finite].min()])
    high = np.array([x[finite].max(), y[finite].max()])
    candidates = np.flatnonzero((polygons.upper >= low).all(1) & (polygons.lower <= high).all(1))
    for index in candidates:
        state, lo, hi, rings = polygons[index]
        use = np.flatnonzero((found == '') & (x >= lo[0]) & (x <= hi[0]) & (y >= lo[1]) & (y <= hi[1]))
        if not len(use):
            continue
        px, py = x[use, None], y[use, None]
        inside = np.zeros(len(use), bool)
        for ring in rings:
            a, b = ring[:-1], ring[1:]
            relevant = (np.maximum(a[:, 1], b[:, 1]) >= py.min()) & (np.minimum(a[:, 1], b[:, 1]) <= py.max())
            a, b = a[relevant], b[relevant]
            dy = b[:, 1]-a[:, 1]
            intercept = a[:, 0]+(py-a[:, 1])*(b[:, 0]-a[:, 0])/np.where(dy == 0., 1., dy)
            inside ^= (((a[:, 1] > py) != (b[:, 1] > py)) & (px < intercept)).sum(1) % 2 != 0
        found[use[inside]] = state
    return found.reshape(lat.shape)


def _points(rng, k, box, verts):
    """k points in box, with outline vertices, their latitudes at other longitudes and NaN / inf among them."""
    lon0, lat0, lon1, lat1 = box
    out = []
    for _ in range(k):
        r = rng.random()
        if r < 0.15 and len(verts):
            v = verts[rng.randrange(len(verts))]
            out.append((float(v[0]), float(v[1])))
        elif r < 0.3 and len(verts):
            v = verts[rng.randrange(len(verts))]
            out.append((rng.uniform(lon0, lon1), float(v[1])))
        elif r < 0.33:
            out.append(rng.choice([(np.nan, 60.0), (15.0, np.nan), (np.inf, 60.0), (-np.inf, -np.inf)]))
        else:
            out.append((rng.uniform(lon0, lon1), rng.uniform(lat0, lat1)))
    return np.array(out, np.float64).reshape(-1, 2)


class RegionsParityTest(unittest.TestCase):
    def test_sweden(self):
        rng = random.Random(385)
        for k in range(300):
            p = _points(rng, rng.randint(0, 60), (9.0, 54.0, 26.0, 70.0), OUTLINE)
            self.assertTrue(np.array_equal(sweden.contains(p[:, 0], p[:, 1]), ref_contains(p[:, 0], p[:, 1])), k)
        for lon, lat in ((15.0, 60.0), (np.float32(15.0), 60.0), (11.25, 59.10), ([15.0, 16.0], 60.0)):
            self.assertTrue(np.array_equal(sweden.contains(lon, lat), ref_contains(lon, lat)), (lon, lat))
        p = _points(rng, 2000, (-180.0, -85.0, 180.0, 85.0), OUTLINE)
        self.assertTrue(np.array_equal(sweden.contains(p[:, 0], p[:, 1]), ref_contains(p[:, 0], p[:, 1])))

    def test_states(self):
        rng = random.Random(3851)
        polys = signstates._polygons()
        verts = np.concatenate([np.concatenate(row[3]) for row in polys])
        for k in range(200):
            p = _points(rng, rng.randint(0, 80), (-180.0, 15.0, -60.0, 72.0), verts)
            got, want = signstates.states(p[:, 1], p[:, 0]), ref_states(p[:, 1], p[:, 0])
            self.assertEqual(got.dtype, want.dtype, k)
            self.assertTrue(np.array_equal(got, want), k)
            found = (want != '').sum()
        p = _points(rng, 5000, (-125.0, 24.0, -66.0, 50.0), verts)
        got, want = signstates.states(p[:, 1], p[:, 0]), ref_states(p[:, 1], p[:, 0])
        self.assertTrue(np.array_equal(got, want))
        self.assertGreater((want != '').sum(), 2000)
        for lat, lon in ((39.28, -76.59), (np.float32(39.28), np.float32(-76.59)), (np.array([[39.28, 40.75]]), -76.6),
                         ([], []), (np.nan, np.nan), (21.3, -157.8), (61.2, -149.9)):
            got, want = signstates.states(lat, lon), ref_states(lat, lon)
            self.assertEqual(got.shape, want.shape)
            self.assertTrue(np.array_equal(got, want), (lat, lon))
        del found


if __name__ == "__main__":
    unittest.main()
