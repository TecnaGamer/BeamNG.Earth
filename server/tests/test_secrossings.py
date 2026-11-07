"""Sweden's crossings, stop lines and priority signs from the map's nodes (secrossings.py, markings.stop_lines)."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrossings  # noqa: E402
import furniture  # noqa: E402
import junctions  # noqa: E402
import markings  # noqa: E402
import roads  # noqa: E402


def road(fid, cls="tertiary", pts=((0.0, 0.0), (200.0, 0.0)), width=8.0, kind="paved", kmh=50.0, oneway=False):
    line = np.c_[np.array(pts, np.float64), np.zeros(len(pts))]
    return fid, roads.Spec(width, kind, True, cls, oneway, kmh=kmh), line, 0.0


def found(*crossed):
    return {"crossings": list(crossed), "stop_lines": np.zeros((0, 2)), "priority": np.zeros((0, 3))}


class PaintTest(unittest.TestCase):
    def test_what_the_tags_say(self):
        for tags, want in (({"crossing:markings": "zebra"}, secrossings.M15),
                           ({"crossing": "marked", "crossing:markings": "yes"}, secrossings.M15),
                           ({"crossing:markings": "dots"}, secrossings.M16),
                           ({"crossing:markings": "dots;zebra"}, secrossings.M15 | secrossings.M16),
                           ({"crossing:markings": "zebra:dots"}, secrossings.M15 | secrossings.M16),
                           ({"crossing:markings": "surface;zebra"}, secrossings.M15),
                           ({"crossing": "unmarked", "crossing:markings": "no"}, 0),
                           ({"crossing": "unmarked"}, 0),
                           ({"crossing": "informal"}, 0),
                           ({"crossing:markings": "surface"}, 0),
                           ({"crossing": "traffic_signals", "crossing:markings": "no"}, 0)):
            self.assertEqual(secrossings.paint(tags), want, tags)

    def test_no_word_on_paint_is_the_maps_majority(self):
        for tags in ({"crossing": "uncontrolled"}, {"crossing": "traffic_signals"}, {"highway": "crossing"}):
            self.assertEqual(secrossings.paint(tags), secrossings.M15, tags)


class WantedTest(unittest.TestCase):
    """Who reads the map's crossing, stop and give-way nodes: Sweden always, Germany with its junction signs on (de: the
    setting and the tile's or the graph's centre inside Germany)."""
    def test_sweden(self):
        self.assertTrue(secrossings.wanted("SE"))
        self.assertTrue(secrossings.wanted("se", de=False))

    def test_germany_with_its_signs_on(self):
        self.assertTrue(secrossings.wanted("DE", de=True))
        self.assertFalse(secrossings.wanted("DE"))
        self.assertFalse(secrossings.wanted("DE", de=False))

    def test_elsewhere(self):
        self.assertFalse(secrossings.wanted("FR"))
        self.assertFalse(secrossings.wanted(None))
        self.assertFalse(secrossings.wanted(""))


class GermanCrossingTest(unittest.TestCase):
    """Germany's crossings by their own tags: Z 293 (a Fußgängerüberweg) only where the map has a marked crossing
    without signals, nothing on a bare highway=crossing, a refuge island, an unmarked one or one at signals (VwV-StVO zu
    § 26 Rn. 5)."""
    def test_marked_is_z293(self):
        for tags in ({"crossing": "zebra"}, {"crossing_ref": "zebra"}, {"crossing:markings": "zebra"},
                     {"crossing:markings": "yes"}, {"crossing": "marked"}, {"crossing": "uncontrolled"},
                     {"crossing": "island", "crossing:markings": "zebra"}, {"crossing:markings": "zebra:double"},
                     {"crossing": "uncontrolled", "crossing_ref": "zebra", "crossing:markings": "zebra"}):
            self.assertEqual(secrossings.de_paint(tags), secrossings.M15, tags)

    def test_unmarked_or_signalled_is_nothing(self):
        for tags in ({}, {"highway": "crossing"}, {"crossing": "island"}, {"crossing": "unmarked"},
                     {"crossing": "traffic_signals"}, {"crossing": "traffic_signals", "crossing:markings": "zebra"},
                     {"crossing": "traffic_signals", "crossing_ref": "zebra"}, {"crossing": "informal"},
                     {"crossing": "no"}, {"crossing": "uncontrolled", "crossing:markings": "no"},
                     {"crossing": "marked", "crossing:signals": "yes"}, {"crossing:markings": "dots"},
                     {"crossing:markings": "surface"}, {"crossing:markings": "lines"}):
            self.assertEqual(secrossings.de_paint(tags), 0, tags)

    def test_nodes_carry_germanys_codes(self):
        """nodes' de_crossings: every crossing (its kerbs are lowered, painted or not), Z 293 where de_paint says and no
        signal is mapped within SIGNALS_NEAR_M; Sweden's crossings as they were."""
        got = secrossings.nodes(Frame(), [Infra([
            ("crossing", (1.0, 2.0), {}),
            ("crossing", (3.0, 4.0), {"crossing": "zebra"}),
            ("crossing", (5.0, 6.0), {"crossing": "island"}),
            ("crossing", (7.0, 8.0), {"crossing": "traffic_signals"}),
            ("crossing", (20.0, 20.0), {"crossing:markings": "zebra"}),
            ("traffic_signals", (20.0, 21.0), {})])])
        self.assertEqual(got["de_crossings"], [(10.0, 20.0, 0), (30.0, 40.0, secrossings.M15), (50.0, 60.0, 0),
                                               (70.0, 80.0, 0), (200.0, 200.0, 0)])
        self.assertEqual(got["crossings"], [(10.0, 20.0, secrossings.M15), (30.0, 40.0, secrossings.M15),
                                            (50.0, 60.0, secrossings.M15), (70.0, 80.0, secrossings.M15),
                                            (200.0, 200.0, secrossings.M15)])


