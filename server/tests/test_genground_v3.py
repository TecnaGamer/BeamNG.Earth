"""The generated ground's stage 3 (earthfast 310): the same output, cheaper. The texel grid and its Web-Mercator
metres in one native pass (gg_grid: the numbers enu_to_geodetic and numpy / dnp gave), value noise on the kernels'
threads (the same bits on any number), the polygon fill by an edge table and the line fill by capsule intervals (the
texels the direct tests gave), the lines' tag decisions kept per layer."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dnp  # noqa: E402
import fast  # noqa: E402
import genground as gg  # noqa: E402
import geodesy  # noqa: E402

fast.require(gg.NEED, "generated ground")
EF = fast._ef


class _Plain:
    """A frame without origin / rot: _grid's numpy / enu_to_geodetic path (the old one)."""

    def __init__(self, frame):
        self.f = frame

    def enu_to_geodetic(self, e, n, u=0.0):
        return self.f.enu_to_geodetic(e, n, u)


def fill_ref(h, w, polys):
    """The direct scanline fill (every edge on every row): the old gg_fill_polys."""
    out = np.zeros((h, w), np.uint32)
    for rings, v in polys:
        ys = np.concatenate([np.asarray(r)[:, 1] for r in rings])
        ra = max(int(np.ceil(ys.min() - 0.5)), 0)
        rb = int(min(np.floor(ys.max() - 0.5), h - 1))
        for row in range(ra, rb + 1):
            ty = row + 0.5
            xs = []
            for r in rings:
                r = np.asarray(r, np.float64)
                if len(r) < 3:
                    continue
                for q in range(len(r)):
                    xa, ya = r[q]
                    xb, yb = r[(q + 1) % len(r)]
                    if (ya > ty) != (yb > ty):
                        xs.append(xa + (ty - ya) * (xb - xa) / (yb - ya))
            xs.sort()
            for a, b in zip(xs[0::2], xs[1::2]):
                ca, cb = max(np.ceil(a - 0.5), 0.0), min(np.ceil(b - 0.5), w)
                if cb > ca:
                    out[row, int(ca):int(cb)] = v
    return out


def lines_ref(h, w, lines):
    """The direct capsule fill (every texel of each segment's box): the old gg_fill_lines."""
    out = np.zeros((h, w), np.uint32)
    for pts, hw, v in lines:
        for (ax, ay), (bx, by) in zip(pts[:-1], pts[1:]):
            c0 = int(max(np.floor(min(ax, bx) - hw - 0.5), 0.0))
            c1 = min(np.ceil(max(ax, bx) + hw - 0.5), w - 1.0)
            r0 = int(max(np.floor(min(ay, by) - hw - 0.5), 0.0))
            r1 = min(np.ceil(max(ay, by) + hw - 0.5), h - 1.0)
            if c1 < 0 or r1 < 0:
                continue
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            for r in range(r0, int(r1) + 1):
                for c in range(c0, int(c1) + 1):
                    px, py = c + 0.5, r + 0.5
                    t = min(max(((px - ax) * dx + (py - ay) * dy) / l2, 0.0), 1.0) if l2 > 0 else 0.0
                    ex, ey = ax + t * dx - px, ay + t * dy - py
                    if ex * ex + ey * ey <= hw * hw:
                        out[r, c] = v
    return out


