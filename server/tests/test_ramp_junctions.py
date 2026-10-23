"""Ramp curves, through-lane continuity, paved returns and terrain-conforming road interiors."""
import sys
import unittest
from pathlib import Path
import numpy as np
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import roads
import roadmesh
import navgraph
from test_roads import features,rules,FRAME,flat


def exit_ramp(reverse=False):
    ramp=[(110,100),(165,80),(205,45),(330,35)]
    f=features([[(10,100),(110,100)],[(110,100),(380,100)],ramp[::-1] if reverse else ramp],
               ['motorway']*3,ids=['before','after','ramp'])
    f.rules['oneway']=rules([(0,1.),(1,1.),(2,-1. if reverse else 1.)],'float')
    f.rules['subclass']=rules([(2,'link')])
    return f


class RampJunctionsTest(unittest.TestCase):
    def test_only_ramp_curves_change_and_merge_tangent_follows_mainline(self):
        for reverse in (False,True):
            f=exit_ramp(reverse)
            np.testing.assert_array_equal(roads.geometry_parts(f,0)[0][0],f.parts(0)[0][0])
            np.testing.assert_array_equal(roads.geometry_parts(f,1)[0][0],f.parts(1)[0][0])
            p=roads.geometry_parts(f,2)[0][0]
            self.assertGreater(len(p),12)
            xy=np.c_[*FRAME.geodetic_to_enu(p[:,1],p[:,0],0.)[:2]]
            if reverse: xy=xy[::-1]
            u=xy[1]-xy[0];u/=np.linalg.norm(u)
            self.assertGreater(u[0],.998,'ramp starts tangent to the through road')
            self.assertLess(abs(u[1]),.06)
            self.assertIs(roads.geometry_parts(f,2),roads.geometry_parts(f,2),'one curve reused per decoded cell')
            np.testing.assert_allclose(roads.ground_parts(f,2)[0],p)
            graph=navgraph.build(FRAME,[f],flat,(150.,100.))
            self.assertTrue(any('ramp' in str(e[:2]) for e in graph['edges']))

    def test_branch_does_not_rotate_through_pavement_cross_sections(self):
        f=exit_ramp()
        sections=roads._join_sections(FRAME,[f])
        ll=f.parts(0)[0][0]
        e,n,_=FRAME.geodetic_to_enu(ll[:,1],ll[:,0],0.)
        p=np.c_[e,n,np.zeros(len(e))]
        cross=roads._with_cross(p,sections)[:,3:5]
        np.testing.assert_allclose(cross[:,0],0.,atol=3e-5)
        np.testing.assert_allclose(cross[:,1],1.,atol=3e-5)

    def test_through_yellow_and_lane_divider_are_continuous_at_exit(self):
        for reverse in (False,True):
            f=exit_ramp(reverse)
            chunks=[]
            for tx in (0,1): chunks+=roads.build_tile(FRAME,tx,0,[f],flat)
            ll=f.parts(0)[0][0][:1]
            _,y,_=FRAME.geodetic_to_enu(ll[:,1],ll[:,0],0.)
            for material,width,offset in (('line_yellow',.45,3.6),(roads.LANE_LINE['m'],.12,0.)):
                runs=[]
                for c in chunks:
                    if c['m']!=material or abs(c['w']-width)>.01: continue
                    p=np.asarray(c['n'])
                    if np.max(np.abs(p[:,1]-y[0]-offset))<.05:
                        runs.append((p[:,0].min(),p[:,0].max()))
                self.assertTrue(runs,(material,reverse))
                runs.sort();end=runs[0][1]
                for a,b in runs[1:]:
                    self.assertLess(a-end,1.,'no blank paint span at a ramp or segment boundary')
                    end=max(end,b)
                self.assertGreater(end,370.)
            self.assertTrue(any(c.get('ramp_guidance') and c.get('dash')==1.8 and c.get('gap')==5.5 for c in chunks),
                            'white edge has dotted merge guidance')

    def test_terminal_t_junction_has_two_rounded_paved_returns(self):
        f=features([[(10,100),(150,100),(300,100)],[(150,100),(150,240)]],['primary','motorway'])
        f.rules['oneway']=rules([(1,-1.)],'float')
        f.rules['subclass']=rules([(1,'link')])
        for urban in (False,True):
            walks=[]
            chunks=roads.build_tile(FRAME,0,0,[f],flat,lambda la,lo:np.full(np.shape(la),urban),walks=walks)
            fills=[c for c in chunks if c.get('poly') and not c.get('turn_arrow')]
            self.assertEqual(len(fills),2,'both corners of ramp terminal filled with curved pavement')
            self.assertTrue(all(len(c['poly'])>8 for c in fills),'curved rather than rectangular corners')
            corners=[c['corner'] for c in walks if 'corner' in c]
            self.assertEqual(len(corners),2 if urban else 0,'existing street sidewalks wrap around the terminal')
            self.assertFalse(any(c['m']=='crossing_white' for c in chunks),'ramp terminal has no invented pedestrian crossing')
        no_walk=roads.build_tile(FRAME,0,0,[f],flat)
        self.assertEqual(sum(bool(c.get('poly')) and not c.get('turn_arrow') for c in no_walk),2,
                         'paved corners also work without sidewalk meshes')

    def test_wide_pavement_samples_the_terrain_under_the_travel_lanes(self):
        chunks=[{'m':'road_asphalt_2lane','w':12.,'tl':11.,'p':20,'n':[[10,0,10],[110,0,10]]}]
        calls=[]
        def hump(la,lo):
            _,n,_=FRAME.geodetic_to_enu(la,lo,0.)
            calls.append(len(np.ravel(n)))
            return 10.+np.exp(-(n/2.)**2)
        mesh=roadmesh.build(FRAME,chunks,hump,(0.,0.,0.),follow=True)
        P,T,_,_,_=mesh
        centre=P[np.abs(P[:,1])<.01]
        self.assertTrue(len(centre)>20)
        self.assertGreater(centre[:,2].min(),10.99,'terrain hump cannot emerge through an edge-only road quad')
        self.assertEqual(len(calls),2,'the upstream follow pass batches edges and terrain-grid vertices')
        normals=np.cross(P[T[:,1]]-P[T[:,0]],P[T[:,2]]-P[T[:,0]])
        self.assertTrue((normals[:,2]>0).all())


if __name__=='__main__': unittest.main()