class Infra:
    """An infrastructure Features of points."""
    def __init__(self, rows):
        n = len(rows)
        self.ids = np.array([str(i) for i in range(n)], object)
        self.attrs = {"class": np.array([r[0] for r in rows], object),
                      "subtype": np.array(["transportation"] * n, object),
                      "tags": np.array([json.dumps(r[2]) for r in rows], object)}
        self.lonlat = np.array([r[1] for r in rows], np.float64)
        self.coord_off, self.ring_off, self.part_off = np.arange(n + 1), np.arange(n + 1), np.arange(n + 1)

    def __len__(self):
        return len(self.ids)


class Frame:
    def geodetic_to_enu(self, lat, lon, h):
        return np.asarray(lon) * 10.0, np.asarray(lat) * 10.0, 0.0


class NodesTest(unittest.TestCase):
    def test_reads_each_kind(self):
        got = secrossings.nodes(Frame(), [Infra([
            ("crossing", (1.0, 2.0), {"crossing": "unmarked"}),
            ("give_way", (3.0, 4.0), {"direction": "forward"}),
            ("stop", (5.0, 6.0), {"road_marking": "stop_line"}),
            ("traffic_signals", (7.0, 8.0), {"road_marking": "stop_line"}),
            ("traffic_signals", (9.0, 9.0), {}),
            ("bus_stop", (9.0, 1.0), {})])])
        self.assertEqual(got["crossings"], [(10.0, 20.0, 0)])
        np.testing.assert_allclose(got["priority"], [[30.0, 40.0, 0.0], [50.0, 60.0, 1.0]])
        np.testing.assert_allclose(got["stop_lines"], [[50.0, 60.0], [70.0, 80.0]])

    def test_a_node_in_two_cells_comes_once(self):
        cell = Infra([("crossing", (1.0, 2.0), {})])
        self.assertEqual(len(secrossings.nodes(Frame(), [cell, cell])["crossings"]), 1)

    def test_nothing_read_means_guess(self):
        self.assertIsNone(secrossings.nodes(Frame(), [None, None]))
        self.assertIsNone(secrossings.nodes(Frame(), []))
        cell = Infra([("crossing", (1.0, 2.0), {})])
        del cell.attrs["tags"]
        self.assertIsNone(secrossings.nodes(Frame(), [cell]))


