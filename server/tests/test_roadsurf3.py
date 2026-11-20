"""Road surface v3 phase S2 (roadsurf3.py, earthfast.lane_surface / cut_lines): the ground surface's kernel on small
synthetic cases (strips tiling the road, tapers without a step, the weld onto a road's row, the markings' kinds, the
folds left out, the cut of the tile's own lines with their dash phase). The real-place seam case is test_seamprobe's
(fixtures/seamprobe_v3s2_*.npz, tools/probes/surfprobe.py --fixture)."""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import fast  # noqa: E402
import roadsurf3  # noqa: E402

HAVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= roadsurf3.SURF_VERSION


def quintic(u):
    u = np.clip(u, 0.0, 1.0)
    return u ** 3 * (10 - 15 * u + 6 * u * u)


def piece(n=51, x0=0.0, kinds=(1, 1, 4), widths=None, step=2.0):
    """One straight piece along +x from x0: rows every step m, boundaries from y = 5 down by each strip's width(s)."""
    s = x0 + np.arange(n) * step
    rows = np.c_[s, s, np.zeros(n)]
    nb = len(kinds) + 1
    t = np.zeros((n, nb))
    t[:, 0] = 5.0
    for k in range(len(kinds)):
        w = widths[k](s - x0) if widths is not None and widths[k] is not None else np.full(n, 3.5)
        t[:, k + 1] = t[:, k] - w
    return rows, t


def inputs(pieces, roads=(), structs=(), shoulders=(1.0, 1.0), country="US"):
    rows = [p[0] for p in pieces]
    ts = [p[1] for p in pieces]
    kinds = [list(p[2]) for p in pieces]
    rxy = [np.asarray(r[0], np.float64) for r in roads]
    sxy = [np.asarray(r[0], np.float64) for r in structs]
    offs = lambda parts: np.r_[0, np.cumsum([len(p) for p in parts])].astype(np.int64)  # noqa: E731
    return {"rows": np.concatenate(rows), "row_off": offs(rows),
            "t": np.concatenate([t.reshape(-1) for t in ts]),
            "t_off": np.r_[0, np.cumsum([t.size for t in ts])].astype(np.int64),
            "kinds": np.asarray(sum(kinds, []), np.int64), "k_off": np.r_[0, np.cumsum([len(k) for k in kinds])],
            "pmeta": np.array([[shoulders[0], shoulders[1], 3.5]] * len(pieces), np.float64),
            "rxy": np.concatenate(rxy) if rxy else np.zeros((0, 2)), "r_off": offs(rxy),
            "r_hw": np.array([r[1] for r in roads], np.float64), "r_oneway": np.array([True] * len(roads), bool),
            "sxy": np.concatenate(sxy) if sxy else np.zeros((0, 2)), "s_off": offs(sxy),
            "s_hw": np.array([r[1] for r in structs], np.float64),
            "box": np.array([-1e5, -1e5, 1e5, 1e5]), "params": roadsurf3.params(country)}


def areas(got):
    v, t = got["mesh"]["v"], got["mesh"]["tri"]
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    return ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) / 2.0


