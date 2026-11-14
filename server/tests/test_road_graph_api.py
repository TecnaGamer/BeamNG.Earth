"""earthd's 'road_graph': the modding API's one road-graph window at a point an add-on names (api.roadGraph in the
mod), and Server.road_graph_data behind it.

The build reads only the map cells already on disk (cached_only) and fetches nothing, not the roads layers and not the
infrastructure or road_routes layers the player's graph reads through cell(kind="demand"); it says how many cells of
the window were left unread, and builds the window it was asked for rather than navgraph.WINDOW_M.
The request clamps its radius to ROAD_GRAPH_RADIUS_MAX_M, defaults to navgraph.WINDOW_M, answers with an error rather
than throwing when it cannot be served (bad coordinates, no map data, a server started without roads, no anchor yet),
writes under its own name (roadgraph_<aid>_<serial>.json, never the player's navgraph_<aid>_*.json), and sweeps the
anchor's older road-graph files down to ROAD_GRAPH_KEEP while leaving the player's graph where it is.
"""
import asyncio
import os
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mapcells  # noqa: E402
import navgraph  # noqa: E402
from test_roads import FRAME, LAT, LON, features, rules  # noqa: E402

AID = 3


def roads_cell(lines, classes=None):
    """A roads layer of straight lines, each [(east m, north m), ...] round (LAT, LON) (test_roads.features)."""
    f = features(lines, classes or ["residential"] * len(lines))
    for name in ("connectors", "oneway", "max_speed_kmh"):
        f.rules.setdefault(name, rules([], "float" if name != "connectors" else "text"))
    return f


FETCHING_KINDS = ("demand", "urgent")       # mapdata: every other kind raises instead of downloading


class FakeMapData:
    """The map cells on disk, by layer. has() says whether a layer's file is there; cell() and cells() are the read
    the player's own graph uses, recorded here with the kind they were asked at — which is what a test checks, since
    a cached-only build reads through the same call and it is the *kind* that makes it unable to download. A cell
    that is not on disk raises at a non-fetching kind, as mapdata does."""

    def __init__(self, layers=None, on_disk=()):
        self.layers = layers or {}
        self.on_disk = set(on_disk)
        self.reads = []
        self.unkept = []

    def has(self, key, layer):
        return (str(key), layer) in self.on_disk

    def layer_unkept(self, key, layer, kind="background"):
        self.unkept.append((str(key), layer))
        return self.layers.get(layer, {}).get(str(key))

    @property
    def fetched(self):
        """Only the reads that could have downloaded something."""
        return [r for r in self.reads if r[2] in FETCHING_KINDS]

    def cell(self, key, layers=(), kind="demand"):
        self.reads.append((str(key), tuple(layers), kind))
        for lr in layers:
            if (str(key), lr) not in self.on_disk and kind not in FETCHING_KINDS:
                raise RuntimeError("deferred: %s of %s is not on disk" % (lr, key))
        return types.SimpleNamespace(layers={lr: self.layers.get(lr, {}).get(str(key)) for lr in layers})

    def cells(self, keys, layers=(), kind="demand"):
        return [self.cell(k, layers, kind) for k in keys]


def fake_anchor(aid=AID):
    return types.SimpleNamespace(id=aid, lat=LAT, lon=LON, frame=FRAME, zooms=(14,), bridges=None, tunnels=None)


class NoChain:
    """Stands in for Server.chain_view's bridge / tunnel profiles: no bridge parts, nothing else answered (the graph's
    tunnel read is wrapped, as in the server)."""

    def parts(self, feats, box):
        return []

    def open_parts(self, feats, box):
        return []

    def depth_of(self, fid):
        return None


def fake_server(md, out_dir, aid=AID, traffic=True, anchors=None):
    """A Server stand-in with what road_graph_data and Connection.road_graph read of it."""
    s = types.SimpleNamespace(
        anchors={aid: fake_anchor(aid)} if anchors is None else anchors,
        mapdata=md, surface=None, road_widths=None, dej_on="off", airports_db=None,
        grader_lock=threading.Lock(), out_dir=out_dir, vfs_dir="/temp/beamngEarth/test", traffic=traffic,
        road_pool=None, dem=types.SimpleNamespace(sample=lambda la, lo, zoom: np.zeros(np.shape(la))))
    s.grader_for = lambda anchor: None
    s.ground = lambda frame, la, lo, z: z
    s.chain_view = lambda *a, **k: NoChain()
    s.country = lambda anchor: "US"
    s.left_hand = lambda anchor: False
    s.mph = lambda anchor: True
    return s


