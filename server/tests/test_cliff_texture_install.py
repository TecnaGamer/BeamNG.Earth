"""Cliff opt-ins affect installation before source reads or native conversion."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import modtextures
from tools.import_cliff_textures import texture_requirements


class CliffTextureInstallTest(unittest.TestCase):
    def test_disabled_maps_need_neither_source_files_nor_native_encoder(self):
        entry = {"source": "missing.webp", "format": "BC5", "role": "normal",
                 "requires_any": ["BNE_CLIFFS", "BNE_CLIFF_ROAD_CUTS"]}
        with patch.object(modtextures, "native", side_effect=AssertionError("native called")), \
                patch.object(modtextures, "_sha256_file", side_effect=AssertionError("source read")):
            self.assertEqual(modtextures.plan(entries={"cliff.dds": entry}, env={}), {})

    def test_all_flag_combinations_prepare_only_the_required_maps(self):
        entries = modtextures.manifest()
        cliffs = {path: e for path, e in entries.items() if "/cliff/" in path}
        self.assertEqual(len(cliffs), 27)
        with patch.object(modtextures, "_sha256_file", return_value="test-source"):
            plans = {}
            for natural in (False, True):
                for cuts in (False, True):
                    env = {"BNE_CLIFFS": str(int(natural)), "BNE_CLIFF_ROAD_CUTS": str(int(cuts))}
                    plans[natural, cuts] = modtextures.plan(entries=cliffs, env=env)
            self.assertFalse(plans[False, False])
            self.assertEqual(set(plans[True, True]), set(cliffs))
            self.assertEqual(set(plans[True, True]), set(plans[True, False]) | set(plans[False, True]))
            for path in plans[True, False]:
                self.assertNotIn("limestone", path)
            for path in plans[False, True]:
                if not path.endswith("/natural_base.color.dds"):
                    self.assertFalse(any(name in path for name in ("natural_", "canyon_", "arid_far")))
            shared = set(plans[True, False]) & set(plans[False, True])
            self.assertTrue(any("arid_near" in path for path in shared))
            self.assertTrue(any(path.endswith("/natural_base.color.dds") for path in shared))
            for path in shared:
                self.assertEqual(plans[True, False][path]["key"], plans[False, True][path]["key"])

    def test_manifest_requirements_follow_actual_material_references(self):
        path = ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff.materials.json"
        expected = texture_requirements(json.loads(path.read_text(encoding="utf-8")))
        entries = modtextures.manifest()
        for path, flags in expected.items():
            self.assertEqual(entries[path]["requires_any"], flags, path)

    def test_changing_flags_invalidates_installed_set_but_reuses_shared_dds(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("L", (4, 4), 128).save(root / "source.webp", lossless=True)
            entries = {
                name + ".dds": {"source": "source.webp", "format": "BC4", "role": "data", "requires_any": flags}
                for name, flags in (("natural", ["BNE_CLIFFS"]), ("cuts", ["BNE_CLIFF_ROAD_CUTS"]),
                                    ("shared", ["BNE_CLIFFS", "BNE_CLIFF_ROAD_CUTS"]))}
            natural = modtextures.plan(root, entries, env={"BNE_CLIFFS": "1"})
            cuts = modtextures.plan(root, entries, env={"BNE_CLIFF_ROAD_CUTS": "1"})
            encoded, reused, _ = modtextures.build(root, natural, threads=1)
            self.assertEqual((encoded, reused), (2, 0))
            self.assertTrue(modtextures.current(root, natural))
            self.assertFalse(modtextures.current(root, cuts))
            keep = modtextures.reusable(root, cuts)
            self.assertEqual(keep, {"shared.dds"})
            encoded, reused, _ = modtextures.build(root, cuts, keep, threads=1)
            self.assertEqual((encoded, reused), (1, 1))
            self.assertTrue(modtextures.current(root, cuts))
