"""The width regions of a layer's roads (earthfast 387, roadnet::regions::width_regions: the road revamp's R2a) against
roads._width_regions' numpy, value for value: random layers over the world, Japan's box (and points exactly on its
edges) and the US, features of 0-7 points in several parts, empty layers; malformed offsets raise, never panic.

The numpy version is kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mapgeom  # noqa: E402
import roads  # noqa: E402
from roads import JAPAN_BOX, WIDTH_REGIONS  # noqa: E402


def ref_width_regions(feats):
    n = len(feats)
    out = np.zeros(n, np.int64)
    if not n:
        return out
    states = roads._marking_states(feats)
    out[np.asarray(states) != ''] = WIDTH_REGIONS.index("US")
    starts = feats.coord_off[feats.ring_off[feats.part_off[:-1]]]
    ends = feats.coord_off[feats.ring_off[feats.part_off[1:]]]
    ok = ends > starts
    mids = ((starts + np.maximum(ends - 1, starts)) // 2).astype(int)
    if len(feats.lonlat):
        ll = feats.lonlat[np.clip(mids, 0, len(feats.lonlat) - 1)]
        la0, lo0, la1, lo1 = JAPAN_BOX
        jp = ok & (ll[:, 1] >= la0) & (ll[:, 1] <= la1) & (ll[:, 0] >= lo0) & (ll[:, 0] <= lo1)
        out[jp] = WIDTH_REGIONS.index("JP")
    return out


def layer(rng):
    n = rng.randint(0, 10)
    po, ro, co, ll = [0], [0], [0], []
    la0, lo0, la1, lo1 = JAPAN_BOX
    for _ in range(n):
        where = rng.random()
        if where < 0.3:
            lon, lat = rng.uniform(lo0 - 1, lo1 + 1), rng.uniform(la0 - 1, la1 + 1)
        elif where < 0.4:
            lon, lat = rng.choice([lo0, lo1]), rng.choice([la0, la1, rng.uniform(la0, la1)])
        elif where < 0.7:
            lon, lat = rng.uniform(-125, -66), rng.uniform(24, 50)
        else:
            lon, lat = rng.uniform(-179, 179), rng.uniform(-80, 80)
        for _p in range(rng.choice([1, 1, 2])):
            for _r in range(rng.choice([1, 1, 2])):
                for _ in range(rng.choice([0, 1, 2, 3, 7])):
                    ll.append((lon, lat))
                    lon += rng.uniform(-1e-3, 1e-3)
                    lat += rng.uniform(-1e-3, 1e-3)
                co.append(len(ll))
            ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    return mapgeom.Features("roads", np.array([f"r{i}" for i in range(n)]), np.full(n, 2, np.uint8),
                            np.array(po, np.int64), np.array(ro, np.int64), np.array(co, np.int64),
                            np.array(ll, np.float64).reshape(-1, 2))


class WidthRegionsParityTest(unittest.TestCase):
    def test_random_layers(self):
        rng = random.Random(387)
        seen = set()
        for k in range(1500):
            f = layer(rng)
            want = ref_width_regions(f)
            f.__dict__.pop("_marking_states", None)
            got = roads._width_regions(f)
            self.assertEqual(got.dtype, want.dtype, k)
            self.assertTrue(np.array_equal(got, want), (k, got, want))
            seen.update(want.tolist())
        self.assertEqual(seen, {0, 1, 2})

    def test_malformed_offsets_raise(self):
        f = layer(random.Random(5))
        while not len(f):
            f = layer(random.Random(6))
        f.coord_off = f.coord_off.copy()
        f.coord_off[-1] += 9
        try:
            roads._width_regions(f)
        except Exception as e:      # noqa: BLE001
            self.assertIsInstance(e, IndexError)
        except BaseException as e:  # noqa: BLE001
            self.fail(f"{type(e).__name__}: {e}")


class OneColumnPointsTest(unittest.TestCase):
    """A (k, 1) points array handed to a roadnet kernel directly (Codex on 35a38d05: width_regions read its column 1:
    a Rust panic): IndexError from each kernel that takes a layer's lon / lat, never a PanicException."""

    def check(self, call):
        try:
            call()
        except IndexError:
            return
        except BaseException as e:  # noqa: BLE001
            self.fail(f"{type(e).__name__}: {e}")
        self.fail("no error")

    def test_kernels_raise_index_error(self):
        import fast
        ef = fast._ef
        one = np.zeros((1, 1), np.float64)
        po, ro, co = (np.array([0, 1], np.int64), np.array([0, 1], np.int64), np.array([0, 1], np.int64))
        self.check(lambda: ef.width_regions(np.zeros(1, bool), po, ro, co, one, tuple(float(v) for v in JAPAN_BOX),
                                            1, 2))
        self.check(lambda: ef.layer_lengths(po, ro, co, one))
        self.check(lambda: ef.lane_flow_rows(po, ro, co, one, None, None, [], None, np.zeros(1, bool), {}, False,
                                             None, [], None))
        self.check(lambda: ef.lane_joint_specs(["r0"], np.full(1, 2, np.uint8), po, ro, co, one, None, None, None,
                                               None, None, None, None, {}, 3.0, (0.5, 1.0, 3)))
        self.check(lambda: ef.lane_offset(one, np.zeros(1)))


