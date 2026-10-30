"""riverlines: river / stream centrelines chained in flow order, resampled, their surface from the channel's
DEM never rising downstream, cut into pieces on a lattice with stable keys, left out where tiles are water."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import riverlines  # noqa: E402

LAT, LON = 40.0, -77.0
M_LON = 1.0 / (111320.0 * math.cos(math.radians(LAT)))


class Lines:
    """mapgeom.Features-like: water lines (kind 2)."""

    def __init__(self, rows):          # (id, subtype, class, [(lon, lat), ...])
        self.ids = np.array([r[0] for r in rows])
        self.kind = np.full(len(rows), 2, np.uint8)
        self.attrs = {"subtype": np.array([r[1] for r in rows]), "class": np.array([r[2] for r in rows]),
                      "is_intermittent": np.zeros(len(rows), bool)}
        self.rows = rows

    def parts(self, i):
        return [[np.asarray(self.rows[i][3], np.float64)]]


def east(m):
    return (LON + m * M_LON, LAT)


class ChainTest(unittest.TestCase):
    def test_continuing_ways_chained_in_order(self):
        a = ("a", "stream", np.array([east(0), east(100)]))
        b = ("b", "stream", np.array([east(100), east(250)]))
        c = ("c", "stream", np.array([east(250), east(400)]))
        out = riverlines.chains([c, a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], ("a", "b", "c"))
        self.assertEqual(len(out[0][2]), 4)

    def test_confluence_not_chained_through(self):
        a = ("a", "stream", np.array([east(0), east(100)]))
        b = ("b", "stream", np.array([(LON, LAT + 0.001), east(100)]))
        c = ("c", "stream", np.array([east(100), east(200)]))
        self.assertEqual(sorted(ids for ids, _, _ in riverlines.chains([a, b, c])), [("a",), ("b",), ("c",)])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.f = geodesy.EnuFrame(LAT, LON)
        self.lines = Lines([("s1", "stream", "stream", [east(-1500), east(-200)]),
                            ("s2", "stream", "stream", [east(-200), east(1500)]),
                            ("d1", "canal", "ditch", [(LON, LAT + 0.002), east(300)])])

    def plan(self, wet=lambda la, lo: np.zeros(np.shape(la))):
        # the channel falls 1 m per 100 m eastwards, with a 2 m bump the surface must not follow up
        def dem(la, lo):
            x = (np.asarray(lo) - LON) / M_LON
            return 50.0 - x / 100.0 + np.where(np.abs(x - 500) < 15, 2.0, 0.0)
        return riverlines.plan(self.f, 0.0, 0.0, lambda k: self.lines, wet, dem)

    def test_pieces_downstream_never_rising(self):
        rv = self.plan()
        self.assertTrue(rv)
        self.assertTrue(all(r.kind == "stream" and r.flow == riverlines.FLOW_MS["stream"] for r in rv), "no ditch")
        nodes = sorted((n for r in rv for n in r.nodes), key=lambda n: n[0])
        z = np.array([n[2] for n in nodes])
        self.assertTrue(np.all(np.diff(z) <= 1e-6), "never rises downstream (the bump is not followed)")
        for r in rv:
            xs = [n[0] for n in r.nodes]
            self.assertTrue(all(b > a for a, b in zip(xs, xs[1:])), "flow order")
            self.assertAlmostEqual(r.nodes[0][3], riverlines.WIDTH_M["stream"] * riverlines.WIDTH_PAD
                                   + riverlines.WIDTH_PAD_M)
        self.assertEqual(sorted(r.key for r in rv), sorted(r.key for r in self.plan()), "stable keys")

    def test_left_out_where_tiles_are_water_but_reaching_in(self):
        wet = lambda la, lo: ((np.asarray(lo) - LON) / M_LON > 0).astype(float)    # noqa: E731
        rv = self.plan(wet)
        self.assertTrue(rv)
        xs = [n[0] for r in rv for n in r.nodes]
        self.assertTrue(all(v <= riverlines.SPACING_M["stream"] + 1e-6 for v in xs), "stops at the lake")
        self.assertGreater(max(xs), 0.0, "one node into it: no gap at the mouth")

    def test_confluence_narrow_ends_in_wide(self):
        f = geodesy.EnuFrame(LAT, LON)
        lines = Lines([("big", "river", "river", [(LON - 0.004, LAT + 0.003), (LON + 0.004, LAT + 0.003)]),
                       ("trib", "stream", "stream", [(LON, LAT - 0.004), (LON, LAT + 0.003)])])
        outline = lambda la, lo: np.abs((np.asarray(la) - (LAT + 0.003)) * 111320.0) < 40.0   # noqa: E731
        rv = riverlines.plan(f, 0.0, 0.0, lambda k: lines, lambda la, lo: np.zeros(np.shape(la)),
                             lambda la, lo: np.full(np.shape(la), 20.0), outline)
        big_y = 0.003 * 111320.0
        trib = [n for r in rv if r.kind == "stream" for n in r.nodes]
        self.assertTrue(trib)
        inside = [n for n in trib if abs(n[1] - big_y) < 40.0]
        self.assertLessEqual(len(inside), 1, "the stream stops at the river (one node into it)")

    def test_tributary_wide_at_its_mouth_never_outranks_the_river(self):
        """GitHub #323: a 30 m river whose last nodes measured 780 m (probed along the river it joins) left that
        river's nodes out under itself; ranked by the median width, the main river keeps its water there."""
        n = 40
        main = {"kind": "river", "ids": ("main",), "centre": np.stack([np.arange(n) * 20.0, np.zeros(n)], -1),
                "width": np.full(n, 150.0), "into": np.zeros(n, bool)}
        trib = {"kind": "river", "ids": ("trib",), "centre": np.stack([np.full(n, 400.0), 800.0 - np.arange(n) * 20.0], -1),
                "width": np.r_[np.full(n - 3, 30.0), [780.0, 780.0, 780.0]], "into": np.zeros(n, bool)}
        riverlines._confluences([main, trib])
        self.assertFalse(main["into"].any(), "the main river keeps every node")
        self.assertTrue(trib["into"][-3:].all(), "the tributary ends where it enters the main river")


