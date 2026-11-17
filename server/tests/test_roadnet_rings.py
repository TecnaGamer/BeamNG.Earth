"""A roundabout ring's radius (earthfast 386, roadnet::rings::ring_radius: the road revamp's R2a) against roads.ring_radius'
numpy / dnp version, value for value: random rings of 5-300 points (circles of 2-100 m with noise, arcs, closed rings,
duplicate points, collinear points that leave dnp.lstsq's QR for its eigh answer, single repeated points, NaN / inf
points), at the equator, mid latitudes and 70 N; never a panic.

The numpy version is kept here only as the reference (the server has no Python twin: fast.require)."""
import math
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dnp  # noqa: E402
import fast  # noqa: E402


# ---- the numpy version (before earthfast 386), verbatim but for the layer lookup ----

def ref_ring_radius(ll):
    ll = np.asarray(ll, np.float64)[:, :2]
    if len(ll) < 5:
        return None
    k = dnp.cos(np.radians(ll[:, 1].mean())) * 111320.0
    xy = np.column_stack([(ll[:, 0] - ll[0, 0]) * k, (ll[:, 1] - ll[0, 1]) * 110540.0])
    a = np.column_stack([2 * xy, np.ones(len(xy))])
    try:
        sol = dnp.lstsq(a, (xy * xy).sum(1), rcond=None)[0]
    except np.linalg.LinAlgError:
        return None
    r2 = sol[2] + sol[0] * sol[0] + sol[1] * sol[1]
    if r2 <= 0:
        return None
    r = float(np.sqrt(r2))
    res = float(np.abs(dnp.hypot(xy[:, 0] - sol[0], xy[:, 1] - sol[1]) - r).max())
    return r if 3.0 <= r <= 80.0 and res <= 0.15 * r else None


def random_ring(rng):
    lat0 = rng.choice([0.5, 39.28, 48.2, 70.0, -33.9])
    lon0 = rng.uniform(-170, 170)
    n = rng.choice([4, 5, 6, 9, 17, 40, 130, 300])
    kind = rng.random()
    r = rng.choice([2.0, 3.0, 12.0, 35.0, 80.0, 100.0])
    out = []
    for j in range(n):
        if kind < 0.15:             # collinear: QR finds the columns dependent
            t = j / max(n - 1, 1)
            e, nn = t * 50.0, t * 20.0
        elif kind < 0.2:            # one point repeated
            e, nn = 0.0, 0.0
        else:
            span = 2 * math.pi if kind < 0.7 else rng.uniform(0.2, 2.0)
            th = span * j / n + rng.uniform(-0.05, 0.05)
            e = r * math.cos(th) + rng.gauss(0, r * rng.choice([0.0, 0.01, 0.2]))
            nn = r * math.sin(th) + rng.gauss(0, r * 0.01)
        out.append((lon0 + e / (111320.0 * math.cos(math.radians(lat0))), lat0 + nn / 110540.0))
    if kind > 0.95 and out:
        out[rng.randrange(len(out))] = rng.choice([(np.nan, lat0), (lon0, np.inf)])
    if rng.random() < 0.1 and len(out) > 2:
        out.append(out[0])          # closed
    if rng.random() < 0.1 and len(out) > 2:
        out.insert(1, out[1])       # a duplicate point
    return np.array(out, np.float64).reshape(-1, 2)


class RingRadiusParityTest(unittest.TestCase):
    def test_random_rings(self):
        rng = random.Random(386)
        counts = {"some": 0, "none": 0}
        for k in range(3000):
            ll = random_ring(rng)
            with np.errstate(all="ignore"):
                want = ref_ring_radius(ll)
            try:
                got = fast._ef.ring_radius(np.ascontiguousarray(ll))
            except BaseException as e:  # noqa: BLE001
                self.fail(f"{type(e).__name__}: {e}")
            if want is None:
                self.assertIsNone(got, (k, ll))
                counts["none"] += 1
            else:
                self.assertEqual(np.float64(got).tobytes(), np.float64(want).tobytes(), (k, got, want))
                counts["some"] += 1
        self.assertGreater(counts["some"], 300)
        self.assertGreater(counts["none"], 800)

    def test_eigh_path(self):
        """Collinear points: dnp.lstsq's QR finds the columns dependent and its eigh answer stands."""
        rng = random.Random(3861)
        for k in range(200):
            n = rng.randint(5, 40)
            lat0, lon0 = 48.2, 16.4
            ll = np.array([(lon0 + j * 1e-5, lat0 + j * 2e-5 * rng.choice([1, 1, 0])) for j in range(n)])
            with np.errstate(all="ignore"):
                want = ref_ring_radius(ll)
            got = fast._ef.ring_radius(np.ascontiguousarray(ll))
            self.assertEqual(got, want, k)


if __name__ == "__main__":
    unittest.main()
