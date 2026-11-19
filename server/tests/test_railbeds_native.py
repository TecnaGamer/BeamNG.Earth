"""railways.bed_heights in Rust (earthfast 483, railbeds::rail_bed_heights: the full Rust port) against the Python it
replaced: random lines of ground heights (lengths 0 to 80, every half width, NaN / inf / -0.0 / subnormal / huge
values, runs of -0.0 at the ends, strided and float32 input) give the same type, dtype, shape and bytes; a line of
fewer than 3 heights is returned as itself; railways.profile's beds are the old per-part ones.

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import railways  # noqa: E402


# ---- the Python versions (before earthfast 483), verbatim but for r passed in ----

def ref_avg(v, r):
    """Running mean over 2r + 1 values, the ends extended along their grade (odd reflection: a straight grade stays
    straight to its end, and two parts meeting at a joint meet at its height)."""
    r = min(r, len(v) - 1)
    if r < 1:
        return np.asarray(v, np.float64)
    pad = np.concatenate((2 * v[0] - v[r:0:-1], v, 2 * v[-1] - v[-2:-r - 2:-1]))
    c = np.concatenate(([0.0], np.cumsum(pad)))
    return (c[2 * r + 1:] - c[:-2 * r - 1]) / (2 * r + 1)


def ref_bed_heights(z, r=None):
    z = np.asarray(z, np.float64)
    if len(z) < 3:
        return z
    if r is None:
        r = max(1, int(round(railways.SMOOTH_M / railways.STEP_M)))
    with np.errstate(all="ignore"):
        return ref_avg(ref_avg(z, r), r // 2)


def same(a, b):
    return (type(a) is type(b) and a.dtype == b.dtype and a.shape == b.shape
            and a.tobytes() == b.tobytes())


SPECIAL = [np.nan, np.inf, -np.inf, -0.0, 0.0, 5e-324, -5e-324, 1e308, -1e308, 2.2250738585072014e-308, 1e-300]


def rand_line(rng, n):
    kind = rng.random()
    if kind < 0.35:             # a smooth ground line with DEM bumps
        s = np.arange(n) * 2.0
        z = rng.uniform(-50, 3000) + rng.uniform(-0.1, 0.1) * s + rng.uniform(0, 2) * np.sin(s / rng.uniform(1, 9))
    elif kind < 0.55:           # values of every size
        z = np.array([rng.uniform(-1, 1) * 10.0 ** rng.uniform(-320, 308) for _ in range(n)])
    elif kind < 0.75:           # sprinkled with the special values
        z = np.array([rng.choice(SPECIAL) if rng.random() < 0.3 else rng.uniform(-100, 100) for _ in range(n)])
    elif kind < 0.9:            # runs of signed zeros (np.cumsum's first value: -0.0 kept)
        z = np.array([rng.choice((-0.0, 0.0, -0.0, 1.0, -1.0)) for _ in range(n)])
    else:                       # integers held as floats (exact sums)
        z = np.array([float(rng.randint(-5, 5)) for _ in range(n)])
    return np.asarray(z, np.float64).reshape(n)


class BedHeightsTest(unittest.TestCase):
    def setUp(self):
        fast.require(483, "rail_bed_heights")

    def test_random_lines_every_half_width(self):
        rng = random.Random(481)
        for t in range(6000):
            n = rng.choice([0, 1, 2, 3, 4, 5, 6, 7, 11, 12, 13, 20, rng.randint(0, 80)])
            r = rng.choice([1, 2, 3, 4, 5, 5, 5, 6, 7, 12, 40, 100])
            zs = [rand_line(rng, n) for _ in range(rng.randint(1, 4))]
            got = fast._ef.rail_bed_heights(zs, r)
            self.assertIs(type(got), list)
            self.assertEqual(len(got), len(zs))
            for z, g in zip(zs, got):
                want = ref_bed_heights(z, r)
                self.assertTrue(same(g, want), f"case {t}: n {n} r {r}\n{z!r}\n{g!r}\n{want!r}")

    def test_signed_zero_runs(self):
        for n in range(3, 30):
            for r in (1, 2, 5, 9):
                for z in (np.full(n, -0.0), np.r_[np.full(n - 1, -0.0), 1.0], np.r_[1.0, np.full(n - 1, -0.0)],
                          np.r_[np.full(n - 1, -0.0), 0.0], np.r_[0.0, np.full(n - 1, -0.0)]):
                    self.assertTrue(same(fast._ef.rail_bed_heights([z], r)[0], ref_bed_heights(z, r)), f"{n} {r} {z}")

    def test_bed_heights_glue(self):
        rng = random.Random(7)
        for _ in range(500):
            z = rand_line(rng, rng.randint(0, 60))
            self.assertTrue(same(railways.bed_heights(z), ref_bed_heights(z)))
        for smooth, step in ((10.0, 2.0), (3.0, 2.0), (1.0, 2.0), (0.2, 2.0), (25.0, 1.0)):     # r 5, 2 (1.5), 1, 1, 25
            with mock.patch.object(railways, "SMOOTH_M", smooth), mock.patch.object(railways, "STEP_M", step):
                for _ in range(100):
                    z = rand_line(rng, rng.randint(0, 60))
                    self.assertTrue(same(railways.bed_heights(z), ref_bed_heights(z)), f"{smooth} {step}")

    def test_inputs_cast_and_strided(self):
        rng = random.Random(3)
        for _ in range(200):
            base = rand_line(rng, rng.randint(0, 90))
            with np.errstate(over="ignore"):
                f32 = base.astype(np.float32)
            for z in (base[::2], base[::-3], f32, list(base), base.astype(np.int64)
                      if np.isfinite(base).all() and (np.abs(base) < 1e15).all() else base):
                self.assertTrue(same(railways.bed_heights(z), ref_bed_heights(z)))

    def test_odd_byte_strides(self):
        """A float64 view whose stride is no whole number of float64s (packed records): read as numpy reads it."""
        rng = random.Random(9)
        for _ in range(100):
            n = rng.randint(0, 40)
            for itemsize in (9, 12, 17):
                rec = np.zeros(n, np.dtype([("z", "<f8"), ("pad", f"V{itemsize - 8}")], align=False))
                rec["z"] = rand_line(rng, n)
                z = rec["z"]
                self.assertEqual(z.strides, (itemsize,) if n else z.strides)
                self.assertTrue(same(railways.bed_heights(z), ref_bed_heights(z)))
        with self.assertRaises(ValueError):         # (the kernel itself refuses such a view)
            rec = np.zeros(8, np.dtype([("z", "<f8"), ("pad", "V1")]))
            fast._ef.rail_bed_heights([rec["z"]], 5)

    def test_unaligned_and_column_lines(self):
        """A unit-stride float64 view at an odd byte offset (unaligned) and an (n, 1) column: as numpy ran them."""
        rng = random.Random(13)
        for _ in range(100):
            n = rng.randint(0, 40)
            src = rand_line(rng, n)
            for off in (1, 3, 7):
                z = np.ndarray(n, np.float64, np.zeros(8 * n + off, np.uint8), off)
                z[...] = src
                self.assertTrue(n == 0 or not z.flags.aligned)
                self.assertTrue(same(railways.bed_heights(z), ref_bed_heights(z)))
            col = src.reshape(n, 1)
            want = ref_bed_heights(col)
            got = railways.bed_heights(col)
            self.assertTrue(same(got, want), f"{n}")
            if n < 3:
                self.assertIs(got, col)

    def test_short_lines_are_themselves(self):
        for n in (0, 1, 2):
            z = np.arange(n, dtype=np.float64)
            self.assertIs(railways.bed_heights(z), z)
        out = railways.bed_heights_many([np.zeros(2), np.zeros(5), np.zeros(0)])
        self.assertEqual([len(z) for z in out], [2, 5, 0])
        self.assertEqual(railways.bed_heights_many([]), [])
        self.assertEqual(fast._ef.rail_bed_heights([], 5), [])

    def test_profile_beds_as_before(self):
        """railways.profile (one kernel call for its ground parts) against bed_heights part by part as it was."""
        rng = random.Random(11)
        for _ in range(60):
            rails = []
            for p in range(rng.randint(1, 6)):
                n = rng.randint(1, 40)
                x0, y0 = rng.uniform(-300, 300), rng.uniform(-300, 300)
                ang = rng.uniform(0, 6.28)
                s = np.arange(n) * railways.STEP_M
                xy = np.column_stack((x0 + s * np.cos(ang), y0 + s * np.sin(ang)))
                rails.append((f"f{p}", "standard_gauge", "ground", xy))

            def ground(xy):
                return 20.0 + 0.03 * xy[:, 0] + np.sin(xy[:, 1] / 7.0)
            with mock.patch.object(railways.railxing, "ENABLED", False):
                got = railways.profile(rails, ground)
            for (_f, _c, _k, xy), (zb, zg) in zip(rails, got):
                self.assertTrue(same(zb, ref_bed_heights(np.asarray(ground(xy), np.float64))))


if __name__ == "__main__":
    unittest.main()
