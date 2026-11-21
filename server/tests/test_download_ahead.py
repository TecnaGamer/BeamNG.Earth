"""earthd.Server.download_ahead: the raw data along a driving player's straight path, fetched ahead."""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import geodesy  # noqa: E402


class SyncPool:
    def submit(self, fn):
        fn()


class FakeDem:
    def __init__(self):
        self.asked = []

    def keys_around(self, lat, lon, radius, zoom):
        return [("k", round(lat, 4), round(lon, 4), zoom)]

    def prefetch(self, keys):
        self.asked += keys
        return len(keys), False


class FakeMapdata:
    def __init__(self):
        self.cells = []

    def has(self, key, layer):
        return False

    def cell(self, key, layers, kind):
        self.cells.append((key, layers, kind))


def server():
    s = types.SimpleNamespace(dem=FakeDem(), mapdata=FakeMapdata(), buildings=True, ahead_pool=SyncPool(),
                              ahead_busy=False, ahead_last=None, ahead_stats={"dem": 0, "cells": 0, "deferred": 0},
                              layout=types.SimpleNamespace(block=1024))
    return s


def anchor(aid=1):
    return types.SimpleNamespace(id=aid, frame=geodesy.EnuFrame(36.17, -115.145), hm_zoom=15)


class DownloadAheadTest(unittest.TestCase):
    def run_at(self, s, a, x, y, vx, vy):
        earthd.Server.download_ahead(s, a, x, y, vx, vy)

    def test_points_ahead_on_the_path(self):
        s, a = server(), anchor()
        self.run_at(s, a, 0.0, 0.0, 0.0, 40.0)          # 40 m/s north: 60 s would be 2400 m (< AHEAD_MAX_M)
        self.assertEqual(len(s.dem.asked), len(earthd.AHEAD_S))
        lats = [k[1] for k in s.dem.asked]
        self.assertEqual(lats, sorted(lats), "nearest point first, going north")
        self.assertAlmostEqual((lats[-1] - 36.17) * 111000, 40.0 * earthd.AHEAD_S[-1], delta=60)
        self.assertTrue(s.mapdata.cells and all(k == "prefetch" for _, _, k in s.mapdata.cells))
        self.assertFalse(s.ahead_busy)

    def test_far_points_capped(self):
        s, a = server(), anchor()
        self.run_at(s, a, 0.0, 0.0, 200.0, 0.0)         # 200 m/s: capped at AHEAD_MAX_M
        lon_m = (s.dem.asked[-1][2] + 115.145) * 111000 * 0.807
        self.assertAlmostEqual(lon_m, earthd.AHEAD_MAX_M, delta=80)

    def test_slow_or_unmoved_does_nothing(self):
        s, a = server(), anchor()
        self.run_at(s, a, 0.0, 0.0, 2.0, 0.0)           # walking pace
        self.assertEqual(s.dem.asked, [])
        self.run_at(s, a, 0.0, 0.0, 30.0, 0.0)
        n = len(s.dem.asked)
        self.run_at(s, a, 50.0, 0.0, 30.0, 0.0)         # less than AHEAD_MOVE_M later: no new round
        self.assertEqual(len(s.dem.asked), n)
        self.run_at(s, a, 50.0 + earthd.AHEAD_MOVE_M, 0.0, 30.0, 0.0)
        self.assertGreater(len(s.dem.asked), n)

    def test_no_buildings_roads_only(self):
        # (from PR #64) without buildings the road layer is still fetched ahead (the road tiles need it)
        s, a = server(), anchor()
        s.buildings = False
        self.run_at(s, a, 0.0, 0.0, 30.0, 0.0)
        self.assertTrue(s.dem.asked)
        self.assertTrue(s.mapdata.cells and all(layers == ("roads",) for _, layers, _ in s.mapdata.cells))



class FakeFuture:
    def result(self):
        return 0.0


class FakeMapAheadPool:
    workers = 1

    def __init__(self):
        import threading
        self.keys = []
        self.lock = threading.Lock()

    def submit(self, fn, key, layers, veg, towns=False, plan=None, due=None):
        with self.lock:
            self.keys.append(key)
            self.towns, self.plan = towns, plan
            self.due = getattr(self, "due", []) + [due]
        return FakeFuture()


