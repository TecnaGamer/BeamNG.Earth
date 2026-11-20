"""GitHub #1496 (grading.FREEWAY_LONG_LIFT): the A1's viaduct at Pitesti starts on the ground 29-36 m before two of its
own slip roads pass under it. Never raising a motorway's approach, the deck stood 1.3-1.6 m over them: a wall the cars
drove into. Its abutments are raised as the clearance asks now, on one ramp along the motorway behind them, and the
slip roads joining it come up to it. On the replay (fixtures/drive_pitesti_a1_1496.npz, tests/drivability.py's site)."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drivability as dv  # noqa: E402
import grading  # noqa: E402
import interchanges  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402
import roadsurf  # noqa: E402

# (the slip roads under the decks, OSM ways 26436280 and 1391260724, under the eastbound and the westbound deck)
CROSSINGS = {"26436280 EB": (44.868997, 24.880894), "1391260724 EB": (44.868956, 24.880945),
             "26436280 WB": (44.869061, 24.881009), "1391260724 WB": (44.869020, 24.881062)}
SITE = "pitesti_a1_1496"


def headroom(on):
    """m between each crossing's slip road (graded ground) and the motorway deck over it, with FREEWAY_LONG_LIFT on."""
    with mock.patch.object(grading, "FREEWAY_LONG_LIFT", on), mock.patch.object(revisit, "ON", False), \
            mock.patch.object(grading, "PLAN_THREADS", 1), mock.patch.object(roadsurf, "ON", False):
        site = interchanges.load(SITE, "drive", keep_m=dv.KEEP_M)
        md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": site.cells.get(str(key))}),
                             dem=SimpleNamespace(sample=site.sample))
        dv._hook_cells(site, md, SITE + (":on" if on else ":off"))
        store = grading.PlanStore(md, site.sample)
        store.frame_of = lambda region: site.frame
        grader = grading.Grader(site.frame, md, site.sample, store=store)

        def elev(la, lo):
            return grader.apply(la, lo, site.sample(la, lo), grading.ROAD_LIFT)
        decks = [(np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64))
                 for _fid, spec, xy, z, _lon in roads.BridgeProfiles(site.frame, lambda k: site.cells.get(str(k)),
                                                                     elev).parts(list(site.cells.values()),
                                                                                 (-300.0, -300.0, 300.0, 300.0))
                 if spec.cls == "motorway"]
        out = {}
        for name, (la, lo) in CROSSINGS.items():
            e, n, _ = site.frame.geodetic_to_enu(np.array([la]), np.array([lo]), np.array([0.0]))
            p = np.array([float(e[0]), float(n[0])])
            best = None
            for xy, z in decks:
                arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
                s_, off = grading._project_on(p[None], xy, arc)
                if best is None or off[0] < best[0]:
                    best = (float(off[0]), float(np.interp(s_[0], arc, z)))
            g = float(np.asarray(elev(np.array([la]), np.array([lo])), np.float64)[0])
            out[name] = (best[1] - g, best[0])
        return out


class SlipRoadsUnderTheViaductTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.on = headroom(True)

    def test_the_deck_is_found_over_each_crossing(self):
        for name, (_gap, off) in self.on.items():
            self.assertLess(off, 4.0, name)

    def test_the_slip_roads_have_the_clearance_under_the_deck(self):
        for name, (gap, _off) in self.on.items():
            self.assertGreater(gap, grading.UNDER_CLEAR_M - 0.5, name)

    def test_without_the_lift_they_ran_into_it(self):
        for name, (gap, _off) in headroom(False).items():
            self.assertLess(gap, 2.5, name)


if __name__ == "__main__":
    unittest.main()
