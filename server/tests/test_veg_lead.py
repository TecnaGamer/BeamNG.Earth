"""The trees along a fast car's way ahead (vegetation.Tracker lead), their order (rank) and earthd.Server.veg_rank."""
import concurrent.futures
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import vegetation  # noqa: E402

FRAME = geodesy.EnuFrame(48.40, 11.62)


class Loop:
    def __init__(self):
        self.futs = []

    def run_in_executor(self, pool, fn, *args):
        f = concurrent.futures.Future()
        self.futs.append(f)
        return f


def pose(tr, x, y, vel):
    conn = SimpleNamespace(send=lambda m: None, s=None, focus=(x, y), vel=vel)
    tr.on_pose(conn, 1, x, y, Loop(), lambda gx, gy: None, frame=FRAME, vel=vel)
    return conn


def dist(k, x, y):
    return float(geotiles.dists(FRAME, [k], geotiles.Z, [(x, y)])[0, 0])


class LeadTest(unittest.TestCase):
    def test_slow_car_wants_the_disc_only(self):
        tr = vegetation.Tracker(per_pose=4, geo=True, lead=vegetation.LEAD)
        pose(tr, 0.0, 0.0, (10.0, 0.0))
        self.assertTrue(tr.rank)
        self.assertLessEqual(max(dist(k, 0.0, 0.0) for k in tr.rank), tr.send_m + 1.0)
        self.assertEqual(tr.ahead, {})
        tr.close()

    def test_fast_car_wants_the_way_ahead_too(self):
        tr = vegetation.Tracker(per_pose=4, geo=True, lead=vegetation.LEAD)
        v = 85.0
        pose(tr, 0.0, 0.0, (v, 0.0))
        far = [k for k in tr.rank if dist(k, 0.0, 0.0) > tr.send_m + 1.0]
        self.assertTrue(far, "tiles beyond the disc, along the way")
        reach = v * vegetation.LEAD_S
        for k in far:
            la, lo = geotiles.centre(*k)
            cx, cy, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            self.assertGreater(cx, 0.0)                                 # ahead, not behind
            self.assertLess(abs(cy), vegetation.LEAD_CORRIDOR_M + 200)  # beside the way (a tile's size of slack)
            self.assertLess(cx, reach + vegetation.LEAD_CORRIDOR_M + 200)
        self.assertGreater(max(dist(k, 0.0, 0.0) for k in far), 0.8 * reach - tr.send_m)
        # the loading line counts the disc's tiles only
        self.assertTrue(all(dist(k, 0.0, 0.0) <= tr.send_m + 1.0 for k in tr.car))
        tr.close()

    def test_order_right_round_the_car_then_ahead_then_the_rest(self):
        tr = vegetation.Tracker(per_pose=4, geo=True, lead=vegetation.LEAD)
        pose(tr, 0.0, 0.0, (85.0, 0.0))
        order = sorted(tr.rank, key=tr.rank.get)

        def centre(k):
            la, lo = geotiles.centre(*k)
            cx, cy, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            return float(cx), float(cy)
        self.assertLessEqual(dist(order[0], 0.0, 0.0), vegetation.NEAR_FIRST_M)
        ahead = next(k for k in order if 1500.0 < centre(k)[0] and abs(centre(k)[1]) < 150.0)     # 1.5 km on, on the way
        behind = next(k for k in order if centre(k)[0] < -600.0)
        side = next(k for k in order if abs(centre(k)[1]) > 900.0 and abs(centre(k)[0]) < 300.0)
        self.assertLess(tr.rank[ahead], tr.rank[behind])
        self.assertLess(tr.rank[ahead], tr.rank[side])
        tr.close()

    def test_tiles_on_the_way_are_not_dropped_beyond_drop_m(self):
        tr = vegetation.Tracker(per_pose=4, geo=True, lead=vegetation.LEAD)
        conn = pose(tr, 0.0, 0.0, (85.0, 0.0))
        far = max(tr.ahead, key=lambda k: dist(k, 0.0, 0.0))
        self.assertGreater(dist(far, 0.0, 0.0), tr.drop_m)
        tr.sent.add(far)
        sent = []
        conn.send = sent.append
        tr._gate = None
        tr.on_pose(conn, 1, 0.0, 0.0, Loop(), lambda gx, gy: None, frame=FRAME, vel=(85.0, 0.0))
        self.assertIn(far, tr.sent)
        self.assertFalse([m for m in sent if m["type"] == "veg_drop"])
        # the car stopped and the way ahead is long past: dropped as any tile that far
        tr.ahead = {}
        tr._gate = None
        tr.on_pose(conn, 1, 0.0, 0.0, Loop(), lambda gx, gy: None, frame=FRAME, vel=(0.0, 0.0))
        self.assertNotIn(far, tr.sent)
        tr.close()

    def test_with_the_time_to_reach_tree_the_way_is_its_roads(self):
        import time
        import numpy as np
        import reach
        if not reach.available():
            self.skipTest("earthfast without the reach kernels")
        v = 85.0                    # (the tree's road goes north; the car's velocity alone would say east)
        pts = np.array([[0.0, d, d / v, 1.0] for d in np.arange(0.0, 3000.0, 32.0)])
        tree = reach.Tree({"pts": pts, "mpp": pts[:, :3], "frontier": np.zeros((0, 4)), "states": 1}, 0.0, 0.0, 0.0, v,
                          time.monotonic())
        tr = vegetation.Tracker(per_pose=4, geo=True, lead=vegetation.LEAD)
        conn = SimpleNamespace(send=lambda m: None, s=None)
        tr.on_pose(conn, 1, 0.0, 0.0, Loop(), lambda gx, gy: None, frame=FRAME, tree=tree, vel=(0.0, v))
        far = [k for k in tr.rank if dist(k, 0.0, 0.0) > tr.send_m + 1.0]
        self.assertTrue(far)
        for k in far:
            la, lo = geotiles.centre(*k)
            cx, cy, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            self.assertGreater(cy, 0.0)
            self.assertLess(abs(cx), vegetation.LEAD_CORRIDOR_M + 200)
        north = geotiles.tiles_near(FRAME, 0.0, 2000.0, 1.0, exact=True)[0][:2]
        east = geotiles.tiles_near(FRAME, 1000.0, 0.0, 1.0, exact=True)[0][:2]
        self.assertLess(tr.rank[north], tr.rank[east], "2 km up the road before 1 km beside the car")
        tr.close()

    def test_no_lead_no_way(self):
        tr = vegetation.Tracker(per_pose=4, geo=True)
        pose(tr, 0.0, 0.0, (85.0, 0.0))
        self.assertTrue(tr.car)         # (earthfast's own pass: the disc round the car, its tiles started nearest first)
        self.assertLessEqual(max(dist(k, 0.0, 0.0) for k in tr.car | tr.building), tr.send_m + 1.0)
        tr.close()


