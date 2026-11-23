"""Limestone's authored PBR maps preserve relief and suppress cavity highlights."""
import json
from pathlib import Path
import unittest

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


class LimestonePbrTest(unittest.TestCase):
    def test_authored_relief_cavity_shading_and_rough_dark_areas(self):
        sources = ROOT/'mod/texture_sources'
        entries = json.loads((sources/'textures.json').read_text())['textures']
        material = json.loads((ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff_materials.src.json').read_text())['bne_cliff_roadcut']['Stages'][0]
        maps = {}
        for channel in ('baseColorMap', 'normalMap', 'roughnessMap', 'ambientOcclusionMap'):
            entry = entries[material[channel].lstrip('/')]
            self.assertNotIn('derive', entry)
            with Image.open(sources/entry['source']) as image:
                self.assertEqual(image.size, (2048, 2048) if channel == "baseColorMap" else (1024, 1024))
                maps[channel] = np.asarray(image.convert('RGB').resize((128, 128), Image.Resampling.NEAREST)).astype(float)/255
        normal = maps['normalMap']*2-1
        np.testing.assert_allclose(np.linalg.norm(normal, axis=-1), 1, atol=.015)
        self.assertGreater(float(normal[..., :2].std()), .02)
        ao = maps['ambientOcclusionMap'][..., 0]
        roughness = maps['roughnessMap'][..., 0]
        luminance = maps['baseColorMap'] @ np.array([.2126, .7152, .0722])
        self.assertLess(float(ao.min()), .8)
        self.assertGreater(float(ao.max()), .98)
        dark = luminance < np.percentile(luminance, 10)
        bright = luminance > np.percentile(luminance, 70)
        self.assertGreater(float(roughness[dark].mean()), .9)
        self.assertGreater(float(roughness[dark].mean()-roughness[bright].mean()), .04)
        self.assertGreater(float(roughness[ao < .6].mean()), .9)
        self.assertEqual(material['metallicFactor'], 0)
        self.assertEqual(material['roughnessFactor'], 1)


if __name__ == '__main__':
    unittest.main()
