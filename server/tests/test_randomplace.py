"""randomplace.py (the level select's "Random location", the default places on roads) and placepreview.py (each
place's picture for the level select)."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mapcells  # noqa: E402
import placepreview  # noqa: E402
import randomplace  # noqa: E402
from test_roads import features, LAT, LON, M_LAT, M_LON  # noqa: E402


class OneCell:
    """The roads f in the cell of (LAT, LON), none elsewhere."""

    def __init__(self, f):
        self.f, self.home = f, mapcells.cell_of(LAT, LON)

    def cell(self, key, layers=(), kind="demand"):
        return SimpleNamespace(layers={"roads": self.f if key == self.home else None})


class RoadSpotTest(unittest.TestCase):
    def test_nearest_public_road_on_the_ground(self):
        # a footway 5 m away, a residential street 40 m away, a tertiary bridge 20 m away
        f = features([[(-200, 5), (200, 5)], [(-200, 40), (200, 40)], [(-200, -20), (200, -20)]],
                     ["footway", "residential", "tertiary"], flags=[(2, "is_bridge", 0.0, 1.0)])
        lat, lon, cls = randomplace.road_spot(OneCell(f), LAT, LON, 500)
        self.assertEqual(cls, "residential")
        self.assertAlmostEqual((lat - LAT) * M_LAT, 40.0, delta=0.5)
        self.assertAlmostEqual((lon - LON) * M_LON, 0.0, delta=0.5)

    def test_kept_off_the_ends_and_none_beyond_the_radius(self):
        f = features([[(30, 0), (300, 0)]], ["residential"])
        lat, lon, _ = randomplace.road_spot(OneCell(f), LAT, LON, 500)
        self.assertAlmostEqual((lon - LON) * M_LON, 30 + randomplace.END_CLEAR_M, delta=0.5, msg="not on its end")
        self.assertIsNone(randomplace.road_spot(OneCell(f), LAT, LON, 20))

    def test_towns_and_a_pick(self):
        ts = randomplace.towns()
        self.assertGreater(len(ts), 5000)
        self.assertTrue(all(-90 <= t[0] <= 90 and -180 <= t[1] <= 180 for t in ts[:200]))
        # every town's roads: one street through (LAT, LON); the random town is put there
        f = features([[(-3000, 0), (3000, 0)]], ["residential"])
        saved = randomplace._towns
        try:
            randomplace._towns = [(LAT, LON, "Testtown")]
            got = randomplace.pick(OneCell(f), np.random.default_rng(3))
        finally:
            randomplace._towns = saved
        self.assertIsNotNone(got)
        self.assertEqual(got[2], "Near Testtown")
        self.assertAlmostEqual((got[0] - LAT) * M_LAT, 0.0, delta=0.5, msg="on the street")


class PlacePreviewTest(unittest.TestCase):
    def test_written_once_and_bad_keys_skipped(self):
        calls = []

        class Img:
            def render(self, frame, x0, y0, size, px):
                calls.append((x0, size, px))
                return np.full((px, px, 3), 120, np.uint8), {}
        with tempfile.TemporaryDirectory() as d:
            keys = placepreview.write(Img(), d, [("39p2833_m76p5935", 39.2833, -76.5935), ("../x", 0, 0)])
            self.assertEqual(keys, ["39p2833_m76p5935"])
            p = placepreview.path_of(d, "39p2833_m76p5935")
            from PIL import Image
            with Image.open(p) as im:
                self.assertEqual(im.size, (placepreview.PX, placepreview.PX * 9 // 16))
            placepreview.write(Img(), d, [("39p2833_m76p5935", 39.2833, -76.5935)])
            self.assertEqual(len(calls), 1, "made once")
            self.assertFalse(os.path.exists(os.path.join(d, "levels", "beamng_earth", "x.jpg")))

    def test_older_pictures_made_again(self):
        """A folder of an older format (pictures that may hold EOX 2023 pixels, before 2026-11-23): its pictures are
        deleted once and made again; other files stay."""
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, *placepreview.FOLDER)
            os.makedirs(folder)
            for name in ("1p5_2p5.jpg", "notes.txt"):
                open(os.path.join(folder, name), "wb").write(b"old")
            placepreview._swept.discard(folder)
            self.assertEqual(placepreview.sweep(d), 1)
            self.assertEqual(sorted(os.listdir(folder)), [placepreview.FORMAT_FILE, "notes.txt"])
            open(os.path.join(folder, "1p5_2p5.jpg"), "wb").write(b"new")
            placepreview._swept.discard(folder)
            self.assertEqual(placepreview.sweep(d), 0, "the current format: kept")
            self.assertTrue(os.path.exists(os.path.join(folder, "1p5_2p5.jpg")))


if __name__ == "__main__":
    unittest.main()
