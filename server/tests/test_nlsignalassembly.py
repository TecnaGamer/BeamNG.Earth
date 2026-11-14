from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae
import heightmap
import nlsignalassembly as assembly


def road(n):
    w=n*1.65
    return dict(kind='road',positions=np.array([[-w,-50.,0.],[w,-50.,0.],[w,50.,0.],[-w,50.,0.]]),
                triangles=np.array([[0,1,2],[0,2,3]]))


def scene(parts, coverage=((-100.,-100.,100.,100.),)):
    return assembly.make_scene(parts,np.zeros((3,3),np.uint16),0.,100.,(50.,50.),coverage,square=100.)


def request(n):
    return dict(stop=[0.,0.,0.],travel=[0.,1.],lanes=n,opposite=0,radius=n*1.65,
                overhead=True,before=20.,after=3.)


class PhysicalSignalPlanning(unittest.TestCase):
    def test_fresh_and_cached_drawn_road_give_identical_plans(self):
        fresh=road(4)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'road.cdae'
            cdae.write(str(path),fresh['positions'],fresh['triangles'],close=False)
            cached=assembly.cached_mesh(path,'road')
            a=assembly.plans(scene([fresh]),[request(4)])
            b=assembly.plans(scene([cached]),[request(4)])
            self.assertEqual(a,b)
            self.assertTrue(a[0]['ready']);self.assertEqual(a[0]['kind'],'portal')
            self.assertEqual(a[0]['groups'][0]['lanes'],[0,1,2,3])

    def test_quantised_terrain_contract_matches_existing_height_at(self):
        q=np.array([[0,100,200],[50,150,250],[100,200,300]],np.uint16)
        native=assembly.make_scene([],q,-2.,65.536,(.5,.5),[[-1.,-1.,1.,1.]],square=1.)
        got=native.probe(np.array([[0.,0.,-1.9]]))[0]
        expected=heightmap.height_at(q,-2.,65.536,.5,.5,0.,0.,square=1.)
        self.assertEqual(got[0],0.);self.assertEqual(got[1],expected)

    def test_terrain_holes_do_not_supply_ground(self):
        native=assembly.make_scene([],np.zeros((3,3),np.uint16),0.,1.,(0.,0.),
                                   [[-100.,-100.,100.,100.]],square=10.,
                                   terrain_holes=np.ones((3,3),np.uint8))
        self.assertEqual(native.probe(np.array([[0.,0.,0.]]))[0,0],-1.)

    def test_hidden_collision_objects_and_coarse_lods_do_not_change_placement(self):
        fine=road(4);coarse=road(8)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'road.cdae'
            variants=[dict(lods=[(100,dict(positions=coarse['positions'],tris=coarse['triangles']))]),
                      dict(collisions=[(coarse['positions'],coarse['triangles'],None)])]
            for extra in variants:
                cdae.write(str(path),fine['positions'],fine['triangles'],close=False,**extra)
                cached=assembly.cached_mesh(path,'road')
                np.testing.assert_array_equal(cached['positions'],fine['positions'].astype(np.float32))
                self.assertEqual(assembly.plans(scene([cached]),[request(4)]),
                                 assembly.plans(scene([fine]),[request(4)]))

    def test_actual_walk_mesh_height_wins_over_dem_floor(self):
        walk=dict(kind='walk',positions=np.array([[-2.,-2.,.15],[2.,-2.,.15],[0.,2.,.15]]),
                  triangles=np.array([[0,1,2]]))
        got=scene([walk]).probe(np.array([[0.,0.,0.]]))[0]
        self.assertEqual(got[0],0.);self.assertAlmostEqual(got[1],.15,places=7);self.assertEqual(got[2],2.)

    def test_pending_neighbours_do_not_become_permanent_omissions(self):
        got=assembly.plans(scene([road(3)],coverage=[]),[request(3)])[0]
        self.assertFalse(got['ready']);self.assertEqual(got['reason'],'surface_tiles_pending')
        self.assertTrue(got['retryable'])
        with self.assertRaises(assembly.SurfacePending):
            assembly.make_scene([],None,0.,1.,(0.,0.),[])

    def test_corrupt_triangle_does_not_reach_native_index(self):
        bad=road(2);bad['triangles'][0,0]=999
        with self.assertRaises(ValueError):scene([bad])


if __name__=='__main__':unittest.main()