class SameOutputTest(unittest.TestCase):
    def test_grid_same_numbers(self):
        frame = geodesy.EnuFrame(40.877, -77.566)
        for x0, y0, size, px in ((-576.5, -576.5, 1152.0, 96), (1000.25, -3000.5, 300.0, 50)):
            new = gg._grid(frame, x0, y0, size, px)
            old = gg._grid(_Plain(frame), x0, y0, size, px)
            for a, b in zip(new, old):
                np.testing.assert_array_equal(a, b)
        # the old canopy coordinates, from the old lat / lon
        lat, lon = old[0], old[1]
        np.testing.assert_array_equal(new[2], gg.R_MERC * np.radians(lon))
        np.testing.assert_array_equal(new[3], gg.R_MERC * dnp.log(dnp.tan(np.pi / 4 + np.radians(lat) / 2)))

    def test_value_noise_any_threads(self):
        rng = np.random.default_rng(4)
        x, y = rng.uniform(-1e6, 1e6, 200000), rng.uniform(-1e6, 1e6, 200000)
        outs = []
        try:
            for t in (1, 3, 8):
                EF.set_native_threads(t)
                outs.append(EF.value_noises(x, y, [1, 2, 7]))
        finally:
            EF.set_native_threads(0)
        for o in outs[1:]:
            for a, b in zip(o, outs[0]):
                np.testing.assert_array_equal(a, b)

    def test_polygon_fill_as_before(self):
        rng = np.random.default_rng(9)
        polys = []
        for v in range(1, 40):
            c = rng.uniform(0, 64, 2)
            k = int(rng.integers(3, 30))
            ang = np.sort(rng.uniform(0, 2 * np.pi, k))
            rad = rng.uniform(1, 25, k)
            outer = np.stack([c[0] + rad * np.cos(ang), c[1] + rad * np.sin(ang)], 1)
            rings = [outer]
            if v % 5 == 0:
                rings.append(np.stack([c[0] + 0.3 * rad * np.cos(ang), c[1] + 0.3 * rad * np.sin(ang)], 1))
            if v % 7 == 0:          # (axis-aligned edges on texel centres and corners: the ties)
                rings = [np.array([(10.5, 10.5), (30.0, 10.5), (30.0, 20.0), (10.5, 20.0)])]
            polys.append((rings, v))
        out = np.zeros((64, 64), np.uint32)
        pts, ring_off, poly_off = [], [0], [0]
        for rings, _v in polys:
            for r in rings:
                pts.append(r)
                ring_off.append(ring_off[-1] + len(r))
            poly_off.append(len(ring_off) - 1)
        EF.gg_fill_polys(out, np.ascontiguousarray(np.concatenate(pts)), np.asarray(ring_off, np.int64),
                         np.asarray(poly_off, np.int64), np.arange(1, len(polys) + 1, dtype=np.uint32))
        np.testing.assert_array_equal(out, fill_ref(64, 64, polys))

    def test_line_fill_as_before(self):
        rng = np.random.default_rng(12)
        lines = []
        for v in range(1, 30):
            k = int(rng.integers(2, 6))
            pts = rng.uniform(-10, 74, (k, 2))
            if v % 6 == 0:
                pts[:, 1] = pts[0, 1]           # (horizontal)
            if v % 9 == 0:
                pts[:, 0] = 20.5                 # (vertical, on texel centres)
            lines.append((pts, float(rng.uniform(0.3, 4.0)), v))
        out = np.zeros((64, 64), np.uint32)
        EF.gg_fill_lines(out, np.ascontiguousarray(np.concatenate([p for p, _h, _v in lines])),
                         np.asarray(np.cumsum([0] + [len(p) for p, _h, _v in lines]), np.int64),
                         np.asarray([h for _p, h, _v in lines]), np.asarray([v for _p, _h, v in lines], np.uint32))
        np.testing.assert_array_equal(out, lines_ref(64, 64, lines))


class _Surf:
    """A land cover that is a pure function of the point (as surface.classes is for cached cells)."""

    def __init__(self, complete=True):
        self.complete, self.calls = complete, 0
        self.cells = {}

    def _cached(self, key):         # (surface.Surface._cached: one Raster object per cell while it is cached)
        return self.cells.setdefault(key, object())

    def classes(self, lat, lon):
        self.calls += np.size(lat)
        c = (np.floor(np.asarray(lat) * 2e4).astype(np.int64) * 7 + np.floor(np.asarray(lon) * 2e4).astype(np.int64))
        return (np.array([10, 30, 40, 50], np.uint8)[c % 4]), self.complete


class FieldsCacheTest(unittest.TestCase):
    def setUp(self):
        gg._fields_last.clear()
        self.addCleanup(gg._fields_last.clear)

    def test_next_block_same_bits_fewer_texels(self):
        frame = geodesy.EnuFrame(40.877, -77.566)
        sf = _Surf()
        gg.texel_fields(frame, -640.5, -576.5, 256.0, 256, sf)       # (the block 128 m west, 64 m north)
        sf.calls = 0
        got, ok = gg.texel_fields(frame, -512.5, -640.5, 256.0, 256, sf)
        self.assertTrue(ok)
        self.assertLess(sf.calls, 256 * 256, "only the strips it adds")
        gg._fields_last.clear()
        fresh, _ = gg.texel_fields(frame, -512.5, -640.5, 256.0, 256, _Surf())
        for n in gg.FIELD_NAMES:
            np.testing.assert_array_equal(got[n], fresh[n], n)

    def test_changed_land_cover_not_reused(self):
        # (Codex review of f25f5d5f..5cff9200) the fields reused from the last block must be keyed on the land cover they
        # were read from: a cell's raster replaced (another version, re-decoded) between two blocks must not carry the
        # old classes into the next block
        class Raster:
            def __init__(self, offset):
                self.offset = offset

        class Surf(_Surf):
            def __init__(self):
                super().__init__()
                self.rasters = {}
                self.offset = 0

            def _cached(self, key):
                return self.rasters.setdefault(key, Raster(self.offset))

            def classes(self, lat, lon):
                c, ok = super().classes(lat, lon)
                return (c + 10 * self.offset).astype(np.uint8), ok

        frame = geodesy.EnuFrame(40.877, -77.566)
        sf = Surf()
        gg.texel_fields(frame, -640.5, -576.5, 256.0, 256, sf)
        sf.offset, sf.rasters = 1, {}               # (the land cover's cells changed: new rasters, new classes)
        got, _ = gg.texel_fields(frame, -512.5, -640.5, 256.0, 256, sf)
        gg._fields_last.clear()
        fresh, _ = gg.texel_fields(frame, -512.5, -640.5, 256.0, 256, sf)
        for n in ("classes", "base"):
            np.testing.assert_array_equal(got[n], fresh[n], n)

    def test_incomplete_not_kept_other_frames_not_used(self):
        frame = geodesy.EnuFrame(40.877, -77.566)
        gg.texel_fields(frame, -64.5, -64.5, 128.0, 128, _Surf(complete=False))
        self.assertEqual(gg._fields_last, [], "a class read before its cell came: not kept")
        gg.texel_fields(frame, -64.5, -64.5, 128.0, 128, _Surf())
        other = geodesy.EnuFrame(40.9, -77.5)
        sf = _Surf()
        gg.texel_fields(other, -64.5, -64.5, 128.0, 128, sf)
        self.assertEqual(sf.calls, 128 * 128, "another anchor: computed in full")
        sf = _Surf()
        gg.texel_fields(other, -64.25, -64.5, 128.0, 128, sf)
        self.assertEqual(sf.calls, 128 * 128, "off the half-metre grid: computed in full")


