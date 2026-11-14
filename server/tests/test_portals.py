"""Tunnel entrances (2026-11-12, the user: "focus on tunnel entrances ... Many entrances are broken or ugly and look
nothing like the real version"): the structure at a mouth built from the ground round it (portals.fit / structure,
earthfast.portal_mesh), the tube's outside where the ground does not cover it (portals.cover), the look from the
road and the ground (earthfast.portal_style)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import portals  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402


def _tube(cls, elev, line=((-100, 0), (100, 0))):
    from test_roads import features, FRAME
    f = features([list(line)], [cls], flags=[(0, "is_tunnel")])
    tp = roads.TunnelProfiles(FRAME, lambda key: f, elev)
    ch = roads._tunnel_chunks(tp, [f], (-128.0, -128.0, 128.0, 128.0))
    return [c for c in ch if c.get("tunnel")]


def _ridge(peak=40.0, slope=0.8, half=30.0):
    """A ridge over x in (-half, half) rising `slope` per m from 10 m to 10 + peak, flat ground beyond."""
    from test_roads import LON, M_LON

    def elev(la, lo):
        e = (np.asarray(lo) - LON) * M_LON
        return 10.0 + np.clip((half + 20.0 - np.abs(e)) * slope, 0.0, peak)
    return elev


@unittest.skipUnless(portals.ON, "BNE_PORTALS=0")
class Entrance(unittest.TestCase):
    def test_a_mountain_bore_gets_a_headwall_not_a_slab(self):
        tube = _tube("primary", _ridge())
        self.assertTrue(tube)
        fits = [f for c in tube for f in (c.get("fit") or []) if f is not None]
        self.assertTrue(fits, "each mouth has its ground fit")
        P, T, UV, mats, M = roadmesh.tunnel(tube, (0.0, 0.0, 0.0))
        # the old face stood PORTAL_FRAME (6 m) beside the opening at the vault's height + 6 m: a slab on the hillside.
        # Now nothing of the structure stands more than its parapet over the ground beside the opening ...
        th = max(float(c.get("tunnel_h") or roadmesh.TUNNEL_H) for c in tube)
        hw = float(tube[0]["w"]) / 2 + roadmesh.TUNNEL_SHOULDER
        self.assertLess(np.abs(P[:, 1]).max(), hw + portals.FACE_SIDE + 2.0, "no frame 6 m beside the opening")
        # ... and its top over the opening is no higher than the opening needs (th + top_max)
        z0 = min(float(np.asarray(c["n"])[:, 2].min()) for c in tube)
        self.assertLess(P[:, 2].max(), z0 + th + max(lk[6] for lk in portals.LOOKS.values()) + 0.5)

    def test_flat_ground_beside_the_mouth_brings_the_headwall_down(self):
        # the headwall's top: the parapet over the ground where it is high, sloping down to end_min where it is low
        fitd = {"st": 1, "fx": [-8.0, -5.0, 0.0, 5.0, 8.0], "fg": [0.0, 0.0, 9.0, 0.0, 0.0], "ws": [2.0, 4.0],
                "wl": None, "wr": None, "rz": [0.0, 0.0], "sl": 3.0, "sr": 3.0}
        sec = roadmesh._tunnel_section(5.0, 6.8)
        P, T, UV, C = portals.structure(fitd, np.zeros(3), np.array([0.0, 1.0]), np.array([1.0, 0.0]), sec, 6.8, 5.0,
                                        4.0)
        self.assertTrue(len(T))
        ends = P[np.abs(P[:, 1]) > 7.9]
        lk = portals.LOOKS[1]
        self.assertLessEqual(ends[:, 2].max(), lk[8] + 1e-6, "the wall's ends at end_min where the ground is low")
        mid = P[np.abs(P[:, 1]) < 0.2]
        self.assertGreaterEqual(mid[:, 2].max(), 6.8 + lk[5] - 1e-6, "over the opening at least th + top_min")

    def test_thin_ground_over_a_full_tube_is_covered(self):
        # a long tube under a low saddle (the ground 1 m over its vault in the middle): the outside built there
        mid = np.c_[np.arange(0.0, 50.0, 5.0), np.zeros(10)]
        left = np.tile([0.0, 1.0], (10, 1))
        z = np.zeros(10)
        g = np.full(10, 20.0)
        g[4:6] = 6.0                    # under th + COVER_NEED
        sec = roadmesh._tunnel_section(5.0, 6.8)
        P, T, UV, C = portals.cover(mid, left, z, g, 5.0, 6.8, (False, False), 4.0, 1.0, sec)
        self.assertTrue(len(T))
        self.assertGreaterEqual(P[:, 2].max(), 6.8 + portals.COVER_OVER - 1e-6)
        self.assertTrue((np.asarray(C) == 1).any(), "soil on its top")
        self.assertTrue(((P[:, 0] >= 10.0 - 1e-6) & (P[:, 0] <= 35.0 + 1e-6)).all(), "only round the thin rows")
        # its ends inside the hill leave the tube's opening clear (no wall across the road)
        tri = P[np.asarray(T)]
        cen = tri.mean(1)
        ends = np.all(np.abs(tri[:, :, 0] - tri[:, :1, 0]) < 1e-6, axis=1)
        inside = ends & (np.abs(cen[:, 1]) < 4.0) & (cen[:, 2] > 0.2) & (cen[:, 2] < 4.0)
        self.assertFalse(inside.any(), "an end cap across the tube's opening")

    def test_styles_from_the_ground(self):
        import fast
        fast.require(portals.VERSION, "portal_style")
        ef = fast._ef
        self.assertEqual(ef.portal_style(True, 6.8, 10.0, 0.5), 0)    # a motorway bore into a mountain: a raked hood
        self.assertEqual(ef.portal_style(False, 6.8, 10.0, 0.5), 2)   # a lane's in a hillside
        self.assertEqual(ef.portal_style(True, 6.8, 1.0, 0.0), 1)     # under flat ground: cut-and-cover
        self.assertEqual(ef.portal_style(True, 6.8, 10.0, 4.0), 0)    # out of a rock cut into a hill: still a hood
        self.assertEqual(ef.portal_style(True, 6.8, 10.0, 9.5), 1)    # out of a trough deeper than the opening


class Box(unittest.TestCase):
    def test_a_major_road_s_low_tube_is_a_box(self):
        sec = roadmesh._tunnel_section(9.0, 5.0, low=True, box=True)
        top = sec[sec[:, 1] > 4.9]
        self.assertGreaterEqual(len(top), 2)
        self.assertGreater(np.ptp(top[:, 0]), 15.0, "a flat roof across")
        self.assertAlmostEqual(sec[:, 1].max(), 5.0)


if __name__ == "__main__":
    unittest.main()
