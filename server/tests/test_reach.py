"""Streaming by time to reach along the road graph (reach.py, earthfast reach.rs; the retro optimization study's section
J): the graph of the cells' drivable roads, the tree a fast car can reach (ETA, chance, its most probable path, the
frontier where the data ends), the geotiles along it and their order, and what follows it in the trackers and the
server (roads.GeoTracker, earthd.ahead_points, Server.geo_unwanted / block_unwanted)."""
import math
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402

import fast  # noqa: E402
import reach  # noqa: E402

HAVE = reach.available()
BIG = [[-1e5, -1e5], [1e5, -1e5], [1e5, 1e5], [-1e5, 1e5]]     # a cell's outline covering every test road


def cell(net, key, roads, quad=BIG):
    """Add roads [(id, [(x, y), ...], rank, oneway, [(at, connector id), ...])] as one cell."""
    xy, start, end, rank, cf, ca, ci, of, ov = [], [], [], [], [], [], [], [], []
    for i, (fid, pts, rk, ow, conns) in enumerate(roads):
        start.append(len(xy))
        xy += [list(p) for p in pts]
        end.append(len(xy))
        rank.append(rk)
        for at, cid in conns:
            cf.append(i)
            ca.append(at)
            ci.append(cid)
        if ow:
            of.append(i)
            ov.append(float(ow))
    n = len(of)
    return net.add_cell(key, quad, [r[0] for r in roads], np.asarray(xy, np.float64).reshape(-1, 2),
                        np.asarray(start, np.int64), np.asarray(end, np.int64), np.asarray(rank, np.uint8),
                        np.asarray(cf, np.int32), np.asarray(ca, np.float32), ci, np.asarray(of, np.int32),
                        np.asarray(ov, np.float32), np.zeros(n, np.float32), np.ones(n, np.float32))


def chance_near(got, x, y, r=20.0):
    p = got["pts"]
    d = np.hypot(p[:, 0] - x, p[:, 1] - y)
    return float(p[d <= r, 3].max()) if (d <= r).any() else 0.0


def eta_near(got, x, y, r=20.0):
    p = got["pts"]
    d = np.hypot(p[:, 0] - x, p[:, 1] - y)
    return float(p[d <= r, 2].min()) if (d <= r).any() else math.inf


def fork(link_deg=20.0):
    """A motorway east along y = 0 with a ramp leaving at 1000 m, link_deg off it."""
    net = fast._ef.RoadReach()
    a = math.radians(link_deg)
    ramp = [(1000.0 + d * math.cos(a), -d * math.sin(a)) for d in (0.0, 200.0, 400.0, 800.0)]
    cell(net, "14/0/0", [
        ("m1", [(0.0, 0.0), (1000.0, 0.0)], 7, 1, [(0.0, "A"), (1.0, "B")]),
        ("m2", [(1000.0, 0.0), (6000.0, 0.0)], 7, 1, [(0.0, "B"), (1.0, "C")]),
        ("r1", ramp, 7 | reach.LINK_BIT, 1, [(0.0, "B"), (1.0, "D")]),
    ])
    return net


