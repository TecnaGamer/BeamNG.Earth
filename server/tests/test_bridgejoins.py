import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bridgejoins
import grading


class EntranceTest(unittest.TestCase):
    def ramp(self, protected=(), terrain_start=0.):
        return bridgejoins.Ramp([[0., 0.], [100., 0.]], 5., -.02,
                                lambda xy: 2. + .01*np.asarray(xy)[:, 0], 5., terrain_start, protected)

    def test_height_and_gradient_meet_both_surfaces(self):
        r = self.ramp()
        self.assertAlmostEqual(r.z[0], 5.)
        self.assertAlmostEqual(r.z[-1], 3.)
        self.assertAlmostEqual((r.z[1]-r.z[0])/2., -.02, delta=.002)
        self.assertAlmostEqual((r.z[-1]-r.z[-2])/2., .01, delta=.003)
        self.assertLess(np.abs(np.diff(r.z)/2.).max(), .05)

    def test_terrain_and_procedural_connector_share_the_profile(self):
        r = self.ramp()
        xy = np.c_[np.arange(0., 100., 2.), np.zeros(50)]
        terrain = r.fit(xy, np.full(50, 2.), terrain=True)
        mesh = r.fit(xy, np.full(50, 7.), terrain=False)
        np.testing.assert_allclose(terrain, mesh, atol=1e-7)
        np.testing.assert_allclose(terrain, r.z[:-1], atol=1e-7)

    def test_no_fill_below_bridge_or_beyond_outer_join(self):
        r = self.ramp(terrain_start=20.)
        xy = np.array([[-2., 0.], [5., 0.], [102., 0.], [40., 80.]])
        np.testing.assert_array_equal(r.fit(xy, np.zeros(4), terrain=True), np.zeros(4))
        self.assertGreater(r.fit([[20., 0.]], np.zeros(1), terrain=True)[0], 4.)

    def test_abutment_fill_has_no_vertical_start_plane(self):
        r=self.ramp(terrain_start=20.)
        xy=np.c_[np.arange(0.,25.,.1),np.zeros(250)]
        z=r.fit(xy,np.zeros(len(xy)),terrain=True)
        self.assertLess(np.max(np.abs(np.diff(z)/.1)),.65)

    def test_lower_crossing_protection_blends_across_its_shoulder(self):
        p=grading.Plan(np.array([[30.,-50.],[30.,50.]]),np.zeros(2),3.)
        r=self.ramp(protected=[p])
        xy=np.c_[np.arange(25.,55.,.1),np.zeros(300)]
        z=r.fit(xy,np.zeros(len(xy)),terrain=True)
        self.assertLess(np.max(np.abs(np.diff(z)/.1)),.65)

    def test_visible_ground_stays_below_the_physics_surface(self):
        r = self.ramp()
        xy = np.array([[0., 0.], [20., 0.], [50., 0.], [-200., 0.]])
        a = r.fit(xy, np.zeros(4), terrain=True)
        b = r.fit(xy, np.zeros(4), terrain=True, lift=-.12)
        np.testing.assert_allclose((a-b)[:3], .12)
        self.assertEqual(b[-1],0.)

    def test_marking_height_offset_survives_connector_adjustment(self):
        r = self.ramp()
        r.reference = (np.array([[0.,0.],[100.,0.]]),np.array([7.,7.]))
        xy = np.array([[10.,0.],[40.,0.]])
        road = r.fit(xy,np.full(2,7.),terrain=False)
        paint = r.fit(xy,np.full(2,7.02),terrain=False)
        np.testing.assert_allclose(paint-road,.02)

    def test_lower_crossing_is_not_buried(self):
        p = grading.Plan(np.array([[30., -30.], [30., 30.]]), np.zeros(2), 3.)
        r = self.ramp(protected=[p])
        xy = np.array([[30., 0.], [50., 0.]])
        z = r.fit(xy, np.zeros(2), terrain=True)
        self.assertEqual(z[0], 0.)
        self.assertGreater(z[1], 3.)
        self.assertGreater(r.fit(xy, np.full(2, 7.), terrain=False)[0], 3.)

    def test_crossing_uses_physical_ground_even_when_its_profile_is_at_upper_level(self):
        p=grading.Plan(np.array([[30.,-30.],[30.,30.]]),np.full(2,5.),3.)
        r=self.ramp(protected=[p])
        xy=np.array([[30.,0.],[31.,0.]])
        native=np.zeros(2)
        np.testing.assert_allclose(r.fit(xy,np.full(2,2.),terrain=True,native=native),native)

    def test_actual_model_edge_joins_a_connected_carriageway(self):
        surf = {'ax': 0, 'u0': -100., 'du': 2., 'v0': -10., 'dv': 1., 'z': np.full((101, 21), 5.)}
        xy = np.array([[-80., 0.], [80., 0.]])
        a = grading.Plan(np.array([[-80., 0.], [-220., 0.]]), np.full(2, 2.), 5., abut=(True, False))
        b = grading.Plan(np.array([[80., 0.], [220., 0.]]), np.full(2, 2.), 5., abut=(True, False))
        a.rid, b.rid = 1, 2
        ramps = bridgejoins.make_ramps(surf, [('x', SimpleNamespace(width=10.), xy, np.full(2, 2.), None)],
                                       [a, b], lambda xy: np.full(len(xy), 2.))
        self.assertEqual(len(ramps), 2)
        np.testing.assert_allclose([r.xy[0, 0] for r in ramps], [-100., 100.])
        self.assertTrue(all(r.z[0] == 5. for r in ramps))