class PlaceTest(unittest.TestCase):
    def test_kerb_to_kerb_square_to_the_road(self):
        walks, squares, ramps = secrossings.place([road("a")], found((50.0, 1.0, secrossings.M15)))
        self.assertEqual(len(walks), 1)
        np.testing.assert_allclose(walks[0]["a"], [50.0, 4.0, 0.0])
        np.testing.assert_allclose(walks[0]["b"], [50.0, -4.0, 0.0])
        self.assertEqual(walks[0]["w"], 3.0)
        self.assertEqual(squares, [])
        self.assertEqual(set(ramps), {("a", 1), ("a", -1)})

    def test_unmarked_is_kerbs_and_no_paint(self):
        walks, squares, ramps = secrossings.place([road("a")], found((50.0, 0.0, 0)))
        self.assertEqual((walks, squares), ([], []))
        self.assertEqual(len(ramps), 2)

    def test_long_on_a_fast_road(self):
        walks, _, _ = secrossings.place([road("a", kmh=70.0)], found((50.0, 0.0, secrossings.M15)))
        self.assertEqual(walks[0]["w"], 4.0)        # TSFS 2010:171 4 kap. 9 §
        walks, _, _ = secrossings.place([road("a", kmh=60.0)], found((50.0, 0.0, secrossings.M15)))
        self.assertGreaterEqual(walks[0]["w"], 2.5)

    def test_off_the_road_or_on_gravel_or_a_motorway_is_nothing(self):
        at = found((50.0, 3.0, secrossings.M15))
        self.assertEqual(secrossings.place([road("a")], at)[0], [])
        at = found((50.0, 0.0, secrossings.M15))
        self.assertEqual(secrossings.place([road("a", kind="gravel")], at)[0], [])
        self.assertEqual(secrossings.place([road("a", cls="motorway")], at)[0], [])

    def test_two_nodes_of_one_crossing(self):
        walks, _, _ = secrossings.place([road("a")], found((50.0, 0.0, secrossings.M15), (52.0, 0.0, secrossings.M15)))
        self.assertEqual(len(walks), 1)

    def test_a_node_on_a_junction_is_left_out(self):
        tee = [road("w", pts=((0.0, 0.0), (50.0, 0.0))), road("e", pts=((50.0, 0.0), (100.0, 0.0))),
               road("n", pts=((50.0, 0.0), (50.0, 60.0)))]
        self.assertEqual(secrossings.place(tee, found((50.0, 0.0, secrossings.M15)))[0], [])
        self.assertEqual(len(secrossings.place(tee, found((50.0, 20.0, secrossings.M15)))[0]), 1)

    def test_a_way_split_is_no_junction(self):
        split = [road("w", pts=((0.0, 0.0), (50.0, 0.0))), road("e", pts=((50.0, 0.0), (100.0, 0.0)))]
        self.assertEqual(len(secrossings.place(split, found((50.0, 0.0, secrossings.M15)))[0]), 1)

    def test_m16_is_two_rows_of_half_metre_squares(self):
        walks, squares, _ = secrossings.place([road("a", width=8.0)], found((50.0, 0.0, secrossings.M16)))
        self.assertEqual(len(walks), 1)
        self.assertIs(walks[0]["paint"], False)
        self.assertEqual(len(squares) % 2, 0)
        self.assertEqual(len(squares), 16)              # (8 m of 1 m pitch, and half a gap fits nowhere)
        side = float(np.ptp(squares[0][:, 0]))
        self.assertAlmostEqual(side, 0.5)
        rows = sorted({round(float(q[:, 0].mean()), 3) for q in squares})
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[1] - rows[0] - side, 2.5)   # VGU's 2.5 m between them

    def test_zebra_and_cycle_crossing_side_by_side(self):
        walks, squares, _ = secrossings.place([road("a")], found((50.0, 0.0, secrossings.M15 | secrossings.M16)))
        self.assertEqual(len(walks), 2)
        zebra = next(w for w in walks if w.get("paint", True))
        band = next(w for w in walks if not w.get("paint", True))
        self.assertAlmostEqual(abs(zebra["a"][0] - band["a"][0]), (zebra["w"] + band["w"]) / 2)
        self.assertLess(zebra["a"][0], 50.0)
        self.assertGreater(band["a"][0], 50.0)


