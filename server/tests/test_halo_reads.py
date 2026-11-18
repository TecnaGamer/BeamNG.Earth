"""The cells round a cell read for its road facts (2026-11-11, a cold first block's halo of decoded cells): only the
neighbours a fact can depend on are read (neighbours_hook's want), and nothing is read where the answer is known
without them: a lane flow with no counted road, a road's home cell told by the eight cells' names, the cells a road
reaching out of the cell does not reach. The answers stay what reading all eight gave."""
import itertools
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gradesep  # noqa: E402
import mapcells  # noqa: E402
import roads  # noqa: E402
from test_roads import LAT, LON, M_LON, features, rules  # noqa: E402

KEY = mapcells.cell_of(LAT, LON)
EAST = mapcells.CellKey(KEY.z, KEY.x + 1, KEY.y)


class Md:
    """MapData's cell() for neighbours_hook: `layers` {key: Features} (an empty layer elsewhere); every cell() asked is
    in `asked`."""

    def __init__(self, layers=None):
        self.layers, self.asked = dict(layers or {}), []

    def cell(self, key, layers=(), kind="demand"):
        self.asked.append(key)
        f = self.layers.get(key)
        if f is None:
            f = self.layers[key] = features([], [])
        f.__dict__.setdefault("_bnge_cell", (str(key), "roads", ()))
        return SimpleNamespace(layers={"roads": f})


_IDS = itertools.count()


def hooked(f, md, key=KEY):
    """f as cell key's roads layer with md's neighbours (a cell id of its own: roads._FACTS_MEM keeps facts by it)."""
    f.__dict__.update(_bnge_cell=(str(key), "roads", f"t{next(_IDS)}"),
                      _bnge_neighbours=gradesep.neighbours_hook(md, key))
    return f


def east_edge():
    return (KEY.bounds()[2] - LON) * M_LON


class HookWantTest(unittest.TestCase):
    def test_only_the_wanted_cells_are_read_and_the_rest_later(self):
        md = Md()
        hook = gradesep.neighbours_hook(md, KEY)
        got = hook({str(EAST)})
        self.assertEqual(md.asked, [EAST])
        self.assertEqual(sum(f is not None for f in got), 1)
        md.asked.clear()
        self.assertEqual(sum(f is not None for f in hook()), 8)
        self.assertEqual(len(md.asked), 7, "the east cell kept, not asked again")
        md.asked.clear()
        hook({str(EAST)})
        hook()
        self.assertEqual(md.asked, [])

    def test_names_are_the_eight_keys(self):
        hook = gradesep.neighbours_hook(Md(), KEY)
        self.assertEqual(hook.names, [str(k) for k in gradesep._neighbour_keys(KEY)])


class LaneFlowReadsTest(unittest.TestCase):
    def layer(self, oneway):
        f = features([[(-300, 0), (0, 0)], [(0, 0), (300, 0)]], ["motorway"] * 2, ids=["r0", "r1"])
        if oneway:
            f.rules["oneway"] = rules([(0, 1.0), (1, 1.0)], "float")
        return f

    def test_no_counted_road_reads_no_neighbour(self):
        md = Md()
        f = hooked(self.layer(oneway=False), md)
        self.assertEqual(roads.lane_flow(f), [None, None])
        self.assertEqual(md.asked, [])

    def test_counted_roads_read_the_eight(self):
        md = Md()
        f = hooked(self.layer(oneway=True), md)
        roads.lane_flow(f)
        self.assertEqual(len(md.asked), 8)

    def test_road_specs_without_a_counted_road_read_no_neighbour(self):
        md = Md()
        # (no regional class either: regional_unpaved reads the eight round a cell with one)
        f = hooked(features([[(-300, 0), (300, 0)], [(0, -300), (0, 300)]], ["secondary", "tertiary"]), md)
        plain = features([[(-300, 0), (300, 0)], [(0, -300), (0, 300)]], ["secondary", "tertiary"])
        self.assertEqual(roads.road_specs(f), roads.road_specs(plain))
        self.assertEqual(md.asked, [])

    def test_only_a_counted_roads_home_cell_is_read(self):
        # a one-way motorway starting in the east cell (its home) and an uncounted road starting there too
        e = east_edge()
        lines = [[(e + 200, 10), (e - 300, 10)], [(e + 200, -10), (e - 300, -10)]]
        md = Md()
        f = hooked(features(lines, ["motorway", "residential"], ids=["m", "r"]), md)
        f.rules["oneway"] = rules([(0, 1.0)], "float")
        east = features(lines, ["motorway", "residential"], ids=["m", "r"])
        east.rules["oneway"] = rules([(0, 1.0)], "float")
        md.layers[EAST] = hooked(east, md, EAST)
        roads.lane_flow(f)
        self.assertIn(EAST, md.asked)
        md2 = Md()
        f2 = hooked(features(lines, ["residential", "residential"], ids=["m", "r"]), md2)
        self.assertEqual(roads.lane_flow(f2), [None, None])
        self.assertEqual(md2.asked, [], "no counted road: neither the eight nor a home cell read")


class GradesepManyReadsTest(unittest.TestCase):
    def test_home_far_reads_nothing(self):
        md = Md()
        f = hooked(features([[(0, 0), (50, 0)]], ["residential"]), md)
        hook = f.__dict__["_bnge_neighbours"]
        self.assertFalse(roads._home_far(f, hook, str(EAST)))
        self.assertTrue(roads._home_far(f, hook, str(mapcells.CellKey(KEY.z, KEY.x + 2, KEY.y))))
        self.assertEqual(md.asked, [])

    def test_only_the_cells_a_road_reaches_are_read_and_their_spans_taken(self):
        e = east_edge()
        line = [[(e - 300.0, 0.0), (e + 300.0, 0.0)]]       # (home here, ends in the cell east of it)
        east = features(line, ["trunk"], ids=["ca1"])
        east.__dict__["_bnge_gradesep"] = lambda f: {0: [(0.6, 0.62, "bridge")]}
        md = Md({EAST: east})
        f = hooked(features(line, ["trunk"], ids=["ca1"]), md)
        f.__dict__["_bnge_gradesep"] = lambda f: {}
        self.assertEqual(roads._gradesep_many(f, [0]), {0: [(0.6, 0.62, "bridge")]})
        self.assertEqual(md.asked, [EAST])


if __name__ == "__main__":
    unittest.main()
