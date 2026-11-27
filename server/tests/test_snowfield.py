"""Local weather S1 (PLAN_FINAL S-1 to S-4): the snow field's kernels (earthfast snowobs.rs, snowfield.rs) and the photos'
S1 tables (snowlook.rs). The snow rule (the user, 2026-11-27): snow only where real data says it snowed or lies."""
import os
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402

NATIVE = 645
NF = 14
MISSING = -2 ** 31
F = {"t": 0, "td": 1, "p": 3, "swe": 4, "wind": 5, "sw": 6, "ts": 7, "depth": 9, "rh": 10, "hour": 11, "month": 12,
     "cloud": 13}
# a golden hash of snowfield.rs selftest(1, 24): the same number on Linux and Windows (the kernel is integer-only)
SELFTEST = None


def window(spec, depth_mm=0, month=1):
    """Hourly records [n, NF]: spec = [(hours, t 0.01 degC, precip 0.01 mm/h, snow share permille)]."""
    out = []
    for n, t, p, share in spec:
        for _ in range(n):
            r = [MISSING] * NF
            r[F["t"]], r[F["td"]], r[F["p"]] = t, t - 200, p
            r[F["swe"]] = p * share // 1000 * 7 // 10
            r[F["wind"]], r[F["sw"]], r[F["ts"]], r[F["rh"]] = 20, 150, t, 70
            r[F["hour"]], r[F["month"]], r[F["cloud"]] = 12, month, 30
            r[F["depth"]] = depth_mm if not out else 0
            out.append(r)
    return np.array(out, np.int32)


def need():
    fast.require(NATIVE, "the snow field (S1)")
    return fast._ef


class TableTest(unittest.TestCase):
    def test_mild_winter_no_snowfall_no_snow(self):
        """Atlanta-like: ten dry January days at +6 degC, the maps bare: no row with snow."""
        ef = need()
        f = window([(246, 600, 0, 0)])
        bare = np.array([[20, 0, 20, 0]] * 5, np.int64)
        k, rows, _st, _runs = ef.snowfield_table(f, 239, 100, 3375, 50, 350, [(100, 0)], bare, -1, bare, -1)
        self.assertEqual(k, -1)
        self.assertTrue(all(r[1] == 0 and r[3] == 0 and r[5] == 0 for r in rows), rows)

    def test_rows_on_the_absolute_grid(self):
        ef = need()
        self.assertEqual(ef.snowfield_rows(1820, 4300), (11, 20))
        k, rows, _st, _runs = ef.snowfield_table(window([(246, -500, 0, 0)]), 239, 2000, 3900, 1820, 4300,
                                                 [(2000, 0)])
        self.assertEqual([r[0] for r in rows], list(range(1650, 4501, 150)))

    def test_dry_spell_keeps_observed_snow_on_the_peaks(self):
        """No snowfall in the window and no model snow, the maps white from 2,700 m: thin observed cover there."""
        ef = need()
        f = window([(246, 300, 0, 0)])
        k0, n = ef.snowfield_rows(1200, 4000)
        z = (k0 + np.arange(n)) * 150
        obs = np.array([[0, 6, 0, 6] if h >= 2700 else [6, 0, 6, 0] for h in z], np.int64)
        k, rows, _st, runs = ef.snowfield_table(f, 239, 1500, 4600, 1200, 4000, [(1500, 0)], obs, k0, obs, k0)
        self.assertTrue(all(r[:3] == [0, 0, 0] for r in runs))
        self.assertEqual(ef.snowfield_line(k, rows, 0)[0], 2625)
        self.assertTrue(all((r[2] > 0) == (r[0] >= 2700) for r in rows), rows)

    def test_bins_settle(self):
        """A one-bin move shows only when it held two evaluations (S-7); the states round-trip."""
        ef = need()
        f = window([(240, -500, 0, 0), (6, -500, 40, 1000)], depth_mm=30)
        k, rows, st, _ = ef.snowfield_table(f, 245, 1000, 4600, 900, 1200, [(1000, 30)])
        self.assertTrue(all(len(s) == 10 for s in st))
        k2, rows2, st2, _ = ef.snowfield_table(f, 245, 1000, 4600, 900, 1200, [(1000, 30)], prev=st)
        self.assertEqual(rows, rows2)

    def test_selftest_deterministic(self):
        ef = need()
        a = ef.snowfield_selftest(1, 24)
        self.assertEqual(a, ef.snowfield_selftest(1, 24))
        if SELFTEST is not None:
            self.assertEqual(a, SELFTEST)


class ObsTest(unittest.TestCase):
    def test_projection_and_snowline(self):
        ef = need()
        r, c = ef.snowobs_ims_pixels(np.array([90.0, -10.0, 45.0]), np.array([0.0, 0.0, -80.0]))
        self.assertEqual((int(r[0]), int(c[0])), (12288, 12288))
        self.assertEqual(int(r[1]), -1)
        self.assertEqual(int(c[2]), 12288)          # 80 W runs straight down from the pole
        counts = np.array([[5, 0, 5, 0], [5, 0, 5, 0], [0, 5, 0, 5]], np.int64)
        self.assertEqual(ef.snowobs_snowline(counts, 10, 0), (1725, 0, 15))

    def test_classes(self):
        ef = need()
        self.assertEqual(list(ef.snowobs_classes(np.array([0, 1, 2, 3, 4], np.uint8), "ims")), [0, 0, 1, 0, 2])
        self.assertEqual(list(ef.snowobs_classes(np.array([0, 34, 35, 100, 250, 239], np.uint8), "ndsi")),
                         [1, 1, 2, 2, 0, 0])


