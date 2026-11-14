import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import geodesy  # noqa: E402
import mapcells  # noqa: E402
import roads  # noqa: E402


class OsmFuelTilesTest(unittest.TestCase):
    """(the OSM test, 2026-10-28) a cell's OpenStreetMap fuel stations fetched after its road tiles were sent: those
    tiles (the ones whose stations reach the cell) are built again; the others stay."""

    def test_tiles_of_cell(self):
        import earthd
        frame = geodesy.EnuFrame(40.44, -80.0)
        key = next(iter(mapcells.cells_for_bbox(40.44, -80.0, 40.4401, -79.9999)))
        near = earthd.osm_fuel_tiles(frame, key, {(0, 0), (-1, -1), (40, 40)})
        self.assertIn((0, 0), near)
        self.assertNotIn((40, 40), near, "10 km off")

    def test_tracker_again(self):
        t = roads.Tracker()
        t.sent = {(0, 0), (1, 0), (5, 5)}
        t.again({(0, 0), (9, 9)})
        self.assertEqual(t.sent, {(1, 0), (5, 5)}, "built again at the next pose (no roads_drop: the mod replaces it)")

    def test_geotiles_of_cell(self):
        # (dev's road geotiles: a geotile's cells are its stations' reach round its own box)
        import earthd
        import geotiles
        frame = geodesy.EnuFrame(40.44, -80.0)
        key = next(iter(mapcells.cells_for_bbox(40.44, -80.0, 40.4401, -79.9999)))
        here = {(a, b) for a, b, _ in geotiles.tiles_near(frame, 0.0, 0.0, 1.0)}
        far = {(a, b) for a, b, _ in geotiles.tiles_near(frame, 20000.0, 20000.0, 1.0)}
        near = earthd.osm_fuel_tiles(frame, key, here | far, geo=True)
        self.assertTrue(here <= near)
        self.assertFalse(far & near, "20 km off")

    def test_geo_tracker_again(self):
        # a sent geotile given again: its content and this anchor's skin built at the next pose (no roads_drop: the
        # mod replaces a geotile whose content hash changed)
        import geotiles
        frame = geodesy.EnuFrame(40.44, -80.0)
        jobs = []

        class Loop:
            def run_in_executor(self, pool, fn, *args):
                jobs.append(args)

                class F:
                    def add_done_callback(self, cb):
                        pass
                return F()
        t = roads.GeoTracker()
        t.frame, t.aid = frame, 1
        t.frames[1] = frame
        car = set(roads.geo_wanted(frame, 0.0, 0.0, 0.0, 0.0))
        t.sent, t.skins = set(car), set(car)
        for k in car:
            t.need[(k, 1)] = {}
        k = sorted(car)[0]
        t.again({k, (10 ** 6, 10 ** 6)})
        self.assertNotIn(k, t.sent)
        t.on_pose(None, 1, 0.0, 0.0, Loop(), None, frame=frame)
        t.close()
        self.assertEqual([(a[1], a[2], a[3], a[4]) for a in jobs], [(k[0], k[1], True, True)])
        self.assertTrue(all(isinstance(g, int) for g in k) and geotiles.key(*k))


    def test_index_read_retried(self):
        # (in game, 2026-10-29: the startup index read refused twice: "busy, prefetch dropped", "bucket low") tried
        # again every OSM_FUEL_WARM_WAIT_S, up to OSM_FUEL_WARM_TRIES times
        import earthd
        calls = []

        class MD:
            def osm_fuel_warm(self):
                calls.append(1)
                if len(calls) < 3:
                    raise IOError("busy, prefetch dropped")

        class S:
            mapdata = MD()
        slept = []
        earthd.Server._osm_fuel_warm(S(), sleep=slept.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept, [earthd.OSM_FUEL_WARM_WAIT_S] * 2)


if __name__ == "__main__":
    unittest.main()
