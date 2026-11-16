"""Hong Kong's ground stretches on a structure (roadz.RoadZ._hk_gaps: the lidar's rule, test_lidargaps.py, read on
Hong Kong's DSM and DTM): a ground piece going on from a deck's measured end is deck as far as the DSM along it stays
well over the DTM; the pieces going on from its end are asked too (an elevated junction: untagged pieces in a row);
an at-grade end and another surface over the road stay ground; the planner asks Hong Kong's cells for the answers."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import mapcells  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402
import roadz  # noqa: E402
from test_roads import features, FRAME, LAT, LON  # noqa: E402
from test_roadz import FakeMDem  # noqa: E402

NATIVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= roadz.GAP_NATIVE
DTM = 10.0
KEY = mapcells.cell_of(LAT, LON)


def enu(lat, lon):
    """The fixture's HK1980 Grid: metres east / north of the test frame's origin."""
    e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat, np.float64), np.asarray(lon, np.float64), 0.0)
    return e, n


def north(lonlat):
    return float(enu(np.array([lonlat[1]]), np.array([lonlat[0]]))[1][0])


def provider(feats, surface):
    """A RoadZ over the map layer feats whose DSM is surface(north m) on the road (6 m either side of east 0) and the
    DTM beside it, the DTM flat."""
    md = FakeMDem(DTM)
    md.cell = lambda key, **_kw: SimpleNamespace(layers={"roads": feats})
    rz = roadz.RoadZ(None, object(), md)

    def dsm(x, y):
        return np.where(np.abs(np.asarray(x)) <= 6.0, surface(np.asarray(y, np.float64)), DTM)

    rz._hk_reader = lambda kind: dsm if kind == "dsm" else (lambda x, y: np.full(len(x), DTM))
    return rz


@unittest.skipUnless(NATIVE, "earthfast 107 (lidar_gap_stretch)")
class StretchTest(unittest.TestCase):
    """A viaduct p0 (north -100..0) 8 m up, tagged a bridge to north 0 only."""

    def setUp(self):
        patch = mock.patch.object(roadz.hkdtm, "grid", enu)
        patch.start()
        self.addCleanup(patch.stop)

    def gaps(self, lines, surface, flags=((0, "is_bridge", 0.0, 1.0),)):
        f = features(lines, ["motorway"] * len(lines), flags=list(flags), ids=[f"p{i}" for i in range(len(lines))])
        self.rows, gaps = provider(f, surface)._hk_gaps(KEY)
        return gaps

    def test_untagged_pieces_in_a_row(self):
        # the structure goes on 60 m over three drawn pieces (20 m, 6 m, the rest) and then down at 6 %; a street
        # leaves the second junction at grade
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 20)], [(0, 20), (0, 26)], [(0, 26), (0, 160)],
                         [(0, 26), (100, 26)]],
                        lambda y: DTM + 8.0 - 0.06 * np.clip(y - 60.0, 0.0, None))
        ends = {fid: north(b) for fid, _a, b in got}
        self.assertEqual(sorted(ends), ["p1", "p2", "p3"], "each piece going on from the one before; not the street")
        self.assertAlmostEqual(ends["p1"], 20.0, delta=0.1)
        self.assertAlmostEqual(ends["p2"], 26.0, delta=0.1, msg="a piece shorter than LIDAR_GAP_MIN_M, all of it")
        # (3 m over the DTM until north 60 + 5 / 0.06 = 143)
        self.assertGreater(ends["p3"], 130.0)
        self.assertLess(ends["p3"], 146.0)
        self.assertEqual([r[2] for r in self.rows], [1, 1, 1], "each stretch measured as a deck")
        z = np.concatenate([r[1] for r in self.rows])
        at = np.concatenate([enu(r[0][:, 1], r[0][:, 0])[1] for r in self.rows])
        np.testing.assert_allclose(z, DTM + 8.0 - 0.06 * np.clip(at - 60.0, 0.0, None), atol=0.3)

    def test_asked_again_once_a_stretch_ends_at_its_start(self):
        # another bridge p3 (not at the road's height) ends at the junction where the stretch p1 ends: p2, first asked
        # from p3's end alone, goes on from p1's
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 40)], [(0, 40), (0, 160)], [(-100, 40), (0, 40)]],
                        lambda y: np.full(len(y), DTM + 8.0),
                        flags=((0, "is_bridge", 0.0, 1.0), (3, "is_bridge", 0.0, 1.0)))
        self.assertEqual(sorted({fid for fid, _a, _b in got}), ["p1", "p2"])

    def test_two_decks_end_at_the_junction(self):
        # two measured deck ends at the candidate's start, 5 m apart in height: it goes on from the one at its height
        f = features([[(0, -100), (0, 0)], [(0, 0), (0, 160)]], ["motorway"] * 2, flags=[(0, "is_bridge", 0.0, 1.0)],
                     ids=["p0", "p1"])
        rz = provider(f, lambda y: np.full(len(y), DTM + 8.0))
        cands = rz._gap_candidates(KEY)
        got = rz._hk_stretches(cands, [(0.0, 0.0, DTM + 13.0), (0.0, 0.0, DTM + 8.0)], rz._hk_reader("dtm"),
                               rz._hk_reader("dsm"))
        self.assertEqual([g[0] for g in got], ["p1"])
        self.assertEqual(rz._hk_stretches(cands, [(0.0, 0.0, DTM + 13.0)], rz._hk_reader("dtm"),
                                          rz._hk_reader("dsm")), [], "no deck end at its height: another surface")

    def test_longer_than_the_reach(self):
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 500)]],
                        lambda y: np.where(y < 300.0, DTM + 8.0, DTM + 0.1))
        self.assertEqual({fid for fid, _a, _b in got}, {"p1"})
        self.assertAlmostEqual(max(north(b) for _f, _a, b in got), 300.0, delta=2.0 * roadz.LIDAR_STEP_M,
                               msg="asked again from where the reach ended")

    def test_at_grade_end_stays_ground(self):
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]], lambda y: np.full(len(y), DTM + 0.2))
        self.assertEqual(got, [])

    def test_another_surface_over_the_road_is_not_the_road(self):
        # a roof 10 m over the deck's end from there on: not the deck going on
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]],
                        lambda y: np.where(y < 0.0, DTM + 8.0, DTM + 18.0))
        self.assertEqual(got, [])

    def test_covered_road_not_read(self):
        # (the same surface as the pieces in a row, but the map has a cover over the road: the DSM is the cover's)
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]], lambda y: np.full(len(y), DTM + 8.0),
                        flags=((0, "is_bridge", 0.0, 1.0), (1, "is_covered", 0.2, 0.6)))
        self.assertEqual(got, [])

    def test_no_bridge_no_stretch(self):
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]], lambda y: np.full(len(y), DTM + 8.0), flags=())
        self.assertEqual(got, [])


class HKProvider:
    """A provider with Hong Kong's answers only (no lidar)."""
    enabled, lidar = True, False

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def gap_answers(self, keys, src="lidar"):
        self.asked.append(src)
        return {k: self.answer for k in keys}