class LookTest(unittest.TestCase):
    """The photos read an S1 table by the texel's aspect (the kernel's pole / equator runs) and never beyond a table's
    own relief when another table reaches the height."""

    def look(self, u, tables, idx, ci0=0, cj0=0, lat=40.0, lon=-89.0):
        ef = need()
        import snowcover
        n, g = u.shape[0], 9
        geo = (np.full((g, g), lat), np.full((g, g), lon), np.zeros((g, g)), np.full((g, g), 1e9), np.zeros((g, g)))
        t = np.zeros((len(tables), max(len(x) for x in tables), 6), np.int32)
        counts = np.zeros((len(tables), 17), np.int32)
        for i, rows in enumerate(tables):
            t[i, :len(rows)] = rows
            counts[i, 0] = len(rows)
        field = (0.25, ci0, cj0, np.array(idx, np.int32), t, counts)
        size = 1000.0
        return ef.SnowLook((-500.0, -500.0, size), (lat, lon), u.astype(np.float64), (-500.0, -500.0, size / (n - 1)),
                           geo, field, snowcover.PARAMS)

    def test_aspect_from_the_table(self):
        # 150-300 m: white facing the pole, bare facing the equator
        rows = [(0, 0, 0, 0, 0, 0), (150, 8, 3, 0, 0, 0), (300, 8, 3, 0, 0, 0), (450, 8, 3, 8, 3, 0)]
        n = 65
        v = np.linspace(100, 350, n)
        north = np.tile(v[:, None], (1, n))            # rising north: facing the equator (south)
        south = north[::-1].copy()                     # falling north: facing the pole
        ci, cj = int((-89.0 + 180) / 0.25), int((90 - 40.0) / 0.25)
        a = self.look(north, [rows], [[0]], ci, cj)
        b = self.look(south, [rows], [[0]], ci, cj)
        ys = np.array([0.0])
        xs = np.zeros(1)
        # the same height (225 m) on each slope: the middle of each grid
        ca = int(a.sample(xs, ys)[0][0])
        cb = int(b.sample(xs, ys)[0][0])
        self.assertGreater(cb, 200, "white facing the pole")
        self.assertLess(ca, 120, "barer facing the equator")

    def test_no_table_beyond_its_relief(self):
        # two cells side by side: a lowland one (rows to 300 m, bare) and a mountain one (white above 1,000 m); a texel
        # at 2,000 m on their border takes the mountain's table only (the lowland has no ground there)
        low = [(0, 0, 0, 0, 0, 0), (150, 0, 0, 0, 0, 0), (300, 0, 0, 0, 0, 0)]
        mtn = [(900, 0, 0, 0, 0, 0), (1050, 8, 4, 8, 4, 0), (3000, 8, 4, 8, 4, 0)]
        lat, lon = 40.0, -89.0
        ci, cj = int((lon + 180) / 0.25), int((90 - lat) / 0.25)
        # the texel on the cells' border: lon at the boundary between ci and ci + 1
        lon_b = (ci + 1) * 0.25 - 180.0
        u = np.full((9, 9), 2000.0)
        lk = self.look(u, [low, mtn], [[0, 1]], ci, cj, lat=(90 - (cj + 0.5) * 0.25), lon=lon_b)
        c = int(lk.sample(np.zeros(1), np.zeros(1))[0][0])
        self.assertGreater(c, 240, c)


class CoverTest(unittest.TestCase):
    """snowcover carries S1 tables to the look with their own key; W2's keys stay what they were."""

    def test_s1_field_through_snowcover(self):
        need()
        import importlib
        import geodesy
        import snowcover
        old = os.environ.get("BNE_WINTER")
        os.environ["BNE_WINTER"] = "on"
        try:
            importlib.reload(snowcover)
            sc = snowcover
            lat, lon = 39.875, -88.875          # (a cell's centre: its neighbours have no table)
            ci, cj = int((lon + 180) / 0.25), int((90 - lat) / 0.25)
            rows = ((0, 0, 0, 0, 0, 0), (150, 8, 3, 0, 0, 0), (300, 8, 3, 0, 0, 0), (450, 8, 3, 8, 3, 0))
            sc.set_provider(lambda s, w, n, e: sc.Field(0.25, cells={(ci, cj): sc.Table(rows, sc.NO_ROAD, 0)}))
            frame = geodesy.EnuFrame(lat, lon)
            sp = sc.spec_for_square(frame, -500, -500, 1000)
            self.assertIsNotNone(sp)
            self.assertEqual(sp.field[4].shape, (1, 4, 6))
            v = np.linspace(100, 350, 65)
            facing_eq = np.tile(v[:, None], (1, 65))
            facing_pole = facing_eq[::-1].copy()
            look = lambda u: sc.look(sp, frame, -500, -500, 1000, u, -500, -500, 1000 / 64)
            ce = int(look(facing_eq).sample(np.zeros(1), np.zeros(1))[0][0])
            cp = int(look(facing_pole).sample(np.zeros(1), np.zeros(1))[0][0])
            self.assertGreater(cp, 200)
            self.assertLess(ce, 120)
            # the same cover as a W2 table: another key
            sc.set_provider(lambda s, w, n, e: sc.Field(0.25, cells={(ci, cj): sc.Table(((150, 8, 3, 0),), sc.NO_ROAD,
                                                                                       0)}))
            self.assertNotEqual(sc.spec_for_square(frame, -500, -500, 1000).sig, sp.sig)
        finally:
            snowcover.set_provider(None)
            if old is None:
                os.environ.pop("BNE_WINTER", None)
            else:
                os.environ["BNE_WINTER"] = old
            importlib.reload(snowcover)


if __name__ == "__main__":
    unittest.main()
