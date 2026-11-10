"""roads._merge_overlaps and roads._ground_pins (their pair kernels in Rust: deckmerge.rs) against the original NumPy
versions (deckmerge_reference.py), bit for bit, on random bridge chains: forks, twin carriageways, crossings, decks
of several levels, heights with NaN and inf, ground roads ending at the joints; and
_deck_beside's pairs (deck_beside's pruned search) against every sample measured against every segment."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import roads
import deckmerge_reference as ref


def same(a, b):
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        x, y = np.asarray(x), np.asarray(y)
        if x.shape != y.shape or x.dtype != y.dtype or x.tobytes() != y.tobytes():
            return False
    return True


def chain(rng):
    """(lines, zs, widths, levels, pins, ground lines) of a random chain of deck parts around a few shared joints."""
    joints = rng.uniform(-120, 120, (int(rng.integers(2, 6)), 2))
    lines = []
    for _ in range(int(rng.integers(2, 9))):
        a = joints[rng.integers(len(joints))] if rng.random() < 0.7 else rng.uniform(-150, 150, 2)
        if rng.random() < 0.3 and lines:            # a twin beside a part already there
            base = lines[int(rng.integers(len(lines)))][:, :2]
            d = np.diff(base, axis=0)
            nrm = np.c_[-d[:, 1], d[:, 0]] / np.maximum(np.hypot(*d.T), 1e-9)[:, None]
            nrm = np.r_[nrm, nrm[-1:]]
            ln = base + nrm * rng.uniform(-14, 14)
        else:
            b = joints[rng.integers(len(joints))] if rng.random() < 0.6 else rng.uniform(-150, 150, 2)
            if np.hypot(*(b - a)) < 5:
                b = a + rng.uniform(10, 60, 2)
            n = int(rng.integers(2, 40))
            t = np.linspace(0, 1, n)[:, None]
            bow = np.sin(np.pi * t) * rng.uniform(-15, 15) * np.array([[-(b - a)[1], (b - a)[0]]]) / np.hypot(*(b - a))
            ln = a + t * (b - a) + bow
        if rng.random() < 0.2:
            ln = np.c_[ln, rng.uniform(0, 5, len(ln))]       # (k, 3) lines too
        lines.append(np.asarray(ln, np.float64))
    zs = []
    for ln in lines:
        z = 8 + rng.normal(0, 3, len(ln)).cumsum() * 0.3
        if rng.random() < 0.08:
            z[int(rng.integers(len(z)))] = rng.choice([np.nan, np.inf, -np.inf])
        if rng.random() < 0.04:
            z[:] = np.nan
        zs.append(z)
    widths = [float(rng.choice([7.0, 9.5, 12.0, 15.3])) for _ in lines]
    levels = None if rng.random() < 0.2 else [float(rng.choice([1.0, 1.0, 2.0, 3.0])) for _ in lines]
    ends = [ln[k, :2] for ln in lines for k in (0, -1)]
    pins = None if rng.random() < 0.4 else [e + rng.uniform(-0.5, 0.5, 2) for e in ends if rng.random() < 0.3]
    ground = []
    for _ in range(int(rng.integers(0, 30))):
        e = ends[int(rng.integers(len(ends)))] + rng.uniform(-1.5, 1.5, 2)
        n = int(rng.integers(1, 8))
        g = e + np.cumsum(rng.uniform(-12, 12, (n, 2)), axis=0) * (rng.random() < 0.8)
        g = np.r_[e[None], g] if rng.random() < 0.5 else np.r_[g, e[None]]
        ground.append(g if rng.random() < 0.8 else np.c_[g, np.zeros(len(g))])
    return lines, zs, widths, levels, pins, ground


class DeckMergeTest(unittest.TestCase):
    def test_merge_overlaps_matches_numpy(self):
        rng = np.random.default_rng(20261108)
        merged = 0
        for case in range(400):
            lines, zs, widths, levels, pins, _ = chain(rng)
            want = ref._merge_overlaps(lines, zs, widths, levels, pins=pins)
            got = roads._merge_overlaps(lines, zs, widths, levels, pins=pins)
            self.assertTrue(same(want, got), f"case {case}")
            merged += want is not zs
        self.assertGreater(merged, 200)         # (most cases have related pairs)

    def test_ground_pins_match_numpy(self):
        rng = np.random.default_rng(7)
        found = 0
        for case in range(400):
            lines, _, _, _, _, ground = chain(rng)
            want = ref._ground_pins(lines, ground)
            got = roads._ground_pins(lines, ground)
            self.assertEqual(len(want), len(got), f"case {case}")
            self.assertTrue(same(want, got), f"case {case}")
            found += len(want)
        self.assertGreater(found, 50)

    def test_deck_beside_matches_brute_force(self):
        # (earthfast.deck_beside leaves out boxes of segments too far to matter: the pairs of measuring every sample
        # against every segment)
        rng = np.random.default_rng(11)
        found = 0
        for case in range(300):
            lines, _, widths, _, _, _ = chain(rng)
            pts = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
            want = []
            for i in range(len(pts)):
                si = np.r_[0.0, np.cumsum(np.hypot(*np.diff(pts[i], axis=0).T))]
                s = np.arange(0.0, si[-1] + 1e-9, roads.STACK_STEP_M)
                q = np.c_[np.interp(s, si, pts[i][:, 0]), np.interp(s, si, pts[i][:, 1])]
                for j in range(i + 1, len(pts)):
                    P, ab = pts[j][:-1], np.diff(pts[j], axis=0)
                    L2 = np.maximum((ab * ab).sum(1), 1e-12)
                    tt = np.clip(((q[:, None, 0] - P[None, :, 0]) * ab[None, :, 0]
                                  + (q[:, None, 1] - P[None, :, 1]) * ab[None, :, 1]) / L2[None], 0.0, 1.0)
                    d = np.hypot(q[:, None, 0] - (P[None, :, 0] + tt * ab[None, :, 0]),
                                 q[:, None, 1] - (P[None, :, 1] + tt * ab[None, :, 1])).min(1)
                    if (d < (widths[i] + widths[j]) / 2 + roads.MERGE_TOUCH_M).sum() >= 2:
                        want.append((i, j))
            got = roads._deck_beside(lines, widths)
            self.assertEqual(want, got, f"case {case}")
            found += len(want)
        self.assertGreater(found, 100)

    def test_short_part_raises_as_numpy(self):
        lines = [np.array([[0.0, 0.0], [10.0, 0.0]]), np.array([[5.0, 0.0]])]
        zs = [np.array([1.0, 2.0]), np.array([3.0])]
        for fn in (ref._merge_overlaps, roads._merge_overlaps):
            with self.assertRaises(ValueError):
                fn(lines, zs, [8.0, 8.0], [1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
