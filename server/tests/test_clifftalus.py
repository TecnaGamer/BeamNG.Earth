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

    def test_arid_outcrops_are_embedded_across_faces_not_toe_replacements(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        skin=lambda x,y,z: np.column_stack((x,y,z+3*np.sin(x/2)*np.sin(y/2)))
        with patch.object(cm,'natural_family',return_value=cm.ARID_CUT):
            rows=self.fixture(skin_at=skin)
            large=[r for r in rows if r[0].startswith('talus_large_')]
            self.assertGreater(len(large),0)
            self.assertGreater(np.ptp([r[3] for r in large]),15)
            for r in large:
                self.assertTrue(4<=r[6]<=10)
                basis=np.array(r[8:]).reshape(3,3)
                np.testing.assert_allclose(basis@basis.T,np.eye(3),atol=2e-6)
                self.assertGreaterEqual(basis[2,2],np.cos(np.radians(talus.OUTCROP_MAX_TILT_DEG))-.000001)
            with patch.object(talus,'OUTCROP_BURIAL',(0.,0.)):
                unburied=[r for r in self.fixture(skin_at=skin) if r[0].startswith('talus_large_')]
            self.assertEqual(len(large),len(unburied))
            for r,base in zip(large,unburied):
                depth=np.linalg.norm(np.array(base[1:4])-r[1:4])/r[6]
                self.assertGreater(depth,.349)
                self.assertLess(depth,.451)
            without_skin=self.fixture()
            self.assertFalse(any(r[0].startswith('talus_large_') for r in without_skin))
            for family in (cm.CANYON,cm.NATURAL):
                with patch.object(cm,'natural_family',return_value=family):
                    self.assertFalse(any(r[0].startswith('talus_large_') for r in self.fixture(skin_at=skin)))

    def test_smooth_steep_faces_do_not_receive_outcrops(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        plane=lambda x,y,z: np.column_stack((x,y,20+3*(x-80)))
        with patch.object(cm,'natural_family',return_value=cm.ARID_CUT):
            rows=self.fixture(skin_at=plane)
            self.assertTrue(rows)  # loose rubble still exists
            self.assertFalse(any(r[0].startswith('talus_large_') for r in rows))
        x=np.array([10.,20.]);y=np.array([10.,20.]);h=np.array([4.,6.])
        sample=lambda x,y: np.column_stack((x,y,3*x))
        n=np.tile(np.array([-3.,0,1])/np.sqrt(10),(2,1))
        self.assertFalse(talus.rough_face(sample,x,y,sample(x,y),n,h,1).any())
        jagged=lambda x,y: np.column_stack((x,y,3*x+3*np.sin(x/2)*np.sin(y/2)))
        x,y=np.meshgrid(np.arange(4.,60.,2),np.arange(4.,60.,2));x,y=x.ravel(),y.ravel()
        dx=jagged(x+.5,y)-jagged(x-.5,y);dy=jagged(x,y+.5)-jagged(x,y-.5)
        n=np.cross(dx,dy);n/=np.linalg.norm(n,axis=1,keepdims=True)
        selected=talus.rough_face(jagged,x,y,jagged(x,y),n,np.full_like(x,5),1)
        self.assertGreater(float(selected.mean()),.01)
        self.assertLess(float(selected.mean()),.95)

    def test_large_arid_rows_keep_ownership_and_road_clearance(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        import roads
        skin=lambda x,y,z: np.column_stack((x,y,z+3*np.sin(x/2)*np.sin(y/2)))
        with patch.object(cm,'natural_family',return_value=cm.ARID_CUT):
            whole=self.fixture(skin_at=skin)
            left=self.fixture((0,0,90,160),skin_at=skin);right=self.fixture((90,0,160,160),skin_at=skin)
            self.assertEqual({tuple(r) for r in whole},{tuple(r) for r in left+right})
            corridor=roads._Segs({'road':(np.array([[85.,-100.],[85.,300.]]),3.)})
            rows=self.fixture(skin_at=skin,road_segments=corridor)
            self.assertLess(len(rows),len(whole))
            self.assertTrue(all(talus.road_clear(np.array([r[1:4]]),np.array([r[6]]),corridor)[0] for r in rows))

    def test_outcrop_companions_share_tint_and_have_varied_smaller_sizes(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        skin=lambda x,y,z: np.column_stack((x,y,z+3*np.sin(x/2)*np.sin(y/2)))
        with patch.object(cm,'natural_family',return_value=cm.ARID_CUT):
            rows=self.fixture(skin_at=skin)
            self.assertEqual(rows,self.fixture(skin_at=skin))
            large=[r for r in rows if r[0].startswith('talus_large_')]
            small=[r for r in rows if r[0].startswith('talus_face_')]
            self.assertGreater(len(small),0)
            self.assertGreater(np.ptp([r[6] for r in large]),2)
            self.assertGreater(np.ptp([r[6] for r in small]),.5)
            for r in small:
                self.assertTrue(.7<=r[6]<=2.4)
                self.assertIn(r[0][-6:],{b[0][-6:] for b in large})
                distance=min(np.linalg.norm(np.array(r[1:4])-b[1:4]) for b in large)
                self.assertLess(distance,12)
                basis=np.array(r[8:]).reshape(3,3)
                np.testing.assert_allclose(basis@basis.T,np.eye(3),atol=2e-6)
                self.assertGreaterEqual(basis[2,2],np.cos(np.radians(12))-.000001)
            plane=lambda x,y,z: np.column_stack((x,y,20+3*(x-80)))
            self.assertFalse(any(r[0].startswith('talus_face_') for r in self.fixture(skin_at=plane)))

    def test_toe_rubble_uses_source_detector_material(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        with patch.object(talus,'mineral_tints',wraps=talus.mineral_tints) as tint:
            self.fixture(families=np.full((40,40),cm.ARID_CUT))
            self.assertTrue(tint.called)
            np.testing.assert_array_equal(tint.call_args.args[2],cm.ARID_CUT)

    def test_base_rubble_is_buried_further_below_support(self):
        from unittest.mock import patch
        with patch.object(talus,'RUBBLE_BURIAL',.12): previous=self.fixture()
        current=self.fixture()
        self.assertEqual(len(previous),len(current))
        for old,new in zip(previous,current):
            self.assertLess(new[3],old[3])
            self.assertAlmostEqual(np.linalg.norm(np.array(new[1:4])-old[1:4])/new[6],.10,delta=.003)

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
