"""Lane-input reuse without changing native graph connectivity or home-cell decisions."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast
import mapcells
import mapgeom
import roads
from test_roads import features, rules


def cell(lines, lanes, ids=None, classes=None, directions=None, subtypes=None):
    f = features(lines, classes or ["motorway"] * len(lines), ids=ids, subtypes=subtypes)
    f.rules["oneway"] = rules(list(enumerate(directions or [1.0] * len(lines))), "float")
    f.attrs["osm_lanes"] = np.array([np.nan if n is None else n for n in lanes])
    return f


class LaneInputsTest(unittest.TestCase):
    def test_curved_and_reversed_lines_keep_endpoint_connectivity(self):
        # An interior point touches another road's endpoint; it must not form a node.
        f = cell([[(-300, 0), (-150, 90), (0, 0)], [(300, 0), (200, 60), (0, 0)],
                  [(-150, 90), (-150, 300)]], [4, None, None], directions=[1, -1, 1])
        self.assertEqual(roads._lane_flow_local(f), [None, 4, None])

    def test_short_features_cannot_block_a_node(self):
        f = cell([[(-300, 0), (0, 0)], [(0, 0), (300, 0)], [(0, 0)], []],
                 [4, None, None, None], classes=["motorway", "motorway", "residential", "residential"])
        with patch.object(fast._ef, "lane_flow", wraps=fast._ef.lane_flow) as solve:
            self.assertEqual(roads._lane_flow_local(f), [None, 4, None, None])
        np.testing.assert_array_equal(np.diff(solve.call_args.args[1]), [2, 2, 1, 0])
        self.assertEqual(roads._lane_flow_local(mapgeom.empty("roads")), [])

    def test_excluded_classes_subtypes_and_two_way_roads_still_block(self):
        for cls, sub, direction in [("residential", "road", 1), ("motorway", "footway", 1),
                                    ("motorway", "road", None)]:
            with self.subTest(cls=cls, sub=sub, direction=direction):
                f = cell([[(-300, 0), (0, 0)], [(0, 0), (300, 0)], [(0, -100), (0, 0)]],
                         [4, None, None], classes=["motorway", "motorway", cls],
                         subtypes=["road", "road", sub])
                f.rules["oneway"] = rules([(0, 1.0), (1, 1.0)] + ([(2, direction)] if direction else []), "float")
                self.assertEqual(roads._lane_flow_local(f), [None, None, None])

    def test_first_copy_wins_even_after_neighbour_preparation(self):
        home = cell([[(-300, 0), (0, 0)], [(0, 0), (300, 0)]], [4, None], ids=["feed", "out"])
        duplicate = cell([[(-300, 0), (0, 0)]], [2], ids=["feed"])
        roads._lane_flow_inputs(duplicate)
        home._bnge_neighbours = lambda: [duplicate, duplicate]
        self.assertEqual(roads._lane_flow_local(home), [None, 4])
        # The first copy within one source cell wins too; duplicate output rows stay unset.
        f = cell([[(-300, 0), (0, 0)], [(-300, 0), (0, 0)], [(0, 0), (300, 0)]],
                 [4, 2, None], ids=["feed", "feed", "out"])
        self.assertEqual(roads._lane_flow_local(f), [None, None, 4])

    def test_home_cell_solve_still_overrides_the_local_neighbourhood(self):
        local = cell([[(0, 0), (300, 0)], [(-300, 0), (0, 0)]], [None, 3], ids=["out", "feed"])
        home = cell([[(0, 0), (300, 0)]], [None], ids=["out"])
        beyond = cell([[(-300, 0), (0, 0)]], [4], ids=["feed"])
        key = str(mapcells.cell_of(home.lonlat[0, 1], home.lonlat[0, 0]))
        local._bnge_cell, home._bnge_cell = ("other", "roads", "test"), (key, "roads", "test")
        local._bnge_neighbours, home._bnge_neighbours = lambda: [home], lambda: [beyond]
        self.assertEqual(roads._lane_flow_local(local), [3, None])
        self.assertEqual(roads.lane_flow(local), [4, None])

    def test_shared_neighbour_attributes_are_parsed_once(self):
        upstream = cell([[(-300, 0), (0, 0)]], [4], ids=["feed"])
        a = cell([[(0, 0), (300, 0)]], [None], ids=["a"])
        b = cell([[(0, 0), (200, 50)]], [None], ids=["b"])
        a._bnge_neighbours = b._bnge_neighbours = lambda: [upstream]
        with patch.object(roads, "_osm", wraps=roads._osm) as parse:
            self.assertEqual(roads._lane_flow_local(a), [4])
            self.assertEqual(roads._lane_flow_local(b), [4])
        self.assertEqual(sum(c.args[0] is upstream for c in parse.call_args_list), 1)
        self.assertTrue(all(not v.flags.writeable for v in roads._lane_flow_inputs(upstream)))

    def test_no_unknown_mainline_count_does_not_read_neighbours(self):
        f = cell([[(-300, 0), (0, 0)], [(0, 0), (300, 0)], [(0, -100), (0, 0)], [(0, 0)]],
                 [4, None, None, None], classes=["motorway", "motorway", "residential", "motorway"])
        f.rules["subclass"] = rules([(1, "link")])
        with patch.object(f, "_bnge_neighbours", create=True, side_effect=AssertionError("unneeded I/O")) as near:
            self.assertEqual(roads._lane_flow_local(f), [None] * 4)
            near.assert_not_called()

    def test_new_cell_and_replaced_lane_tags_do_not_reuse_old_attributes(self):
        a = cell([[(-300, 0), (0, 0)], [(0, 0), (300, 0)]], [4, None])
        b = cell([[(-300, 0), (0, 0)], [(0, 0), (300, 0)]], [2, None])
        self.assertEqual(roads._lane_flow_local(a), [None, 4])
        self.assertEqual(roads._lane_flow_local(b), [None, 2])
        # LaneTags.join replaces this column when a new shard becomes available.
        c = cell([[(-300, 0), (0, 0)], [(0, 0), (300, 0)]], [4, None])
        roads._lane_flow_inputs(c)  # previously prepared as another cell's neighbour
        c.attrs["osm_lanes"] = np.array([2.0, np.nan])
        self.assertEqual(roads._lane_flow_local(c), [None, 2])


if __name__ == "__main__":
    unittest.main()
