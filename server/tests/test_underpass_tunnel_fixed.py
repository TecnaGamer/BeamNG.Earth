"""A road bridge over a road the map has in a short tunnel there (an underpass: the lower road's tunnel, gradesep's open
underpass included) is never raised: the road under dips to clear it (grading UNDER_TUNNEL_FIXED_ON). The SR17 at Popoli
over Via Aldo Moro (42.1779 N 13.8246 E) stood on 5 m and 8 m approach fills where it is level with the ground."""
import os
import sys
import threading
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import grading  # noqa: E402


class _Cells:
    """A MapData stand-in: every cell's roads layer is None (road_underpasses is mocked)."""
    class _C:
        layers = {"roads": None}

    def cell(self, key, layers=None, kind=None):
        return self._C()


def planner():
    p = object.__new__(grading._Planner)
    p.frame = geodesy.EnuFrame(42.17, 13.83)
    p.md = _Cells()
    p.lock = threading.RLock()
    return p


def underpasses(*tunnels, cls="residential"):
    """A road_underpasses stand-in giving these tunnels (xy (k, 2)) of class cls."""
    def fake(frame, cells, box, reach=None, classes=None):
        if classes is not None:
            classes.extend([cls] * len(tunnels))
        return [(t, 4.0) for t in tunnels]
    return fake


BRIDGE = np.array([[0.0, 0.0], [0.0, 20.0], [0.0, 44.0]])       # (44 m north)


class OverMappedUnderpassTest(unittest.TestCase):
    def setUp(self):
        # (the cells' roads: none beside the tunnel unless a test says so)
        patch = mock.patch.object(grading._Planner, "_cell_enu",
                                  lambda self, k, ground=True: {"ground": [], "rail_ground": []})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_bridge_over_a_short_road_tunnel_is_fixed(self):
        tunnel = np.array([[-10.0, 22.0], [10.0, 20.0]])           # (a 20 m tunnel crossing under its middle)
        with mock.patch.object(grading.railwaysmod, "road_underpasses", underpasses(tunnel)):
            self.assertTrue(planner()._over_mapped_underpass(BRIDGE))

    def test_no_tunnel_under_it(self):
        with mock.patch.object(grading.railwaysmod, "road_underpasses", underpasses()):
            self.assertFalse(planner()._over_mapped_underpass(BRIDGE))

    def test_a_tunnel_at_its_end_is_a_junction_not_an_underpass(self):
        tunnel = np.array([[-10.0, 2.0], [10.0, 2.0]])             # (crossing 2 m from its end: APPROACH_MIN_CROSS_M)
        with mock.patch.object(grading.railwaysmod, "road_underpasses", underpasses(tunnel)):
            self.assertFalse(planner()._over_mapped_underpass(BRIDGE))

    def test_a_tunnel_beside_it_is_none(self):
        tunnel = np.array([[5.0, 10.0], [25.0, 10.0]])
        with mock.patch.object(grading.railwaysmod, "road_underpasses", underpasses(tunnel)):
            self.assertFalse(planner()._over_mapped_underpass(BRIDGE))

    def test_not_over_a_motorway_s_or_trunk_s_tunnel(self):
        """A freeway in a short tunnel under a street keeps its own profile: the street's bridge is raised as before
        (Avenue de Neuilly over the Peripherique at Porte Maillot)."""
        tunnel = np.array([[-10.0, 22.0], [10.0, 20.0]])
        for cls in ("motorway", "trunk"):
            with mock.patch.object(grading.railwaysmod, "road_underpasses", underpasses(tunnel, cls=cls)):
                self.assertFalse(planner()._over_mapped_underpass(BRIDGE), cls)

    def test_not_when_it_also_spans_a_motorway_or_a_track(self):
        """Dalton Street, Boston: a bridge over a service tunnel and the Mass Pike's carriageway at the ground: the Pike
        never dips, the deck rises over it as before (and over a track on the ground)."""
        tunnel = np.array([[-10.0, 22.0], [10.0, 20.0]])
        pike = np.array([[-20.0, 10.0], [20.0, 10.0]])
        for hc in ({"ground": [(pike, 6.0, "motorway")], "rail_ground": []},
                   {"ground": [], "rail_ground": [pike]},
                   {"ground": [(pike, 6.0, "residential")], "rail_ground": []}):
            with mock.patch.object(grading.railwaysmod, "road_underpasses", underpasses(tunnel)),                     mock.patch.object(grading._Planner, "_cell_enu", lambda self, k, ground=True, hc=hc: hc):
                got = planner()._over_mapped_underpass(BRIDGE)
            self.assertEqual(got, hc["ground"] != [] and hc["ground"][0][2] == "residential", hc)

    def test_switched_off(self):
        tunnel = np.array([[-10.0, 22.0], [10.0, 20.0]])
        with mock.patch.object(grading.railwaysmod, "road_underpasses", underpasses(tunnel)), \
                mock.patch.object(grading, "UNDER_TUNNEL_FIXED_ON", False):
            self.assertFalse(planner()._over_mapped_underpass(BRIDGE))

    def test_no_lift_for_its_abutments(self):
        """_end_lift_raw_calc gives no lift at the end of a bridge over a mapped underpass."""
        p = planner()
        hc = {"ends": np.array([BRIDGE[0], BRIDGE[-1]]), "end_of": [0, 0], "bridges": [BRIDGE]}
        with mock.patch.object(grading._Planner, "_cell_enu", lambda self, k, ground=True: hc), \
                mock.patch.object(grading._Planner, "_over_mapped_underpass", lambda self, b: True):
            self.assertIsNone(p._end_lift_raw_calc(np.array([0.0, 0.0])))


if __name__ == "__main__":
    unittest.main()