if __name__ == "__main__":
    unittest.main()


class RoadClearanceTest(unittest.TestCase):
    """Rivers keep off the roads beside them (Lauterbrunnen valley, 2026-10-15: the AI drove into a river
    drawn over the valley road)."""

    def test_distance_to_road_edges(self):
        rc = riverlines.RoadClearance([(0.0, 0.0, 100.0, 0.0, 4.0)])        # a road along x, 8 m wide
        d = rc(np.array([[50.0, 10.0], [50.0, 3.0], [500.0, 0.0]]))
        self.assertAlmostEqual(d[0], 6.0)
        self.assertEqual(d[1], 0.0)
        self.assertTrue(np.isinf(d[2]), "no road within the search radius")

    def test_width_capped_beside_a_road(self):
        """A river along y = 0 with a road along y = 10: its width stops short of the road; without the road
        it keeps its width by class."""
        frame = geodesy.EnuFrame(46.6, 7.9, 0.0)
        pts = np.stack([np.arange(0.0, 200.0, 20.0), np.zeros(10)], -1)
        water = lambda la, lo: np.zeros(np.shape(la))              # noqa: E731
        dem = lambda la, lo: np.full(np.shape(la), 600.0)          # noqa: E731
        free = riverlines._nodes(frame, ("w",), "river", pts, water, dem, None)
        rc = riverlines.RoadClearance([(-50.0, 10.0, 250.0, 10.0, 3.0)])
        kept = riverlines._nodes(frame, ("w",), "river", pts, water, dem, None, road_clear=rc)
        self.assertGreater(free["width"].min(), 14.0)
        self.assertLessEqual(kept["width"].max(), 2 * (7.0 - riverlines.ROAD_MARGIN_M) + 1e-6)


class UndergroundTest(unittest.TestCase):
    """Rivers piped underground (OSM tunnel=* / location=underground, in Overture's source tags) are neither drawn nor
    carved: Novosibirsk's Kamenka under its streets was a strip of water along a dual carriageway (2026-10-25)."""

    @staticmethod
    def lines(tags):
        f = Lines([("s1", "stream", "stream", [east(-1500), east(-200)]),
                   ("s2", "stream", "stream", [east(-200), east(1500)])])
        f.attrs["tags"] = np.array(tags, object)
        return f

    def test_tags(self):
        f = self.lines(['{"waterway":"river","tunnel":"yes"}', '{"waterway":"stream","tunnel":"culvert"}'])
        self.assertTrue(riverlines.underground(f, 0) and riverlines.underground(f, 1))
        f = self.lines(['{"waterway":"river","location":"underground"}', '{"waterway":"river","tunnel":"no"}'])
        self.assertTrue(riverlines.underground(f, 0))
        self.assertFalse(riverlines.underground(f, 1), "tunnel=no: open")
        f = self.lines(['{"waterway":"river","layer":"-1"}', None])
        self.assertFalse(riverlines.underground(f, 0), "a layer alone (under a bridge) is open water")
        self.assertFalse(riverlines.underground(f, 1), "no tags")
        self.assertFalse(riverlines.underground(Lines([("a", "river", "river", [east(0), east(10)])]), 0),
                         "no tags column")
        f = self.lines(['{not json "tunnel"', ''])
        self.assertFalse(riverlines.underground(f, 0) or riverlines.underground(f, 1))

    def test_piped_part_not_drawn(self):
        f = geodesy.EnuFrame(LAT, LON)

        def dem(la, lo):
            return 50.0 - (np.asarray(lo) - LON) / M_LON / 100.0

        def plan(lines):
            return riverlines.plan(f, 0.0, 0.0, lambda k: lines, lambda la, lo: np.zeros(np.shape(la)), dem)

        both = plan(self.lines(['{"waterway":"stream"}', '{"waterway":"stream"}']))
        piped = plan(self.lines(['{"waterway":"stream"}', '{"waterway":"stream","tunnel":"culvert"}']))
        xs = lambda rv: [x for r in rv for x, _, _, _, _ in r.nodes]      # noqa: E731
        self.assertGreater(max(xs(both)), 1000.0, "open: drawn on to its end")
        self.assertTrue(piped, "the open part is still drawn")
        self.assertLess(max(xs(piped)), -150.0, "the piped part (from -200 m on) is not")
