"""Racetracks: the track surface wins (racedig.py, BNE_RACEWAY_DIG; road revamp R1k): no grade separation bridge on a
raceway and its tunnels kept (gradesep), a tunnel under a raceway dug under its band (racedig.dig_part, the Rust
race_dig_floor), no props or wire ends on its band (racedig.clear_props)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fast  # noqa: E402
import geodesy  # noqa: E402
import gradesep  # noqa: E402
import racedig  # noqa: E402
import roads  # noqa: E402
from test_gradesep import cross, hooked  # noqa: E402
from test_roads import features  # noqa: E402


class Switch:
    def __init__(self, on):
        self.on = on

    def __enter__(self):
        self.was = racedig.ON
        racedig.ON = self.on

    def __exit__(self, *a):
        racedig.ON = self.was


def flat(e, n):
    return np.zeros_like(e)


class GradeSepTest(unittest.TestCase):
    """GitHub #1352 / #1220: a service road's short tunnel under the oval (Daytona's, Talladega's) was taken for an
    underpass: opened to the ground and the oval given a 16-19 m deck over it."""

    def test_off_the_raceway_bridges_the_tunnel_as_before(self):
        with Switch(False):
            f = hooked(cross(("raceway", "service"), flags=[(1, "is_tunnel", 0.45, 0.55)]), flat)
            self.assertEqual([k for _, _, k in roads.level_ranges(f, 0)], ["bridge"])
            self.assertEqual(roads.level_ranges(f, 1), [])

    def test_on_no_bridge_on_the_raceway_and_its_tunnel_kept(self):
        with Switch(True):
            f = hooked(cross(("raceway", "service"), flags=[(1, "is_tunnel", 0.45, 0.55)]), flat)
            self.assertEqual(roads.level_ranges(f, 0), [])
            self.assertEqual([k for _, _, k in roads.level_ranges(f, 1)], ["tunnel"])

    def test_on_a_tunnel_under_a_raceway_and_a_road_stays_a_tunnel(self):
        # the infield road crossing over the same tunnel piece would make it an underpass: under a track never
        with Switch(True):
            f = hooked(features([[(-200.0, 0.0), (200.0, 0.0)], [(0.0, -200.0), (0.0, 200.0)],
                                 [(-200.0, 12.0), (200.0, 12.0)]], ["raceway", "service", "secondary"],
                                flags=[(1, "is_tunnel", 0.45, 0.55)]), flat)
            self.assertEqual([k for _, _, k in roads.level_ranges(f, 1)], ["tunnel"])
            self.assertEqual(roads.level_ranges(f, 0), [])

    def test_on_other_roads_unchanged(self):
        with Switch(True):
            f = hooked(cross(("motorway", "secondary"), flags=[(1, "is_tunnel", 0.45, 0.55)]), flat)
            self.assertEqual([k for _, _, k in roads.level_ranges(f, 0)], ["bridge"])
            self.assertEqual(roads.level_ranges(f, 1), [])


class DigTest(unittest.TestCase):
    """A 120 m service tunnel square under a 14 m raceway on flat ground 10 m up, its floor solved 1 m under the ground
    (a shallow underpass the old rules drew as an open road across the track)."""

    def setUp(self):
        fast.require(384, "race_dig_floor")
        self.frame = geodesy.EnuFrame(33.56, -86.06, 0.0)
        self.xy = np.stack([np.zeros(61), np.linspace(-60.0, 60.0, 61)], axis=1)
        self.s = np.linspace(0.0, 120.0, 61)
        self.floor = np.full(61, self.up(9.0))
        self.kinds = np.zeros(61, np.int64)
        self.races = [(np.array([[-300.0, 0.0], [300.0, 0.0]]), 7.0)]

    def up(self, z):
        return float(self.frame.geodetic_to_enu(np.array([33.56]), np.array([-86.06]), np.array([z]))[2][0])

    def elev(self, lat, lon):
        return np.full(np.shape(lat), 10.0)

    def dig(self, races=None):
        return racedig.dig_part(self.frame, self.elev, self.xy, self.s, self.floor, self.kinds,
                                self.races if races is None else races, 2.0 + 1.2, roads.TUNNEL_LOW_H, 6.8)

    def test_floor_under_the_band_leaves_room_for_the_tube_and_its_soil(self):
        floor, kinds, cap = self.dig()
        mid = np.abs(self.xy[:, 1]) <= 7.0 + racedig.BAND_MARGIN_M
        want = 10.0 - roads.TUNNEL_LOW_H - racedig.DIG_SOIL_M - racedig.DIG_SLACK_M
        self.assertTrue((floor[mid] <= self.up(want) + 1e-6).all())
        self.assertEqual(floor[0], self.floor[0])
        self.assertEqual(floor[-1], self.floor[-1])
        self.assertTrue((floor <= self.floor + 1e-12).all())
        g = np.abs(np.diff(floor)) / np.diff(self.s)
        self.assertLessEqual(g.max(), racedig.DIG_MAX_GRADE + 1e-9)
        # covered across the band and its portal clearance, open past it
        self.assertTrue((kinds[mid] == 1).all())
        self.assertEqual(kinds[0], 0)
        self.assertTrue(np.isfinite(cap[mid]).all())
        self.assertTrue(np.isinf(cap[0]))

    def test_no_raceway_nothing(self):
        self.assertIsNone(self.dig(races=[(np.array([[-300.0, 200.0], [300.0, 200.0]]), 7.0)]))

    def test_the_tube_fits_under_the_track(self):
        floor, _kinds, cap = self.dig()
        th = racedig.tube_height(6.8, cap, floor, 20, 40)
        self.assertLessEqual(float(np.min(floor[20:41] + th)), float(np.min(cap[20:41])) + 1e-9)
        self.assertGreaterEqual(th, racedig.MIN_TUBE_H)


    def test_no_room_no_tube(self):
        # Codex review of 47edf660: a 20 m part whose portals cannot move kept a 2.5 m vault 1.46 m over the track;
        # now no tube at all: the road on the ground, the track's surface over the band
        xy = np.stack([np.zeros(11), np.linspace(-10.0, 10.0, 11)], axis=1)
        s = np.linspace(0.0, 20.0, 11)
        floor = np.full(11, self.up(9.0))
        floor2, kinds, cap = racedig.dig_part(self.frame, self.elev, xy, s, floor, np.ones(11, np.int64), self.races,
                                              3.2, roads.TUNNEL_LOW_H, 6.8)
        self.assertTrue((kinds == 0).all())
        self.assertTrue(np.isinf(cap).all())
        self.assertAlmostEqual(float(floor2[5]), self.up(10.0), delta=1e-6)
        self.assertEqual(floor2[0], floor[0])


def _hump(la, lo):
    """Flat at 10 m, a 3 m Gaussian hump across x = 0 (sigma 8 m): the track's smoothing takes its top down."""
    from test_roads import LON, M_LON
    x = (np.asarray(lo, np.float64) - LON) * M_LON
    return 10.0 + 3.0 * np.exp(-0.5 * (x / 8.0) ** 2)


