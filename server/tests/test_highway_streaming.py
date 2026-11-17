"""Tests for high-speed highway and bridge streaming (speed > 28 m/s, ROADMAP #13)."""
import asyncio
import math
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import areas  # noqa: E402
import deckspans  # noqa: E402
import earthd  # noqa: E402
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import mapcells  # noqa: E402
import pools  # noqa: E402
import reach  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402


class _FakeConn:
    def __init__(self, road_workers=4):
        self.sent = []
        self.s = types.SimpleNamespace(
            out_dir=None,
            pool_shapes=None,
            road_pool=types.SimpleNamespace(workers=road_workers),
        )

    def send(self, m):
        self.sent.append(m)


class _SyncLoop:
    """Minimal loop supporting run_in_executor and call_soon_threadsafe."""
    def __init__(self):
        self.jobs = []
        self.callbacks = []

    def run_in_executor(self, pool, fn, *args):
        from concurrent.futures import Future
        fut = Future()
        self.jobs.append((fut, fn, args))
        return fut

    def call_soon_threadsafe(self, cb, *args):
        self.callbacks.append((cb, args))

    def drain_callbacks(self):
        while self.callbacks:
            cb, args = self.callbacks.pop(0)
            cb(*args)


class HighwayLookaheadTest(unittest.TestCase):
    def test_geo_wanted_preserves_normal_speed_and_focuses_corridor_at_highway_speed(self):
        frame = geodesy.EnuFrame(47.855, 11.405)
        # Normal speed (20 m/s <= 28 m/s): full SEND_M disc
        low = roads.geo_wanted(frame, 0.0, 0.0, 0.0, -20.0)
        # Highway speed (55 m/s > 28 m/s): shrunk 360-deg disc + extended forward corridor
        high = roads.geo_wanted(frame, 0.0, 0.0, 0.0, -55.0)
        self.assertGreater(len(high), 0)

        def split_tiles(gw):
            fwd = side = behind = 0
            for gx, gy in gw:
                q = geotiles.quad(frame, gx, gy)
                cx, cy = float(q[:, 0].mean()), float(q[:, 1].mean())
                along = -cy  # moving south (-y)
                lat = abs(cx)
                if along < -roads.TILE:
                    behind += 1
                elif lat > 250.0:
                    side += 1
                else:
                    fwd += 1
            return fwd, side, behind

        lf, ls, lb = split_tiles(low)
        hf, hs, hb = split_tiles(high)
        # At 55 m/s, far more forward corridor tiles and far fewer side/behind tiles than 360-deg SEND_M
        self.assertGreater(hf, lf)
        self.assertLess(hs + hb, ls + lb)

    def test_ground_busy_keeps_forward_corridor_and_deck_first_at_highway_speed(self):
        frame = geodesy.EnuFrame(47.855, 11.405)
        tr = roads.GeoTracker()
        conn = _FakeConn(road_workers=4)
        loop = _SyncLoop()
        # Pick a forward tile ~900 m ahead (beyond SEND_M=640 m) and a bridge tile ~1100 m ahead
        fwd_key = geotiles.tile_of(*frame.enu_to_geodetic(0.0, -900.0, 0.0)[:2])
        deck_key = geotiles.tile_of(*frame.enu_to_geodetic(0.0, -1100.0, 0.0)[:2])
        tr.deck_first = {deck_key}
        tr.on_pose(conn, 1, 0.0, 0.0, loop, lambda *a: None, vel=(0.0, -55.0), ground_busy=True, frame=frame)
        queued_keys = {args[1:3] for _, _, args in loop.jobs}
        # First round dispatches up to limit (8), verify deck_key is first and forward corridor >640m is eligible
        self.assertIn(deck_key, queued_keys)
        pending_keys = {j[0] for j in tr._pending[0]}
        self.assertIn(fwd_key, queued_keys | pending_keys)

    def test_continuous_refill_on_built(self):
        frame = geodesy.EnuFrame(47.855, 11.405)
        tr = roads.GeoTracker()
        conn = _FakeConn(road_workers=4)
        loop = _SyncLoop()
        tr.on_pose(conn, 1, 0.0, 0.0, loop, lambda *a: None, vel=(0.0, -55.0), ground_busy=False, frame=frame)
        initial_n = len(loop.jobs)
        self.assertEqual(initial_n, roads.BUILD_PER_POSE_FAST)
        self.assertGreater(len(tr._pending[0]), 0)
        # Complete one job -> _built schedules _dispatch_pending via call_soon_threadsafe
        fut, _, _ = loop.jobs.pop(0)
        fut.set_result(None)
        loop.drain_callbacks()
        self.assertEqual(len(loop.jobs), initial_n)  # refilled back to limit immediately


class ReachOrderingTest(unittest.TestCase):
    def test_horizon_scales_with_highway_speed(self):
        self.assertAlmostEqual(reach.horizon_s(20.0), max(reach.HORIZON_S, reach.HORIZON_MIN_M / 20.0))
        self.assertAlmostEqual(reach.horizon_s(55.0), reach.HORIZON_S_FAST)
        self.assertAlmostEqual(reach.horizon_s(134.0), reach.HORIZON_MAX_M / 134.0)

    def test_order_key_protects_near_forward_tiles_from_late_keep_inversion(self):
        # With elevated build_s=8.0s at 55 m/s, a tile 150m ahead (eta=2.7s) must still beat a 1500m tile (eta=27s)
        near_k = reach.order_key(2.7, 1.0, 10.0, 55.0, 8.0)
        far_k = reach.order_key(27.0, 1.0, 10.0, 55.0, 8.0)
        self.assertLess(near_k, far_k)