class VegRankTest(unittest.TestCase):
    def test_smallest_of_the_players_ranks_or_none(self):
        a = SimpleNamespace(veg=SimpleNamespace(rank={(1, 1): 5, (2, 2): 0}))
        b = SimpleNamespace(veg=SimpleNamespace(rank={(1, 1): 2}))
        s = SimpleNamespace(connections=[a, b])
        self.assertEqual(earthd.Server.veg_rank(s, (1, 1)), 2)
        self.assertEqual(earthd.Server.veg_rank(s, (2, 2)), 0)
        self.assertIsNone(earthd.Server.veg_rank(s, (9, 9)))


if __name__ == "__main__":
    unittest.main()


class BuildVegGeoOrderedTest(unittest.TestCase):
    """Server.build_veg_geo with the trees processes' queue (veg_jobs): one tile a job, None when dropped unwanted."""

    def server(self, rank):
        import orderedjobs
        import threading

        class Inline:
            workers = 1

            def submit(self, fn, *args):
                f = concurrent.futures.Future()
                f.set_result(fn(*args))
                return f
        anchor = SimpleNamespace(id=3, lat=48.4, lon=11.62, zooms={0: 15})
        return SimpleNamespace(veg=object(), anchors={3: anchor}, mapdata=object(), out_dir="/o", vfs_dir="/v",
                               veg_jobs=orderedjobs.OrderedJobs(Inline(), rank, "trees"), road_pool=None,
                               _stale=None, _veg_jobs={}, _veg_job_lock=threading.Lock())

    def test_built_or_dropped(self):
        import unittest.mock
        import blockworker
        with unittest.mock.patch.object(blockworker, "veg_geo_many",
                                        lambda aid, lat, lon, zoom, tiles, *a: [("tile",) + tuple(t) for t in tiles]), \
                unittest.mock.patch.object(earthd.Server, "stale_of", lambda self, anchor: None):
            s = self.server({(1, 2): 0}.get)
            self.assertEqual(earthd.Server.build_veg_geo(s, 1, 2, aid=3), ("tile", 1, 2))
            self.assertIsNone(earthd.Server.build_veg_geo(s, 7, 7, aid=3), "no tracker wants it: dropped")
            self.assertEqual(s.veg_jobs.stats["dropped"], 1)
