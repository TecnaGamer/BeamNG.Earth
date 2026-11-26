"""tunnelrec.py (tunnel plan T1): the record's sections as TunnelProfiles stages them, and its hole rule on a block."""
import os
import sys
import unittest
from collections import namedtuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np

import tunnelrec

Spec = namedtuple("Spec", "width cls")


class TunnelRecordTest(unittest.TestCase):
    def test_runs_share_their_end_samples(self):
        self.assertEqual(tunnelrec._runs([1, 1, 2, 2, 2, 0, 0, 2]), [(0, 1, 1), (1, 4, 2), (4, 6, 0), (6, 7, 2)])
        self.assertEqual(tunnelrec._runs([2, 2, 2]), [(0, 2, 2)])

    def test_a_portal_is_never_lifted_onto_the_plans_approach(self):
        # (631: the plans' first stage 5.5 m over the record's portal (Moscow): the floor stays, the approach is dug)
        self.assertEqual(tunnelrec.portal_ground(143.4, 138.0), 138.0)
        self.assertEqual(tunnelrec.portal_ground(137.0, 138.0), 137.0)     # (a drop onto the approach stays)
        self.assertTrue(np.isnan(tunnelrec.portal_ground(float("nan"), 138.0)))
        self.assertEqual(tunnelrec.portal_ground(137.0, float("nan")), 137.0)

    def test_holes_only_at_a_portal_and_never_under_deep_ground(self):
        # a 100 m tube along +x from its portal at x = 0 into a hill rising 1 m per metre; a 1 m lattice from -20 m
        n = 141
        xs = -20.0 + np.arange(n)
        u = np.tile(np.maximum(xs, 0.0), (n, 1))          # u[iy, ix]
        mask = np.zeros((n, n), bool)
        part = ("t.0#0", Spec(7.0, "primary"), np.array([[0.0, 0.0], [100.0, 0.0]]), np.array([0.0, 0.0]), 0.0)
        rest = tunnelrec.holes(mask, u, -20.0, -20.0, 1.0, [part], {}, {"t.0#0": (30.0, 0.0)})
        self.assertEqual(rest, [])
        iy, ix = np.nonzero(mask)
        self.assertTrue(len(ix) > 0)
        x, y = xs[ix], -20.0 + iy
        self.assertTrue((x >= -1.0).all() and (x <= 8.0).all(), sorted(set(x)))
        self.assertTrue((np.abs(y + 0.5) <= 6.0).all(), sorted(set(y)))
        # a part the record has no holes for: left to the legacy rule
        other = ("u.0#0",) + part[1:]
        self.assertEqual(tunnelrec.holes(np.zeros((n, n), bool), u, -20.0, -20.0, 1.0, [other], {}, {}), [other])


class TwinFloors(unittest.TestCase):
    def test_twin_record_floors_at_one_height_end_on_one_seam(self):
        # (636) two bores' floors 7 m apart, 9 m wide, at one height (the record holds twins at one floor: the Caldecott
        # Tunnel): a record's floors are cut on the seam between them (one owner per lane point); legacy tunnel floors
        # are left as they were
        import roadmesh

        def chunk(y, rec):
            c = {"m": "road_asphalt_2lane", "w": 9.0, "tl": 0, "p": 0, "deck": True, "fixed": True, "tunnel": True,
                 "fid": f"t{y}", "n": [[float(x), y, 10.0] for x in range(0, 101, 10)]}
            if rec:
                c["rec"] = True
            return c

        def reach(rec):
            rb = [(r, c) for r, c in roadmesh.ribbon_pairs([chunk(0.0, rec), chunk(7.0, rec)]) if r[5] is not None]
            out = roadmesh.trim_overlaps(rb, (), None, coplanar=True)
            (l0, r0, *_), _c0 = out[0]
            (l1, r1, *_), _c1 = out[1]
            return float(max(l0[:, 1].max(), r0[:, 1].max())), float(min(l1[:, 1].min(), r1[:, 1].min()))
        a, b = reach(True)
        self.assertAlmostEqual(a, 3.5, places=3)
        self.assertAlmostEqual(b, 3.5, places=3)
        a, b = reach(False)
        self.assertGreater(a, 4.4)
        self.assertLess(b, 2.6)


class LegacyPins(unittest.TestCase):
    def test_round_a_tunnel_the_record_does_not_build_the_targets_are_the_old_ones(self):
        # (636) a line with the record's dig at a mouth (low 1) and its legacy variant (the tunnel fade, no mouth low):
        # pins_near takes the legacy one only when legacy() says its nearest tunnel is left off the record (under water)
        import roadsys
        old = roadsys._square_pins
        self.addCleanup(setattr, roadsys, "_square_pins", old)
        la = np.linspace(40.0, 40.0005, 6)
        lo = np.full(6, -74.0)
        line = (np.zeros((6, 2)), la, lo, np.full(6, 5.0), 4.0, np.ones(6), np.ones(6), np.zeros(6), np.ones(6),
                np.zeros(6), np.zeros(6), "tun", 10.0, 40.0, -74.0)
        roadsys._square_pins = lambda fid, frame, sq, cell_fn, ground_ll: [line]

        def today(a, b):
            return np.full(len(a), 7.0)            # (the plans 2 m over the profile: a dig)
        dug = roadsys.pins_near(40.0, -74.0, 40.0005, -74.0, 5.0, None, today)
        self.assertTrue(dug)
        same = roadsys.pins_near(40.0, -74.0, 40.0005, -74.0, 5.0, None, today, legacy=lambda f, a, b: False)
        self.assertEqual(repr(dug), repr(same))
        old_ = roadsys.pins_near(40.0, -74.0, 40.0005, -74.0, 5.0, None, today, legacy=lambda f, a, b: f == "tun")
        self.assertEqual(old_, [], "no dig round a tunnel left off the record")


if __name__ == "__main__":
    unittest.main()
