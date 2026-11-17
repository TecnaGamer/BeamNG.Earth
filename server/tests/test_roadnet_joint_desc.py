"""roads.joint_desc (earthfast 378, roadnet::copies::joint_desc: the road revamp's R2a) against the Python it replaced,
row for row: random copies of random joints near cell edges (exactly on them too), keys that tie at 6 decimals (Python's
round: ties to even on the exact value), -0.0 against 0.0, the same joint in several copies in and out of its cell,
copies without a cell, numpy float coordinates (numpy's own round), empty and None rows.

The Python version is kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mapcells  # noqa: E402
import roads  # noqa: E402


def ref_joint_desc(copies, fn):
    """roads.joint_desc as Python had it (before earthfast 378), verbatim."""
    out, other = {}, {}
    for f, i in copies:
        cid = f.__dict__.get("_bnge_cell")
        cid = cid[0] if cid else None
        for d in fn(f)[i] or ():
            key = (round(d[0], 6), round(d[1], 6))
            if cid is None or str(mapcells.cell_of(d[1], d[0])) == cid:
                out[key] = d
            else:
                other.setdefault(key, d)
    for key, d in other.items():
        out.setdefault(key, d)
    return tuple(out[k] for k in sorted(out))


class Layer:
    """A stand-in road layer: its cell (or none) and its features' joint rows."""

    def __init__(self, cell, rows):
        if cell is not None:
            self.__dict__["_bnge_cell"] = (cell, "roads", "{}")
        self.rows = rows


def fn(f):
    return f.rows


def _edge_points(rng, k):
    """Points on, just off and inside the edges of cells round Fells Point, Tromso (69.6 N) and 180 E."""
    out = []
    for _ in range(k):
        cell = rng.choice([mapcells.cell_of(39.2834, -76.5934), mapcells.cell_of(69.6489, 18.956),
                           mapcells.cell_of(-33.9, 179.999), mapcells.cell_of(0.0, 0.0)])
        lon0, lat0, lon1, lat1 = cell.bounds()
        lon = rng.choice([lon0, lon1, rng.uniform(lon0, lon1), np.nextafter(lon0, -1e9), np.nextafter(lon1, 1e9)])
        lat = rng.choice([lat0, lat1, rng.uniform(lat0, lat1), np.nextafter(lat0, -1e9), np.nextafter(lat1, 1e9)])
        out.append((float(lon), float(lat)))
    return out


def _tie_points(rng, k):
    """Coordinates that are exact ties at 6 decimals (odd multiples of 1/128 on a whole degree), and their neighbours at
    the two candidate roundings, which merge with them or not by the tie rule."""
    out = []
    for _ in range(k):
        base_lo, base_la = rng.randint(-179, 178), rng.randint(-80, 79)
        lo = base_lo + rng.randrange(1, 128, 2) / 128.0
        la = base_la + rng.randrange(1, 128, 2) / 128.0
        out.append((lo, la))
        out.append((round(lo, 6), la))
        out.append((lo + 0.5e-6, la))
        out.append((lo, la - 0.5e-6))
        out.append((float(np.nextafter(lo, 1e9)), la))
    for _ in range(k):
        # decimal halves that are no exact tie in binary (2.675's kind: Python rounds the exact value, numpy's x * 1e6
        # rounds onto the half first), and their candidate roundings
        lo = float(f"{rng.randint(-179, 178)}.{rng.randrange(10 ** 6):06d}5")
        la = float(f"{rng.randint(-80, 79)}.{rng.randrange(10 ** 6):06d}5")
        out += [(lo, la), (round(lo, 6), round(la, 6)), (float(np.round(lo, 6)), float(np.round(la, 6)))]
    return out


class JointDescParityTest(unittest.TestCase):
    def same(self, copies, what=""):
        want = ref_joint_desc(copies, fn)
        got = roads.joint_desc(copies, fn)
        self.assertIsInstance(got, tuple)
        self.assertEqual(len(got), len(want), what)
        for g, w in zip(got, want):
            self.assertIs(g, w, what)

    def random_case(self, rng):
        pts = _edge_points(rng, rng.randint(0, 12)) + _tie_points(rng, rng.randint(0, 3))
        rng.shuffle(pts)
        layers = []
        for _ in range(rng.randint(1, 4)):
            rows = []
            for _ in range(rng.randint(1, 3)):
                if rng.random() < 0.1:
                    rows.append(None if rng.random() < 0.5 else ())
                    continue
                sel = [p for p in pts if rng.random() < 0.6]
                rr = []
                for lo, la in sel:
                    if rng.random() < 0.1:
                        lo, la = np.float64(lo), np.float64(la)
                    if rng.random() < 0.05:
                        lo = -0.0 if rng.random() < 0.5 else 0.0
                    rr.append((lo, la, rng.uniform(3, 20), rng.uniform(10, 90)))
                rows.append(tuple(rr))
            if rng.random() < 0.2:
                cell = None
            else:
                lo, la = rng.choice(pts) if pts else (0.0, 0.0)
                cell = str(mapcells.cell_of(float(la), float(lo)))
            layers.append(Layer(cell, rows))
        copies = [(f, rng.randrange(len(f.rows))) for f in layers for _ in range(rng.randint(1, 2))]
        rng.shuffle(copies)
        return copies

    def test_random(self):
        rng = random.Random(374)
        for k in range(3000):
            self.same(self.random_case(rng), f"case {k}")

    def test_ties_merge_as_python_rounds(self):
        lo = -76.0 + 1 / 128.0              # -75.9921875: a tie at 6 places, to even (-75.992188)
        a = (lo, 39.25, 1.0, 1.0)
        b = (-75.992188, 39.25, 2.0, 2.0)
        c = (-75.992187, 39.25, 3.0, 3.0)
        f = Layer(None, [(a, b, c)])
        got = roads.joint_desc([(f, 0)], fn)
        self.assertEqual(got, ref_joint_desc([(f, 0)], fn))
        self.assertEqual(len(got), 2)

    def test_negative_zero_is_zero(self):
        f = Layer(None, [((-0.0, 1.0, 1.0, 1.0), (0.0, 1.0, 2.0, 2.0), (-1e-9, 1.0, 3.0, 3.0))])
        self.same([(f, 0)])

    def test_in_cell_copy_wins(self):
        cell = mapcells.cell_of(39.2834, -76.5934)
        lon0, lat0, lon1, lat1 = cell.bounds()
        inside = ((lon0 + lon1) / 2, (lat0 + lat1) / 2)
        far = Layer(str(mapcells.cell_of(40.0, -76.0)), [((inside + (1.0, 1.0)),)])
        home = Layer(str(cell), [((inside + (2.0, 2.0)),)])
        self.same([(far, 0), (home, 0)])
        self.assertEqual(roads.joint_desc([(far, 0), (home, 0)], fn)[0][2], 2.0)
        self.same([(home, 0), (far, 0)])

    def test_empty(self):
        self.assertEqual(roads.joint_desc([], fn), ())
        self.same([(Layer(None, [None]), 0), (Layer("14/1/1", [()]), 0)])

    def test_beyond_mercator_raises(self):
        f = Layer("14/1/1", [((0.0, 86.0, 1.0, 1.0),)])
        with self.assertRaises(ValueError):
            ref_joint_desc([(f, 0)], fn)
        with self.assertRaises(ValueError):
            roads.joint_desc([(f, 0)], fn)


if __name__ == "__main__":
    unittest.main()
