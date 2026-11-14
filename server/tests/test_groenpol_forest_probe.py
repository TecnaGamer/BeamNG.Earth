import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'server')]
import build_groenpol_forest_probe as producer
import cdae


class ForestProbeAssets(unittest.TestCase):
    def test_authored_body_and_optics_are_small_reproducible_static_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            a,b=Path(folder)/'a',Path(folder)/'b'
            first=producer.build(a);second=producer.build(b)
            self.assertEqual(first,second)
            self.assertEqual(first['license'],'CC-BY-SA-4.0')
            self.assertEqual(first['files']['head_dark.cdae']['triangles'],432)
            for name in ('red','amber','green'):
                self.assertEqual(first['files'][f'lens_{name}.cdae']['triangles'],16)
            for name in first['files']:
                self.assertEqual((a/name).read_bytes(),(b/name).read_bytes())
            materials=json.loads((a/'main.materials.json').read_text())
            for mat in materials.values():
                for stage in mat['Stages']:
                    self.assertNotIn('colorPaletteMap',stage)
                    self.assertFalse(stage.get('instanceEmissive',False))
            self.assertTrue((a/'LICENSE.md').is_file())
            with self.assertRaises(ValueError):producer.build(a)

    def test_300mm_uses_the_supplied_larger_lens_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            a,b=Path(folder)/'200',Path(folder)/'300'
            producer.build(a,'vehicle');producer.build(b,'vehicle_300')
            def width(path):
                shape=cdae.read(path)
                p=cdae.mesh_arrays(shape['meshes'][0])['verts']
                return np.ptp(p[:,1])
            # The supplied FBX has micrometre-level mesh rounding; preserve it.
            self.assertAlmostEqual(float(width(b/'lens_green.cdae')/width(a/'lens_green.cdae')),1.5,delta=1e-3)


if __name__=='__main__':unittest.main()