@unittest.skipUnless(HAVE, "earthfast >= reach.NEED")
class GraphTreeTest(unittest.TestCase):
    def test_eta_is_the_roads_length_at_the_cars_speed(self):
        net = fork()
        got = net.grow(100.0, 0.0, 50.0, 0.0, 60.0)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(eta_near(got, 2100.0, 0.0, 17.0), 40.0, delta=0.7)
        self.assertAlmostEqual(eta_near(got, 1100.0, 0.0, 17.0), 20.0, delta=0.7)
        self.assertLessEqual(float(got["pts"][:, 2].max()), 60.0 + 1e-6)
        self.assertAlmostEqual(float(got["pts"][:, 0].max()), 3100.0, delta=1.0, msg="the horizon: 60 s at 50 m/s")

    def test_a_ramp_is_likely_slow_unlikely_fast(self):
        slow = fork().grow(100.0, 0.0, 30.0, 0.0, 60.0)
        p_ramp = chance_near(slow, 1000.0 + 300.0 * math.cos(math.radians(20)), -300.0 * math.sin(math.radians(20)))
        self.assertGreater(p_ramp, 0.1, "at 67 mph an exit is taken now and then")
        self.assertLess(p_ramp, 0.5)
        fast_ = fork().grow(100.0, 0.0, 220.0, 0.0, 22.0)
        p_fast = chance_near(fast_, 1000.0 + 300.0 * math.cos(math.radians(20)), -300.0 * math.sin(math.radians(20)))
        self.assertLess(p_fast, 0.05, "at 500 mph the ramp's turn is next to impossible")
        self.assertGreater(chance_near(fast_, 3000.0, 0.0), 0.9)
        # (the most probable path stays on the motorway)
        self.assertLess(float(np.abs(fast_["mpp"][:, 1]).max()), 1.0)

    def test_one_way_and_no_road(self):
        net = fork()
        self.assertIsNone(net.grow(500.0, 0.0, -50.0, 0.0, 30.0), "against a one-way motorway: on no road")
        self.assertIsNone(net.grow(500.0, 200.0, 50.0, 0.0, 30.0), "200 m off any road")
        self.assertIsNone(net.grow(500.0, 0.0, 0.0, 50.0, 30.0), "across the road")

    def test_a_t_junction_splits_and_a_dead_end_stops(self):
        net = fast._ef.RoadReach()
        cell(net, "14/0/0", [
            ("s", [(0.0, 0.0), (500.0, 0.0)], 4, 0, [(0.0, "A"), (1.0, "T")]),
            ("l", [(500.0, 0.0), (500.0, 500.0)], 4, 0, [(0.0, "T"), (1.0, "L")]),
            ("r", [(500.0, 0.0), (500.0, -500.0)], 4, 0, [(0.0, "T"), (1.0, "R")]),
        ])
        got = net.grow(50.0, 0.0, 30.0, 0.0, 60.0)
        pl, pr = chance_near(got, 500.0, 300.0), chance_near(got, 500.0, -300.0)
        self.assertAlmostEqual(pl, 0.5, delta=0.05)
        self.assertAlmostEqual(pr, 0.5, delta=0.05)
        self.assertEqual(len(got["frontier"]), 0)
        self.assertLess(float(got["pts"][:, 2].max()), 60.0, "the roads end before the horizon")

    def test_the_frontier_goes_on_straight(self):
        net = fast._ef.RoadReach()
        quad = [[-100.0, -100.0], [1000.0, -100.0], [1000.0, 100.0], [-100.0, 100.0]]
        cell(net, "14/0/0", [("e", [(0.0, 0.0), (1500.0, 0.0)], 5, 0, [(0.0, "A"), (1.0, "B")])], quad)
        got = net.grow(0.0, 0.0, 50.0, 0.0, 60.0)
        self.assertEqual(len(got["frontier"]), 1)
        fx, fy, feta, fp = got["frontier"][0]
        self.assertAlmostEqual(fx, 1500.0, delta=1.0)
        self.assertAlmostEqual(feta, 30.0, delta=0.1)
        far = got["pts"][np.argmax(got["pts"][:, 2])]
        self.assertAlmostEqual(far[0], 3000.0, delta=40.0, msg="straight on to the horizon: 60 s at 50 m/s")
        self.assertTrue(net.is_loaded(500.0, 0.0))
        self.assertFalse(net.is_loaded(1500.0, 0.0))

    def test_a_root_on_the_next_piece_does_not_stop_the_road(self):
        # the car 5 m before a connector: the next piece is a candidate root too, of little chance; reached again from
        # the likely root it is walked again with that chance (the road beyond keeps it)
        net = fast._ef.RoadReach()
        cell(net, "14/0/0", [
            ("a", [(0.0, 0.0), (100.0, 0.0)], 5, 1, [(0.0, "A"), (1.0, "B")]),
            ("b", [(100.0, 0.0), (100.0 + 30.0 * math.cos(0.5), 30.0 * math.sin(0.5))], 5, 1,
             [(0.0, "B"), (1.0, "C")]),
            ("c", [(100.0 + 30.0 * math.cos(0.5), 30.0 * math.sin(0.5)), (2000.0, 30.0 * math.sin(0.5) + 1000.0)], 5, 1,
             [(0.0, "C"), (1.0, "D")]),
            ("x", [(100.0, 0.0), (900.0, -10.0)], 2, 1, [(0.0, "B"), (1.0, "E")]),
        ])
        got = net.grow(95.0, 0.5, 40.0, 0.0, 40.0)
        self.assertGreater(chance_near(got, 900.0, -10.0, 30.0) + chance_near(got, 1000.0, 500.0, 60.0), 0.5)

    def test_a_gps_route_takes_the_turn(self):
        net = fast._ef.RoadReach()
        cell(net, "14/0/0", [
            ("s", [(0.0, 0.0), (500.0, 0.0)], 4, 0, [(0.0, "A"), (1.0, "X")]),
            ("on", [(500.0, 0.0), (2000.0, 0.0)], 4, 0, [(0.0, "X"), (1.0, "E")]),
            ("left", [(500.0, 0.0), (500.0, 1500.0)], 4, 0, [(0.0, "X"), (1.0, "N")]),
        ])
        free = net.grow(50.0, 0.0, 60.0, 0.0, 30.0)
        self.assertGreater(chance_near(free, 1200.0, 0.0), 0.9, "at 134 mph straight on")
        route = np.array([[0.0, 0.0], [500.0, 0.0], [500.0, 1500.0]])
        got = net.grow(50.0, 0.0, 60.0, 0.0, 30.0, route=route)
        self.assertGreaterEqual(chance_near(got, 500.0, 800.0), 0.9 - 1e-9, "the route turns left (route_share)")
        self.assertAlmostEqual(eta_near(got, 500.0, 800.0, 5.0), (450.0 + 800.0) / 60.0, delta=0.6,
                               msg="no time added for the turn: the ETA is the road's length")
        self.assertGreater(float(got["mpp"][-1, 1]), 500.0, "the most probable path follows the route")

    def test_cells_share_roads_until_the_last_goes(self):
        net = fast._ef.RoadReach()
        road = [("m", [(0.0, 0.0), (3000.0, 0.0)], 7, 0, [(0.0, "A"), (1.0, "B")])]
        self.assertEqual(cell(net, "14/0/0", road), 1)
        self.assertEqual(cell(net, "14/1/0", road), 0, "the same segment from the next cell: once")
        self.assertEqual(cell(net, "14/1/0", road), 0, "a cell added twice: once")
        self.assertTrue(net.remove_cell("14/0/0"))
        self.assertIsNotNone(net.grow(10.0, 0.0, 50.0, 0.0, 10.0), "the other cell still has it")
        self.assertTrue(net.remove_cell("14/1/0"))
        self.assertIsNone(net.grow(10.0, 0.0, 50.0, 0.0, 10.0))
        nodes, live, dead, cells = net.stats()
        self.assertEqual((live, cells), (0, 0))
        self.assertEqual(dead, 1)
        self.assertEqual(net.edges_near(10.0, 0.0, 50.0), [])

    def test_bad_input(self):
        net = fast._ef.RoadReach()
        with self.assertRaises(ValueError):
            net.add_cell("k", BIG, ["a"], np.zeros((2, 3)), np.zeros(1, np.int64), np.ones(1, np.int64),
                         np.zeros(1, np.uint8), np.zeros(0, np.int32), np.zeros(0, np.float32), [],
                         np.zeros(0, np.int32), np.zeros(0, np.float32), np.zeros(0, np.float32),
                         np.zeros(0, np.float32))
        with self.assertRaises(ValueError):
            net.grow(0.0, 0.0, 1.0, 0.0, 10.0, step_m=0.0)
        self.assertIsNone(net.grow(0.0, 0.0, 30.0, 0.0, 10.0), "an empty graph")
        self.assertIsNone(net.grow(float("nan"), 0.0, 30.0, 0.0, 10.0))


