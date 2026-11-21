"""oneway_joins.apply in Rust (earthfast 452, onewayjoin::oneway_join_ends: the full Rust port) against the Python it
replaced, point for point: random road layers of one-way and two-way lines meeting at shared points (several at a
point, chains of one-ways, one-ways of either sense, lines of one point, several parts, near-duplicate points), lane
counts known or NaN, both hands: the same moved count and the same lon / lat bits after (every move applied in the
same order). NaN / inf points: the same exception class; a read-only array: read, refused at the first move. Outside
the input contract (a float32 or non-contiguous lonlat, a lanes column of objects): TypeError.

The Python version is kept here only as the reference (the server has no Python twin: fast.require)."""
import math
import os
import random
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dmath  # noqa: E402
import fast  # noqa: E402
import mapgeom  # noqa: E402
import oneway_joins  # noqa: E402
from oneway_joins import BLEND_M, KEY_DEG, LANE_M, MAX_SHIFT_M, TWO_WAY_W_M  # noqa: E402


# ---- the Python version (before earthfast 452), verbatim ----

def _oneway(feats, i):
    v = feats.attrs.get("osm_oneway")
    if v is None:
        return 0
    s = str(v[i])
    return 1 if s in ("yes", "1", "true") else -1 if s == "-1" else 0


def ref_apply(feats, left_hand=False):
    if len(feats) == 0 or "osm_oneway" not in feats.attrs:
        return 0
    ll = feats.lonlat
    co, ro, po = feats.coord_off, feats.ring_off, feats.part_off
    rings = []
    for i in range(len(feats)):
        r = ro[po[i]]
        rings.append((i, int(co[r]), int(co[r + 1]) - 1))
    at = {}
    for i, a, b in rings:
        for c in range(a, b + 1):
            at.setdefault((round(ll[c, 0] / KEY_DEG), round(ll[c, 1] / KEY_DEG)), []).append((i, c))
    lanes = feats.attrs.get("osm_lanes")
    moved = 0
    for i, a, b in rings:
        ow = _oneway(feats, i)
        if not ow or b - a < 1:
            continue
        for end, nxt, sign in ((a, a + 1, 1), (b, b - 1, -1)):
            key = (round(ll[end, 0] / KEY_DEG), round(ll[end, 1] / KEY_DEG))
            others = [(j, c) for j, c in at.get(key, ()) if j != i]
            two = [(j, c) for j, c in others if _oneway(feats, j) == 0]
            if not two or any(_oneway(feats, j) for j, _ in others):
                continue
            lat = math.radians(ll[end, 1])
            mx = 111320.0 * dmath.cos(lat)
            my = 110540.0
            dx = (ll[nxt, 0] - ll[end, 0]) * mx * sign
            dy = (ll[nxt, 1] - ll[end, 1]) * my * sign
            dx, dy = dx * ow, dy * ow
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            dx, dy = dx / L, dy / L
            j = two[0][0]
            w2 = TWO_WAY_W_M
            if lanes is not None and not np.isnan(lanes[j]):
                w2 = float(lanes[j]) * LANE_M
            off = min(w2 / 4.0, MAX_SHIFT_M)
            rx, ry = (dy, -dx) if not left_hand else (-dy, dx)
            ox, oy = rx * off / mx, ry * off / my
            run = 0.0
            c, prev = end, end
            while a <= c <= b:
                if c != end:
                    run += math.hypot((ll[c, 0] - ll[prev, 0]) * mx, (ll[c, 1] - ll[prev, 1]) * my)
                t = max(0.0, 1.0 - run / BLEND_M)
                if t <= 0:
                    break
                ll[c, 0] += ox * t
                ll[c, 1] += oy * t
                prev = c
                c += sign
            moved += 1
    return moved


def layer(rng, bad=False):
    """Lines around a few junction points (lon / lat near 39.28 N 76.6 W), some meeting there."""
    lon0, lat0 = -76.6 + rng.uniform(-0.01, 0.01), 39.28 + rng.uniform(-0.01, 0.01)
    hubs = [(lon0 + rng.uniform(-3e-4, 3e-4), lat0 + rng.uniform(-3e-4, 3e-4)) for _ in range(rng.randint(1, 4))]
    lines = []
    for _ in range(rng.randint(1, 9)):
        h = rng.choice(hubs)
        k = rng.choice([1, 2, 2, 3, 5, 8])
        ang = rng.uniform(0, 6.283)
        step = rng.choice([1e-5, 3e-5, 1e-4, 2e-6])
        pts = [(h[0] + j * step * dmath.cos(ang), h[1] + j * step * dmath.sin(ang)) for j in range(k)]
        if rng.random() < 0.5:
            pts.reverse()
        if rng.random() < 0.3 and len(pts) > 1:
            pts[0] = rng.choice(hubs)
        lines.append(pts)
    po, ro, co, ll = [0], [0], [0], []
    for pts in lines:
        for _ in range(rng.choice([1, 1, 1, 2])):
            ll.extend(pts)
            co.append(len(ll))
        ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    n = len(lines)
    f = mapgeom.Features("roads", np.array([f"r{i}" for i in range(n)]), np.full(n, mapgeom.LINE, np.uint8),
                         np.array(po, np.int64), np.array(ro, np.int64), np.array(co, np.int64),
                         np.array(ll, np.float64).reshape(-1, 2))
    vals = [rng.choice(["yes", "-1", "", "no", "1", "true", "", ""]) for _ in range(n)]
    f.attrs["osm_oneway"] = mapgeom.text_column(vals) if rng.random() < 0.7 else np.array(vals, object)
    if rng.random() < 0.7:
        f.attrs["osm_lanes"] = np.array([rng.choice([np.nan, 1.0, 2.0, 4.0, 6.0]) for _ in range(n)], np.float64)
    if bad and len(f.lonlat):
        k = rng.randrange(len(f.lonlat))
        f.lonlat[k, rng.randrange(2)] = rng.choice([np.nan, np.inf, -np.inf])
    return f