def ref_kernel(us, po, ro, co, ll):
    """ref_width_regions on raw arrays (the kernel's own inputs: n = len(part offsets) - 1)."""
    n = max(len(po) - 1, 0)
    out = np.zeros(n, np.int64)
    if not n:
        return out
    out[us] = WIDTH_REGIONS.index("US")
    starts = co[ro[po[:-1]]]
    ends = co[ro[po[1:]]]
    ok = ends > starts
    mids = ((starts + np.maximum(ends - 1, starts)) // 2).astype(int)
    if len(ll):
        p = ll[np.clip(mids, 0, len(ll) - 1)]
        la0, lo0, la1, lo1 = JAPAN_BOX
        jp = ok & (p[:, 1] >= la0) & (p[:, 1] <= la1) & (p[:, 0] >= lo0) & (p[:, 0] <= lo1)
        out[jp] = WIDTH_REGIONS.index("JP")
    return out


class OffsetFuzzTest(unittest.TestCase):
    """The layer checks every roadnet kernel runs first (roadnet::lanesplit::check_offsets / check_lonlat; Codex rounds
    1-2 on 35a38d05: a one-column array, a negative singleton ring offset) on random small arrays: negative, singleton,
    empty, decreasing, past-the-end offsets, points of 0-3 columns. width_regions raises IndexError or gives numpy's
    answer; the other kernels on the same arrays raise an ordinary exception or answer, never a Rust panic."""

    def arrays(self, rng):
        def offs():
            k = rng.choice([0, 1, 1, 2, 3, 4])
            if rng.random() < 0.5:          # (mostly well formed: non-decreasing from 0, then a fault or none)
                a = [0]
                for _ in range(k - 1):
                    a.append(a[-1] + rng.choice([0, 1, 1, 2]))
                a = a[:k]
                if a and rng.random() < 0.6:
                    a[rng.randrange(len(a))] += rng.choice([-9, -2, -1, 1, 2, 9])
                return np.array(a, np.int64)
            return np.array([rng.randint(-3, 6) for _ in range(k)], np.int64)
        po, ro, co = offs(), offs(), offs()
        cols = rng.choice([2, 2, 2, 1, 0, 3])
        k = rng.randint(0, 6)
        ll = np.array([[rng.uniform(120, 150), rng.uniform(20, 50), 0.0][:cols] for _ in range(k)],
                      np.float64).reshape(k, cols)
        n = max(len(po) - 1, 0)
        us = np.array([rng.random() < 0.3 for _ in range(n if rng.random() < 0.9 else n + 1)], bool)
        return us, po, ro, co, ll

    def test_raises_index_error_or_matches_numpy(self):
        import fast
        ef = fast._ef
        rng = random.Random(3871)
        stats = {"ok": 0, "raised": 0}
        for k in range(20000):
            us, po, ro, co, ll = self.arrays(rng)
            try:
                got = ef.width_regions(us, po, ro, co, ll, tuple(float(v) for v in JAPAN_BOX), 1, 2)
            except IndexError:
                stats["raised"] += 1
                got = None
            except BaseException as e:  # noqa: BLE001
                self.fail(f"{k}: {type(e).__name__}: {e} {(us, po, ro, co, ll)}")
            if got is not None:
                stats["ok"] += 1
                want = ref_kernel(us, po, ro, co, ll)
                self.assertTrue(np.array_equal(got, want), (k, got, want, us, po, ro, co, ll))
            n = max(len(po) - 1, 0)
            for name, call in (
                    ("layer_lengths", lambda: ef.layer_lengths(po, ro, co, ll)),
                    ("lane_flow_rows", lambda: ef.lane_flow_rows(po, ro, co, ll, None, None, [], None,
                                                                 np.zeros(n, bool), {}, False, None, [], None)),
                    ("lane_joint_specs", lambda: ef.lane_joint_specs(
                        [f"r{i}" for i in range(n)], np.full(n, 2, np.uint8), po, ro, co, ll, None, None, None,
                        None, None, None, None, {}, 3.0, (0.5, 1.0, 3)))):
                try:
                    call()
                except Exception:       # noqa: BLE001 (an ordinary error is the old code's; a panic is not one)
                    pass
                except BaseException as e:  # noqa: BLE001
                    self.fail(f"{k} {name}: {type(e).__name__}: {e} {(po, ro, co, ll)}")
        self.assertGreater(stats["ok"], 2000)
        self.assertGreater(stats["raised"], 2000)

    def test_negative_singleton_ring_offset(self):
        """Codex round 2 on fde0e52d: part offsets [0, 0], ring offsets [-1] (one feature)."""
        import fast
        with self.assertRaises(IndexError):
            fast._ef.width_regions(np.zeros(1, bool), np.array([0, 0], np.int64), np.array([-1], np.int64),
                                   np.array([0], np.int64), np.zeros((1, 2)), tuple(float(v) for v in JAPAN_BOX), 1, 2)


if __name__ == "__main__":
    unittest.main()
