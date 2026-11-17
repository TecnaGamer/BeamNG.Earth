"""clearance.py: the clearance log's reference trucks and wagons find what the geometry puts in their bodies (terrain beside the
road, a deck too low overhead, a cutting's slope against a wagon), leave their own road and deck alone, stand on both sides of
every carriageway, and log each place once."""
import logging
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clearance as cl  # noqa: E402
import geodesy  # noqa: E402

FRAME = geodesy.EnuFrame(35.40, 138.73)
EVERYWHERE = lambda x, y: np.ones(np.shape(x), bool)   # noqa: E731
FLAT = lambda x, y: np.zeros(np.shape(x))              # noqa: E731


def road(x0=-60.0, x1=60.0, y=0.0, z=0.0, width=7.0, oneway=False, level="ground", rail=False, fid="r1", cls="primary"):
    """A straight line along x (east) at y, 4 points."""
    xs = np.linspace(x0, x1, 4)
    return cl.Line(fid, cls, level, np.c_[xs, np.full(4, y), np.full(4, z)], width, oneway, rail)


def slab(x0, x1, y0, y1, z):
    """Two triangles of a flat surface at height z."""
    a, b, c, d = (x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)
    return np.array([[a, b, c], [a, c, d]], np.float64)


def box(x0, x1, y0, y1, z0, z1):
    """The 12 triangles of a closed box (a deck's slab)."""
    c = np.array([[x, y, z] for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)], np.float64)
    # corners: index = x + 2 y + 4 z
    quads = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    return np.array([t for a, b, cc, d in quads for t in ((c[a], c[b], c[cc]), (c[a], c[cc], c[d]))])


def find(lines, ground=FLAT, meshes=(), owns=EVERYWHERE):
    return cl.find(lines, owns, ground, list(meshes))


class TriBoxTest(unittest.TestCase):
    H = np.array([[6.0, 1.275, 1.75]])      # a truck's body: half length, half width, half of its 3.5 m over the floor

    def depth(self, tri):
        return float(cl.tri_box_depth(np.asarray(tri, np.float64)[None], self.H)[0])

    def test_separated_touching_and_through(self):
        self.assertEqual(self.depth([(-1, 5, 0), (1, 5, 0), (0, 6, 0)]), 0.0)          # beside it
        self.assertEqual(self.depth([(-1, -1, 1.75), (1, -1, 1.75), (0, 1, 1.75)]), 0.0)   # on its roof: touching
        self.assertAlmostEqual(self.depth([(-1, -1, 1.45), (1, -1, 1.45), (0, 1, 1.45)]), 0.30, places=6)   # 0.3 m under its roof
        self.assertGreater(self.depth([(-9, 0, -3), (9, 0, -3), (0, 0, 4)]), 0.0)      # a slanted face through the middle


