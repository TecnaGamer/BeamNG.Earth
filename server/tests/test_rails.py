"""Railway and tram tracks (rails.py): ballast, sleepers and rails on open ground, the street track on a carriageway."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import rails  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def cells(lines, classes, subtypes):
    return [features(lines, classes, ids=[f"id{i:03d}" for i in range(len(lines))], subtypes=subtypes)]


class TrackTest(unittest.TestCase):
    def setUp(self):
        # a primary street along y = 128 and a tram crossing it at x = 100, in tile (0, 0) (0..256 m)
        self.cells = cells([[(-50, 128), (300, 128)], [(100, -50), (100, 300)]], ["primary", "tram"],
                           ["road", "rail"])
        self.half = roads.road_specs(self.cells[0])[0].width / 2
        self.chunks = rails.tile_chunks(FRAME, 0, 0, self.cells)

    def of(self, m):
        return [np.asarray(c["n"], np.float64)[:, :2] for c in self.chunks if c["m"] == m]

    def test_street_track_on_the_carriageway_ballast_off_it(self):
        street = self.of(rails.STREET_TRACK["m"])
        self.assertEqual(len(street), 1)
        y = street[0][:, 1]
        self.assertLess(abs(y.min() - (128 - self.half)), rails.MIN_RUN_M + 1.0)
        self.assertLess(abs(y.max() - (128 + self.half)), rails.MIN_RUN_M + 1.0)
        ballast = self.of(rails.BALLAST["m"])
        self.assertEqual(len(ballast), 2, "one bed either side of the street")
        for b in ballast:
            inner = b[(b[:, 1] > 128 - self.half + 2) & (b[:, 1] < 128 + self.half - 2)]
            self.assertEqual(len(inner), 0, "no ballast on the carriageway")

    def test_rails_at_the_gauge_and_sleepers_off_the_street(self):
        centre = float(np.mean([b[:, 0].mean() for b in self.of(rails.BALLAST["m"])]))     # (the test frame's x ~100)
        xs = np.array(sorted(float(r[:, 0].mean()) for r in self.of(rails.RAIL["m"]))) - centre
        off = rails.GAUGE_M["tram"] / 2 + rails.HEAD_M / 2
        np.testing.assert_allclose(xs, [-off, -off, off, off], atol=0.01)
        mids = np.array([s.mean(0) for s in self.of(rails.SLEEPER["m"])])
        self.assertGreater(len(mids), 300)
        self.assertFalse(np.any(np.abs(mids[:, 1] - 128) < self.half - rails.ON_ROAD_INSET_M), "none on the street")
        self.assertTrue(np.all((mids[:, 1] >= -2) & (mids[:, 1] <= 258)), "centres in the tile (+ half a sleeper)")

    def test_drawn_in_order_and_stacked(self):
        mats = []
        for c in self.chunks:
            if c["m"] not in mats:
                mats.append(c["m"])
        self.assertEqual(mats, [rails.BALLAST["m"], rails.SLEEPER["m"], rails.RAIL["m"], rails.STREET_TRACK["m"]])
        lift = {m: roadmesh.LIFT[roadmesh.layer_of(c)] for c in self.chunks for m in [c["m"]]}
        self.assertLess(lift[rails.BALLAST["m"]], lift[rails.SLEEPER["m"]])
        self.assertLess(lift[rails.SLEEPER["m"]], lift[rails.RAIL["m"]])

    def test_subways_and_tracks_elsewhere_left_out(self):
        self.assertEqual(rails.tile_chunks(FRAME, 0, 0, cells([[(100, 0), (100, 256)]], ["subway"], ["rail"])), [])
        self.assertEqual(rails.tile_chunks(FRAME, 5, 5, self.cells), [])


if __name__ == "__main__":
    unittest.main()