class FreshSkinWorkerTest(unittest.TestCase):
    def test_generic_bridge_floor_does_not_reshape_terrain(self):
        ctx=bridgejoins.Context(SimpleNamespace(),'/models',None)
        pos=np.array([[-100.,-10.,0.],[100.,10.,8.]])
        mesh=(pos,None,np.empty((0,3),dtype=int),None,[(None,None,None,False,.5,False,'Concrete')],np.empty(0,dtype=int))
        with mock.patch('buildings._model_mesh',return_value=mesh), \
             mock.patch('buildings._bridge_info',return_value={}), \
             mock.patch('buildings._is_bridge',return_value=True):
            self.assertIsNone(ctx._load(22,16.,108.))

    def test_skin_reconstructs_model_grading_without_building_content_in_this_worker(self):
        import blockworker
        import geotiles
        class Verified(Exception): pass
        for models in (False,True):
            g=SimpleNamespace()
            g.apply=lambda la,lo,z,lift: np.asarray(z)+(5. if hasattr(g,'model_joins') else 0.)
            state=SimpleNamespace(anchor=lambda *a:(g,None),md=None,dem=SimpleNamespace(sample=lambda la,lo,z:np.zeros_like(la)))
            def configure(grader,*args): grader.model_joins=True
            def mesh(*args,**kw):
                height=kw['drawn'](np.array([-32.]),np.array([116.]))
                np.testing.assert_allclose(height,5. if models else 0.)
                raise Verified()
            gx,gy=geotiles.tile_of(-32.,116.)
            with mock.patch.object(blockworker,'_s',state), \
                 mock.patch.object(blockworker,'_skin_inputs',return_value={'models':models}), \
                 mock.patch.object(blockworker,'ground',side_effect=lambda frame,la,lo,z:z), \
                 mock.patch.object(blockworker,'tunnel_profiles',return_value=None), \
                 mock.patch('bridgejoins.configure',side_effect=configure) as configured, \
                 mock.patch('roads.block_arch_raise',side_effect=lambda tun,cell,frame,la,lo,z,box:z), \
                 mock.patch('roadmesh.build',side_effect=mesh):
                with self.assertRaises(Verified):
                    blockworker._geo_skin(1,-32.,116.,16,gx,gy,'/session/out','/x',True,.01,None,lambda:None)
                self.assertEqual(configured.call_count,int(models))


if __name__ == '__main__':
    unittest.main()