def map_ahead_server():
    import threading
    s = server()
    s.map_ahead_pool = FakeMapAheadPool()
    s.map_ahead_last, s.map_ahead_want, s.map_ahead_done = None, [], set()
    s.map_ahead_cv, s.map_ahead_threads = threading.Condition(), None
    s.map_ahead_stats, s.map_ahead_state = {"cells": 0, "s": 0.0}, {}
    s.veg = None
    s._map_ahead_loop = lambda: earthd.Server._map_ahead_loop(s)
    return s


class MapAheadTest(unittest.TestCase):
    def wait_for(self, pool, n):
        import time
        t0 = time.monotonic()
        while len(pool.keys) < n and time.monotonic() - t0 < 5.0:
            time.sleep(0.01)

    def test_download_ahead_leaves_the_cells_to_it(self):
        s, a = map_ahead_server(), anchor()
        earthd.Server.download_ahead(s, a, 0.0, 0.0, 0.0, 40.0)
        self.assertTrue(s.dem.asked)
        self.assertEqual(s.mapdata.cells, [])

    def test_cells_along_the_path_nearest_first_once(self):
        s, a = map_ahead_server(), anchor()
        earthd.Server.map_ahead(s, a, 0.0, 0.0, 0.0, 40.0)      # 40 m/s north: 4.8 km ahead
        n = len(s.map_ahead_done) + len(s.map_ahead_want)
        self.wait_for(s.map_ahead_pool, n)
        keys = s.map_ahead_pool.keys
        self.assertGreater(len(keys), 2)
        self.assertEqual(len(keys), len(set(keys)))
        ys = [k[2] for k in keys]       # (tile rows count southwards)
        self.assertGreaterEqual(ys[0], max(ys) - 1, "the cells around the car first")
        # (the guess spreads with the distance: the last cells are the far end's, a row of them at most beside it)
        self.assertLessEqual(ys[-1], min(ys) + 1, "the farthest north last")
        # each with the moment the car is MAP_AHEAD_URGENT_S from it: at once round the car, later further on
        import time
        due, now = s.map_ahead_pool.due, time.time()
        self.assertLess(due[0], now)
        self.assertGreater(due[-1], now + 60.0 - earthd.MAP_AHEAD_URGENT_S)
        earthd.Server.map_ahead(s, a, 50.0, 0.0, 0.0, 40.0)     # less than MAP_AHEAD_MOVE_M later: no new list
        self.assertEqual(s.map_ahead_last[1], 0.0)
        earthd.Server.map_ahead(s, a, 0.0, 300.0, 0.0, 40.0)    # further on: only new cells
        self.wait_for(s.map_ahead_pool, len(keys) + 1)
        self.assertEqual(len(s.map_ahead_pool.keys), len(set(s.map_ahead_pool.keys)))

    def test_standing_asks_nothing(self):
        s, a = map_ahead_server(), anchor()
        earthd.Server.map_ahead(s, a, 0.0, 0.0, 0.0, 0.5)
        self.assertEqual(s.map_ahead_pool.keys, [])
        self.assertIsNone(s.map_ahead_threads)



class LoadMapTest(unittest.TestCase):
    def test_zone_cells_and_lead(self):
        s, a = map_ahead_server(), anchor()
        m = earthd.Server.load_map(s, a, 0.0, 0.0, 0.0, 40.0)
        self.assertEqual(m["type"], "load_map")
        self.assertGreater(len(m["zone"]), 2)
        self.assertTrue(m["cells"])
        self.assertTrue(all(c[4] == earthd.LOAD_MAP_MISSING for c in m["cells"]))
        self.assertEqual(m["lead"], 0)
        for c in m["cells"]:            # (west < east, south < north in the anchor frame)
            self.assertLess(c[0], c[2])
            self.assertLess(c[1], c[3])
        # every cell ready: the lead reaches the end of the path
        import mapcells
        for k in mapcells.cells_for_bbox(36.0, -115.4, 36.4, -114.9):
            s.map_ahead_state[(k.z, k.x, k.y)] = (earthd.LOAD_MAP_READY, 2.0)
        m = earthd.Server.load_map(s, a, 0.0, 0.0, 0.0, 40.0)
        self.assertAlmostEqual(m["lead"], 40.0 * earthd.MAP_AHEAD_S, delta=50)

    def test_standing_is_a_circle(self):
        s, a = map_ahead_server(), anchor()
        m = earthd.Server.load_map(s, a, 0.0, 0.0, 0.0, 0.0)
        self.assertEqual(m["zone"], [[0, 0, round(earthd.WARM_AROUND)]])



