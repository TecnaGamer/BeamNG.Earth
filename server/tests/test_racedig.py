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
        # (its grade breaks rounded: at most DIG_ROUND_UP_M of the slack taken, the vault and its soil always under)
        self.assertTrue((floor[mid] <= self.up(want) + racedig.DIG_ROUND_UP_M + 1e-6).all())
        self.assertLess(racedig.DIG_ROUND_UP_M, racedig.DIG_SLACK_M)
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


def _banked(la, lo):
    """An oval's straight along x: a flat apron (y -9..-3), its banking 0.25 m per m up to the outside wall's foot at
    y = 9 (3 m up), the ground behind falling back 3 m over 6 m: a pit road from the infield (y < -9, flat) ending on
    the oval's centre line, its surface smoothed along it over its straight run-on past its end, reads the drop."""
    from test_roads import LAT, M_LAT
    y = (np.asarray(la, np.float64) - LAT) * M_LAT
    bank = np.clip(y + 3.0, 0.0, 12.0) * 0.25
    return 10.0 + bank - np.clip((y - 9.0) * 0.5, 0.0, 3.0)


class JoinTest(unittest.TestCase):
    """The user's Atlanta reports 65 / 70 (2026-11-17): bumps across the racing surface where an infield road
    (highway=raceway, the oval's default width) joins it. Each raceway's own smoothed surface, the nearest centre line's
    taken: the pit road's surface cut into the oval's banking half way between the two lines."""

    def zones(self):
        import grading
        from test_roads import FRAME
        oval = np.array([[-200.0, 0.0], [200.0, 0.0]])
        pit = np.array([[0.0, -80.0], [0.0, 0.0]])
        hw = 7.0 + grading.UNGRADED_MARGIN_M
        zs = [grading.raceway_surface(FRAME, _banked, oval, hw), grading.raceway_surface(FRAME, _banked, pit, hw)]
        return grading, zs, [(oval, hw), (pit, hw)]

    def test_the_pit_road_gives_way_to_the_oval(self):
        grading, _zs, lines = self.zones()
        self.assertEqual(grading.raceway_dominance(lines), [[], [(0, 0.0, 0.0)]])

    def test_the_oval_kept_and_the_pit_road_meets_it(self):
        grading, zs, lines = self.zones()
        y = np.arange(-60.0, 7.01, 0.5)
        e = np.zeros_like(y)
        base = np.full_like(y, 5.0)
        old = grading.raceway_ground(e, y, base, zs)
        new, cov = grading.raceway_ground(e, y, base, zs, joins=grading.raceway_dominance(lines),
                                          blend=racedig.JOIN_BLEND_M)
        alone = grading.raceway_ground(e, y, base, zs[:1])
        on = y >= -7.0                      # (the oval's carriageway)
        self.assertTrue(cov.all())
        # before: the pit road's surface on the oval's carriageway from half way (a step of the banking's)
        self.assertGreater(float(np.max(np.abs(old[on] - alone[on]))), 0.3)
        # now: the oval's own surface there, bit for bit
        self.assertEqual(new[on].tobytes(), alone[on].tobytes())
        # and the pit road meeting it without a kink: off the oval's own apron / banking crease (y = -3), no grade
        # change over 0.01 m per 0.5 m sample
        sel = y[1:-1] <= -6.0
        self.assertLess(float(np.max(np.abs(np.diff(new, 2))[sel])), 0.01)

    def test_no_dominance_is_the_nearest_rule(self):
        grading, zs, _lines = self.zones()
        rng = np.random.default_rng(3)
        e, n = rng.uniform(-30, 30, 500), rng.uniform(-30, 20, 500)
        base = rng.normal(8.0, 1.0, 500)
        old = grading.raceway_ground(e, n, base, zs)
        new, _cov = grading.raceway_ground(e, n, base, zs, joins=[[], []])
        self.assertEqual(new.tobytes(), old.tobytes())

    def test_joins_whatever_else_is_loaded(self):
        # Codex reviews of 98a65723 / 80e9b3da: the ownership and the zone set came from the planner's loaded lines
        # (every planned cell's keeps): cold and warmed planners, cells read in another order, graded a join
        # differently. Now each join from its two lines, held along its own overlap, the zones from the map cells
        # round the points and round their lines' ends, whatever was planned or read before
        oval = np.array([[-200.0, 0.0], [200.0, 0.0]])
        pit = np.array([[0.0, -80.0], [0.0, 0.0]])
        far_end = np.array([[-60.0, -80.0], [0.0, -80.0]])          # (a third raceway ending on the pit road's far end)
        g = np.arange(-30.0, 30.01, 0.5)
        e, n = (v.ravel() for v in np.meshgrid(g, g - 45.0))      # (points 15-75 m from the oval: some only by the
        base = np.full(len(e), 3.0)                                 # join's reach, beyond a portal's 30 m)
        md = _FakeMD([oval, pit, far_end])
        runs = []
        for keeps, warm in (({}, False), ({"z": [(np.array([[500.0, 500.0], [700.0, 500.0]]), 9.0)]}, True),
                            ({"a": [(oval, 9.0)]}, False)):
            pl = _FakePlanner(keeps, md=md)
            pl.elev = _banked
            if warm:            # (another query first: other cells read, memos filled)
                pl._raceway_band(e + 400.0, n, base)
            idx, z = pl._raceway_band(e, n, base)
            runs.append((idx.tobytes(), z.tobytes()))
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[0], runs[2])
        self.assertGreater(len(np.frombuffer(runs[0][0], np.int64)), 0)

    def test_two_lines_with_the_same_ends_are_two_lines(self):
        # Codex review of b68ade87: lines were told apart by their rounded ends, vertex count and width: two branches
        # that part and meet again with as many vertices shared one surface, whichever was read first
        a = np.array([[-60.0, 0.0], [0.0, 20.0], [60.0, 0.0]])
        b = np.array([[-60.0, 0.0], [0.0, -20.0], [60.0, 0.0]])
        g = np.arange(-20.0, 20.01, 1.0)
        e, n = (v.ravel() for v in np.meshgrid(g, g))
        base = np.full(len(e), 3.0)
        runs = []
        for order in ((a, b), (b, a)):
            pl = _FakePlanner({}, md=_FakeMD(list(order)))
            pl.elev = _rolling
            la, lo, _ = pl.frame.enu_to_geodetic(e, n, 0.0)
            zones, kept = pl._raceway_zones(np.asarray(la), np.asarray(lo))
            self.assertEqual(len(zones), 2)
            idx, z = pl._raceway_band(e, n, base)
            runs.append((idx.tobytes(), z.tobytes()))
        self.assertEqual(runs[0], runs[1])

    def test_the_band_has_no_holes_at_vertices(self):
        # report 69: points on the outside of a vertex lie beyond both of its segments' ends: the segment test left
        # them out (single samples left to other roads' grading, 2.3 m spikes along Atlanta's turn 3-4); and the band
        # must be the line the surface is sampled on (Codex review of 98a65723: a 9 m band on a 90 degree corner, a
        # point 8.91 m from the map's vertex is 9.62 m from the resampled line: in by one, out by the other)
        import grading
        for bend, corner in ((np.array([[-50.0, 0.0], [0.0, 0.0], [50.0, -10.0]]), (0.25, 6.5)),     # 11 degrees
                             (np.array([[-30.0, 0.0], [0.0, 0.0], [0.0, -30.0]]), (4.5, 4.5))):     # 90 degrees
            pl = _FakePlanner({"k": [(bend, 9.0)]})
            g = np.arange(-12.0, 12.01, 0.25)
            e, n = (v.ravel() for v in np.meshgrid(g, g))
            base = np.full(len(e), 3.0)
            idx, z = pl._raceway_band(e, n, base)
            la, lo, _ = pl.frame.enu_to_geodetic(e, n, 0.0)
            zones, _kept = pl._raceway_zones(np.asarray(la), np.asarray(lo))
            line, _st, offs, _c = zones[0]
            # every point within the band of the sampled line is taken, with the surface's height
            a, b = line[:-1], line[1:]
            d = b - a
            pts = np.stack([e, n], 1)
            t = np.clip(((pts[:, None] - a) * d).sum(2) / (d * d).sum(1), 0.0, 1.0)
            dist = np.hypot(*(a + t[..., None] * d - pts[:, None]).transpose(2, 0, 1)).min(1)
            want = np.flatnonzero(dist < float(offs[-1]) - 1e-6)
            self.assertTrue(np.isin(want, idx).all(), np.setdiff1d(want, idx)[:5])
            got, cov = grading.raceway_ground(e[idx], n[idx], base[idx], zones, joins=[[]],
                                              blend=racedig.JOIN_BLEND_M)
            self.assertTrue(cov.all())
            self.assertEqual(z.tobytes(), got.tobytes())
            # a point outside the corner, inside the carriageway (6.4-6.5 m out), at the track's height (10 m, not 3)
            k = int(np.argmin(np.hypot(e - corner[0], n - corner[1])))
            self.assertIn(k, set(idx.tolist()))
            self.assertGreater(float(z[list(idx).index(k)]), 9.0)


