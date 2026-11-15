"""Tunnel lids (tunnellids.py, GitHub #1083): a ground road over a tunnel that the bare-earth DEM puts at the tunnel's
floor gets its lid back."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roads  # noqa: E402
import tunnellids  # noqa: E402

TUNNEL = np.c_[np.linspace(-40.0, 40.0, 21), np.zeros(21)]     # 80 m along x, a primary's half width 5 m


def across(x, y0=-60.0, y1=60.0):
    return np.c_[np.full(61, x), np.linspace(y0, y1, 61)]


def trench(banks=(6.5, 6.5), half=8.0):
    """The DEM: the cutting's floor (0 m) |y| < half under the whole tunnel and its ramps, the banks beside it
    (south, north)."""
    def z_at(p):
        p = np.asarray(p, np.float64)
        y = p[:, 1]
        return np.where(np.abs(y) < half, 0.0, np.where(y < 0, banks[0], banks[1]))
    return z_at


class LidsOfTest(unittest.TestCase):
    def test_a_square_over_a_cut_and_cover_gets_its_lid(self):
        # Suedtiroler Platz (#1): two ground roads 25 m apart over the Guertel, the DEM at the floor under both
        got = tunnellids.lids_of([(TUNNEL, 5.0)], [(across(-10.0), 3.0), (across(15.0), 3.0)], trench())
        x = np.array([-10.0, 15.0, 2.0, 2.0, -10.0, 30.0])
        y = np.array([0.0, 0.0, 0.0, 9.0, 40.0, 0.0])
        z = tunnellids.lid_z(got, x, y)
        np.testing.assert_allclose(z[:3], 6.5, atol=1e-6)      # under both roads and the square between them
        self.assertTrue(np.isfinite(z[3]), "the tunnel's width between them")
        self.assertTrue(np.isnan(z[4]), "not past the trench along the road")
        self.assertTrue(np.isnan(z[5]), "not past the roads along the tunnel: the rest stays open")

    def test_the_lid_under_a_bridge_mapped_as_ground_is_straight_between_its_abutments(self):
        # Kennedybruecke (#5): one bank 1 m, the other 4.2 m; the road on top climbs out at 5 m and 4.5 m either side
        def z_at(p):
            p = np.asarray(p, np.float64)
            y = p[:, 1]
            road = np.abs(p[:, 0]) < 4.0
            return np.where(np.abs(y) < 8.0, 0.0, np.where(road, np.where(y < 0, 5.0, 4.5),
                                                         np.where(y < 0, 1.0, 4.2)))
        got = tunnellids.lids_of([(TUNNEL, 5.0)], [(across(0.0), 4.0), (across(25.0), 3.0)], z_at)
        self.assertEqual(len(got), 1, "under the road that climbs out only: no lid at x 25, no fill between")
        xy, z, hw = got[0]
        self.assertAlmostEqual(hw, 4.0 + roads.TUNNEL_ROAD_CLEAR_M)
        self.assertAlmostEqual(float(z[0]), 5.0)
        self.assertAlmostEqual(float(z[-1]), 4.5)
        self.assertTrue((np.diff(z) <= 1e-9).all(), "straight from one abutment to the other")

    def test_a_wide_trench(self):
        # Capitol Crossing (#9): the road on top at the floor 50 m across, 6 m up beyond
        got = tunnellids.lids_of([(TUNNEL, 5.0)], [(across(0.0), 4.0)], trench(half=25.0))
        self.assertAlmostEqual(float(tunnellids.lid_z(got, np.array([0.0]), np.array([0.0]))[0]), 6.5)

    def test_what_stays_open(self):
        lid = tunnellids.lids_of
        self.assertEqual(lid([(TUNNEL, 5.0)], [(across(0.0), 4.0)], trench(banks=(0.5, 0.5))), [],
                         "a garage ramp: nothing rises on either side")
        self.assertEqual(lid([(TUNNEL, 5.0)], [(across(0.0), 4.0)], trench(banks=(6.0, 2.0))), [],
                         "the road on top is not out of the cut on one side")
        self.assertEqual(lid([(TUNNEL, 5.0)], [], trench()), [], "nothing over it (a footway is no road here)")

        def kept(p):        # Dupont Circle: the lid is in the DEM, the road on top at the banks' height
            p = np.asarray(p, np.float64)
            return np.where(np.abs(p[:, 0]) > 30.0, 0.0, 6.0)
        self.assertEqual(lid([(TUNNEL, 5.0)], [(across(0.0), 4.0)], kept), [])
        skew = np.c_[np.linspace(-60.0, 60.0, 61), np.linspace(-15.0, 15.0, 61)]
        self.assertEqual(lid([(TUNNEL, 5.0)], [(skew, 4.0)], trench()), [], "along the tunnel, not across it")

    def test_the_tunnel_finds_a_low_arch_under_the_lid(self):
        z_at = trench()
        got = tunnellids.lids_of([(TUNNEL, 5.0)], [(across(-10.0), 3.0), (across(15.0), 3.0)], z_at)
        s = np.hypot(*(TUNNEL - TUNNEL[0]).T)
        g = np.asarray(z_at(TUNNEL), np.float64)
        floor, kinds = roads.tunnel_sections(s, g)
        self.assertFalse(kinds.any(), "on the DEM alone: an open road")
        lz = tunnellids.lid_z(got, TUNNEL[:, 0], TUNNEL[:, 1])
        floor, kinds = roads.tunnel_sections(s, np.fmax(g, np.nan_to_num(lz, nan=-1e9)))
        self.assertTrue((kinds[(TUNNEL[:, 0] > -10) & (TUNNEL[:, 0] < 15)] == 1).all(), kinds)
        self.assertFalse(kinds[TUNNEL[:, 0] > 30].any(), "its ramps stay open")


class _Cell:
    def __init__(self, f):
        self.layers = {"roads": f}


class _Md:
    def __init__(self, f):
        self.f = f

    def cell(self, key, layers=None, kind=None):
        return _Cell(self.f)


class LidsApplyTest(unittest.TestCase):
    def setUp(self):
        from test_roads import features, LAT, LON, M_LAT, M_LON
        self.LAT, self.LON, self.M_LAT, self.M_LON = LAT, LON, M_LAT, M_LON

        def raw(la, lo):
            n = (np.asarray(la, np.float64) - LAT) * M_LAT
            return np.where(np.abs(n) < 8.0, 100.0, 106.5)
        self.raw = raw
        self.features = features
        self.lines = [[(-40, 0), (40, 0)], [(0, -60), (0, 60)]]

    def ll(self, e, n):
        return self.LAT + np.asarray(n, np.float64) / self.M_LAT, self.LON + np.asarray(e, np.float64) / self.M_LON

    def test_the_ground_gets_the_lid(self):
        f = self.features(self.lines, ["primary", "residential"], flags=[(0, "is_tunnel")])
        lids = tunnellids.Lids(_Md(f), self.raw)
        la, lo = self.ll([0.0, 0.0, 30.0, 0.0], [0.0, 30.0, 0.0, 5.0])
        z = lids.apply(la, lo, self.raw(la, lo))
        np.testing.assert_allclose(z, [106.5, 106.5, 100.0, 106.5], atol=0.05)
        la, lo = self.ll(0.0, 0.0)
        self.assertAlmostEqual(float(lids.apply(float(la), float(lo), 100.0)), 106.5, delta=0.05)
        grid = lids.apply(*self.ll(np.zeros((2, 3)), np.zeros((2, 3))), np.full((2, 3), 100.0))
        self.assertEqual(grid.shape, (2, 3))

    def test_a_footway_over_it_is_no_road(self):
        f = self.features(self.lines, ["primary", "footway"], flags=[(0, "is_tunnel")])
        la, lo = self.ll([0.0], [0.0])
        z = tunnellids.Lids(_Md(f), self.raw).apply(la, lo, self.raw(la, lo))
        self.assertAlmostEqual(float(z[0]), 100.0)


if __name__ == "__main__":
    unittest.main()