class LoadMapBlocksTest(unittest.TestCase):
    def test_block_states(self):
        from collections import OrderedDict
        import farfield
        lay = types.SimpleNamespace(block=512)
        srv = types.SimpleNamespace(layout=lay, hm=OrderedDict({(1, 0.0, 0.0): {"seconds": 4.2},
                                                               (1, 384.0, 0.0): {"seconds": 6.0}}),
                                    hm_inflight={(1, 768.0, 0.0): None, (1, 1152.0, 0.0): None,
                                                 (1, 1536.0, 0.0): None},
                                    hm_stage={(1, 768.0, 0.0): "the block process",
                                              (1, 1152.0, 0.0): "its base photo"})
        c = types.SimpleNamespace(s=srv, active=1, block=farfield.Block(0.0, 0.0, 512),
                                  pending_block=farfield.Block(384.0, 0.0, 512), pending_at=None,
                                  predicted_block=farfield.Block(1920.0, 0.0, 512), predicted_chain=[])
        blocks, asked = earthd.Connection.load_map_blocks(c, 1)
        st = {(b[0] + b[2]) / 2: b[4] for b in blocks}
        self.assertEqual(st[0.0], earthd.BLOCK_IN_USE)
        self.assertEqual(st[384.0], earthd.BLOCK_READY)     # asked for, but built: no wait
        self.assertEqual(st[768.0], earthd.BLOCK_BUILDING)
        self.assertEqual(st[1152.0], earthd.BLOCK_PHOTO)
        self.assertEqual(st[1536.0], earthd.BLOCK_QUEUED)
        self.assertEqual(st[1920.0], earthd.BLOCK_WANTED)
        self.assertEqual(asked, 0.0)
        # asked for and not built: the game waits on it
        import time
        c.pending_block, c.pending_at = farfield.Block(768.0, 0.0, 512), time.monotonic() - 2.5
        blocks, asked = earthd.Connection.load_map_blocks(c, 1)
        st = {(b[0] + b[2]) / 2: b[4] for b in blocks}
        self.assertEqual(st[768.0], earthd.BLOCK_ASKED)
        self.assertGreaterEqual(asked, 2.5)


