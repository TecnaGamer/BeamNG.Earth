"""A fast player's chain of predicted blocks (earthd.predict_chain): the next requests in order, each one predict_request
from the last, along the path; none standing; no repeats."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402


class PrefetchChainTest(unittest.TestCase):
    def test_chain(self):
        rule = earthd.FOLLOW_RULES[512]
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


if __name__ == "__main__":
    unittest.main()
