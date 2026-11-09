"""Racing street furniture (racefurniture.py): synthetic tracks, then the cached OSM cells of three real ones."""
import collections
import logging
import math
import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import guardrails
import racefurniture as rf


def track(race=(), access=(), roads=(), barriers=(), stands=(), race_w=10.0, access_w=4.0):
    tr = rf.Track()
    for i, ln in enumerate(race):
        tr.race.append(np.asarray(ln, float)); tr.race_half.append(race_w / 2); tr.race_ids.append(f"r{i}")
    for i, ln in enumerate(access):
        tr.access.append(np.asarray(ln, float)); tr.access_half.append(access_w / 2); tr.access_ids.append(f"a{i}")
    for ln in roads:
        tr.roads.append(np.asarray(ln, float)); tr.roads_half.append(3.5)
    for i, (t, ln) in enumerate(barriers):
        tr.barriers.append((f"b{i}", t, None, np.asarray(ln, float)))
    for i, ring in enumerate(stands):
        tr.stands.append((f"s{i}", np.asarray(ring, float)))
    return tr.finish()


def arc(cx, cy, r, a0, a1, n=60):
    t = np.linspace(a0, a1, n)
    return np.column_stack((cx + r * np.cos(t), cy + r * np.sin(t)))


STRAIGHT = [[0, 0], [400, 0]]


