"""Notes dimensions survive clipping, short ramps and fixed-height structures."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import unittest
from types import SimpleNamespace
import numpy as np
import roads, roadmarkings, markings, roadmesh
from test_roads import features, rules, FRAME, flat

LINE = np.array([[0.,0.,10.],[160.,0.,10.]])

def spec(state='', link=False):
    return roads.Spec(roads.carriageway('motorway','paved',False,oneway=True,lanes=2,link=link),
                      'paved',False,'motorway',True,dir=1,lanes=2,link=link,state=state or 'GA')

class ProfileTest(unittest.TestCase):
    def test_measured_dimensions_and_widths(self):
        expected=[('',False,3.,8.,.35),('',True,1.2,2.8,.35),
                  ('MI',False,4.5,11.5,.4),('MI',True,1.5,6.,.35),
                  ('MN',False,3.1,12.25,.3),('KY',False,3.5,8.2,.3),
                  ('WI',False,5.15,10.25,.31),('WI',True,1.25,2.9,.3),
                  ('CA',False,4.6,10.5,.35),('CA',True,2.3,4.9,.25)]
        for state,link,length,gap,width in expected:
            with self.subTest(state=state,link=link):
                ex=roads.extras(spec(state,link),LINE,-85.,defer_dashes=True)
                look,w,line=next(row for row in ex if 'dash' in row[0])
                chunks=roadmarkings.paint([roads._chunk(look,w,line)])
                self.assertAlmostEqual(roadmarkings.arc(np.array(chunks[0]['n']))[-1],length)
                self.assertAlmostEqual(chunks[1]['n'][0][0]-chunks[0]['n'][-1][0],gap)
                self.assertEqual(chunks[0]['w'],width)
                ribbon=roadmesh.ribbons(chunks[:1])[0]
                np.testing.assert_allclose(np.linalg.norm(ribbon[0]-ribbon[1],axis=1),width)
                for lk,w,_ in ex:
                    if lk.get('kind')=='edge':self.assertEqual(w,roadmarkings.profile(state).solid)

    def test_no_ramp_dashes_in_ky_mn_but_through_lanes_still_marked(self):
        for state in ('KY','MN'):
            ramp=spec(state,True)
            for sp,expected in ((ramp,False),(ramp._replace(link=False),True)):
                ex=roads.extras(sp,LINE,-85.,defer_dashes=True)
                self.assertEqual(any('dash' in lk for lk,_,_ in ex),expected)
                self.assertEqual(sum(lk.get('kind')=='edge' for lk,_,_ in ex),2)

    def test_phase_and_height_survive_tile_cuts(self):
        look,w,line=next(r for r in roads.extras(spec(),LINE,-85.,defer_dashes=True) if 'dash' in r[0])
        chunks=[]
        for a,b in ((0.,12.),(12.,24.),(24.,40.)):
            piece=np.array([[a,line[0,1],10.],[b,line[0,1],10.]])
            chunk=roads._chunk(look,w,piece);chunk['fixed']=True
            chunks+=roadmarkings.paint([chunk])
        np.testing.assert_allclose([(c['n'][0][0],c['n'][-1][0]) for c in chunks],
                                   [(0.,3.),(11.,12.),(12.,14.),(22.,24.),(24.,25.),(33.,36.)])
        self.assertTrue(all(c['fixed'] and all(p[2]==10. for p in c['n']) for c in chunks))
        self.assertEqual(roadmarkings.paint(chunks),chunks)

    def test_short_ramp_dashes_survive_ground_bridge_and_tunnel(self):
        f=features([[(10.,100.),(170.,100.)]],['motorway'])
        f.rules['oneway']=rules([(0,1.)],'float');f.rules['subclass']=rules([(0,'link')])
        f.attrs['lane_count']=np.array([2],np.int32)
        ground=roads.build_tile(FRAME,0,0,[f],flat,terrain_paint=False)
        sp=spec(link=True)
        tunnel=SimpleNamespace(parts=lambda cells,box:[('ramp',sp,LINE[:,:2],LINE[:,2],-85.)])
        bridge=roads._deck_chunks([('ramp',sp,LINE,-85.)],(0.,-30.,160.,30.))
        tube=roads._tunnel_chunks(tunnel,[],(0.,-30.,160.,30.))
        for chunks in (ground,bridge,tube):
            lengths=[roadmarkings.arc(np.array(c['n']))[-1] for c in chunks if c['m']=='line_white']
            self.assertGreater(sum(abs(n-1.2)<.002 for n in lengths),20)

    def test_foreign_and_minor_road_styles_remain_native(self):
        for country,lon in (('GB',0.),('DE',10.),('CA',-100.)):
            ex=roads.extras(spec(),LINE,lon,country=country,defer_dashes=True)
            dashes = [p for lk,_,p in ex if lk.get('kind') == 'lane' and lk.get('dash')]
            self.assertTrue(dashes)
            self.assertAlmostEqual(roadmarkings.arc(dashes[0])[-1], markings.style(country).lane_motorway[0])
        self.assertIsNone(roadmarkings.for_road(spec('MI')._replace(cls='primary'),country='US'))

    def test_state_metadata_on_roads_and_bridges(self):
        f=features([[(0,0),(100,0)]],['motorway'],flags=[(0,'is_bridge',.3,.6)])
        f.lonlat[:]=[[-84.55,42.73],[-84.549,42.73]]
        self.assertEqual(roads.road_specs(f)[0].state,'MI')
        self.assertEqual(roads.bridge_specs(f)[0].state,'MI')


class NorwegianHighwayMarkings(unittest.TestCase):
    def test_trunk_line_width_follows_posted_speed(self):
        trunk = roads.Spec(7.3, "paved", False, "trunk", False, lanes=2)
        for speed, expected in ((80, .10), (90, .15)):
            paint = roads.extras(trunk._replace(kmh=speed), LINE, 10., country="NO")
            widths = {width for look, width, _ in paint if look.get("kind") in ("centre", "edge")}
            self.assertEqual(widths, {expected})

    def test_direction_and_width_of_motorway_and_ramp_edges(self):
        for direction in (1, -1):
            main = spec()._replace(state=None, dir=direction, kmh=100)
            for road, in_tunnel, expected_width in (
                (main, False, .30), (main, True, .15),
                (main._replace(kmh=80), False, .15), (main._replace(link=True), False, .15),
            ):
                with self.subTest(direction=direction, road=road, in_tunnel=in_tunnel):
                    paint = roads.extras(road, LINE, 10.0, country="NO", in_tunnel=in_tunnel)
                    edges = {look["side"]: (look, width) for look, width, _ in paint if look.get("kind") == "edge"}
                    self.assertEqual(edges[direction][0]["m"], "line_yellow")
                    self.assertEqual(edges[-direction][0]["m"], "line_white")
                    self.assertTrue(all(width == expected_width and "dash" not in look for look, width in edges.values()))
                    white = [(width, path) for look, width, path in paint if look.get("kind") == "lane"]
                    self.assertAlmostEqual(roadmarkings.arc(white[0][1])[-1], 3.)
                    self.assertAlmostEqual(white[1][1][0, 0] - white[0][1][-1, 0], 9.)
                    self.assertEqual(white[0][0], .15)

    def test_mapped_auxiliary_lane_keeps_short_wide_white_dashes(self):
        highway = spec()._replace(width=17., state=None, kmh=100, lanes=3)
        paint = roads.extras(highway, LINE, 10., country="NO", osm={"lanes": 3},
                             lane_growth=(-1, 1, np.zeros(len(LINE))))
        rows = [(look, width, path) for look, width, path in paint if look.get("kind") == "lane"]
        for width, length, opening in ((.30, 2., 2.), (.15, 3., 9.)):
            stripe = [path for look, w, path in rows if w == width and look["m"] == "line_white"]
            self.assertGreater(len(stripe), 1)
            self.assertAlmostEqual(roadmarkings.arc(stripe[0])[-1], length)
            self.assertAlmostEqual(stripe[1][0, 0] - stripe[0][-1, 0], opening)

if __name__=='__main__':unittest.main()