@unittest.skipUnless(HAVE, "earthfast >= 542 needed")
class Kernel(unittest.TestCase):
    def test_strips_tile_the_road(self):
        # two lanes and an added lane tapering in over 100 m (quintic), shoulders 1 m: every triangle turned up, the
        # area the strips' (no overlap, no gap)
        rows, t = piece(kinds=(1, 1, 4), widths=(None, None, lambda s: 3.5 * quintic(s / 100.0)))
        got = roadsurf3.run_kernel(inputs([(rows, t, (1, 1, 4))]))
        self.assertEqual(got["kept"], [0])
        ar = areas(got)
        self.assertTrue((ar > 0).all())
        self.assertAlmostEqual(float(ar.sum()), 100.0 * (2 + 7.0) + 100.0 * 1.75, delta=1.0)
        kinds = {name for name, *_ in got["lines"]}
        self.assertTrue({"edge", "edge_left", "lane", "exit"} <= kinds, kinds)
        # the added lane's exit dashes start where it is LINE_W_MIN of a lane wide, never in the taper's thin start
        ex = [xy for name, xy, *_ in got["lines"] if name == "exit"]
        w_at = 3.5 * quintic(min(float(xy[0, 0]) for xy in ex) / 100.0)
        self.assertGreaterEqual(w_at, roadsurf3.LINE_W_MIN * 3.5 - 0.3)

    def test_weld_onto_road_rows(self):
        # a road along y = 1.4 (half width 4.0) through a piece from x = 20 to 120: the road is cut at both end rows,
        # and the piece's end rows ARE the road's cut rows (the same points: a shared row, no gap or overlap)
        rows, t = piece(x0=20.0, kinds=(1, 1))
        road = (np.array([[-50.0, 1.4], [200.0, 1.4]]), 4.0)
        got = roadsurf3.run_kernel(inputs([(rows, t, (1, 1))], roads=[road]))
        st = got["status"][0]
        self.assertIn("weld_lo:road1", st["flags"])
        self.assertIn("weld_hi:road1", st["flags"])
        v, owner, tri = got["mesh"]["v"], got["mesh"]["owner"], got["mesh"]["tri"]
        for x in (20.0, 120.0):
            for y in (5.4, -2.6):
                hit = np.flatnonzero((np.abs(v[:, 0] - x) < 1e-9) & (np.abs(v[:, 1] - y) < 1e-9))
                users = {int(owner[k]) for k in range(len(tri)) if np.isin(tri[k], hit).any()}
                self.assertEqual(users, {0, -1}, (x, y, users))
        # the road strip has nothing between the cuts
        mid = v[tri[owner == -1]].mean(axis=1)
        self.assertFalse(((mid[:, 0] > 21.0) & (mid[:, 0] < 119.0)).any())
        # no width step: the welded ends ease over at least BLEND_MIN_M
        self.assertLessEqual(st["lane_rate"], 0.06)

    def test_gore_lines_and_chevrons(self):
        # through, gore opening to 4 m, branch: channelizing lines where it is wide, chevrons in it (US), hatching
        # elsewhere
        g = lambda s: 4.0 * quintic(s / 100.0)  # noqa: E731
        rows, t = piece(kinds=(1, 2, 3), widths=(None, g, None))
        got = roadsurf3.run_kernel(inputs([(rows, t, (1, 2, 3))]))
        names = [name for name, *_ in got["lines"]]
        self.assertIn("channel", names)
        self.assertGreater(names.count("stripe"), 4)
        got_eu = roadsurf3.run_kernel(inputs([(rows, t, (1, 2, 3))], country="DE"))
        self.assertGreater([n for n, *_ in got_eu["lines"]].count("stripe"), 0)
        # before the gore opens the through / branch boundary is one exit line, not two channel lines
        ex = [xy for name, xy, *_ in got["lines"] if name == "exit"]
        self.assertTrue(ex and min(float(xy[0, 0]) for xy in ex) < 30.0)

    def test_earlier_piece_trims_and_welds(self):
        # two overlapping pieces on one road: the second keeps its rows outside the first and its end lies on the
        # first's end row
        r1, t1 = piece(n=51, x0=0.0, kinds=(1, 1))
        r2, t2 = piece(n=51, x0=60.0, kinds=(1, 1))
        got = roadsurf3.run_kernel(inputs([(r1, t1, (1, 1)), (r2, t2, (1, 1))]))
        st = {s["piece"]: s for s in got["status"]}
        self.assertTrue(st[1]["kept"])
        self.assertIn("weld_lo:piece", st[1]["flags"])
        self.assertLess(float(sum(areas(got))), 160.0 * 9.0 + 1.0)      # (0 .. 160 m once)

    def test_structure_trims(self):
        rows, t = piece(kinds=(1, 1))
        deck = (np.array([[60.0, 1.5], [200.0, 1.5]]), 6.0)
        got = roadsurf3.run_kernel(inputs([(rows, t, (1, 1))], structs=[deck]))
        st = got["status"][0]
        self.assertIn("trim_hi:structure", st["flags"])
        self.assertTrue(any(f.startswith("deck_hi:") for f in st["flags"]), st["flags"])
        self.assertLess(float(got["mesh"]["v"][:, 0].max()), 61.0)

    def test_folds_left_out(self):
        # a road bending back hard: no triangle turned over (roadmesh._keep_up's rule)
        th = np.linspace(0.0, np.pi * 0.9, 40)
        line = np.c_[6.0 * np.cos(th), 6.0 * np.sin(th)]
        got = roadsurf3.run_kernel(inputs([piece(n=3, x0=500.0, kinds=(1,))[0:2] + ((1,),)], roads=[(line, 7.0)]))
        self.assertTrue((areas(got) > 0).all())

    def test_deterministic(self):
        rows, t = piece(kinds=(1, 1, 4), widths=(None, None, lambda s: 3.5 * quintic(s / 100.0)))
        road = (np.array([[-50.0, 1.4], [300.0, 1.4]]), 6.0)
        a = roadsurf3.run_kernel(inputs([(rows, t, (1, 1, 4))], roads=[road]))
        b = roadsurf3.run_kernel(inputs([(rows, t, (1, 1, 4))], roads=[road]))
        self.assertEqual(a["mesh"]["v"].tobytes(), b["mesh"]["v"].tobytes())
        self.assertEqual(a["mesh"]["tri"].tobytes(), b["mesh"]["tri"].tobytes())


@unittest.skipUnless(HAVE, "earthfast >= 542 needed")
class Cuts(unittest.TestCase):
    def test_cut_lines_and_phase(self):
        poly = np.array([[10.0, -5.0], [20.0, -5.0], [20.0, 5.0], [10.0, 5.0]])
        line = {"m": "line_white", "w": 0.15, "n": [[0.0, 0.0, 0.0], [30.0, 0.0, 0.0]], "dash": (3.0, 9.0), "phase": 1.0}
        edge = {"m": "ECA_road_edge_dirt", "w": 1.6, "layer": "edge", "n": [[0.0, 6.0, 0.0], [30.0, 6.0, 0.0]]}
        deck = {"m": "line_white", "w": 0.15, "deck": True, "n": [[0.0, 0.0, 9.0], [30.0, 0.0, 9.0]]}
        wide = np.array([[10.0, -8.0], [20.0, -8.0], [20.0, 8.0], [10.0, 8.0]])
        out = roadsurf3.cut_chunks([line, edge, deck], [poly], [wide], roadsurf3._is_edge)
        lines = [c for c in out if c["m"] == "line_white" and not c.get("deck")]
        self.assertEqual(len(lines), 2)
        a, b = sorted(lines, key=lambda c: c["n"][0][0])
        self.assertAlmostEqual(a["n"][-1][0], 10.0)
        self.assertAlmostEqual(b["n"][0][0], 20.0)
        self.assertAlmostEqual(b["phase"], 1.0 + 20.0)          # (the dash cycle goes on from the line's start)
        self.assertEqual(len([c for c in out if c["m"] == "ECA_road_edge_dirt"]), 2)    # (the wide polygon cuts it)
        self.assertIn(deck, out)


if __name__ == "__main__":
    unittest.main()
