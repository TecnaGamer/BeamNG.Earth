"""Canyon surface-space samples must retain strata and avoid vertical strips."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cliffdetail as cd, cliffmaterial as cm, cliffrocks as cr, clifflod, cdae


class CanyonDetailTest(unittest.TestCase):
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
