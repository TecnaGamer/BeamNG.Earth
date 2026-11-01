import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import clifftalus as talus
import cliffrocks as cr
import geodesy


class TalusTest(unittest.TestCase):
    def fixture(self, box=(0,0,160,160), classes=None, blocked=None, keep=None):
        x = np.arange(41)*4.
        if keep is None:
            keep = np.zeros((40,40),bool)
            keep[5:35,20:25] = True
        if classes is None: classes = np.full((41,41),60)
        if blocked is None: blocked = np.zeros((40,40),bool)
        def elev(lat,lon): return np.asarray(lon)*cr.WORLD_M_PER_DEG*np.tan(np.radians(48))+20
        return talus.rows(geodesy.EnuFrame(0,0),box,x,x,keep,classes,blocked,
                          np.zeros((40,40),int),elev)

    def test_density_needs_high_slope_and_nearby_cliff(self):
        p = talus.probability(np.array((0,25,39.9,40,44,48,52,78,85)),0)
        np.testing.assert_array_equal(p[:4],0)
        self.assertTrue((np.diff(p[3:7]) > 0).all())
        np.testing.assert_array_equal(p[7:],0)
        self.assertEqual(talus.probability(52,13),0)
        self.assertEqual(self.fixture(keep=np.zeros((40,40),bool)),[])

    def test_deterministic_slope_aligned_instances_only_at_cliff_rim(self):
        rows = self.fixture()
        self.assertGreater(len(rows),20)
        self.assertEqual(rows,self.fixture())
        for r in rows:
            self.assertEqual(len(r),17)
            self.assertTrue(r[0].startswith('talus_'))
            self.assertGreater(r[1],64)
            self.assertLess(r[1],116)
            basis = np.array(r[8:]).reshape(3,3)
            np.testing.assert_allclose(basis@basis.T,np.eye(3),atol=2e-6)
            tilt = np.degrees(np.arccos(basis[2,2]))
            self.assertLessEqual(tilt,talus.MAX_TILT_DEG)
            self.assertGreater(tilt,10)

    def test_seating_samples_faded_skin_instead_of_full_displacement(self):
        xs = np.arange(9)*.5
        h = np.tile(xs*2,(9,1))
        keep = np.ones((8,8),bool)
        surface,_,_ = cr.displaced_surface(h,keep,xs,xs,np.zeros((8,8),int))
        sample = talus.surface_sampler(xs,xs,h,surface)
        # The buried boundary sits on terrain, never at the full interior offset.
        rim = sample(np.array([0.]),np.array([2.]),np.array([0.]))[0]
        np.testing.assert_allclose(rim,(0,2,0),atol=1e-10)
        center = sample(np.array([2.]),np.array([2.]),np.array([4.]))[0]
        np.testing.assert_allclose(center,surface[4,4],atol=1e-10)
        self.assertGreater(center[2],4)
        self.assertLess(center[0],2)
        # Triangle interpolation matches the rendered A-B-C diagonal exactly.
        offset = surface-np.stack((*np.meshgrid(xs,xs),h),axis=-1)
        offset[...,2] = np.maximum(offset[...,2],0)
        p = sample(np.array([1.4]),np.array([1.1]),np.array([2.8]))[0]
        expected = np.array([1.4,1.1,2.8])+.2*offset[2,2]+.6*offset[2,3]+.2*offset[3,3]
        np.testing.assert_allclose(p,expected,atol=1e-10)

    def test_tints_cannot_inherit_green_or_magenta_satellite_casts(self):
        rgb = np.array([[90,140,80],[110,75,110],[180,170,150],[200,165,115],[0,0,0]])
        tints = talus.mineral_tints(np.zeros(5,int),rgb)
        # Green lies between red and blue; no independent green/magenta bucket.
        np.testing.assert_allclose(tints[:,1],(tints[:,0]+tints[:,2])/2,atol=1)
        self.assertLessEqual(np.max(np.ptp(tints,axis=1)),80)
        self.assertFalse(np.array_equal(tints[2],tints[3]))

    def test_adjacent_tiles_own_each_instance_once(self):
        whole = self.fixture()
        left,right = self.fixture((0,0,88,160)),self.fixture((88,0,160,160))
        freeze = lambda rows: {tuple(r) for r in rows}
        self.assertFalse(freeze(left)&freeze(right))
        self.assertEqual(freeze(whole),freeze(left)|freeze(right))

    def test_city_road_and_water_exclusions_survive_rubble_growth(self):
        self.assertEqual(self.fixture(blocked=np.ones((40,40),bool)),[])
        for code in (50,80,0):
            self.assertEqual(self.fixture(classes=np.full((41,41),code)),[])
        c = np.full((41,41),60)
        c[20,22] = 50
        self.assertEqual(self.fixture(classes=c),[])


if __name__ == '__main__': unittest.main()
