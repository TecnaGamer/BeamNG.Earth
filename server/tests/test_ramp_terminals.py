"""Bent ramp lane tapers, city approach paint, directionality and tile ownership."""
import json
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import roads,roadlanes,roadterminals,roadmesh,mapgeom
from test_roads import FRAME,features,rules,flat
from test_roads import rules

def counted(lines,counts,links=(),bridge=()):
    f=features(lines,['motorway']*len(lines),flags=[(i,'is_bridge') for i in bridge])
    f.rules['oneway']=rules([(i,1.) for i in range(len(lines))],'float')
    f.rules['subclass']=rules([(i,'link') for i in links])
    for key,k in (('lanes_forward',0),('lanes_backward',1),('lanes_both_ways',2)):
        f.rules[key]=rules([(i,n if k==0 else 0.) for i,n in enumerate(counts)],'float')
    return f


def terminal_rows(reverse=False,tagged=True):
    sp=roads.Spec(12.8,'paved',False,'motorway',True,dir=-1 if reverse else 1,
                  shoulder=1.,link=True,lanes=(0,3,0) if reverse else (3,0,0),lane_w=3.6)
    a=np.array([[20.,100.,10.],[170.,100.,10.]])
    b=np.array([[170.,100.,10.],[210.,100.,10.]])
    if reverse:a,b=a[::-1],b[::-1]
    city=roads.Spec(12.,'paved',True,'primary')
    rows=[('a',sp,a,-76.),('b',sp,b,-76.),('city',city,np.array([[210.,30.,10.],[210.,100.,10.],[210.,180.,10.]]),-76.)]
    tags={'b':{'turn:lanes:backward' if reverse else 'turn:lanes':'left|left;right|right'}} if tagged else {}
    return rows,tags


class RampChains(unittest.TestCase):
    def check_chain(self,reverse=False):
        lines=[[(0,0),(100,0)],[(100,0),(120,15),(120,35)],[(120,35),(140,60),(170,70)]]
        if reverse:lines=[list(reversed(p)) for p in lines]
        f=counted(lines,[1,2,3],links=(0,1,2))
        if reverse:
            f.rules['oneway']=rules([(i,-1.) for i in range(3)],'float')
            f.rules['lanes_forward']=rules([(i,0.) for i in range(3)],'float')
            f.rules['lanes_backward']=rules([(i,n) for i,n in enumerate((1,2,3))],'float')
        specs=roads.road_specs(f)
        end_sections=[]
        for i,sp in enumerate(specs):
            self.assertIn('ramp_chain',roadlanes.model(sp.lane_model))
            ll=next(r for p in f.parts(i) for r in p)
            p=np.column_stack(FRAME.geodetic_to_enu(ll[:,1],ll[:,0],0.))
            p=roads.densify(p,.5)
            n=roadlanes.counts(sp,p,FRAME).sum(1)
            width=sp.width+(n-sum(sp.lanes))*sp.lane_w
            self.assertTrue(np.all((n>=.999)&(n<=3.001)))
            self.assertLess(np.max(abs(np.diff(width))),.12,'no compressed lane-width step')
            end_sections.append(width[[0,-1]] if not reverse else width[[-1,0]])
            self.assertGreaterEqual(roadlanes.grading_spec(sp).width+1e-9,3*3.6+2.)
        for a,b in zip(end_sections,end_sections[1:]):self.assertAlmostEqual(a[1],b[0],places=4)
        self.assertAlmostEqual(end_sections[0][0],5.6,places=4)
        self.assertAlmostEqual(end_sections[-1][-1],12.8,places=4)
        return f

    def test_one_two_three_lanes_around_bent_source_joins(self):self.check_chain()

    def test_reversed_osm_ramp_uses_the_same_taper(self):self.check_chain(True)

    def test_cross_class_ramp_chain_shares_physical_width(self):
        f=counted([[(-180,0),(0,0)],[(0,0),(160,40)]],[1,3],links=(0,1))
        f.attrs['class'][1]='primary'
        sp=roads.road_specs(f)
        self.assertEqual(sp[0].lane_w,sp[1].lane_w)
        p=np.array([[0.,0.,0.]])
        widths=[s.width+(roadlanes.counts(s,p,FRAME).sum()-sum(s.lanes))*s.lane_w for s in sp]
        self.assertAlmostEqual(*widths,places=3)

    def test_city_cross_street_is_not_part_of_the_ramp_chain(self):
        f=counted([[(0,0),(180,0)],[(180,-80),(180,80)]],[1,3],links=(0,))
        f.attrs['class'][1]='primary'
        sp=roads.road_specs(f)
        self.assertNotIn('ramp_chain',roadlanes.model(sp[0].lane_model))

    def test_lane_opening_before_a_fork_does_not_restart_at_the_auxiliary_taper(self):
        f=counted([[(0,0),(100,0)],[(100,0),(120,15),(120,35)],[(120,35),(140,60),(170,70)],
                   [(170,70),(280,70)],[(170,70),(270,115)]],[1,2,3,2,1],links=range(5))
        specs=roads.road_specs(f)
        for i in range(3):
            sp=specs[i];ll=next(r for p in f.parts(i) for r in p)
            p=np.column_stack(FRAME.geodetic_to_enu(ll[:,1],ll[:,0],0.))
            p=roads.densify(p,.5)
            n=roadlanes.counts(sp,p,FRAME).sum(1)
            self.assertLess(np.max(abs(np.diff(n))),.06)

    def test_city_node_inside_a_way_breaks_ramp_profiles_at_the_stop_line(self):
        f=counted([[(0,0),(150,0)],[(150,0),(260,0)],[(150,-80),(150,0),(150,80)]],
                  [3,1,2],links=(0,1))
        f.attrs['class'][2]='primary'
        sp=roads.road_specs(f)
        self.assertNotIn('ramp_chain',roadlanes.model(sp[0].lane_model))
        self.assertAlmostEqual(roadlanes.counts(sp[0],np.array([[150,0,0.]]),FRAME).sum(),3.)