class RoadGraphDataTest(unittest.TestCase):
    """Server.road_graph_data(cached_only=True): the window's cells on disk and nothing else."""

    def setUp(self):
        self.us = mock.patch.object(navgraph, "country_at", lambda *a, **k: "US")
        self.us.start()
        self.addCleanup(self.us.stop)

    def keys(self, window):
        w = window + 200.0
        la, lo, _ = FRAME.enu_to_geodetic(np.array([-w, w]), np.array([-w, w]), 0.0)
        return [str(k) for k in mapcells.cells_for_bbox(float(la.min()), float(lo.min()),
                                                        float(la.max()), float(lo.max()))]

    def test_reads_the_cells_on_disk_and_counts_the_rest(self):
        import earthd
        keys = self.keys(300.0)
        road = roads_cell([[(-200.0, 0.0), (200.0, 0.0)]])
        md = FakeMapData({"roads": {keys[0]: road}}, on_disk=[(keys[0], "roads")])
        graph, not_read = earthd.Server.road_graph_data(fake_server(md, "."), AID, 0.0, 0.0,
                                                        window=300.0, cached_only=True)
        self.assertEqual(not_read, len(keys) - 1)
        self.assertGreater(len(graph["nodes"]), 0, "the road of the cell on disk is in the graph")
        self.assertEqual(md.fetched, [], "nothing fetched for it")
        self.assertEqual([k for k, ls, _ in md.reads if "roads" in ls], [keys[0]],
                         "the cell on disk read once, the others not reached for at all")
        self.assertTrue(all(kind not in FETCHING_KINDS for _, _, kind in md.reads),
                        "read at a kind that cannot download: %r" % (md.reads,))

    def test_every_cell_on_disk_is_complete(self):
        import earthd
        keys = self.keys(300.0)
        road = roads_cell([[(-200.0, 0.0), (200.0, 0.0)]])
        md = FakeMapData({"roads": {k: (road if k == keys[0] else roads_cell([])) for k in keys}},
                         on_disk=[(k, "roads") for k in keys])
        _graph, not_read = earthd.Server.road_graph_data(fake_server(md, "."), AID, 0.0, 0.0,
                                                         window=300.0, cached_only=True)
        self.assertEqual(not_read, 0)
        self.assertEqual(md.fetched, [])
        self.assertTrue(md.reads and all(kind not in FETCHING_KINDS for _, _, kind in md.reads),
                        "every cell read, none of them at a fetching kind")

    def test_no_layer_of_the_window_is_ever_fetched(self):
        """signals_for, secrossings.read and blockworker._de_routes all read their cells through
        cell(kind="demand"), which downloads: a cached-only build must take those layers from disk too, or the
        request's "nothing is downloaded" would be false for every layer but the roads."""
        import earthd
        keys = self.keys(300.0)
        md = FakeMapData({"roads": {keys[0]: roads_cell([[(-200.0, 0.0), (200.0, 0.0)]])}},
                         on_disk=[(keys[0], "roads"), (keys[0], "infrastructure"), (keys[0], "road_routes")])
        earthd.Server.road_graph_data(fake_server(md, "."), AID, 0.0, 0.0, window=300.0, cached_only=True)
        self.assertEqual(md.fetched, [], "no read at a kind that could download, for any layer")
        # the mapped signals still come from the cell that has them: read, but never at a fetching kind
        infra = [(k, kind) for k, ls, kind in md.reads if "infrastructure" in ls]
        self.assertIn(keys[0], [k for k, _ in infra], "the infrastructure layer of the cell that has it was read")
        self.assertTrue(infra and all(kind not in FETCHING_KINDS for _, kind in infra),
                        "and not at a kind that downloads: %r" % (infra,))

    def test_the_window_asked_for_is_the_window_built(self):
        import earthd
        keys = self.keys(1500.0)
        far = [[(-1400.0, 900.0), (1400.0, 900.0)]]      # (outside a 300 m window, inside a 1500 m one)
        md = FakeMapData({"roads": {k: roads_cell(far) for k in keys}},
                         on_disk=[(k, "roads") for k in keys])
        s = fake_server(md, ".")
        small, _ = earthd.Server.road_graph_data(s, AID, 0.0, 0.0, window=300.0, cached_only=True)
        big, _ = earthd.Server.road_graph_data(s, AID, 0.0, 0.0, window=1500.0, cached_only=True)
        self.assertEqual(len(small["nodes"]), 0, "clipped to the window asked for")
        self.assertGreater(len(big["nodes"]), 0)

    def test_none_when_the_anchor_or_the_map_data_went(self):
        import earthd
        md = FakeMapData({})
        self.assertIsNone(earthd.Server.road_graph_data(fake_server(md, "."), 99, 0.0, 0.0, cached_only=True))
        self.assertIsNone(earthd.Server.road_graph_data(fake_server(None, "."), AID, 0.0, 0.0, cached_only=True))


