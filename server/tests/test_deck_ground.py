"""GitHub #1098: the Pont de Normandie's south approach (49.4263-49.4279 N, 0.2747 E). The elevation tiles hold the
bridge (a 60-150 m ridge exactly under the deck and its cables, over ~8 m of marsh: Copernicus GLO-30 is a surface
model), and the terrain block copied it as a sheer wall under the deck. Under a mapped deck, ground that stands well
over both sides of the deck is the bridge, not the ground: grading.DECK_GROUND_* lowers it to the line between the
sides (Rust: earthfast deckground.rs), for the plans' own elevation and the ground they grade alike."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
from test_roads import features, FRAME  # noqa: E402
from test_grading import FakeMD  # noqa: E402

MARSH = 8.0


def enu(la, lo):
    e, n, _ = FRAME.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
    return np.asarray(e, np.float64), np.asarray(n, np.float64)


def ridge(la, lo):
    """The marsh, with the deck (y = 0, -180 < x < 180) held as a ridge 100 m high, smeared 25 m either side."""
    e, n = enu(la, lo)
    return MARSH + 100.0 * np.clip(1.0 - np.abs(n) / 25.0, 0.0, None) * (np.abs(e) < 180.0)


def valley(la, lo):
    """A valley across the deck: the ground falls 30 % to its floor under the deck's middle."""
    e, _n = enu(la, lo)
    return 30.0 + 0.3 * np.abs(e)


def embankment(la, lo):
    """The marsh, with a road's 8 m embankment crossing under the deck along x = 60 (its top 16 m wide, 1:1 slopes)."""
    e, _n = enu(la, lo)
    return MARSH + np.clip(16.0 - np.abs(e - 60.0), 0.0, 8.0)


def deck_road():
    """A motorway along y = 0 from x -300 to 300, a bridge over its middle 360 m."""
    return features([[(-300.0, 0.0), (300.0, 0.0)]], ["motorway"], flags=[(0, "is_bridge", 0.2, 0.8)])


def grid(xs, ys):
    e, n = np.meshgrid(np.asarray(xs, np.float64), np.asarray(ys, np.float64))
    la, lo, _ = FRAME.enu_to_geodetic(e.ravel(), n.ravel(), 0.0)
    return np.asarray(la), np.asarray(lo), e.ravel(), n.ravel()


class DeckGroundTest(unittest.TestCase):
    def setUp(self):
        import roadz
        p = mock.patch.object(roadz, "PROVIDER", None)
        p.start()
        self.addCleanup(p.stop)

    def grader(self, elev, on=True):
        with mock.patch.object(grading, "DECK_GROUND_ON", on):
            return grading.Grader(FRAME, FakeMD(deck_road()), elev)

    def test_ridge_under_the_deck_is_the_marsh(self):
        g = self.grader(ridge)
        la, lo, _e, n = grid(np.arange(-150.0, 151.0, 30.0), np.arange(-100.0, 101.0, 5.0))
        z = g.apply(la, lo, ridge(la, lo))
        np.testing.assert_allclose(z, MARSH, atol=0.05)
        # the plans read it too (their own elevation: elev_raw as read)
        pl = g.store.planners_of(la[:1], lo[:1])[0][0]
        np.testing.assert_allclose(pl.elev(la, lo), MARSH, atol=0.05)
        self.assertGreater(float(np.max(pl.elev_raw(la, lo))), 100.0)

    def test_off_leaves_it(self):
        g = self.grader(ridge, on=False)
        la, lo, _e, _n = grid([0.0], np.arange(-10.0, 11.0, 5.0))
        with mock.patch.object(grading, "DECK_GROUND_ON", False):
            z = g.apply(la, lo, ridge(la, lo))
        self.assertGreater(float(z.max()), 100.0)

    def test_approach_past_the_deck_end_kept(self):
        g = self.grader(ridge)
        # (the elevation's ridge stops at the deck's ends here; 20 m onto the approach nothing is lowered)
        la, lo, _e, _n = grid([200.0, 240.0], np.arange(-100.0, 101.0, 10.0))
        base = ridge(la, lo) + 3.0          # (3 m over the marsh: not the ridge, and not lowered)
        np.testing.assert_allclose(g.apply(la, lo, base), self.grader(ridge, on=False).apply(la, lo, base),
                                   atol=1e-9)

    def test_valley_and_embankment_under_the_deck_stay(self):
        for elev in (valley, embankment):
            la, lo, _e, _n = grid(np.arange(-150.0, 151.0, 15.0), np.arange(-120.0, 121.0, 8.0))
            on = self.grader(elev).apply(la, lo, elev(la, lo))
            off = self.grader(elev, on=False).apply(la, lo, elev(la, lo))
            np.testing.assert_allclose(on, off, atol=1e-9, err_msg=elev.__name__)

    def test_kept_with_the_plans(self):
        """Another process reading the cell's plans from the plan cache corrects the ground the same, without the
        map's road data (the corrected decks are kept with the plans)."""
        import shutil
        import tempfile
        import plancache
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.environ[plancache.RUN_ENV] = "deckground"
        self.addCleanup(os.environ.pop, plancache.RUN_ENV, None)
        la, lo, _e, _n = grid(np.arange(-150.0, 151.0, 30.0), np.arange(-100.0, 101.0, 10.0))
        a = self.grader(ridge)
        a.shared = plancache.for_grader(root, a, None, None)
        za = a.apply(la, lo, ridge(la, lo))
        md = FakeMD(deck_road())
        with mock.patch.object(grading, "DECK_GROUND_ON", True):
            b = grading.Grader(FRAME, md, ridge)
        b.shared = plancache.for_grader(root, b, None, None)
        zb = b.apply(la, lo, ridge(la, lo))
        self.assertEqual(md.calls, 0, "read from the cache: no road data fetched")
        np.testing.assert_allclose(zb, za, atol=1e-9)
        np.testing.assert_allclose(zb, MARSH, atol=0.05)
        # ... and the far tiles past the graded levels read them too (DeckGroundDem: only what is planned)
        np.testing.assert_allclose(b.deck_ground(la, lo, ridge(la, lo)), MARSH, atol=0.05)

    def test_points_asked_apart_or_together_agree(self):
        g = self.grader(ridge)
        la, lo, _e, _n = grid(np.arange(-150.0, 151.0, 50.0), np.arange(-60.0, 61.0, 20.0))
        whole = g.apply(la, lo, ridge(la, lo))
        one = np.array([g.apply(la[i:i + 1], lo[i:i + 1], ridge(la[i:i + 1], lo[i:i + 1]))[0] for i in range(len(la))])
        np.testing.assert_allclose(whole, one, atol=1e-9)


if __name__ == "__main__":
    unittest.main()