class PlacementTest(unittest.TestCase):
    def fleet(self, *lines):
        return cl.place(list(lines), EVERYWHERE)

    def test_both_directions_of_a_two_way_road(self):
        f = self.fleet(road(width=7.0))
        offs = sorted(set(np.round(f["off"], 2)))
        self.assertEqual(offs, [-1.75, 1.75])
        self.assertTrue((f["env"] == 0).all())
        # every ~24 m, the first and the last half a body from the line's ends
        on = f["c"][f["off"] > 0]
        self.assertGreaterEqual(len(on), 5)
        self.assertAlmostEqual(on[:, 0].min(), -60.0 + 6.0, places=3)
        self.assertAlmostEqual(on[:, 0].max(), 60.0 - 6.0, places=3)
        self.assertLessEqual(np.diff(np.sort(on[:, 0])).max(), cl.TRUCK_EVERY_M + 1e-6)
        # the lanes are the road's halves: a truck's body inside its carriageway
        self.assertTrue((np.abs(f["c"][:, 1]) + cl.TRUCK.width / 2 <= 3.5 + 1e-9).all())

    def test_both_carriageways_of_a_dual_road(self):
        north = road(y=9.0, width=7.5, oneway=True, fid="n")
        south = road(y=-9.0, width=7.5, oneway=True, fid="s")
        f = self.fleet(north, south)
        lines = np.unique(f["line"])
        self.assertEqual(list(lines), [0, 1])
        for li in lines:                    # (a one-way carriageway wide enough for two lanes: a truck in each)
            np.testing.assert_allclose(sorted(set(f["off"][f["line"] == li])), [-1.875, 1.875])
        self.assertAlmostEqual(f["c"][f["line"] == 0][:, 1].mean(), 9.0, places=6)

    def test_narrow_road_has_one_truck_on_its_middle_and_paths_none(self):
        f = self.fleet(road(width=4.0))
        self.assertEqual(set(np.round(f["off"], 3)), {0.0})
        self.assertEqual(len(self.fleet(road(width=2.0))["c"]), 0)
        lines = cl.tile_lines([("p", SimpleNamespace(cls="footway", width=4.0, oneway=False), road().xyz, 0.0)], [], [], [], 0.58)
        self.assertEqual(lines, [])

    def test_wagons_on_tracks_and_none_on_trams(self):
        f = self.fleet(road(rail=True, width=0.0, cls="rail", z=1.0))
        self.assertTrue((f["env"] == 1).all())
        self.assertEqual(set(f["off"]), {0.0})
        tr = [("a", "rail", "ground", np.c_[np.linspace(0, 80, 5), np.zeros(5)], np.zeros(5)),
              ("b", "tram", "ground", np.c_[np.linspace(0, 80, 5), np.zeros(5)], np.zeros(5))]
        lines = cl.tile_lines([], [], [], tr, 0.58)
        self.assertEqual([ln.fid for ln in lines], ["a"])
        self.assertAlmostEqual(float(lines[0].xyz[0, 2]), 0.58)

    def test_only_the_tiles_own_places_and_the_limit(self):
        f = cl.place([road()], lambda x, y: x < 0.0)
        self.assertTrue((f["c"][:, 0] < 0.0).all() and len(f["c"]) > 0)
        f = cl.place([road(), road(y=20.0)], EVERYWHERE, limit=3)
        self.assertLessEqual(len(f["c"]), 3 + 2 * 5)        # (a line's vehicles go together: stops at the line that passes it)
        self.assertLess(len(f["c"]), len(cl.place([road(), road(y=20.0)], EVERYWHERE)["c"]))

    def test_a_tight_bend_gets_none_and_a_slope_tilts_the_body(self):
        a = np.linspace(0.0, np.pi / 2, 30)
        arc = cl.Line("c", "primary", "ground", np.c_[12 * np.cos(a), 12 * np.sin(a), np.zeros(30)], 7.0, False, False)
        self.assertEqual(len(cl.place([arc], EVERYWHERE)["c"]), 0)
        xs = np.linspace(-60, 60, 4)
        ramp = cl.Line("r", "primary", "bridge", np.c_[xs, np.zeros(4), 0.1 * xs], 7.0, False, False)
        f = cl.place([ramp], EVERYWHERE)
        self.assertAlmostEqual(float(f["a"][0, 2]), 0.1 / np.hypot(1, 0.1), places=6)
        self.assertTrue(np.allclose((f["a"] * f["up"]).sum(1), 0.0))