def arrays(classes, lines, subclass=None, links=(), conns=(), oneway=()):
    """A raw roads layer (mapdata.layer_arrays) of line features [(lon, lat), ...]."""
    n = len(lines)
    ll, co = [], [0]
    for ln in lines:
        ll += ln
        co.append(len(ll))
    a = {"ids": np.array([f"f{i}" for i in range(n)]), "lonlat": np.asarray(ll, np.float64),
         "part_off": np.arange(n + 1), "ring_off": np.arange(n + 1), "coord_off": np.asarray(co, np.int64),
         "attr.class": np.array(classes), "attr.subtype": np.array(["road"] * n),
         "attr.subclass": np.array(subclass or [""] * n)}
    a["rule.flags.feature"] = np.asarray([i for i in links], np.int32)
    a["rule.flags.value"] = np.array(["is_link"] * len(links))
    a["rule.connectors.feature"] = np.asarray([c[0] for c in conns], np.int32)
    a["rule.connectors.value"] = np.array([c[2] for c in conns])
    a["rule.connectors.start"] = np.asarray([c[1] for c in conns], np.float32)
    a["rule.connectors.end"] = np.asarray([c[1] for c in conns], np.float32)
    a["rule.oneway.feature"] = np.asarray([o[0] for o in oneway], np.int32)
    a["rule.oneway.value"] = np.asarray([o[1] for o in oneway], np.float32)
    a["rule.oneway.start"] = np.zeros(len(oneway), np.float32)
    a["rule.oneway.end"] = np.ones(len(oneway), np.float32)
    return a


