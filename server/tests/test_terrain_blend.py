"""The detail terrain base is 90% generated material colour and 10% aerial image."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import surface
import terrainblend


class TerrainBlendTest(unittest.TestCase):
    def test_aerial_variation_is_reduced_to_ten_percent_in_linear_light(self):
        dark = np.zeros((2, 2, 3), np.uint8)
        light = np.full_like(dark, 255)
        out_dark = terrainblend.blend(dark, "")
        out_light = terrainblend.blend(light, "")
        delta = terrainblend._linear(out_light) - terrainblend._linear(out_dark)
        np.testing.assert_allclose(delta, 0.10, atol=0.004)
        np.testing.assert_array_equal(dark, 0)
        np.testing.assert_array_equal(light, 255)

    def test_each_terrain_material_gets_its_own_base_colour(self):
        with tempfile.TemporaryDirectory() as tmp:
            ids = np.array([[surface.GRASS, surface.SNOW], [surface.ROCK, surface.DIRT]], np.uint8)
            # The engine's outer sample ring is deliberately holes. Keep the four
            # material samples inside that ring instead of testing an all-hole 2x2 block.
            ids = np.pad(ids, 1, constant_values=surface.DIRT)
            surface.write_ter(tmp, "test.ter", np.zeros(ids.shape, np.uint16), ids)
            ter = Path(tmp) / "test.ter"
            before = ter.read_bytes()
            photo = np.full((*ids.shape, 3), 128, np.uint8)
            shown = terrainblend.blend(photo, tmp, "test.ter")
            # The material map and aerial image use opposite row origins.
            self.assertEqual(terrainblend.material_grid(tmp, "test.ter").tolist(), ids[::-1].tolist())
            self.assertEqual(len(np.unique(shown.reshape(-1, 3), axis=0)), 4)
            self.assertEqual(ter.read_bytes(), before)

    def test_palette_covers_current_generated_materials(self):
        self.assertEqual(set(terrainblend.MATERIAL_COLOURS), {name for name, _ in surface.MATERIALS})


if __name__ == "__main__":
    unittest.main()
