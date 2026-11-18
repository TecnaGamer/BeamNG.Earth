"""railways.parts' selection (earthfast.RailIndex / rail_parts_select): each feature once from its longest copy, its
pieces by the level ranges asked on every call, trams' bridges only, the parts sorted, wear and gauges reported."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import railways  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402

BOX = (0.0, 0.0, 256.0, 256.0)


def line(y, x0=-150.0, x1=400.0):
    return [(x, y) for x in np.arange(x0, x1 + 1e-9, 10.0)]


class RailIndexTest(unittest.TestCase):
    def test_a_feature_comes_from_the_cell_with_its_longest_copy(self):
        # (the same id in two cells: one copy cut short; its bridge range, a fraction of each copy's length, is taken
        # from the long copy's cell, GitHub #1137)
        short = features([line(100.0, -150.0, 200.0)], ["standard_gauge"], ids=["r1"], subtypes=["rail"],
                         flags=[(0, "is_bridge", 0.5, 0.6)])
        full = features([line(100.0, -150.0, 400.0)], ["standard_gauge"], ids=["r1"], subtypes=["rail"],
                        flags=[(0, "is_bridge", 0.5, 0.6)])
        for cells in ([short, full], [full, short]):
            parts = railways.parts(FRAME, cells, BOX, reach=0.0)
            bridges = [xy for fid, _c, kind, xy in parts if kind == "bridge"]
            self.assertEqual({fid for fid, *_ in parts}, {"r1"})
            self.assertEqual(len(bridges), 1)
            # (the long copy's 0.5-0.6: x 125..180, not the short one's 25..60)
            self.assertAlmostEqual(float(bridges[0][0, 0]), 125.0, delta=1.0)

    def test_pieces_follow_the_level_ranges_of_each_call(self):
        f = features([line(100.0)], ["standard_gauge"], ids=["r1"], subtypes=["rail"])
        self.assertEqual({k for _f, _c, k, _xy in railways.parts(FRAME, [f], BOX)}, {"ground"})
        with mock.patch.object(roads, "level_ranges", return_value=[(0.4, 0.5, "bridge")]) as lr:
            kinds = [k for _f, _c, k, _xy in railways.parts(FRAME, [f], BOX)]
            self.assertIn("bridge", kinds)
            lr.assert_called_with(f, 0)
        self.assertEqual({k for _f, _c, k, _xy in railways.parts(FRAME, [f], BOX)}, {"ground"})

    def test_short_tunnels_lie_on_the_ground_and_long_ones_are_left_out(self):
        f = features([line(100.0)], ["standard_gauge"], ids=["r1"], subtypes=["rail"])
        with mock.patch.object(roads, "level_ranges", return_value=[(0.4, 0.45, "tunnel")]):
            ground = [xy for _f, _c, k, xy in railways.parts(FRAME, [f], BOX, reach=0.0) if k == "ground"]
            self.assertEqual(len(ground), 1)            # (27.5 m: track through it)
        with mock.patch.object(roads, "level_ranges", return_value=[(0.4, 0.6, "tunnel")]):
            ground = [xy for _f, _c, k, xy in railways.parts(FRAME, [f], BOX, reach=0.0) if k == "ground"]
            self.assertEqual(len(ground), 2)            # (110 m: the track stops either side)

    def test_trams_keep_their_bridges_only_and_parts_are_sorted(self):
        f = features([line(100.0), line(50.0), line(150.0)], ["tram", "standard_gauge", "light_rail"],
                     ids=["c", "b", "a"], subtypes=["rail"] * 3, flags=[(0, "is_bridge", 0.4, 0.5)])
        parts = railways.parts(FRAME, [f], BOX)
        self.assertEqual([(fid, c, k) for fid, c, k, _xy in parts],
                         [("a", "light_rail", "ground"), ("b", "standard_gauge", "ground"), ("c", "tram", "bridge")])
        for _f, _c, _k, xy in parts:
            self.assertEqual(xy.dtype, np.float64)
            self.assertTrue(xy.flags.c_contiguous)
            self.assertLessEqual(float(np.hypot(*np.diff(xy, axis=0).T).max()), railways.STEP_M + 1e-9)

    def test_wear_and_gauge_are_reported(self):
        f = features([line(100.0), line(50.0)], ["standard_gauge", "standard_gauge"], ids=["a", "b"],
                     subtypes=["rail"] * 2, flags=[(0, "is_abandoned")])
        f.attrs["osm_gauge"] = np.array(["", "1000"], dtype=object)
        worn, gauges = {}, {}
        railways.parts(FRAME, [f], BOX, worn=worn, gauges=gauges)
        self.assertEqual(worn, {"a": 2})
        self.assertEqual(gauges, {"b": 1.0})

    def test_road_tunnels_are_kept_per_road_revision(self):
        f = features([line(100.0), [(120.0, 90.0), (120.0, 110.0)]], ["standard_gauge", "secondary"],
                     subtypes=["rail", "road"], flags=[(1, "is_tunnel")], widths=[(1, 8.6)])
        got = railways.road_underpasses(FRAME, [f, f], BOX)
        self.assertEqual(len(got), 1)               # (the same tunnel once, by id and ends)
        self.assertAlmostEqual(got[0][1], 5.3, places=5)
        ix = railways._underpass_index(f, FRAME)
        self.assertIs(railways._underpass_index(f, FRAME), ix)
        f._road_revision = getattr(f, "_road_revision", 0) + 1     # (roadlanes' reset: specs and ranges made again)
        self.assertIsNot(railways._underpass_index(f, FRAME), ix)
        self.assertEqual(len(railways.road_underpasses(FRAME, [f], (0.0, 0.0, 10.0, 10.0), reach=0.0)), 0)

    def test_a_missing_tunnels_width_is_each_calls_road_spec(self):
        # (a map tunnel gradesep made ground takes the road's own spec: road_specs made while a download failed are
        # made again, so each call's is the one used, not the first call's)
        f = features([line(100.0), [(120.0, 90.0), (120.0, 110.0)]], ["standard_gauge", "secondary"],
                     subtypes=["rail", "road"], flags=[(1, "is_tunnel")], widths=[(1, 8.6)])
        ranges = {0: [], 1: []}
        with mock.patch.multiple(roads, raw_level_ranges=lambda feats, i: list(ranges[i]),
                                 level_ranges=lambda feats, i: list(ranges[i]),
                                 _raw_all=lambda feats: [list(ranges[i]) for i in range(len(feats))],
                                 _level_lists=lambda feats, idx: {i: list(ranges[i]) for i in idx}):
            self.assertIsNone(roads.tunnel_specs(f)[1])
            spec = roads.road_specs(f)[1]
            for width in (6.0, 10.0):
                wide = spec._replace(width=width)
                with mock.patch.object(roads, "road_specs", return_value=[None, wide]):
                    got = railways.road_underpasses(FRAME, [f], BOX)
                self.assertEqual(len(got), 1)
                self.assertEqual(got[0][1], width / 2 + railways.UNDERPASS_SIDE_M)

    def test_a_road_gaining_a_tunnel_spec_is_taken(self):
        # (a road with no map tunnel whose tunnel spec comes later, same road revision: each call's specs decide)
        f = features([line(100.0), [(120.0, 90.0), (120.0, 110.0)]], ["standard_gauge", "secondary"],
                     subtypes=["rail", "road"], widths=[(1, 8.6)])
        spec = roads.road_specs(f)[1]
        with mock.patch.object(roads, "tunnel_specs", return_value=[None, None]):
            self.assertEqual(railways.road_underpasses(FRAME, [f], BOX), [])
        with mock.patch.object(roads, "tunnel_specs", return_value=[None, spec]), \
                mock.patch.object(roads, "tunnel_parts", side_effect=lambda feats, i: [feats.lonlat[2:4]]):
            self.assertEqual(len(railways.road_underpasses(FRAME, [f], BOX)), 1)

    def test_layers_are_taken_one_after_another(self):
        # (a layer's roads are done before the next layer's specs are asked: a failure there leaves the earlier
        # layers' lookups made, as before)
        road = [(120.0, 90.0), (120.0, 110.0)]
        a = features([line(100.0), road], ["standard_gauge", "secondary"], subtypes=["rail", "road"],
                     flags=[(1, "is_tunnel")], widths=[(1, 8.6)], ids=["a0", "a1"])
        b = features([road], ["secondary"], flags=[(0, "is_tunnel")], ids=["b0"])
        calls = []
        specs = roads.tunnel_specs

        def tunnel_specs(feats):
            calls.append(("specs", feats is a))
            if feats is b:
                raise RuntimeError("b's specs")
            return specs(feats)

        def tunnel_parts(feats, i):
            calls.append(("parts", feats is a))
            return roads._parts_by_level(feats, i, "tunnel")
        with mock.patch.object(roads, "tunnel_specs", side_effect=tunnel_specs), \
                mock.patch.object(roads, "tunnel_parts", side_effect=tunnel_parts):
            with self.assertRaises(RuntimeError):
                railways.road_underpasses(FRAME, [a, b], BOX)
        self.assertEqual(calls, [("specs", True), ("parts", True), ("specs", False)])


if __name__ == "__main__":
    unittest.main()
