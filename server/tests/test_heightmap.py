import math
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem as demmod  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy as g  # noqa: E402
import heightmap  # noqa: E402

LAT0, LON0 = 36.5785, -118.2923


def rough(lat, lon):
    """Synthetic rough DEM: metre-scale bumps at ~10 m wavelength on a 600 m relief."""
    lat, lon = np.asarray(lat), np.asarray(lon)
    return (1500 + 300 * np.sin(lat * 90) * np.cos(lon * 70)
            + 3.0 * np.sin(lat * 61000) * np.cos(lon * 47000) + 1.3 * np.sin(lon * 151000))


class GeometryTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(heightmap.SIZE, 1024)
        self.assertEqual(heightmap.SQUARE, 1.0)
        self.assertEqual(heightmap.FAR_SQUARE, 2.0)         # the far field's lattice: every second sample
        self.assertEqual(heightmap.HALF, 512.0)
        self.assertEqual(heightmap.VALID, 1023.0)           # (N-1)*s, probe P1
        self.assertEqual(heightmap.GRID / heightmap.SQUARE, 128)

    def test_sample_positions_on_lattice(self):
        for cx, cy in ((0.0, 0.0), (256.0, -768.0), (10240.0, 5376.0), (-24832.0, 24576.0)):
            xs, ys = heightmap.sample_coords(cx, cy)
            self.assertEqual(len(xs), 1024)
            # Block position (SW corner) = (cx - 512, cy - 512); sample gx at x0 + 1 gx, exactly.
            self.assertEqual(xs[0], cx - 512)
            self.assertEqual(ys[0], cy - 512)
            self.assertTrue((xs == cx - 512 + 1.0 * np.arange(1024)).all())
            self.assertTrue((xs % 1 == 0).all())
            self.assertEqual(xs[1023], cx + 511)
        # Adjacent blocks share their samples bit for bit.
        a, _ = heightmap.sample_coords(0.0, 0.0)
        b, _ = heightmap.sample_coords(128.0, 0.0)
        self.assertTrue((a[128:] == b[:896]).all())
        with self.assertRaises(ValueError):
            heightmap.sample_coords(0.5, 0.0)

    def test_block_size_parameter(self):
        """The block size is a parameter (the server passes its Layout's block, docs/V1_4_SPEC.md
        §3.1), not a module setting: a 2048² block (2 km) sits on the same lattice, centred the same way."""
        self.assertEqual(heightmap.half_extent(), heightmap.HALF)
        self.assertEqual(heightmap.half_extent(2048), 1024.0)
        big, bys = heightmap.sample_coords(256.0, -768.0, 2048)
        self.assertEqual((len(big), big[0], bys[0], big[-1]), (2048, 256.0 - 1024, -768.0 - 1024, 256.0 + 1023))
        xs, _ = heightmap.sample_coords(256.0, -768.0)
        self.assertTrue((big[512:1536] == xs).all())                   # the same lattice points
        # height_at takes the block size from the grid: a 4² grid centred at (0, 0) spans [-2, 2)
        q = np.arange(16, dtype=np.uint16).reshape(4, 4)
        self.assertEqual(heightmap.height_at(q, 0.0, 65536.0, 0.0, 0.0, -2.0, -2.0), 0.0)
        self.assertEqual(heightmap.height_at(q, 0.0, 65536.0, 0.0, 0.0, -1.0, 0.0), 9.0)
        self.assertIsNone(heightmap.height_at(q, 0.0, 65536.0, 0.0, 0.0, 1.5, 0.0))

    def test_grids(self):
        """The centre grid by the block's width (1 km: 128 m, 2 km: 256 m); snap takes it."""
        self.assertEqual((heightmap.grid(), heightmap.grid(1024), heightmap.grid(2048)), (128.0, 128.0, 256.0))
        self.assertEqual((heightmap.valid(), heightmap.valid(2048)), (1023.0, 2047.0))
        self.assertEqual((heightmap.snap(127.9, 256.0), heightmap.snap(128.0, 256.0), heightmap.snap(-128.1, 256.0)),
                         (0.0, 256.0, -256.0))

    def test_snap(self):
        self.assertEqual(heightmap.snap(63.9), 0.0)
        self.assertEqual(heightmap.snap(64.0), 128.0)
        self.assertEqual(heightmap.snap(-64.1), -128.0)
        self.assertEqual(heightmap.snap(-63.0), 0.0)
        self.assertEqual(str(heightmap.snap(-1.0)), "0.0")    # no -0.0 in file names / JSON
        self.assertEqual(heightmap.snap(5000.0), 4992.0)

    def test_choose_q_and_block_params(self):
        self.assertEqual(heightmap.choose_q(50), 1 / 256)
        self.assertEqual(heightmap.choose_q(600), 1 / 64)       # 1024 m range
        self.assertEqual(heightmap.choose_q(3000), 1 / 16)
        self.assertEqual(heightmap.choose_q(1e6), 1 / 16)
        q, zb, mh = heightmap.block_params(1234.5, 1500.0, 1 / 64)
        self.assertEqual((q, zb, mh), (1 / 64, 1216.0, 1024.0))
        self.assertEqual(zb % 64, 0)
        # Too much relief for the anchor's quantum: the next coarser one, for this block only.
        q, zb, mh = heightmap.block_params(0.0, 1500.0, 1 / 64)
        self.assertEqual((q, mh), (1 / 32, 2048.0))
        self.assertEqual(heightmap.block_params(0.0, 1e5, 1 / 64)[0], 1 / 16)


