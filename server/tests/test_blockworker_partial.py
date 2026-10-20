"""KyzoFox 1.5.4: blockworker.roads_tile never waits for the buildings' map data: missing layers make a partial
tile (no buildings), fetched in the background."""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import blockworker  # noqa: E402
import mapcells  # noqa: E402


class FakeMD:
    def __init__(self, have):
        self.have, self.fetched = set(have), []
        self.done = threading.Event()

    def has(self, key, layer):
        return (str(key), layer) in self.have

    def cell(self, key, layers=(), kind="demand"):
        self.fetched.append((str(key), tuple(layers), kind))
        self.done.set()


class PartialTest(unittest.TestCase):
    def test_missing_layers_listed_and_fetched_once(self):
        k1, k2 = mapcells.CellKey(14, 1, 1), mapcells.CellKey(14, 1, 2)
        md = FakeMD([(str(k1), ly) for ly in blockworker.BLD_LAYERS] + [(str(k2), "buildings")])

        class S:
            pass
        old = blockworker._s
        blockworker._s = S()
        blockworker._s.md = md
        try:
            miss = blockworker._bld_missing([k1, k2])
            self.assertEqual(miss, [(k2, ("building_parts", "infrastructure", "water"))])
            blockworker._fetch_later(miss)
            self.assertTrue(md.done.wait(5))
        finally:
            blockworker._s = old
        self.assertEqual(md.fetched[0][0], str(k2))
        self.assertEqual(md.fetched[0][1], ("building_parts", "infrastructure", "water"))


if __name__ == "__main__":
    unittest.main()
