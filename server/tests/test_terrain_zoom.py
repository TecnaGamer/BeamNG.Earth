"""The near terrain's DEM zoom follows its squares (dem.terrain_zoom)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem  # noqa: E402
import heightmap  # noqa: E402


class TerrainZoomTest(unittest.TestCase):
    def test_1_m_squares_as_before(self):
        with mock.patch.object(heightmap, "SQUARE", 1.0):
            for lat in (0.0, 36.0, 51.2, 70.0):
                self.assertEqual(dem.terrain_zoom(lat), dem.zoom_for(lat, 1.25, dem.FINE_ZOOM))

    def test_2_m_squares_one_zoom_less(self):
        with mock.patch.object(heightmap, "SQUARE", 2.0):
            self.assertEqual(dem.terrain_zoom(51.2), 15)
        with mock.patch.object(heightmap, "SQUARE", 1.0):
            self.assertEqual(dem.terrain_zoom(51.2), 16)


if __name__ == "__main__":
    unittest.main()
