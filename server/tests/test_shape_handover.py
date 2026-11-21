"""The shapes' solved heights reach what the roads build (grading._Planner._shape_at, the deck / tunnel hooks): the
solve keeps one copy of a road, its lane sections ("<road>~lanes<a>-<b>") or the road, and the roads build the decks
under either id. On the Popoli replay the A25's decks were solved as sections and built under the road's id: no height
found, the decks built at their own, 1 m over the streets under them (the law: 6.35 m)."""
import os
import sys
import types
import unittest
import unittest.mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import gradeshapes as gs  # noqa: E402
import grading  # noqa: E402
import mapcells  # noqa: E402
import revisit  # noqa: E402

REPLAY = os.path.join(HERE, "fixtures", "interchange_popoli_ss17_underpass.npz")
DECKS = ("ae40784f-ac2a-473f-984f-504dbb5fc02b", "08a77b6f-b0da-44b8-a38d-cf4e22f72950")   # (the A25's two decks)
AT = (42.174704, 13.823807)     # (the streets under them)


@unittest.skipUnless(os.path.exists(REPLAY), "the Popoli replay")
class DeckUnderRoadIdTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import interchanges
        from interchange_audit_run import planner_of
        cls.found = []
        real = gs.find_interchanges

        def spy(members, mk, trace=None, want=None):
            net, found = real(members, mk, trace, want)
            cls.found.append((net, [x.sysm for x in found]))
            return net, found
        rep = interchanges.load("popoli_ss17_underpass")
        with unittest.mock.patch.object(revisit, "ON", False), \
                unittest.mock.patch.object(grading, "SHAPES_ON", True), \
                unittest.mock.patch.object(gs, "find_interchanges", spy):
            cls.pl = planner_of(rep)
            cls.home = mapcells.cell_of(*AT)
            cls.pl._shape_region(cls.home)
            cls.finds = list(cls.found)     # (the region's own find, then each interchange's whole)
            # (the decks as the roads build them: the map's own feature, before one_road kept its sections; the A25
            # has many bridges, these are the ones over the streets)
            e, n, _ = cls.pl.frame.geodetic_to_enu(np.array([AT[0]]), np.array([AT[1]]), 0.0)
            at = np.array([float(e[0]), float(n[0])])
            best = {}
            for k in mapcells.cells_for_bbox(AT[0] - 0.006, AT[1] - 0.006, AT[0] + 0.006, AT[1] + 0.006):
                for m in cls.pl._shape_members(k):
                    f = str(m.key[0])
                    if f in DECKS and m.kind == "deck":
                        line = np.asarray(m.line, np.float64)[:, :2]
                        d = float(np.sqrt(((line - at) ** 2).sum(1)).min())
                        if d < 60.0 and (f not in best or d < best[f][0]):
                            best[f] = (d, line)
            cls.lines = {f: v[1] for f, v in best.items()}

    def test_the_road_id_finds_its_sections_solve(self):
        self.assertEqual(sorted(self.lines), sorted(DECKS))
        for f, line in self.lines.items():
            with unittest.mock.patch.object(grading, "SHAPES_ON", True):
                z, modes = self.pl._shape_at(line, line, f, ("deck",))
            self.assertTrue(np.isfinite(z).all(), "%s: %d of %d points without the solve's height"
                            % (f, int((~np.isfinite(z)).sum()), len(z)))
            self.assertEqual(set(modes), {"deck"})

    def test_the_decks_clear_the_streets_under_them(self):
        n = 0
        for net, s in ((n_, s_) for n_, systems in self.finds for s_ in systems):
            for c in s["crossings"]:
                u = str(net.m[c["u"]].key[0]).partition("~lanes")[0]
                if u not in self.lines or net.m[c["d"]].kind != "ground":
                    continue
                q = np.asarray(c["q"], np.float64)
                if float(np.sqrt(((self.lines[u] - q) ** 2).sum(1)).min()) > 3.0:
                    continue        # (another of its bridges)
                with unittest.mock.patch.object(grading, "SHAPES_ON", True):
                    zu, _m = self.pl._shape_at(self.lines[u], q[None], u, ("deck",))
                ground = float(self.pl._ground_line(np.array([q, q + 0.01]))[0])
                need = gs.clearance_need(net, c["u"], c["d"], c["q"])
                self.assertGreaterEqual(float(zu[0]) - ground, need - 0.05, "%s over %s at %s"
                                        % (u[:8], str(net.m[c["d"]].key[0])[:8], np.round(q, 1)))
                n += 1
        self.assertGreaterEqual(n, 5, "the A25's decks cross the streets 5 times")