class GapTest(unittest.TestCase):
    """A road whose ground parts stop short of each other with no deck between them (a bridge never built)."""
    SPEC = SimpleNamespace(cls="secondary", width=8.0, oneway=False)

    def parts(self, gap=20.0):
        a = np.c_[np.linspace(-100.0, -10.0, 8), np.zeros(8), np.full(8, 50.0)]
        b = np.c_[np.linspace(-10.0 + gap, 80.0 + gap, 8), np.zeros(8), np.full(8, 50.0)]
        return [("road-1", self.SPEC, a, 0.0), ("road-1", self.SPEC, b, 0.0)]

    def test_the_stretch_between_two_ground_parts_is_a_line_of_its_own(self):
        gaps = [ln for ln in cl.tile_lines(self.parts(), [], [], [], 0.58) if ln.gap]
        self.assertEqual(len(gaps), 1)
        ln = gaps[0]
        self.assertEqual((ln.fid, ln.level, ln.cls), ("road-1", "bridge", "secondary"))
        np.testing.assert_allclose(ln.xyz[:, 0], [-10.0, 10.0])
        self.assertEqual(len(cl.place([ln], EVERYWHERE)["c"]), 2 * 2)        # (two stations, a truck each way)

    def test_a_built_deck_or_tunnel_fills_it_and_a_parting_of_ways_is_not_one(self):
        deck = [("road-1", self.SPEC, np.c_[np.linspace(-12.0, 12.0, 4), np.zeros(4)], np.full(4, 50.0), 0.0)]
        self.assertEqual([ln for ln in cl.tile_lines(self.parts(), deck, [], [], 0.58) if ln.gap], [])
        tun = [("road-1", self.SPEC, np.c_[np.linspace(-12.0, 12.0, 4), np.zeros(4)], np.full(4, 50.0), 0.0)]
        self.assertEqual([ln for ln in cl.tile_lines(self.parts(), [], tun, [], 0.58) if ln.gap], [])
        self.assertEqual([ln for ln in cl.tile_lines(self.parts(gap=1.0), [], [], [], 0.58) if ln.gap], [], "joined parts")
        self.assertEqual([ln for ln in cl.tile_lines(self.parts(gap=300.0), [], [], [], 0.58) if ln.gap], [], "far apart")
        hairpin = [("r", self.SPEC, np.c_[np.linspace(0, 50, 5), np.zeros(5), np.zeros(5)], 0.0),
                   ("r", self.SPEC, np.c_[np.linspace(50, 0, 5), np.full(5, 30.0), np.zeros(5)], 0.0)]
        self.assertEqual([ln for ln in cl.tile_lines(hairpin, [], [], [], 0.58) if ln.gap], [], "a hairpin's two legs")

    def test_over_a_gorge_nothing_is_under_the_wheels_over_level_ground_it_is_fine(self):
        gap = [ln for ln in cl.tile_lines(self.parts(), [], [], [], 0.58) if ln.gap]
        gorge = lambda x, y: np.where(np.abs(x) < 15.0, 30.0, 50.0)         # noqa: E731
        hits = find(gap, ground=gorge)
        self.assertTrue(hits)
        self.assertTrue(all(h.what == "gap" and abs(h.depth - 20.0) < 1e-9 for h in hits))
        self.assertEqual(find(gap, ground=lambda x, y: np.full(np.shape(x), 49.8)), [])
        ridge = lambda x, y: np.where(np.abs(x) < 15.0, 53.0, 50.0)         # noqa: E731  (a ridge across the stretch)
        self.assertTrue(any(h.what == "terrain" for h in find(gap, ground=ridge)))

    def test_the_log_line_says_what_is_missing(self):
        ln = [ln for ln in cl.tile_lines(self.parts(), [], [], [], 0.58) if ln.gap][0]
        h = cl.Hit("truck", 0, 2.0, 0.0, 0.0, 50.0, 90.0, "gap", 20.0, (0.0, 0.0, 30.0))
        text = cl.Reporter.line(h, [ln], FRAME, "0_0", 3, 35.4, 138.73)
        self.assertIn("(secondary, bridge)", text)
        self.assertIn("has no deck or tunnel under it (an unbuilt bridge), terrain below by 20.00 m (at x 0.0 y 0.0 z 30.0)", text)


