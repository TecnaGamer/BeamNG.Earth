"""The roofs' and squares' photo (the user's report 14 at Fells Point, 2026-11-12: Broadway Square drawn grey)."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import buildings  # noqa: E402
import geodesy  # noqa: E402
import imagery  # noqa: E402


class PhotoSourcesTest(unittest.TestCase):
    def test_maryland_reads_its_own_photo_first(self):
        got = buildings._photo_sources(geodesy.EnuFrame(39.2822, -76.5931), -50, -50, 50, 50)
        self.assertEqual(got[0], "md6in")
        self.assertIn("virginia", got)          # (its box reaches Baltimore: the next in the chain)
        self.assertNotIn("md6in", buildings._photo_sources(geodesy.EnuFrame(40.7, -74.0), -50, -50, 50, 50))

    def test_any_size_asked_is_rendered_in_whole_cells(self):
        asked = []

        class Reader:
            def render(self, frame, x0, y0, size, px):
                asked.append(px)
                if px % imagery.MESH_CELLS:
                    raise ValueError("tile_warp: cells * cw == px")
                return np.zeros((px, px, 3), np.uint8), {"md6in": px * px}
        with mock.patch("streetfront._photo_reader", lambda *_a: Reader()):
            ph = buildings.roof_photo(geodesy.EnuFrame(39.2822, -76.5931), -40, -40, 40, 40, "/nowhere", px=174)
        self.assertIsNotNone(ph)
        self.assertEqual(asked, [176])


if __name__ == "__main__":
    unittest.main()
