"""buildings' mesh assembly in Rust (earthfast 519, bldmesh.rs; the full Rust port's r2a19): Mesh (BldMesh) and the
pieces build() draws with it (the box-frame roofs and their chimneys, flat lids, cornices, boxes, stoops, signs, roof
clutter) give the numbers try598's Python gave, byte for byte. DIGEST is the SHA-256 of every piece's arrays over
seeded footprints and specs, measured with try598's Python buildings.py (the same calls; ~/bnge-tests/r2a19:
parity.py and callcheck.py compared them call by call, randomly and on three cold caches)."""
import hashlib
import math
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import buildings as bld  # noqa: E402
import dmath  # noqa: E402  (the fixture's trigonometry: the same bytes on every platform)

ROOFS = ["flat", "gabled", "hipped", "half-hipped", "pyramidal", "mansard", "gambrel", "round", "skillion", "saltbox"]


def _ring(r):
    kind = r.random()
    if kind < 0.5:
        w, h, a = r.uniform(3, 40), r.uniform(3, 30), r.uniform(0, math.pi)
        c, s = dmath.cos(a), dmath.sin(a)
        pts = [(x * c - y * s, x * s + y * c) for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2),
                                                             (-w / 2, h / 2))]
    elif kind < 0.8:
        n, base = r.randint(5, 14), r.uniform(4, 25)
        pts = []
        for k in range(n):
            t, q = 2 * math.pi * k / n, base * r.uniform(0.6, 1.0)
            pts.append((q * dmath.cos(t), q * dmath.sin(t)))
    else:
        a, b = r.uniform(6, 30), r.uniform(6, 30)
        pts = [(0, 0), (a, 0), (a, b / 2), (a / 2, b / 2), (a / 2, b), (0, b)]
    ox, oy = r.uniform(-120, 120), r.uniform(-120, 120)
    return np.array([(x + ox, y + oy) for x, y in pts], np.float64)


def _spec(r):
    ring = _ring(r)
    return bld.Spec(ring=ring, roof=r.choice(ROOFS), roof_mat=bld.ROOF_TILES, facade=bld.FACADE, cls=r.choice(
        ["house", "detached", "barn", "shed", "commercial", "retail", "apartments"]), seed=r.random(),
        floors=r.choice([None, 1, 2, 3, 4, 7]), floor_h=3.0, height=r.uniform(3, 30), min_h=r.choice([None, 0.0, 2.0]),
        across=r.random() < 0.3, roof_h=r.choice([None, 0.0, r.uniform(0.3, 5)]), hi=r.choice([None, 1.0, -1.0]),
        pitch=r.choice([None, r.uniform(10, 55)]), gring=[r.uniform(-2, 3) for _ in ring] if r.random() < 0.5 else None,
        plain=r.random() < 0.2, tank=None)


def digest(b):
    """The SHA-256 of every piece's arrays (b: a buildings module) over 400 seeded specs."""
    r = random.Random(519)
    h = hashlib.sha256()
    for _ in range(400):
        sp = _spec(r)
        looks = b._Looks(sp.seed)
        lite = r.random() < 0.3
        m = b.Mesh()
        ring = b.drawn_ring(sp)
        sp.stoop, sp.sign = (r.randrange(len(ring)), r.uniform(0, 8)), r.randrange(len(ring))   # (on the drawn walls)
        z = r.uniform(3, 40)
        b._roof(m, sp, ring, z, looks, lite)
        if sp.roof == "flat":
            b._cornice(m, sp, ring, z, looks)
            b._clutter(m, sp, ring, z, looks)
        b._stoop(m, sp, ring, z - 9.0, z - 8.0, looks)
        b._signs(m, sp, ring, z, looks)
        b._box(m, ring[0], np.array([0.6, 0.8]), 1.5, 0.7, z, z + 2.0, looks["bne_bld_fac_grey"])
        b._box_tiled(m, ring[1], np.array([-0.8, 0.6]), 1.1, 0.4, z - 1.0, z + 1.0, looks[b.MARBLE_LOOK], 2.4)
        m.poly([(p[0], p[1], z) for p in ring[::-1]], [(0.002, 0.995)] * len(ring), looks[sp.facade])
        q = r.randint(1, 4)
        m.quads(np.arange(12 * q, dtype=np.float64) * 0.37, np.arange(8 * q, dtype=np.float64) * 0.11,
                np.array([r.choice([0, 2]) for _ in range(q)]), [looks[sp.facade], None, looks["bne_bld_fac_white"]])
        m.tris(np.arange(18, dtype=np.float64).reshape(-1, 3), np.zeros((6, 2)), looks[sp.roof_mat])
        h.update(np.int64(m.n).tobytes())
        for a in m.arrays():
            h.update(str((a.dtype, a.shape)).encode())
            h.update(np.ascontiguousarray(a).tobytes())
    return h.hexdigest()


# try598 (627722d5)'s Python buildings.py on this digest()
DIGEST = "09454b3b7c9438ea061efd7037226bc2ff3d3b51ef3cfda795afaefa22ccf82d"


class BldMeshTest(unittest.TestCase):
    def test_pieces_match_try598(self):
        self.assertEqual(digest(bld), DIGEST)

    def test_mesh_ops(self):
        m = bld.Mesh()
        tok = (3, (10, 20, 30, 255))
        m.quad((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0), (1, 0), (1, 1), (0, 1), tok)
        m.tri((0, 0, 1), (1, 0, 1), (0, 1, 1), (0, 0), (1, 0), (0, 1), tok)
        m.poly([(0, 0, 2), (0, 2, 2), (2, 2, 2), (2, 0, 2)], [(0, 0)] * 4, tok)     # clockwise: its underside
        self.assertEqual(m.n, 11)
        self.assertEqual(m.T, [0, 1, 2, 0, 2, 3, 4, 5, 6, 9, 10, 7, 7, 8, 9])
        self.assertEqual(m.M, [3] * 5)
        self.assertEqual(m.C[:4], [10, 20, 30, 255])
        P, UV, T, M, C = m.arrays()
        self.assertEqual((P.shape, UV.shape, T.shape, M.shape, C.shape, C.dtype),
                         ((11, 3), (11, 2), (5, 3), (5,), (11, 4), np.uint8))
        with self.assertRaises(TypeError):
            m.quads(np.zeros(12), np.zeros(8), np.array([1]), [tok, None])
        # a clockwise float32 square far out: its area in float32 (numpy's sums, as the Python took it) is 0, so it is
        # not turned and the ear clipping finds no triangle (Codex r2a19 round 2); ragged rows are refused as numpy did
        m2 = bld.Mesh()
        sq = np.array([(4096, 4096, 0), (4096, 4097, 0), (4097, 4097, 0), (4097, 4096, 0)], np.float32)
        m2.poly(sq, [(0, 0)] * 4, tok)
        self.assertEqual((m2.n, m2.T, m2.P[:3]), (4, [], [4096.0, 4096.0, 0.0]))
        with self.assertRaises(ValueError):
            m2.tris([[0, 0, 0], [1, 1, 1, 2, 2, 2]], np.zeros((3, 2)), tok)
        self.assertEqual(m2.n, 4)


if __name__ == "__main__":
    unittest.main()
