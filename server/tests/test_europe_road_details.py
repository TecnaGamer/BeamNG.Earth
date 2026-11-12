"""French signs and European rails: geometry, coverage gates and placement.

Synthetic road geometry; these checks cannot validate BeamNG's importer or FPS.
"""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))  # (PR #505's data: the tests' slice)
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'tools')]
import euguardrails as eu
import frroadmesh as faces
import frstreets
import geodesy
import guardrails
import poleprofiles
import roadphones
import roads
import signmesh
import build_europe_guardrails as assets


def highway(fid='east',y=0.,reverse=False,link=False):
    points=np.array([[-50.,y,0.],[550.,y,0.]])
    if reverse:points=points[::-1]
    return fid,roads.Spec(7.,'paved',False,'motorway',True,dir=1,link=link),points,7.7


def flat(lat,lon):return np.zeros(np.broadcast_arrays(lat,lon)[0].shape)


class RailsTest(unittest.TestCase):
    def setUp(self):
        self.frame=geodesy.EnuFrame(48.6,7.7)
        self.env=mock.patch.dict(os.environ,{'BNE_EU_ROAD_DETAILS':'on'});self.env.start();self.addCleanup(self.env.stop)
        self.phones=mock.patch.object(roadphones,'nearby',return_value=[]);self.phones.start();self.addCleanup(self.phones.stop)
        self.roads=[highway(),highway('west',18.,True)]

    def rows(self,box=(0.,-20.,512.,40.),mapped=(),rr=None):
        return eu.extend(self.frame,self.roads if rr is None else rr,box,flat,list(mapped))

    def test_two_outer_edges_not_the_central_reservation(self):
        rows=self.rows();self.assertGreater(len(rows),200)
        self.assertEqual({round(r[2],1)for r in rows},{-4.5,22.5})
        for r in rows:
            axes=np.array(r[8:17]).reshape(3,3)
            np.testing.assert_allclose(axes@axes.T,np.eye(3),atol=1e-10)
            self.assertGreater(np.linalg.det(axes),.999)
            toward_road=np.array([0.,1. if r[2]<0 else -1.])
            self.assertGreater(float(-axes[1,:2]@toward_road),.99)

    def test_country_gate_left_hand_side_and_disable(self):
        with mock.patch.object(eu,'country_at',return_value='GB'):
            rows=self.rows(rr=[highway()]);self.assertEqual({round(r[2],1)for r in rows},{4.5})
        with mock.patch.object(eu,'country_at',return_value='US'):self.assertEqual(self.rows(),[])
        with mock.patch.dict(os.environ,{'BNE_EU_ROAD_DETAILS':'off'}):self.assertEqual(self.rows(),[])
        self.assertEqual(self.rows(rr=[highway(link=True)]),[])
        self.assertEqual(eu.PROFILES['HR'],('b','red'))
        self.assertEqual(eu.PROFILES['PL'],('b','red'))
        self.assertEqual(eu.PROFILES['DE'],('mixed','none'))
        self.assertEqual(eu.PROFILES['SE'],('a','white'))
        self.assertEqual(eu.PROFILES['CH'],('a','none'))

    def test_tile_ownership_and_mapped_barrier_suppression(self):
        union=self.rows();split=self.rows((0,-20,256,40))+self.rows((256,-20,512,40))
        key=lambda r:tuple(np.round(r,7))
        self.assertEqual(sorted(map(key,union)),sorted(map(key,split)))
        concrete=[[6,100.,-4.5,0.,0.,0.,.88,0.,0.,-1.,0.,1.,0.,0.,0.,0.,1.]]
        r=self.rows(mapped=concrete)
        self.assertIn(concrete[0],r)
        self.assertFalse(any(abs(v[1]-100)<3. and v[2]<0 for v in r if v[0]!=6))

    def phone(self,x=100.,y=-7.):
        la,lo,_=self.frame.enu_to_geodetic(x,y,0.)
        return {'id':123,'lat':float(la),'lon':float(lo),'point':np.array([x,y,0.])}

    def test_sos_uses_motorway_projection_recesses_steel_and_keeps_concrete(self):
        with mock.patch.object(roadphones,'nearby',return_value=[self.phone()]):
            phones=roadphones.on_highways(self.frame,self.roads,(0,-20,512,40))
            self.assertEqual(len(phones),1)
            rows=self.rows()
        near=[r for r in rows if r[2]<0 and abs(r[1]-100)<20]
        self.assertTrue(any(r[2]<-4.6 for r in near))
        caps=[r for r in near if (r[0]-len(guardrails.STOCK_GROUPS))%3]
        self.assertEqual(len(caps),2)
        self.assertFalse(any(abs(r[1]-100)<6 for r in near if r not in caps))
        for r in near:
            part=('run','start','end')[(r[0]-9)%3]
            vertices=np.asarray(assets.collision(part).p)@np.array(r[8:17]).reshape(3,3)+r[1:4]
            self.assertGreater(np.min(abs(vertices[:,0]-100)),.70)
        concrete=[6,100.,-4.5,0.,0.,0.,.88,0.,0.,-1.,0.,1.,0.,0.,0.,0.,1.]
        self.assertEqual(eu.sos_access([concrete],phones),[concrete])

    def test_phone_in_carriageway_cannot_cut_a_gap(self):
        with mock.patch.object(roadphones,'nearby',return_value=[self.phone(y=0.)]):
            self.assertEqual(roadphones.on_highways(self.frame,self.roads,(0,-20,512,40)),[])

    def test_fallback_rails_do_not_span_a_crossing_road(self):
        sp=roads.Spec(6.,'paved',False,'secondary',False)
        cross=('cross',sp,np.array([[100.,-50,0],[100.,50,0]]),7.7)
        rows=self.rows(rr=self.roads+[cross])
        self.assertTrue(rows)
        for r in rows:self.assertGreater(abs(r[1]-100),3.+1.5+.54)