class WindowTest(unittest.TestCase):
    def test_a_sparse_long_straight_reads_the_smoothed_surface(self):
        # Codex review of 47edf660: no vertex of a 600 m straight within the window: the bare ground was read, 3 m on
        # the hump where the track is drawn about 1.3 m lower
        import grading
        from test_roads import FRAME
        line = np.array([[-300.0, 0.0], [300.0, 0.0]])
        pts = np.array([[0.0, 0.0], [2.0, 3.0], [-4.0, -5.0]])
        la, lo, _ = FRAME.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
        raw = _hump(np.asarray(la), np.asarray(lo))
        got = racedig.track_surface(FRAME, _hump, [(line, 7.0)], pts, raw)
        zone = grading.raceway_surface(FRAME, _hump, line, 7.0 + racedig.BAND_MARGIN_M)
        want = grading.raceway_ground(pts[:, 0], pts[:, 1], raw, [zone], margin=1e-6)
        self.assertLess(float(np.max(np.abs(got - want))), 0.02)
        self.assertLess(float(got[0]), float(raw[0]) - 0.5)


class TunnelUnderTrackTest(unittest.TestCase):
    """roads.TunnelProfiles with the dig: a service road's tunnel square under a 14 m raceway on a 6 m embankment."""

    def build(self, half):
        from test_roads import FRAME, LAT, M_LAT
        f = features([[(-300.0, 0.0), (300.0, 0.0)], [(0.0, -half), (0.0, half)]], ["raceway", "service"],
                     flags=[(1, "is_tunnel")], ids=["osm-raceway-000000000001", "t0"])

        flat = getattr(self, "flat", False)

        def elev(la, lo):
            y = (np.asarray(la, np.float64) - LAT) * M_LAT
            if flat:
                return np.full(np.shape(y), 10.0)
            return 10.0 + np.where(np.abs(y) < 14.0, 6.0, np.maximum(0.0, 6.0 - (np.abs(y) - 14.0) * 0.5))
        tp = roads.TunnelProfiles(FRAME, lambda key: f, elev)
        return tp, tp.parts([f], (-128.0, -128.0, 128.0, 128.0)), elev

    def vault_under_track(self, half):
        with Switch(True):
            tp, parts, elev = self.build(half)
            covered = 0
            from test_roads import FRAME
            for fid, spec, xy, z, _l in parts:
                xy = np.asarray(xy, np.float64)[:, :2]
                th = tp.heights.get(fid, 6.8)
                band = np.abs(xy[:, 1]) <= 7.0 + racedig.BAND_MARGIN_M + 3.0
                if not band.any():
                    continue
                covered += 1
                la, lo, _ = FRAME.enu_to_geodetic(xy[band, 0], xy[band, 1], 0.0)
                top = np.asarray(FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo),
                                                       elev(np.asarray(la), np.asarray(lo)))[2])
                self.assertTrue((np.asarray(z)[band] + th + racedig.DIG_SOIL_M <= top + 1e-6).all(),
                                (fid, np.asarray(z)[band] + th, top))
            return covered, tp

    def test_a_long_tube_goes_under_the_track(self):
        covered, _tp = self.vault_under_track(60.0)
        self.assertGreater(covered, 0)

    def test_a_short_tube_is_no_tube_through_the_track(self):
        # (a 20 m mapped part: its portals carried off the band, then a tube under the track or none: never through)
        self.vault_under_track(10.0)

    def test_a_short_tube_under_a_track_at_grade(self):
        # the track on the flat ground: 5 m to dig within a few metres of portals that cannot move
        self.flat = True
        try:
            covered, tp = self.vault_under_track(6.0)
        finally:
            self.flat = False
        self.assertEqual(covered, 0, "no room: no tube")
        self.assertTrue(tp.open, "the road on the ground across the track")


