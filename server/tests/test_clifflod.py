"""Cliff LOD reduction must preserve boundaries, charts and disconnected faces."""
import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cdae, clifflod
from cliffrocks import Mesh


def surface(n=49):
    y,x=np.mgrid[:n,:n]*.5
    p=np.stack((x,y,y*2+.1*np.sin(x)),axis=-1).reshape(-1,3)
    i=np.arange(n*n).reshape(n,n)
    a,b,c,d=i[:-1,:-1].ravel(),i[:-1,1:].ravel(),i[1:,:-1].ravel(),i[1:,1:].ravel()
    t=np.concatenate((np.stack((a,b,c),1),np.stack((b,d,c),1)))
    colors=np.full((len(p),4),180,np.uint8); colors[:,3]=255
    material=np.where(p[t].mean(axis=1)[:,0]<n*.25,0,2)
    return Mesh(p,t,p[:,:2]/91.44,colors,cdae.face_normal_sum(p,t),tri_materials=material)


def boundary(p,t):
    e,c=np.unique(clifflod.edges(t),axis=0,return_counts=True)
    return {tuple(v) for v in p[np.unique(e[c==1])]},c.max()


class CliffLodTest(unittest.TestCase):
    def test_reduction_preserves_outline_material_seam_and_chart(self):
        rock=surface()
        levels=clifflod.levels(rock)
        self.assertGreaterEqual(len(levels),2)
        for _,coarse in levels:
            self.assertLess(len(coarse['tris']),len(rock.tris)*.75)
            np.testing.assert_allclose(coarse['uvs'],coarse['positions'][:,:2]/91.44,atol=1e-12)
            self.assertEqual(set(coarse['tri_materials']),{0,2})
            self.assertTrue(np.isfinite(coarse['normals']).all())
            b,count=boundary(coarse['positions'],coarse['tris'])
            self.assertEqual(b,boundary(rock.positions,rock.tris)[0])
            self.assertLessEqual(count,2)

    def test_opposing_faces_and_holes_never_weld(self):
        rock=surface(35)
        center=rock.positions[rock.tris].mean(axis=1)
        keep=~((center[:,0]>5)&(center[:,0]<9)&(center[:,1]>5)&(center[:,1]<9))
        rock.tris=rock.tris[keep]; rock.tri_materials=rock.tri_materials[keep]
        p=rock.positions.copy(); p[:,2]+=.2
        paired=Mesh(np.concatenate((rock.positions,p)),np.concatenate((rock.tris,rock.tris[:,::-1]+len(p))),
                    np.tile(rock.uvs,(2,1)),np.tile(rock.colors,(2,1)),
                    np.concatenate((rock.normals,-rock.normals)),tri_materials=np.tile(rock.tri_materials,2))
        coarse=clifflod.simplify(paired,6)
        self.assertLess(len(coarse['tris']),len(paired.tris))
        self.assertEqual(boundary(paired.positions,paired.tris)[0],boundary(coarse['positions'],coarse['tris'])[0])
        nn=coarse['normals'][coarse['tris']]
        self.assertTrue(((nn[:,:,2]>0).all(axis=1)|(nn[:,:,2]<0).all(axis=1)).all())

    def test_tiny_mesh_stays_single_detail(self):
        self.assertEqual(clifflod.levels(surface(3)),[])


class LoadTimeThresholdTest(unittest.TestCase):
    def test_only_detail_size_bytes_change(self):
        rock = surface(25)
        data = cdae.encode(rock.positions, rock.tris, uvs=rock.uvs, normals=rock.normals,
                           colors=rock.colors, material=["rock", "unused", "cut"],
                           tri_materials=rock.tri_materials, split_meshes=True,
                           lods=clifflod.levels(rock))
        original = cdae.decode(data)
        self.assertGreater(original["details"][0], 1)
        for level, scale in clifflod.SCALES.items():
            adjusted = cdae.scale_lod_thresholds(data, scale)
            decoded = cdae.decode(adjusted)
            self.assertEqual(len(data), len(adjusted))
            old = original["details"][2]
            new = decoded["details"][2]
            for i in range(original["details"][0]):
                a, b = i*52, (i+1)*52
                self.assertEqual(old[a:a+12], new[a:a+12])
                self.assertEqual(old[a+16:b], new[a+16:b])
            self.assertEqual(old[-52:], new[-52:], "final cull level stays intact")
            decoded["details"] = original["details"]
            self.assertEqual(decoded, original, "all geometry, collision, attributes and indices unchanged")
        self.assertEqual(cdae.scale_lod_thresholds(data, 1), data)

    def test_legacy_and_collision_shapes_unchanged(self):
        rock = surface(3)
        for collision in (False, True):
            data = cdae.encode(rock.positions, rock.tris, material="rock", collision_only=collision)
            self.assertEqual(cdae.scale_lod_thresholds(data, 2), data)

    def test_invalid_scale(self):
        for scale in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                cdae.scale_lod_thresholds(b"", scale)

if __name__ == "__main__":
    unittest.main()
