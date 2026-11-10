"""earthd.Server._veg_geo_job: the trees geotiles asked for while a road-process job waits for its turn join it (one
job of up to VEG_GEO_BATCH tiles instead of one each behind the road tiles); blockworker.veg_geo_many builds them."""
import concurrent.futures
import os
import sys
import threading
import unittest
import unittest.mock
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import blockworker  # noqa: E402
import earthd  # noqa: E402


class FakePool:
    """RespawningPool.submit_low_lazy's contract: make_args called at handover (here: hand_over)."""

    def __init__(self):
        self.queued = []

    def submit_low_lazy(self, fn, make_args):
        f = concurrent.futures.Future()
        self.queued.append((f, fn, make_args))
        return f

    def hand_over(self):
        f, fn, make_args = self.queued.pop(0)
        args, kwargs = make_args()
        f.set_result(fn(*args, **kwargs))
        return args


def server(pool):
    return SimpleNamespace(road_pool=pool, _veg_jobs={}, _veg_job_lock=threading.Lock(), out_dir="/o", vfs_dir="/v")


ANCHOR = SimpleNamespace(id=3, lat=39.5, lon=-80.0)


class VegGeoJobTest(unittest.TestCase):
    def setUp(self):
        patch = unittest.mock.patch.object(blockworker, "veg_geo",
                                           lambda aid, lat, lon, zoom, gx, gy, *a: ("tile", gx, gy))
        patch.start()
        self.addCleanup(patch.stop)

    def test_waiting_tiles_share_a_job(self):
        pool = FakePool()
        s = server(pool)
        got = [earthd.Server._veg_geo_job(s, ANCHOR, 15, gx, 7, False) for gx in range(5)]
        self.assertEqual(len(pool.queued), 1)
        self.assertEqual([i for _f, i in got], [0, 1, 2, 3, 4])
        args = pool.hand_over()
        self.assertEqual(args[:5], (3, 39.5, -80.0, 15, [(0, 7), (1, 7), (2, 7), (3, 7), (4, 7)]))
        res = got[0][0].result()
        self.assertEqual([res[i] for _f, i in got], [("tile", gx, 7) for gx in range(5)])
        # handed over: the next tile starts a job of its own
        f, i = earthd.Server._veg_geo_job(s, ANCHOR, 15, 9, 9, False)
        self.assertEqual((len(pool.queued), i), (1, 0))
        self.assertIsNot(f, got[0][0])

    def test_a_job_holds_at_most_a_batch(self):
        pool = FakePool()
        s = server(pool)
        n = earthd.VEG_GEO_BATCH + 3
        got = [earthd.Server._veg_geo_job(s, ANCHOR, 15, gx, 0, False) for gx in range(n)]
        self.assertEqual(len(pool.queued), 2)
        self.assertEqual(len(pool.hand_over()[4]), earthd.VEG_GEO_BATCH)
        self.assertEqual(pool.hand_over()[4], [(gx, 0) for gx in range(earthd.VEG_GEO_BATCH, n)])
        self.assertEqual(got[-1][1], 2)

    def test_other_anchor_or_zoom_or_format_not_mixed(self):
        pool = FakePool()
        s = server(pool)
        earthd.Server._veg_geo_job(s, ANCHOR, 15, 0, 0, False)
        earthd.Server._veg_geo_job(s, ANCHOR, 14, 0, 0, False)
        earthd.Server._veg_geo_job(s, ANCHOR, 15, 0, 0, True)
        earthd.Server._veg_geo_job(s, SimpleNamespace(id=4, lat=1.0, lon=2.0), 15, 0, 0, False)
        self.assertEqual(len(pool.queued), 4)

    def test_a_failed_tile_fails_alone(self):
        def veg_geo(aid, lat, lon, zoom, gx, gy, *a):
            if gx == 1:
                raise ValueError("no DEM")
            return gx
        with unittest.mock.patch.object(blockworker, "veg_geo", veg_geo):
            out = blockworker.veg_geo_many(1, 0.0, 0.0, 15, [(0, 0), (1, 0), (2, 0)], "/o", "/v")
        self.assertEqual(out[0], 0)
        self.assertIsInstance(out[1], ValueError)
        self.assertEqual(out[2], 2)

    def test_a_plain_executor_builds_one_tile_a_job(self):
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            s = server(ex)
            f, i = earthd.Server._veg_geo_job(s, ANCHOR, 15, 4, 5, False)
            self.assertEqual((f.result(timeout=10), i), ([("tile", 4, 5)], 0))


if __name__ == "__main__":
    unittest.main()
