"""Streaming pieces preserve the exact cliff surface and attribute seams for every rock family."""
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import numpy as np
import cdae, cliffrocks, clifflod, cliffmaterial, fast, roads
from test_clifflod import surface, boundary

class CliffStreamTest(unittest.TestCase):
    def test_parts_cover_every_face_with_exact_attributes(self):
        rock = surface(37)
        rock.uvs2 = rock.uvs * 3 + .13
        rock.colors[:,0] = np.arange(len(rock.positions)) % 255
        rock.tri_materials = np.arange(len(rock.tris)) % len(cliffmaterial.MATERIALS)
        with patch.object(cliffrocks, 'STREAM_TRIANGLES', 256):
            parts = list(cliffrocks.stream_parts(rock))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p.tris) <= 256 for p in parts))
        source = {tuple(v): i for i,v in enumerate(rock.positions)}
        actual = []
        for part in parts:
            ids = np.array([source[tuple(v)] for v in part.positions])
            for name in ('normals','uvs','uvs2','colors'):
                np.testing.assert_array_equal(getattr(part,name),getattr(rock,name)[ids])
            actual.extend(zip(map(tuple,ids[part.tris]),part.tri_materials))
            self.assertGreater(part.lod_scale,0)
            self.assertLess(part.lod_scale,1)
            for _, lod in clifflod.levels(part):
                self.assertEqual(boundary(part.positions,part.tris)[0],boundary(lod['positions'],lod['tris'])[0])
        self.assertEqual(sorted(actual),sorted(zip(map(tuple,rock.tris),rock.tri_materials)))

    def test_small_contour_growth_does_not_double_batch_count(self):
        rock=surface(37)
        p=np.ascontiguousarray(rock.positions,np.float64)
        t=np.ascontiguousarray(rock.tris,np.int64)
        for count in (1024,1025,1100):
            groups=fast._ef.cliff_stream_parts(p,t[:count],128,64)
            self.assertEqual(len(groups),(count+127)//128)
            self.assertTrue(all(len(g)<=128 for g in groups))
            self.assertEqual(sorted(sum(groups,[])),list(range(count)))

    def test_cap_empty_and_invalid_input(self):
        rock = surface(15)
        p,t = np.ascontiguousarray(rock.positions,np.float64),np.ascontiguousarray(rock.tris,np.int64)
        groups = fast._ef.cliff_stream_parts(p,t,1,4)
        self.assertEqual(len(groups),4)
        self.assertEqual(sorted(sum(groups,[])),list(range(len(t))))
        self.assertEqual(groups,fast._ef.cliff_stream_parts(p,t,1,4))
        self.assertEqual(fast._ef.cliff_stream_parts(p,t[:0],10,4),[])
        for bad in (np.array([[-1,0,1]]),np.array([[0,1,len(p)]])):
            with self.assertRaises(ValueError): fast._ef.cliff_stream_parts(p,bad.astype(np.int64),10,4)
        with self.assertRaises(ValueError): fast._ef.cliff_stream_parts(p,t,0,4)

    def test_small_legacy_mesh_and_all_material_families(self):
        rock = surface(7)
        rock.uvs2 = rock.uvs.copy()
        part, = cliffrocks.stream_parts(rock)
        self.assertEqual(part.lod_scale,1)
        for family in range(len(cliffmaterial.MATERIALS)):
            part.tri_materials = np.full(len(part.tris),family)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp,'piece.cdae')
                cliffrocks.write(str(path),part)
                shape = cdae.decode(path.read_bytes())
                self.assertEqual([m['name'] for m in shape['materials']],[cliffmaterial.MATERIALS[family]])
                self.assertEqual(shape['meshes'][0]['tverts2'][0],len(part.positions))

    def test_weak_lod_reduction_is_not_serialized(self):
        rock = surface(25)
        with patch.object(clifflod,'simplify',return_value={'tris':rock.tris[:int(len(rock.tris)*.94)]}):
            self.assertEqual(clifflod.levels(rock),[])

    def test_all_piece_paths_are_declared(self):
        files = roads.shape_files(1,2,3)
        self.assertEqual(len([k for k in files if k.startswith('cliff') and not k.startswith('cliffStrip')]),cliffrocks.STREAM_PARTS_MAX)
        self.assertTrue(files['cliff'].endswith('cliff_1_2_3.cdae'))
        self.assertTrue(files['cliff64'].endswith('cliff_1_2_3_64.cdae'))

    def test_tile_publishes_every_piece_with_rows_only_once(self):
        import json
        rock = surface(15)
        rock.talus = [['talus_test',1,2,3]]
        with tempfile.TemporaryDirectory() as tmp, patch.object(cliffrocks,'STREAM_TRIANGLES',128):
            roads.write_tile(tmp,'/test',1,2,3,[],cliff_mesh=rock)
            data = json.loads(next(Path(tmp).glob('*.json')).read_text())
            parts = [v for k,v in data.items() if k.startswith('cliff') and k!='cliffDecals']
            self.assertGreater(len(parts),1)
            self.assertTrue(all(Path(tmp,Path(v['shape']).name).is_file() for v in parts))
            self.assertNotIn('cliffDecals',data)
            self.assertEqual(len(data['talus']['items']),1)
