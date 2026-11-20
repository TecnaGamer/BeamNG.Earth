"""osmml.py: OpenStreetMap over machine-learned building outlines (earthfast osm_over_ml), and mapgeom.take."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapcells  # noqa: E402
import mapgeom  # noqa: E402
import osmml  # noqa: E402

KEY = mapcells.CellKey(14, 4706, 6244)          # (Fells Point, Baltimore)
LON0, LAT0, LON1, LAT1 = KEY.bounds()
KX = math.cos(math.radians((LAT0 + LAT1) / 2)) * 111320.0


def ring(e, n, w, h=None):
    """A closed lon/lat rectangle w x h m at (e, n) m from the cell's south-west corner."""
    h = w if h is None else h
    pts = [(e, n), (e + w, n), (e + w, n + h), (e, n + h), (e, n)]
    return np.array([[LON0 + x / KX, LAT0 + y / 110540.0] for x, y in pts])


def layer(items):
    """A buildings layer of [(ring, dataset)], ids in order."""
    n = len(items)
    rings = [r for r, _ in items]
    coord_off = np.r_[0, np.cumsum([len(r) for r in rings])].astype(np.int64)
    f = mapgeom.Features("buildings", np.array([f"id{i:04d}" for i in range(n)]), np.full(n, mapgeom.POLYGON, np.uint8),
                         np.arange(n + 1, dtype=np.int64), np.arange(n + 1, dtype=np.int64), coord_off,
                         np.concatenate(rings), own=np.ones(n, bool))
    f.attrs["src_dataset"] = mapgeom.text_column([d for _, d in items])
    f.attrs["height"] = np.arange(n, dtype=np.float32)
    return f


OSM, MS = "OpenStreetMap", "Microsoft ML Buildings"


def street(n=10, e=500.0, north=500.0):
    """n mapped rowhouses in a row."""
    return [(ring(e + 20.0 * k, north, 12.0), OSM) for k in range(n)]


