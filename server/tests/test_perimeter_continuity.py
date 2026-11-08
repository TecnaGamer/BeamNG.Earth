"""Continuity and transport regressions, including the user's two captured OSM cells."""
import json
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'server'))
sys.path.insert(0,str(ROOT/'server/tests'))
import fencecrossings as fc
import perimeters as p
import mapgeom,geodesy
import test_perimeters as base_tests


class ContinuityTest(unittest.TestCase):
    def setUp(self):
        self.base=base_tests.GlobalPerimeterTest();self.base.setUp()
        self.frame=self.base.frame
        self.area=self.base.feature()

    def transport(self,xy,value='rail',grade=None,record='w15733913@6'):
        f=self.base.feature([np.asarray(xy,float)],tags={'railway':value} if value else {},record=record,kind=mapgeom.LINE)
        f.attrs['subtype']=np.array(['rail' if value else 'road'])
        f.attrs['class']=np.array(['standard_gauge' if value else 'residential'])
        f.attrs['osm_railway']=np.array([value or ''])
        if grade:
            f.rules['flags']=mapgeom.rule_table([(0,'is_'+grade,[0.,1.])],'text')
        return f

    def rows(self,transport=None,areas=None,box=None,events=None,surface=None):
        return p.rows(self.frame,lambda k:areas or [self.area],lambda k:None,box or self.base.box,
            lambda la,lo:np.full(len(la),10.),transport_fn=(lambda k:transport) if transport is not None else None,
            surface=surface,diagnostics=events)

    def test_active_rail_crossings_open_but_near_parallel_does_not(self):
        crossing=self.transport([[20,-20],[20,60]])
        events=[];rows=self.rows(crossing,events=events)
        self.assertTrue(events)
        self.assertTrue(all(e['transport_osm_id']=='way/15733913' and e['at_grade'] for e in events))
        self.assertTrue(all(e['reason']=='railway_crossing' and e['opening_m']>7 for e in events))
        for r in rows:
            if abs(r[2]-r[5])<.001:
                self.assertTrue(max(r[1],r[4])<=16.381 or min(r[1],r[4])>=23.619)
        near=self.transport([[-20,-1],[60,-1]])
        self.assertEqual(self.rows(near),self.rows())

    def test_inactive_tracks_no_openings_active_light_rail_tram_open(self):
        for value in ('disused','abandoned','razed','construction'):
            self.assertEqual(self.rows(self.transport([[20,-20],[20,60]],value)),self.rows())
        for value in ('rail','light_rail','tram'):
            events=[];self.rows(self.transport([[20,-20],[20,60]],value),events=events)
            self.assertTrue(events)

    def test_bridge_tunnel_and_measured_height(self):
        for grade in ('bridge','tunnel'):
            self.assertEqual(self.rows(self.transport([[20,-20],[20,60]],grade=grade)),self.rows())
        corridor=dict(a=np.array([0.,0.,20.]),b=np.array([10.,0.,20.]),grade='bridge')
        self.assertFalse(fc.at_grade(corridor,.5,10.))
        corridor['a'][2]=corridor['b'][2]=11.
        self.assertTrue(fc.at_grade(corridor,.5,10.))

    def test_road_crossing_and_near_parallel(self):
        self.assertEqual(self.rows(self.transport([[-20,-1],[60,-1]],value=None)),self.rows())
        events=[];self.rows(self.transport([[20,-20],[20,60]],value=None),events=events)
        self.assertTrue(events and all(e['reason']=='road_crossing' for e in events))

    def test_multiple_parallel_tracks_and_fragment_removal(self):
        cuts=[]
        a,b=np.array([0.,0.]),np.array([40.,0.])
        for x in (18.,24.): cuts.extend(p.capsule(a,np.array([1.,0.]),40.,np.array([x,-10]),np.array([x,10]),3.62))
        cleaned=p.clean_cuts(cuts,40.)
        self.assertEqual(len(cleaned),1)
        self.assertGreater(cleaned[0][1]-cleaned[0][0],13.)
        first=self.transport([[18,-20],[18,60]],record='w15733913@6')
        second=self.transport([[24,-20],[24,60]],record='w148715389@11')
        second.ids=np.array(['fixture2'])
        two=mapgeom.concat(first,second)
        events=[];rows=self.rows(two,events=events)
        self.assertEqual({e['transport_osm_id'] for e in events},{'way/15733913','way/148715389'})
        self.assertFalse(any(14.39<r[1]<27.61 and abs(r[2])<.001 for r in rows))

    def test_partial_disused_range_keeps_only_active_crossing(self):
        track=self.transport([[20,-20],[20,60]])
        track.rules['flags']=mapgeom.rule_table([(0,'is_disused',[0.,.5])],'text')
        events=[];self.rows(track,events=events)
        self.assertTrue(events)
        self.assertTrue(all(e['latitude']>41.1501 for e in events)) # top edge only, bottom is disused

    def test_generic_mapped_fence_becomes_selected_physical_perimeter(self):
        fence=self.base.feature([np.array([[0,0],[40,0]])],tags={'barrier':'fence'},kind=mapgeom.LINE)
        baseline=self.rows()
        result=p.rows(self.frame,lambda k:[self.area],lambda k:fence,self.base.box,
                      lambda la,lo:np.full(len(la),10.))
        self.assertTrue(any(r[8]=='physical:way/1' for r in result))
        self.assertTrue(all(r[8]=='physical:way/1' for r in result if abs(r[2])<.01 and abs(r[5])<.01))

    def test_near_duplicate_shared_edges_and_distinct_internal_perimeter(self):
        original=self.rows()
        # Same installation mapped twice with 40 cm surveying displacement.
        shifted=self.base.feature([np.array([[.4,0],[40.4,0],[40.4,40],[.4,40],[.4,0]])],record='r2@1')
        doubled=self.rows(areas=[self.area,shifted])
        self.assertLess(sum(math.dist(r[1:3],r[4:6]) for r in doubled),170.)
        inner=self.base.feature([np.array([[5,5],[35,5],[35,35],[5,35],[5,5]])],record='r2@1')
        # An unnamed nested land-use area supplies no evidence of another fence.
        self.assertEqual(len(self.rows(areas=[self.area,inner])),len(original))

    def test_ring_phase_does_not_restart_at_collinear_vertex(self):
        ring=np.array([[0,0],[7,0],[40,0],[40,40],[0,40],[0,0]])
        f=self.base.feature([ring])
        rows=self.rows(areas=[f])
        # A vertex at 7 m splits a module; the next endpoint is the ring grid's 8 m tick, not 11 m.
        ends=[v for r in rows if abs(r[2])<.001 and abs(r[5])<.001 for v in (r[1],r[4])]
        self.assertTrue(any(abs(v-8)<.15 for v in ends))
        self.assertFalse(any(abs(v-11)<.15 for v in ends))

    def test_visible_terrain_overrides_dem_and_tile_ownership_remains_complete(self):
        surface=lambda xy:20.+.03*xy[:,0]
        rows=self.rows(surface=surface)
        for r in rows:
            self.assertAlmostEqual(r[3],20.+.03*r[1])
            self.assertAlmostEqual(r[6],20.+.03*r[4])
        left=self.rows(box=(-100,-100,20,200),surface=surface)
        right=self.rows(box=(20,-100,200,200),surface=surface)
        self.assertEqual(sorted(left+right),sorted(rows))

    def test_captured_geographic_cases(self):
        for name,lat,lon in [('airport',41.15559,-104.82271),('roundtop',41.16365,-104.87995)]:
            self.frame=geodesy.EnuFrame(lat,lon,0.)
            def load(layer):
                with np.load(ROOT/'probes'/('fence_'+name+'_'+layer+'.npz')) as arrays:
                    return mapgeom.Features.from_arrays(layer,arrays,'')
            areas=[load(layer) for layer in p.LAYERS]
            roads,infra=load('roads'),load('infrastructure')
            events=[];audit={}
            rows=p.rows(self.frame,lambda k:areas,lambda k:infra,(-400,-400,400,400),
                lambda la,lo:np.full(len(la),10.),transport_fn=lambda k:roads,diagnostics=events,audit=audit)
            if name=='airport':self.assertTrue(rows)
            else:self.assertTrue(any(f['administrative_unsupported_m']>0 or f['corroborating_outer_members'] for f in audit['facilities']))
            self.assertEqual(len(rows),len({r[7] for r in rows}))
            self.assertTrue(all(not (f.clipped is not None and f.clipped.any()) for f in areas))
            # Each generated panel avoids every at-grade active railway corridor at a crossing.
            corridors=fc.corridors(self.frame,roads,p.tags_of,p.osm_id)
            for r in rows:
                a,b=np.array(r[1:3]),np.array(r[4:6])
                self.assertFalse(any(c['kind']=='railway' and c['grade']=='ground' and fc.intersection(a,b,c['a'],c['b'])
                                     for c in corridors))


if __name__=='__main__':unittest.main()
