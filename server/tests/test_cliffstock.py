"""Private contact maps are deterministic BC4 cutout data, never shipped stock images."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
from io import BytesIO
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
from PIL import Image
import fast
import cliffstock


def height_png(values):
    buffer = BytesIO()
    Image.fromarray(values).save(buffer, "PNG")
    return buffer.getvalue()


class CliffStockMapsTest(unittest.TestCase):
    def test_both_categories_off_skip_private_preparation(self):
        env = {'BNE_CLIFFS': '1', 'BNGEARTH_START_SETTINGS': json.dumps({'values': {
            'naturalCliffs': False, 'cliffRoadCuts': False}})}
        with patch.object(cliffstock, 'prepare', side_effect=AssertionError('private preparation')):
            self.assertIsNone(cliffstock.prepare_enabled(
                lambda: self.fail('game lookup when disabled'), 'unused-user', env=env))

    def test_private_archive_preparation_with_native_encoder(self):
        height = height_png(np.array([[1024, 1025], [1027, 1026]], dtype=np.uint16))
        color = height_png(np.full((2, 2, 3), 128, dtype=np.uint8))
        dds, _ = fast._ef.cliff_contact_maps(height)
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory) / "game"
            archive_path = game / "content/assets/materials/terrain.zip"
            archive_path.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive_path, "w") as archive:
                for suffix in cliffstock.COPIES:
                    archive.writestr(cliffstock.PREFIX + suffix, dds)
                archive.writestr(cliffstock.PREFIX + "_h.png", height)
                archive.writestr(cliffstock.PREFIX + "_b.png", color)
            messages = []
            target = cliffstock.prepare(game, Path(directory) / "user", enabled=True,
                                        native=fast._ef, diagnostic=messages.append)
            self.assertEqual(messages, [])
            self.assertEqual((target / "coverage.data.dds").read_bytes(), dds)
            material = json.loads((target / "contact.materials.json").read_text())[cliffstock.MATERIAL]
            np.testing.assert_allclose(material["Stages"][0]["baseColorFactor"],
                                       fast._ef.cliff_contact_color_factor(color))

    def test_contact_color_factor_matches_linear_light_reference(self):
        rgb = np.array([[[0, 255, 128], [255, 255, 128]]], dtype=np.uint8)
        values = rgb.astype(float) / 255
        mean = np.where(values <= .04045, values / 12.92, ((values + .055) / 1.055) ** 2.4).mean(axis=(0, 1))
        expected = np.append(1.055 * (1 / mean) ** (1 / 2.4) - .055, 1.)
        np.testing.assert_allclose(fast._ef.cliff_contact_color_factor(height_png(rgb)), expected, atol=1e-12)
        with self.assertRaisesRegex(ValueError, "zero-energy"):
            fast._ef.cliff_contact_color_factor(height_png(np.zeros((2, 2), dtype=np.uint8)))

    def test_contact_maps_have_correct_mips_and_preserve_source_depth(self):
        fast.require(__import__("cliffsettings").CLIFF_KERNEL, "private cliff contact maps")
        # Distinct values within a single high byte catch accidental conversion
        # of the stock height to eight bits before normalization.
        source = height_png(np.array([[1024, 1025], [1027, 1026]], dtype=np.uint16))
        coverage, threshold = fast._ef.cliff_contact_maps(source)
        self.assertEqual(coverage[:4], b"DDS ")
        self.assertEqual(struct.unpack_from("<II", coverage, 12), (2, 2))
        self.assertEqual(struct.unpack_from("<I", coverage, 28)[0], 2)
        self.assertEqual(struct.unpack_from("<I", coverage, 128)[0], 80)
        self.assertEqual(len(coverage), 148 + 16)
        self.assertEqual(struct.unpack_from("<II", threshold, 12), (8, 1024))
        self.assertEqual(struct.unpack_from("<I", threshold, 28)[0], 1)
        self.assertEqual(len(threshold), 148 + 4096)
        flat = fast._ef.cliff_contact_maps(height_png(np.full((2, 2), 1024, dtype=np.uint16)))[0]
        self.assertNotEqual(coverage, flat)
        self.assertEqual((coverage, threshold), fast._ef.cliff_contact_maps(source))

    def test_bad_or_non_power_of_two_sources_are_rejected(self):
        for source in (b"invalid png", height_png(np.zeros((3, 4), dtype=np.uint8))):
            with self.assertRaises(ValueError):
                fast._ef.cliff_contact_maps(source)
