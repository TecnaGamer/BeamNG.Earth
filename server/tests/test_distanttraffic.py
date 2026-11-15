"""Visual-road routes: shared road rules, ground/deck heights, stable ownership and bounded payloads."""
import json
import asyncio
import sys
import tempfile
import importlib.util
from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import distanttraffic as dt
from test_roads import FRAME, features, flat, rules
import test_server

BOX = (-1000, -1000, 1000, 1000)


class NativeContractTest(unittest.TestCase):
    def load(self, native):
        spec = importlib.util.spec_from_file_location("traffic_native_check", dt.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(dt.fast, "_ef", native):
            spec.loader.exec_module(module)
        return module

    def test_rejects_older_native(self):
        with self.assertRaisesRegex(dt.fast.NativeMissing, "143"):
            self.load(SimpleNamespace(VERSION=142, TrafficRoutes=dt._KERNEL))

    def test_rejects_newer_native_without_routes(self):
        with self.assertRaisesRegex(dt.fast.NativeMissing, "TrafficRoutes"):
            self.load(SimpleNamespace(VERSION=144))

    def test_accepts_matching_native(self):
        module = self.load(SimpleNamespace(VERSION=143, TrafficRoutes=dt._KERNEL))
        self.assertIs(module._KERNEL, dt._KERNEL)


class TrackerHistoryTest(unittest.TestCase):
    def test_dropped_tiles_retain_no_diagnostic_history(self):
        import vegetation
        conn = SimpleNamespace(s=None, send=lambda _m: None)
        tracker = vegetation.Tracker(geo=True, z=dt.Z, rebuild_history=False)
        future = Future()
        future.set_result(("/traffic.json", 0, (0, 0), 0))
        for i in range(10000):
            key = (i, 0)
            tracker._built_geo(conn, key, future)
            tracker.sent.discard(key)
        self.assertFalse(tracker.sent or tracker.ever)
        tracker.close()
        # Existing trackers still report repeated builds when their diagnostics are enabled.
        tracker = vegetation.Tracker(geo=True, z=dt.Z)
        for _ in range(2):
            tracker._built_geo(conn, (0, 0), future)
        self.assertEqual(tracker.rebuilt, 1)
        tracker.close()


class RoutesTest(unittest.TestCase):
    def test_two_sides_and_duplicate_cells(self):
        f = features([[(0, 0), (600, 0)]], ["primary"])
        routes = dt.build(FRAME, [f, f], flat, BOX)
        self.assertEqual(len(routes), 2)
        a, b = sorted(routes, key=lambda p: p["id"])
        self.assertEqual({p["id"] for p in routes}, {"id000:g0c0:1", "id000:g0c0:-1"})
        np.testing.assert_allclose([p[0] for p in a["points"]], [p[0] for p in b["points"]][::-1], atol=0.002)
        self.assertGreater(a["points"][0][1], b["points"][0][1])
        left = dt.build(FRAME, [f], flat, BOX, left_hand=True)
        self.assertGreater(left[0]["points"][0][1], routes[0]["points"][0][1])

    def test_oneway_and_limit(self):
        f = features([[(0, 0), (300, 0)]], ["secondary"])
        f.rules["oneway"] = rules([(0, -1.0)], "float")
        f.rules["max_speed_kmh"] = rules([(0, 72.0)], "float")
        p, = dt.build(FRAME, [f], flat, BOX)
        self.assertTrue(p["id"].endswith(":-1"))
        self.assertGreater(p["points"][0][0], p["points"][-1][0])
        self.assertEqual(p["offset"], 0)
        self.assertEqual(p["speed"], 20)

    def test_ground_parts_do_not_cross_a_bridge_or_tunnel(self):
        for flag in ("bridge", "tunnel"):
            f = features([[(0, 0), (600, 0)]], ["primary"], flags=[(0, "is_" + flag, 0.3, 0.7)])
            paths = dt.build(FRAME, [f], flat, BOX)
            self.assertEqual(len(paths), 4, flag)
            for path in paths:
                xx = [p[0] for p in path["points"]]
                self.assertTrue(max(xx) < 182 or min(xx) > 418, (flag, min(xx), max(xx)))
                self.assertTrue(all(abs(p[2] - 10) < 0.1 for p in path["points"]))

    def test_divided_carriageways_follow_shared_direction_rules(self):
        f = features([[(0, 0), (600, 0)], [(0, 12), (600, 12)]], ["primary"] * 2)
        f.attrs['name'] = np.array(['Avenue', 'Avenue'])
        for left in (False, True):
            specs = dt.roads.road_specs(f, left_hand=left)
            self.assertEqual({sp.dir for sp in specs}, {1, -1})
            paths = dt.build(FRAME, [f], flat, BOX, left_hand=left)
            for i, sp in enumerate(specs):
                path, = [p for p in paths if p['id'].startswith(str(f.ids[i]) + ':')]
                self.assertEqual(int(path['id'].rsplit(':', 1)[1]), sp.dir)
                dx = path['points'][-1][0] - path['points'][0][0]
                self.assertGreater(dx * sp.dir, 0)
        f.rules['oneway'] = rules([(0, -1.), (1, 1.)], 'float')
        explicit = dt.build(FRAME, [f], flat, BOX)
        self.assertEqual({p['id'] for p in explicit}, {'id000:g0c0:-1', 'id001:g0c0:1'})

    def test_bridge_uses_solved_deck_relief_not_water(self):
        f = features([[(0, 0), (600, 0)]], ["primary"], flags=[(0, "is_bridge", 0, 1)])
        deck = np.array([[0., 0., 30.], [300., 0., 60.], [600., 0., 30.]])
        bridges = Mock()
        bridges.parts_with_ids.return_value = [(("id000", 0), dt.roads.bridge_specs(f)[0], deck[:, :2], deck[:, 2], 0)]
        def no_ground(la, lo):
            self.fail("a solved bridge must not sample the water's elevation")
        paths = dt.build(FRAME, [f], no_ground, BOX, bridges=bridges)
        self.assertEqual({p["id"] for p in paths}, {"id000:b0c0:1", "id000:b0c0:-1"})
        for path in paths:
            self.assertAlmostEqual(max(p[2] for p in path["points"]), 60, delta=0.01)
            self.assertAlmostEqual(path["points"][0][2], 30, delta=0.01)
            self.assertAlmostEqual(path["points"][-1][2], 30, delta=0.01)
        far = dt.build(FRAME, [f], no_ground, BOX, major_only=True, bridges=bridges)
        self.assertEqual(paths, far, "both streams share the same bridge and motion identity")

    def test_missing_or_invalid_deck_is_not_replaced_by_water(self):
        f = features([[(0, 0), (600, 0)]], ["primary"], flags=[(0, "is_bridge", 0, 1)])
        bridges = Mock()
        bridges.parts_with_ids.return_value = []
        self.assertEqual(dt.build(FRAME, [f], flat, BOX, bridges=bridges), [])
        bridges.parts_with_ids.return_value = [(("id000", 0), dt.roads.bridge_specs(f)[0], np.array([[0., 0.], [600., 0.]]),
                                      np.array([30., np.nan]), 0)]
        self.assertEqual(dt.build(FRAME, [f], flat, BOX, bridges=bridges), [])

    def test_separate_bridge_parts_have_tile_independent_ids(self):
        f = features([[(0, 0), (1000, 0)]], ["primary"],
                     flags=[(0, "is_bridge", .1, .3), (0, "is_bridge", .7, .9)])
        bridges, found = Mock(), []
        for part, (x, z, box) in enumerate(((100, 40, (-10, -20, 350, 20)), (700, 70, (650, -20, 1010, 20)))):
            bridges.parts_with_ids.return_value = [(("id000", part), dt.roads.bridge_specs(f)[0], np.array([[x, 0.], [x + 200, 0.]]),
                                          np.array([z, z]), 0)]
            found.append({p["id"] for p in dt.build(FRAME, [f], flat, box, bridges=bridges) if ":b" in p["id"]})
        self.assertEqual(found[0], {"id000:b0c0:1", "id000:b0c0:-1"})
        self.assertEqual(found[1], {"id000:b1c0:1", "id000:b1c0:-1"})

    def test_chain_view_preserves_bridge_part_ids_and_default_contract(self):
        import roads
        f = features([[(0, 0), (1000, 0)]], ["primary"],
                     flags=[(0, "is_bridge", .1, .3), (0, "is_bridge", .7, .9)])
        profiles = roads.BridgeProfiles(FRAME, lambda key: f, flat)
        keys = roads.part_keys(f, 0, "bridge")      # (each part's key, the same in every cell's copy)
        for part, x in zip(keys, (100, 700)):
            profiles.done["id000", part] = (roads.bridge_specs(f)[0], np.array([[x, 0.], [x + 200, 0.]]),
                                            np.array([30., 30.]), 0)
        cs = Mock(elev=flat)
        cs.cls = roads.BridgeProfiles
        cs.store_at.return_value = profiles
        profiles.region = "test"
        view = roads.ChainView(cs, FRAME)
        self.assertEqual([p[0] for p in view.parts_with_ids([f], (-10, -20, 1010, 20))],
                         [("id000", keys[0]), ("id000", keys[1])])
        self.assertEqual([p[0] for p in view.parts([f], (-10, -20, 1010, 20))], ["id000", "id000"])

    def test_classes_bounds_and_bends(self):
        f = features([[(0, 0), (100, 0), (100, 100)], [(0, 50), (300, 50)], [(5000, 0), (5200, 0)]],
                     ["primary", "residential", "trunk"])
        out = dt.build(FRAME, [f], flat, BOX)
        self.assertEqual(len(out), 4, "local roads are included too")
        xy = np.array(out[0]["points"])[:, :2]
        self.assertLess(np.min(np.hypot(xy[:, 0] - 100, xy[:, 1])), 3, "the lane keeps the mapped bend")

    def test_stable_id_and_geometry_in_neighbouring_tiles(self):
        f = features([[(-500, 0), (500, 0)]], ["primary"])
        a = dt.build(FRAME, [f], flat, (-600, -50, 0, 50))
        b = dt.build(FRAME, [f], flat, (0, -50, 600, 50))
        self.assertEqual(a, b, "whole parts, not a new route id and phase at the tile edge")

    def test_world_points_agree_between_tile_frames(self):
        import geotiles
        f = features([[(-500, 0), (500, 0)]], ["primary"])
        other = geotiles.WorldFrame(FRAME.lat0 + 0.002, FRAME.lon0 - 0.002)
        a = dt.build(FRAME, [f], flat, BOX)
        b = dt.build(other, [f], flat, BOX)
        self.assertEqual([p["id"] for p in a], [p["id"] for p in b])
        for pa, pb in zip(a, b):
            la, lo, h = geotiles.world_to_geodetic(other, np.array(pb["points"]))
            x, y, z = geotiles.geodetic_to_world(FRAME, la, lo, h)
            np.testing.assert_allclose(np.c_[x, y, z], pa["points"], atol=0.02)

    def test_piece_id_does_not_change_at_the_length_threshold(self):
        f = features([[(0, 0), (2000, 0)]], ["primary"])
        ids = []
        for length in (1999.99, 2000.01):
            with patch("mapgeom.enu_xy", return_value=(np.array([0.0, length]), np.array([0.0, 0.0]))):
                paths = dt.build(FRAME, [f], flat, (-100, -100, 2100, 100))
            ids.append([p["id"] for p in paths])
        self.assertEqual(ids[0], ids[1], "a neighbouring frame must not create another identity for the first piece")

    def test_payload_caps_and_order_do_not_depend_on_cell_order(self):
        a = features([[(-100, i), (500, i)] for i in range(40)], ["primary"] * 40)
        b = features([[(-100, -20), (500, -20)]], ["motorway"], ids=["motorway"])
        out = dt.build(FRAME, [a, b], flat, BOX)
        self.assertEqual(out, dt.build(FRAME, [b, a], flat, BOX))
        self.assertLessEqual(len(out), dt.MAX_PATHS)
        self.assertLessEqual(sum(len(p["points"]) for p in out), dt.MAX_TILE_POINTS)
        self.assertTrue(out[0]["id"].startswith("motorway"))
        self.assertTrue(all(len(p["points"]) <= dt.MAX_POINTS for p in out))
        huge = features([[(-10000, 0), (10000, 0)]], ["primary"])
        pieces = dt.build(FRAME, [huge], flat, BOX)
        self.assertTrue(pieces, "long rural ways are emitted as stable pieces")
        self.assertTrue(all("c" in p["id"].split(":")[1] for p in pieces))
        self.assertTrue(all(np.ptp(np.array(p["points"])[:, 0]) <= dt.CHUNK_M + 0.1 for p in pieces))

    def test_class_density_and_sparse_unpaved_roads(self):
        f = features([[(0, y), (1000, y)] for y in (0, 100, 200)], ["primary", "residential", "track"],
                     surfaces=[(2, "gravel")])
        out = dt.build(FRAME, [f], flat, BOX)
        by = {p["roadClass"]: p for p in out}
        self.assertEqual(set(by), {"primary", "residential", "track"})
        self.assertLess(by["primary"]["spacing"], by["residential"]["spacing"])
        self.assertLess(by["residential"]["spacing"], by["track"]["spacing"])
        self.assertLessEqual(by["track"]["speed"], 8)
        self.assertEqual(by["primary"]["reach"], 10000)

    def test_compaction_preserves_relief(self):
        f = features([[(0, 0), (300, 0)]], ["primary"])
        def elev(la, lo):
            x, _, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            return 10 + 5 * np.sin(np.asarray(x) / 90)
        paths = dt.build(FRAME, [f], elev, BOX)
        self.assertTrue(paths)
        for path in paths:
            p = np.array(path["points"])
            p = p[np.argsort(p[:, 0])]
            xx = np.linspace(p[0, 0], p[-1, 0], 100)
            np.testing.assert_allclose(np.interp(xx, p[:, 0], p[:, 2]), 10 + 5 * np.sin(xx / 90), atol=0.3)
            self.assertLess(len(p), 17, "simplify the sampled profile after retaining its relief")

    def test_far_stream_shares_identical_major_routes_without_minor_payload(self):
        f = features([[(0, y), (1000, y)] for y in (0, 100, 200)], ["primary", "residential", "track"])
        near = {p["id"]: p for p in dt.build(FRAME, [f], flat, BOX)}
        far = dt.build(FRAME, [f], flat, BOX, major_only=True)
        self.assertTrue(far)
        self.assertTrue(all(p["roadClass"] == "primary" and p == near[p["id"]] for p in far))

    def test_nonfinite_heights_are_not_sent(self):
        f = features([[(0, 0), (300, 0)]], ["primary"])
        self.assertEqual(dt.build(FRAME, [f], lambda la, lo: np.full(len(la), np.nan), BOX), [])

    def test_dense_tile_reserves_local_and_unpaved_routes(self):
        classes = ["primary"] * 100 + ["residential"] * 40 + ["track"] * 20
        f = features([[(-400, i), (400, i)] for i in range(len(classes))], classes)
        paths = dt.build(FRAME, [f], flat, BOX)
        self.assertEqual(len(paths), dt.MAX_PATHS)
        self.assertEqual({p["roadClass"] for p in paths}, set(classes))
        self.assertLessEqual(sum(len(p["points"]) for p in paths), dt.MAX_TILE_POINTS)

    def test_native_boundary_rejects_mismatched_samples(self):
        import fast
        planner = fast._ef.TrafficRoutes([("road", "primary", "paved", False, 0.0, 6.0, 30.0, False,
                                           [(0, np.array([[0., 0.], [300., 0.]]))])], BOX, False, False)
        _, centre = planner.next()
        with self.assertRaises(ValueError):
            planner.next()
        with self.assertRaises(ValueError):
            planner.lane_points(np.zeros(len(centre) - 1))
        lane = planner.lane_points(None)
        with self.assertRaises(ValueError):
            planner.accept(np.zeros(len(lane) - 1))
        planner.accept(np.full(len(lane), np.nan))
        self.assertIsNone(planner.next())
        self.assertEqual(planner.routes(), [])

    def test_curvature_is_in_world_z(self):
        f = features([[(0, 10000), (300, 10000)]], ["primary"])
        out = dt.build(FRAME, [f], flat, (-100, 9900, 400, 10100))
        self.assertTrue(out)
        for path in out:
            self.assertAlmostEqual(path["points"][0][2], 10 - 10000 ** 2 / (2 * 6371000), delta=0.15)

    def test_tile_prefetches_missing_roads_without_reading_them(self):
        md = Mock()
        md.has.return_value = False
        with patch("roads.cells_of_tile", return_value=["cell"]):
            self.assertIsNone(dt.tile(md, 1000, 1000, flat))
        md.prefetch.assert_called_once_with("cell", layers=("roads",))
        md.cell.assert_not_called()

    def test_write_contract_and_finite_json(self):
        data = {"v": dt.PROTO, "key": "g15_1_2", "frame": [0, 0], "pivot": 10, "paths": []}
        with tempfile.TemporaryDirectory() as folder:
            result = dt.write(folder, "/temp/routes", data)
            self.assertEqual(result, ("/temp/routes/traffic_g15_1_2.json", 0, [0, 0], 10))
            self.assertEqual(json.loads((Path(folder) / "traffic_g15_1_2.json").read_text()), data)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_ready_cell_uses_the_mapdata_contract(self):
        md = Mock()
        md.has.return_value = True
        md.cell.return_value.layers = {"roads": features([], [])}
        with patch("roads.cells_of_tile", return_value=["cell"]):
            data = dt.tile(md, 20000, 12000, flat)
        self.assertEqual(data["paths"], [])
        self.assertEqual(data["v"], dt.PROTO)
        md.cell.assert_called_once_with("cell", layers=("roads",), kind="background")


class NegotiationTest(test_server.ServerHarness):
    def test_opt_in_and_switch(self):
        self.server.geo_tiles = True
        self.server.traffic = True
        self.server.mapdata, self.server.far_bld_pool = object(), object()
        hello = {"type": "hello", "protocol": 4, "format": 4, "modVersion": "test", "geoTiles": 17}
        self.c.send(hello)
        old, _ = self.c.until(lambda m: m["type"] == "hello", 5)
        self.assertNotIn("trafficVisuals", old)
        c = self.conn()
        self.assertEqual(c.distant_traffic_far.inner, (dt.FAR_SKIP_M, 1))
        self.assertIsNone(c.distant_traffic.ever)
        self.assertIsNone(c.distant_traffic_far.ever)
        c.dispatch({"type": "traffic_visuals", "enabled": True})
        self.assertFalse(c.traffic_on, "an old client cannot enable the stream")
        self.c.send(dict(hello, trafficVisuals=dt.PROTO))
        new, _ = self.c.until(lambda m: m["type"] == "hello", 5)
        self.assertEqual(new["trafficVisuals"], dt.PROTO)
        self.assertFalse(c.traffic_on, "negotiation alone starts no jobs")
        c.dispatch({"type": "traffic_visuals", "enabled": True})
        self.assertTrue(c.traffic_on)
        c.distant_traffic.sent.add((1, 2))
        c.distant_traffic_far.sent.add((3, 4))
        c.dispatch({"type": "traffic_visuals", "enabled": False})
        self.assertFalse(c.traffic_on)
        c.dispatch({"type": "traffic_visuals", "enabled": True})
        self.assertTrue(c.distant_traffic.sent and c.distant_traffic_far.sent, "an ordinary toggle retains the cache")
        c.dispatch({"type": "traffic_visuals", "enabled": True, "reset": True})
        self.assertFalse(c.distant_traffic.sent or c.distant_traffic_far.sent, "a discarded client cache needs both streams again")

    def test_requires_traffic_mapdata_and_worker(self):
        self.server.geo_tiles, self.server.traffic = True, True
        self.server.mapdata, self.server.far_bld_pool = object(), object()
        hello = {"type": "hello", "protocol": 4, "format": 4, "modVersion": "test", "geoTiles": 17, "trafficVisuals": dt.PROTO}
        for field, missing in (("traffic", False), ("mapdata", None), ("far_bld_pool", None)):
            with self.subTest(field=field):
                saved = getattr(self.server, field)
                setattr(self.server, field, missing)
                try:
                    self.c.send(hello)
                    reply, _ = self.c.until(lambda m: m["type"] == "hello", 5)
                    self.assertNotIn("trafficVisuals", reply)
                    self.conn().dispatch({"type": "traffic_visuals", "enabled": True})
                    self.assertFalse(self.conn().traffic_on)
                finally:
                    setattr(self.server, field, saved)


class StreamingTest(unittest.IsolatedAsyncioTestCase):
    async def test_far_stream_skips_inner_tiles(self):
        import geotiles
        import vegetation
        frame = geotiles.frame(10000, 6000, dt.Z)
        tr = vegetation.Tracker(dt.FAR_SEND_M, dt.FAR_DROP_M, 1, "traffic_far",
                                inner=(dt.FAR_SKIP_M, 1), geo=True, z=dt.Z)
        conn, built = Mock(), []

        def build(x, y):
            built.append((x, y))
            return None

        tr.on_pose(conn, 1, 0, 0, asyncio.get_running_loop(), build, frame=frame)
        while tr.building:
            await asyncio.sleep(0.01)
        inner = {(x, y) for x, y, _ in geotiles.tiles_near(frame, 0, 0, dt.FAR_SKIP_M, dt.Z)}
        self.assertTrue(tr.car)
        self.assertFalse(tr.car & inner)
        self.assertTrue(built)
        self.assertFalse(set(built) & inner)
        tr.close()

    async def test_geo_stream_keeps_routes_across_rebases_and_drops(self):
        import geotiles
        import vegetation
        frame = geotiles.frame(20000, 12000, dt.Z)
        tr = vegetation.Tracker(1, 200, 1, "traffic", geo=True, z=dt.Z)
        messages = []
        conn = Mock()
        conn.send = messages.append
        with tempfile.TemporaryDirectory() as folder:
            conn.s.out_dir = folder
            conn.s.pool_shapes = None

            def build(x, y):
                data = {"v": dt.PROTO, "key": geotiles.key(x, y, dt.Z), "frame": list(geotiles.centre(x, y, dt.Z)),
                        "pivot": 0, "paths": []}
                return dt.write(folder, "/temp/traffic", data)

            loop = asyncio.get_running_loop()
            tr.on_pose(conn, 1, 0, 0, loop, build, frame=frame)
            for _ in range(100):
                if messages:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(messages), 1)
            tile = messages[0]
            self.assertEqual(tile["type"], "traffic_tile")
            self.assertEqual(tile["data"]["v"], dt.PROTO)
            tr.on_pose(conn, 2, 0, 0, loop, build, frame=frame)
            self.assertEqual(len(messages), 1, "a rebase reuses the sent tile")
            tr.on_pose(conn, 2, 10000, 0, loop, build, frame=frame)
            self.assertTrue(any(m["type"] == "traffic_drop" and m["key"] == tile["key"] for m in messages))
            while tr.building:
                await asyncio.sleep(0.01)
            tr.close()


if __name__ == "__main__":
    unittest.main()