class StopLinesTest(unittest.TestCase):
    ARM = ("a", roads.Spec(8.0, "paved", True, "tertiary", False), np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]), 5.0,
           furniture.G_SIGNAL)

    def spot(self, walks=(), mapped=None):
        lines, cuts = markings.stop_lines([self.ARM], list(walks), mapped)
        self.assertEqual(len(lines), 1)
        return lines[0], cuts

    def test_a_metre_from_the_crossing_roads_edge(self):
        line, cuts = self.spot()
        self.assertAlmostEqual(line["a"][0], 6.0)
        self.assertEqual(line["w"], 0.40)               # TSFS 2010:171 3 kap. 4 §
        self.assertIs(line["stop"], True)
        self.assertEqual(len(cuts["a"]), 1)
        self.assertAlmostEqual(cuts["a"][0][2], 6.0)

    def test_on_the_half_coming_in(self):
        line, _ = self.spot()
        # (traffic comes in along -x, so on +y: right-hand traffic)
        self.assertGreater(min(line["a"][1], line["b"][1]), 0.0)

    def test_two_metres_before_a_crossing_at_signals(self):
        cw = {"a": np.array([10.0, 4.0, 0.0]), "b": np.array([10.0, -4.0, 0.0]), "w": 3.0}
        line, _ = self.spot([cw])
        self.assertAlmostEqual(line["a"][0], 13.5)      # VGU 2024 K237740

    def test_where_the_map_has_it(self):
        line, _ = self.spot(mapped=np.array([[20.0, 0.5]]))
        self.assertAlmostEqual(line["a"][0], 20.0)

    def test_room_for_a_car_before_a_crossing_at_a_stop_sign(self):
        arm = self.ARM[:4] + (furniture.G_STOP,)
        far = {"a": np.array([14.0, 4.0, 0.0]), "b": np.array([14.0, -4.0, 0.0]), "w": 3.0}
        near = {"a": np.array([9.0, 4.0, 0.0]), "b": np.array([9.0, -4.0, 0.0]), "w": 3.0}
        self.assertAlmostEqual(markings.stop_lines([arm], [far])[0][0]["a"][0], 6.0)     # (a car between them)
        self.assertAlmostEqual(markings.stop_lines([arm], [near])[0][0]["a"][0], 11.5)   # (before it)

    def test_none_on_a_stub(self):
        arm = ("a", self.ARM[1], np.array([[0.0, 0.0, 0.0], [7.0, 0.0, 0.0]]), 5.0, furniture.G_SIGNAL)
        self.assertEqual(markings.stop_lines([arm], [])[0], [])


class GiveWayLineTest(unittest.TestCase):
    def test_crossing_with_room_for_a_car_stays_behind_the_line(self):
        spec = roads.Spec(8.0, "paved", True, "tertiary", False)
        line = np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]])
        at = lambda x: {"a": np.array([x, 4.0, 0.0]), "b": np.array([x, -4.0, 0.0]), "w": 3.0}     # noqa
        for x, want in ((14.0, 6.0), (9.0, 11.5)):
            teeth, _ = markings.give_way_lines([("a", spec, line, 5.0)], [at(x)])
            self.assertAlmostEqual(max(float(t[:, 0].max()) for t in teeth) - markings.M14_H, want, msg=str(x))


