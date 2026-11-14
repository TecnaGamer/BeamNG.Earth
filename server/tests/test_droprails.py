"""droprails: estimated rails on a rural road's drop side (GitHub #1063)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import droprails  # noqa: E402
import roads  # noqa: E402


class Frame:
    enu_to_geodetic = staticmethod(lambda x, y, z: (y, x, z))
    geodetic_to_enu = staticmethod(lambda la, lo, h: (lo, la, h))


BOX = (-2000.0, -2000.0, 2000.0, 2000.0)
SPEC = roads.Spec(6.0, "paved", False, "secondary", False)
ROAD_Z = 100.0


def line(x0=0.0, x1=300.0, y=0.0):
    return np.c_[np.linspace(x0, x1, 31), np.full(31, y), np.full(31, ROAD_Z)]


def cliff(y_edge=-4.5, below=10.0, x0=-1e9, x1=1e9):
    """Ground at the road's height, falling below metres south of y_edge between x0 and x1."""
    def elev(la, lo):
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        return np.where((la < y_edge) & (lo > x0) & (lo < x1), ROAD_Z - below, ROAD_Z)
    return elev


def rows(near, elev, box=BOX, mapped=(), decks=()):
    with mock.patch.dict(os.environ, {"BNE_DROP_RAILS": "on"}):
        got = droprails.tile_rows(Frame, near, box, elev, mapped, decks)
    return np.array(got).reshape(-1, 17)


class RunsTest(unittest.TestCase):
    def test_short_gaps_closed_short_runs_dropped_runs_led_in(self):
        f = np.zeros(30, bool)
        f[10:15] = True
        f[17:20] = True             # (a 2-station gap: one run 10-19)
        f[25:27] = True             # (too short)
        got = droprails.runs(f)
        self.assertEqual(np.flatnonzero(got).tolist(), list(range(8, 22)))

    def test_lead_stops_at_the_ends(self):
        f = np.ones(5, bool)
        self.assertTrue(droprails.runs(f).all())
        self.assertFalse(droprails.runs(np.zeros(5, bool)).any())


class PlaceTest(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {"BNE_DROP_RAILS": ""}):
            self.assertEqual(droprails.tile_rows(Frame, [("a", SPEC, line(), 0.0)], BOX, cliff()), [])
            self.assertEqual(droprails.key(), ())
        with mock.patch.dict(os.environ, {"BNE_DROP_RAILS": "on"}):
            self.assertEqual(droprails.key(), (droprails.VERSION,))

    def test_on_the_drop_side_only_smooth_face_to_the_road(self):
        r = rows([("a", SPEC, line(), 0.0)], cliff())
        run = r[r[:, 0] == droprails.G_RAIL]
        self.assertGreater(len(run), 90)
        self.assertTrue(np.allclose(run[:, 2], -(3.0 + droprails.OUT_M)), "a rail's line 0.75 m off the edge")
        self.assertTrue((run[:, 8] < -0.99).all(), "right of the way: forward against it")
        face = -run[:, 11:14]       # (-back: the smooth face)
        self.assertTrue((face[:, 1] > 0.99).all(), "the smooth face toward the road")
        self.assertTrue(np.allclose(run[:, 3], ROAD_Z), "on the ground at the rail")
        self.assertEqual(sorted(r[:, 0].astype(int).tolist()).count(droprails.G_START), 1)
        self.assertEqual(sorted(r[:, 0].astype(int).tolist()).count(droprails.G_END), 1)

    def test_left_side_runs_along_the_way(self):
        def north(la, lo):
            return np.where(np.asarray(la) > 4.5, ROAD_Z - 10.0, ROAD_Z)
        run = rows([("a", SPEC, line(), 0.0)], north)
        run = run[run[:, 0] == droprails.G_RAIL]
        self.assertTrue((run[:, 2] > 0).all() and (run[:, 8] > 0.99).all())
        self.assertTrue((-run[:, 12] < -0.99).all(), "the smooth face toward the road (south)")

    def test_none_where_flat_or_a_small_drop(self):
        self.assertEqual(len(rows([("a", SPEC, line(), 0.0)], cliff(below=0.0))), 0)
        self.assertEqual(len(rows([("a", SPEC, line(), 0.0)], cliff(below=2.0))), 0)
        self.assertEqual(len(rows([("a", SPEC, line(), 0.0)], cliff(y_edge=-15.0))), 0, "the drop beyond the reach")

    def test_a_run_with_its_lead_and_caps(self):
        r = rows([("a", SPEC, line(), 0.0)], cliff(x0=100.0, x1=150.0))
        run = r[r[:, 0] == droprails.G_RAIL]
        self.assertGreater(run[:, 1].min(), 100.0 - 4 * droprails.MODULE_STEP_M)
        self.assertLess(run[:, 1].max(), 150.0 + 4 * droprails.MODULE_STEP_M)
        start, end = r[r[:, 0] == droprails.G_START], r[r[:, 0] == droprails.G_END]
        self.assertEqual((len(start), len(end)), (1, 1))
        self.assertGreater(start[0, 1], end[0, 1], "forward runs west: the start cap at the east end")

    def test_none_too_short_in_town_on_gravel_or_a_track(self):
        self.assertEqual(len(rows([("a", SPEC, line(), 0.0)], cliff(x0=100.0, x1=106.0))), 0)
        for sp in (SPEC._replace(urban=True), SPEC._replace(kind="gravel"), SPEC._replace(cls="track"),
                   SPEC._replace(cls="residential")):
            self.assertEqual(len(rows([("a", sp, line(), 0.0)], cliff())), 0, str(sp))

    def test_not_on_another_carriageway(self):
        side = roads.Spec(6.0, "paved", False, "tertiary", False)
        cross = np.c_[np.full(2, 150.0), [-60.0, 0.0], [ROAD_Z, ROAD_Z]]
        run = rows([("a", SPEC, line(), 0.0), ("b", side, cross, 0.0)], cliff())
        run = run[(run[:, 0] == droprails.G_RAIL) & (run[:, 2] < 0)]
        self.assertFalse((np.abs(run[:, 1] - 150.0) < 3.0 + droprails.CLEAR_M).any(), "the junction left open")
        self.assertGreater(len(run), 80)

    def test_not_beside_a_mapped_rail(self):
        mapped = [[0, x, -3.75, ROAD_Z, 180.0, 0.0, 0.9, 0.5, -1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 1.0]
                  for x in np.arange(0.0, 301.0, 2.8)]
        self.assertEqual(len(rows([("a", SPEC, line(), 0.0)], cliff(), mapped=mapped)), 0)

    def test_not_on_a_deck(self):
        deck = {"n": np.c_[[50.0, 250.0], [0.0, 0.0], [ROAD_Z, ROAD_Z]], "w": 6.0}
        run = rows([("a", SPEC, line(), 0.0)], cliff(), decks=[deck])
        run = run[run[:, 0] == droprails.G_RAIL]
        self.assertFalse(((run[:, 1] > 55.0) & (run[:, 1] < 245.0)).any())

    def test_tiles_share_the_modules(self):
        near, elev = [("a", SPEC, line(), 0.0)], cliff()
        whole = rows(near, elev)
        west, east = rows(near, elev, (-2000.0, -2000.0, 150.0, 2000.0)), rows(near, elev, (150.0, -2000.0, 2000.0, 2000.0))
        both = np.r_[west, east]
        self.assertEqual(len(both), len(whole))
        self.assertTrue(np.allclose(np.sort(both[:, 1]), np.sort(whole[:, 1])))


if __name__ == "__main__":
    unittest.main()
