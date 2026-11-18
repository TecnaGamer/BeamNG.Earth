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

    def test_an_assembly_goes_as_one(self):
        # the user's reports 56/59/66/70 (Daytona, Atlanta 2026-11-17): a lit raceway's utility pole just inside the
        # band went, its cobra-head arm 7 m up and the wire ending 0.8 m farther out stayed in the air
        import furniture
        lines = [(np.array([[-100.0, 0.0], [100.0, 0.0]]), 7.0)]
        pole = lambda x, y, z=0.0: [furniture.G_POLE, x, y, z, 90.0, 1.0, 9.2, 0.0]
        arm = lambda x, y, z=0.0: [furniture.G_POLELAMP, x, y, z + furniture.POLE_LAMP_Z, 90.0, 1.0, 1.1, 0.0]
        rows = [pole(0.0, 8.7), arm(0.0, 8.7),              # inside the band (9 m): all of it goes
                pole(40.0, 9.6), arm(40.0, 9.6),            # outside: stays
                pole(-40.0, 8.0, 5.0), arm(-40.0, 8.0, 5.0)]  # on a deck 5 m over the track: stays, arm and all
        props = rows + furniture.lamp_lenses(rows)
        self.assertEqual(len(props), 12)
        wires = [[[0.0, 8.7 + d, 7.8], [40.0, 9.6 + d, 7.8]] for d in (-0.8, 0.0, 0.8)]   # (the 0.8 one ends at 9.5)
        wires += [[[40.0, 9.6 + d, 7.8], [80.0, 9.6 + d, 7.8]] for d in (0.0, 0.8)]      # (between kept poles)
        racedig.clear_props(props, wires, lines, lambda x, y: np.zeros(len(x)))
        self.assertEqual(sorted({(r[1], round(r[2], 1)) for r in props if r[0] in (furniture.G_POLE,
                                                                                  furniture.G_POLELAMP)}),
                         [(-40.0, 8.0), (40.0, 9.6)])
        lenses = [r for r in props if r[0] in (furniture.G_LENS, furniture.G_GLOW)]
        self.assertEqual(len(lenses), 4, "the kept arms' lenses and glows, none of the dropped one's")
        self.assertTrue(all(abs(r[1]) > 1.0 for r in lenses))
        self.assertEqual(len(wires), 2, "every wire of the dropped pole, the kept poles' span stays")
        self.assertTrue(all(w[0][0] == 40.0 for w in wires))

    def test_a_lamp_lens_follows_its_post(self):
        # a post outside the band, its head over the track: the lens stays with the post (judged alone, a lens over
        # the band with no ground given went)
        import furniture
        lines = [(np.array([[-100.0, 0.0], [100.0, 0.0]]), 7.0)]
        post = [furniture.G_LAMP, 0.0, 10.0, 0.0, 270.0, 1.0, 9.0, 0.0]
        props = [post] + furniture.lamp_lenses([post])
        self.assertLess(abs(props[1][2]), 9.0, "(the head over the band)")
        racedig.clear_props(props, [], lines, None)
        self.assertEqual(len(props), 3)
        post = [furniture.G_LAMP, 0.0, 8.0, 0.0, 270.0, 1.0, 9.0, 0.0]
        props = [post] + furniture.lamp_lenses([post])
        racedig.clear_props(props, [], lines, lambda x, y: np.zeros(len(x)))
        self.assertEqual(props, [])

    def test_a_pylon_takes_its_wires(self):
        import powerlines
        lines = [(np.array([[-100.0, 0.0], [100.0, 0.0]]), 7.0)]
        props = [[powerlines.G_PYLON, 0.0, 8.5, 0.0, 0.0, 1.0, 35.8, 0.0]]
        # (its far conductor ends 4.12 m out, off the band)
        wires = [[[0.0, 12.62, 20.0], [300.0, 12.62, 20.0]], [[50.0, 300.0, 20.0], [60.0, 300.0, 20.0]]]
        racedig.clear_props(props, wires, lines, lambda x, y: np.zeros(len(x)))
        self.assertEqual(props, [])
        self.assertEqual(len(wires), 1)

    def test_a_span_goes_with_the_next_tiles_pole(self):
        # Codex's review of 1bc9f8cb: the span is this tile's (its first pole's), the pole at its other end the next
        # tile's, on the band (culled there); the wire's end 1 m off it and off the band went by no geometric test
        import furniture
        lines = [(np.array([[30.0, -100.0], [30.0, 100.0]]), 7.0)]          # the band to x = 30 + 7 + 2 = 39 ... 21
        a = [furniture.G_POLE, -20.0, 0.0, 0.0, 0.0, 1.0, 9.2, 0.0]
        b = [furniture.G_POLE, 38.5, 0.0, 0.0, 0.0, 1.0, 9.2, 0.0]           # (the next tile's row)
        line = np.c_[np.linspace(-20.0, 39.6, 9), np.full(9, 0.0), np.full(9, 7.8)]
        wires = furniture.with_ends([line], a, b) + [line.copy()]
        props = [list(a)]
        racedig.clear_props(props, wires, lines, lambda x, y: np.zeros(len(x)))
        self.assertEqual(props, [a])
        self.assertEqual(len(wires), 1)
        self.assertIsNone(furniture.wire_ends(wires[0]))
        # its pole on a deck over the track (5 m over the ground there): it stays, and the span with it
        b2 = list(b)
        b2[3] = 5.0
        wires = furniture.with_ends([line], a, b2)
        racedig.clear_props([list(a)], wires, lines, lambda x, y: np.zeros(len(x)))
        self.assertEqual(len(wires), 1)

    def test_the_far_supports_road_cells_are_read(self):
        # Codex's review of 26580e0c: a power span's far tower may stand 1 km off, by a raceway in no road cell of the
        # span's tile; its own tile culls it, so the span's tile must read the cells round it for the band
        from unittest import mock
        import furniture
        import mapcells
        frame = geodesy.EnuFrame(33.38, -84.32)
        near, far = [furniture.G_POLE, 0.0, 0.0, 0.0, 0.0, 1.0, 9.2, 0.0], ["pylon", 800.0, 0.0, 0.0, 0.0, 1.0, 35.8, 0.0]
        line = np.c_[np.linspace(0.0, 800.0, 9), np.full(9, 4.5), np.full(9, 20.0)]
        la, lo, _ = frame.enu_to_geodetic(np.array([800.0]), np.array([0.0]), 0.0)
        far_cell = mapcells.cell_of(float(np.ravel(la)[0]), float(np.ravel(lo)[0]))

        class Md:
            def cell(self, key, layers=(), kind="demand"):
                return mock.Mock(layers={"roads": ("roads", key)})

        def bands(frame_, cells):       # (the raceway by the far tower: only in the far tower's cell)
            if any(c == ("roads", far_cell) for c in cells):
                return [(np.array([[700.0, -8.0], [900.0, -8.0]]), 7.0)]
            return []
        flat = lambda la_, lo_: np.zeros(np.shape(la_))
        with mock.patch.object(racedig, "ON", True), mock.patch.object(racedig, "bands", bands):
            wires = furniture.with_ends([line], near, far)
            racedig.tile_clear(frame, [], [list(near)], wires, flat)
            self.assertEqual(len(wires), 1, "(the tile's own cells: no band, the span stayed)")
            racedig.tile_clear(frame, [], [list(near)], wires, flat, Md())
            self.assertEqual(wires, [])

    def test_an_unreadable_support_cell_costs_only_its_spans(self):
        # Codex's review of 12b731e1: a far support's road cell that cannot be read (OSError) aborted the whole road
        # tile; now the tile completes, the spans needing that cell are left out and the tile is not kept (a fault)
        from unittest import mock
        import furniture
        import mapcells
        import revisit
        frame = geodesy.EnuFrame(33.38, -84.32)
        near = [furniture.G_POLE, 0.0, 0.0, 0.0, 0.0, 1.0, 9.2, 0.0]
        far = ["pylon", 6000.0, 0.0, 0.0, 0.0, 1.0, 35.8, 0.0]     # (another map cell)
        other = [furniture.G_POLE, 40.0, 0.0, 0.0, 0.0, 1.0, 9.2, 0.0]
        la, lo, _ = frame.enu_to_geodetic(np.array([6000.0]), np.array([0.0]), 0.0)
        far_cell = mapcells.cell_of(float(np.ravel(la)[0]), float(np.ravel(lo)[0]))

        class Md:
            def cell(self, key, layers=(), kind="demand"):
                if key == far_cell:
                    raise OSError("unreadable")
                return mock.Mock(layers={"roads": ("roads", key)})
        line = np.c_[np.linspace(0.0, 6000.0, 9), np.full(9, 4.5), np.full(9, 20.0)]
        short = np.c_[np.linspace(0.0, 40.0, 5), np.full(5, 0.8), np.full(5, 7.8)]
        wires = furniture.with_ends([line], near, far) + furniture.with_ends([short], near, other)
        before = revisit.faults()
        flat = lambda la_, lo_: np.zeros(np.shape(la_))
        with mock.patch.object(racedig, "ON", True), mock.patch.object(racedig, "bands", lambda f, c: []):
            racedig.tile_clear(frame, [], [list(near)], wires, flat, Md())
        self.assertEqual(len(wires), 1)
        self.assertEqual(furniture.wire_ends(wires[0])[1][1], 40.0)
        self.assertGreater(revisit.faults(), before)

    def test_a_support_by_the_antimeridian(self):
        # Codex's review of 117bdde7: a support whose 60 m box crosses +-180 deg made min / max of its longitudes the
        # whole world (over 4,096 cells: ValueError, the road tile lost); the span's own tile is west of the crossing
        from unittest import mock
        import furniture
        frame = geodesy.EnuFrame(10.0, 179.99)
        la, lo, _ = frame.enu_to_geodetic(np.array([1090.0]), np.array([0.0]), 0.0)
        self.assertGreater(float(np.ravel(lo)[0]), 179.9995)                # (within 60 m of the antimeridian)
        near = [furniture.G_POLE, 0.0, 0.0, 0.0, 0.0, 1.0, 9.2, 0.0]
        far = ["pylon", 1090.0, 0.0, 0.0, 0.0, 1.0, 35.8, 0.0]
        line = np.c_[np.linspace(0.0, 1090.0, 9), np.full(9, 4.5), np.full(9, 20.0)]
        cells = racedig.support_cells(frame, furniture.with_ends([line], near, far))
        far_cells = cells[(1090.0, 0.0)]
        self.assertTrue(0 < len(far_cells) <= 4, far_cells)
        self.assertEqual({k.x for k in far_cells}, {0, (1 << far_cells[0].z) - 1})    # (both sides of it)

        class Md:
            def cell(self, key, layers=(), kind="demand"):
                return mock.Mock(layers={"roads": ("roads", key)})
        wires = furniture.with_ends([line], near, far)
        flat = lambda la_, lo_: np.zeros(np.shape(la_))
        with mock.patch.object(racedig, "ON", True), mock.patch.object(racedig, "bands", lambda f, c: []):
            racedig.tile_clear(frame, [], [list(near)], wires, flat, Md())
        self.assertEqual(len(wires), 1)

    def test_no_raceway_nothing_dropped(self):
        props = [["pole", 0.0, 0.0, 0.0, 0.0]]
        self.assertEqual(racedig.clear_props(props, [], [], None), 0)
        self.assertEqual(len(props), 1)


if __name__ == "__main__":
    unittest.main()