@unittest.skipUnless(NATIVE, "earthfast 107")
class PlannerTest(unittest.TestCase):
    """roads.level_ranges of a viaduct p0 going on as ground pieces p1 and p2, in a Hong Kong cell and outside it."""

    def setUp(self):
        self.old = (roadz.PROVIDER, os.environ.pop("BNGE_PLAN_RUN", None), revisit.CACHE_DIR)
        revisit.CACHE_DIR = None
        roads._FACTS_MEM.clear()

    def tearDown(self):
        roadz.PROVIDER, run, revisit.CACHE_DIR = self.old
        if run is not None:
            os.environ["BNGE_PLAN_RUN"] = run
        roads._FACTS_MEM.clear()

    def feats(self, cell):
        f = features([[(0, -300), (0, 0)], [(0, 0), (0, 100)], [(0, 100), (0, 200)]], ["motorway"] * 3,
                     flags=[(0, "is_bridge", 0.0, 1.0)], ids=["p0", "p1", "p2"])
        f.__dict__["_bnge_cell"] = (cell, "roads", "{}")
        return f

    def answer(self):
        def ll(n):
            la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([float(n)]), 0.0)
            return float(lo[0]), float(la[0])
        return [("p1", ll(0), ll(100)), ("p2", ll(100), ll(150))]

    def test_hong_kong_cell_asks_hong_kong(self):
        roadz.PROVIDER = HKProvider(self.answer())
        f = self.feats("14/13389/7144")
        self.assertEqual(roads.level_ranges(f, 1)[0][2], "bridge")
        a, b, kind = roads.level_ranges(f, 2)[0]
        self.assertEqual(kind, "bridge", "the piece going on from the stretch (no candidate of the rule's)")
        self.assertAlmostEqual(b, 0.5, delta=0.01)
        self.assertEqual(set(roadz.PROVIDER.asked), {"hk"})

    def test_kept_ranges_of_other_cells_not_read(self):
        # the level ranges kept on disk (cellfacts) hold the stretches: Hong Kong's by its cells' tag, so those kept
        # before, or made with other cells, are made again; a cell elsewhere keeps its key
        import tempfile
        revisit.CACHE_DIR = tempfile.mkdtemp()
        hk, away = self.feats("14/13389/7144"), self.feats("14/8000/5000")
        k = roads._facts_key(hk, "ranges", None)
        plain = revisit.format_key("cellfacts", roads.FACTS_FORMAT, "ranges", hk.__dict__["_bnge_cell"], None)
        self.assertNotEqual(k, plain, "not the key its ranges were kept under before")
        with mock.patch.object(roadz, "HK_CELLS", "hk-next"):
            self.assertNotEqual(roads._facts_key(hk, "ranges", None), k)
            self.assertEqual(roads._facts_key(away, "specs", "x"),
                             revisit.format_key("cellfacts", roads.FACTS_FORMAT, "specs", away.__dict__["_bnge_cell"],
                                                "x"))

    def test_elsewhere_nobody_is_asked(self):
        roadz.PROVIDER = HKProvider(self.answer())
        f = self.feats("14/8000/5000")
        self.assertEqual(roads.level_ranges(f, 1), [])
        self.assertEqual(roadz.PROVIDER.asked, [])


if __name__ == "__main__":
    unittest.main()
