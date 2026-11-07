"""Collision proxies retain contact boundaries while rendering remains byte-identical."""
import sys, tempfile, unittest
from pathlib import Path
from dataclasses import replace
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae, cliffrocks, cliffcollision, clifflod, fast
from test_clifflod import surface, boundary

class CliffCollisionTest(unittest.TestCase):
    def test_boundary_materials_and_original_vertices(self):
        rock = surface()
        coarse = cliffcollision.build(rock)
        self.assertIsNotNone(coarse)
        self.assertLess(len(coarse['tris']), len(rock.tris)*.4)
        originals = {tuple(v) for v in rock.positions}
        self.assertTrue(all(tuple(v) in originals for v in coarse['positions']))
        for mat in np.unique(rock.tri_materials):
            a = boundary(rock.positions, rock.tris[rock.tri_materials==mat])[0]
            b = boundary(coarse['positions'], coarse['tris'][coarse['tri_materials']==mat])[0]
            self.assertEqual(a,b)

    def test_hole_and_disconnected_opposing_surfaces(self):
        rock = surface(35)
        center = rock.positions[rock.tris].mean(axis=1)
        keep = ~((center[:,0]>5)&(center[:,0]<9)&(center[:,1]>5)&(center[:,1]<9))
        t = rock.tris[keep]
        p = rock.positions.copy(); p[:,2] += .2
        paired = replace(rock, positions=np.concatenate((rock.positions,p)),
            tris=np.concatenate((t,t[:,::-1]+len(p))),tri_materials=None)
        coarse = cliffcollision.build(paired)
        self.assertIsNotNone(coarse)
        self.assertEqual(boundary(paired.positions,paired.tris)[0],boundary(coarse['positions'],coarse['tris'])[0])
        # Every proxy face belongs entirely to one original sheet.
        upper = {tuple(v) for v in p}
        sheet = np.array([tuple(v) in upper for v in coarse['positions']])
        self.assertTrue(np.all(np.ptp(sheet[coarse['tris']].astype(int),axis=1)==0))

    def test_sharp_fracture_and_touching_faces_remain_exact(self):
        rock = surface(35)
        rock.positions[:,2] = abs(rock.positions[:,0]-8.5)*3
        locks = cliffcollision.fracture_locks(rock.positions,rock.tris)
        self.assertGreater(locks.sum(),0)
        coarse = cliffcollision.build(rock)
        self.assertIsNotNone(coarse)
        expected = {tuple(sorted(map(tuple, rock.positions[t]))) for t in rock.tris if locks[t].all()}
        actual = {tuple(sorted(map(tuple, coarse['positions'][t]))) for t in coarse['tris']}
        self.assertTrue(expected <= actual)

    def test_export_preserves_every_render_mesh_and_lod(self):
        rock = surface()
        with tempfile.TemporaryDirectory() as tmp:
            a,b = Path(tmp)/'exact.cdae',Path(tmp)/'proxy.cdae'
            cliffrocks.write(str(a),rock)
            cliffrocks.write(str(b),rock,cliffcollision.build(rock))
            exact,proxy = cdae.read(a),cdae.read(b)
        count = len(exact['meshes'])
        self.assertEqual(proxy['meshes'][:count],exact['meshes'])
        self.assertEqual([m['type'] for m in proxy['meshes'][count:]], [4]*(count+1)+[0])
        self.assertEqual(proxy['smallestVisibleDL'],exact['smallestVisibleDL'])
        self.assertEqual(proxy['materials'],exact['materials'])

    def test_protocol_advertises_only_shapes_with_proxy_collision(self):
        import roads, json
        from unittest.mock import patch
        for enabled in (False, True):
            with tempfile.TemporaryDirectory() as tmp, patch.object(cliffcollision,'ENABLED',enabled):
                roads.write_tile(tmp,'/test',1,2,3,[],cliff_mesh=surface(25))
                data=json.loads(next(Path(tmp).glob('*.json')).read_text())
                self.assertEqual(data['cliff'].get('collisionMesh',False),enabled)
                shape=cdae.read(Path(tmp,Path(data['cliff']['shape']).name))
                self.assertEqual('Collision-1' in shape['names'],enabled)

    def test_small_mesh_and_invalid_native_inputs(self):
        self.assertIsNone(cliffcollision.build(surface(3)))
        p=np.zeros((3,3));t=np.array([[0,1,3]],np.int64);locks=np.zeros(3,np.uint8)
        with self.assertRaises(ValueError):fast._ef.cliff_collision_indices(p,t,locks,1,.08)
        with self.assertRaises(ValueError):fast._ef.cliff_collision_indices(p,np.array([[0,1,2]],np.int64),locks,1,float('nan'))
        p[0,0]=np.inf
        with self.assertRaises(ValueError):fast._ef.cliff_collision_indices(p,np.array([[0,1,2]],np.int64),locks,1,.08)

if __name__ == '__main__':unittest.main()
