"""Shipped cliff sources retain authored detail and use the install DDS pipeline."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
import json
from pathlib import Path
import struct
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "mod/texture_sources"
CLIFF = "levels/beamng_earth/art/shapes/beamngEarth/cliff/"


class CliffAssetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manifest = json.loads((SOURCES / "textures.json").read_text(encoding="utf-8"))["textures"]
        cls.entries = {path: value for path, value in manifest.items() if path.startswith(CLIFF)}

    def test_authored_sources_are_lossless_and_have_no_runtime_derivation(self):
        self.assertTrue(self.entries)
        for path, entry in self.entries.items():
            with self.subTest(path=path):
                source = SOURCES / entry["source"]
                self.assertFalse(entry.get("derive"), "authored PBR must not be replaced by color-derived maps")
                # Read RIFF chunk headers only; source maps can be many megabytes.
                with source.open("rb") as stream:
                    self.assertEqual(stream.read(4), b"RIFF")
                    stream.read(4)
                    self.assertEqual(stream.read(4), b"WEBP")
                    chunks = []
                    while header := stream.read(8):
                        self.assertEqual(len(header), 8)
                        kind, size = struct.unpack("<4sI", header)
                        chunks.append(kind)
                        stream.seek(size + size % 2, 1)
                self.assertIn(b"VP8L", chunks)
                self.assertNotIn(b"VP8 ", chunks)
                expected = "BC5" if entry["role"] == "normal" else "BC7_SRGB" if entry["role"] == "color" else "BC4"
                self.assertEqual(entry["format"], expected)

    def test_bundled_cliff_maps_use_compact_resolutions(self):
        for path, entry in self.entries.items():
            name = Path(path).name
            with Image.open(SOURCES / entry["source"]) as image:
                limit = 2048 if entry["role"] == "color" else 1024
                self.assertLessEqual(max(image.size), limit, name)
                if name not in {"rock_grain.normal.dds", "terrain_rim.data.dds"} and "_far" not in name:
                    self.assertEqual(image.size, (limit, limit), name)

    def test_private_rock_detail_paths_match_runtime_cache(self):
        import cliffstock
        materials = json.loads((ROOT / "mod/beamng_earth" / CLIFF).parent.joinpath("cliff_materials.src.json").read_text())
        references = [value for material in materials.values() for stage in material.get("Stages", [])
                      for value in stage.values() if isinstance(value, str) and "/cliff_ground_v" in value]
        self.assertTrue(references)
        for value in references:
            self.assertIn(value, {"/temp/beamngEarth/shapes/cliff_ground_v1/" + name for name in
                                  ("detail.data.dds", "normal.dds", "ao.data.dds", "roughness.data.dds")})
        client = (ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/clifftalus.lua").read_text()
        self.assertIn(cliffstock.MODEL_ROOT, client)

    def test_no_obsolete_or_stock_derived_cliff_files_are_shipped(self):
        folder = SOURCES / CLIFF
        actual = {p.relative_to(SOURCES).as_posix() for p in folder.rglob("*.webp")}
        self.assertEqual(actual, {e["source"] for e in self.entries.values()})
        self.assertFalse(list(folder.rglob("*.dds")))
        self.assertFalse(list((ROOT / "mod/beamng_earth" / CLIFF).glob("*.dds")))
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix != ".md":
                self.assertFalse(any(marker in path.name for marker in ("t_dirt_rocky", "ground_detail", "contact_")), path)


if __name__ == "__main__":
    unittest.main()
