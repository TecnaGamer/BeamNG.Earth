"""bridgetypes: a bridge's structure from a tagged outline, the NBI or the size rule; the truss beside its decks."""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridgetypes as bt  # noqa: E402
import geodesy  # noqa: E402
import roadmesh  # noqa: E402

LAT, LON = 45.0, 10.0
FRAME = geodesy.EnuFrame(LAT, LON)


def deck(x0=-150.0, x1=150.0, z=30.0, y=0.0, w=10.0):
    xs = np.arange(x0, x1 + 1e-6, 4.0)
    return (np.c_[xs, np.full(len(xs), y)], np.full(len(xs), z), w)


def db_with(*rows):
    """An inventory of rows (east m, north m, type, span) in FRAME."""
    e = np.array([r[0] for r in rows], np.float64)
    n = np.array([r[1] for r in rows], np.float64)
    la, lo, _ = FRAME.enu_to_geodetic(e, n, np.zeros(len(e)))
    o = np.argsort(la)
    return {"lat": np.asarray(la, np.float32)[o], "lon": np.asarray(lo, np.float32)[o],
            "type": np.array([r[2] for r in rows], np.uint8)[o], "span": np.array([r[3] for r in rows], np.float32)[o],
            "length": np.full(len(rows), 300.0, np.float32), "width": np.full(len(rows), 20.0, np.float32),
            "nav": np.array([r[4] if len(r) > 4 else 0.0 for r in rows], np.float32)[o]}


class Outline:
    """Infrastructure Features: one bridge outline (lon/lat ring) with tags."""
    def __init__(self, box, tags):
        x0, y0, x1, y1 = box
        e = np.array([x0, x1, x1, x0, x0], np.float64)
        n = np.array([y0, y0, y1, y1, y0], np.float64)
        la, lo, _ = FRAME.enu_to_geodetic(e, n, np.zeros(5))
        self.ring = np.c_[lo, la]
        self.kind = np.array([3], np.uint8)
        self.attrs = {"tags": np.array([json.dumps(tags)], object), "subtype": np.array(["bridge"], object)}

    def __len__(self):
        return 1

    def parts(self, i):
        return [[self.ring]]


