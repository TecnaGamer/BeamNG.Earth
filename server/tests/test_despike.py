"""heightmap.despike / despiked: a sample one sample thick (a post, a wall one sample wide) takes its neighbours' height;
a cliff, a slope and a wall two samples wide stay; a block built whole, through chunks, or next to another gives every
sample the same height."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import geodesy  # noqa: E402
import heightmap  # noqa: E402


def _grid(n=7, z=10.0):
    return np.full((n + 2, n + 2), z)


class DespikeTest(unittest.TestCase):
    def test_a_post_and_a_pit_go(self):
        z = _grid()
        z[4, 4] = 16.0
        z[2, 6] = 3.0
        out = heightmap.despike(z)
        self.assertEqual(out.shape, (7, 7))
        self.assertTrue(np.allclose(out, 10.0))

    def test_a_wall_one_sample_wide_goes(self):
        z = _grid()
        z[:, 5] = 15.0          # (a wall along the rows' fifth column, one sample wide)
        out = heightmap.despike(z)
        self.assertTrue(np.allclose(out, 10.0))

    def test_a_wall_two_samples_wide_a_cliff_and_a_slope_stay(self):
        z = _grid()
        z[:, 4:6] = 15.0
        self.assertTrue(np.array_equal(heightmap.despike(z), z[1:-1, 1:-1]))
        z = _grid()
        z[:, 5:] = 20.0         # (a cliff: the top's edge has the top beside it)
        self.assertTrue(np.array_equal(heightmap.despike(z), z[1:-1, 1:-1]))
        z = np.add.outer(np.arange(9.0), np.arange(9.0)) * 3.0     # (a 3 m-a-sample slope)
        self.assertTrue(np.array_equal(heightmap.despike(z), z[1:-1, 1:-1]))

    def test_under_the_threshold_stays(self):
        z = _grid()
        z[4, 4] = 10.0 + heightmap.SPIKE_M * 0.9
        self.assertTrue(np.array_equal(heightmap.despike(z), z[1:-1, 1:-1]))


def _spiky(lat, lon):
    """A smooth DEM with a post every so often (elementwise)."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    base = 50.0 + 30.0 * np.sin(lat * 900.0) * np.cos(lon * 700.0)
    post = (np.round(lat * 1e5).astype(np.int64) % 37 == 0) & (np.round(lon * 1e5).astype(np.int64) % 41 == 0)
    return base + np.where(post, 8.0, 0.0)


class DespikedBlockTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(39.2834, -76.5934)

    def test_whole_chunked_and_moved_blocks_agree(self):
        fn = heightmap.despiked(self.frame, _spiky)
        whole = heightmap.grid_u(self.frame, fn, 0.0, 0.0, 256)
        cache = heightmap.ChunkCache()
        chunked, _ = heightmap.grid_u_chunked(self.frame, fn, 0.0, 0.0, 256, cache)
        self.assertTrue(np.array_equal(whole, chunked))
        # (the block 128 m east: its west half is this one's east half, sample for sample)
        moved = heightmap.grid_u(self.frame, fn, 128.0, 0.0, 256)
        k = int(round(128.0 / heightmap.SQUARE))
        self.assertTrue(np.array_equal(whole[:, k:], moved[:, :-k]))

    def test_the_posts_are_gone(self):
        raw = heightmap.grid_u(self.frame, _spiky, 0.0, 0.0, 256)
        got = heightmap.grid_u(self.frame, heightmap.despiked(self.frame, _spiky), 0.0, 0.0, 256)
        inner = (slice(1, -1), slice(1, -1))
        self.assertGreater(np.abs(raw - got)[inner].max(), 5.0)
        lap = np.abs(got[1:-1, 1:-1] - (got[:-2, 1:-1] + got[2:, 1:-1]) / 2)
        self.assertLess(lap.max(), heightmap.SPIKE_M)


if __name__ == "__main__":
    unittest.main()