class ApproachPaint(unittest.TestCase):
    def test_osm_lane_arrows_preserve_travel_order_on_reversed_ways(self):
        for reverse in (False,True):
            rows,tags=terminal_rows(reverse)
            a=roadterminals.find(FRAME,rows,tags)
            self.assertEqual(len(a),1)
            self.assertEqual(a[0]['turns'],[{'left'},{'left','right'},{'right'}])
            self.assertEqual(a[0]['owners'],{'a','b'})
            self.assertFalse(a[0]['inferred'])

    def test_untagged_t_junction_never_invents_a_straight_arrow(self):
        rows,tags=terminal_rows(tagged=False)
        a=roadterminals.find(FRAME,rows,tags)
        self.assertEqual(a[0]['turns'][0],{'left'})
        self.assertEqual(a[0]['turns'][-1],{'right'})
        self.assertTrue(all('straight' not in t for t in a[0]['turns']))
        self.assertTrue(a[0]['inferred'])

    def test_straight_arrow_only_when_city_road_continues_forward(self):
        rows,tags=terminal_rows(tagged=False)
        rows.append(('forward',rows[-1][1],np.array([[210.,100.,10.],[300.,100.,10.]]),-76.))
        a=roadterminals.find(FRAME,rows,tags)
        self.assertEqual(a[0]['turns'],[{'left'},{'straight'},{'right'}])

    def test_two_lanes_can_share_left_straight_and_right(self):
        rows,tags=terminal_rows(tagged=False)
        rows=[(fid,sp._replace(lanes=(2,0,0),width=9.2) if sp.link else sp,p,lon) for fid,sp,p,lon in rows]
        rows.append(('forward',rows[-1][1],np.array([[210.,100.,10.],[300.,100.,10.]]),-76.))
        a=roadterminals.find(FRAME,rows,tags)
        self.assertEqual(set.union(*a[0]['turns']),{'left','straight','right'})

    def test_solid_divider_has_exact_boundary_and_does_not_change_ramp_dots(self):
        rows,tags=terminal_rows();a=roadterminals.find(FRAME,rows,tags)
        line=np.array([[100.,98.2,10.],[210.,98.2,10.]])
        look={**roads.LANE_LINE,'lane_index':1,'dash':1.8,'gap':5.5}
        runs=roadterminals.solid_runs('b',look,line,a)
        self.assertEqual(len(runs),2)
        self.assertEqual(runs[0][0]['m'],roads.LANE_LINE['m'])
        self.assertEqual(runs[1][0]['m'],'line_white')
        self.assertNotIn('dash',runs[1][0]);self.assertNotIn('gap',runs[1][0])
        self.assertAlmostEqual(runs[1][1][0,0],210.-a[0]['clear']-60.)
        self.assertAlmostEqual(runs[1][1][-1,0],210.-a[0]['clear'])
        dots={**look,'ramp_guidance':True}
        untouched=roadterminals.solid_runs('b',dots,line,a)
        self.assertIs(untouched[0][0],dots)
        self.assertEqual(untouched[0][0]['gap'],5.5)

    def test_arrow_mesh_is_finite_white_and_owned_once_at_a_tile_border(self):
        rows,tags=terminal_rows();a=roadterminals.find(FRAME,rows,tags)
        whole=roadterminals.arrows(FRAME,rows,a,(0,0,256,256))
        self.assertTrue(whole)
        def area(chunks):
            total=0.
            for c in chunks:
                p=np.asarray(c['poly']);q=np.roll(p,-1,axis=0)
                total+=abs((p[:,0]*q[:,1]-p[:,1]*q[:,0]).sum())/2
            return total
        split=(roadterminals.arrows(FRAME,rows,a,(0,0,188,256))+
               roadterminals.arrows(FRAME,rows,a,(188,0,256,256)))
        self.assertAlmostEqual(area(whole),area(split),places=3)
        mesh=roadmesh.build(FRAME,whole,flat,(0,0,0))
        self.assertEqual(mesh[3],[roadterminals.PAINT])
        self.assertTrue(np.isfinite(mesh[0]).all())
        self.assertTrue(np.all(mesh[0][:,2]>9.9))
        self.assertEqual(roadmesh.layer_of(whole[0]),'mark')

    def test_no_terminal_paint_at_a_freeway_merge(self):
        rows,tags=terminal_rows()
        fid,sp,p,lon=rows[-1]
        rows[-1]=(fid,sp._replace(cls='motorway',oneway=True,dir=1),p,lon)
        self.assertFalse(roadterminals.find(FRAME,rows,tags))

    def test_actual_tile_contains_arrow_mesh_and_solid_travel_dividers(self):
        f=counted([[(0,100),(110,100)],[(110,100),(210,100)],[(210,0),(210,100),(210,230)]],[3,3,2],links=(0,1))
        f.attrs['class'][2]='primary'
        f.attrs['osm_tags']=mapgeom.text_column(['','{"turn:lanes":"left|through|right"}',''])
        chunks=roads.build_tile(FRAME,0,0,[f],flat)
        self.assertTrue(any(c.get('turn_arrow') for c in chunks))
        self.assertTrue(any(c.get('terminal_solid') and c['m']=='line_white' for c in chunks))
        self.assertFalse(any(c.get('terminal_solid') and c.get('ramp_guidance') for c in chunks))


if __name__=='__main__':unittest.main()
