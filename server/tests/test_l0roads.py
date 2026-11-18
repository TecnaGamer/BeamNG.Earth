"""L0 roads (l0roads.py, earthfast.l0_flatten): a provisional block's road corridors flattened into its heights before
any road plan exists. The core is flat across at a smoothed profile, the ground beyond eased back and untouched past
T_MAX, a side road gives way to the main road inside its carriageway, two overlapping blocks agree away from their
edges, bridges are left out, and heightmap.generate writes the flattened heights."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import geodesy  # noqa: E402
import grading  # noqa: E402
import heightmap  # noqa: E402
import l0roads  # noqa: E402
from test_roads import LAT, LON, features  # noqa: E402

FRAME = geodesy.EnuFrame(LAT, LON)


def ground(x, y):
    """A hillside (6 % up to the north) with 1 m of DEM bumps every few metres and a 2 % slope east."""
    return 50.0 + 0.06 * y + 0.02 * x + 0.5 * np.sin(x / 3.1) * np.cos(y / 2.3) + 0.3 * np.sin(x / 1.7 + y / 5.0)


def grid(cx=0.0, cy=0.0, size=256):
    xs, ys = heightmap.sample_coords(cx, cy, size)
    X, Y = np.meshgrid(xs, ys)
    return xs, ys, ground(X, Y)


def corridor(lines, core, sigma=14.0, median=24.0, rank=3.0):
    pts = np.concatenate([np.asarray(ln, np.float64) for ln in lines])
    starts = np.zeros(len(lines) + 1, np.int64)
    starts[1:] = np.cumsum([len(ln) for ln in lines])
    k = len(lines)

    def arr(v):
        return np.asarray(np.broadcast_to(np.asarray(v, np.float64), (k,)), np.float64).copy()
    return pts, starts, arr(core), arr(sigma), arr(median), arr(rank)


def roughness(z):
    """RMS of the second difference along a line of 1 m samples (m): bumps, not the grade."""
    return float(np.sqrt(np.mean(np.diff(z, 2) ** 2)))


class FlattenTest(unittest.TestCase):
    def test_core_flat_across_and_smooth_along(self):
        xs, ys, u = grid()
        before = u.copy()
        road = [(-120.0, 0.0), (120.0, 0.0)]          # east-west, along the 2 % slope, across the 6 % one
        n, dmax, _ = l0roads.flatten_grid(u, xs, ys, corridor([road], core=5.0))
        self.assertGreater(n, 0)
        self.assertLess(dmax, 2.0)
        j0 = int(np.flatnonzero(ys == 0.0)[0])
        cols = slice(64, 192)
        across = u[j0 - 4:j0 + 5, cols]             # 4 m either side of the centreline: inside the 5 m core
        self.assertLess(float(np.max(np.ptp(across, axis=0))), 0.02, "flat across the core")
        self.assertLess(roughness(u[j0, cols]), 0.25 * roughness(before[j0, cols]), "the bumps along it gone")
        # the grade along it kept (2 % east)
        g = np.polyfit(xs[cols], u[j0, cols], 1)[0]
        self.assertAlmostEqual(g, 0.02, delta=0.004)
        far = np.abs(ys) > 5.0 + grading.T_MAX + 1
        self.assertTrue(np.array_equal(u[far], before[far]), "nothing changed past the core + T_MAX")

    def test_ease_back_is_continuous(self):
        xs, ys, u = grid()
        l0roads.flatten_grid(u, xs, ys, corridor([[(-120.0, 0.0), (120.0, 0.0)]], core=4.0))
        col = u[:, 128]
        steps = np.abs(np.diff(col))
        # the hillside's own 6 % grade and its bumps, but no wall at the core's edge
        self.assertLess(float(steps.max()), 0.6)

    def test_side_road_gives_way_to_the_main_road(self):
        xs, ys, u = grid()
        main = [(-120.0, 0.0), (120.0, 0.0)]
        side = [(0.0, 0.0), (0.0, 120.0)]
        only = u.copy()
        l0roads.flatten_grid(only, xs, ys, corridor([main], core=6.0, rank=7.0))
        both = u.copy()
        pts, starts, core, sigma, median, rank = corridor([main, side], core=[6.0, 4.0], rank=[7.0, 3.0])
        _, _, joins = l0roads.flatten_grid(both, xs, ys, (pts, starts, core, sigma, median, rank))
        self.assertEqual(joins, 1, "the side road's end at the main road joined it")
        j0 = int(np.flatnonzero(ys == 0.0)[0])
        i0 = int(np.flatnonzero(xs == 0.0)[0])
        inside = both[j0 - 3:j0 + 4, i0 - 3:i0 + 4] - only[j0 - 3:j0 + 4, i0 - 3:i0 + 4]
        self.assertLess(float(np.max(np.abs(inside))), 0.03, "the main road's carriageway kept at the junction")
        up = both[j0:j0 + 40, i0]
        self.assertLess(float(np.max(np.abs(np.diff(up)))), 0.25, "the side road eases onto it, no step")

    def test_two_blocks_agree_away_from_their_edges(self):
        roads_ = [[(-300.0, -20.0), (-50.0, 10.0), (60.0, 5.0), (300.0, 40.0)], [(30.0, -300.0), (35.0, 300.0)]]
        cor = corridor(roads_, core=[5.0, 3.5], sigma=[14.0, 20.0], rank=[3.0, 5.0])
        xa, ya, ua = grid(0.0, 0.0, 512)
        xb, yb, ub = grid(128.0, 0.0, 512)
        l0roads.flatten_grid(ua, xa, ya, cor)
        l0roads.flatten_grid(ub, xb, yb, cor)
        # overlap x in [-128, 255]; away from either block's edge by 3 sigma + T_MAX + core
        keep = 3 * 20.0 + grading.T_MAX + 6.0
        x_lo, x_hi = xb[0] + keep, xa[-1] - keep
        ia = (xa >= x_lo) & (xa <= x_hi)
        ib = (xb >= x_lo) & (xb <= x_hi)
        rows = slice(int(keep), 512 - int(keep))
        self.assertTrue(np.allclose(ua[rows][:, ia], ub[rows][:, ib], atol=1e-9))

    def test_roads_off_the_grid_change_nothing(self):
        xs, ys, u = grid()
        before = u.copy()
        n, _, _ = l0roads.flatten_grid(u, xs, ys, corridor([[(1000.0, 1000.0), (1200.0, 1000.0)]], core=5.0))
        self.assertEqual(n, 0)
        self.assertTrue(np.array_equal(u, before))


class FakeSurface:
    def __init__(self, feats):
        self.feats = feats

    def roads(self, lat, lon):
        return [self.feats], True

    urban = None


class GenerateTest(unittest.TestCase):
    def elev(self, la, lo):
        e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
        return ground(np.asarray(e), np.asarray(n))

    def build(self, feats, size=256):
        st = {}
        keep = {}
        with tempfile.TemporaryDirectory() as d:
            post = l0roads.post_for(FRAME, FakeSurface(feats), 0.0, 0.0, size, st)
            info = heightmap.generate(FRAME, self.elev, os.path.join(d, "hm.png"), 0.0, 0.0, size=size, keep=keep,
                                      q=1 / 256, post=post)
        return info, keep, st

    def test_written_heights_are_flattened(self):
        if not l0roads.ON:
            self.skipTest("BNGE_L0_ROADS=0")
        f = features([[(-200, 0), (200, 0)]], ["residential"])
        _, keep, st = self.build(f)
        self.assertGreater(st["l0Samples"], 0)
        q = keep["qgrid"].astype(np.float64) / 256
        j0 = 128                                     # y = 0
        self.assertLess(float(np.max(np.ptp(q[j0 - 3:j0 + 4, 40:216], axis=0))), 0.03, "the qgrid's core is flat")
        _, keep0, st0 = self.build(features([[(-200, 0), (200, 0)]], ["residential"], flags=[(0, "is_bridge")]))
        self.assertEqual(st0["l0Samples"], 0, "a bridge is not laid into the ground")


class GroundPhotoTest(unittest.TestCase):
    def test_materials_coloured_north_up(self):
        """The ground stand-in (l0roads.ground_photo): each sample's material colour, row 0 of the photo north (the
        .ter's row 0 south), the slope shading only a tint; dirt by climate where the land cover is unknown."""
        import surface as sf
        n, q = 256, 1 / 64
        z = np.tile(np.linspace(0.0, 10.0, n), (n, 1))
        idx = np.full((n, n), sf.GRASS, np.uint8)
        idx[120:136, :] = sf.ASPHALT
        idx[:40, :] = sf.DIRT
        with tempfile.TemporaryDirectory() as d:
            name, _ = sf.write_ter(d, "t.ter", np.round(z / q).astype(np.uint16), idx)
            rgb = l0roads.ground_photo(os.path.join(d, name), q, 40.0, -80.0, True, 512, 1.0)
            dry = l0roads.ground_photo(os.path.join(d, name), q, 25.0, 45.0, False, 512, 1.0)    # (Arabia: arid)
        self.assertEqual(rgb.shape, (512, 512, 3))
        g, a, dirt = rgb[20, 256].astype(int), rgb[512 - 2 * 128, 256].astype(int), rgb[-10, 256].astype(int)
        self.assertGreater(g[1], g[0], "grass at the top (north) is green")
        self.assertLess(abs(a[0] - a[2]), 8, "the road grey")
        self.assertLess(a.sum(), g.sum(), "the road darker than the grass")
        self.assertGreater(dirt[0], dirt[2], "the dirt (south, the bottom) brown")
        self.assertGreater(int(dry[-10, 256, 0]), 150, "unknown land in a desert: sand-coloured, not brown")


if __name__ == "__main__":
    unittest.main()
