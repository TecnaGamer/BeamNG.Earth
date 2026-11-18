"""A fast player's chain of predicted blocks (earthd.predict_chain): the next requests in order, each one predict_request
from the last, along the path; none standing; no repeats."""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402


class PrefetchChainTest(unittest.TestCase):
    def test_chain(self):
        rule = earthd.FOLLOW_RULES[1024.0]                 # the 1 km block (by its width in metres)
        v = 223.0                                   # 500 mph, west
        first = earthd.predict_request(0.0, 0.0, -v, 0.0, 0.0, 0.0, rule)
        chain = earthd.predict_chain(0.0, 0.0, -v, 0.0, 0.0, 0.0, rule, 4)
        self.assertGreaterEqual(len(chain), 3, chain)
        self.assertEqual(chain[0], first, "the first: the mod's next request")
        xs = [c[0] for c in chain]
        self.assertEqual(xs, sorted(xs, reverse=True), "westward, in order")
        self.assertEqual(len(set(chain)), len(chain), "no repeats")
        self.assertTrue(all(abs(c[1]) < 1e-6 for c in chain), "on the path")
        self.assertEqual(earthd.predict_chain(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, rule, 4), [], "standing: none")


    def test_along_the_route(self):
        """On the GPS route the chain follows it round a corner (predict_route_chain); off it, or heading the other
        way, none (the straight chain then)."""
        import numpy as np
        rule = earthd.FOLLOW_RULES[1024.0]
        v = 67.0                                    # 150 mph, east, then the route turns north at x = 600
        route = np.array([[-200.0, 0.0], [600.0, 0.0], [600.0, 2500.0]])
        chain = earthd.predict_route_chain(route, 0.0, 0.0, v, 0.0, 0.0, 0.0, rule, 5)
        self.assertGreaterEqual(len(chain), 3, chain)
        self.assertEqual(chain[0], earthd.predict_request(0.0, 0.0, v, 0.0, 0.0, 0.0, rule), "the first as before")
        self.assertGreater(max(c[1] for c in chain), 500.0, "round the corner, north")
        self.assertEqual(len(set(chain)), len(chain), "no repeats")
        straight = earthd.predict_chain(0.0, 0.0, v, 0.0, 0.0, 0.0, rule, 5)
        self.assertTrue(all(abs(c[1]) < 1e-6 for c in straight), "the straight chain stays east")
        self.assertIsNone(earthd.predict_route_chain(route, 0.0, 300.0, v, 0.0, 0.0, 0.0, rule, 5), "off it")
        self.assertIsNone(earthd.predict_route_chain(route, 0.0, 0.0, -v, 0.0, 0.0, 0.0, rule, 5), "the other way")
        self.assertIsNone(earthd.predict_route_chain(None, 0.0, 0.0, v, 0.0, 0.0, 0.0, rule, 5), "no route")

    def test_a_block_per_hop_at_speed(self):
        """300 km/h on the 1 km block: the block just taken is centred ahead of the car, and the next request comes
        once the car is past it (follow.lua centreAhead), three grid steps on: a chain of one block per 384 m, not one
        a grid step on after every one (asked for at once, then dropped or taken)."""
        import numpy as np
        rule = earthd.FOLLOW_RULES[1024.0]
        v = 85.0
        self.assertTrue(earthd.centre_ahead(200.0, 0.0, v, 0.0, 384.0, 0.0, rule), "its centre 184 m ahead")
        self.assertFalse(earthd.centre_ahead(400.0, 0.0, v, 0.0, 384.0, 0.0, rule), "past it")
        self.assertFalse(earthd.centre_ahead(200.0, 0.0, -v, 0.0, 384.0, 0.0, rule), "driving away from it")
        self.assertFalse(earthd.centre_ahead(200.0, 0.0, 160.0, 0.0, 384.0, 0.0, rule), "the fast mode: as before")
        road = np.array([[-100.0, 0.0], [6000.0, 0.0]])
        chain = earthd.predict_route_chain(road, 0.0, 0.0, v, 0.0, 0.0, 0.0, rule, 6, None, 40.0, 5000.0)
        self.assertEqual([c[0] for c in chain], [384.0 * k for k in range(1, 7)], chain)
        self.assertEqual([c[0] for c in earthd.predict_chain(0.0, 0.0, v, 0.0, 0.0, 0.0, rule, 4)],
                         [384.0 * k for k in range(1, 5)])

    def test_chain_with_the_built_blocks(self):
        """The mod takes a built block near its target (follow.lua preferReady), and its next request is from that
        block: the chain with the built blocks known goes on from the one it takes, and holds it (still wanted)."""
        import numpy as np
        rule = earthd.FOLLOW_RULES[1024.0]
        v = 85.0
        road = np.array([[-100.0, 0.0], [6000.0, 0.0]])
        # a block built one grid step short of the first target, one step to the side
        ready = {(256.0, 128.0)}
        chain = earthd.predict_route_chain(road, 0.0, 0.0, v, 0.0, 0.0, 0.0, rule, 5, None, 40.0, 5000.0, ready=ready)
        self.assertEqual(chain[0], (256.0, 128.0), "the built one for the first target")
        self.assertEqual(chain[1][0], 256.0 + 384.0, "the next: three steps on from the one taken")
        self.assertEqual(len(set(chain)), len(chain))
        # none near: the targets themselves
        far = earthd.predict_route_chain(road, 0.0, 0.0, v, 0.0, 0.0, 0.0, rule, 3, None, 40.0, 5000.0,
                                         ready={(3000.0, 2000.0)})
        self.assertEqual([c[0] for c in far], [384.0, 768.0, 1152.0])
        # the mod's own choice (modsim: follow.lua's rule) agrees
        self.assertEqual(earthd.prefer_ready(ready, 129.0, 0.0, v, 0.0, 384.0, 0.0, 0.0, 0.0, rule.reach,
                                             earthd.READY_STEPS * rule.grid), (256.0, 128.0))
        self.assertIsNone(earthd.prefer_ready({(0.0, 0.0)}, 129.0, 0.0, v, 0.0, 384.0, 0.0, 0.0, 0.0, rule.reach,
                                              earthd.READY_STEPS * rule.grid), "never the block in use")


