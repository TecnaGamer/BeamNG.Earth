"""The generated ground's stage 2 (genground.py, earthfast genground.rs / ggshapes.rs 308): the map's own lines and
surfaces (OSM's tags in Overture's layers, the road lines), the polygons' row patterns, the season graded over the
generated ground; all world-locked (blocks agree)."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fast  # noqa: E402
import genground as gg  # noqa: E402
from test_genground import gen  # noqa: E402

fast.require(gg.NEED, "generated ground")
EF = fast._ef


class _F:
    """A stand-in Features: rows of (kind, class, attrs, tags, coordinates in metres)."""

    def __init__(self, rows):
        self.ids = np.array([f"f{i}" for i in range(len(rows))])
        self.kind = np.array([r[0] for r in rows], np.uint8)
        names = sorted({k for r in rows for k in r[2]} | {"class", "tags"})
        self.attrs = {n: np.array([(r[1] if n == "class" else json.dumps(r[3]) if n == "tags" else r[2].get(n))
                                   for r in rows], object) for n in names}
        self._xy = [np.asarray(r[4], np.float64) for r in rows]

    def __len__(self):
        return len(self.ids)

    def xy_in(self, frame):
        return None

    def parts(self, i, xy=None):
        return [[self._xy[i]]]


class ShapesTest(unittest.TestCase):
    def test_fill_lines(self):
        out = np.zeros((20, 20), np.uint32)
        pts = np.array([[2.0, 10.0], [18.0, 10.0], [10.0, 2.0], [10.0, 18.0]])
        n = EF.gg_fill_lines(out, pts, np.array([0, 2, 4], np.int64), np.array([1.0, 0.5]),
                             np.array([1, 2], np.uint32))
        self.assertGreater(n, 0)
        self.assertEqual(out[10, 5], 1)
        self.assertEqual(out[9, 5], 1, "1 texel either side")
        self.assertEqual(out[7, 5], 0)
        self.assertEqual(out[10, 10], 2, "the later line over the earlier")
        self.assertEqual(out[5, 10], 2)
        self.assertEqual(out[5, 11], 0, "half a texel: the centre column only")

    def test_poly_axes(self):
        # a long field 200 x 40 turned 30 degrees, its long side with extra vertices: its rows along the long side
        a = np.radians(30.0)
        u, v = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
        ring = [p * u for p in (0, 50, 100, 150, 200)] + [200 * u + 40 * v, 40 * v]
        ring = np.array(ring)
        ax = EF.gg_poly_axes(ring, np.array([0, len(ring)], np.int64), np.array([0, 1], np.int64))
        self.assertEqual(ax.shape, (1, 2))
        self.assertAlmostEqual(abs(float(ax[0] @ u)), 1.0, places=9)
        self.assertGreaterEqual(ax[0, 0], 0.0, "pointing east")


class KindsTest(unittest.TestCase):
    def test_polygon_kinds_from_tags(self):
        sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
        lu = _F([(3, "pitch", {}, {"leisure": "pitch", "surface": "artificial_turf"}, sq),
                 (3, "pitch", {}, {"leisure": "pitch", "sport": "tennis"}, sq),
                 (3, "pitch", {}, {"leisure": "pitch"}, sq),
                 (3, "plaza", {}, {"place": "square", "surface": "bricks"}, sq),
                 (3, "plaza", {}, {"place": "square"}, sq),
                 (3, "orchard", {}, {"landuse": "orchard"}, sq),
                 (3, "industrial", {}, {"landuse": "industrial"}, sq),
                 (3, "residential", {}, {"landuse": "residential"}, sq)])
        got = [gg.polygon_kind("land_use", lu, i)[0] for i in range(len(lu))]
        self.assertEqual(got, [gg.ARTTURF, gg.COURT, gg.TURF, gg.BRICK, gg.PAVERS, gg.ORCHARD, gg.LOT, 255])
        water = _F([(3, "swimming_pool", {}, {"leisure": "swimming_pool"}, sq), (3, "pond", {}, {}, sq)])
        self.assertEqual([gg.polygon_kind("water", water, i)[0] for i in range(2)], [gg.POOL, gg.WATER])
        infra = _F([(3, "parking", {"surface": "gravel"}, {"amenity": "parking"}, sq),
                    (3, "parking", {}, {"amenity": "parking"}, sq)])
        self.assertEqual([gg.polygon_kind("infrastructure", infra, i)[0] for i in range(2)], [gg.GRAVEL, 255],
                         "a paved lot is painted by the photo's own steps")

    def test_line_items(self):
        ln = [(0, 0), (50, 0)]
        roads = _F([(2, "footway", {"subtype": "road"}, {}, ln),
                    (2, "footway", {"subtype": "road", "osm_surface": "paving_stones"}, {}, ln),
                    (2, "track", {"subtype": "road", "osm_width": "4"}, {}, ln),
                    (2, "service", {"subtype": "road", "osm_surface": "gravel", "osm_service": "driveway"}, {}, ln),
                    (2, "service", {"subtype": "road"}, {}, ln),
                    (2, "footway", {"subtype": "road", "osm_layer": "1"}, {}, ln),
                    (2, "standard_gauge", {"subtype": "rail"}, {}, ln),
                    (2, "subway", {"subtype": "rail"}, {}, ln)])
        water = _F([(2, "stream", {}, {"waterway": "stream"}, ln),
                    (2, "drain", {}, {"waterway": "drain", "tunnel": "culvert"}, ln)])
        infra = _F([(2, "hedge", {}, {"barrier": "hedge"}, ln), (2, "pier", {}, {"man_made": "pier"}, ln),
                    (2, "fence", {}, {"barrier": "fence"}, ln)])
        got = [(o, k, h) for o, _id, k, h, _f, _i in gg.line_items([roads], [water], [infra])]
        self.assertEqual(got, [(1, gg.PATH_AUTO, 0.9), (1, gg.PAVERS, 0.9), (1, gg.GRAVEL, 2.0), (1, gg.GRAVEL, 1.5),
                               (0, gg.GRAVEL, gg.RAIL_HALF_M), (2, gg.TREE, gg.HEDGE_HALF_M),
                               (4, gg.BUILT, gg.PIER_HALF_M), (3, gg.WATER, 1.5)],
                         "no paved service road (painted), bridge, subway, culvert or fence")


class PatternsTest(unittest.TestCase):
    def test_pitch_lines_and_world_locked_rows(self):
        n = 128
        s2 = np.full((n, n, 3), (90, 120, 70), np.uint8)
        kind = np.full((n, n), gg.GRASS, np.uint8)
        kind[20:100, 30:110] = gg.TURF
        poly = np.where(kind == gg.TURF, 1, 0).astype(np.uint32)
        region = kind.astype(np.uint32) | (poly << 5)
        dirs, ph = np.array([[0.6, 0.8]]), np.array([3.0])
        a = gen(s2, kind, region, poly=poly, dirs=dirs, phases=ph, ox=100.0, oy=900.0).astype(float)
        self.assertGreater(a[21, 60].mean(), a[60, 60].mean() + 40, "the pitch's white line 1.5-3 m in")
        inner = a[30:90, 40:100, 1]
        self.assertGreater(inner.max() - inner.min(), 8, "mown stripes")
        # the same pitch seen from a block 16 texels east: the same stripes
        sh = 16
        b = gen(np.roll(s2, -sh, 1), np.roll(kind, -sh, 1), np.roll(region, -sh, 1), poly=np.roll(poly, -sh, 1),
                dirs=dirs, phases=ph, ox=100.0 + sh, oy=900.0).astype(float)
        np.testing.assert_array_equal(a[40:80, 60:90], b[40:80, 60 - sh:90 - sh])


class SeasonTest(unittest.TestCase):
    def test_graded_after_the_ground_is_made(self):
        import geodesy
        import imagery

        calls = []

        class Im:
            def square(self, frame, x0, y0, size, px):
                return imagery.EnuSquare(frame, x0, y0, size)

            def _render(self, where, px, pending=None):
                calls.append("render")
                return np.full((px, px, 3), 100, np.uint8), {"eox2023": px * px}

            def _seasoned(self, where, rgb):
                calls.append(("season", rgb.shape))
                return np.full_like(rgb, 7)

        rgb, _ = gg.render(Im(), geodesy.EnuFrame(40.0, -77.0), -32.0, -32.0, 64.0, 64, None, None)
        self.assertEqual(calls, ["render", ("season", (64 + 2 * int(gg.HALO_M),) * 2 + (3,))])
        self.assertTrue((rgb == 7).all(), "the graded generated ground, cut to the block")


if __name__ == "__main__":
    unittest.main()
