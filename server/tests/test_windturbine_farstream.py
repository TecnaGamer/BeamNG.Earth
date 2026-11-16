"""Existing far-content tracker serves mapped turbines with bounded terrain work and hysteresis."""
from concurrent.futures import Future
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd
import geodesy
import geotiles
import vegetation
import windturbines

LAT, LON = 41.1397814, -104.9978882


class TurbineFarBuildTest(unittest.TestCase):
    def server(self, directory, height=1800.0):
        sample = mock.Mock(side_effect=lambda la, lo, zoom: np.full(np.shape(la), height))
        return SimpleNamespace(out_dir=directory, vfs_dir="/isolated", sid="isolated-session", dem=SimpleNamespace(sample=sample),
                               ground=mock.Mock(side_effect=lambda frame, la, lo, z: z))

    def test_empty_cell_never_samples_dem(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(windturbines, "near", return_value=()):
            server = self.server(directory)
            gx, gy = geotiles.tile_of(LAT, LON, earthd.TURBINE_Z)
            got = earthd.Server.build_turbines_geo(server, gx, gy)
            server.dem.sample.assert_not_called()
            server.ground.assert_not_called()
            self.assertEqual(got[1], 0)
            data = json.loads((Path(directory) / Path(got[0]).name).read_text())
            self.assertEqual(data["turbines"], [])
            self.assertEqual(data["sid"], "isolated-session")

    def test_populated_cell_batches_exact_points_and_memoizes(self):
        gx, gy = geotiles.tile_of(LAT, LON, earthd.TURBINE_Z)
        clat, clon = geotiles.centre(gx, gy, earthd.TURBINE_Z)
        features = tuple(windturbines.Turbine("osm:node:" + str(i), clat + i * 1e-5, clon, {}, 1)
                         for i in (1, 2))
        outside = windturbines.Turbine("osm:node:3", clat + 1, clon, {}, 1)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(windturbines, "near",
                                                                          return_value=features + (outside,)):
            server = self.server(directory)
            got = earthd.Server.build_turbines_geo(server, gx, gy)
            self.assertEqual(got[1], 2)
            self.assertEqual(server.dem.sample.call_count, 1)
            sampled_lat, sampled_lon, _ = server.dem.sample.call_args.args
            np.testing.assert_array_equal(sampled_lat, [f.lat for f in features])
            np.testing.assert_array_equal(sampled_lon, [f.lon for f in features])
            data = json.loads((Path(directory) / Path(got[0]).name).read_text())
            self.assertEqual([r["id"] for r in data["turbines"]], [f.id for f in features])
            self.assertTrue(all(r["collide"] is False for r in data["turbines"]))
            self.assertEqual(earthd.Server.build_turbines_geo(server, gx, gy), got)
            self.assertEqual(server.dem.sample.call_count, 1, "a resend keeps its sampled installation heights")

    def test_missing_height_is_retried_and_not_written(self):
        gx, gy = geotiles.tile_of(LAT, LON, earthd.TURBINE_Z)
        feature = windturbines.Turbine("osm:node:1", LAT, LON, {}, 1)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(windturbines, "near",
                                                                          return_value=(feature,)):
            server = self.server(directory, np.nan)
            self.assertIsNone(earthd.Server.build_turbines_geo(server, gx, gy))
            self.assertEqual(list(Path(directory).iterdir()), [])
            server.dem.sample.side_effect = lambda la, lo, zoom: np.full(np.shape(la), 1800.0)
            self.assertEqual(earthd.Server.build_turbines_geo(server, gx, gy)[1], 1)


class TurbineFarTrackerTest(unittest.TestCase):
    def test_existing_tracker_bounds_work_and_keeps_hysteresis_across_rebase(self):
        tracker = vegetation.Tracker(earthd.TURBINE_SEND_M, earthd.TURBINE_DROP_M, earthd.TURBINE_PER_POSE,
                                     "turbines", geo=True, z=earthd.TURBINE_Z)
        futures, messages = [], []

        class Loop:
            def run_in_executor(self, pool, fn, *args):
                f = Future()
                futures.append((f, args))
                return f

        conn = SimpleNamespace(send=messages.append)
        frame = geodesy.EnuFrame(LAT, LON)
        target = geotiles.tile_of(LAT, LON, earthd.TURBINE_Z)
        loop = Loop()
        build = lambda gx, gy: None
        try:
            tracker.on_pose(conn, 1, 0, 0, loop, build, frame=frame)
            self.assertEqual(len(futures), 2, "terrain batches use the existing two-job limit")
            tracker.on_pose(conn, 1, 0, 0, loop, build, frame=frame)
            self.assertEqual(len(futures), 2, "another pose cannot queue extra work while both jobs are pending")
            for future, (gx, gy) in futures[:]:
                future.set_result(("/isolated/turbines.json", 0, geotiles.centre(gx, gy, earthd.TURBINE_Z), 0))
            self.assertTrue(all(m["type"] == "turbines_tile" and m["key"].startswith("g15_") for m in messages))
            tracker.sent.add(target)
            quad = geotiles.quad(frame, *target, earthd.TURBINE_Z)
            x = float(quad[:, 0].max())
            y = float(quad[:, 1].mean())
            tracker.on_pose(conn, 1, x + 4500, y, loop, build, frame=frame)
            self.assertIn(target, tracker.sent, "outside send reach but inside drop reach: kept")
            tracker.on_pose(conn, 1, x + 5501, y, loop, build, frame=frame)
            self.assertNotIn(target, tracker.sent)
            self.assertTrue(any(m["type"] == "turbines_drop" and m["key"] == geotiles.key(*target, earthd.TURBINE_Z)
                                for m in messages))
            tracker.sent.add(target)
            rebased = geodesy.EnuFrame(LAT, LON + 0.1)
            rx, ry, _ = rebased.geodetic_to_enu(LAT, LON, 0)
            tracker.on_pose(conn, 2, float(rx), float(ry), loop, build, frame=rebased)
            self.assertIn(target, tracker.sent, "a rebase keeps the same global tile identity")
        finally:
            tracker.close()


if __name__ == "__main__":
    unittest.main()
