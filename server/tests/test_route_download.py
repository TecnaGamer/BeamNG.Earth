"""routeahead.py (pure corridor point/key generation) and earthd.Connection.route_download (the fetch loop
and its progress pushes, superseded by a newer route())."""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import routeahead  # noqa: E402


class ArcLengthPointsTest(unittest.TestCase):
    def test_spacing_and_order(self):
        # a straight 2 km line north: points every ROUTE_SAMPLE_M, nearest first
        pts = routeahead.arc_length_points([[36.0, -115.0], [36.018, -115.0]], max_m=2000.0, step_m=250.0)
        self.assertEqual(pts[0], (36.0, -115.0))
        lats = [p[0] for p in pts]
        self.assertEqual(lats, sorted(lats))
        self.assertGreater(len(pts), 5)

    def test_capped_at_max_m(self):
        pts = routeahead.arc_length_points([[36.0, -115.0], [36.1, -115.0]], max_m=1000.0, step_m=250.0)
        lat_m = (pts[-1][0] - 36.0) * 111320.0
        self.assertLessEqual(lat_m, 1000.0 + 1.0)

    def test_short_route(self):
        self.assertEqual(routeahead.arc_length_points([[1.0, 2.0]], 5000.0), [(1.0, 2.0)])
        self.assertEqual(routeahead.arc_length_points([], 5000.0), [])


class FakeDem:
    def __init__(self, cached=()):
        self.cached = set(cached)
        self.fetched = []

    def keys_around(self, lat, lon, radius, zoom):
        return [(zoom, round(lat, 3), round(lon, 3))]

    def has(self, *k):
        return k in self.cached

    def prefetch(self, keys):
        got = 0
        for k in keys:
            if k not in self.cached:
                self.cached.add(k)
                self.fetched.append(k)
                got += 1
        return got, False


class FakeMapdata:
    def __init__(self):
        self.cached = set()
        self.fetched = []

    def has(self, key, layer):
        return (key, layer) in self.cached

    def cell(self, key, layers, kind):
        for layer in layers:
            self.cached.add((key, layer))
            self.fetched.append((key, layer))


class NeedAndDoneCountTest(unittest.TestCase):
    def test_need_dedupes_and_done_count_is_cheap(self):
        dem, mapdata = FakeDem(), FakeMapdata()
        pts = [(36.0, -115.0), (36.001, -115.0)]       # close enough to share DEM keys
        dem_keys, cell_layer_pairs = routeahead.need(dem, mapdata, pts, reach_m=300.0, zoom=15, layers=("roads",))
        self.assertTrue(dem_keys)
        self.assertTrue(cell_layer_pairs)
        self.assertEqual(routeahead.done_count(dem, mapdata, dem_keys, cell_layer_pairs), 0)
        dem.cached.add(dem_keys[0])
        self.assertEqual(routeahead.done_count(dem, mapdata, dem_keys, cell_layer_pairs), 1)

    def test_no_layers_means_no_cells(self):
        dem, mapdata = FakeDem(), FakeMapdata()
        _, cell_layer_pairs = routeahead.need(dem, mapdata, [(36.0, -115.0)], 300.0, 15, layers=())
        self.assertEqual(cell_layer_pairs, [])


def connection(dem=None, mapdata=None, buildings=True, aid=1, dl_version=1):
    sent = []
    s = types.SimpleNamespace(dem=dem, mapdata=mapdata, buildings=buildings,
                              layout=types.SimpleNamespace(block=1024),
                              anchors={aid: types.SimpleNamespace(hm_zoom=15)},
                              route_pool=types.SimpleNamespace(submit=lambda fn: fn()))
    c = types.SimpleNamespace(s=s, active=aid, route_dl_version=dl_version,
                              loop=types.SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a)),
                              send=lambda msg: sent.append(msg))
    return c, sent


ROUTE = [[36.0, -115.0], [36.02, -115.0]]       # ~2.2 km north


class RouteDownloadTest(unittest.TestCase):
    def test_too_short_route_sends_inactive_empty(self):
        c, sent = connection(FakeDem(), FakeMapdata())
        earthd.Connection.route_download(c, 1, [[36.0, -115.0]])
        self.assertEqual(sent, [{"type": "route_download", "total": 0, "done": 0, "active": False}])

    def test_fetches_and_reports_completion(self):
        c, sent = connection(FakeDem(), FakeMapdata())
        earthd.Connection.route_download(c, 1, ROUTE)
        self.assertTrue(sent)
        self.assertFalse(sent[-1]["active"])
        self.assertEqual(sent[-1]["done"], sent[-1]["total"])
        self.assertGreater(sent[-1]["total"], 0)

    def test_already_cached_route_is_instantly_complete(self):
        dem, mapdata = FakeDem(), FakeMapdata()
        # a dry run first, to know every key this route needs, then mark them all cached
        c, _ = connection(dem, mapdata)
        pts = routeahead.arc_length_points(ROUTE, earthd.ROUTE_AHEAD_MAX_M)
        reach = 1024 * 2.0 / 2 + earthd.AHEAD_MARGIN_M
        dem_keys, cell_layer_pairs = routeahead.need(dem, mapdata, pts, reach, 15, earthd.AHEAD_LAYERS)
        dem.cached.update(dem_keys)
        mapdata.cached.update(cell_layer_pairs)
        c, sent = connection(dem, mapdata)
        earthd.Connection.route_download(c, 1, ROUTE)
        self.assertEqual(len(sent), 1)          # one push: already done == total from the start
        self.assertEqual(sent[0]["done"], sent[0]["total"])
        self.assertFalse(sent[0]["active"])
        self.assertEqual(dem.fetched, [])
        self.assertEqual(mapdata.fetched, [])

    def test_modes(self):
        # the GPS setting "Download along route": off sends the empty bar; the whole route reaches past ROUTE_AHEAD_MAX_M
        c, sent = connection(FakeDem(), FakeMapdata())
        earthd.Connection.route_download(c, 1, ROUTE, "off")
        self.assertEqual(sent, [{"type": "route_download", "total": 0, "done": 0, "active": False}])
        long_route = [[36.0, -115.0], [36.3, -115.0]]          # ~33 km north
        totals = {}
        for mode in ("ahead", "route"):
            c, sent = connection(FakeDem(), FakeMapdata())
            earthd.Connection.route_download(c, 1, long_route, mode)
            totals[mode] = sent[-1]["total"]
        self.assertGreater(totals["route"], totals["ahead"])

    def test_superseded_job_stops_early(self):
        conn_box = {}

        class StoppingDem(FakeDem):
            def prefetch(self, keys):
                conn_box["c"].route_dl_version = 2       # a new route() arrives mid-corridor
                return super().prefetch(keys)
        dem, mapdata = StoppingDem(), FakeMapdata()
        c, sent = connection(dem, mapdata, dl_version=1)
        conn_box["c"] = c
        earthd.Connection.route_download(c, 1, ROUTE)
        # the version bump during the first DEM fetch must stop the job before any map cell is fetched
        self.assertLessEqual(len(dem.fetched), 1)
        self.assertEqual(mapdata.fetched, [])
        self.assertTrue(sent)
        self.assertNotEqual(sent[-1].get("done"), sent[-1].get("total"))

    def test_no_buildings_roads_only(self):
        mapdata = FakeMapdata()
        c, sent = connection(FakeDem(), mapdata, buildings=False)
        earthd.Connection.route_download(c, 1, ROUTE)
        self.assertTrue(mapdata.fetched)
        for key, layer in mapdata.fetched:
            self.assertEqual(layer, "roads")


if __name__ == "__main__":
    unittest.main()