class _FakeMD:
    """A map whose every cell holds the given raceway lines (test_roads.features) and counts its reads."""

    def __init__(self, lines, widths=None):
        import types
        from test_roads import features
        ids = [f"osm-raceway-{i:012d}" for i in range(len(lines))]
        self.f = features([[tuple(p) for p in ln] for ln in lines], ["raceway"] * len(lines),
                          widths=[(i, w) for i, w in enumerate(widths or [])], ids=ids)
        self.reads = 0
        self._cell = types.SimpleNamespace(layers={"roads": self.f})

    def cell(self, key, layers=None, kind=None):
        self.reads += 1
        return self._cell


class _FakePlanner:
    """grading._Planner's raceway pieces on a namespace: a map (_FakeMD) or keeps, the frame, an elevation, no marks."""

    def __init__(self, keeps, md=None):
        import threading
        import grading
        from test_roads import FRAME
        self.lock = threading.Lock()
        self.keeps = keeps
        self.frame = FRAME
        self.md = md if md is not None else _FakeMD([np.asarray(xy) for v in keeps.values() for xy, _hw in v])
        self.widths = None
        self.elev = lambda la, lo: np.full(np.shape(la), 10.0)
        self._P = grading._Planner

    def _marks_at(self, *a):
        pass

    def _raceway_cell(self, key):
        return self._P._raceway_cell(self, key)

    def _raceway_cell_read(self, key):
        return self._P._raceway_cell_read(self, key)

    def _raceway_lines(self, lat, lon):
        return self._P._raceway_lines(self, lat, lon)

    def _raceway_zones(self, lat=None, lon=None):
        return self._P._raceway_zones(self, lat, lon)

    def _raceway_joins(self, kept):
        return self._P._raceway_joins(self, kept)

    def _raceway_band(self, e, n, base, lat=None, lon=None):
        return self._P._raceway_band(self, e, n, base, lat, lon)