class TerrainTest(unittest.TestCase):
    def test_a_mound_beside_the_truck_is_a_violation(self):
        mound = lambda x, y: np.where((y > 2.2) & (np.abs(x) < 20.0), 1.4, 0.0)       # noqa: E731  (beside the left lane's truck)
        hits = find([road(width=7.0)], ground=mound)
        self.assertTrue(hits)
        self.assertTrue(all(h.what == "terrain" and h.env == "truck" for h in hits))
        self.assertTrue(all(h.off > 0 for h in hits), "only the left lane's trucks have it in their body")
        self.assertAlmostEqual(max(h.depth for h in hits), 0.9, places=6)           # 1.4 m terrain over a floor 0.5 m up
        self.assertTrue(all(h.at[2] == 1.4 and h.at[1] > 2.2 for h in hits))

    def test_terrain_under_the_body_floor_is_not(self):
        self.assertEqual(find([road()], ground=lambda x, y: np.where(y > 2.2, 0.45, 0.0)), [])      # 0.05 m under the body
        self.assertEqual(find([road()]), [])

    def test_unbuilt_bridge_gap_is_a_violation_a_valley_under_the_deck_is_not(self):
        deck = road(level="bridge", z=20.0)
        valley = lambda x, y: np.where(np.abs(x) < 30.0, 0.0, 20.0)         # noqa: E731  (a gorge under a bridge)
        self.assertEqual(find([deck], ground=valley), [])
        ridge = lambda x, y: np.where(np.abs(x) < 30.0, 24.0, 20.0)         # noqa: E731  (the gap filled: never built)
        hits = find([deck], ground=ridge)
        self.assertTrue(hits)
        self.assertTrue(all(h.what == "terrain" and h.depth > 3.0 for h in hits))      # the body's whole 3.5 m

    def test_no_terrain_test_inside_a_tunnel(self):
        self.assertEqual(find([road(level="tunnel", z=5.0)], ground=lambda x, y: np.full(np.shape(x), 60.0)), [])

    def test_wagon_against_a_cutting(self):
        cut = lambda x, y: np.where(np.abs(y) > 1.4, 3.0, 0.0)              # noqa: E731  (a 3 m wall beside the track)
        track = road(rail=True, width=0.0, cls="rail", z=0.58, fid="t1")
        hits = find([track], ground=cut)
        self.assertTrue(hits)
        self.assertTrue(all(h.env == "wagon" and h.what == "terrain" for h in hits))
        self.assertGreater(max(h.depth for h in hits), 1.9)                 # 3 m up to the roof, the body's floor at 1.08 m
        sloped = lambda x, y: np.clip((np.abs(y) - 4.0) * 0.5, 0.0, None)    # noqa: E731  (1:2 from 4 m out: clear of the wagon)
        self.assertEqual(find([track], ground=sloped), [])

    def test_a_ground_road_is_painted_into_the_terrain_its_trucks_stand_on_it(self):
        slope = lambda x, y: 0.05 * x + 0.3 * np.sin(x / 7.0)               # noqa: E731  (the road's own z line says 0: not used)
        fleet = cl.place([road(z=0.0)], EVERYWHERE, slope)
        c = fleet["c"]
        np.testing.assert_allclose(c[:, 2], slope(c[:, 0], c[:, 1]) + 0.0, atol=0.3)
        self.assertTrue(np.abs(c[:, 2]).max() > 1.0, "on the terrain, not on the line's z of 0")
        self.assertEqual(find([road(z=0.0)], ground=slope), [], "a road following a rolling terrain has no violation")
        self.assertEqual(find([road(z=9.0, level="bridge")], ground=lambda x, y: np.zeros(np.shape(x))), [],
                         "a deck is meant to stand over the ground")


