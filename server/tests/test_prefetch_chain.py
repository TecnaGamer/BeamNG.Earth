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


if __name__ == "__main__":
    unittest.main()