class PropsTest(unittest.TestCase):
    def test_props_and_wire_ends_off_the_band(self):
        lines = [(np.array([[-100.0, 0.0], [100.0, 0.0]]), 7.0)]
        props = [["pole", 0.0, 8.5, 0.0, 0.0], ["pole", 0.0, 9.5, 0.0, 0.0], ["lamp", 5.0, -3.0, 0.0, 0.0],
                 ["lamp", 5.0, -3.0, 6.0, 0.0]]         # (the last on a deck over the track)
        wires = [[[0.0, 8.5, 6.0], [0.0, 40.0, 6.0]], ([[0.0, 30.0, 6.0], [0.0, 40.0, 6.0]], 0.02)]
        n = racedig.clear_props(props, wires, lines, lambda x, y: np.zeros(len(x)))
        self.assertEqual([r[2] for r in props], [9.5, -3.0])
        self.assertEqual(props[1][3], 6.0)
        self.assertEqual(len(wires), 1)
        self.assertEqual(n, 3)

    def test_no_raceway_nothing_dropped(self):
        props = [["pole", 0.0, 0.0, 0.0, 0.0]]
        self.assertEqual(racedig.clear_props(props, [], [], None), 0)
        self.assertEqual(len(props), 1)


if __name__ == "__main__":
    unittest.main()