def gen(s2, kind, ox, oy, canopy=None):
    n = kind.shape[0]
    canopy = np.full((n, n), 255, np.uint8) if canopy is None else canopy
    return EF.gen_ground(np.ascontiguousarray(s2), np.ascontiguousarray(kind, np.uint8),
                         np.ascontiguousarray(kind.astype(np.uint32)), np.ascontiguousarray(canopy, np.uint8),
                         np.zeros((n, n), np.uint32), np.zeros((0, 2)), np.zeros(0), float(ox), float(oy), 1.0,
                         int(-oy), int(ox), None, 0)


def two_fields(n, col, a, b, blur=12):
    """One crop region (unmapped fields): colour a west of col, b east, smeared over `blur` texels (the 10 m photo)."""
    x = np.clip((np.arange(n) - (col - blur / 2)) / blur, 0.0, 1.0)[None, :, None]
    img = np.asarray(a, float)[None, None, :] * (1 - x) + np.asarray(b, float)[None, None, :] * x
    return np.ascontiguousarray(np.broadcast_to(img, (n, n, 3)).round().astype(np.uint8))


class BarrierTest(unittest.TestCase):
    """(stage 3, step 2) a field's edge the photo shows inside one land-cover region: each side keeps its own colour
    and the edge is sharp, not the 10 m photo's smear; no barrier inside a uniform or merely noisy field."""

    def test_two_fields_sharp(self):
        n, col = 256, 132
        a, b = (150, 140, 90), (70, 105, 50)            # (stubble beside green crop)
        s2 = two_fields(n, col, a, b)
        kind = np.full((n, n), 4, np.uint8)             # CROP
        out = gen(s2, kind, 0.0, 0.0).astype(float)
        m = n // 2
        left, right = out[m - 8:m + 8, col - 3].mean(0), out[m - 8:m + 8, col + 2].mean(0)
        s_left, s_right = s2[m, col - 3].astype(float), s2[m, col + 2].astype(float)
        self.assertGreater(np.abs(left - right).sum(), 2.0 * np.abs(s_left - s_right).sum(),
                           "the sides apart at the edge (the photo smeared them together)")
        self.assertLess(np.abs(left - np.array(a)).sum(), 40)
        self.assertLess(np.abs(right - np.array(b)).sum(), 40)

    def test_no_barrier_in_noise(self):
        n = 256
        rng = np.random.default_rng(3)
        s2 = np.clip(np.full((n, n, 3), (100, 110, 70), float) + rng.normal(0, 3, (n // 8, n // 8, 1)).repeat(8, 0)
                     .repeat(8, 1), 0, 255).astype(np.uint8)
        kind = np.full((n, n), 4, np.uint8)
        plain = gen(s2, kind, 0.0, 0.0).astype(float)
        # (the same field without the analysis's chance of a barrier: compare with a flat field's detail alone)
        flat = gen(np.full_like(s2, (100, 110, 70)), kind, 0.0, 0.0).astype(float)
        diff = np.abs((plain - s2) - (flat - np.full_like(s2, (100, 110, 70))))
        self.assertLess(diff.max(), 12.0, "no edge drawn in a noisy field")

    def test_blocks_agree_with_barriers(self):
        n, halo, W = 160, int(gg.HALO_M), 600
        rng = np.random.default_rng(8)
        cols = rng.integers(40, 200, (W // 40 + 1, W // 56 + 1, 3)).repeat(40, 0).repeat(56, 1)[:W, :W]
        s2 = np.ascontiguousarray(cols.astype(np.uint8))
        kind = np.full((W, W), 4, np.uint8)

        def block(r0, c0):
            sl = (slice(r0 - halo, r0 + n + halo), slice(c0 - halo, c0 + n + halo))
            return gen(s2[sl], kind[sl], float(c0 - halo), float(-(r0 - halo)))[halo:halo + n, halo:halo + n]
        a, b = block(140, 140), block(140 + 24, 140 + 40)
        np.testing.assert_array_equal(a[24:, 40:], b[:n - 24, :n - 40])


def strips(n, period_m, deg, a=(150, 140, 90), b=(90, 120, 60), ox=0.0, oy=0.0):
    """Broad alternating strips (the photo's view of strip cropping), smooth as the 10 m photo shows them."""
    t = np.radians(deg)
    nx, ny = np.cos(t), np.sin(t)
    c, r = np.meshgrid(np.arange(n) + 0.5 + ox, -(np.arange(n) + 0.5) + oy)
    u = (c * nx + r * ny) / period_m
    w = 0.5 + 0.5 * np.cos(2 * np.pi * u)
    img = np.asarray(a, float) * w[..., None] + np.asarray(b, float) * (1 - w[..., None])
    return np.ascontiguousarray(img.round().astype(np.uint8))


def furrow_power(out, base, deg, period=4.0):
    """The 4 m furrows' share of the generated texture's residual (generated / photo) along direction deg (world
    coordinates as gen()'s: texel (r, c) at (c, -r))."""
    resid = out.astype(float).mean(-1) / np.maximum(base.astype(float).mean(-1), 1.0) - 1.0
    t = np.radians(deg)
    n = out.shape[0]
    c, r = np.meshgrid(np.arange(n, dtype=float), -np.arange(n, dtype=float))
    u = (c * np.cos(t) + r * np.sin(t)) / period
    tri = 1.0 - 4.0 * np.abs((u - np.floor(u)) - 0.5)
    return float(np.mean(resid * tri))


class StripesTest(unittest.TestCase):
    """(stage 3, step 4) unmapped crop gets 4 m furrows only where the photo shows periodic strips, along them."""

    def test_periodic_strips_get_furrows_along_them(self):
        n = 384
        s2 = strips(n, 40.0, 30.0)
        kind = np.full((n, n), 4, np.uint8)
        out = gen(s2, kind, 0.0, 0.0)
        # (the furrows' triangle wave along the strips' normal: the 32 normals, 5.625 degrees apart; the best is the
        # one nearest 30 degrees, none across)
        powers = {k: furrow_power(out, s2, k * 5.625) for k in range(32)}
        best = max(powers, key=powers.get)
        self.assertIn(best, (5, 6), powers)
        self.assertGreater(powers[best], 0.008)
        self.assertLess(abs(powers[(best + 16) % 32]), powers[best] / 4)

    def test_no_furrows_from_a_lone_edge_or_flat(self):
        n = 384
        kind = np.full((n, n), 4, np.uint8)
        for s2 in (two_fields(n, 190, (150, 140, 90), (80, 110, 55)),
                   np.full((n, n, 3), (120, 125, 80), np.uint8)):
            out = gen(s2, kind, 0.0, 0.0)
            for k in range(32):
                self.assertLess(abs(furrow_power(out, s2, k * 5.625)), 0.002, k)

    def test_blocks_agree_with_stripes(self):
        n, halo, W = 160, int(gg.HALO_M), 700
        s2 = strips(W, 48.0, 65.0)
        kind = np.full((W, W), 4, np.uint8)

        def block(r0, c0):
            sl = (slice(r0 - halo, r0 + n + halo), slice(c0 - halo, c0 + n + halo))
            return gen(s2[sl], kind[sl], float(c0 - halo), float(-(r0 - halo)))[halo:halo + n, halo:halo + n]
        a, b = block(200, 200), block(200 + 32, 200 + 56)
        np.testing.assert_array_equal(a[32:, 56:], b[:n - 32, :n - 56])


class SupportTest(unittest.TestCase):
    """(stage 3, step 5) a square whose photo colour is well supported keeps it: pavers take less of their palette."""

    def test_red_square_stays_red(self):
        n = 128
        s2 = np.full((n, n, 3), (100, 105, 95), np.uint8)
        kind = np.full((n, n), 11, np.uint8)            # (paved round it)
        kind[24:104, 24:104] = 16                       # PAVERS, 80 m square
        s2[24:104, 24:104] = (150, 82, 66)              # red brick in the photo
        out = gen(s2, kind, 0.0, 0.0).astype(float)
        mid = out[50:78, 50:78].mean((0, 1))
        self.assertGreater(mid[0] - mid[2], 50.0, f"red, not the pavers' grey: {mid}")


if __name__ == "__main__":
    unittest.main()