class RoadOverItselfTest(unittest.TestCase):
    """A road winding over itself: its deck passes another of its levels within SHAPES_NEAR_M, nearer than its own
    piece stands off the line, and takes its own level's height (the nearest piece's was a step of 20 m)."""

    def test_the_deck_takes_its_own_levels_height(self):
        x = np.arange(-40.0, 40.1, 4.0)
        own = np.c_[x, np.full(len(x), 1.2)]                # (the level the road runs along: 1.2 m off its line)
        over = np.c_[x, 0.3 * x]                            # (another level crossing the line at a slant, on it at 0)
        line = np.c_[np.arange(-30.0, 30.1, 2.0), np.zeros(31)]
        reg = {"road": [(own, np.full(len(x), 10.0), "deck"), (over, np.full(len(x), 30.0), "deck")]}
        frame = types.SimpleNamespace(enu_to_geodetic=lambda e, n, h: (e * 0 + 45.0, e * 0 + 7.0, h))
        pl = types.SimpleNamespace(frame=frame, _shape_region=lambda home: reg)
        z, modes = grading._Planner._shape_at(pl, line, line, "road", ("deck",))
        self.assertEqual(set(z.tolist()), {10.0})
        self.assertEqual(set(modes), {"deck"})


class EndBesideItsPieceTest(unittest.TestCase):
    """A deck whose line runs a metre beside its solved piece takes the piece's height to its very end: its endpoint is
    level with the piece's end, not past it (the whole distance to the end point counted the side offset as overshoot,
    and the deck lost the solve's height for all its points, built at its own over the roads under it)."""

    def test_the_end_level_with_the_pieces_end_takes_its_height(self):
        x = np.arange(0.0, 40.1, 4.0)
        piece = np.c_[x, np.zeros(len(x))]
        line = np.c_[np.arange(0.0, 40.1, 2.0), np.full(21, 1.0)]       # (1 m beside it, its ends level with the piece's)
        reg = {"road": [(piece, 10.0 + 0.1 * x, "deck")]}
        frame = types.SimpleNamespace(enu_to_geodetic=lambda e, n, h: (e * 0 + 45.0, e * 0 + 7.0, h))
        pl = types.SimpleNamespace(frame=frame, _shape_region=lambda home: reg)
        z, modes = grading._Planner._shape_at(pl, line, line, "road", ("deck",))
        self.assertTrue(np.isfinite(z).all(), z)
        self.assertAlmostEqual(float(z[-1]), 14.0, places=6)

    def test_a_point_past_the_pieces_end_is_not_its(self):
        x = np.arange(0.0, 40.1, 4.0)
        piece = np.c_[x, np.zeros(len(x))]
        line = np.c_[np.arange(0.0, 44.1, 2.0), np.full(23, 1.0)]       # (its last 4 m past the piece's end)
        reg = {"road": [(piece, 10.0 + 0.1 * x, "deck")]}
        frame = types.SimpleNamespace(enu_to_geodetic=lambda e, n, h: (e * 0 + 45.0, e * 0 + 7.0, h))
        pl = types.SimpleNamespace(frame=frame, _shape_region=lambda home: reg)
        z, _m = grading._Planner._shape_at(pl, line, line, "road", ("deck",))
        self.assertTrue(np.isnan(z[-1]) and np.isfinite(z[-3]), z[-4:])



@unittest.skipUnless(os.path.exists(REPLAY), "the Popoli replay")
class LeftPlaceTest(unittest.TestCase):
    """A region's shapes for a place a teleport left are given up before its next system (xproc.check_cancel): a left
    city's systems (Shanghai's, ~40 s to find and ~40 s to solve a region) held the road tiles of the place the
    player was at for minutes. Nothing is kept of a region given up: asked again, it is solved."""

    def test_given_up_then_solved(self):
        import interchanges
        import pools
        import xproc
        from interchange_audit_run import planner_of
        rep = interchanges.load("popoli_ss17_underpass")
        with unittest.mock.patch.object(revisit, "ON", False),                 unittest.mock.patch.object(grading, "SHAPES_ON", True):
            pl = planner_of(rep)
            home = mapcells.cell_of(*AT)
            xproc.set_thread_cancel(lambda: True)
            try:
                with self.assertRaises(pools.Abandoned):
                    pl._shape_region(home)
            finally:
                xproc.set_thread_cancel(None)
            self.assertNotIn(str(home), pl.__dict__.get("_shape_regions", {}))
            self.assertTrue(pl._shape_region(home))


if __name__ == "__main__":
    unittest.main()