def _rolling(la, lo):
    """A slope across y with 2 m rolls along x near the oval: surfaces smoothed along other lines differ there."""
    from test_roads import LAT, LON, M_LAT, M_LON
    x = (np.asarray(lo, np.float64) - LON) * M_LON
    y = (np.asarray(la, np.float64) - LAT) * M_LAT
    return 10.0 + 0.15 * y + 2.0 * np.sin(x / 12.0) * np.exp(-((y - 6.0) / 20.0) ** 2)


class WindowJoinTest(unittest.TestCase):
    def test_a_second_overlap_past_a_gap_does_not_give_way_in_a_window(self):
        # Codex review of 80e9b3da: the tunnel solve reads the track over windows of the lines; a join's reach taken
        # from the window's end nearest the join made a second stretch along the oval, past a 60 m gap, give way
        # there while the terrain (the whole line) did not. The reach measured on the whole line
        import grading
        from test_roads import FRAME
        oval = np.array([[-500.0, 0.0], [300.0, 0.0]])
        road = np.array([[-450.0, 25.0], [-150.0, 0.0], [-100.0, 60.0], [0.0, 60.0], [0.0, 0.0]])
        races = [(oval, 7.0), (road, 7.0)]
        xs = np.arange(-400.0, -200.0, 5.0)
        yy = 25.0 - 25.0 * (xs + 450.0) / 300.0
        pts = np.stack([np.repeat(xs, 3), np.concatenate([[v - 6.0, v - 7.5, v - 8.5] for v in yy])], 1)
        la, lo, _ = FRAME.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
        raw = _rolling(np.asarray(la), np.asarray(lo))
        got = racedig.track_surface(FRAME, _rolling, races, pts, raw)
        hw = 7.0 + racedig.BAND_MARGIN_M
        zs = [grading.raceway_surface(FRAME, _rolling, oval, hw), grading.raceway_surface(FRAME, _rolling, road, hw)]
        joins = grading.raceway_dominance([(oval, hw), (road, hw)])
        self.assertEqual([j[0] for j in joins[1]], [0])
        whole, _c = grading.raceway_ground(pts[:, 0], pts[:, 1], raw, zs, margin=1e-6, joins=joins,
                                           blend=racedig.JOIN_BLEND_M)
        given, _c = grading.raceway_ground(pts[:, 0], pts[:, 1], raw, zs, margin=1e-6,
                                           joins=[[], [(0, 0.0, 0.0, 0.0, 1e9)]], blend=racedig.JOIN_BLEND_M)
        self.assertLess(float(np.max(np.abs(got - whole))), 0.02)
        self.assertGreater(float(np.max(np.abs(given - whole))), 0.5, "(the test tells the two apart)")


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
