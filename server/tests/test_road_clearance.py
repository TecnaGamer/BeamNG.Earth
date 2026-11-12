"""Buildings kept off the drawn carriageways (buildings.road_clearance, earthfast 243 bldclear) and a town street's
carriageway kept clear of the walls along it (roads._wall_capped, roadwidth.clearance_row): the user's "houses
blocking the road", GitHub #523 #543 #1184 #433 #74."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import buildings as bld  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import roads  # noqa: E402
import roadwidth  # noqa: E402
from test_buildings import Feats  # noqa: E402
from test_roads import features  # noqa: E402

FRAME = geodesy.EnuFrame(39.2834, -76.5934, 0.0)


def flat(la, lo):
    return np.zeros(np.shape(la))


def ll(x, y, w, d):
    e = np.array([x, x + w, x + w, x, x], np.float64)
    n = np.array([y, y, y + d, y + d, y], np.float64)
    la, lo, _ = FRAME.enu_to_geodetic(e, n, 0.0)
    return np.stack([lo, la], 1)


def street(half=4.0, y=100.0):
    """An 8 m street along y (tile-relative, tile (0, 0)), as carriageway_segments gives it."""
    return np.array([[-100.0, y, 400.0, y]]), np.array([half])


def items_of(rings, cw, ml=None, heights=None, classes=None, roofs=None):
    n = len(rings)
    attrs = {"class": classes or ["house"] * n, "height": heights or [7.0] * n, "roof_shape": roofs or ["flat"] * n}
    if ml is not None:
        attrs["src_dataset"] = ["Microsoft ML Buildings" if m else "OpenStreetMap" for m in ml]
    f = Feats([f"b{i}" for i in range(n)], rings, **attrs)
    stats = {}
    out = bld.tile_buildings(FRAME, 0, 0, 256, 256, [f], flat, carriageways=cw, clear_stats=stats)
    return out, stats


def depth(items, cw):
    specs = [s for s, _a, _b in items if bld._clearable(s)]
    if not specs:
        return -np.inf
    rings = [np.asarray(bld.drawn_ring(s), np.float64)[:, :2] for s in specs]
    off = np.r_[0, np.cumsum([len(r) for r in rings])].astype(np.int64)
    return float(np.max(fast._ef.footprint_road_overlap(np.concatenate(rings), off, cw[0], cw[1], 0.5)[1]))


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 243, "earthfast 243")
class RoadClearanceTest(unittest.TestCase):
    def test_a_wall_over_the_edge_is_cut_to_it(self):
        cw = street()
        out, stats = items_of([ll(20, 102, 12, 10)], cw)          # 2 m into the 8 m street's lanes
        self.assertEqual(len(out), 1)
        self.assertEqual(stats, {"osm_trimmed": 1})
        self.assertGreaterEqual(out[0][0].ring[:, 1].min(), 104.0 + bld.ROAD_CLEAR_MARGIN_M - 1e-6)
        self.assertEqual(len(out[0][0].gring), len(out[0][0].ring))
        self.assertLessEqual(depth(out, cw), 0.0)

    def test_a_pitched_roofs_box_stays_a_box(self):
        cw = street()
        out, _ = items_of([ll(20, 103, 12, 9)], cw, roofs=["gabled"])
        spec = out[0][0]
        self.assertNotEqual(spec.roof, "flat")
        self.assertEqual(len(bld.drawn_ring(spec)), 4)
        self.assertLessEqual(depth(out, cw), 0.0)

    def test_clear_and_slight_overlaps_are_left(self):
        cw = street()
        out, stats = items_of([ll(20, 105, 12, 10), ll(60, 103.9, 12, 10)], cw)     # clear; 0.1 m in (TOL)
        self.assertEqual(stats, {})
        self.assertAlmostEqual(abs(bld._area2(out[1][0].ring)) / 2, 120.0, delta=0.01)

    def test_a_machine_learned_footprint_deep_in_the_lanes_is_left_out(self):
        cw = street()
        out, stats = items_of([ll(20, 98, 12, 10), ll(60, 103.5, 12, 10)], cw, ml=[True, True])
        self.assertEqual(stats.get("ml_dropped"), 1, stats)
        self.assertEqual(stats.get("ml_trimmed"), 1, stats)          # (0.5 m in: its outline's slop, cut)
        self.assertEqual(len(out), 1)

    def test_a_tall_mapped_building_the_road_runs_through_is_a_passage(self):
        cw = street()
        out, stats = items_of([ll(20, 80, 20, 40)], cw, heights=[15.0])
        self.assertEqual(stats, {"osm_passage": 1})
        over = [s for s, _a, _b in out if s.min_h > 0.5]
        ground = [s for s, _a, _b in out if s.min_h <= 0.5]
        self.assertEqual(len(over), 1, "the whole of it over the road")
        self.assertAlmostEqual(over[0].min_h, bld.ROAD_CLEAR_PASSAGE_H)
        self.assertAlmostEqual(over[0].min_h + over[0].height, 15.0, delta=0.01)
        self.assertEqual(len(ground), 2, "a piece either side")
        self.assertTrue(all(abs(s.height - bld.ROAD_CLEAR_PASSAGE_H) < 1e-9 for s in ground))
        self.assertLessEqual(depth(out, cw), 0.0)
        # its pieces' tops meet the part over the road
        under = [bld.base_z(s, a, b) + s.min_h for s, a, b in out if s.min_h > 0.5][0]
        for s, a, b in out:
            if s.min_h <= 0.5:
                self.assertAlmostEqual(bld.base_z(s, a, b) + s.height, under, places=6)

    def test_a_low_shed_on_the_roads_line_is_left_out_a_low_house_keeps_its_pieces(self):
        cw = street()
        out, stats = items_of([ll(20, 97, 6, 6), ll(60, 85, 10, 40)], cw, heights=[3.0, 3.0])
        self.assertEqual(stats.get("osm_dropped"), 1, stats)
        self.assertEqual(stats.get("osm_pieces"), 1, stats)
        self.assertTrue(all(s.min_h <= 0.5 for s, _a, _b in out))
        self.assertLessEqual(depth(out, cw), 0.0)

    def test_canopies_and_off_switch(self):
        cw = street()
        out, stats = items_of([ll(20, 90, 20, 20)], cw, classes=["roof"])     # a fuel station's canopy: driven under
        self.assertEqual(stats, {})
        try:
            bld.ROAD_CLEAR_ON = False
            out, stats = items_of([ll(20, 102, 12, 10)], cw)
            self.assertEqual(stats, {})
            self.assertGreater(depth(out, cw), 1.5)
        finally:
            bld.ROAD_CLEAR_ON = True

    def test_carriageway_segments_follow_the_road_specs(self):
        f = features([[(-200, 100), (300, 100)], [(-200, 50), (300, 50)]], ["residential", "service"],
                     subclasses=[(1, "driveway")])
        seg, half = bld.carriageway_segments(FRAME, [f, f], 0, 0)
        self.assertTrue(np.allclose(seg[:, [1, 3]], 100.0, atol=0.5), "the driveway left out, the street once")
        self.assertAlmostEqual(float(half[0]), roads.road_specs(f)[0].width / 2, places=6)


class _Walls:
    def __init__(self, segs):
        self.w = roadwidth.Walls(np.asarray(segs, np.float64))


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 243, "earthfast 243")
class WallCapTest(unittest.TestCase):
    def row(self, segs, line):
        return roadwidth.clearance_row(np.asarray(line, np.float64), roadwidth.Walls(np.asarray(segs, np.float64)))

    def test_walls_both_sides_give_the_room_and_the_street(self):
        walls = [[-50, 3, 150, 3], [-50, -2, 150, -2]]
        near, row = self.row(walls, [[0, 0], [100, 0]])
        self.assertAlmostEqual(near, 4.0)
        self.assertAlmostEqual(row, 5.0)

    def test_one_house_over_the_line_does_not_narrow_the_street(self):
        walls = [[-50, 6, 150, 6], [-50, -6, 150, -6], [40, 1, 50, 1]]
        near, row = self.row(walls, [[0, 0], [100, 0]])
        self.assertAlmostEqual(near, 12.0)

    def test_no_walls_no_limit(self):
        near, row = self.row([[-50, 40, 150, 40]], [[0, 0], [100, 0]])
        self.assertIsNone(near)
        self.assertIsNone(row)

    def _capped(self, near, row, **kw):
        f = features([[(-100, 0), (100, 0)]], ["residential"])
        sp = roads.road_specs(f)[0]._replace(urban=True, **kw)

        class W:
            def cap(self, fid, ll):
                return near, row

            def get(self, fid, ll):
                return None
        return roads._wall_capped(f, [sp], W().get)[0], sp

    def test_a_town_street_between_close_walls_is_narrowed(self):
        got, sp = self._capped(4.6, 4.6, width=10.6)
        self.assertAlmostEqual(got.width, 4.0, delta=0.01)      # (one lane between the walls, the slack off)

    def test_walls_near_on_one_side_keep_two_lanes(self):
        got, _ = self._capped(4.0, 14.0, width=10.6)
        self.assertAlmostEqual(got.width, roads.CAP_TWO_WAY_M)

    def test_room_enough_leaves_it(self):
        got, sp = self._capped(30.0, 30.0, width=10.6)
        self.assertEqual(got, sp)


if __name__ == "__main__":
    unittest.main()
