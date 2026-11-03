"""GitHub #685: Järve tee under Tartu mnt at Assaku, Estonia (59.3788 N 24.8441 E). The lidar keeps the trunk's two
short decks as ground, so the road under them has a 6 m hump 40 m long in its elevation. Its plan climbed it, and
the underpass dip, measured from the hump, lowered it and the streets joined to it 5.5 m under their ground for about
560 m. A road's ground where a deck crosses over it is read from beside the deck (grading.deck_capped)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402
from test_roads import features, FRAME  # noqa: E402
from test_grading import FakeMD  # noqa: E402

ROAD = 43.0         # the road under, level
BANK = 5.7          # the trunk's embankment over it (its deck kept as ground: no opening)


def bank_profile(y):
    """The ground across the trunk's embankment along y = 0: its top 12 m either side, its toe 20 m."""
    return ROAD + BANK * np.clip((20.0 - np.abs(y)) / 8.0, 0.0, 1.0)


def hump(la, lo):
    return bank_profile(np.asarray(FRAME.geodetic_to_enu(la, lo, 0.0)[1], np.float64))


class DeckCappedTest(unittest.TestCase):
    pts = np.stack([np.zeros(101), np.arange(-100.0, 101.0, 2.0)], axis=1)      # a road north-south
    deck = (np.array([[-10.0, 0.0], [10.0, 0.0]]), 6.0)                         # a 20 m deck east-west over it

    def test_hump_capped_at_the_ground_beside_the_deck(self):
        got = grading.deck_capped(self.pts, bank_profile(self.pts[:, 1]), [self.deck])
        np.testing.assert_allclose(got, ROAD, atol=0.01)

    def test_never_raised(self):
        dip = ROAD - 4.0 * (np.abs(self.pts[:, 1]) < 8.0)          # a road that does dip under its bridge
        np.testing.assert_allclose(grading.deck_capped(self.pts, dip, [self.deck]), dip)

    def test_left_by_a_deck_end_and_without_decks(self):
        z = bank_profile(self.pts[:, 1])
        end = (np.array([[-3.0, 0.0], [17.0, 0.0]]), 6.0)          # 3 m from the deck's end: a junction at grade
        self.assertIs(grading.deck_capped(self.pts, z, [end]), z)
        self.assertIs(grading.deck_capped(self.pts, z, []), z)


def _line_hits_ref(a, b):
    """deck_capped's test of one deck as it was before every near deck was tested at once (2026-11-03)."""
    p, r = a[:-1], np.diff(a, axis=0)
    q, s = b[:-1], np.diff(b, axis=0)
    rxs = r[:, None, 0] * s[None, :, 1] - r[:, None, 1] * s[None, :, 0]
    qp = q[None, :, :] - p[:, None, :]
    ok = np.abs(rxs) > 1e-12
    den = np.where(ok, rxs, 1.0)
    t = (qp[..., 0] * s[None, :, 1] - qp[..., 1] * s[None, :, 0]) / den
    u = (qp[..., 0] * r[:, None, 1] - qp[..., 1] * r[:, None, 0]) / den
    i, j = np.nonzero(ok & (t >= 0.0) & (t <= 1.0) & (u >= 0.0) & (u <= 1.0))
    la, lb = np.hypot(*r.T), np.hypot(*s.T)
    sa, sb = np.r_[0.0, np.cumsum(la)], np.r_[0.0, np.cumsum(lb)]
    sin = np.abs(rxs[i, j]) / np.maximum(la[i] * lb[j], 1e-12)
    return sa[i] + t[i, j] * la[i], sb[j] + u[i, j] * lb[j], sin, float(sb[-1])


