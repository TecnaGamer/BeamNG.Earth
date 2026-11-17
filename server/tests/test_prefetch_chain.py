"""A fast player's chain of predicted blocks (earthd.predict_chain): the next requests in order, each one predict_request
from the last, along the path; none standing; no repeats."""
import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