class StructureTest(unittest.TestCase):
    def tearDown(self):
        bt._db = None

    def test_nbi_record_on_the_bridge(self):
        bt._db = db_with((20.0, 5.0, bt.NBI_THROUGH_TRUSS, 180.0))
        info = bt.chain_structure(FRAME, [deck()])
        self.assertEqual((info["type"], info["src"]), (bt.NBI_THROUGH_TRUSS, "nbi"))

    def test_inventory_without_a_record_is_plain(self):
        bt._db = db_with((5000.0, 0.0, bt.NBI_THROUGH_TRUSS, 180.0))      # 5 km off: covered, not this bridge
        self.assertIsNone(bt.chain_structure(FRAME, [deck()], lambda xy: np.ones(len(xy), bool)))

    def test_size_rule_outside_the_inventory(self):
        bt._db = db_with((90000.0, 0.0, bt.NBI_THROUGH_TRUSS, 180.0))
        wet = lambda xy: np.abs(xy[:, 0]) < 200                          # 400 m of water
        self.assertEqual(bt.chain_structure(FRAME, [deck(-300.0, 300.0)], wet)["src"], "size")
        self.assertIsNone(bt.chain_structure(FRAME, [deck(-300.0, 300.0)], lambda xy: np.abs(xy[:, 0]) < 100))

    def test_size_rule_not_on_a_network(self):
        """A city's elevated network is one long chain (Tokyo's: 76 km): no truss for its runs over water."""
        bt._db = db_with((90000.0, 0.0, bt.NBI_THROUGH_TRUSS, 180.0))
        wet = lambda xy: np.abs(xy[:, 0]) < 200
        self.assertEqual(bt.chain_structures(FRAME, [deck(-5000.0, 5000.0)], wet), [])

    def test_wikidata_corrects_the_tag(self):
        """OSM calls Tokyo's Rainbow Bridge cable-stayed; Wikidata, a suspension bridge with a 570 m span."""
        bt._db = db_with((90000.0, 0.0, 1, 0.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 5000.0]), np.array([0.0, 0.0]), np.zeros(2))
        bt._wd = {"lat": np.asarray(la, np.float32), "lon": np.asarray(lo, np.float32),
                  "type": np.array([bt.NBI_SUSPENSION, bt.PLAIN], np.uint8),
                  "span": np.array([570.0, 0.0], np.float32), "length": np.array([800.0, 0.0], np.float32)}
        try:
            o = Outline((-160, -10, 160, 10), {"bridge:structure": "cable-stayed"})
            got = bt.chain_structures(FRAME, [deck()], None, [o])
            self.assertEqual([(g["type"], g["span"]) for g in got], [(bt.NBI_SUSPENSION, 570.0)])
            # a Wikidata girder bridge: a plain deck, and no size rule there
            wet = lambda xy: np.abs(xy[:, 0] - 5000.0) < 200
            self.assertEqual(bt.chain_structures(FRAME, [deck(4700.0, 5300.0)], wet), [])
        finally:
            bt._wd = None

    def test_wikidata_of_another_kind_does_not_replace_the_tag(self):
        """GitHub #65: Wikidata's Port Mann Bridge kept the 1964 tied arch (366 m span) over the 2012 cable-stayed
        bridge the map tags: the map's kind stands, without Wikidata's span."""
        bt._db = db_with((90000.0, 0.0, 1, 0.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([0.0]), np.zeros(1))
        bt._wd = {"lat": np.asarray(la, np.float32), "lon": np.asarray(lo, np.float32),
                  "type": np.array([bt.NBI_THROUGH_ARCH], np.uint8),
                  "span": np.array([366.0], np.float32), "length": np.array([2093.0], np.float32)}
        try:
            o = Outline((-160, -10, 160, 10), {"bridge:structure": "cable-stayed"})
            got = bt.chain_structures(FRAME, [deck()], None, [o])
            self.assertEqual([(g["type"], g["span"]) for g in got], [(bt.NBI_STAYED, 0.0)])
        finally:
            bt._wd = None

    def test_members_keep_clear_of_the_lanes(self):
        """GitHub #65: a stay reaching across the other carriageway down to car height is dropped; one over the
        deck's edge or high over it stays."""
        P = [(np.c_[np.arange(0.0, 101.0, 4.0), np.zeros(26)], np.full(26, 30.0), 12.0)]
        low = (np.array([50.0, 20.0, 80.0]), np.array([50.0, -2.0, 32.0]), 0.25, 0.25)     # into the lanes at 2 m
        high = (np.array([10.0, 20.0, 80.0]), np.array([10.0, -20.0, 80.0]), 0.25, 0.25)
        edge = (np.array([90.0, 20.0, 80.0]), np.array([90.0, 6.0, 30.0]), 0.25, 0.25)    # anchored at the edge
        kept = bt._clear_of_decks([low, high, edge], P)
        self.assertEqual([b[0][0] for b in kept], [10.0, 90.0])

    def test_each_tagged_outline_its_own_site(self):
        bt._db = db_with((90000.0, 0.0, 1, 0.0))
        a = Outline((-1900, -10, -1500, 10), {"bridge:structure": "suspension", "colour": "orange"})
        b = Outline((1500, -10, 1900, 10), {"bridge:structure": "truss"})
        got = bt.chain_structures(FRAME, [deck(-2000.0, 2000.0)], None, [a, b])
        self.assertEqual(sorted(g["type"] for g in got), sorted([bt.NBI_SUSPENSION, bt.NBI_THROUGH_TRUSS]))
        self.assertEqual({g["type"]: g["paint"] for g in got}[bt.NBI_SUSPENSION], "orange")
        beams, _ = bt.structure_members([deck(-2000.0, 2000.0)], [g for g in got if g["type"] == bt.NBI_SUSPENSION][0],
                                        lambda xy: np.zeros(len(xy)))
        xs = np.array([p[0] for bm in beams for p in bm[:2]])
        self.assertTrue(len(xs) and xs.max() < -1000.0, "the suspension span on its own outline")

    def test_osm_tag_first(self):
        bt._db = db_with((20.0, 5.0, bt.NBI_THROUGH_TRUSS, 180.0))
        beam = Outline((-160, -10, 160, 10), {"man_made": "bridge", "bridge:structure": "beam"})
        self.assertIsNone(bt.chain_structure(FRAME, [deck()], None, [beam]))
        bt._db = db_with((90000.0, 0.0, 1, 0.0))
        truss = Outline((-160, -10, 160, 10), {"man_made": "bridge", "bridge:structure": "truss"})
        self.assertEqual(bt.chain_structure(FRAME, [deck()], None, [truss])["type"], bt.NBI_THROUGH_TRUSS)


class NavTest(unittest.TestCase):
    def tearDown(self):
        bt._db = None

    def test_nav_clearance_of_the_bridge(self):
        bt._db = db_with((20.0, 5.0, 2, 60.0, 41.1), (3000.0, 0.0, 2, 60.0, 9.0))
        self.assertAlmostEqual(bt.nav_clearance(FRAME, [deck()[0]]), 41.1, places=3)
        self.assertEqual(bt.nav_clearance(FRAME, [deck(y=500.0)[0]]), 0.0)

    def test_girder_record_is_not_a_truss(self):
        bt._db = db_with((20.0, 5.0, 2, 60.0, 41.1))
        info = bt.chain_structure(FRAME, [deck()])
        self.assertEqual(bt.truss_members([deck()], info, lambda xy: np.zeros(len(xy))), [])


class TrussTest(unittest.TestCase):
    def test_through_truss_beside_the_decks(self):
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 180.0}
        parts = [deck(y=0.0, w=10.0), deck(y=12.0, w=8.0)]            # two carriageways side by side
        beams = bt.truss_members(parts, info, lambda xy: np.zeros(len(xy)))
        self.assertGreater(len(beams), 40)
        ends = np.array([p for b in beams for p in b[:2]])
        upright = [b for b in beams if abs(b[0][0] - b[1][0]) < 1e-6 and abs(b[0][1] - b[1][1]) < 1e-6]
        ys = np.array([b[0][1] for b in upright])
        self.assertTrue(((ys < -5.0 - roadmesh.PARAPET_W) | (ys > 16.0 + roadmesh.PARAPET_W)).all(),
                        "posts outside both decks, none between them")
        self.assertGreaterEqual(ends[:, 2].max(), 30.0 + bt.HEADROOM_M - 1e-6)

    def test_plain_or_low_deck_no_truss(self):
        self.assertEqual(bt.truss_members([deck()], None, lambda xy: np.zeros(len(xy))), [])
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 180.0}
        self.assertEqual(bt.truss_members([deck(z=3.0)], info, lambda xy: np.zeros(len(xy))), [])

    def test_big_truss_over_the_water_only(self):
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 100.0}
        long = deck(-600.0, 600.0)
        beams = bt.truss_members([long], info, lambda xy: np.zeros(len(xy)), lambda xy: np.abs(xy[:, 0]) < 100)
        xs = np.array([p[0] for b in beams for p in b[:2]])
        self.assertLessEqual(np.abs(xs).max(), 100.0 + 100.0 + bt.PANEL_M)

    def test_anchor_piers_off_the_roads(self):
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 100.0}
        long = deck(-600.0, 600.0)
        wet = lambda xy: np.abs(xy[:, 0]) < 100
        beams, piers = bt.truss_members([long], info, lambda xy: np.zeros(len(xy)), wet, with_piers=True)
        self.assertEqual(len(piers), 2)
        x_end = piers[0][0]
        road = [np.array([[x_end, -50.0], [x_end, 50.0]])]          # a street under that end
        _, moved = bt.truss_members([long], info, lambda xy: np.zeros(len(xy)), wet, with_piers=True,
                                    road_lines=road)
        self.assertTrue(all(abs(p[0] - x_end) > bt.ROAD_GAP_M for p in moved))

    def test_mesh(self):
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 180.0}
        beams = bt.truss_members([deck()], info, lambda xy: np.zeros(len(xy)))
        P, T, UV, mats, M = roadmesh.deck([], (0.0, 0.0, 0.0), beams=beams)
        self.assertIn(roadmesh.TRUSS_STEEL, mats)
        self.assertEqual(len(T), 8 * len(beams))
        self.assertEqual(len(P), 8 * len(beams))