@unittest.skipUnless(HAVE, "earthfast >= reach.NEED")
class CellInputsTest(unittest.TestCase):
    def test_drivable_roads_their_ramps_connectors_and_one_ways(self):
        import geodesy
        frame = geodesy.EnuFrame(40.0, -100.0, 0.0)
        a = arrays(["motorway", "footway", "residential", "motorway"],
                   [[(-100.0, 40.0), (-99.99, 40.0)], [(-100.0, 40.001), (-99.99, 40.001)],
                    [(-100.0, 40.002), (-99.99, 40.002)], [(-99.99, 40.0), (-99.98, 40.0)]],
                   subclass=["", "", "driveway", "link"], conns=[(0, 0.0, "c0"), (0, 1.0, "c1"), (3, 0.0, "c1"),
                                                               (1, 0.0, "cx")],
                   oneway=[(3, 1.0), (1, 1.0)])
        got = reach.cell_inputs(frame, a)
        self.assertEqual(got["feat_ids"], ["f0", "f3"], "the footway and the driveway left out")
        self.assertEqual(got["rank"].tolist(), [7, 7 | reach.LINK_BIT])
        self.assertEqual(got["conn_feat"].tolist(), [0, 0, 1], "connectors renumbered to the kept features")
        self.assertEqual(got["conn_ids"], ["c0", "c1", "c1"])
        self.assertEqual(got["ow_feat"].tolist(), [1])
        self.assertEqual(got["start"].tolist(), [0, 2])
        self.assertEqual(got["end"].tolist(), [2, 4])
        self.assertAlmostEqual(float(got["xy"][1, 0]), 853.0, delta=5.0, msg="0.01 deg of longitude at 40 N")
        net = fast._ef.RoadReach()
        self.assertEqual(net.add_cell("14/1/1", BIG, **got), 2)
        self.assertIsNone(reach.cell_inputs(frame, arrays(["footway"], [[(-100.0, 40.0), (-99.99, 40.0)]])))


class OrderKeyTest(unittest.TestCase):
    def test_sooner_likelier_nearer_the_road_first(self):
        k = lambda eta, p=1.0, gap=0.0, v=100.0, b=2.0: reach.order_key(eta, p, gap, v, b)    # noqa: E731
        self.assertLess(k(5.0), k(10.0))
        self.assertLess(k(10.0), k(10.0, p=0.25))
        self.assertAlmostEqual(k(10.0, p=0.25), 20.0, delta=1e-9, msg="a 1-in-4 branch counts twice as far")
        self.assertLess(k(10.0), k(10.0, gap=100.0))
        self.assertAlmostEqual(k(10.0, gap=100.0) - k(10.0), 100.0 / reach.V_SIDE, msg="at speed: V_SIDE")
        self.assertAlmostEqual(reach.order_key(10.0, 1.0, 100.0, 40.0, 2.0) - reach.order_key(10.0, 1.0, 0.0, 40.0, 2.0),
                               10.0, msg="slower: a quarter of the car's speed, as roads.SIDE_PENALTY")

    def test_a_tile_that_would_come_too_late_goes_after(self):
        v = 220.0                       # (500 mph: a tile matters ~3.4 s from when the car reaches it)
        late = reach.order_key(0.5, 1.0, 0.0, v, 2.5)
        on_time = reach.order_key(4.0, 1.0, 0.0, v, 2.5)
        self.assertLess(on_time, late, "the tile under the car can only come when it is nearly passed: later")
        self.assertGreater(reach.order_key(-5.0, 1.0, 0.0, v, 2.5), late, "past: last")
        slow = reach.order_key(0.5, 1.0, 0.0, 25.0, 2.5)
        self.assertLess(slow, reach.order_key(4.0, 1.0, 0.0, 25.0, 2.5), "at 56 mph it still has its time")

    def test_horizon(self):
        self.assertEqual(reach.horizon_s(30.0), reach.HORIZON_S)
        self.assertAlmostEqual(reach.horizon_s(250.0), reach.HORIZON_MAX_M / 250.0)