class PoolPriorityTest(unittest.TestCase):
    def test_low_cooldown_reserves_half_workers_and_pause_low_blocks(self):
        p = pools.RespawningPool.__new__(pools.RespawningPool)
        p.workers = 4
        p.inflight = 2
        p._low = [1]
        p._low_active = 1
        p._low_pauses = set()
        p._low_cv = threading.Condition()
        p._last_high_at = time.monotonic()
        p.LOW_COOLDOWN_S = 1.0
        # 2 of 4 workers busy and high-priority submit was recent -> reserve half (2) workers
        self.assertFalse(p._low_can_run())
        p.inflight = 1
        self.assertTrue(p._low_can_run())
        p.pause_low("hw")
        self.assertFalse(p._low_can_run())
        p.resume_low("hw")
        self.assertTrue(p._low_can_run())


class Phase2StreamingTest(unittest.TestCase):
    def test_follow_rule_prep_ahead_scales_at_highway_speeds(self):
        rule = earthd.FOLLOW_RULES[1024.0]
        self.assertEqual(rule.prep_ahead(25.0, 0.0), 0.0)
        # 45 m/s (162 km/h): ~14 s edge lead -> > 5 s ahead
        self.assertGreater(rule.prep_ahead(45.0, 0.0), 5.0)
        # 67 m/s (241 km/h): capped at prep_ahead_max_m / speed (~3.82 s)
        self.assertAlmostEqual(rule.prep_ahead(67.0, 0.0), rule.prep_ahead_max_m / 67.0, places=5)

    def test_photo_surface_miss_does_not_taint_grading_revisit_scope(self):
        # Simulate missing photo surface during grading: outer revisit.scope stays clean (persist=True),
        # while _road_specs_kept still detects photo miss via _PHOTO_MISS_TL so partial specs aren't stored.
        with revisit.scope() as b:
            m0 = getattr(roads._PHOTO_MISS_TL, "count", 0)
            with roads.suppress_photo_faults():
                roads._note_photo_miss()
            self.assertEqual(b.n, 0)
            self.assertGreater(getattr(roads._PHOTO_MISS_TL, "count", 0), m0)

    def test_far_limit_throttles_when_highway_terrain_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=None, road_pool=None, tile_workers=3)
            try:
                s.speed = 55.0

                class _Conn:
                    map_view = False
                    map_open = False
                    terrain_wait = 1

                conn = _Conn()
                s.connections.add(conn)
                self.assertEqual(s.far_limit(), 1)
                conn.terrain_wait = 0
                self.assertEqual(s.far_limit(), s.far_drive)
            finally:
                s.hm_pool.shutdown(wait=True)
                s.tile_pool.shutdown(wait=True)

    def test_areas_world_phase_includes_roadsurf_and_plan_when_supported(self):
        class _FakeSrv:
            grading = True
            surface = True
            gas_stations = False
            mapdata = object()
            road_pool = None
            fast_pool = None

            def plan_prebuilt(self, z, x, y):
                return False

            def prebuild_plan(self, z, x, y):
                return True

            def roadsurf_prebuilt(self, z, x, y):
                return False

            def prebuild_roadsurf(self, z, x, y):
                return 1

            def geo_prebuilt(self, gx, gy):
                return None

            def prebuild_geo(self, gx, gy):
                return "k"

        with tempfile.TemporaryDirectory() as tmp:
            a = areas.Areas(tmp, _FakeSrv())
            steps = a.phase_steps("world", (47.74, 11.33, 47.77, 11.36))
            kinds = [st[0] for st in steps]
            self.assertIn("roadsurf", kinds)
            self.assertIn("plan", kinds)
            self.assertLess(kinds.index("roadsurf"), kinds.index("geo"))
            self.assertLess(kinds.index("plan"), kinds.index("geo"))


class WarmRoadsAndDeckSpansTest(unittest.TestCase):
    def test_warm_roads_projects_further_ahead_at_highway_speed(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=None, road_pool=None, grading=True)
            try:
                anchor = s.new_anchor(47.855, 11.405)
                captured = []

                class _FakeQ:
                    def put_nowait(self, item):
                        captured.append(item)

                s.block_pools = [(None, _FakeQ())]
                # Mock grader so no real mapdata/DEM is needed
                fake_grader = types.SimpleNamespace(shared=None, planned=lambda k: False, warm=lambda k: None)
                with mock.patch.object(s, "grader_for", return_value=fake_grader):
                    s.warm_roads(anchor, 0.0, 0.0, 0.0, -55.0)
                self.assertEqual(len(captured), 1)
                keys = captured[0][3]
                # Farthest warmed cell along -y should be ~3.3 km (60 s * 55 m/s) ahead, well past 2.0 km WARM_AROUND
                dists = []
                for k in keys:
                    cla, clo = k.center()
                    cx, cy, _ = anchor.frame.geodetic_to_enu(cla, clo, 0.0)
                    dists.append(-float(cy))
                self.assertGreater(max(dists), 2800.0)
            finally:
                s.hm_pool.shutdown(wait=True)
                s.tile_pool.shutdown(wait=True)

    def test_deckspans_accepts_speed_scaled_radius(self):
        frame = geodesy.EnuFrame(47.855, 11.405)
        import numpy as np
        pieces = [("1", np.array([[0.0, 1500.0], [0.0, 1600.0]]))]
        msg_default = deckspans.message(1, 0.0, 0.0, pieces, radius=deckspans.RADIUS_M)
        msg_fast = deckspans.message(1, 0.0, 0.0, pieces, radius=1800.0)
        self.assertEqual(len(msg_default["spans"]), 0)
        self.assertEqual(len(msg_fast["spans"]), 1)


if __name__ == "__main__":
    unittest.main()