class HeightmapTest(unittest.TestCase):
    """The block as the server writes it, parametrised over the layouts' block sizes
    (docs/V1_4_SPEC.md §3.5, E11: (32 cells, 1024² = 1 km) here, (64 cells, 2048² = 2 km) in Heightmap2kmTest):
    a move of the size's grid shares bit-identical samples, the q / zBase rules are the same."""
    layout = ff.Layout(32, 1024)
    ORIENT_C = (384.0, -256.0)          # a block centre with (0, 0) inside it

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "hm.png")
        self.frame = g.EnuFrame(LAT0, LON0)
        self.n = self.layout.block
        self.half = heightmap.half_extent(self.n)
        self.grid = self.layout.grid

    def tearDown(self):
        self.tmp.cleanup()

    def generate(self, elev, path, cx=0.0, cy=0.0, **kw):
        return heightmap.generate(self.frame, elev, path, cx, cy, size=self.n, **kw)

    def read(self, path=None):
        with Image.open(path or self.path) as im:
            self.assertEqual(im.size, (self.n, self.n))
            self.assertIn(im.mode, ("I;16", "I"))
            return np.asarray(im).astype(np.int64)[::-1]       # back to [gy, gx], gy = north

    def test_grid_and_parity(self):
        """Centres on the size's grid (128 / 256 m); the SW corner is then an even lattice index, so
        the block-local Torque diagonal parity is the world lattice's (farfield.torque_split45)."""
        self.assertEqual(self.grid, {1024: 128.0, 2048: 256.0}[self.n])
        self.assertEqual(self.grid / heightmap.SQUARE % 2, 0)
        for k in range(-7, 8):
            c = k * self.grid
            i0 = heightmap.lattice_index(c, self.n)
            self.assertEqual(i0 % 2, 0, c)
            gx = np.arange(5)
            self.assertTrue((ff.torque_split45(gx, 3) == ff.torque_split45(i0 + gx, i0 + 3)).all())
        self.assertEqual(heightmap.snap(self.grid / 2 - 0.1, self.grid), 0.0)
        self.assertEqual(heightmap.snap(self.grid / 2, self.grid), self.grid)

    def test_orientation_and_sample_positions(self):
        # Ground rising to the north-east: north edge high, east edge high.
        elev = lambda lat, lon: 1000 + (lat - LAT0) * 20000 + (lon - LON0) * 5000
        cx, cy = self.ORIENT_C
        n, half = self.n, self.half
        info = self.generate(elev, self.path, cx, cy)
        with Image.open(self.path) as im:
            png = np.asarray(im).astype(np.float64)
        self.assertGreater(png[0].mean(), png[-1].mean())        # row 0 = north
        self.assertGreater(png[:, -1].mean(), png[:, 0].mean())  # column 0 = west
        self.assertEqual((info["cx"], info["cy"]), (cx, cy))
        self.assertEqual(info["maxHeight"], info["q"] * 65536)
        self.assertEqual(info["zBase"] % 64, 0)
        self.assertLessEqual(info["zBase"], info["minU"] - 8)
        q = self.read()
        h = heightmap.heights(q, info["zBase"], info["maxHeight"])
        # Sample (gx, gy) at (cx-half+s gx, cy-half+s gy); the last row/column is real data.
        for gy, gx in ((100, n - 124), (0, 0), (n - 1, n - 1), (n - 1, 0), (0, n - 1), (n // 2 - 1, n // 2 - 1)):
            x, y = cx - half + heightmap.SQUARE * gx, cy - half + heightmap.SQUARE * gy
            _, _, _, _, _, u = self.frame.surface_enu(np.float64(x), np.float64(y), elev)
            self.assertAlmostEqual(h[gy, gx], float(u), delta=info["q"] / 2 + 1e-9)
        self.assertFalse((q[:, n - 1] == q[:, n - 2]).all())
        self.assertFalse((q[n - 1] == q[n - 2]).all())
        # (0, 0) is inside this block: groundZ is the terrain height there.
        _, _, _, _, _, u0 = self.frame.surface_enu(np.float64(0), np.float64(0), elev)
        self.assertAlmostEqual(info["groundZ"], float(u0), delta=0.01)

    def test_overlapping_blocks_identical(self):
        """§1.8: adjacent blocks' overlapping samples are bit-identical (same stored height and,
        with the same zBase, the same uint16), on rough ground, for moves on the size's grid."""
        n, G = self.n, self.grid
        base = self.generate(rough, self.path, 0.0, 0.0)
        q0 = self.read()
        h0 = heightmap.heights(q0, base["zBase"], base["maxHeight"])
        for dx, dy in ((G, 0.0), (0.0, G), (G, G), (-2 * G, 3 * G)):
            with self.subTest(dx=dx, dy=dy):
                p = os.path.join(self.tmp.name, f"hm_{dx}_{dy}.png")
                info = self.generate(rough, p, dx, dy)
                q1 = self.read(p)
                h1 = heightmap.heights(q1, info["zBase"], info["maxHeight"])
                sx, sy = int(dx // heightmap.SQUARE), int(dy // heightmap.SQUARE)
                a = (slice(max(0, sy), n + min(0, sy)), slice(max(0, sx), n + min(0, sx)))
                b = (slice(max(0, -sy), n - max(0, sy)), slice(max(0, -sx), n - max(0, sx)))
                self.assertEqual(info["q"], base["q"])
                self.assertGreater(h0[a].size, 0)
                self.assertTrue((h0[a] == h1[b]).all())
                if info["zBase"] == base["zBase"]:
                    self.assertTrue((q0[a] == q1[b]).all())
                else:
                    self.assertTrue((q0[a] - q1[b] == (info["zBase"] - base["zBase"]) / info["q"]).all())
        # Stored heights are Q * round(u / Q) for any zBase.
        u = heightmap.grid_u(self.frame, rough, 0.0, 0.0, self.n)
        self.assertTrue((h0 == heightmap.quantise(u, base["q"]) * base["q"]).all())

    def test_overlap_identical_through_dem_mosaic(self):
        """The same, through dem.Dem.sample (tile mosaic + bilinear) on synthetic z15 tiles."""
        class SynthDem(demmod.Dem):
            def _load_tile(self, key):
                rng = np.random.default_rng(abs(hash(key)) % 2 ** 32)
                return (2000 + rng.normal(0, 3, (demmod.TILE, demmod.TILE))).astype(np.float32)
        n, G = self.n, self.grid
        with tempfile.TemporaryDirectory() as cache:
            dem = SynthDem(cache, fetch_workers=2)
            fn = lambda la, lo: dem.sample(la, lo, 15)      # noqa: E731
            a = self.generate(fn, self.path, 0.0, 0.0)
            h0 = heightmap.heights(self.read(), a["zBase"], a["maxHeight"])
            p = os.path.join(self.tmp.name, "b.png")
            b = self.generate(fn, p, G, -3 * G)
            h1 = heightmap.heights(self.read(p), b["zBase"], b["maxHeight"])
            dem.pool.shutdown()
        sx, sy = int(G // heightmap.SQUARE), int(3 * G // heightmap.SQUARE)    # new (gx, gy) = old (gx + sx, gy - sy)
        self.assertTrue((h0[:n - sy, sx:] == h1[sy:, :n - sx]).all())

    def test_ground_outside_block(self):
        info = self.generate(lambda lat, lon: np.zeros_like(lat), self.path, 2 * self.half, 0.0)
        self.assertIsNone(info["groundZ"])

    def test_valid_area(self):
        info = self.generate(lambda lat, lon: np.zeros_like(lat), self.path, 0.0, 0.0)
        q = self.read()
        h, v = self.half, heightmap.valid(self.n)
        args = (q, info["zBase"], info["maxHeight"], 0.0, 0.0)
        self.assertIsNotNone(heightmap.height_at(*args, -h, -h))
        self.assertIsNotNone(heightmap.height_at(*args, v - h, v - h))       # sample n-1: still collidable
        self.assertIsNone(heightmap.height_at(*args, v - h + heightmap.SQUARE / 2, 0.0))     # the last N/E strip
        self.assertIsNone(heightmap.height_at(*args, 0.0, -h - 0.5))

    def test_quantisation_and_curvature(self):
        info = self.generate(lambda lat, lon: np.zeros_like(lat), self.path)
        q = self.read()
        # Flat ellipsoid: centre is highest; corners (0.72 km out for the 1 km block) drop d² / 2R.
        self.assertEqual(info["q"], heightmap.Q_DEFAULT)
        self.assertEqual(info["zBase"], math.floor((info["minU"] - 8) / 64) * 64)
        self.assertAlmostEqual(info["groundZ"], 0.0, delta=0.01)
        corner = heightmap.heights(q, info["zBase"], info["maxHeight"])[0, 0]
        self.assertAlmostEqual(corner, -(self.half ** 2 * 2) / (2 * 6371000), delta=0.02)

    def test_ter_file(self):
        """write_ter: the PNG's samples, south row first (the engine's sample (x, y) = block-local
        (x, y) * squareSize), the material indices the same way, then the names."""
        keep = {}
        self.generate(rough, self.path, keep=keep)
        idx = (np.arange(self.n * self.n) % 3).reshape(self.n, self.n).astype(np.uint8)
        ter = os.path.join(self.tmp.name, "hm.ter")
        heightmap.write_ter(ter, keep["qgrid"], idx, ["a", "bb", "ccc"])
        data = open(ter, "rb").read()
        n2 = self.n * self.n
        self.assertEqual(struct.unpack_from("<BI", data), (9, self.n))
        h = np.frombuffer(data, "<u2", n2, 5).reshape(self.n, self.n)
        np.testing.assert_array_equal(h, self.read())
        lay = np.frombuffer(data, np.uint8, n2, 5 + 2 * n2).reshape(self.n, self.n)
        np.testing.assert_array_equal(lay[1:-1, 1:-1], idx[1:-1, 1:-1])
        for edge in (lay[0], lay[-1], lay[:, 0], lay[:, -1]):        # the edge samples are holes
            self.assertTrue((edge == heightmap.TER_HOLE).all())
        o = 5 + 3 * n2
        self.assertEqual(struct.unpack_from("<I", data, o), (3,))
        self.assertEqual(data[o + 4:], b"\x01a\x02bb\x03ccc")


class Heightmap2kmTest(HeightmapTest):
    """The 2 km block (2048² at 1 m, mesh mode's --block 2048) of the (64 cells, 2048) layout, on a 256 m grid."""
    layout = ff.Layout(64, 2048)
    ORIENT_C = (768.0, -256.0)


if __name__ == "__main__":
    unittest.main()
