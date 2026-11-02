"""Sign tiles once the road geotiles under them are in (roads.GeoTracker.ready_tiles): each anchor tile on its own, not
only once every road geotile round the car is (2026-10-29: while driving that seldom was, and the signs came only
right next to a car that had slowed down)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import roads  # noqa: E402


class SignReadyTest(unittest.TestCase):
    def test_per_tile(self):
        frame = geodesy.EnuFrame(39.2950, -76.6100)
        g = roads.GeoTracker()
        self.assertEqual(g.ready_tiles(frame, 0.0, 0.0, 300.0), set(), "no road geotiles yet: no signs")
        reach = roads.TILE / 2 * 2 ** 0.5
        under = {(a, b) for a, b, _ in geotiles.tiles_near(frame, 0.5 * roads.TILE, 0.5 * roads.TILE, reach)}
        g.sent |= under
        g.skins |= under
        got = g.ready_tiles(frame, 0.0, 0.0, 300.0)
        self.assertIn((0, 0), got, "the tile whose geotiles are all in")
        self.assertLess(len(got), len(roads.tiles_near(0.0, 0.0, 300.0)), "not the ones whose geotiles are missing")
        # content without this anchor's skin: not yet
        g.skins.clear()
        self.assertEqual(g.ready_tiles(frame, 0.0, 0.0, 300.0), set())
        # waiting for a retry counts as in (a failed geotile never holds the signs)
        g.retry = {k: 0.0 for k in under}
        self.assertIn((0, 0), g.ready_tiles(frame, 0.0, 0.0, 300.0))


if __name__ == "__main__":
    unittest.main()
