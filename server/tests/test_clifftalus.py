import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import clifftalus as talus
import cliffrocks as cr
import geodesy


class TalusTest(unittest.TestCase):
    def fixture(self, box=(0,0,160,160), classes=None, blocked=None, keep=None, **kwargs):
        x = np.arange(41)*4.
        if keep is None:
            keep = np.zeros((40,40),bool)
            keep[5:35,20:25] = True
        if classes is None: classes = np.full((41,41),60)
        if blocked is None: blocked = np.zeros((40,40),bool)
        def elev(lat,lon): return 20+np.clip(np.asarray(lon)*cr.WORLD_M_PER_DEG-80,0,20)*3
        return talus.rows(geodesy.EnuFrame(0,0),box,x,x,keep,classes,blocked,
                          np.zeros((40,40),int),elev, **kwargs)

    def test_density_favors_stable_ground_below_cliff(self):
        p = talus.probability(np.array((0,25,28,35,44,78,85)),0)
        np.testing.assert_allclose(p[:3],talus.MAX_DENSITY)
        self.assertGreater(p[2],p[3])
        np.testing.assert_array_equal(p[4:],0)
        self.assertEqual(talus.probability(0,talus.REACH_M),0)
        self.assertEqual(self.fixture(keep=np.zeros((40,40),bool)),[])

    def test_deterministic_settled_instances_only_below_cliff(self):
        rows = self.fixture()
        self.assertGreater(len(rows),20)
        self.assertEqual(rows,self.fixture())
        for r in rows:
            self.assertEqual(len(r),17)
            self.assertTrue(r[0].startswith('talus_'))
            self.assertGreater(r[1],80-talus.REACH_M)
            self.assertLess(r[1],80)  # no wall or high plateau/crest rocks
            self.assertGreater(r[2],20-talus.SCATTER_M)
            self.assertLess(r[2],140+talus.SCATTER_M)
            basis = np.array(r[8:]).reshape(3,3)
            np.testing.assert_allclose(basis@basis.T,np.eye(3),atol=2e-6)
            tilt = np.degrees(np.arccos(np.clip(basis[2,2],-1,1)))
            self.assertLessEqual(tilt,talus.MAX_TILT_DEG+.01)
        self.assertTrue(any(r[1]<70 for r in rows))  # downslope runout
        self.assertTrue(any(r[1]>76 for r in rows))  # scattered toe contact
        self.assertGreater(np.std([r[1] for r in rows]),3)

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

    def test_tints_match_canyon_and_classic_cliff_albedo(self):
        import json
        import cliffcolors as cc
        import cliffmaterial as cm
        means = np.asarray(json.loads(Path(cr.__file__).with_name('cliff_transition_means.json').read_text()))
        rgb = np.array([[90,140,80],[110,75,110],[180,170,150],[200,165,115],[0,0,0]])
        biome = np.zeros(5,int)
        for family in (cm.NATURAL,cm.ARID_CUT,cm.CANYON):
            tint = (cc.canyon_tint(biome,rgb) if family==cm.CANYON else cr.rock_colors(biome,rgb,family))[:,:3]
            expected = cc.srgb(cc.linear(tint/255)*means[family])*255
            actual = talus.mineral_tints(biome,rgb,family)
            np.testing.assert_allclose(actual,expected,atol=talus.TINT_STEP/2)
        self.assertFalse(np.array_equal(talus.mineral_tints(biome,rgb,cm.NATURAL),
                                        talus.mineral_tints(biome,rgb,cm.CANYON)))

    def test_adjacent_tiles_own_each_instance_once(self):
        whole = self.fixture()
        left,right = self.fixture((0,0,70,160)),self.fixture((70,0,160,160))
        freeze = lambda rows: {tuple(r) for r in rows}
        self.assertFalse(freeze(left)&freeze(right))
        self.assertEqual(freeze(whole),freeze(left)|freeze(right))

    def test_full_footprint_clears_bends_and_endpoints(self):
        import roads
        corridor = roads._Segs({'bend': (np.array([[0.,0.],[20.,0.],[20.,20.]]), 3.)})
        points = np.array([[10,6,0],[10,9,0],[24,15,0],[24,24,0],[30,30,0]],float)
        # Centres are all off-road, but wide clusters overlap its sides/end cap.
        np.testing.assert_array_equal(talus.road_clear(points,np.full(5,2.),corridor),
                                      [False,True,False,False,True])
        self.assertTrue(talus.road_clear(points[2:3],np.array([.2]),corridor)[0])

    def test_road_check_uses_final_seated_position(self):
        import roads
        corridor = roads._Segs({'road': (np.array([[100.,-100.],[100.,300.]]), 3.)})
        self.assertEqual(self.fixture(),self.fixture(road_segments=corridor))
        def shifted(e,n,z):
            return np.column_stack((np.full_like(e,100.),n,z))
        self.assertGreater(len(self.fixture(skin_at=shifted)),20)
        self.assertEqual(self.fixture(skin_at=shifted,road_segments=corridor),[])

    def test_elevated_access_roads_excluded_but_not_tunnels(self):
        import surface
        from test_roads import FRAME, features
        feats = features([[(0,0),(200,0)]],['unclassified'],widths=[(0,8)],
                         flags=[(0,'is_bridge',.25,.5),(0,'is_tunnel',.65,.85)])
        ground = surface._roads_list_enu(FRAME,[feats],None,None)
        corridor = talus.road_exclusion(FRAME,[feats],None,None,ground)
        points = np.array([[20,0,0],[75,0,0],[150,0,0],[75,20,0]],float)
        np.testing.assert_array_equal(talus.road_clear(points,np.ones(4),corridor),
                                      [False,False,True,True])

    def test_city_road_and_water_exclusions_survive_rubble_growth(self):
        self.assertEqual(self.fixture(blocked=np.ones((40,40),bool)),[])
        for code in (50,80,0):
            self.assertEqual(self.fixture(classes=np.full((41,41),code)),[])
        c = np.full((41,41),60)
        c[20,22] = 50
        self.assertEqual(self.fixture(classes=c),[])


if __name__ == '__main__': unittest.main()