class MeshTest(unittest.TestCase):
    def test_low_deck_over_a_road_is_a_violation(self):
        girders = box(-8.0, 8.0, -12.0, 12.0, 3.4, 5.0)                     # a deck's slab with its underside at 3.4 m over the road
        hits = find([road(y=0.0, width=7.0)], meshes=[("deck", girders)])
        self.assertTrue(hits)
        self.assertTrue(all(h.what == "deck" for h in hits))
        self.assertAlmostEqual(max(h.depth for h in hits), 0.6, places=6)    # the truck is 4.0 m high
        self.assertTrue(all(abs(h.x) < 14.0 for h in hits), "only where the deck is over the road")
        self.assertTrue(all(3.4 <= h.at[2] <= 5.0 for h in hits))

    def test_a_deck_high_enough_is_not(self):
        self.assertEqual(find([road()], meshes=[("deck", box(-8.0, 8.0, -12.0, 12.0, 4.01, 5.0))]), [])

    def test_a_tunnel_vault_and_a_railway_are_named(self):
        low = box(-8.0, 8.0, -12.0, 12.0, 3.0, 3.3)
        hits = find([road()], meshes=[("deck", np.zeros((0, 3, 3))), ("tunnel", low), ("rail", low)])
        self.assertEqual({h.what for h in hits}, {"tunnel", "rail"})

    def test_a_truck_on_its_own_deck_is_not(self):
        z = 20.0
        deck = road(level="bridge", z=z)
        own = box(-60.0, 60.0, -3.95, 3.95, z - 1.2, z + 0.02)              # its slab, its top at the road's surface
        parapets = [box(-60.0, 60.0, 3.95, 4.4, z, z + 0.84), box(-60.0, 60.0, -4.4, -3.95, z, z + 0.84)]
        piers = box(-1.0, 1.0, -1.0, 1.0, 0.0, z - 1.2)
        meshes = [("deck", np.concatenate([own] + parapets + [piers]))]
        self.assertEqual(find([deck], ground=lambda x, y: np.zeros(np.shape(x)), meshes=meshes), [])

    def test_a_truck_on_its_own_road_is_not(self):
        self.assertEqual(find([road()], meshes=[("deck", slab(-60, 60, -4, 4, 0.0))]), [])         # a road surface under the wheels
        self.assertEqual(find([road()], meshes=[("deck", slab(-60, 60, -4, 4, 0.45))]), [])        # within the body's 0.5 m base
        self.assertEqual(find([road(level="tunnel", z=5.0)],
                              meshes=[("tunnel", box(-60, 60, -4.2, -3.8, 5.0, 10.0))]), [], "a wall outside its lane")

    def test_a_crossing_deck_far_above_and_neighbours_are_not(self):
        self.assertEqual(find([road()], meshes=[("deck", box(-60, 60, 20, 30, 0, 3))]), [])