def straight_tree(frame_lat=40.0, v=100.0, length=3000.0, p=1.0):
    pts = np.array([[d, 0.0, d / v, p] for d in np.arange(0.0, length, 32.0)])
    return reach.Tree({"pts": pts, "mpp": pts[:, :3], "frontier": np.zeros((0, 4)), "states": 1}, 0.0, 0.0, v, 0.0,
                      time.monotonic())


@unittest.skipUnless(HAVE, "earthfast >= reach.NEED")
class TreeTest(unittest.TestCase):
    def test_geotiles_along_it_in_order(self):
        import geodesy
        import geotiles
        frame = geodesy.EnuFrame(40.0, -100.0, 0.0)
        tr = straight_tree()
        keys = tr.geotile_keys(frame, 160.0)
        self.assertGreater(len(keys), 10)
        x0, y0 = geotiles.tile_of(40.0, -100.0)
        on = sorted((k for k in keys if k[1] == y0 and keys[k][4] == 0.0), key=lambda k: k[0])
        self.assertGreater(len(on), 8, "the tiles the road runs through")
        etas = [keys[k][2] for k in on]
        self.assertEqual(etas, sorted(etas), "east: later and later")
        self.assertTrue(all(keys[k][1] for k in keys), "a sure path: all wanted")
        far = {k for k in keys if keys[k][4] > 0.0}
        self.assertTrue(far and all(keys[k][4] <= 160.0 for k in far))
        self.assertIs(tr.geotile_keys(frame, 160.0), keys, "kept per tree")
        short = tr.geotile_keys(frame, 160.0, horizon=5.0)
        self.assertLess(len(short), len(keys))
        unlikely = straight_tree(p=0.05).geotile_keys(frame, 160.0)
        self.assertFalse(any(v[1] for v in unlikely.values()), "below P_WANT: ordered, not wanted")

    def test_points_at(self):
        tr = straight_tree(v=100.0, length=1000.0)
        (x1, y1), (x2, y2) = tr.points_at([5.0, 20.0])
        self.assertAlmostEqual(x1, 500.0, delta=33.0)
        self.assertAlmostEqual(x2, 2000.0, delta=40.0, msg="past its end: straight on")


@unittest.skipUnless(HAVE, "earthfast >= reach.NEED")
class AheadPointsTest(unittest.TestCase):
    def test_along_the_tree_and_its_frontier(self):
        import earthd
        straight = earthd.ahead_points(0.0, 0.0, 30.0, 0.0)
        self.assertEqual(len(straight), len(earthd.AHEAD_S))
        self.assertTrue(all(abs(y) < 1e-9 for _x, y in straight))
        # a road curving north: the points follow it
        pts = np.array([[1000.0 * math.sin(a), 1000.0 * (1 - math.cos(a)), 1000.0 * a / 30.0, 1.0]
                        for a in np.linspace(0.0, math.pi / 2, 60)])
        fr = np.array([[1200.0, 600.0, 45.0, 0.5], [500.0, -500.0, 10.0, 0.05]])
        tr = reach.Tree({"pts": pts, "mpp": pts[:, :3], "frontier": fr}, 0.0, 0.0, 30.0, 0.0, 0.0)
        got = earthd.ahead_points(0.0, 0.0, 30.0, 0.0, tr)
        self.assertGreater(got[2][1], 100.0, "30 s on: well north of the straight line")
        self.assertIn((1200.0, 600.0), got, "a likely branch's data end")
        self.assertNotIn((500.0, -500.0), got, "an unlikely one's not")


class FakeLoop:
    def __init__(self):
        self.t = 100.0
        self.jobs = []

    def now(self):
        return self.t

    def run_in_executor(self, _pool, _build, aid, gx, gy, content, skin, make):
        import asyncio
        f = asyncio.Future(loop=asyncio.new_event_loop())
        self.jobs.append(((gx, gy), f))
        return f