def copy_layer(f):
    g = mapgeom.Features(f.layer, f.ids.copy(), f.kind.copy(), f.part_off.copy(), f.ring_off.copy(), f.coord_off.copy(),
                         f.lonlat.copy())
    g.attrs = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in f.attrs.items()}
    return g


def outcome(fn, *args):
    try:
        return "ok", fn(*args)
    except Exception as e:      # noqa: BLE001 (the class is the point)
        return "raised", type(e)


class OnewayJoinParityTest(unittest.TestCase):
    def check(self, seed, count, bad):
        rng = random.Random(seed)
        tally = {"moved": 0, "raised": 0, "layers": 0}
        for k in range(count):
            f = layer(rng, bad)
            lh = rng.random() < 0.3
            g = copy_layer(f)
            want = outcome(ref_apply, f, lh)
            got = outcome(oneway_joins.apply, g, lh)
            self.assertEqual(got, want, k)
            self.assertEqual(g.lonlat.tobytes(), f.lonlat.tobytes(), k)
            tally["layers"] += 1
            tally["moved"] += want[1] if want[0] == "ok" else 0
            tally["raised"] += want[0] == "raised"
        return tally

    def test_random_layers(self):
        t = self.check(452, 6000, False)
        self.assertGreater(t["moved"], 1500)
        self.assertEqual(t["raised"], 0)

    def test_int_lane_counts(self):
        """osmtags.join makes osm_lanes int64 when every road has a lane count (np.array of ints): the same moves as
        the removed Python (float(lanes[j])), never a TypeError (Codex on d4bd7eb4)."""
        rng = random.Random(4523)
        moved = 0
        for k in range(1500):
            f = layer(rng)
            f.attrs["osm_lanes"] = np.array([rng.choice([1, 2, 3, 4, 6]) for _ in range(len(f))])
            self.assertEqual(f.attrs["osm_lanes"].dtype, np.int64)
            g = copy_layer(f)
            want, got = outcome(ref_apply, f, False), outcome(oneway_joins.apply, g, False)
            self.assertEqual(got, want, k)
            self.assertEqual(want[0], "ok", k)
            self.assertEqual(g.lonlat.tobytes(), f.lonlat.tobytes(), k)
            self.assertEqual(g.attrs["osm_lanes"].dtype, np.int64)     # (the stored column left as it was)
            moved += want[1]
        self.assertGreater(moved, 300)

    def test_non_finite_points(self):
        t = self.check(4521, 2000, True)
        self.assertGreater(t["raised"], 500)

    def test_read_only_points(self):
        rng = random.Random(4522)
        seen = set()
        for k in range(500):
            f = layer(rng)
            g = copy_layer(f)
            f.lonlat.flags.writeable = False
            g.lonlat.flags.writeable = False
            want, got = outcome(ref_apply, f, False), outcome(oneway_joins.apply, g, False)
            self.assertEqual(got, want, k)
            seen.add(want[0])
        self.assertEqual(seen, {"ok", "raised"})

    def test_contract(self):
        f = layer(random.Random(5))
        for bad in (lambda g: setattr(g, "lonlat", g.lonlat.astype(np.float32)),
                    lambda g: setattr(g, "lonlat", np.asfortranarray(np.c_[g.lonlat, g.lonlat])[:, :2]),
                    lambda g: g.attrs.__setitem__("osm_lanes", np.array([None] * len(g), object))):
            g = copy_layer(f)
            bad(g)
            self.assertEqual(outcome(oneway_joins.apply, g, False), ("raised", TypeError))


class VersionSkewTest(unittest.TestCase):
    def test_a_451_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 452)
        f = layer(random.Random(1))
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=451)):
            with self.assertRaises(fast.NativeMissing):
                oneway_joins.apply(f)


if __name__ == "__main__":
    unittest.main()