def deck_capped_ref(pts, z, decks):
    """deck_capped one deck at a time (its form before 2026-11-03)."""
    if not decks or len(pts) < 2:
        return z
    pts = np.asarray(pts, np.float64)[:, :2]
    lo_p, hi_p = pts.min(0) - grading.UNDER_DECK_MAX_M, pts.max(0) + grading.UNDER_DECK_MAX_M
    wins = []
    for line, half in decks:
        line = np.asarray(line, np.float64)[:, :2]
        if len(line) < 2 or (line.max(0) < lo_p).any() or (line.min(0) > hi_p).any():
            continue
        arcs, on_deck, sin, length = _line_hits_ref(pts, line)
        for a, b, sn in zip(arcs, on_deck, sin):
            if grading.APPROACH_MIN_CROSS_M <= b <= length - grading.APPROACH_MIN_CROSS_M:
                h = min(float(half) / max(float(sn), 0.2), grading.UNDER_DECK_MAX_M) + grading.UNDER_DECK_SIDE_M
                wins.append((float(a) - h, float(a) + h))
    if not wins:
        return z
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))]
    z = np.asarray(z, np.float64)
    out = z.copy()
    wins.sort()
    merged = [list(wins[0])]
    for a, b in wins[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for a, b in merged:
        a, b = max(a, 0.0), min(b, float(s[-1]))
        inn = (s > a) & (s < b)
        if b - a < 1e-6 or not inn.any():
            continue
        za, zb = float(np.interp(a, s, z)), float(np.interp(b, s, z))
        out[inn] = np.minimum(out[inn], za + (zb - za) * (s[inn] - a) / (b - a))
    return out if (out < z).any() else z


class DeckHitsTest(unittest.TestCase):
    def test_every_near_deck_at_once_is_bit_for_bit_one_at_a_time(self):
        """Wiggly roads under a city's worth of random decks (some colinear with the road, some ending on it), in
        small chunks too: the same answer to the last bit, and z itself where nothing is capped."""
        rng = np.random.default_rng(698)
        decks = []
        for _ in range(120):
            c = rng.uniform(-400.0, 400.0, 2) + 5000.0
            d = rng.normal(0.0, 1.0, 2)
            pts = c + np.cumsum(rng.normal(d * 15.0, 4.0, (int(rng.integers(2, 9)), 2)), axis=0)
            decks.append((pts, float(rng.uniform(2.0, 12.0))))
        decks.append((np.array([[5000.0, 4800.0], [5000.0, 5200.0]]), 5.0))        # along the first road
        decks.append((np.array([[5000.0, 5100.0]]), 5.0))                           # no segment
        table = grading._Decks(decks)
        keep = grading.DECK_HITS_CELLS
        try:
            for cells in (keep, 7):
                grading.DECK_HITS_CELLS = cells
                for n in range(40):
                    if n == 0:
                        road = np.stack([np.full(300, 5000.0), np.linspace(4700.0, 5300.0, 300)], axis=1)
                    else:
                        c = rng.uniform(-300.0, 300.0, 2) + 5000.0
                        road = c + np.cumsum(rng.normal(rng.normal(0.0, 1.0, 2) * 2.0, 0.5,
                                                        (int(rng.integers(2, 400)), 2)), axis=0)
                    z = 40.0 + 6.0 * np.sin(np.arange(len(road)) / 7.0)
                    want = deck_capped_ref(road, z, decks)
                    for d in (table, decks):
                        got = grading.deck_capped(road, z, d)
                        self.assertEqual(got is z, want is z)
                        self.assertEqual(np.asarray(got).tobytes(), np.asarray(want).tobytes())
        finally:
            grading.DECK_HITS_CELLS = keep


class UnderDeckTest(unittest.TestCase):
    def test_road_under_keeps_its_level(self):
        f = features([[(-400, 0), (400, 0)], [(0, -400), (0, 400)]], ["trunk", "tertiary"],
                      flags=[(0, "is_bridge", 390 / 800, 410 / 800)], ids=["tartu", "jarve"])
        g = grading.Grader(FRAME, FakeMD(f), hump)
        ys = np.arange(-150.0, 151.0, 2.0)
        la, lo, _ = FRAME.enu_to_geodetic(np.zeros(len(ys)), ys, 0.0)
        z = np.asarray(g.apply(la, lo, hump(la, lo)), np.float64)
        far = np.abs(ys) >= 60.0
        self.assertLess(np.abs(z[far] - ROAD).max(), 0.3, f"no trench beside the bridge: {z[far].min():.2f}")
        depth = roads.deck_depth(roads.geom_span([np.array([[-10.0, 0.0], [10.0, 0.0]])]))
        need = ROAD + BANK - roads.deck_road_clear(depth) - grading.ROAD_LIFT     # (the deck on the embankment's top)
        under = z[np.abs(ys) <= 4.0]
        self.assertLess(under.max(), need + 0.2, "the road clears the deck")
        self.assertGreater(z.min(), need - 0.6, "dug only as deep as the clearance needs")


if __name__ == "__main__":
    unittest.main()
