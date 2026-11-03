"""OSM billboards (billboardsource.py): the bundled extract, filtered to a map cell."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import billboardsource
import mapcells
import worldprops


class ExtractTest(unittest.TestCase):
    def test_a_point_comes_back_only_from_its_cell(self):
        lat = np.array([10.0, 34.08214], np.float64)
        lon = np.array([20.0, -118.26032], np.float64)
        ids = np.array([1, -7], np.int64)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "billboards.npz")
            np.savez(path, lat=lat, lon=lon, id=ids)
            src = billboardsource.Source(path)
            echo = mapcells.cells_for_points(np.array([34.08214]), np.array([-118.26032]))
            other = mapcells.cells_for_points(np.array([10.0]), np.array([20.0]))
            far = mapcells.cells_for_points(np.array([0.0]), np.array([0.0]))
            got = src.cells(echo)
            self.assertEqual(len(got), 1)
            self.assertEqual(len(got[0]), 1)
            self.assertTrue(str(got[0].ids[0]).startswith("osm-billboard-w"))
            self.assertTrue(worldprops._advertising(json_tags(got[0], 0)))
            self.assertEqual(len(src.cells(other)), 1)
            self.assertTrue(str(src.cells(other)[0].ids[0]).startswith("osm-billboard-n"))
            self.assertEqual(src.cells(far), [])

    def test_the_echo_park_board_is_in_the_extract(self):
        src = billboardsource.Source()
        src._load()
        self.assertGreater(len(src._lat), 0)
        d = (src._lat - 34.08214) ** 2 + (src._lon + 118.26032) ** 2
        self.assertLess(float(d.min()) ** 0.5, 0.0004)


def json_tags(feats, i):
    import json
    return json.loads(str(feats.attrs["tags"][i]))


if __name__ == "__main__":
    unittest.main()
