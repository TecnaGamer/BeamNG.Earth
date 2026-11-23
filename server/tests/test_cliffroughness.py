"""Authored roughness survives the portable lossless texture pipeline."""
import json
from pathlib import Path
import unittest

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT/'mod/texture_sources'


class CliffRoughnessTest(unittest.TestCase):
    def test_materials_use_aligned_nonuniform_authored_maps(self):
        entries = json.loads((SOURCES/'textures.json').read_text())['textures']
        materials = json.loads((ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff_materials.src.json').read_text())
        checked = set()
        for name, material in materials.items():
            stage = material['Stages'][0]
            path = stage['roughnessMap'].lstrip('/')
            self.assertEqual(stage.get('roughnessMapUseUV', 0), stage.get('normalMapUseUV', 0), name)
            self.assertEqual(stage['roughnessFactor'], 1, name)
            self.assertEqual(stage['metallicFactor'], 0, name)
            if path in checked:
                continue
            checked.add(path)
            entry = entries[path]
            self.assertEqual((entry['format'], entry['role']), ('BC4', 'data'))
            self.assertNotIn('derive', entry, 'preserve the authored roughness rather than regenerate from color')
            with Image.open(SOURCES/entry['source']) as image:
                pixels = np.asarray(image)
                if pixels.ndim == 3:
                    np.testing.assert_array_equal(pixels[..., 0], pixels[..., 1])
                    np.testing.assert_array_equal(pixels[..., 0], pixels[..., 2])
                    pixels = pixels[..., 0]
                sample = pixels[::4, ::4].astype(float)/255
                far = '_far_' in path
                self.assertGreaterEqual(float(sample.min()), .74 if far else .37)
                self.assertLessEqual(float(sample.max()), .985)
                self.assertGreater(float(np.percentile(sample, 95)-np.percentile(sample, 5)), .045)
                # Local grain and broad variation are both retained; a uniform
                # scalar map or a few perfectly smooth islands cannot pass.
                patch = pixels[:256, :256].astype(float)/255
                self.assertGreater(float(np.abs(np.diff(patch, axis=1)).mean()), .001)
                self.assertGreater(float(np.std(sample.mean(axis=1))), .002)
        self.assertEqual(checked, {p for p in entries if '/cliff/' in p and '_roughness.' in p})


if __name__ == '__main__':
    unittest.main()
