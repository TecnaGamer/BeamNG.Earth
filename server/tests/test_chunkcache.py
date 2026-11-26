"""heightmap.ChunkCache / grid_u_chunked: a block built through the cache equals one built from scratch,
and a moved block only computes its new chunks."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import geodesy  # noqa: E402
import geotiles  # noqa: E402
import heightmap  # noqa: E402


def elev(lat, lon):
    """A smooth synthetic DEM, elementwise."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    return 50.0 + 30.0 * np.sin(lat * 900.0) * np.cos(lon * 700.0) + 1000.0 * (lat - 39.28)


class ChunkCacheTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(39.2834, -76.5934)
        self.calls = []

        def counted(la, lo):
            self.calls.append(np.size(la))
            return elev(la, lo)
        self.elev = counted

    def test_equal_to_full_build(self):
        cache = heightmap.ChunkCache()
        for c in [(0.0, 0.0), (0.0, 128.0), (128.0, 256.0), (-384.0, 1024.0)]:
            got, _ = heightmap.grid_u_chunked(self.frame, self.elev, *c, 1024, cache)
            want = heightmap.grid_u(self.frame, elev, *c, 1024)
            self.assertTrue(np.array_equal(got, want), c)

    def test_moved_block_computes_new_chunks_only(self):
        cache = heightmap.ChunkCache()
        _, share = heightmap.grid_u_chunked(self.frame, self.elev, 0.0, 0.0, 1024, cache)
        self.assertEqual(share, 1.0)
        self.calls.clear()
        _, share = heightmap.grid_u_chunked(self.frame, self.elev, 0.0, 128.0, 1024, cache)
        self.assertEqual(share, 1 / 8)                  # one new row of 8 chunks (128 samples = 128 m each)
        self.assertEqual(sum(self.calls), 1024 * heightmap.CHUNK)
        self.calls.clear()
        _, share = heightmap.grid_u_chunked(self.frame, self.elev, 0.0, 128.0, 1024, cache)
        self.assertEqual((share, self.calls), (0.0, []))

    def test_lru_cap(self):
        cache = heightmap.ChunkCache(cap=10)
        heightmap.grid_u_chunked(self.frame, self.elev, 0.0, 0.0, 1024, cache)
        self.assertEqual(len(cache.chunks), 10)

    def test_world_frame_samples_the_ellipsoid_lattice(self):
        frame = geotiles.WorldFrame(39.2834, -76.5934)
        cache = heightmap.ChunkCache()
        size = 2 * heightmap.CHUNK
        for cx in (65536, 65536 + heightmap.CHUNK * heightmap.SQUARE):
            positions = {}
            heightmap.grid_u_chunked(frame, elev, cx, 0, size, cache, positions)
            east, north, _ = frame.geodetic_to_enu(positions["lat"], positions["lon"], 0.)
            xs, ys = heightmap.sample_coords(cx, 0, size)
            self.assertLess(np.max(np.abs(east - xs)), 1e-5)
            self.assertLess(np.max(np.abs(north - ys[:, None])), 1e-5)

    def test_keep_matches_unchunked(self):
        k1, k2 = {}, {}
        heightmap.grid_u_chunked(self.frame, elev, 0.0, 0.0, 1024, heightmap.ChunkCache(), k1)
        heightmap.grid_u(self.frame, elev, 0.0, 0.0, 1024, k2)
        self.assertTrue(np.array_equal(k1["lat"], k2["lat"]) and np.array_equal(k1["lon"], k2["lon"]))


if __name__ == "__main__":
    unittest.main()
