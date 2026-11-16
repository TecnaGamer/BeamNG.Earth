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
        ax = EF.gg_poly_axes(ring, np.array([0, len(ring)], np.int64), np.array([0, 1], np.int64),
                             np.zeros(len(ring), np.uint8))
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


class _Clip:
    """Stand-in land_use Features of one cell: polygons in lon / lat (parts(i)) and metres (parts(i, xy)), clipped."""

    def __init__(self, rows, lat_c, lon_c):
        rows = sorted(rows, key=lambda r: r[0])
        self.ids = np.array([r[0] for r in rows])
        self.kind = np.full(len(rows), 3, np.uint8)
        self.attrs = {"class": np.array([r[1] for r in rows], object), "tags": np.array(["{}"] * len(rows), object)}
        self.clipped = np.array([r[3] for r in rows], bool)
        self._ll = [np.asarray(r[2], np.float64) for r in rows]
        k = 111320.0
        self._xy = [np.stack([(p[:, 0] - lon_c) * k * np.cos(np.radians(lat_c)), (p[:, 1] - lat_c) * k], 1)
                    for p in self._ll]

    def __len__(self):
        return len(self.ids)

    def index(self, fid):
        j = int(np.searchsorted(self.ids, fid))
        return j if j < len(self.ids) and self.ids[j] == fid else -1

    def xy_in(self, frame):
        return "xy"

    def parts(self, i, xy=None):
        return [[self._xy[i] if xy is not None else self._ll[i]]]


class CodexRound2Test(unittest.TestCase):
    """The Codex review of 41d74b61..f64f29d2."""

    def setUp(self):
        gg._retries.clear()
        self.addCleanup(gg._retries.clear)

    def test_axes_linear(self):
        # [high] the all-pairs vote was quadratic: a 50,000-vertex forest or farm outline took seconds; now linear
        import time
        t = np.linspace(0, 2 * np.pi, 50000, endpoint=False)
        ring = np.stack([400 * np.cos(t) + 3 * np.sin(37 * t), 150 * np.sin(t)], 1)
        ring = np.concatenate([ring] * 4)
        off = np.arange(0, len(ring) + 1, 50000, dtype=np.int64)
        t0 = time.perf_counter()
        ax = EF.gg_poly_axes(np.ascontiguousarray(ring), off, np.arange(0, 5, dtype=np.int64),
                             np.zeros(len(ring), np.uint8))
        self.assertLess(time.perf_counter() - t0, 1.0, "200,000 vertices in well under a second")
        self.assertEqual(ax.shape, (4, 2))

    def test_clip_edges_left_out(self):
        # a field's piece whose longest edges are the cell border's clip: the field's own edges decide
        ring = np.array([(0, 0), (100, 0), (100, 10), (0, 10)], np.float64)      # its long edges: 100 m east-west
        skip = np.array([1, 0, 1, 0], np.uint8)                                   # ... both the clip
        ax = EF.gg_poly_axes(ring, np.array([0, 4], np.int64), np.array([0, 1], np.int64), skip)
        self.assertAlmostEqual(abs(float(ax[0, 1])), 1.0, places=9, msg="north-south: the field's own sides")

    def test_clipped_field_same_direction_in_every_cell(self):
        # [medium] an orchard 300 m long at 20 degrees, split by a cell border: each cell's piece, asked from either
        # cell, gets the field's direction (not its own piece's: the small piece's longest edge is the clip)
        import mapcells
        key_a = mapcells.cell_of(40.5, -77.5)
        key_b = mapcells.CellKey(key_a.z, key_a.x + 1, key_a.y)
        lon_b, lat0, _lon1, lat1 = key_a.bounds()[2], key_a.bounds()[1], key_a.bounds()[2], key_a.bounds()[3]
        lat_c = (lat0 + lat1) / 2
        mlon = 111320.0 * np.cos(np.radians(lat_c))
        a = np.radians(20.0)
        u, v = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
        # the field in metres from the border point (lon_b, lat_c): 280 m of it east of the border, 20 m west
        corners = [-20 * u, 280 * u, 280 * u + 60 * v, -20 * u + 60 * v]

        def ll(p):
            return (lon_b + p[0] / mlon, lat_c + p[1] / 111320.0)

        def clip(poly, east):
            # the polygon cut at x = 0 (the border), Sutherland-Hodgman on one side
            out = []
            for k in range(len(poly)):
                p, q = poly[k], poly[(k + 1) % len(poly)]
                pin, qin = (p[0] >= 0) == east, (q[0] >= 0) == east
                if pin:
                    out.append(p)
                if pin != qin:
                    t = p[0] / (p[0] - q[0])
                    out.append(p + t * (q - p))
            out = [np.array((0.0, o[1])) if abs(o[0]) < 1e-9 else o for o in out]
            return np.array([ll(o) for o in out])
        west, east = clip(corners, False), clip(corners, True)
        fa = _Clip([("orch1", "orchard", west, True)], lat_c, lon_b)
        fb = _Clip([("orch1", "orchard", east, True)], lat_c, lon_b)
        cells = {key_a: fa, key_b: fb}
        got = []
        for f, key in ((fa, key_a), (fb, key_b)):
            _p, _k, dirs, _ph = gg.polygons(None, -400.0, -400.0, 800.0, 200, [("land_use", f, key)],
                                            more=lambda k2: cells.get(k2))
            got.append(dirs[0])
        for d in got:
            self.assertAlmostEqual(abs(float(d @ u)), 1.0, places=6, msg=str(d))
        # (before: each piece alone; the west piece's longest edge is the 60 m clip, north-south)
        alone = gg.polygons(None, -400.0, -400.0, 800.0, 200, [("land_use", fa, None)])[2][0]
        self.assertLess(abs(float(alone @ u)), 0.9, "the test sees the old fault")

    def test_missing_layer_retried_with_backoff_then_given_up(self):
        # [high] a layer whose fetch failed was never asked again (the block stale for good); asked at every photo it
        # would hammer the host: asked again with a doubling backoff, given up after RETRY_TRIES
        import geodesy
        from unittest import mock
        asks = []

        class Surf:
            def _queue(self, key, layers):
                asks.append((now[0], key, layers))

        class MD:
            def has(self, key, layer):
                return layer != "canopy"

            def cell(self, key, layers=None, kind=None):
                import types
                return types.SimpleNamespace(layers={})
        now = [0.0]
        frame = geodesy.EnuFrame(40.877, -77.566)
        with mock.patch.object(gg, "_now", lambda: now[0]):
            ready_at = None
            for step in range(0, 1700, 10):
                now[0] = float(step)
                if gg.data_ready(MD(), frame, -64.5, -64.5, 128.0, Surf()) and ready_at is None:
                    ready_at = step
            one = sorted({k for _t, k, _l in asks}, key=str)[0]
            times = [t for t, k, _l in asks if k == one]
            self.assertEqual(times, [0.0, 30.0, 90.0, 210.0, 450.0, 930.0])
            self.assertEqual(ready_at, 1530, "given up after its last wait: the photo is made final")
            pending = set()
            gg.render(_ImStub(), frame, -64.5, -64.5, 128.0, 128, MD(), None, None, pending)
            self.assertNotIn(gg.GEN_PENDING, pending, "given up: not pending")


class _ImStub:
    def render(self, frame, x0, y0, size, px, pending=None):
        return np.full((px, px, 3), 100, np.uint8), {"eox2023": px * px}


if __name__ == "__main__":
    unittest.main()