class RebaseChainTest(unittest.TestCase):
    """The blocks past a rebase (Connection.predict_rebase): the chain in the new anchor's frame from the rebase's
    request on, built before this frame's own chain (maybe_prefetch)."""

    def conn(self, v=(-68.0, -68.0), at=(-10400.0, -10400.0)):
        import types
        import farfield
        import geodesy
        import numpy as np
        lat, lon = 52.2, 8.9
        old = earthd.Anchor(2, lat, lon, geodesy.EnuFrame(lat, lon))
        la, lo, _ = old.frame.enu_to_geodetic(at[0], at[1], 0.0)
        new = earthd.Anchor(3, float(la), float(lo), geodesy.EnuFrame(float(la), float(lo)))
        road = np.array([[-9000.0, -9000.0], [-16000.0, -16000.0]])        # (in the old frame)
        srv = types.SimpleNamespace(anchors={2: old}, follow=earthd.FOLLOW_RULES[1024.0], layout=farfield.Layout(64, 1024),
                                    fast=True, hm={}, hm_inflight={}, abandoned=set())
        c = types.SimpleNamespace(s=srv, active=2, focus=at, vel=v, following=True, ready_set=True, route_points=None,
                                  rebase_to=(new, -256.0, -256.0), prefetch_busy=set(),
                                  road_xy=lambda aid, x, y: road if aid == 2 else None)
        c.route_xy = lambda aid, anchor=None: earthd.Connection.route_xy(c, aid, anchor)
        c.blocks_known = lambda aid, px=None, py=None: earthd.Connection.blocks_known(c, aid, px, py)
        return c, old, new, road

    def test_the_chain_in_the_new_frame(self):
        c, old, new, road = self.conn()
        chain = earthd.Connection.predict_rebase(c)
        self.assertEqual(c.rebase_by, "road")
        self.assertGreaterEqual(len(chain), 6, chain)
        # in the new anchor's frame: from its first block on down the road, a block per hop, none of them the first
        steps = [(b.cx - a.cx, b.cy - a.cy) for a, b in zip(chain, chain[1:])]
        self.assertTrue(all(dx <= 0 and dy <= 0 and (dx, dy) != (0, 0) for dx, dy in steps), chain)
        self.assertNotIn((-256.0, -256.0), [(b.cx, b.cy) for b in chain])
        self.assertTrue(all(abs(b.cx) < 5000 and abs(b.cy) < 5000 for b in chain), "the new frame's, near its origin")
        self.assertTrue(all(b.size == c.s.layout.block for b in chain))
        # ... as the prediction gives them after the commit: the same road, the car and its speed in that frame
        moved = earthd.moved_xy(old.frame, new.frame, road)
        (px, py), = earthd.moved_xy(old.frame, new.frame, [c.focus])
        want = earthd.predict_route_chain(moved, px, py, *c.vel, -256.0, -256.0, c.s.follow, earthd.ROAD_CHAIN + 1,
                                          None, earthd.ROAD_AHEAD_S, earthd.ROAD_AHEAD_MAX_M, ready=set())
        self.assertEqual([(b.cx, b.cy) for b in chain], want)
        # the new frame's road ahead until its own lines are read (road_xy)
        self.assertEqual(c.rebase_road[0], 3)
        self.assertLess(abs(c.rebase_road[1] - moved).max(), 1e-6)

    def test_none_without_a_rebase_at_hand(self):
        c, *_ = self.conn()
        c.rebase_to = None
        self.assertEqual(earthd.Connection.predict_rebase(c), [], "none asked for")
        c, *_ = self.conn(v=(-20.0, -20.0))
        self.assertEqual(earthd.Connection.predict_rebase(c), [], "not a fast player")
        c, *_ = self.conn(v=(68.0, 68.0))
        self.assertEqual(earthd.Connection.predict_rebase(c), [], "turned back towards the origin")
        c, *_ = self.conn()
        c.s.abandoned.add(3)
        self.assertEqual(earthd.Connection.predict_rebase(c), [], "a teleport left it")
        c, *_ = self.conn()
        c.active = 3
        self.assertEqual(earthd.Connection.predict_rebase(c), [], "committed")

    def test_off_the_road_straight(self):
        c, *_ = self.conn()
        c.road_xy = lambda aid, x, y: None
        chain = earthd.Connection.predict_rebase(c)
        self.assertEqual(c.rebase_by, "straight")
        self.assertTrue(chain)
        self.assertLessEqual(len(chain), earthd.PREFETCH_CHAIN)

    def test_built_before_this_frames_chain(self):
        """The next block of this frame first (asked for before the rebase), then the new anchor's, in order."""
        c, old, new, _road = self.conn()
        c.rebase_chain = earthd.Connection.predict_rebase(c)
        block = c.s.layout.block_at
        c.block, c.predicted_block = block(-10112.0, -10112.0), block(-10496.0, -10496.0)
        c.predicted_chain = [block(-10880.0, -10880.0)]
        c.s.terrain_cached = lambda key: key in c.s.hm
        c.ready_will_do = lambda *a: False
        spawned = []

        def spawn(coro):
            spawned.append(coro)
            coro.close()
        c.spawn = spawn
        with unittest.mock.patch.object(earthd, "PREFETCH_PARALLEL", 3):
            earthd.Connection.maybe_prefetch(c)
        first = c.rebase_chain[0]
        self.assertEqual(c.prefetch_busy, {(2, -10496.0, -10496.0), (3, first.cx, first.cy),
                                           (3, c.rebase_chain[1].cx, c.rebase_chain[1].cy)})
        self.assertEqual(len(spawned), 3)
        self.assertNotIn((-256.0, -256.0), c.chain_wanted_at, "the new frame's are not this frame's wanted blocks")


if __name__ == "__main__":
    unittest.main()
