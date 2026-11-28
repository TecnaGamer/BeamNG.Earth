"""Far tiles across a drawn terrain block's edge never stand above it (the user, 2026-11-21: "im driving through the old
lod terrain sometimes with the solid ground being under it"): farfield.cover_sink, the exact sink of a tile against the
block's own samples, and Connection.far_cover, the terrain message's `cover` table (farfield.lua 'partial')."""
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import farfield  # noqa: E402


def tri_surface(g, sp, x, y, diag):
    """A cell-split triangulated surface of grid g [j, i] at tile-local (x, y); diag: 'sw' (SW-NE everywhere), 'se'."""
    n = g.shape[0] - 1
    u, v = x / sp, y / sp
    i, j = np.clip(np.floor(u).astype(int), 0, n - 1), np.clip(np.floor(v).astype(int), 0, n - 1)
    fu, fv = u - i, v - j
    z00, z10, z01, z11 = g[j, i], g[j, i + 1], g[j + 1, i], g[j + 1, i + 1]
    if diag == "sw":     # triangles (00, 10, 11) and (00, 11, 01)
        lo = fu >= fv
        return np.where(lo, z00 + (z10 - z00) * fu + (z11 - z10) * fv, z00 + (z11 - z01) * fu + (z01 - z00) * fv)
    hi = fu + fv <= 1    # (00, 10, 01) and (10, 11, 01)
    return np.where(hi, z00 + (z10 - z00) * fu + (z01 - z00) * fv,
                    z11 + (z11 - z01) * (fu - 1) + (z11 - z10) * (fv - 1))


class CoverSinkTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        # an L0 tile (256 m, 32 cells: 8 m vertices) of rolling ground, half over a 1 m block whose west edge is x = 128
        self.n, self.S = 32, 256.0
        xs = np.arange(self.n + 1) * (self.S / self.n)
        X, Y = np.meshgrid(xs, xs)
        self.grid = (40 + 6 * np.sin(X / 37) + 4 * np.cos(Y / 23) + rng.normal(0, 0.8, X.shape)).astype(np.float32)
        self.bx0, self.by0, self.N = 128.0, -200.0, 512
        gx = self.bx0 + np.arange(self.N)
        gy = self.by0 + np.arange(self.N)
        GX, GY = np.meshgrid(gx, gy)
        h = 40 + 6 * np.sin(GX / 37) + 4 * np.cos(GY / 23)
        # a road cut 9 m deep and 12 m wide across the overlap, the kind of ground a coarse tile's sink did not know
        h = np.where(np.abs(GY - 60) < 6, h - 9, h)
        self.h = h

    def test_tile_under_every_sample_after_the_sink(self):
        s = farfield.cover_sink(self.grid, (0.0, 0.0, self.S, self.S), self.h, self.bx0, self.by0, 1.0)
        self.assertGreater(s, 9.0)
        g = self.grid.astype(np.float64)
        sp = self.S / self.n
        # every block sample in the overlap, both diagonal patterns: the sunk tile is under the block by the margin
        gx = np.arange(0, int(self.S - self.bx0) + 1)
        gy = np.arange(int(-self.by0), int(self.S - self.by0) + 1)
        GX, GY = np.meshgrid(gx, gy)
        x, y = self.bx0 + GX, self.by0 + GY          # (tile-local: the tile's SW corner is (0, 0))
        for diag in ("sw", "se"):
            t = tri_surface(g, sp, x, y, diag)
            gap = (self.h[GY, GX]) - (t - s)
            self.assertGreaterEqual(float(gap.min()), farfield.COVER_MARGIN - 1e-9, diag)
        # and not much deeper than needed: within the cells' diagonal bound of the true worst point
        t = np.maximum(tri_surface(g, sp, x, y, "sw"), tri_surface(g, sp, x, y, "se"))
        need = float((t - self.h[GY, GX]).max()) + farfield.COVER_MARGIN
        self.assertLess(s - need, 2.0)

    def test_no_overlap(self):
        self.assertIsNone(farfield.cover_sink(self.grid, (-1000.0, 0.0, -744.0, 256.0), self.h, self.bx0, self.by0, 1.0))


class FarCoverTest(unittest.TestCase):
    """Connection.far_cover against a .ter on disk: the partial tiles' sinks, nothing for tiles inside or outside the
    block, nothing in mesh mode or for another anchor."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        N = 1024
        q = np.full((N, N), 4000, np.uint16)          # rows south first, as heightmap.write_ter
        q[600:612, :] = 3000                          # a cut 1000 * 1/64 = 15.6 m deep across the block, y in [88, 100)
        with open(os.path.join(self.dir, "hm.ter"), "wb") as f:
            f.write(struct.pack("<BI", 9, N))
            f.write(q.tobytes())
            f.write(bytes(N * N))
            f.write(struct.pack("<I", 0))
        self.info = {"ter": "hm.ter", "heightmap": "hm.png", "zBase": 0.0, "maxHeight": 1024.0}   # q = 1/64 m
        self.block_z = 4000 / 64                      # 62.5 m
        s = SimpleNamespace(near_visual="detail", layout=SimpleNamespace(block=N), out_dir=self.dir, tiles={},
                            far_ck=lambda aid, k: (aid, k))
        self.c = earthd.Connection.__new__(earthd.Connection)
        self.c.s, self.c.active = s, 1
        # the block centred at (0, 0): [-512, 512]^2. An L2 tile [0, 1024]^2 across its NE corner, flat 1 m above the block
        # (the cut runs under it), an L-1 tile inside, an L0 tile outside
        self.sent = {}
        for level, i, j, z in ((2, 0, 0, self.block_z + 1.0), (-1, 0, 0, self.block_z + 5.0), (0, 3, 0, self.block_z + 9)):
            t = farfield.Tile(level, i, j)
            self.sent[t.key] = t
            s.tiles[(1, t.key)] = SimpleNamespace(grid=np.full((33, 33), z, np.float32))
        p = mock.patch.object(earthd.Connection, "sent", new=property(lambda c: self.sent))
        p.start()
        self.addCleanup(p.stop)

    def test_partial_tiles_only(self):
        cover = self.c.far_cover((1, 0.0, 0.0), self.info)
        self.assertEqual(set(cover), {"2_0_0"})
        # its top 1 m over the block, 15.625 m more over the cut: sunk under both by the margin
        self.assertAlmostEqual(cover["2_0_0"], 1.0 + 15.625 + farfield.COVER_MARGIN, delta=0.011)

    def test_nothing_in_mesh_mode_or_for_another_anchor(self):
        self.assertEqual(self.c.far_cover((2, 0.0, 0.0), self.info), {})
        self.c.s.near_visual = "mesh"
        self.assertEqual(self.c.far_cover((1, 0.0, 0.0), self.info), {})

    def test_missing_heights_give_nothing(self):
        self.assertEqual(self.c.far_cover((1, 0.0, 0.0), {"heightmap": "none.png", "zBase": 0, "maxHeight": 1}), {})


if __name__ == "__main__":
    unittest.main()