class SyntheticTest(unittest.TestCase):
    def assertClear(self, tr, rows):
        xy = np.array([[r[1], r[2]] for r in rows]).reshape(-1, 2)
        if len(xy):
            self.assertGreaterEqual(float(tr.race_segs.edge(xy).min()), rf.CLEAR_M)

    def test_catch_fence_inferred_along_parallel_service_road(self):
        tr = track(race=[STRAIGHT], access=[[[0, 25], [400, 25]]])     # edge gap 25 - 5 - 2 = 18 m
        rows = rf.plan(tr)
        fence = [r for r in rows if r[0] == rf.G_CATCH]
        self.assertGreater(len(fence), 80)
        ys = np.array([r[2] for r in fence])
        self.assertTrue(np.allclose(ys, 25 - 2 - rf.FENCE_OFF_M, atol=0.01))   # on the raceway side of the road
        self.assertClear(tr, rows)

    def test_no_inferred_fence_far_away_short_or_across(self):
        for access in ([[0, 80], [400, 80]],            # too far
                       [[0, 25], [30, 25]],             # too short
                       [[200, 10], [200, 200]]):        # across, not alongside
            self.assertFalse([r for r in rf.plan(track(race=[STRAIGHT], access=[access])) if r[0] == rf.G_CATCH],
                             access)

    def test_mapped_barrier_suppresses_inferred_fence_and_is_used(self):
        tr = track(race=[STRAIGHT], access=[[[0, 25], [400, 25]]], barriers=[("fence", [[0, 20], [400, 20]])])
        fence = [r for r in rf.plan(tr) if r[0] == rf.G_CATCH]
        self.assertTrue(fence)
        self.assertTrue(all(r[5] == "b0" for r in fence))
        far = track(race=[STRAIGHT], barriers=[("fence", [[0, 120], [400, 120]])])   # a field's fence
        self.assertFalse(rf.plan(far))

    def test_mapped_wall_and_tyres(self):
        tr = track(race=[STRAIGHT], barriers=[("wall", [[0, 12], [200, 12]]), ("tyres", [[250, -15], [270, -15]])])
        c = collections.Counter(r[0] for r in rf.plan(tr))
        self.assertEqual(c[rf.G_WALL], 0)               # (the wall a jersey barrier run, not noise-wall blocks)
        self.assertGreater(c[rf.G_TYRE], 20)
        runs = rf.wall_runs(tr)
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(float(np.ptp(runs[0][1][:, 0])), 200.0, delta=1.0)

    def test_jersey_modules_continuous_on_a_grade(self):
        line = np.c_[np.linspace(0, 200, 101), np.full(101, 12.0)]
        z = 0.08 * line[:, 0] + 0.3 * np.sin(line[:, 0] / 15.0)      # an 8 % climb with crests and sags
        mods = rf.jersey_modules(line, z)
        cs = np.array([m[0] for m in mods])
        f = np.array([m[2] for m in mods])
        # (along the run: each module's end at or past the next one's start, the first at 0, the last at 200)
        s = cs[:, 0]
        half = 0.5 * rf.JERSEY_LEN_M * np.hypot(f[:, 0], f[:, 1])
        self.assertAlmostEqual(s[0] - half[0], 0.0, delta=0.05)
        self.assertAlmostEqual(s[-1] + half[-1], 200.0, delta=0.05)
        self.assertTrue(np.all(s[1:] - half[1:] <= s[:-1] + half[:-1] + 1e-6))
        # (tilted to the grade, its middle never over the ground)
        self.assertTrue(np.all(f[:, 2] > 0.0))
        zg = np.interp(s, line[:, 0], z)
        self.assertTrue(np.all(np.array([m[1] for m in mods]) <= zg + 1e-6))

    def test_crossing_road_leaves_a_gap(self):
        tr = track(race=[STRAIGHT], access=[[[0, 25], [400, 25]]], roads=[[[200, -100], [200, 100]]])
        xs = np.array([r[1] for r in rf.plan(tr) if r[0] == rf.G_CATCH])
        self.assertFalse(np.any(np.abs(xs - 200) < 3.5 + rf.ROAD_CLEAR_M))
        self.assertTrue(np.any(xs < 190) and np.any(xs > 210))

    def test_tyre_wall_outside_a_tight_corner_with_short_runoff(self):
        corner = arc(0, 0, 40, -math.pi / 2, math.pi / 2)                  # a 40 m hairpin
        lead = np.vstack(([[-200, -40]], corner[:1]))
        race = np.vstack((lead, corner, [[-200, 40]]))
        service = arc(0, 0, 40 + 5 + 20, -math.pi / 2, math.pi / 2)        # 20 m past the edge, outside
        tr = track(race=[race], access=[service])
        tyres = [r for r in rf.plan(tr) if r[0] == rf.G_TYRE]
        self.assertGreater(len(tyres), 50)
        rad = np.hypot([r[1] for r in tyres], [r[2] for r in tyres])
        self.assertTrue(np.all(rad > 45 + rf.CLEAR_M + rf.MARGIN_M - 0.5))   # outside, past the margin
        self.assertTrue(np.all(rad < 65 - 2 - 0.5))                          # in front of the service road
        self.assertClear(tr, tyres)

    def test_corner_split_across_two_ways_is_measured_whole(self):
        corner = arc(0, 0, 40, -math.pi / 2, math.pi / 2)
        a = np.vstack(([[-200, -40]], corner[:31]))
        b = np.vstack((corner[30:], [[-200, 40]]))[::-1]                  # (the second way drawn backwards)
        service = arc(0, 0, 65, -math.pi / 2, math.pi / 2)
        tyres = [r for r in rf.plan(track(race=[a, b], access=[service])) if r[0] == rf.G_TYRE]
        ang = np.degrees(np.arctan2([r[2] for r in tyres], [r[1] for r in tyres]))
        self.assertTrue(np.any(np.abs(ang) < 5))                            # at the junction, the apex

    def test_no_tyres_on_a_small_closed_loop(self):
        ring = arc(0, 0, 40, 0, 2 * math.pi, 120)                          # 250 m skidpad in two ways
        tr = track(race=[ring[:61], ring[60:]], access=[arc(0, 0, 60, 0, 2 * math.pi, 120)])
        self.assertFalse([r for r in rf.plan(tr) if r[0] == rf.G_TYRE])

    def test_no_tyres_on_open_runoff_or_gentle_bends(self):
        corner = arc(0, 0, 40, -math.pi / 2, math.pi / 2)
        self.assertFalse([r for r in rf.plan(track(race=[corner])) if r[0] == rf.G_TYRE])
        gentle = arc(0, 0, 300, -0.5, 0.5)
        tr = track(race=[gentle], access=[arc(0, 0, 320, -0.5, 0.5)])
        self.assertFalse([r for r in rf.plan(tr) if r[0] == rf.G_TYRE])

    def test_stands_off_by_default_footprint_still_claimed(self):
        # (the user 2026-11-07: the models off for now; a footprint on the band still never a building there)
        ring = [[100, 15], [160, 15], [160, 35], [100, 35]]
        tr = track(race=[STRAIGHT], stands=[ring])
        with unittest.mock.patch.object(rf, "STANDS_ON", False):
            self.assertFalse([r for r in rf.plan(tr) if r[0] == rf.G_STAND])
        self.assertTrue(rf.race_stand(np.array(ring, float), "grandstand", [np.array(STRAIGHT, float)]))

    def test_grandstand_faces_the_track(self):
        ring = [[100, 15], [160, 15], [160, 35], [100, 35]]               # 60 x 20, its front 10 m off the edge
        tr = track(race=[STRAIGHT], stands=[ring])
        with unittest.mock.patch.object(rf, "STANDS_ON", True):
            stands = [r for r in rf.plan(tr) if r[0] == rf.G_STAND]
        self.assertEqual(len(stands), 1)                                   # 60 m at the 20 m depth's scale: one
        self.assertAlmostEqual(stands[0][4], rf.STAND_H * 60 / rf.STAND_LEN, 2)   # fitted to the length
        for r in stands:
            yaw = math.radians(r[3])
            front = (-math.sin(yaw), math.cos(yaw))                        # local +y (the open front) turned by yaw
            self.assertLess(front[1], -0.99)                               # toward the track (south)
        self.assertTrue(rf.replaces_grandstand(np.array(ring, float), [np.array(STRAIGHT, float)]))
        self.assertFalse(rf.replaces_grandstand(np.array(ring, float) + [0, 300], [np.array(STRAIGHT, float)]))

    def test_curved_grandstand_follows_its_footprint(self):
        # a 60 m deep band round the outside of a 400 m radius bend, its inner edge 2 m into the track's band
        race = arc(0, 0, 400, -0.9, 0.9)
        th = np.linspace(-0.8, 0.8, 60)
        inner, outer = 400 + 12, 400 + 72
        ring = np.r_[np.c_[inner * np.cos(th), inner * np.sin(th)], np.c_[outer * np.cos(th), outer * np.sin(th)][::-1]]
        tr = track(race=[race], stands=[ring], race_w=18.0)
        pieces = rf.stand_pieces(ring, tr.race)
        self.assertGreaterEqual(len(pieces), 4)
        heads = [math.degrees(math.atan2(a[1], a[0])) % 180 for _, a, _, _ in pieces]
        self.assertGreater(max(heads) - min(heads), 40)                   # turned along the bend, not one row
        for c, a, s, f in pieces:
            self.assertLess(np.dot(f, c / np.hypot(*c)), -0.9)            # open front toward the track (inward)
            box = rf._box_pts(c, a, f, rf.STAND_LEN * s, rf.STAND_DEPTH * s)
            self.assertGreaterEqual(float(tr.race_segs.edge(box).min()), rf.CLEAR_M)
        self.assertTrue(rf.replaces_grandstand(ring, tr.race))

    def test_grandstand_on_the_track_stays_a_building(self):
        ring = [[100, -5], [160, -5], [160, 5], [100, 5]]               # straddling the track: no model fits
        self.assertFalse(rf.replaces_grandstand(np.array(ring, float), [np.array(STRAIGHT, float)]))
        self.assertFalse([r for r in rf.plan(track(race=[STRAIGHT], stands=[ring])) if r[0] == rf.G_STAND])

    def test_guard_rail_faces_a_raceway(self):
        rsegs = guardrails._road_segments([(None, type("S", (), {"width": 10.0})(), np.array(STRAIGHT, float))])
        self.assertEqual(guardrails._road_side(np.array([[0.0, 8.0], [100.0, 8.0]]), rsegs), -1)   # track on its right
        self.assertEqual(guardrails._road_side(np.array([[100.0, 8.0], [0.0, 8.0]]), rsegs), 1)