class OsmOverMlTest(unittest.TestCase):

    def test_ghost_on_a_mapped_street_dropped(self):
        f = layer(street() + [(ring(600.0, 540.0, 10.0), MS)])
        v = osmml.verdicts(f, KEY)
        self.assertEqual(v.tolist(), [0] * 10 + [osmml.COVERED])
        g = osmml.apply(f, KEY)
        self.assertEqual(len(g), 10)
        self.assertEqual(g.ids.tolist(), f.ids[:10].tolist())
        self.assertTrue(np.array_equal(g.lonlat, f.lonlat[:50]))

    def test_small_outline_behind_a_house_kept(self):
        """A garden shed (at most 40 m^2) within 20 m of a mapped house stays (OSM seldom maps them); one on a cleared
        lot, a 49 m^2 ghost, and a small duplicate of a mapped house are dropped."""
        f = layer(street() + [(ring(504.0, 518.0, 5.0), MS), (ring(560.0, 580.0, 5.0), MS),
                              (ring(520.0, 520.0, 7.0), MS), (ring(503.0, 503.0, 5.0), MS)])
        self.assertEqual(osmml.verdicts(f, KEY).tolist(),
                         [0] * 10 + [osmml.KEPT, osmml.COVERED, osmml.COVERED, osmml.DUPLICATE])

    def test_sparse_osm_keeps_ml(self):
        # a town nobody traced: 3 mapped houses, 20 learned ones
        items = street(3) + [(ring(500.0 + 20.0 * (k % 5), 530.0 + 20.0 * (k // 5), 12.0), MS) for k in range(20)]
        f = layer(items)
        self.assertFalse(osmml.verdicts(f, KEY).any())
        self.assertIs(osmml.apply(f, KEY), f)

    def test_unmapped_complex_beside_mapped_houses_kept(self):
        # Phoenix: a street of mapped houses beside an apartment complex only Microsoft has (OSM under 70 % of the area)
        f = layer(street() + [(ring(500.0 + 50.0 * k, 560.0, 40.0), MS) for k in range(4)])
        self.assertFalse(osmml.verdicts(f, KEY).any())

    def test_far_from_the_mapped_street_kept(self):
        # the window is 325 m: an outline 600 m off has no OSM round it
        f = layer(street() + [(ring(1300.0, 1300.0, 10.0), MS)])
        self.assertFalse(osmml.verdicts(f, KEY).any())

    def test_duplicate_dropped_even_where_sparse(self):
        f = layer([(ring(300.0, 300.0, 20.0), OSM), (ring(305.0, 305.0, 10.0), MS), (ring(900.0, 900.0, 10.0), MS)])
        self.assertEqual(osmml.verdicts(f, KEY).tolist(), [0, osmml.DUPLICATE, 0])

    def test_small_osm_does_not_erase_a_large_ml_building(self):
        """A sparse cell: a small OSM entrance shed at a large ML warehouse's edge, and one inside it, cover little of
        its footprint: the warehouse stays (Codex, 2026-11-18: either vertex mean in the other dropped it)."""
        f = layer([(ring(300.0, 300.0, 100.0), MS), (ring(297.0, 340.0, 6.0), OSM), (ring(345.0, 345.0, 10.0), OSM)])
        self.assertEqual(osmml.verdicts(f, KEY).tolist(), [0, 0, 0])

    def test_duplicate_needs_half_the_ml_footprint(self):
        """An ML outline 60 % on an OSM one is a duplicate, 40 % is not; a row of OSM rowhouses under one ML outline
        covers it together."""
        f = layer([(ring(300.0, 300.0, 20.0), OSM), (ring(308.0, 300.0, 20.0), MS)])
        self.assertEqual(osmml.verdicts(f, KEY).tolist(), [0, osmml.DUPLICATE])
        f = layer([(ring(300.0, 300.0, 20.0), OSM), (ring(312.0, 300.0, 20.0), MS)])
        self.assertEqual(osmml.verdicts(f, KEY).tolist(), [0, 0])

    def test_courtyard_building_no_duplicate(self):
        """An ML building in a mapped block's courtyard (the block's hole) shares no footprint with it."""
        outer, hole = ring(300.0, 300.0, 60.0), ring(310.0, 310.0, 40.0)
        yard = ring(325.0, 325.0, 10.0)
        ll = np.concatenate([outer, hole, yard])
        f = mapgeom.Features("buildings", np.array(["a", "b"]), np.full(2, mapgeom.POLYGON, np.uint8),
                             np.array([0, 1, 2]), np.array([0, 2, 3]), np.array([0, 5, 10, 15]), ll,
                             own=np.ones(2, bool))
        f.attrs["src_dataset"] = mapgeom.text_column([OSM, MS])
        self.assertEqual(osmml.verdicts(f, KEY).tolist(), [0, 0])

    def test_owner_cell_judges_a_straddling_outline(self):
        """Only the cell an outline's box centre is in judges it: its copy in a neighbour cell is left out, so a
        reader of both cells gets the owner's verdict whichever copy it meets first."""
        items = street(3) + [(ring(600.0, 540.0, 10.0), MS)]
        f = layer(items)
        self.assertEqual(osmml.verdicts(f, KEY).tolist(), [0, 0, 0, 0])      # (owner: OSM sparse here, kept)
        f.own = np.array([True, True, True, False])
        self.assertEqual(osmml.verdicts(f, KEY).tolist(), [0, 0, 0, osmml.ELSEWHERE])
        self.assertEqual(len(osmml.apply(f, KEY)), 3)

    def test_other_sources_neither_dropped_nor_counted(self):
        f = layer(street() + [(ring(600.0, 540.0, 10.0), "Esri Community Maps")])
        self.assertFalse(osmml.verdicts(f, KEY).any())
        # Esri's large outlines do not lower OSM's share either
        f = layer(street() + [(ring(500.0 + 50.0 * k, 600.0, 40.0), "Esri Community Maps") for k in range(4)]
                  + [(ring(600.0, 540.0, 10.0), MS)])
        self.assertEqual(int(osmml.verdicts(f, KEY)[-1]), osmml.COVERED)

    def test_order_free(self):
        """The verdict of a row is the same whichever order the rows come in (ids sort the layer: only the rows'
        own data counts)."""
        items = street() + [(ring(600.0, 540.0, 10.0), MS), (ring(300.0, 300.0, 20.0), OSM),
                            (ring(305.0, 305.0, 10.0), MS), (ring(1300.0, 1300.0, 10.0), MS)]
        want = osmml.verdicts(layer(items), KEY).tolist()
        rng = np.random.default_rng(3)
        for _ in range(5):
            p = rng.permutation(len(items))
            got = osmml.verdicts(layer([items[i] for i in p]), KEY).tolist()
            self.assertEqual([got[int(np.flatnonzero(p == i)[0])] for i in range(len(items))], want)

    def test_switch_off(self):
        f = layer(street() + [(ring(600.0, 540.0, 10.0), MS)])
        on = osmml.ON
        try:
            osmml.ON = False
            self.assertIs(osmml.apply(f, KEY), f)
        finally:
            osmml.ON = on

    def test_bad_offsets_raise(self):
        f = layer(street(2))
        with self.assertRaises(ValueError):
            osmml._ef.osm_over_ml(f.lonlat, f.part_off, f.ring_off, np.array([0, 5, 99], np.int64),
                                  np.array([1, 2], np.int8), np.ones(2, bool), KEY.bounds())
        with self.assertRaises(ValueError):
            osmml._ef.osm_over_ml(f.lonlat, f.part_off, f.ring_off, f.coord_off, np.array([1, 2], np.int8),
                                  np.ones(3, bool), KEY.bounds())


class MlTileCentreTest(unittest.TestCase):
    """buildings.ml_box_centres: an ML outline's tile is its box centre's (the centre its cell owns it by), not its
    ring mean's, which an asymmetric outline puts metres away (past a cell edge, in a tile that reads only the cell
    whose copy osmml leaves out)."""

    def test_box_centre_for_ml_only(self):
        import buildings
        import geodesy
        # an L of 40 x 40 m with a long arm: its ring mean (16, 16) is ~6 m off its box centre
        pts = [(0, 0), (40, 0), (40, 8), (8, 8), (8, 40), (0, 40), (0, 0)]
        ll = np.array([[LON0 + x / KX + 0.001, LAT0 + y / 110540.0 + 0.001] for x, y in pts])
        f = mapgeom.Features("buildings", np.array(["a", "b"]), np.full(2, mapgeom.POLYGON, np.uint8),
                             np.array([0, 1, 2]), np.array([0, 1, 2]), np.array([0, 7, 14]), np.concatenate([ll, ll]),
                             own=np.ones(2, bool))
        f.attrs["src_dataset"] = mapgeom.text_column([MS, OSM])
        frame = geodesy.EnuFrame(float(ll[0, 1]), float(ll[0, 0]))
        cx, cy = buildings.ml_box_centres(f, frame)
        self.assertTrue(np.isnan(cx[1]) and np.isnan(cy[1]))
        self.assertAlmostEqual(float(cx[0]), 20.0, delta=0.5)      # (the test's metres per degree are round)
        self.assertAlmostEqual(float(cy[0]), 20.0, delta=0.5)
        rx, ry = f.outer_centres(frame)
        self.assertGreater(abs(float(rx[0]) - 20.0), 3.0)     # (the ring mean is elsewhere: 16 m)


class TakeTest(unittest.TestCase):

    def test_rows_attrs_rules_and_multiparts(self):
        a = ring(0, 0, 10)
        b1, b2 = ring(100, 0, 10), ring(200, 0, 10)
        c = ring(300, 0, 10)
        ll = np.concatenate([a, b1, b2, c])
        f = mapgeom.Features("buildings", np.array(["a", "b", "c"]), np.full(3, mapgeom.POLYGON, np.uint8),
                             np.array([0, 1, 3, 4]), np.array([0, 1, 2, 3, 4]), np.array([0, 5, 10, 15, 20]), ll,
                             en=ll * 2, own=np.array([True, False, True]), clipped=np.array([False, True, False]))
        f.attrs["height"] = np.array([1.0, 2.0, 3.0], np.float32)
        f.attrs["name"] = mapgeom.text_column(["x", "y", "z"])
        f.rules["flags"] = mapgeom.RuleTable(np.array([0, 1, 2, 2], np.int32), mapgeom.text_column(["p", "q", "r", "s"]),
                                             np.zeros(4, np.float32), np.ones(4, np.float32))
        g = mapgeom.take(f, [False, True, True])
        self.assertEqual(g.ids.tolist(), ["b", "c"])
        self.assertEqual(g.part_off.tolist(), [0, 2, 3])
        self.assertEqual(g.ring_off.tolist(), [0, 1, 2, 3])
        self.assertEqual(g.coord_off.tolist(), [0, 5, 10, 15])
        self.assertTrue(np.array_equal(g.lonlat, np.concatenate([b1, b2, c])))
        self.assertTrue(np.array_equal(g.en, np.concatenate([b1, b2, c]) * 2))
        self.assertEqual(g.own.tolist(), [False, True])
        self.assertEqual(g.clipped.tolist(), [True, False])
        self.assertEqual(g.attrs["height"].tolist(), [2.0, 3.0])
        self.assertEqual(g.attrs["name"].tolist(), ["y", "z"])
        self.assertEqual(g.rules["flags"].feature.tolist(), [0, 1, 1])
        self.assertEqual(g.rules["flags"].value.tolist(), ["q", "r", "s"])
        self.assertEqual([np.asarray(r).tolist() for r in g.parts(0)[1]], [b2.tolist()])
        self.assertIs(mapgeom.take(f, [True, True, True]), f)
        self.assertEqual(len(mapgeom.take(f, [False, False, False])), 0)


if __name__ == "__main__":
    unittest.main()