class EarthdRoadGraphTest(unittest.TestCase):
    """Connection.road_graph: the request's checks, the answer, the file it writes and the files it sweeps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def graph(self, nodes=1):
        return {"nodes": {f"n{i}": [float(i), 0.0, 0.0, 3.0] for i in range(nodes)},
                "edges": [["n0", f"n{nodes - 1}", 1, 1, 0, 13.4, 1.0, 3.0]] if nodes > 1 else [],
                "lines": [], "rules": {"mph": True}}

    def run_road_graph(self, msgs, s, active=AID, data=None, not_read=0):
        """Connection.road_graph over a Server stand-in: returns (what was sent, the connection, the calls made to
        road_graph_data as (aid, x, y, window, cached_only))."""
        import earthd
        sent, calls = [], []

        def road_graph_data(aid, x, y, window=None, cached_only=False):
            calls.append((aid, x, y, window, cached_only))
            if data is None:
                return None
            return data, not_read
        s.road_graph_data = road_graph_data

        async def run():
            pool = ThreadPoolExecutor(1)
            s.api_pool = pool
            conn = types.SimpleNamespace(s=s, send=sent.append, active=active)
            conn._sweep_road_graphs = lambda *a: earthd.Connection._sweep_road_graphs(conn, *a)
            try:
                for m in msgs:
                    await earthd.Connection.road_graph(conn, m)
            finally:
                pool.shutdown()
            return conn
        return sent, asyncio.run(run()), calls

    def ask(self, **kw):
        return dict({"id": 1, "lat": LAT, "lon": LON}, **kw)

    def test_radius_clamped_and_defaulted(self):
        import earthd
        s = fake_server(FakeMapData({}), self.tmp.name)
        sent, _conn, calls = self.run_road_graph([self.ask(radius=1e6), self.ask(id=2), self.ask(id=3, radius=250)],
                                                 s, data=self.graph(2))
        self.assertEqual([c[3] for c in calls], [earthd.ROAD_GRAPH_RADIUS_MAX_M, navgraph.WINDOW_M, 250.0])
        self.assertTrue(all(c[4] is True for c in calls), "cached-only is its only mode")
        self.assertEqual([m["complete"] for m in sent], [True, True, True])
        self.assertEqual([m["anchorId"] for m in sent], [AID, AID, AID])
        self.assertEqual([m["serial"] for m in sent], [1, 2, 3])

    def test_the_point_is_converted_to_the_anchor_frame(self):
        s = fake_server(FakeMapData({}), self.tmp.name)
        _sent, _conn, calls = self.run_road_graph([self.ask(lat=LAT + 0.01, lon=LON)], s, data=self.graph())
        self.assertAlmostEqual(calls[0][1], 0.0, delta=2.0)                 # (0.01 deg north: east stays put)
        self.assertGreater(calls[0][2], 1000.0)
        self.assertLess(calls[0][2], 1200.0)

    def test_says_why_rather_than_throwing(self):
        s = fake_server(FakeMapData({}), self.tmp.name)
        sent, _conn, calls = self.run_road_graph(
            [self.ask(lat=95.0), self.ask(id=2, lat="north"), self.ask(id=3, radius=float("inf"))], s,
            data=self.graph())
        self.assertEqual([m["type"] for m in sent], ["road_graph_result"] * 3)
        self.assertEqual([m["error"] for m in sent], ["bad coordinates"] * 3)
        self.assertEqual([m["complete"] for m in sent], [False] * 3)
        self.assertTrue(all("path" not in m for m in sent))
        self.assertEqual(calls, [], "nothing was built for any of them")

    def test_says_why_with_no_map_data_no_roads_and_no_anchor(self):
        no_md = fake_server(None, self.tmp.name)
        sent, _conn, calls = self.run_road_graph([self.ask()], no_md, data=self.graph())
        self.assertIn("no map data", sent[0]["error"])
        no_roads = fake_server(FakeMapData({}), self.tmp.name, traffic=False)
        sent, _conn, _ = self.run_road_graph([self.ask()], no_roads, data=self.graph())
        self.assertIn("without roads", sent[0]["error"])
        s = fake_server(FakeMapData({}), self.tmp.name)
        sent, _conn, _ = self.run_road_graph([self.ask()], s, active=None, data=self.graph())
        self.assertIn("no anchor", sent[0]["error"])
        self.assertEqual(calls, [])

    def test_a_request_without_an_id_is_not_answered(self):
        s = fake_server(FakeMapData({}), self.tmp.name)
        sent, _conn, calls = self.run_road_graph(
            [{"lat": LAT, "lon": LON}, {"id": "one", "lat": LAT, "lon": LON}, {"id": True, "lat": LAT, "lon": LON},
             {"id": 1.5, "lat": LAT, "lon": LON}, {"id": float("nan"), "lat": LAT, "lon": LON}],
            s, data=self.graph())
        self.assertEqual(sent, [], "no id to answer to")
        self.assertEqual(calls, [])

    def test_an_id_the_game_wrote_as_a_float_is_answered_as_an_int(self):
        """The mod counts its requests in Lua, whose numbers are doubles: jsonEncode may write 4 or 4.0, and the
        answer's id is the int the protocol table declares either way (the mod looks its request up by it)."""
        s = fake_server(FakeMapData({}), self.tmp.name)
        sent, _conn, _ = self.run_road_graph([self.ask(id=4.0)], s, data=self.graph())
        self.assertEqual(sent[0]["id"], 4)
        self.assertIsInstance(sent[0]["id"], int)

    def test_incomplete_when_cells_were_missing(self):
        s = fake_server(FakeMapData({}), self.tmp.name)
        sent, _conn, _ = self.run_road_graph([self.ask()], s, data=self.graph(), not_read=4)
        self.assertFalse(sent[0]["complete"])
        self.assertIn("roadgraph_", sent[0]["path"])
        self.assertNotIn("error", sent[0])

    def test_the_graph_is_written_under_its_own_name_and_the_players_survives(self):
        import earthd
        mine = os.path.join(self.tmp.name, f"navgraph_{AID}_7.json")
        with open(mine, "w") as f:
            f.write('{"nodes":{}}')
        s = fake_server(FakeMapData({}), self.tmp.name)
        sent, _conn, _ = self.run_road_graph([self.ask(id=i) for i in range(1, 6)], s, data=self.graph(2))
        self.assertTrue(os.path.exists(mine), "the player's live graph is never swept by a road-graph request")
        names = sorted(n for n in os.listdir(self.tmp.name) if n.startswith("roadgraph_"))
        self.assertEqual(names, [f"roadgraph_{AID}_4.json", f"roadgraph_{AID}_5.json"])
        self.assertEqual(len(names), earthd.ROAD_GRAPH_KEEP)
        self.assertEqual(sent[-1]["path"], f"/temp/beamngEarth/test/roadgraph_{AID}_5.json")
        self.assertEqual([m["serial"] for m in sent], [1, 2, 3, 4, 5])

    def test_another_anchors_road_graphs_are_left_alone(self):
        other = os.path.join(self.tmp.name, "roadgraph_9_1.json")
        with open(other, "w") as f:
            f.write('{"nodes":{}}')
        s = fake_server(FakeMapData({}), self.tmp.name)
        self.run_road_graph([self.ask(id=i) for i in range(1, 6)], s, data=self.graph())
        self.assertTrue(os.path.exists(other))

    def test_the_file_holds_the_graph_the_add_on_reads(self):
        import json
        s = fake_server(FakeMapData({}), self.tmp.name)
        sent, _conn, _ = self.run_road_graph([self.ask()], s, data=self.graph(2))
        with open(os.path.join(self.tmp.name, f"roadgraph_{AID}_1.json")) as f:
            got = json.load(f)
        self.assertEqual(len(got["nodes"]), 2)
        self.assertEqual(len(got["edges"][0]), 8)           # (in, out, oneway, lanesOut, lanesIn, speed, driv, radius)
        self.assertIn(got["edges"][0][0], got["nodes"])     # (short_ids: the edges name the nodes' keys)

    def test_the_world_moving_on_is_an_error_not_a_crash(self):
        s = fake_server(FakeMapData({}), self.tmp.name)
        sent, _conn, _ = self.run_road_graph([self.ask()], s, data=None)
        self.assertFalse(sent[0]["complete"])
        self.assertIn("world moved on", sent[0]["error"])
        self.assertNotIn("path", sent[0])


if __name__ == "__main__":
    unittest.main()
