"""The shapes' solved heights reach what the roads build (grading._Planner._shape_at, the deck / tunnel hooks): the
solve keeps one copy of a road, its lane sections ("<road>~lanes<a>-<b>") or the road, and the roads build the decks
under either id. On the Popoli replay the A25's decks were solved as sections and built under the road's id: no height
found, the decks built at their own, 1 m over the streets under them (the law: 6.35 m)."""
import os
import sys
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
        real = gs.find_systems

        def spy(members, mk):
            net, systems = real(members, mk)
            cls.found.append((net, systems))
            return net, systems
        rep = interchanges.load("popoli_ss17_underpass")
        with unittest.mock.patch.object(revisit, "ON", False), \
                unittest.mock.patch.object(grading, "SHAPES_ON", True), \
                unittest.mock.patch.object(gs, "find_systems", spy):
            cls.pl = planner_of(rep)
            cls.home = mapcells.cell_of(*AT)
            cls.pl._shape_region(cls.home)
            cls.net, cls.systems = cls.found[-1]       # (the home region's: later lookups solve their own regions)
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
        net, systems = self.net, self.systems
        n = 0
        for s in systems:
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


if __name__ == "__main__":
    unittest.main()