class LogTest(unittest.TestCase):
    def hit(self, x=10.0, y=0.0, what="terrain", env="truck", depth=0.7):
        return cl.Hit(env, 0, 1.75, x, y, 2249.7, 87.0, what, depth, (x, y + 2.5, 2250.4))

    def reporter(self, **kw):
        return cl.Reporter(**kw)

    def inside_a_cell(self):
        """(x, y) in the frame whose 2 x 2 m square is inside one dedup square (the squares' edges lie wherever lat / lon put them)."""
        for k in range(400):
            x, y = 0.5 * (k % 20), 0.5 * (k // 20)
            pts = [(x + dx, y + dy) for dx in (0.0, 2.0) for dy in (0.0, 2.0)]
            la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]), 0.0)
            i, j = cl.Reporter().cells(la, lo)
            if len(set(zip(i.tolist(), j.tolist()))) == 1:
                return x, y
        raise AssertionError("no cell found")

    def test_line_has_everything_to_rebuild_the_spot(self):
        ln = cl.Line("abc-123", "primary", "bridge", road().xyz, 7.0, False, False)
        rep = self.reporter()
        with self.assertLogs("earthd.clearance", logging.INFO) as cm:
            n = rep.emit([self.hit(-599.5, -803.2)], [ln], FRAME, "g17_113_52", 3)
        self.assertEqual(n, 1)
        self.assertEqual(len(cm.output), 1)
        text = cm.records[0].getMessage()
        self.assertEqual(cm.records[0].levelno, logging.INFO)
        self.assertNotIn("\n", text)
        la, lo, _ = FRAME.enu_to_geodetic(-599.5, -803.2, 2249.7)
        for part in ("clearance: truck on road abc-123 (primary, bridge) at x -599.5 y -803.2 z 2249.7",
                     "lat %.6f lon %.6f" % (la, lo), "lane +1.8", "heading 87", "tile g17_113_52", "frame 35.400000 138.730000",
                     "anchor 3 hits terrain by 0.70 m (at x -599.5 y -800.7 z 2250.4)"):
            self.assertIn(part, text)

    def test_one_line_per_place_and_kind_and_session(self):
        ln = cl.Line("r", "primary", "ground", road().xyz, 7.0, False, False)
        rep = self.reporter()
        x, y = self.inside_a_cell()
        with self.assertLogs("earthd.clearance", logging.INFO) as cm:
            self.assertEqual(rep.emit([self.hit(x, y), self.hit(x + 2.0, y + 2.0, depth=0.2)], [ln], FRAME, "t", 1), 1)   # (one 10 m cell)
            self.assertEqual(rep.emit([self.hit(x, y)], [ln], FRAME, "t", 1), 0)           # (again, this session)
            self.assertEqual(rep.emit([self.hit(x + 1.0, y + 1.0, depth=0.9)], [ln], FRAME, "other", None), 0)   # (another tile, the same place)
            self.assertEqual(rep.emit([self.hit(x, y, what="deck")], [ln], FRAME, "t", 1), 1)       # (another kind of violation)
            self.assertEqual(rep.emit([self.hit(x, y, env="wagon")], [ln], FRAME, "t", 1), 1)       # (another vehicle)
            self.assertEqual(rep.emit([self.hit(x + 25.0, y)], [ln], FRAME, "t", 1), 1)                 # (another place)
        self.assertEqual(len(cm.output), 4)
        self.assertIn("by 0.70 m", cm.records[0].getMessage())      # (the deepest of a cell's hits is the one named)

    def test_the_deepest_first_and_a_rate_limit(self):
        ln = cl.Line("r", "primary", "ground", road().xyz, 7.0, False, False)
        rep = self.reporter(tile_max=3, minute_max=4)
        hits = [self.hit(30.0 * i, 0.0, depth=0.1 * (i + 1)) for i in range(6)]
        with self.assertLogs("earthd.clearance", logging.INFO) as cm:
            self.assertEqual(rep.emit(hits, [ln], FRAME, "t", 1), 3)
        msgs = [r.getMessage() for r in cm.records]
        self.assertEqual(len(msgs), 4)
        self.assertIn("by 0.60 m", msgs[0])
        self.assertIn("3 more violation(s) not logged", msgs[-1])
        with self.assertLogs("earthd.clearance", logging.INFO) as cm:      # (the minute's limit: 4 in all)
            self.assertEqual(rep.emit([self.hit(900.0 + 30 * i, 0.0) for i in range(5)], [ln], FRAME, "t2", 1), 1)

    def test_run_logs_a_mound_once_however_often_the_tile_is_built(self):
        mound = lambda x, y: np.where((y > 2.2) & (np.abs(x) < 20.0), 1.4, 0.0)       # noqa: E731
        lines = [road(width=7.0)]
        rep = self.reporter()
        build = lambda: (lines, EVERYWHERE, mound, [])                              # noqa: E731
        with self.assertLogs("earthd.clearance", logging.INFO) as cm:
            first = cl.run(build, FRAME, "0_0", 2, reporter=rep)
            cl.run(build, FRAME, "0_0", 2, reporter=rep)
        self.assertTrue(first)
        self.assertTrue(len(cm.output) >= 1 and all("hits terrain" in m for m in cm.output))
        n = len(cm.output)
        with self.assertLogs("earthd.clearance", logging.INFO) as cm:
            cl.run(build, FRAME, "0_0", 2, reporter=self.reporter())              # (a fresh session logs it again)
        self.assertEqual(len(cm.output), n)

    def test_off_switch(self):
        build = mock.Mock(side_effect=AssertionError("built though off"))
        for v in ("off", "OFF", "0", "false"):
            with mock.patch.dict(os.environ, {"BNE_CLEARANCE_LOG": v}):
                self.assertFalse(cl.enabled())
                self.assertEqual(cl.run(build, FRAME, "t", 1, reporter=self.reporter()), [])
        build.assert_not_called()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BNE_CLEARANCE_LOG", None)
            self.assertTrue(cl.enabled())

    def test_a_fault_is_logged_once_and_never_raised(self):
        rep = self.reporter()
        boom = mock.Mock(side_effect=ValueError("bad plan"))
        with self.assertLogs("earthd.clearance", logging.WARNING) as cm:
            self.assertEqual(cl.run(boom, FRAME, "t", 1, reporter=rep), [])
            self.assertEqual(cl.run(boom, FRAME, "t", 1, reporter=rep), [])
        self.assertEqual(len(cm.output), 1)
        self.assertIn("ValueError: bad plan", cm.output[0])

    def test_threads_share_one_reporter(self):
        ln = cl.Line("r", "primary", "ground", road().xyz, 7.0, False, False)
        rep = self.reporter()
        counts = []
        with self.assertLogs("earthd.clearance", logging.INFO):
            ts = [threading.Thread(target=lambda: counts.append(rep.emit([self.hit(5.0, 5.0)], [ln], FRAME, "t", 1)))
                  for _ in range(8)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(sum(counts), 1)


class TileGlueTest(unittest.TestCase):
    """blockworker's road tile builds the check's inputs from its own plans: the roads of the tile, their decks and its
    tunnels, with the meshes it wrote."""

    def test_a_road_tile_is_checked_with_its_roads_on_both_sides(self):
        import blockworker
        import test_blockworker as tb
        import roads
        f = tb.features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        keys = roads.cells_of_tile(tb.FRAME, 0, 0)
        got = {}

        def spy(build, frame, tile, aid=None, reporter=None):
            got["lines"], got["owns"], got["ground"], got["meshes"] = build()
            got["tile"], got["aid"] = tile, aid
            return []
        blockworker._s = SimpleNamespace(md=tb.FakeMapData(f, keys[0]), dem=tb.FakeDem(), widths=None, surface=None, airports=None,
                                         sea=None, lock=threading.Lock(), cache_dir=tb.tmp_cache(self), bridges={},
                                         anchor=lambda aid, lat, lon: (None, None))
        import tempfile
        with mock.patch.object(cl, "run", spy), tempfile.TemporaryDirectory() as a:
            blockworker.roads_tile(7, tb.LAT, tb.LON, 15, 0, 0, a, "/temp/x", True, 0.05)
        self.assertEqual((got["tile"], got["aid"]), ("0_0", 7))
        lines = got["lines"]
        self.assertEqual({ln.cls for ln in lines}, {"secondary", "residential"})
        fleet = cl.place(lines, got["owns"])
        self.assertTrue(len(fleet["c"]) > 0)
        self.assertTrue({-1, 1} <= set(np.sign(fleet["off"][fleet["line"] == [i for i, ln in enumerate(lines) if ln.cls == "secondary"][0]])),
                        "the two-way secondary has a truck each way")
        z = got["ground"](np.array([0.0, 50.0]), np.array([0.0, 50.0]))
        self.assertTrue(np.isfinite(z).all())
        self.assertEqual(cl.find(lines, got["owns"], got["ground"], got["meshes"]), [], "flat ground: nothing to log")


if __name__ == "__main__":
    unittest.main()