class MapAheadRoadTest(unittest.TestCase):
    """map_ahead along the road the player drives on (aheadpath.along), not its straight line."""

    def test_cells_follow_the_road(self):
        import numpy as np
        s, a = map_ahead_server(), anchor()
        # driving north at 80 m/s on a road that turns east after 1 km and goes on 9 km
        road = np.array([(0.0, 0.0), (0.0, 1000.0), (9000.0, 1000.0)])
        straight = earthd.Server.ahead_path(0.0, 0.0, 0.0, 80.0)
        along = earthd.Server.ahead_path(0.0, 0.0, 0.0, 80.0, road=lambda: road)
        self.assertGreater(straight[-1][2], 9000.0)
        self.assertAlmostEqual(along[-1][2], 1000.0, delta=1.0)
        self.assertGreater(along[-1][1], 8000.0)
        earthd.Server.map_ahead(s, a, 0.0, 0.0, 0.0, 80.0, road=road)
        want = set(s.map_ahead_done) | set(s.map_ahead_want)
        east = a.frame.enu_to_geodetic(8000.0, 1000.0, 0.0)
        north = a.frame.enu_to_geodetic(0.0, 8000.0, 0.0)
        cell = lambda p: tuple(int(v) for v in earthd.mapcells.cell_of(float(p[0]), float(p[1])))   # noqa: E731
        self.assertIn(cell(east), want, "the road's cells 8 km on")
        self.assertNotIn(cell(north), want, "not the straight line's")
        self.assertLess(s.map_ahead_eta[cell(east)], earthd.MAP_AHEAD_S + 1.0)

    def test_no_road_or_a_failing_one_is_the_cars_line(self):
        def broken():
            raise RuntimeError("no net")
        straight = earthd.Server.ahead_path(0.0, 0.0, 0.0, 80.0)
        self.assertEqual(earthd.Server.ahead_path(0.0, 0.0, 0.0, 80.0, road=lambda: None), straight)
        self.assertEqual(earthd.Server.ahead_path(0.0, 0.0, 0.0, 80.0, road=broken), straight)
        self.assertEqual(earthd.Server.ahead_path(0.0, 0.0, 0.0, 0.5, road=lambda: [(0, 0), (0, 9)]), [])

    def test_the_town_signs_inputs_are_asked_with_the_cell(self):
        s, a = map_ahead_server(), anchor()
        s.towns_on, s.buildings = True, True
        earthd.Server.map_ahead(s, a, 0.0, 0.0, 0.0, 40.0)
        MapAheadTest.wait_for(self, s.map_ahead_pool, 1)
        self.assertTrue(s.map_ahead_pool.towns)

    def test_the_cell_on_the_path_before_those_beside_it(self):
        s, a = map_ahead_server(), anchor()
        s.map_ahead_done = None             # (nothing begun: the whole list stays to look at)
        s.map_ahead_done = set()
        s.map_ahead_threads = [None]        # (no worker takes any)
        earthd.Server.map_ahead(s, a, 0.0, 0.0, 0.0, 80.0)
        want, eta = s.map_ahead_want, s.map_ahead_eta
        la, lo, _ = a.frame.enu_to_geodetic(0.0, 0.0, 0.0)
        own = tuple(int(v) for v in earthd.mapcells.cell_of(float(la), float(lo)))
        self.assertEqual(want[0], own, "the cell the car is in first")
        col = [k for k in want if k[1] == own[1]]           # (its column: the cells the path north runs through)
        self.assertGreater(len(col), 3)
        self.assertEqual([k[2] for k in col], sorted((k[2] for k in col), reverse=True), "northwards in order")
        for k in col:                                       # (each before the cells beside it in its row)
            beside = [j for j in want if j[2] == k[2] and j != k]
            self.assertTrue(beside)
            self.assertTrue(all(want.index(k) < want.index(j) for j in beside))

    def test_the_cells_road_plans_are_asked_where_the_ground_is_graded(self):
        s, a = map_ahead_server(), anchor()
        a.lat, a.lon = 36.17, -115.145
        earthd.Server.map_ahead(s, a, 0.0, 0.0, 0.0, 40.0)
        MapAheadTest.wait_for(self, s.map_ahead_pool, 1)
        self.assertIsNone(s.map_ahead_pool.plan)
        s2 = map_ahead_server()
        s2.grading = True
        earthd.Server.map_ahead(s2, a, 0.0, 0.0, 0.0, 40.0)
        MapAheadTest.wait_for(self, s2.map_ahead_pool, 1)
        self.assertEqual(s2.map_ahead_pool.plan, (1, 36.17, -115.145))


