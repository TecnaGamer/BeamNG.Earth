"""Private additive shader data retains neutral colour and complete DDS mips."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
from io import BytesIO
import struct
import unittest

import numpy as np
from PIL import Image
import fast


def png(values):
    output = BytesIO()
    Image.fromarray(values).save(output, "PNG")
    return output.getvalue()


class GroundMapsTest(unittest.TestCase):
    def test_flat_stock_becomes_neutral_detail_without_a_color_cast(self):
        rgb = np.tile(np.array([64, 128, 192], np.uint8), (8, 8, 1))
        scalar = png(np.full((8, 8), 255, np.uint8))
        maps = fast._ef.cliff_ground_maps(png(rgb), scalar, scalar)
        for data, dxgi in zip(maps, (98, 80, 80)):
            self.assertEqual(struct.unpack_from("<I", data, 128)[0], dxgi)
            self.assertEqual(struct.unpack_from("<I", data, 28)[0], 4)
        color = np.asarray(Image.open(BytesIO(maps[0])).convert("RGB"), dtype=int)
        np.testing.assert_allclose(color, 128, atol=1)
        ao = np.asarray(Image.open(BytesIO(maps[1])).convert("L"), dtype=int)
        self.assertLessEqual(ao.max(), 127)
        roughness = np.asarray(Image.open(BytesIO(maps[2])).convert("L"), dtype=int)
        np.testing.assert_allclose(roughness, 128, atol=1)
        self.assertEqual(maps, fast._ef.cliff_ground_maps(png(rgb), scalar, scalar))

    def test_roughness_variation_and_ao_cavities_survive(self):
        values = np.tile(np.array([0] * 4 + [255] * 4, np.uint8), (8, 1))
        source = png(values)
        _, ao_dds, roughness_dds = fast._ef.cliff_ground_maps(source, source, source)
        ao = np.asarray(Image.open(BytesIO(ao_dds)).convert("L"), dtype=int)
        roughness = np.asarray(Image.open(BytesIO(roughness_dds)).convert("L"), dtype=int)
        np.testing.assert_allclose(ao[:, :4], 0, atol=1)
        np.testing.assert_allclose(ao[:, 4:], 127, atol=1)
        np.testing.assert_allclose(roughness[:, :4], 64, atol=1)
        np.testing.assert_allclose(roughness[:, 4:], 191, atol=1)

    def test_invalid_sizes_fail_before_encoding(self):
        source = png(np.ones((3, 4), np.uint8))
        with self.assertRaisesRegex(ValueError, "power-of-two"):
            fast._ef.cliff_ground_maps(source, source, source)
