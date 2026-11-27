"""Hourly wind forcing and seam-free terrain shelter for seasonal snow."""

import unittest
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402
import geodesy  # noqa: E402
import snowcover  # noqa: E402
import snowwind  # noqa: E402
from test_snowcover import winter  # noqa: E402


def ridge(lat, lon, zoom):
    return 100 * np.exp(-(lon / 0.004) ** 2)


def field(vector=(104, 0)):
    # One weather cell centred at (0.05 N, 0.05 E), with its upwind neighbour.
    return (0.1, 1799, 899, np.tile(np.asarray(vector, dtype=np.int8), (1, 2, 1)))


class ForcingTest(unittest.TestCase):
    def test_calm_warm_and_future_hours(self):
        times = np.arange(96) * 3600
        cold = np.full(96, -500)
        wind = np.full(96, 150)
        west = np.full(96, 270).tolist()
        vectors = snowwind.transport(times, west, wind, cold, 71)
        self.assertGreater(vectors[0], 65)
        self.assertEqual(vectors[1], 0)
        north = snowwind.transport(times, [180] * 96, wind, cold, 71)
        self.assertEqual(north, (0, vectors[0]))
        warm = snowwind.transport(times, west, wind, np.full(96, 200), 71)
        self.assertLess(warm[0], vectors[0])
        wind[:72] = 50
        self.assertEqual(snowwind.transport(times, west, wind, cold, 71), (0, 0))
        self.assertEqual(snowwind.transport(times, [], wind, cold, 95), (0, 0))
        self.assertEqual(snowwind.transport(times, [None] * 96, wind, cold, 95), (0, 0))
        self.assertGreater(snowwind.transport(times, west, wind, cold, 95)[0], 0)

    def test_flat_terrain_uses_no_correction(self):
        calls = []

        def flat(lat, lon, zoom):
            calls.append((lat.shape[0], zoom))
            return np.full(lat.shape, 192.0)

        self.assertIsNone(snowwind.shelter_grid(field(), flat, 0.046, -0.014, 0.054, 0.014))
        self.assertEqual(calls, [(1 + len(snowwind.FETCH_M), snowwind.DEM_ZOOM)])

    def test_shelter_reverses_with_wind(self):
        a = snowwind.shelter_grid(field(), ridge, 0.049, -0.007, 0.051, 0.007)[0]
        b = snowwind.shelter_grid(field((-104, 0)), ridge, 0.049, -0.007, 0.051, 0.007)[0]
        self.assertLess(a[:, :a.shape[1] // 2].min(), snowwind.VECTOR_SCALE)
        self.assertGreater(a[:, a.shape[1] // 2:].max(), snowwind.VECTOR_SCALE)
        np.testing.assert_array_equal(a, b[:, ::-1])


@unittest.skipUnless(fast._ef is not None and fast._ef.VERSION >= 639, "needs snow shelter in earthfast 639")
class NativeWindTest(unittest.TestCase):
    def test_shelter_shared_by_texture_and_physics_and_kept_off_pavement(self):
        with winter("real") as sc:
            table = sc.Table(((0, 4, 3, 0),), (8,) * 15, 8)
            cells = {(ci, 899): table for ci in (1799, 1800)}
            winds = {k: (104, 0) for k in cells}
            frame = geodesy.EnuFrame(0.05, 0.0)
            dem = SimpleNamespace(sample=ridge)
            rgb = np.full((64, 64, 3), (92, 113, 64), np.uint8)
            road = np.zeros((64, 64), np.uint8)
            road[:8] = np.resize(np.arange(1, 16, dtype=np.uint8), road.shape[1])
            water = np.zeros_like(road)
            water[-8:] = sc.WATER_STILL

            def look(x0, y0, size, n, road=None, water=None):
                spec = sc.spec_for_square(frame, x0, y0, size)
                heights = np.full((n, n), 100, np.float64)
                return spec.sig, sc.look(spec, frame, x0, y0, size, heights, x0, y0, size / (n - 1),
                                         road, water, dem=dem)

            sc.set_provider(lambda *box: sc.Field(0.1, cells=cells))
            calm_sig, calm = look(-500, -500, 1000, 65, road, water)
            sc.set_provider(lambda *box: sc.Field(0.1, cells=cells, winds=winds))
            windy_sig, windy = look(-500, -500, 1000, 65, road, water)
            self.assertNotEqual(calm_sig, windy_sig)
            _, far = look(-500, -500, 2000, 33)
            _, left = look(-500, -500, 500, 33)
            _, right = look(0, -500, 500, 33)

            x = np.array([-450.0, -350.0, -100.0, 0.0, 100.0, 350.0, 450.0])
            y = np.full_like(x, -300.0)
            cc = calm.sample(x, y)[0].astype(int)
            wc, wd = windy.sample(x, y)
            self.assertLess(wc[0], cc[0])
            self.assertGreater(wc[-1], cc[-1])
            for probe in (far.sample(x, y)[0],
                          np.r_[left.sample(x[:3], y[:3])[0], right.sample(x[3:], y[3:])[0]]):
                np.testing.assert_allclose(wc, probe, atol=1, rtol=0)
            np.testing.assert_array_equal(wd, far.sample(x, y)[1])

            calm_rgb, wind_rgb = calm.apply(rgb), windy.apply(rgb)
            for slot in range(1, 16):
                mask = road == slot
                self.assertTrue(np.any(calm_rgb[mask] != rgb[mask]), slot)
            np.testing.assert_array_equal(wind_rgb[road > 0], calm_rgb[road > 0])
            np.testing.assert_array_equal(wind_rgb[water > 0], calm_rgb[water > 0])
            self.assertGreater(wind_rgb[24:40, 48:, 0].mean(), calm_rgb[24:40, 48:, 0].mean())
            self.assertLess(wind_rgb[24:40, :16, 0].mean(), calm_rgb[24:40, :16, 0].mean())


if __name__ == "__main__":
    unittest.main()