class SpanTest(unittest.TestCase):
    wet = staticmethod(lambda xy: np.abs(xy[:, 0]) < 200)                # 400 m of water under the middle
    flat = staticmethod(lambda xy: np.zeros(len(xy)))

    def test_through_arch_over_the_water(self):
        beams, _ = bt.structure_members([deck(-400.0, 400.0, z=40.0)], {"type": bt.ARCH_ANY, "span": 0.0,
                                                                         "src": "osm"}, self.flat, self.wet)
        z = np.array([p[2] for b in beams for p in b[:2]])
        self.assertGreater(z.max(), 40.0 + bt.THROUGH_TOP * 400 - 5)   # the crown over the deck
        x = np.array([p[0] for b in beams for p in b[:2]])
        self.assertLess(np.abs(x).max(), 0.6 * bt.ARCH_SPAN_SHARE * 400 + 5)

    def test_deck_arch_under_a_high_deck(self):
        beams, _ = bt.structure_members([deck(-400.0, 400.0, z=200.0)], {"type": bt.ARCH_ANY, "span": 0.0,
                                                                          "src": "osm"}, self.flat, self.wet)
        z = np.array([p[2] for b in beams for p in b[:2]])
        self.assertLess(z.max(), 200.0)

    def test_suspension_and_stayed_towers(self):
        for kind, share in ((bt.NBI_SUSPENSION, bt.SUSP_TOWER), (bt.NBI_STAYED, bt.STAYED_TOWER)):
            beams, _ = bt.structure_members([deck(-600.0, 600.0, z=50.0)], {"type": kind, "span": 0.0, "src": "osm"},
                                            self.flat, self.wet)
            self.assertGreater(len(beams), 20)
            z = np.array([p[2] for b in beams for p in b[:2]])
            L = bt.SPAN_SHARE * 400
            self.assertAlmostEqual(z.max(), 50.0 + share * L, delta=0.05 * share * L + 1.0)

    def test_plain_draws_nothing(self):
        self.assertEqual(bt.structure_members([deck()], None, self.flat, self.wet), ([], []))
        self.assertEqual(bt.structure_members([deck()], {"type": bt.PLAIN, "span": 50.0, "src": "nbi"}, self.flat,
                                              self.wet), ([], []))


if __name__ == "__main__":
    unittest.main()


def test_tower_spans_a_sparse_twin_carriageway():
    # a suspension span's ways can run a kilometre between nodes: the twin is beside it at the tower, though its
    # nearest node is 500 m off (a leg stood in its lanes on 25 de Abril and the Bay Bridge)
    import numpy as np
    import bridgetypes as bt
    a = (np.array([[0.0, 0.0], [1000.0, 0.0]]), np.array([60.0, 60.0]), 12.0)
    b = (np.array([[0.0, 20.0], [1000.0, 20.0]]), np.array([60.0, 60.0]), 12.0)
    P, arcs, tans = bt._prep([a, b])
    _, _, _, lo, hi, _, _ = bt._station(P, tans, 0, 500.0, arcs)
    assert lo < -6.0 and hi > 26.0