CACHE = os.path.expanduser("~/.cache/beamng-earth")
TRACKS = {"cadwell": (53.3097, -0.0596, 0.0, 0.0), "limerock": (41.9283, -73.3832, 0.0, 0.0),
          "bandimere": (39.6650, -105.2097, 1900.0, 0.0)}      # (the strip is 1.9 km east of the given point)


@unittest.skipUnless(os.path.isdir(CACHE) and os.environ.get("BNE_REAL_TRACKS", "1") != "0", "no map cache")
class RealTracksTest(unittest.TestCase):
    """The cached cells of three tracks: furniture placed, none on the racing surface or another road."""
    counts = {}

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.ERROR)
        os.environ.setdefault("BNE_REVISIT_SKIP", "roads")
        import blockworker as bw
        import geodesy
        try:
            st = bw._State(CACHE, False, True, True, True, False, True, True)
        except Exception as ex:
            raise unittest.SkipTest(f"no map state: {ex}")
        cls.tracks = {}
        for name, (la, lo, ex, ny) in TRACKS.items():
            frame = geodesy.EnuFrame(la, lo)
            tr = rf.gather(frame, lambda k, l: st.md.cell(k, layers=l, kind="demand").layers,
                           (ex - 900, ny - 900, ex + 900, ny + 900), pad=0.0)
            if not tr.race:
                raise unittest.SkipTest(f"{name}: no raceways in the cache")
            cls.tracks[name] = (tr, rf.plan(tr))
            cls.counts[name] = dict(collections.Counter(r[0] for r in cls.tracks[name][1]))

    def test_furniture_placed_and_clear(self):
        for name, (tr, rows) in self.tracks.items():
            c = self.counts[name]
            self.assertGreater(c.get(rf.G_CATCH, 0), 20, name)
            self.assertGreater(c.get(rf.G_TYRE, 0), 10, name)
            xy = np.array([[r[1], r[2]] for r in rows if r[0] != rf.G_STAND])
            self.assertGreaterEqual(float(tr.race_segs.edge(xy).min()), rf.CLEAR_M, name)
            self.assertGreaterEqual(float(tr.road_segs.edge(xy).min()), rf.ROAD_CLEAR_M, name)

    def test_cadwell_has_inferred_fences(self):
        tr, rows = self.tracks["cadwell"]
        mapped = {r[5] for r in rf.mapped_rows(tr)}
        self.assertGreater(sum(1 for r in rows if r[0] == rf.G_CATCH and r[5] not in mapped), 100)

    @unittest.skipUnless(rf.STANDS_ON, "stand models off by default (BNE_RACE_STANDS=on)")
    def test_grandstands(self):
        self.assertGreaterEqual(self.counts["cadwell"].get(rf.G_STAND, 0), 1)
        self.assertGreaterEqual(self.counts["bandimere"].get(rf.G_STAND, 0), 3)


if __name__ == "__main__":
    unittest.main()