class GeoPatienceTest(unittest.TestCase):
    """Server.geo_patience: how long a road geotile's build may wait for what it can be built without."""

    def setUp(self):
        import geotiles
        self.a = anchor()
        la, lo, _ = self.a.frame.enu_to_geodetic(3000.0, 0.0, 0.0)
        self.far = geotiles.tile_of(float(la), float(lo))
        self.here = geotiles.tile_of(self.a.frame.lat0, self.a.frame.lon0)

    def server(self, *cars):
        conns = [types.SimpleNamespace(active=aid, focus=at, vel=vel) for aid, at, vel in cars]
        return types.SimpleNamespace(anchors={1: self.a}, connections=conns)

    def test_until_the_car_gets_there_less_the_build(self):
        import reach
        s = self.server((1, (0.0, 0.0), (100.0, 0.0)))
        got = earthd.Server.geo_patience(s, 1, *self.far)
        self.assertLess(got, 30.0 - reach.BUILD_EST_S + 0.1)
        self.assertGreater(got, 30.0 - reach.BUILD_EST_S - 3.0)     # (its near edge: a tile's width sooner)
        self.assertEqual(earthd.Server.geo_patience(s, 1, *self.here), 0.0)     # under the car: no waiting at all

    def test_nobody_moving_waits_as_long_as_it_takes(self):
        self.assertIsNone(earthd.Server.geo_patience(self.server((1, (0.0, 0.0), (0.0, 0.5))), 1, *self.far))
        self.assertIsNone(earthd.Server.geo_patience(self.server((2, (0.0, 0.0), (100.0, 0.0))), 1, *self.far))
        self.assertIsNone(earthd.Server.geo_patience(self.server(), 1, *self.far))
        self.assertIsNone(earthd.Server.geo_patience(self.server(), 9, *self.far))

    def test_no_waiting_while_no_road_process_is_free(self):
        s = self.server((1, (0.0, 0.0), (100.0, 0.0)))
        s.road_pool = types.SimpleNamespace(inflight=2, workers=3)
        self.assertGreater(earthd.Server.geo_patience(s, 1, *self.far), 20.0)
        s.road_pool.inflight = 3            # (the tiles asked for meanwhile wait for a process)
        self.assertEqual(earthd.Server.geo_patience(s, 1, *self.far), 0.0)
        s.connections[0].vel = (0.0, 0.0)   # (nobody moving: as long as it takes, whatever the pool does)
        self.assertIsNone(earthd.Server.geo_patience(s, 1, *self.far))

    def test_the_first_car_to_get_there_counts(self):
        s = self.server((1, (0.0, 0.0), (10.0, 0.0)), (1, (2000.0, 0.0), (50.0, 0.0)))
        self.assertLess(earthd.Server.geo_patience(s, 1, *self.far), 20.0)

    def test_cell_state(self):
        s = map_ahead_server()
        c = earthd.mapcells.cell_of(*__import__("geotiles").centre(*self.far))
        kk = (int(c.z), int(c.x), int(c.y))
        self.assertEqual(earthd.Server.geo_cell_state(s, *self.far), "not asked")
        s.map_ahead_want = [kk]
        self.assertEqual(earthd.Server.geo_cell_state(s, *self.far), "queued")
        s.map_ahead_state[kk] = (earthd.LOAD_MAP_LOADING, 0.0)
        self.assertEqual(earthd.Server.geo_cell_state(s, *self.far), "loading")
        s.map_ahead_state[kk] = (earthd.LOAD_MAP_READY, 2.0)
        self.assertEqual(earthd.Server.geo_cell_state(s, *self.far), "ready")


class FarUrgencyTest(unittest.TestCase):
    """Server.build_far_geo: a far geotile a moving car is about to reach is no longer work for later (budget.due)."""

    def setUp(self):
        import geotiles
        self.a = anchor()
        la, lo, _ = self.a.frame.enu_to_geodetic(3000.0, 0.0, 0.0)
        self.far = geotiles.tile_of(float(la), float(lo))

    def server(self, vel):
        got = []

        class Pool:
            def submit(self, fn, *args):
                got.append(args)
                return types.SimpleNamespace(result=lambda: None)
        s = types.SimpleNamespace(anchors={1: self.a}, connections=[types.SimpleNamespace(active=1, focus=(0.0, 0.0),
                                                                                           vel=vel)],
                                  far_bld_pool=Pool(), mapdata=object(), out_dir="/nowhere", vfs_dir="/v",
                                  geo_rules=lambda gx, gy, z: (False, False, 0.0, None, "DE", 17))
        return s, got

    def test_due_from_the_car_s_time_to_reach_it(self):
        import time
        s, got = self.server((100.0, 0.0))
        t0 = time.time()
        earthd.Server.build_far_geo(s, "bldfar", *self.far)
        due = got[-1][-1]
        # (30 s away less FAR_URGENT_S, a tile's width sooner: its near edge)
        self.assertLess(due, t0 + 30.0 - earthd.FAR_URGENT_S + 0.5)
        self.assertGreater(due, t0 + 30.0 - earthd.FAR_URGENT_S - 3.0)
        self.assertAlmostEqual(earthd.Server.geo_reach_s(s, *self.far, 17), 30.0, delta=3.0)

    def test_nobody_moving_work_for_later(self):
        s, got = self.server((0.0, 0.5))
        earthd.Server.build_far_geo(s, "bldfar", *self.far)
        self.assertIsNone(got[-1][-1])
        self.assertIsNone(earthd.Server.geo_reach_s(s, *self.far, 17))


if __name__ == "__main__":
    unittest.main()
