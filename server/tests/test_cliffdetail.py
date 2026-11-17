"""Canyon surface-space samples must retain strata and avoid vertical strips."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cliffdetail as cd, cliffmaterial as cm, cliffrocks as cr, clifflod, cdae


class CanyonDetailTest(unittest.TestCase):
    def test_native_boundary_weights_match_open_edges_and_material_seams(self):
        import fast
        from collections import Counter
        rng = np.random.default_rng(42)
        for _ in range(20):
            triangles = rng.integers(0, 15, (20, 3), dtype=np.int64)
            material = rng.integers(0, 2, 20, dtype=np.int32)
            edges = Counter(tuple(sorted((int(a), int(b)))) for face in triangles
                for a, b in ((face[0],face[1]),(face[1],face[2]),(face[2],face[0])))
            locked = {v for edge,count in edges.items() if count != 2 for v in edge}
            locked.update(int(v) for face,m in zip(triangles,material) if m != 1 for v in face)
            expected = np.array([[0. if i in locked else 1.] for i in range(15)])
            actual = fast._ef.cliff_detail_weights(triangles,material,15,1)
            np.testing.assert_array_equal(actual,expected)
        with self.assertRaises(ValueError):
            fast._ef.cliff_detail_weights(triangles+15,material,15,1)

    def test_native_displacement_respects_ground_boundaries_and_winding(self):
        import fast
        p = np.array([[0.,0.,0.], [1.,0.,0.], [0.,1.,0.]])
        t = np.array([[0,1,2]], np.int64)
        n = np.tile([0.,0.,-2.],(3,1))
        weight, sampled, coarse = np.ones(3), np.ones(3), np.zeros(3)
        for ground in (lambda q: np.zeros(len(q)), lambda q: np.full(len(q),np.nan)):
            candidate, normal = fast._ef.cliff_detail_displace(p,t,n,weight,sampled,coarse,True,ground)
            np.testing.assert_array_equal(candidate,p)
            np.testing.assert_array_equal(normal,np.tile([0.,0.,1.],(3,1)))
        candidate, normal = fast._ef.cliff_detail_displace(p,t,n,np.zeros(3),sampled,coarse,True,None)
        np.testing.assert_array_equal(candidate,p)
        np.testing.assert_array_equal(normal,np.tile([0.,0.,-1.],(3,1)))
        opposing = np.array([[1.,0.,0.],[-1.,0.,0.],[0.,1.,0.]])
        candidate, _ = fast._ef.cliff_detail_displace(p,t,opposing,weight,sampled*2,coarse,True,None)
        self.assertGreaterEqual(np.cross(candidate[1]-candidate[0],candidate[2]-candidate[0])[2],.2)
        with self.assertRaises(ValueError):
            fast._ef.cliff_detail_displace(p,t,n,weight,sampled[:2],coarse,True,None)
        with self.assertRaises(ValueError):
            fast._ef.cliff_detail_displace(p,t,n,weight,sampled*np.nan,coarse,True,None)

    def test_native_subdivision_handles_empty_and_invalid_inputs(self):
        import fast
        p = np.array([[0.,0.,0.], [1.,0.,4.], [0.,1.,0.]])
        t = np.array([[0,1,2]], np.int64)
        a = np.arange(6, dtype=float).reshape(3,2)
        m = np.array([cm.NATURAL], np.int32)
        actual = fast._ef.cliff_detail_subdivide(p,t,a,m,cm.CANYON)
        for expected, found in zip((p,t,a,m), actual):
            np.testing.assert_array_equal(found, expected)
        for pp,tt,aa,mm in ((p,t+3,a,m),(p,t-3,a,m),(p,t,a[:-1],m),
                           (p,t,a,m[:0]),(p*np.nan,t,a,m)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_detail_subdivide(pp,tt,aa,mm,cm.CANYON)
        empty = fast._ef.cliff_detail_subdivide(np.empty((0,3)),np.empty((0,3),np.int64),
            np.empty((0,2)),np.empty(0,np.int32),cm.CANYON)
        self.assertEqual([v.shape for v in empty],[(0,3),(0,3),(0,2),(0,)])

    def wall(self):
        y,x = np.mgrid[:9,:9].astype(float)
        p = np.stack((x,y*.2,y*8),axis=-1).reshape(-1,3)
        i = np.arange(len(p)).reshape(x.shape)
        a,b,c,d = i[:-1,:-1].ravel(),i[:-1,1:].ravel(),i[1:,1:].ravel(),i[1:,:-1].ravel()
        t = np.concatenate((np.stack((a,b,c),1),np.stack((a,c,d),1)))
        n = cdae.face_normal_sum(p,t)
        return cr.Mesh(p,t,np.c_[p[:,0]/cm.WIDTH_M,p[:,2]/cm.CANYON_HEIGHT_M],
            np.full((len(p),4),255,np.uint8),n,tri_materials=np.full(len(t),cm.CANYON))

    def test_vertical_samples_resolve_layers_without_cracks(self):
        rock = self.wall()
        original = rock.positions.copy()
        # Coarse vertical samples all land on zero: the intermediate ledges
        # must still appear. An endpoint-only heightmap loses them entirely.
        def relief(u,v,**kw):
            return .3*np.sin(np.asarray(v)*cm.CANYON_HEIGHT_M*np.pi/4)
        with patch.object(cm,'geometry_sample',side_effect=relief):
            cd.refine_canyon(rock)
        self.assertGreater(len(rock.positions),len(original))
        e,c = np.unique(clifflod.edges(rock.tris),axis=0,return_counts=True)
        self.assertLessEqual(c.max(),2)
        self.assertLess(np.abs(np.diff(rock.positions[e,2],axis=1)).max(),2.01)
        # Original vertex relief and UV phase stay exact.
        np.testing.assert_allclose(rock.positions[:len(original)],original,atol=1e-5)
        self.assertGreater(np.ptp(rock.positions[:,1]-rock.positions[:,2]/40),.15)
        np.testing.assert_allclose(np.linalg.norm(rock.normals,axis=1),1,atol=1e-5)
        self.assertTrue(np.isfinite(rock.positions).all())
        self.assertTrue(np.all(rock.tri_materials==cm.CANYON))

    def test_full_relief_is_sampled_after_vertical_refinement(self):
        rock=self.wall()
        before=rock.positions.copy()
        def relief(u,v,**kw):
            return .3+.2*np.sin(np.asarray(v)*cm.CANYON_HEIGHT_M*np.pi/4)
        with patch.object(cm,'geometry_sample',side_effect=relief):
            cd.refine_canyon(rock,full_relief=True)
        # It must apply the whole field to original interior vertices too;
        # endpoint residual refinement alone cannot remove baked vertical ribs.
        self.assertGreater(np.linalg.norm(rock.positions[40]-before[40]),.25)
        q=rock.positions
        t=rock.tris
        cross=np.cross(q[t[:,1]]-q[t[:,0]],q[t[:,2]]-q[t[:,0]])
        normal=np.array([0,-40,1])/np.sqrt(1601)
        self.assertGreater((cross@normal).min(),0)
        self.assertLess(abs(np.diff(q[clifflod.edges(t),2],axis=1)).max(),2.01)
        self.assertLess(len(q),len(before)*16)

    def test_refined_relief_cannot_cut_through_ground(self):
        rock = self.wall()
        rock.lod_ground = lambda p: p[:,1]*40-.05
        with patch.object(cm,'geometry_sample',side_effect=lambda u,v,**kw:
                          .7*np.sin(np.asarray(v)*cm.CANYON_HEIGHT_M*np.pi/4)):
            cd.refine_canyon(rock)
        self.assertGreaterEqual(np.min(rock.positions[:,2]-rock.lod_ground(rock.positions)),-1e-4)

    def test_other_material_families_are_unchanged(self):
        rock = self.wall();rock.tri_materials[:]=cm.NATURAL
        before=rock.positions.copy()
        self.assertIs(cd.refine_canyon(rock),rock)
        np.testing.assert_array_equal(rock.positions,before)


if __name__=='__main__':
    unittest.main()
