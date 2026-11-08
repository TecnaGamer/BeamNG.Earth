"""Packaged cliff bindings and authoring inputs survive asset renames/pruning."""

import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from cliff_assets import read_bc5_normal
import bake_cliff_roughness as roughness


class CliffAssetTest(unittest.TestCase):
    def test_every_local_material_texture_exists(self):
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for child in value.values():
                    yield from strings(child)
            elif isinstance(value, list):
                for child in value:
                    yield from strings(child)

        for filename in ('cliff.materials.json', 'cliff_contact.materials.json'):
            materials = json.loads((roughness.ASSETS / filename).read_text())
            for path in strings(materials):
                if path.startswith('/levels/beamng_earth/'):
                    self.assertTrue((roughness.MOD / path.lstrip('/')).is_file(), path)

    def test_roughness_rebake_uses_the_approved_sources_names_and_seeds(self):
        records = json.loads(roughness.AUDIT_PATH.read_text())['files']
        seeds = roughness.noise_seeds()
        for record in records:
            source = roughness.OUT / record['source']
            self.assertTrue(source.is_file(), source)
            self.assertEqual(roughness.roughness_filename(source, record['profile']), record['file'])
            self.assertEqual(seeds[source.name], record['noise_seed'])
            self.assertIsInstance(record['noise_seed'], int)

    def test_cooked_normal_rejects_partial_wrong_format_or_wrong_dimensions(self):
        # A 4x4 BC5 texture has 4x4, 2x2 and 1x1 mips, one block apiece.
        data = bytearray(148 + 3 * 16)
        data[:4] = b'DDS '
        data[84:88] = b'DX10'
        struct.pack_into('<II', data, 12, 4, 4)
        struct.pack_into('<I', data, 28, 3)
        struct.pack_into('<I', data, 128, 83)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'normal.dds'
            path.write_bytes(data)
            self.assertEqual(read_bc5_normal(path, 4, 4), data)
            with self.assertRaises(ValueError):
                read_bc5_normal(path, 8, 4)
            for broken in (data[:30], data[:-1], data + b'x'):
                path.write_bytes(broken)
                with self.assertRaises(ValueError):
                    read_bc5_normal(path, 4, 4)
            struct.pack_into('<I', data, 128, 71)  # BC1 is not a two-channel normal.
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                read_bc5_normal(path, 4, 4)


if __name__ == '__main__':
    unittest.main()
