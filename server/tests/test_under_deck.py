"""GitHub #685: Järve tee under Tartu mnt at Assaku, Estonia (59.3788 N 24.8441 E). The lidar keeps the trunk's two
short decks as ground, so the road under them has a 6 m hump 40 m long in its elevation. Its plan climbed it, and
the underpass dip, measured from the hump, lowered it and the streets joined to it 5.5 m under their ground for about
560 m. A road's ground where a deck crosses over it is read from beside the deck (grading.deck_capped)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dnp  # noqa: E402
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
    la, lb = dnp.hypot(*r.T), dnp.hypot(*s.T)
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
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(pts, axis=0).T))]
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


DECK_HITS_CELLS = 1 << 18   # (pts segments x deck segments) a line test took at once (memory: ~8 arrays)


def _seg_old(decks, k):
    """grading._Decks.seg(k) as it was: line k's (segment starts (m, 2), vectors (m, 2), lengths (m,), arcs at their
    starts (m,), its length), or None without a segment."""
    segs = decks.__dict__.setdefault("_segs_old", {})
    got = segs.get(k, False)
    if got is not False:
        return got
    b = np.asarray(decks[k][0], np.float64)[:, :2]
    got = None
    if len(b) >= 2:
        s = np.diff(b, axis=0)
        lb = dnp.hypot(*s.T)
        sb = np.r_[0.0, np.cumsum(lb)]
        got = (b[:-1], s, lb, sb[:-1], float(sb[-1]))
    segs[k] = got
    return got


def _deck_hits_old(pts, decks, near):
    """Where polyline pts (n, 2) crosses the near decks of _Decks decks (their indices): (arcs on pts, arcs on the
    deck, |sin| of the angle, the deck's index) of each crossing, every near deck's segments in one test (a loop per
    deck was most of deck_capped's ~0.1 ms a call, 2026-11-03), and pts' arcs (n,). The same products in the same
    order per pair of segments as one deck at a time (elementwise: bit for bit). Pure."""
    p, r = pts[:-1], np.diff(pts, axis=0)
    la = dnp.hypot(*r.T)
    sa = np.r_[0.0, np.cumsum(la)]
    ks, sg = [], []
    for k in near:
        got = _seg_old(decks, int(k))
        if got is not None:
            ks.append(int(k))
            sg.append(got)
    if not sg:
        return (), (), (), (), sa
    q_all, s_all = np.concatenate([g[0] for g in sg]), np.concatenate([g[1] for g in sg])
    lb_all, sb_all = np.concatenate([g[2] for g in sg]), np.concatenate([g[3] for g in sg])
    own = np.repeat(np.asarray(ks, np.int64), [len(g[2]) for g in sg])
    out_a, out_b, out_s, out_k = [], [], [], []
    step = max(1, DECK_HITS_CELLS // max(1, len(r)))
    for c0 in range(0, len(own), step):
        q, s = q_all[c0:c0 + step], s_all[c0:c0 + step]
        s0, s1 = s[None, :, 0], s[None, :, 1]
        rxs = r[:, None, 0] * s1 - r[:, None, 1] * s0
        qp0, qp1 = q[None, :, 0] - p[:, None, 0], q[None, :, 1] - p[:, None, 1]
        ok = np.abs(rxs) > 1e-12
        den = np.where(ok, rxs, 1.0)
        t = (qp0 * s1 - qp1 * s0) / den
        i, j = np.nonzero(ok & (t >= 0.0) & (t <= 1.0))
        if not len(i):
            continue
        # (the arc on the deck where the arc on pts is in range only: the same products, pair by pair)
        u = (qp0[i, j] * r[i, 1] - qp1[i, j] * r[i, 0]) / den[i, j]
        m = (u >= 0.0) & (u <= 1.0)
        if not m.any():
            continue
        i, j, u = i[m], j[m], u[m]
        g = c0 + j
        lb = lb_all[g]
        out_s.append(np.abs(rxs[i, j]) / np.maximum(la[i] * lb, 1e-12))
        out_a.append(sa[i] + t[i, j] * la[i])
        out_b.append(sb_all[g] + u * lb)
        out_k.append(own[g])
    if not out_a:
        return (), (), (), (), sa
    return (np.concatenate(out_a), np.concatenate(out_b), np.concatenate(out_s), np.concatenate(out_k), sa)


def deck_capped_old(pts, z, decks):
    """grading.deck_capped in numpy as it was before its native kernel (underdeck.rs, 2026-11-08): every near deck's
    segments tested at once (_deck_hits_old), the oracle the kernel must equal to the last bit."""
    if not decks or len(pts) < 2:
        return z
    pts = np.asarray(pts, np.float64)[:, :2]
    lo_p, hi_p = pts.min(0) - grading.UNDER_DECK_MAX_M, pts.max(0) + grading.UNDER_DECK_MAX_M
    if isinstance(decks, grading._Decks):           # (every deck's box at once; ~0.9 ms a call in Manhattan as a loop)
        box = decks.box
        near = np.flatnonzero((box[:, 2] >= lo_p[0]) & (box[:, 3] >= lo_p[1]) & (box[:, 0] <= hi_p[0])
                              & (box[:, 1] <= hi_p[1]))
    else:                                   # (a plain list: each deck's own test, the near ones' segments made here)
        lines = [(np.asarray(ln, np.float64)[:, :2], h) for ln, h in decks]
        decks = grading._Decks([(ln, h) for ln, h in lines
                                if len(ln) >= 2 and not ((ln.max(0) < lo_p).any() or (ln.min(0) > hi_p).any())])
        near = np.arange(len(decks))
    if not len(near):
        return z
    arcs, on_deck, sin, which, s = _deck_hits_old(pts, decks, near)
    wins = []
    for a, b, sn, k in zip(arcs, on_deck, sin, which):
        length = _seg_old(decks, int(k))[4]
        if grading.APPROACH_MIN_CROSS_M <= b <= length - grading.APPROACH_MIN_CROSS_M:
            h = min(float(decks[int(k)][1]) / max(float(sn), 0.2), grading.UNDER_DECK_MAX_M) \
                + grading.UNDER_DECK_SIDE_M
            wins.append((float(a) - h, float(a) + h))
    if not wins:
        return z
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
        """Wiggly roads under a city's worth of random decks (some colinear with the road, some ending on it): the
        native kernel, numpy's every-deck-at-once test (in small chunks too) and the one-deck-at-a-time form give the
        same answer to the last bit, and z itself where nothing is capped."""
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
        global DECK_HITS_CELLS
        keep = DECK_HITS_CELLS
        try:
            for cells in (keep, 7):
                DECK_HITS_CELLS = cells
                for n in range(40):
                    if n == 0:
                        road = np.stack([np.full(300, 5000.0), np.linspace(4700.0, 5300.0, 300)], axis=1)
                    else:
                        c = rng.uniform(-300.0, 300.0, 2) + 5000.0
                        road = c + np.cumsum(rng.normal(rng.normal(0.0, 1.0, 2) * 2.0, 0.5,
                                                        (int(rng.integers(2, 400)), 2)), axis=0)
                    if n % 5 == 1:          # (repeated points: zero-length segments, np.interp's equal arcs)
                        road = np.repeat(road, rng.integers(1, 3, len(road)), axis=0)
                    z = 40.0 + 6.0 * dnp.sin(np.arange(len(road)) / 7.0)
                    if n % 7 == 3:          # (sea level, flat stretches)
                        z = np.where(np.arange(len(road)) % 30 < 12, 0.0, z)
                    want = deck_capped_ref(road, z, decks)
                    for d in (table, decks):
                        for f in (grading.deck_capped, deck_capped_old):
                            got = f(road, z, d)
                            self.assertEqual(got is z, want is z)
                            self.assertEqual(np.asarray(got).tobytes(), np.asarray(want).tobytes())
        finally:
            DECK_HITS_CELLS = keep


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