@unittest.skipUnless(HAVE, "earthfast >= reach.NEED")
class GeoTrackerTreeTest(unittest.TestCase):
    def test_tree_order_and_unwanted(self):
        import geodesy
        import geotiles
        import roads
        frame = geodesy.EnuFrame(40.0, -100.0, 0.0)
        v = 200.0
        # a road ahead bending north 1 km on: the straight corridor would miss it
        pts = [[d, 0.0, d / v, 1.0] for d in np.arange(0.0, 1000.0, 32.0)]
        pts += [[1000.0, d, (1000.0 + d) / v, 1.0] for d in np.arange(0.0, 4000.0, 32.0)]
        pts = np.array(pts)
        tr = reach.Tree({"pts": pts, "mpp": pts[:, :3], "frontier": np.zeros((0, 4))}, 0.0, 0.0, v, 0.0, 0.0)
        t = roads.GeoTracker()
        t.pool = object()
        loop = FakeLoop()
        conn = SimpleNamespace(s=None, send=lambda m: None)
        t.on_pose(conn, 1, 0.0, 0.0, loop, None, vel=(v, 0.0), frame=frame, tree=tr)
        north = geotiles.tiles_near(frame, 1000.0, 3000.0, 10.0, exact=True)[0][:2]
        east = geotiles.tiles_near(frame, 3000.0, 0.0, 10.0, exact=True)[0][:2]
        self.assertIn(north, t.car, "the road's way is wanted")
        self.assertNotIn(east, t.car, "the straight line past the bend is not")
        self.assertEqual(len(loop.jobs), roads.BUILD_PER_POSE)
        self.assertTrue(t.tree_on)
        self.assertFalse(t.unwanted(north, now=loop.t + 1.0))
        self.assertTrue(t.unwanted(east, now=loop.t + 1.0), "never wanted, far from the car")
        self.assertFalse(t.unwanted(north, now=loop.t + reach.UNWANTED_GRACE_S - 0.1), "wanted a moment ago")
        self.assertTrue(t.unwanted(north, now=loop.t + reach.UNWANTED_GRACE_S + 0.1), "not wanted for a while")
        near = geotiles.tiles_near(frame, 0.0, 300.0, 10.0, exact=True)[0][:2]
        self.assertFalse(t.unwanted(near, now=loop.t + 10.0), "right by the car")
        # without a tree (slow, or no road): as before, nothing dropped
        t.on_pose(conn, 1, 0.0, 0.0, loop, None, vel=(10.0, 0.0), frame=frame, tree=tr)
        self.assertFalse(t.tree_on)
        self.assertFalse(t.unwanted(east, now=loop.t + 10.0))


class ServerUnwantedTest(unittest.TestCase):
    def test_geo_and_block_unwanted(self):
        import earthd
        tr = SimpleNamespace(unwanted=lambda k, now=None: k == (1, 1))
        a = SimpleNamespace(active=1, roads=tr, tree_led=True, block=None, pending_block=None,
                            chain_wanted_at={(128.0, 0.0): time.monotonic()})
        s = SimpleNamespace(connections=[a])
        self.assertTrue(earthd.Server.geo_unwanted(s, 1, 1, 1))
        self.assertFalse(earthd.Server.geo_unwanted(s, 1, 2, 2))
        self.assertFalse(earthd.Server.geo_unwanted(s, 2, 1, 1), "nobody on that anchor")
        self.assertTrue(earthd.Server.block_unwanted(s, 1, 512.0, 0.0))
        self.assertFalse(earthd.Server.block_unwanted(s, 1, 128.0, 0.0), "predicted a moment ago")
        a.pending_block = SimpleNamespace(cx=512.0, cy=0.0)
        self.assertFalse(earthd.Server.block_unwanted(s, 1, 512.0, 0.0), "the block the mod asked for")
        a.pending_block = None
        a.tree_led = False
        self.assertFalse(earthd.Server.block_unwanted(s, 1, 512.0, 0.0), "not led by a tree: as before")
        b = SimpleNamespace(active=1, roads=SimpleNamespace(unwanted=lambda k, now=None: False), tree_led=True,
                            block=None, pending_block=None, chain_wanted_at={})
        a.tree_led = True
        s.connections.append(b)
        self.assertFalse(earthd.Server.geo_unwanted(s, 1, 1, 1), "another player still wants it")


if __name__ == "__main__":
    unittest.main()
