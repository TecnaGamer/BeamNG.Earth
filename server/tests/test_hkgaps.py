"""Hong Kong's ground stretches on a structure (roadz.RoadZ._hk_gaps: the lidar's rule, test_lidargaps.py, read on
Hong Kong's DSM and DTM): a ground piece going on from a deck's measured end is deck as far as the DSM along it stays
well over the DTM; the pieces going on from its end are asked too (an elevated junction: untagged pieces in a row);
an at-grade end and another surface over the road stay ground; the planner asks Hong Kong's cells for the answers.
The approaches (the lidar's, roadz APPROACH_*): the ground piece going on from a deck's or a stretch's end is measured
down to where it is back on the DTM, kept beside the cell's lines and given to the grader alone
(observed(lidar_ground=True)). The bridge parts of the map measured on their own lines (the lidar's decks), kept beside
the cell's lines with the stretches (Lines.own) and joined with the government's lines into one surface for a bridge
part (observed, OWN_ROWS, roadzhk.one_surface)."""
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
import roadzhk  # noqa: E402
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


def provider(feats, surface, dsm=None):
    """A RoadZ over the map layer feats whose DSM is surface(north m) on the road (6 m either side of east 0) and the
    DTM beside it, the DTM flat (dsm(x, y): the DSM itself)."""
    md = FakeMDem(DTM)
    md.cell = lambda key, **_kw: SimpleNamespace(layers={"roads": feats})
    rz = roadz.RoadZ(None, object(), md)

    if dsm is None:
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

    def gaps(self, lines, surface, flags=((0, "is_bridge", 0.0, 1.0),), levels=(), dsm=None):
        f = features(lines, ["motorway"] * len(lines), flags=list(flags), levels=list(levels),
                     ids=[f"p{i}" for i in range(len(lines))])
        self.rows, gaps, self.appr, self.decks = provider(f, surface, dsm)._hk_gaps(KEY)
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
        # (HK_GAP_RISE_M over the DTM until north 60 + 7 / 0.06 = 177: to the piece's end)
        self.assertAlmostEqual(ends["p3"], 160.0, delta=roadz.LIDAR_STEP_M)
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
        got = rz._hk_stretches(cands, [(0.0, 0.0, DTM + 13.0), (0.0, 0.0, DTM + 8.0)], [], rz._hk_reader("dtm"),
                               rz._hk_reader("dsm"))
        self.assertEqual([g[0] for g in got], ["p1"])
        self.assertEqual(rz._hk_stretches(cands, [(0.0, 0.0, DTM + 13.0)], [], rz._hk_reader("dtm"),
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
        self.assertEqual(self.appr, [], "on the DTM all along: nothing to measure")

    def test_another_surface_over_the_road_is_not_the_road(self):
        # a roof 10 m over the deck's end from there on: not the deck going on
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]],
                        lambda y: np.where(y < 0.0, DTM + 8.0, DTM + 18.0))
        self.assertEqual(got, [])
        self.assertEqual(self.appr, [], "nor its approach")

    def test_covered_road_not_read(self):
        # (the same surface as the pieces in a row, but the map has a cover over the road: the DSM is the cover's)
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]], lambda y: np.full(len(y), DTM + 8.0),
                        flags=((0, "is_bridge", 0.0, 1.0), (1, "is_covered", 0.2, 0.6)))
        self.assertEqual(got, [])

    def test_no_bridge_no_stretch(self):
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]], lambda y: np.full(len(y), DTM + 8.0), flags=())
        self.assertEqual(got, [])
        self.assertEqual(self.appr, [])

    def approach(self):
        """(north m, height over the DTM) along the one approach found."""
        self.assertEqual([r[2] for r in self.appr], [0], "one approach, a ground line")
        ll, z = self.appr[0][:2]
        return enu(ll[:, 1], ll[:, 0])[1], np.asarray(z) - DTM

    def test_approach_from_a_stretchs_end(self):
        # the structure goes on 40 m and then down at 6 % to the ground at north 173: a stretch to where it stands
        # HK_GAP_RISE_M over the DTM (north 157), the road still that high there
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 300)]],
                        lambda y: DTM + np.clip(8.0 - 0.06 * np.clip(y - 40.0, 0.0, None), 0.0, None))
        end = max(north(b) for _f, _a, b in got)
        self.assertAlmostEqual(end, 40.0 + (8.0 - roadz.HK_GAP_RISE_M) / 0.06, delta=2.0 * roadz.LIDAR_STEP_M)
        at, up = self.approach()
        self.assertAlmostEqual(at[0], end, delta=roadz.LIDAR_STEP_M, msg="from the stretch's end")
        self.assertAlmostEqual(up[0], 8.0 - 0.06 * (at[0] - 40.0), delta=0.6, msg="at the structure's height there")
        self.assertTrue((np.diff(up) < 0.05).all(), "down all the way")
        self.assertGreater(at[-1], 165.0)
        self.assertLess(at[-1], 173.0 + 3.0 * roadz.LIDAR_STEP_M, "to where it is back on the DTM, no further")
        self.assertLess(up[-1], roadz.APPROACH_REJOIN_M)

    def test_approach_from_a_low_deck_end(self):
        # a bridge down to 0.9 m over the DTM at its end, the road down from there at 3 %: no stretch, its approach
        got = self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]],
                        lambda y: DTM + np.clip(0.9 - np.where(y < 0.0, 0.05, 0.03) * y, 0.0, None))
        self.assertEqual(got, [])
        at, up = self.approach()
        self.assertLess(at[0], 2.0 * roadz.LIDAR_STEP_M)
        self.assertAlmostEqual(up[0], 0.9, delta=0.3)
        self.assertGreater(at[-1], 16.0)
        self.assertLess(at[-1], 30.0 + 3.0 * roadz.LIDAR_STEP_M)

    def test_approach_ends_where_the_dsm_is_the_dtm(self):
        # the road is on the ground 6 m past the bridge's end, and from north 100 a structure stands 0.8 m up on its
        # line; the decks' reader finds no surface between them: the approach ends at the ground, not at north 160
        real = roadz.roadzhk._deck_heights

        def reader(runs, dsm, hide=None):
            return [np.where((r[:, 1] > 6.0) & (r[:, 1] < 98.0), np.nan, z)
                    for r, z in zip(runs, real(runs, dsm, hide))]

        with mock.patch.object(roadz.roadzhk, "_deck_heights", reader):
            self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]],
                      lambda y: DTM + np.where(y <= 6.0, 0.9 - 0.05 * np.clip(y, None, 0.0),
                                               np.where(y < 98.0, 0.0, 0.8)))
        at, _up = self.approach()
        self.assertLess(at[-1], 30.0)

    def test_approach_the_reader_sees_little_of_is_none(self):
        # a ramp down from a deck 6 m up, back on the ground at north 120; the decks' reader finds a surface on its
        # first 10 m alone: 3 stations in 30 are no measured approach (the planner would hold the road by them)
        real = roadz.roadzhk._deck_heights

        def reader(runs, dsm, hide=None):
            return [np.where(r[:, 1] > 10.0, np.nan, z) for r, z in zip(runs, real(runs, dsm, hide))]

        def ramp(y):
            return DTM + np.clip(6.0 - 0.05 * np.clip(y, 0.0, None), 0.0, None)

        with mock.patch.object(roadz.roadzhk, "_deck_heights", reader):
            self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]], ramp)
        self.assertEqual(self.appr, [])
        self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]], ramp)
        self.assertEqual(len(self.appr), 1, "the same ramp read all along: its approach")

    def test_covered_road_no_approach(self):
        self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 160)]],
                  lambda y: DTM + np.clip(0.9 - 0.03 * np.clip(y, 0.0, None), 0.0, None),
                  flags=((0, "is_bridge", 0.0, 1.0), (1, "is_covered", 0.2, 0.6)))
        self.assertEqual(self.appr, [])

    def deck(self, k):
        """(north m, height) along the own row k of the bridge parts."""
        ll, z = self.decks[k][:2]
        return enu(ll[:, 1], ll[:, 0])[1], np.asarray(z)

    def test_bridge_part_measured_on_its_own_line(self):
        # no ground piece goes on from it (no candidate): its row all the same
        self.assertEqual(self.gaps([[(0, -100), (0, 0)]], lambda y: np.full(len(y), DTM + 8.0)), [])
        self.assertEqual(self.rows, [])
        self.assertEqual([r[2] for r in self.decks], [1], "a deck")
        at, z = self.deck(0)
        self.assertAlmostEqual(at.min(), -100.0, delta=0.5)
        self.assertAlmostEqual(at.max(), 0.0, delta=0.5)
        self.assertGreater(np.isfinite(z).mean(), 0.9)
        np.testing.assert_allclose(z[np.isfinite(z)], DTM + 8.0, atol=0.3)

    def test_decks_going_on_end_to_end_are_not_under_each_other(self):
        self.gaps([[(0, -100), (0, 0)], [(0, 0), (0, 100)]], lambda y: np.full(len(y), DTM + 8.0),
                  flags=((0, "is_bridge", 0.0, 1.0), (1, "is_bridge", 0.0, 1.0)))
        self.assertEqual(len(self.decks), 2)
        for k in range(2):
            self.assertGreater(np.isfinite(self.deck(k)[1]).mean(), 0.9)

    def cross(self, levels):
        """A bridge p0 (north -100..0, 8 m up) and another p1 across it at north -50, 16 m up."""
        def dsm(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return np.where(np.abs(y + 50.0) <= 6.0, DTM + 16.0, np.where(np.abs(x) <= 6.0, DTM + 8.0, DTM))
        self.gaps([[(0, -100), (0, 0)], [(-60, -50), (60, -50)]], None, levels=levels, dsm=dsm,
                  flags=((0, "is_bridge", 0.0, 1.0), (1, "is_bridge", 0.0, 1.0)))

    def test_bridge_under_a_higher_bridge_has_no_height_there(self):
        self.cross([(0, 1.0), (1, 2.0)])
        at, z = self.deck(0)
        under = np.abs(at + 50.0) <= roadz.roadzhk.OCCLUDE_M
        self.assertTrue(under.any())
        self.assertTrue(np.isnan(z[under]).all(), "the DSM there is the upper deck's")
        self.assertGreater(np.isfinite(z[~under]).mean(), 0.9, "and its own beyond")
        up = self.deck(1)[1]
        self.assertGreater(np.isfinite(up).mean(), 0.9, "the upper one is the DSM's top all along")
        np.testing.assert_allclose(up[np.isfinite(up)], DTM + 16.0, atol=0.3)

    def test_bridge_part_on_the_dtm_has_no_row(self):
        # (the survey is older than the bridge, or its DTM holds the structure: nothing says where the deck is)
        self.gaps([[(0, -100), (0, 0)]], lambda y: np.full(len(y), DTM + 0.3))
        self.assertTrue(np.isnan(self.deck(0)[1]).all())
        self.gaps([[(0, -100), (0, 0)]], lambda y: DTM + np.clip(0.08 * (y + 100.0), 0.3, 8.0))
        z = self.deck(0)[1]
        self.assertGreater(np.isfinite(z).mean(), 0.9, "over the DTM for most of it: a structure, its ends on the ground")

    def test_bridge_under_another_has_no_measured_end_to_go_on_from(self):
        # p0 under a crossing deck at its end (north 0): the DSM there is the upper one's, 16 m up, and the ground
        # piece p2 going on from p0's end, under the same deck, is no stretch at that height
        def dsm(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return np.where(np.abs(y) <= 20.0, DTM + 16.0, np.where(np.abs(x) <= 6.0, DTM + 8.0, DTM))
        got = self.gaps([[(0, -100), (0, 0)], [(-60, 3), (60, 3)], [(0, 0), (0, 16)]], None, dsm=dsm,
                        levels=[(0, 1.0), (1, 2.0)], flags=((0, "is_bridge", 0.0, 1.0), (1, "is_bridge", 0.0, 1.0)))
        self.assertEqual(got, [])

    def test_the_edge_of_a_viaduct_beside_the_part_is_not_its_surface(self):
        # a viaduct p1 16 m up, its line 8.5 m east of p0's (further than roadzhk.OCCLUDE_M) and its deck over the
        # east edge of p0's pixels; p0's own deck is not in the survey for its first 20 m, where the viaduct's edge
        # is all the DSM has round its line
        def dsm(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return np.where(x >= 3.5, DTM + 16.0, np.where(y < -80.0, np.nan, np.where(np.abs(x) <= 6.0, DTM + 8.0, DTM)))
        self.gaps([[(0, -100), (0, 0)], [(8.5, -100), (8.5, 0)]], None, levels=[(0, 1.0), (1, 2.0)], dsm=dsm,
                  flags=((0, "is_bridge", 0.0, 1.0), (1, "is_bridge", 0.0, 1.0)))
        at, z = self.deck(0)
        self.assertTrue(np.isnan(z[at < -80.0]).all(), "no surface of its own there")
        np.testing.assert_allclose(z[at > -78.0], DTM + 8.0, atol=0.3, err_msg="its own deck, not the viaduct's 16 m")
        np.testing.assert_allclose(self.deck(1)[1], DTM + 16.0, atol=0.3)

    def test_a_surface_beside_the_deck_is_not_its_height(self):
        # a deck 6 m wide and 8 m up with a hole in the survey over its first 20 m: deck_lidar starts on what is
        # beside it there, 2 m up, and keeps to it; across the part most of the DSM is the deck
        def dsm(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return np.where(np.abs(x) > 3.0, DTM + 2.0, np.where(y < -80.0, np.nan, DTM + 8.0))
        self.gaps([[(0, -100), (0, 0)]], None, dsm=dsm)
        at, z = self.deck(0)
        self.assertTrue(np.isnan(z[at > -78.0]).all(), "6 m under the surface across the part: no height of its")
        # (the same deck all in the survey: its own height)
        self.gaps([[(0, -100), (0, 0)]], None, dsm=lambda x, y: np.where(np.abs(np.asarray(x)) > 3.0, DTM + 2.0, DTM + 8.0))
        np.testing.assert_allclose(self.deck(0)[1], DTM + 8.0, atol=0.3)

    def test_same_level_crossing_decks_neither_is_taken_for_the_upper(self):
        self.cross([])
        for k in range(2):
            at, z = self.deck(k)
            self.assertTrue(np.isnan(z).any(), "the map does not say which is over")
        self.assertTrue(np.isnan(self.deck(0)[1][np.abs(self.deck(0)[0] + 50.0) < 3.0]).all())

    def piece(self, dsm, *viaducts):
        """A bridge p0 (north -100..0), a service road p1 (half width 2.5 m, north 0..160) going on from its end, and
        the viaducts (lines) beside or over it, all measured on dsm(x, y)."""
        f = features([[(0, -100), (0, 0)], [(0, 0), (0, 160)]] + [list(v) for v in viaducts],
                     ["motorway", "service"] + ["motorway"] * len(viaducts),
                     flags=[(0, "is_bridge", 0.0, 1.0)] + [(2 + i, "is_bridge", 0.0, 1.0) for i in range(len(viaducts))],
                     ids=[f"p{i}" for i in range(2 + len(viaducts))])
        self.rows, gaps, self.appr, self.decks = provider(f, None, dsm)._hk_gaps(KEY)
        return {fid for fid, _a, _b in gaps}

    def test_a_viaduct_over_the_piece_is_not_its_surface(self):
        # a viaduct p2 8 m up from north 10, over the line of p1 (the deck's own height there); the DSM over the road
        # is its surface. The same viaduct from north 0 goes on from the bridge's end together with p1: its deck
        def dsm(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return np.where((np.abs(x) <= 6.0) & (y > -100.0) & (y < 120.0), DTM + 8.0, DTM)
        self.assertEqual(self.piece(dsm, [(0, 10), (0, 120)]), set())
        self.assertEqual(self.appr, [], "no approach on it either")
        self.assertEqual(self.piece(dsm, [(0, 0), (0, 120)]), {"p1"})

    def test_a_deck_beside_the_piece_is_not_its_surface(self):
        # no surface of its own on the road itself (a hole in the survey), a viaduct deck 8 m up either side of it from
        # 2.6 m: what is left of the DSM in its pixels is the viaducts'
        def dsm(x, y):
            x, y = np.asarray(x), np.asarray(y)
            beside = (np.abs(x) > 2.5) & (np.abs(x) <= 13.0)
            return np.where((y >= 0.0) & (np.abs(x) <= 2.5), np.nan, np.where(beside | (np.abs(x) <= 5.0) & (y < 0.0),
                                                                             DTM + 8.0, DTM))
        self.assertEqual(self.piece(dsm, [(8, -20), (8, 200)], [(-8, -20), (-8, 200)]), set())
        self.assertEqual(self.appr, [])

    def test_the_surface_across_the_piece_is_its_height(self):
        # walls 1.5 m either side of a road on the ground: most of the DSM in its pixels is the walls', across its
        # 5 m width it is the ground
        def dsm(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return np.where(np.abs(x) <= 5.0, np.where((y >= 0.0) & (np.abs(x) <= 1.5), DTM, DTM + 8.0), DTM)
        self.assertEqual(self.piece(dsm), set())
        self.assertEqual(self.appr, [])

    def test_a_surface_over_most_of_the_decks_width_does_not_end_its_stretch(self):
        # the deck goes on 8 m up to north 120; for the first 8 m past the bridge's end something stands 7 m over all
        # of its width but the line's side: across the width the DSM is that, the line's own height is the deck under it
        def dsm(x, y):
            x, y = np.asarray(x), np.asarray(y)
            deck = (np.abs(x) <= 6.0) & (y > -100.0) & (y < 120.0)
            return np.where(deck, np.where((y >= 2.0) & (y < 10.0) & (x <= 1.0), DTM + 15.0, DTM + 8.0), DTM)
        self.assertEqual(self.piece(dsm), {"p1"})

    def test_ground_line_reads_no_pixels_of_a_bridge_it_is_not_joined_with(self):
        line = np.c_[np.zeros(26), np.linspace(0.0, 100.0, 26)]
        bridges = [(np.array([[8.0, 20.0], [8.0, 80.0]]), 5.0),            # beside
                   (np.array([[0.0, 0.0], [-60.0, 0.0]]), 5.0),            # going on from its start: joined
                   (np.array([[-40.0, 50.0], [40.0, 50.0]]), 5.0)]         # across it
        hide = roadzhk.hidden_by_bridges([line], bridges, 2.0 * roadz.LIDAR_STEP_M)
        pts = np.array([[3.0, 80.0], [1.5, 80.0], [-30.0, 2.0], [0.0, 50.0], [0.0, 70.0]])
        self.assertEqual(hide(0, pts).tolist(), [True, False, False, True, False],
                         "the one beside to its half width and 1 m, the joined one not, the one across it")
        self.assertFalse(roadzhk.hidden_by_bridges([line], [], 8.0)(0, pts).any())

    def test_stretches_and_decks_kept_beside_the_cells_lines(self):
        # the cell's own rows are the bridge parts' and the stretches': the lines of the government hold neither
        f = features([[(0, -100), (0, 0)], [(0, 0), (0, 60)]], ["motorway"] * 2, flags=[(0, "is_bridge", 0.0, 1.0)],
                     ids=["p0", "p1"])
        rz = provider(f, lambda y: np.full(len(y), DTM + 8.0))
        with mock.patch.object(roadz.hklidar, "arcgis", lambda *a, **k: []):
            got = rz._hk(KEY)
        self.assertEqual(len(got), 0)
        self.assertEqual([g[0] for g in got.gaps], ["p1"])
        self.assertEqual(got.own.level.tolist(), [1, 1], "the bridge p0's row and the stretch p1's")


class OneSurfaceTest(unittest.TestCase):
    """roadzhk.one_surface: a part's stations 4 m apart, the government's deck heights and the own row's."""
    arc = np.arange(0.0, 80.0, 4.0)

    def test_own_fills_the_gap_within_the_grade_of_the_government_heights(self):
        gov = np.where(self.arc >= 40.0, 9.9, np.nan)
        gov_out, own = roadzhk.one_surface(self.arc, gov, np.zeros(20), np.full(20, 10.9))
        np.testing.assert_array_equal(gov_out, gov)
        self.assertTrue(np.isnan(own[self.arc >= 40.0]).all(), "the government's stations are its own")
        fill = own[self.arc < 40.0]
        self.assertTrue(np.isfinite(fill).all())
        self.assertAlmostEqual(float(fill[-1]), 9.9 + roadzhk.OWN_GRADE * 4.0, places=6, msg="a ramp up from 9.9")
        self.assertAlmostEqual(float(fill[0]), 10.9, places=6, msg="its own height beyond the ramp")

    def test_own_contradicting_the_parts_government_line_is_dropped(self):
        gov = np.where(self.arc >= 40.0, 20.0, np.nan)
        own = np.where(self.arc < 60.0, 4.0, np.nan)
        gov_out, own_out = roadzhk.one_surface(self.arc, gov, np.zeros(20), own)
        np.testing.assert_array_equal(gov_out, gov)
        self.assertTrue(np.isnan(own_out).all(), "16 m under the deck within 40 m of it: not this deck")

    def test_another_roads_government_line_contradicting_the_own_row_is_dropped(self):
        gov = np.where(self.arc >= 40.0, 20.0, np.nan)
        own = np.full(20, 4.0)
        gov_out, own_out = roadzhk.one_surface(self.arc, gov, np.full(20, roadzhk.OWN_SAME_M + 3.0), own)
        self.assertTrue(np.isnan(gov_out).all())
        np.testing.assert_array_equal(own_out, own)

    def test_only_the_own_stations_that_contradict_are_dropped(self):
        gov = np.where(self.arc >= 40.0, 20.0, np.nan)
        own = np.where(self.arc < 16.0, 4.0, np.where(self.arc < 40.0, 20.0, np.nan))
        _gov, own_out = roadzhk.one_surface(self.arc, gov, np.zeros(20), own)
        self.assertTrue(np.isnan(own_out[self.arc < 16.0]).all())
        np.testing.assert_array_equal(own_out[(self.arc >= 16.0) & (self.arc < 40.0)], 20.0)

    def test_only_the_other_roads_stations_that_contradict_are_dropped(self):
        # a line coming in over the part, at its height where it starts and rising 50 % from there
        gov = np.where(self.arc >= 40.0, 20.0 + 0.5 * (self.arc - 40.0), np.nan)
        own = np.where(self.arc < 40.0, 19.0, np.nan)
        gov_out, own_out = roadzhk.one_surface(self.arc, gov, np.full(20, roadzhk.OWN_SAME_M + 3.0), own)
        self.assertEqual(self.arc[np.isfinite(gov_out)].tolist(), [40.0])
        self.assertTrue(np.isfinite(own_out[self.arc < 40.0]).all())

    def test_own_between_two_runs_of_the_government_line_meets_both(self):
        gov = np.where((self.arc < 16.0) | (self.arc >= 60.0), 10.0, np.nan)
        own = np.where(np.isnan(gov), 13.0, np.nan)
        _gov, own_out = roadzhk.one_surface(self.arc, gov, np.zeros(20), own)
        z = np.where(np.isfinite(gov), gov, own_out)
        ok = np.isfinite(z)
        self.assertTrue(np.isfinite(own_out).any())
        self.assertLessEqual(float(np.abs(np.diff(z[ok]) / np.diff(self.arc[ok])).max()), roadzhk.OWN_GRADE + 1e-9)
        np.testing.assert_allclose(own_out[np.isfinite(own_out)], [11.92, 12.4, 12.88, 12.4, 11.92],
                                   err_msg="12 % up from each run's end, never its own 13 m")

    def test_own_between_government_heights_no_one_grade_joins_is_left(self):
        arc = np.array([0.0, 4.0, 8.0])
        _gov, own = roadzhk.one_surface(arc, np.array([10.0, np.nan, 12.5]), np.zeros(3), np.array([np.nan, 11.25, np.nan]))
        self.assertEqual(float(own[1]), 11.25)

    def test_one_of_them_alone_is_left_as_it_is(self):
        gov = np.where(self.arc >= 40.0, 20.0, np.nan)
        none = np.full(20, np.nan)
        np.testing.assert_array_equal(roadzhk.one_surface(self.arc, gov, np.zeros(20), none)[0], gov)
        np.testing.assert_array_equal(roadzhk.one_surface(self.arc, none, np.zeros(20), np.full(20, 4.0))[1], 4.0)


class ApproachObservedTest(unittest.TestCase):
    """A Hong Kong cell: the government's ground line along north (on the DTM, 3 m over our DEM's datum) and an
    approach over its first 60 m, from 3 m up down to the ground."""

    def setUp(self):
        y = np.arange(-50.0, 252.0, 2.0)
        ya = np.arange(0.0, 64.0, 4.0)
        self.cell = roadz.Lines.build([self.row(y, np.full(len(y), DTM + 3.0))])
        self.cell.approaches = roadz.Lines.build([self.row(ya, DTM + 3.0 + 3.0 - 0.05 * ya)])
        self.rz = roadz.RoadZ(None, object(), FakeMDem(DTM))
        self.rz.enabled = True
        self.rz.lines = lambda s, key: self.cell
        self.part = np.c_[np.zeros(26), np.linspace(0.0, 100.0, 26)]
        patch = mock.patch.object(roadz, "sources_at", lambda lat, lon, lidar=None: ["hk"])
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def row(y, z):
        la, lo, _ = FRAME.enu_to_geodetic(np.zeros(len(y)), y, 0.0)
        return (np.c_[lo, la], z, 0, "", "")

    def test_grader_takes_the_approach(self):
        info = {}
        z = self.rz.observed(FRAME, self.part, 4.0, 0, info=info, lidar_ground=True)
        y = self.part[:, 1]
        self.assertEqual(info["source"], "hk")
        self.assertAlmostEqual(info["offset"], 3.0, delta=0.01, msg="the cell's ground lines' datum")
        np.testing.assert_allclose(z[y <= 56.0], DTM + 3.0 - 0.05 * y[y <= 56.0], atol=0.3)
        self.assertTrue(np.isnan(z[y > 64.0]).all(), "nothing past the approach: the DTM's from there")

    def test_ground_roads_do_not_take_it(self):
        z = self.rz.observed(FRAME, self.part, 4.0, 0)
        np.testing.assert_allclose(z, DTM, atol=0.05, err_msg="the government's ground line")

    def test_another_roads_approach_is_not_taken(self):
        # a road beside the approach (5 m east) that starts 40 m before it: within reach of it, not its own
        part = np.c_[np.full(26, 5.0), np.linspace(-40.0, 60.0, 26)]
        self.assertIsNone(self.rz.observed(FRAME, part, 4.0, 0, lidar_ground=True))
        # (and the approach's own road, drawn from the other end)
        z = self.rz.observed(FRAME, self.part[::-1], 4.0, 0, lidar_ground=True)
        self.assertTrue(np.isfinite(z[self.part[::-1][:, 1] <= 56.0]).all())

    def test_approach_ending_off_our_terrain_is_not_given(self):
        # the survey's ground is 3 m over our terrain at the approach's end, as the ground lines say (reclaimed land
        # where ours is flat sea level): the planner would see no lift left to ramp down. The same row ending 0.5 m over
        # it is given
        ya = np.arange(0.0, 64.0, 4.0)
        for end, given in ((3.0, False), (0.5, True)):
            self.cell.approaches = roadz.Lines.build([self.row(ya, DTM + 3.0 + end + (60.0 - ya) * 0.05)])
            z = self.rz.observed(FRAME, self.part, 4.0, 0, lidar_ground=True)
            self.assertEqual(z is not None, given, f"ends {end} m over our terrain")

    def test_cell_without_approaches(self):
        del self.cell.approaches
        self.assertIsNone(self.rz.observed(FRAME, self.part, 4.0, 0, lidar_ground=True))

    def test_other_sources_have_none(self):
        with mock.patch.object(roadz, "sources_at", lambda lat, lon, lidar=None: ["ign"]):
            self.assertIsNone(self.rz.observed(FRAME, self.part, 4.0, 0, lidar_ground=True))

    def planner_obs(self, touches=True):
        """Grader._obs of a planner on coarse elevation (Hong Kong's is, dem.coarse_at) whose parts all start at a
        bridge's end (or none does)."""
        import grading
        fake = SimpleNamespace(frame=FRAME, _coarse=lambda key: True, _touches_bridge_end=lambda xy: touches,
                               _touches_bridge_ends=lambda xys: [touches] * len(xys))
        fake._approach_obs = lambda prov: grading._Planner._approach_obs(fake, prov)
        old = roadz.PROVIDER
        roadz.PROVIDER = self.rz
        self.addCleanup(setattr, roadz, "PROVIDER", old)
        return grading._Planner._obs(fake, KEY)

    def test_planner_asks_on_coarse_elevation(self):
        obs = self.planner_obs()
        obs.prepare([self.part])
        z = obs(self.part, SimpleNamespace(width=8.0))
        y = self.part[:, 1]
        np.testing.assert_allclose(z[y <= 56.0], DTM + 3.0 - 0.05 * y[y <= 56.0], atol=0.3, err_msg="the approach")
        np.testing.assert_allclose(z[y > 64.0], DTM, atol=0.05, err_msg="the ground line where the approach ends")

    def test_planner_part_away_from_a_bridge(self):
        z = self.planner_obs(touches=False)(self.part, SimpleNamespace(width=8.0))
        np.testing.assert_allclose(z, DTM, atol=0.05, err_msg="the ground line alone")

    def test_planner_elsewhere_as_before(self):
        with mock.patch.object(roadz, "sources_at", lambda lat, lon, lidar=None: ["ign"]):
            obs = self.planner_obs()
            self.assertFalse(hasattr(obs, "prepare"), "no bridge ends looked for")
            np.testing.assert_allclose(obs(self.part, SimpleNamespace(width=8.0)), DTM, atol=0.05)

    def test_kept_in_the_cells_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.npz")
            self.cell.save(p)
            got = roadz.Lines.load(p)
            np.testing.assert_array_equal(got.approaches.z, self.cell.approaches.z)
            np.testing.assert_array_equal(got.approaches.lonlat, self.cell.approaches.lonlat)
            self.assertEqual(got.approaches.level.tolist(), [0])
            roadz.Lines.build([self.row(np.arange(0.0, 20.0, 4.0), np.zeros(5))]).save(p)
            self.assertFalse(hasattr(roadz.Lines.load(p), "approaches"))


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= roadz.NATIVE, "earthfast roadz kernels")
class OwnObservedTest(unittest.TestCase):
    """A Hong Kong cell: the government's ground line along north (on the DTM, our DEM's datum), a deck line 0.3 m east
    of the part at 3.7 m over it all along, and the map's own row of the part at 8 m, down to north 50."""

    def setUp(self):
        y = np.arange(-50.0, 252.0, 2.0)
        ys = np.arange(0.0, 52.0, 2.0)
        self.cell = roadz.Lines.build([self.row(y, np.full(len(y), DTM)),
                                       self.row(y, np.full(len(y), DTM + 3.7), 1, 0.3)])
        self.cell.own = roadz.Lines.build([self.row(ys, np.full(len(ys), DTM + 8.0), 1)])
        self.rz = roadz.RoadZ(None, object(), FakeMDem(DTM))
        self.rz.enabled = True
        self.rz.lines = lambda s, key: self.cell
        self.part = np.c_[np.full(26, 0.3), np.linspace(0.0, 100.0, 26)]
        patch = mock.patch.object(roadz, "sources_at", lambda lat, lon, lidar=None: ["hk"])
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def row(y, z, level=0, east=0.0):
        la, lo, _ = FRAME.enu_to_geodetic(np.full(len(y), east), y, 0.0)
        return (np.c_[lo, la], z, level, "", "")

    def test_the_government_line_along_the_part_is_not_overruled(self):
        # (the row 4.3 m over it reads another deck's edge, or a barrier beside the road)
        info = {}
        z = self.rz.observed(FRAME, self.part, 4.0, 1, info=info)
        np.testing.assert_allclose(z, DTM + 3.7, atol=0.05, err_msg="the government's line, as without the row")
        self.assertEqual(info["own"], 0.0)

    def test_own_row_on_the_ground_under_the_government_deck_is_dropped(self):
        # (the row began at a hole in the survey and stayed on the shore under the viaduct)
        ys = np.arange(0.0, 52.0, 2.0)
        self.cell.own = roadz.Lines.build([self.row(ys, np.full(len(ys), DTM - 9.0), 1)])
        info = {}
        z = self.rz.observed(FRAME, self.part, 4.0, 1, info=info)
        np.testing.assert_allclose(z, DTM + 3.7, atol=0.05, err_msg="the government's line, as without the row")
        self.assertEqual(info["own"], 0.0)

    def test_own_row_fills_where_the_government_line_has_no_height(self):
        y = np.arange(60.0, 252.0, 2.0)
        ys = np.arange(0.0, 62.0, 2.0)
        self.cell = roadz.Lines.build([self.row(np.arange(-50.0, 252.0, 2.0), np.full(151, DTM)),
                                       self.row(y, np.full(len(y), DTM + 3.7), 1, 0.3)])
        self.cell.own = roadz.Lines.build([self.row(ys, DTM + 3.7 + 0.05 * (60.0 - ys), 1)])
        info = {}
        z = self.rz.observed(FRAME, self.part, 4.0, 1, info=info)
        y = self.part[:, 1]
        np.testing.assert_allclose(z[y >= 60.0], DTM + 3.7, atol=0.05, err_msg="the government's, untouched")
        np.testing.assert_allclose(z[y < 60.0], DTM + 3.7 + 0.05 * (60.0 - y[y < 60.0]), atol=0.3)
        self.assertAlmostEqual(info["own"], 0.6, delta=0.1)

    def test_government_line_of_another_road_gives_way_to_the_own_row(self):
        # the only deck line 7 m beside the part (another carriageway's, or a road crossing over): 25 m over the own
        # row at the part's far end only, as a line coming in over it
        y = np.arange(60.0, 252.0, 2.0)
        ys = np.arange(0.0, 102.0, 2.0)
        self.cell = roadz.Lines.build([self.row(np.arange(-50.0, 252.0, 2.0), np.full(151, DTM)),
                                       self.row(y, np.full(len(y), DTM + 33.0), 1, 7.0)])
        self.cell.own = roadz.Lines.build([self.row(ys, np.full(len(ys), DTM + 8.0), 1)])
        z = self.rz.observed(FRAME, self.part, 4.0, 1)
        np.testing.assert_allclose(z, DTM + 8.0, atol=0.05)

    def test_the_own_row_meets_the_government_deck_without_a_step(self):
        # the government's deck line from north 60 on, the own row 2.3 m over it up to there
        y = np.arange(60.0, 252.0, 2.0)
        ys = np.arange(0.0, 60.0, 2.0)
        self.cell = roadz.Lines.build([self.row(np.arange(-50.0, 252.0, 2.0), np.full(151, DTM)),
                                       self.row(y, np.full(len(y), DTM + 3.7), 1, 0.3)])
        self.cell.own = roadz.Lines.build([self.row(ys, np.full(len(ys), DTM + 6.0), 1)])
        z = self.rz.observed(FRAME, self.part, 4.0, 1)
        y, ok = self.part[:, 1], np.isfinite(z)
        self.assertLessEqual(float(np.abs(np.diff(z[ok]) / np.diff(y[ok])).max()), roadzhk.OWN_GRADE + 0.01,
                             "a ramp, not a step the planner drops the part for")
        np.testing.assert_allclose(z[y >= 60.0], DTM + 3.7, atol=0.05, err_msg="the government's, untouched")
        np.testing.assert_allclose(z[y <= 28.0], DTM + 6.0, atol=0.3, err_msg="the row's own height beyond the ramp")

    def test_government_deck_where_the_own_row_has_no_surface(self):
        z = self.rz.observed(FRAME, self.part, 4.0, 1)
        y = self.part[:, 1]
        np.testing.assert_allclose(z[y >= 90.0], DTM + 3.7, atol=0.3, err_msg="(the step between the two is cleaned)")

    def test_own_row_with_no_surface_at_all(self):
        self.cell.own = roadz.Lines.build([self.row(np.arange(0.0, 52.0, 2.0), np.full(26, np.nan), 1)])
        info = {}
        np.testing.assert_allclose(self.rz.observed(FRAME, self.part, 4.0, 1, info=info), DTM + 3.7, atol=0.3)
        self.assertEqual(info["own"], 0.0)

    def test_own_rows_far_from_the_part_are_not_taken(self):
        # the government's deck line from north 60 on, a row at its height up to there: the part's own fills the gap,
        # another part's, roadz.OWN_REACH_M and more off, does not
        y = np.arange(60.0, 252.0, 2.0)
        ys = np.arange(0.0, 60.0, 2.0)
        self.cell = roadz.Lines.build([self.row(np.arange(-50.0, 252.0, 2.0), np.full(151, DTM)),
                                       self.row(y, np.full(len(y), DTM + 3.7), 1, 0.3)])
        self.cell.own = roadz.Lines.build([self.row(ys, np.full(len(ys), DTM + 3.7), 1)])
        info = {}
        self.rz.observed(FRAME, self.part, 4.0, 1, info=info)
        self.assertGreater(info["own"], 0.4)
        part = np.c_[np.full(26, 0.3 + roadz.OWN_REACH_M + 1.0), np.linspace(0.0, 100.0, 26)]
        self.rz.observed(FRAME, part, 4.0, 1, info=info)
        self.assertEqual(info["own"], 0.0)

    def test_other_asks_do_not_see_it(self):
        info = {}
        ground = self.rz.observed(FRAME, self.part, 4.0, 0, info=info)
        np.testing.assert_allclose(ground, DTM, atol=0.05, err_msg="a ground part: the government's ground line")
        self.assertNotIn("own", info)
        with_own = self.rz.observed(FRAME, self.part, 4.0, 1, lidar_ground=True)
        self.assertIsNone(with_own, "the approaches alone: none here")
        self.cell.approaches = roadz.Lines.build([self.row(np.arange(0.0, 64.0, 4.0), np.full(16, DTM + 3.0))])
        got = self.rz.observed(FRAME, self.part, 4.0, 0, lidar_ground=True)
        del self.cell.own
        np.testing.assert_array_equal(got, self.rz.observed(FRAME, self.part, 4.0, 0, lidar_ground=True))

    def ground_ask(self, *rows):
        """A ground ask of the part in a cell of these rows alone (a provider of its own: the segments are kept)."""
        self.cell = roadz.Lines.build(list(rows))
        rz = roadz.RoadZ(None, object(), FakeMDem(DTM))
        rz.enabled = True
        rz.lines = lambda s, key: self.cell
        return rz.observed(FRAME, self.part, 4.0, 0)

    def test_ground_part_whose_own_line_is_a_deck_takes_no_line_beside_it(self):
        # the government's deck line along the part (the road's own, 0.4 m over the terrain); its ground line 7 m beside
        # it; the datum from the many ground lines of the streets round it (30 m away, on the terrain)
        y = np.arange(-50.0, 252.0, 2.0)
        deck = self.row(y, np.full(len(y), DTM + 0.4), 1, 0.3)
        streets = [self.row(y, np.full(len(y), DTM), 0, east) for east in (30.0, 40.0, -30.0)]
        beside = self.row(y, np.full(len(y), DTM + 3.0), 0, 7.3)
        self.assertIsNone(self.ground_ask(beside, deck, *streets), "the carriageway's beside it: not this road's")
        np.testing.assert_allclose(self.ground_ask(beside, *streets), DTM + 3.0, atol=0.05,
                                   err_msg="no line of another tier nearer: the ground line in reach, as before")
        np.testing.assert_allclose(self.ground_ask(self.row(y, np.full(len(y), DTM + 3.0), 0, 2.3), deck, *streets),
                                   DTM + 3.0, atol=0.05, err_msg="a ground line 2 m from the deck's: as near the road")
        over = self.row(y, np.full(len(y), DTM + 6.0), 1, 0.3)
        np.testing.assert_allclose(self.ground_ask(self.row(y, np.full(len(y), DTM + 0.5), 0, 7.3), over, *streets),
                                   DTM + 0.5, atol=0.05, err_msg="a deck over the road: the ground line is the terrain's")
        with mock.patch.object(roadz, "sources_at", lambda lat, lon, lidar=None: ["ign"]):
            np.testing.assert_allclose(self.ground_ask(beside, deck, *streets), DTM + 3.0, atol=0.05,
                                       err_msg="another source: as before")

    def test_other_sources_do_not_see_it(self):
        with mock.patch.object(roadz, "sources_at", lambda lat, lon, lidar=None: ["ign"]):
            with_own = self.rz.observed(FRAME, self.part, 4.0, 1)
            del self.cell.own
            np.testing.assert_array_equal(with_own, self.rz.observed(FRAME, self.part, 4.0, 1))
        np.testing.assert_allclose(with_own[self.part[:, 1] <= 46.0], DTM + 3.7, atol=0.3)

    def test_cell_without_own_rows(self):
        del self.cell.own
        np.testing.assert_allclose(self.rz.observed(FRAME, self.part, 4.0, 1), DTM + 3.7, atol=0.3)

    def test_kept_in_the_cells_file(self):
        import tempfile
        self.cell.approaches = roadz.Lines.build([self.row(np.arange(0.0, 64.0, 4.0), np.full(16, DTM + 3.0))])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.npz")
            self.cell.save(p)
            got = roadz.Lines.load(p)
            np.testing.assert_array_equal(got.own.z, self.cell.own.z)
            np.testing.assert_array_equal(got.own.lonlat, self.cell.own.lonlat)
            np.testing.assert_array_equal(got.own.off, self.cell.own.off)
            self.assertEqual(got.own.level.tolist(), [1])
            np.testing.assert_array_equal(got.approaches.z, self.cell.approaches.z)
            del self.cell.own
            self.cell.save(p)
            got = roadz.Lines.load(p)
            self.assertFalse(hasattr(got, "own"), "a cell file without them")
            self.assertTrue(hasattr(got, "approaches"))


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


class HKCellFilesTest(unittest.TestCase):
    """What a Hong Kong cell costs its hosts and the disk: the LiDAR index asked once for a cell's reads, and both of
    the cell's files marked used when it is read (the stretches' file is read back with the lines, or made again)."""

    def sheets(self, fail=()):
        asked = []

        def index(layer, box, kind="demand"):
            asked.append((layer, tuple(box)))
            if len(asked) in fail:
                raise OSError("no answer")
            return [("A", "u", (-1e6, -1e6, 1e6, 1e6))]
        sheet = SimpleNamespace(sample=lambda x, y: np.full(len(x), 7.0))
        return SimpleNamespace(index=index, get=lambda layer, name, url: sheet), asked

    def test_index_asked_once_for_reads_inside_a_box_asked_before(self):
        rz = roadz.RoadZ(None, object(), FakeMDem(DTM))
        rz.sheets, asked = self.sheets()
        read = rz._hk_reader("dsm")
        np.testing.assert_array_equal(read(np.array([100.0, 200.0]), np.array([100.0, 200.0])), [7.0, 7.0])
        pad = roadz.HK_INDEX_PAD_M
        self.assertEqual(asked, [("dsm", (99.0 - pad, 99.0 - pad, 201.0 + pad, 201.0 + pad))])
        read(np.array([150.0]), np.array([150.0]))
        read(np.array([-300.0, 600.0]), np.array([-300.0, 600.0]))
        self.assertEqual(len(asked), 1, "inside the box asked before")
        read(np.array([5000.0]), np.array([5000.0]))
        self.assertEqual(len(asked), 2, "outside it")
        rz._hk_reader("dtm")(np.array([150.0]), np.array([150.0]))
        self.assertEqual([a[0] for a in asked], ["dsm", "dsm", "dtm"], "a layer's own")
        for i in range(2 * roadz.HK_INDEX_KEPT):
            read(np.array([1e5 * (i + 1)]), np.array([0.0]))
        self.assertEqual(len(rz._hk_index["dsm"]), roadz.HK_INDEX_KEPT)

    def test_failed_index_request_is_asked_again(self):
        rz = roadz.RoadZ(None, object(), FakeMDem(DTM))
        rz.sheets, asked = self.sheets(fail=(1,))
        read = rz._hk_reader("dsm")
        with self.assertRaises(OSError):
            read(np.array([100.0]), np.array([100.0]))
        np.testing.assert_array_equal(read(np.array([100.0]), np.array([100.0])), [7.0])
        self.assertEqual(len(asked), 2)

    def test_both_files_of_a_kept_cell_are_marked_used(self):
        import tempfile
        y = np.arange(0.0, 40.0, 4.0)
        lonlat = np.stack([np.full(len(y), LON), LAT + y / 111320.0], 1)
        with tempfile.TemporaryDirectory() as d:
            rz = roadz.RoadZ(d, object(), FakeMDem(DTM))
            p = rz._path("hk", KEY)
            os.makedirs(os.path.dirname(p))
            roadz.Lines.build([(lonlat, np.full(len(y), DTM), 0, "", "")]).save(p)
            with open(p + ".gaps.json", "w", encoding="utf-8") as fh:
                fh.write("[]")
            with mock.patch("cache.touch_once") as touch:
                got = rz.lines("hk", KEY)
            self.assertEqual(len(got), 1)
            self.assertEqual(sorted(c.args[0] for c in touch.call_args_list), [p, p + ".gaps.json"])


if __name__ == "__main__":
    unittest.main()