class PriorityTest(unittest.TestCase):
    def arms(self):
        through = roads.Spec(8.0, "paved", True, "tertiary", False)
        side = roads.Spec(6.0, "paved", True, "tertiary", False)
        o = np.array([0.0, 0.0, 0.0])
        return [("w", through, np.array([-1.0, 0.0]), o, True, 200.0), ("e", through, np.array([1.0, 0.0]), o, True, 200.0),
                ("n", side, np.array([0.0, 1.0]), o, True, 200.0)]

    def controls(self, *rows):
        at = np.array(rows, np.float64).reshape(-1, 3) if rows else None
        return [(a[0], what) for a, what in furniture.junction_controls(self.arms(), None, signals=np.zeros((0, 2)),
                                                                         country="SE", priority=at)]

    def test_roads_of_a_class_have_nothing_without_a_sign(self):
        self.assertEqual(self.controls(), [])

    def test_the_maps_give_way_sign(self):
        self.assertEqual(self.controls((0.5, 10.0, 0.0)), [("n", furniture.G_YIELD)])

    def test_the_maps_stop_sign(self):
        self.assertEqual(self.controls((0.5, 10.0, 1.0)), [("n", furniture.G_STOP)])

    def test_a_sign_far_up_the_road_is_not_this_junctions(self):
        self.assertEqual(self.controls((0.5, 120.0, 0.0)), [])

    def test_a_sign_beside_the_arm_is_not_on_it(self):
        self.assertEqual(self.controls((20.0, 10.0, 0.0)), [])


class TileTest(unittest.TestCase):
    """Through build_tile: a street, with the junction's guesses gone and the map's paint in their place."""
    def tile(self, nodes, signals=None):
        import test_roads as tr
        feats = tr.features([[(-300, 2), (600, 2)], [(100, 2), (100, 300)]], ["tertiary", "residential"])
        urban = lambda la, lo: np.ones(np.shape(la), bool)      # noqa: E731
        return roads.build_tile(tr.FRAME, 0, 0, [feats], lambda a, b: np.zeros(np.shape(a)),
                                urban, walks=[], country="SE", mph=False, signals=signals, nodes=nodes)

    def test_no_paint_the_map_does_not_have(self):
        ch = self.tile({"crossings": [], "stop_lines": np.zeros((0, 2)), "priority": np.zeros((0, 3))},
                       signals=np.zeros((0, 2)))
        self.assertFalse([c for c in ch if c["m"] == "crossing_white"])
        self.assertFalse([c for c in ch if c["m"] == "line_white" and c.get("w") in (0.40, 0.45)])

    def test_a_mapped_crossing_is_m15_and_stops_the_lines(self):
        nodes = {"crossings": [(60.0, 2.0, secrossings.M15)], "stop_lines": np.zeros((0, 2)), "priority": np.zeros((0, 3))}
        ch = self.tile(nodes, signals=np.zeros((0, 2)))
        zebra = [c for c in ch if c["m"] == "crossing_white"]
        self.assertEqual(len(zebra), 1)
        self.assertEqual(zebra[0]["tl"], 4.0)
        self.assertEqual(zebra[0]["w"], 3.0)
        lines = [c for c in ch if c.get("n") and c["m"] == "line_white" and c.get("w", 0) < 0.3]
        self.assertTrue(lines)
        for c in lines:
            xs = np.array(c["n"])[:, 0]
            self.assertTrue(xs.max() <= 58.6 or xs.min() >= 61.4, c)

    def test_no_nodes_keeps_the_guess(self):
        ch = self.tile(None, signals=np.zeros((0, 2)))
        for c in ch:
            if c["m"] == "crossing_white":
                self.assertEqual(c["tl"], 4.8)


if __name__ == "__main__":
    unittest.main()
