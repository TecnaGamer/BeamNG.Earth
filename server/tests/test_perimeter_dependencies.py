"""Dependency isolation and evidence invalidation must hold in source and frozen apps."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import perimeterevidence

ROOT = Path(__file__).resolve().parents[2]


class PerimeterDependenciesTest(unittest.TestCase):
    def test_cache_key_without_geometry_runtime(self):
        code = '''
import importlib.abc, sys
class BlockGeometry(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('shapely', 'numpy', 'facilityperimeters'):
            raise AssertionError('cache identity imported geometry: ' + fullname)
sys.meta_path.insert(0, BlockGeometry())
import revisit
assert revisit.key('fartex', 1, 2)
assert 'facilityperimeters' not in sys.modules
'''
        result = subprocess.run([sys.executable, '-S', '-c', code], cwd=ROOT / 'server',
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_evidence_paths_and_bytes_preserve_identity(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            os.environ.pop('BNE_PERIMETER_OVERRIDES', None)
            evidence = Path(directory) / 'evidence.geojson'
            constraints = Path(directory) / 'constraints.geojson'
            override = Path(directory) / 'override.geojson'
            evidence.write_bytes(b'evidence')
            initial = perimeterevidence.signature(evidence, constraints)
            constraints.write_bytes(b'constraints')
            constrained = perimeterevidence.signature(evidence, constraints)
            self.assertNotEqual(initial, constrained)
            override.write_bytes(b'override')
            os.environ['BNE_PERIMETER_OVERRIDES'] = str(override)
            expected = hashlib.sha256()
            for path in (evidence, constraints, override):
                expected.update(str(path).encode())
                expected.update(path.read_bytes())
            self.assertEqual(perimeterevidence.signature(evidence, constraints), expected.hexdigest())
            before = perimeterevidence.signature(evidence, constraints)
            override.write_bytes(b'changed')
            self.assertNotEqual(before, perimeterevidence.signature(evidence, constraints))
            override.unlink()
            with self.assertRaises(FileNotFoundError):
                perimeterevidence.signature(evidence, constraints)

    def test_geometry_runtime_and_shared_signature(self):
        import facilityperimeters
        import fencenetwork
        import authoritativeperimeters
        import shapely
        self.assertGreaterEqual(int(shapely.__version__.split('.')[0]), 2)
        self.assertEqual(facilityperimeters.evidence_signature(), perimeterevidence.signature())
        self.assertFalse(shapely.union_all([shapely.LineString([(0, 0), (1, 1)])]).is_empty)

    def test_source_and_frozen_payloads_include_geometry_and_snapshots(self):
        sys.path.insert(0, str(ROOT))
        from tools import build_app, package
        from unittest.mock import patch
        snapshots = ('perimeter_evidence.geojson', 'perimeter_constraints.geojson',
                     'warren_outer_boundary.json')
        payload = set(package.collect())
        for name in snapshots:
            self.assertIn('server/' + name, payload)
        self.assertIn('server/perimeterevidence.py', payload)
        self.assertIn('server/requirements.txt', payload)
        with tempfile.TemporaryDirectory() as work, patch.object(build_app.subprocess, 'run') as run:
            build_app.pyinstaller(work, 'icon.ico', onefile=True, ver='0.0.0')
        command = run.call_args.args[0]
        collected = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == '--collect-all']
        self.assertIn('shapely', collected)
        data = [command[i + 1].split(build_app.SEP, 1)[0]
                for i, arg in enumerate(command[:-1]) if arg == '--add-data']
        for name in snapshots:
            self.assertIn(name, {Path(path).name for path in data})


if __name__ == '__main__':
    unittest.main()