class SignsTest(unittest.TestCase):
    def test_french_highway_render_uses_live_index_without_us_mile_markers(self):
        import test_roads as geometry
        import test_highway_signs as fixture
        import highwaysigns
        import signstream
        lat,lon=48.6,7.7;frame=geodesy.EnuFrame(lat,lon)
        with tempfile.TemporaryDirectory()as d, mock.patch.multiple(geometry,LAT=lat,LON=lon,M_LON=111320.*np.cos(np.radians(lat))),mock.patch.object(fixture,'FRAME',frame):
            md=fixture.fixture(d);self.addCleanup(md.highway_signs.close)
            with mock.patch.dict(os.environ,{'BNE_EU_ROAD_DETAILS':'on'}):
                mesh,miles=highwaysigns.build_tile(md,frame,0,0,flat,mph=False,french=True)
                self.assertIsNotNone(mesh);self.assertIsNone(miles)
                self.assertEqual(mesh[3],signmesh.MATERIALS)
                signstream.build_tile(md,frame,1,0,0,flat,d,'/temp',mph=False,country='DE')
                doc=json.loads((Path(d)/'signs_1_0_0.json').read_text())
                self.assertIn('exits',doc);self.assertNotIn('mileposts',doc);self.assertNotIn('merges',doc)

    def test_street_names_and_sos_are_serializable_with_extended_materials(self):
        frame=geodesy.EnuFrame(48.6,7.7);m=signmesh.Mesh()
        sp=roads.Spec(6.,'paved',True,'residential',False)
        rr=[('a',sp,np.array([[100.,100,0],[200.,100,0]]),7.7),
            ('b',sp,np.array([[100.,100,0],[100.,200,0]]),7.7)]
        with mock.patch.object(roadphones,'nearby',return_value=[]):
            posts,records=frstreets.place(frame,rr,(0,0,256,256),lambda *p:'FR',m,
                                          {'a':'Rue du Houblon','b':'Rue de l’Église'})
        self.assertEqual(len(posts),2);self.assertEqual(len(records),2)
        faces.sos(m,(80.,80.,0.),(0,1))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'signs.cdae';m.write(path)
            self.assertGreater(path.stat().st_size,1000)
        self.assertIn('bne_fr_road_orange',m.materials)

    def test_mapped_sos_is_actually_emitted_by_tile_placement(self):
        frame=geodesy.EnuFrame(48.6,7.7);m=signmesh.Mesh();lat,lon,_=frame.enu_to_geodetic(100,-7,0)
        phone={'id':3,'lat':float(lat),'lon':float(lon),'point':np.array([100.,-7.,0.])}
        with mock.patch.object(roadphones,'nearby',return_value=[phone]):
            posts,records=frstreets.place(frame,[highway()],(0,-20,256,30),lambda *p:'FR',m)
        self.assertEqual([r[0]for r in posts],['fr_sos_phone']);self.assertEqual([r['id']for r in records],['sos:3'])
        self.assertAlmostEqual(abs(posts[0][4]),180.)
        self.assertEqual(sum(map(len,assets.sos_collision().tri.values())),12)

    def test_arrow_band_and_route_colours(self):
        m=signmesh.Mesh();faces.guide_panel(m,{'names':['MULHOUSE','ST DIÉ-COLMAR','OFFENBURG'],
                                           'routes':['A35','E25']},5.,3.51,3.51/2)
        p=np.asarray(m.p);tris=np.asarray(m.t);tm=np.asarray(m.tm)
        arrows=p[tris[tm==signmesh.WHITE]]
        # White border also occupies the edges; the centre's triangles are arrows.
        arrows=arrows[(np.max(abs(arrows[:,:,0]),axis=1)<.26)&(arrows[:,:,2].max(1)<1.2)]
        text=p[tris[tm==signmesh.GLYPH_WHITE]]
        text=text[np.max(text[:,:,2],axis=1)<3.51]  # route cartouches are above
        self.assertLess(arrows[:,:,2].max(),text[:,:,2].min()-.1)
        self.assertEqual(faces.route_colour('N83'),signmesh.RED)
        self.assertEqual(faces.route_colour('M884'),signmesh.BLUE)

    def test_new_assets_have_lods_finite_normals_and_small_collision(self):
        for family in eu.FAMILIES:
            for part in ('run','start','end'):
                high=assets.module(family,'orange_red',part);low=assets.module(family,'orange_red',part,False)
                self.assertLess(len(low.p),len(high.p))
                for m in (high,low,assets.collision(part)):
                    self.assertTrue(np.isfinite(m.p).all())
                    np.testing.assert_allclose(np.linalg.norm(m.n,axis=1),1.,atol=1e-10)
                self.assertLessEqual(sum(map(len,assets.collision(part).tri.values())),12)

    def test_cache_changes_only_when_enabled(self):
        with mock.patch.dict(os.environ,{'BNE_EU_ROAD_DETAILS':'off'}):off=poleprofiles.geographic_profile('europe')
        with mock.patch.dict(os.environ,{'BNE_EU_ROAD_DETAILS':'on'}):on=poleprofiles.geographic_profile('europe')
        self.assertNotEqual(off,on);self.assertIn(roadphones.signature(),on[2])

    def test_national_snapshots_are_nonempty_unique_and_attributed(self):
        phones,_=roadphones.bundled()
        self.assertGreater(len(phones['phones']),10000)
        self.assertEqual(len(phones['phones']),len({r['id']for r in phones['phones']}))
        self.assertEqual(phones['license'],'ODbL-1.0');self.assertIn('OpenStreetMap',phones['attribution'])
        import directionsigns
        data=directionsigns.bundled(True)
        self.assertGreater(len(data.signs),2000)
        self.assertEqual({r['colour']for r in data.signs},{'white','blue','green'})
        for lat,lon in ((48.6,7.7),(50.63,3.06),(48.4,-4.49),(43.3,5.37),(42.3,9.15)):
            frame=geodesy.EnuFrame(lat,lon)
            self.assertTrue(roadphones.nearby(frame,(-15000,-15000,15000,15000)))


if __name__=='__main__':unittest.main()
