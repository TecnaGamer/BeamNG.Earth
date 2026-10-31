"""Cross-border, placement, asset and cache regressions for the European kit.

Real offline boundaries/localities plus synthetic roads. These cannot replace
an in-game Windows check of lighting, BeamNG importing and driving performance.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'tools')]
import countrybounds,europeinfra as ei,furniture,geodesy,poleprofiles as pp
import regionalmarkers,regionalstops,roads,signmesh,townsigns
import build_european_infrastructure as assets

NS={'c':'http://www.collada.org/2005/11/COLLADASchema'}

class GeographyTest(unittest.TestCase):
    def test_quebec_not_all_canada_and_turkey(self):
        points=[(45.5017,-73.5673,'stop_arret'),(46.8139,-71.2080,'stop_arret'),
                (45.4772,-75.7016,'stop_arret'),(45.4215,-75.6972,'stop'),
                (43.6532,-79.3832,'stop'),(46.0878,-64.7782,'stop'),
                (40.7128,-74.006,'stop'),(41.0082,28.9784,'stop_dur'),
                (39.9334,32.8597,'stop_dur'),(40.64,22.94,'stop')]
        for lat,lon,want in points:
            with self.subTest(lat=lat,lon=lon):
                self.assertEqual(regionalstops.style(countrybounds.bundled().at(lat,lon),lat,lon),want)
    def test_country_gate_overrides_province_geometry(self):
        self.assertEqual(regionalstops.style(None,45.5,-73.56),'stop')
        self.assertEqual(regionalstops.style('US',45.5,-73.56),'stop')
        self.assertEqual(regionalstops.style('CA',0,0),'stop')
    def test_actual_border_changes_poles_without_airports(self):
        token=pp.geographic_profile('europe')
        for lat,lon,code in [(49.026,7.816,'FR'),(49.06593,7.79547,'DE'),
                             (55.75,37.62,'RU'),(39.9334,32.8597,'TR'),(45.50,-73.56,'CA')]:
            frame=geodesy.EnuFrame(lat,lon)
            self.assertEqual(pp.country_selector(token,frame)(0,0,0),code)
            style=pp.selector(token,frame)('road',0,0,0)
            self.assertEqual(style,pp.choose(pp.profile_for(code,'europe'),'road'))
    def test_unknown_and_off_still_stock(self):
        for mode,code in [('off','DE'),('fr-varied','DE'),('europe',None),('europe','US'),('europe','CA')]:
            self.assertEqual(pp.choose(pp.profile_for(code,mode),'x'),pp.STOCK)
    def test_cache_token_contains_all_geographic_sources(self):
        token=pp.geographic_profile('europe')
        for text in (ei.VERSION,townsigns.EU_DATA_SHA256,regionalstops.DATA_SHA256,countrybounds.DATA_SHA256):
            self.assertIn(text,token[2])
        frame=geodesy.EnuFrame(52.5,13.4)
        with self.assertRaises(ValueError):pp.selector((token[0],token[1],token[2]+'old'),frame)


class StopTest(unittest.TestCase):
    def junction(self):
        spec=roads.Spec(7.,'paved',True,'residential',False)
        return [(str(i),spec,np.array([[0.,0,0],[x,y,0]]),7.8)
                for i,(x,y) in enumerate(((0,180),(0,-180),(180,0)))]
    def test_only_model_changes_in_real_furniture_pipeline(self):
        rr=self.junction();box=(-200,-200,200,200)
        stock=[r for r in furniture.place(rr,[],box) if r[0]=='stop']
        self.assertGreater(len(stock),0)
        for lat,lon,want in ((45.5017,-73.5673,'stop_arret'),(39.9334,32.8597,'stop_dur'),(45.4215,-75.6972,'stop')):
            rows=furniture.place(rr,[],box,pole_profile=pp.geographic_profile('europe'),pole_frame=geodesy.EnuFrame(lat,lon))
            stops=[r for r in rows if r[0] in ('stop','stop_arret','stop_dur')]
            self.assertEqual([r[1:] for r in stops],[r[1:] for r in stock])
            self.assertEqual({r[0] for r in stops},{want})
    def test_localization_does_not_mutate_input_or_other_props(self):
        row=['stop',0.,0.,0.,0.,1.,3.,0.];other=['giveway',1.,1.,0.,0.,1.,3.,0.]
        source=[row,other];frame=geodesy.EnuFrame(39.9334,32.8597)
        out=regionalstops.localize(source,pp.geographic_profile('europe'),frame,lambda *p:'TR')
        self.assertEqual(row[0],'stop');self.assertEqual(out[0][0],'stop_dur');self.assertEqual(out[1],other)
    def test_old_modes_keep_original_stop_even_inside_quebec(self):
        rows=[['stop',0,0,0,0,1,3,0]]
        for profile in (None,pp.FR_KIT,pp.geographic_profile('fr-varied')):
            self.assertIs(regionalstops.localize(rows,profile,None,lambda *p:'CA'),rows)


class ModelsTest(unittest.TestCase):
    def test_every_pole_has_model_and_attached_conductors(self):
        catalog=json.loads((assets.OUT/'catalog.json').read_text())
        seen=set()
        for code in ei.COUNTRIES:
            for fid in map(str,range(30)):
                style=pp.choose(pp.profile_for(code,'europe'),fid)
                self.assertNotEqual(style,pp.STOCK)
                if style.group.startswith('eu_pole_'):
                    seen.add(style.group);self.assertIn(style.group,catalog)
                    mesh=assets.pole(style.group[8:]);points=np.asarray(mesh.p)
                    for anchor in style.wire_anchors:
                        self.assertLess(np.linalg.norm(points-np.asarray(anchor),axis=1).min(),.07)
        self.assertEqual(seen,{'eu_pole_'+k for k in ei.POLE_LAYOUTS})
    def test_choices_stable_with_road_order(self):
        for code in ('FR','PL','RU','TR','SE'):
            profile=pp.profile_for(code,'europe');ids=list(map(str,range(50)))
            self.assertEqual({i:pp.choose(profile,i) for i in ids},{i:pp.choose(profile,i) for i in reversed(ids)})
    def test_delineators_have_no_collision_node_at_any_lod(self):
        catalog=json.loads((assets.OUT/'catalog.json').read_text())
        for key,value in catalog.items():
            if not key.startswith('eu_marker_'):continue
            self.assertTrue(value['no_collision'])
            tree=ET.parse(assets.OUT/('bne_'+key+'.dae'))
            self.assertFalse(any('col' in n.get('id','').lower() for n in tree.findall('.//c:node',NS)))
            self.assertEqual(len(tree.findall('.//c:geometry',NS)),3)
    def test_all_meshes_finite_valid_indices_budget_and_materials(self):
        materials={}
        for folder in ('europe_infrastructure','regional_signs'):
            materials.update(json.loads((assets.OUT.parent/folder/'main.materials.json').read_text()))
        for p in assets.OUT.glob('*.dae'):
            tree=ET.parse(p)
            for m in tree.findall('.//c:material',NS):self.assertIn(m.get('name'),materials)
            for array in tree.findall('.//c:float_array',NS):
                a=np.fromstring(array.text or '',sep=' ')
                self.assertTrue(np.isfinite(a).all(),p.name);self.assertEqual(len(a),int(array.get('count')))
            for g in tree.findall('.//c:geometry',NS):
                verts=g.find('.//c:float_array',NS);count=int(verts.get('count'))//3
                tris=g.findall('.//c:triangles',NS)
                self.assertLess(sum(int(t.get('count')) for t in tris),2500,p.name)
                for t in tris:
                    ix=np.fromstring(t.find('c:p',NS).text or '',sep=' ',dtype=int)
                    self.assertTrue(np.all((ix>=0)&(ix<count)),p.name)
    def test_poles_have_three_decreasing_lods(self):
        for kind in ei.POLE_LAYOUTS:
            counts=[sum(map(len,assets.pole(kind,d).tri.values())) for d in range(3)]
            self.assertGreater(counts[0],counts[2]);self.assertGreaterEqual(counts[0],counts[1])
    def test_unknown_layout_never_connects_three_wires_to_one(self):
        eu=pp.choose(pp.profile_for('DE','europe'),'a')
        run=[(0.,0.,0.,0.,pp.WOOD),(50.,0.,0.,0.,eu)]
        self.assertEqual(furniture._own_spans(run,(-1,-1,100,100)),[])
    def test_pole_wires_survive_tile_split(self):
        style=pp.choose(pp.profile_for('PL','europe'),'a')
        run=[(x,0.,0.,45.,style) for x in (0.,50.,100.,150.)]
        whole=furniture._own_spans(run,(-1,-1,160,10))
        split=furniture._own_spans(run,(-1,-1,80,10))+furniture._own_spans(run,(80,-1,160,10))
        self.assertEqual(len(whole),len(split))
        for a,b in zip(whole,split):np.testing.assert_array_equal(a[0],b[0])


class MarkerTest(unittest.TestCase):
    def curve(self):
        sp=roads.Spec(7.,'paved',False,'secondary',False)
        a=np.linspace(-1.8,1.8,41);line=np.c_[25*np.sin(a),25*np.cos(a),np.zeros(len(a))]
        return [('curve',sp,line,7.8)]
    def test_country_chevrons_and_no_foreign_french_j3(self):
        for code,scheme in [('DE','red_white'),('FR','white_blue'),('SE','yellow_blue'),('RU','white_red'),('IE','yellow_black')]:
            rows=regionalmarkers.place(self.curve(),(-200,-200,200,200),lambda *p:code)
            chevrons=[r for r in rows if '_chevron_' in r[0] or r[0].startswith('fr_j4')]
            self.assertTrue(chevrons)
            if code!='FR':self.assertTrue(all(not r[0].startswith('fr_') for r in rows))
            for r in chevrons:self.assertIn(scheme,r[0]) if code!='FR' else self.assertTrue(r[0].startswith('fr_j4'))
    def test_markers_split_tiles_and_border_gate(self):
        rr=self.curve();lookup=lambda x,y,z:'DE' if x<0 else 'PL'
        full=regionalmarkers.place(rr,(-200,-200,200,200),lookup)
        split=regionalmarkers.place(rr,(-200,-200,0,200),lookup)+regionalmarkers.place(rr,(0,-200,200,200),lookup)
        self.assertEqual(sorted(full),sorted(split))
        for r in full:
            if '_marker_' in r[0]:self.assertIn('de' if r[1]<0 else 'pl',r[0])
            else:self.assertIn('red_white',r[0])
    def test_urban_motorway_link_exclusions(self):
        fid,sp,line,z=self.curve()[0]
        for spec in (sp._replace(urban=True),sp._replace(cls='motorway'),sp._replace(link=True)):
            self.assertEqual(regionalmarkers.place([(fid,spec,line,z)],(-200,-200,200,200),lambda *p:'DE'),[])
    def test_opposite_verges_expose_opposite_reflectors(self):
        fid=next(str(i) for i in range(100) if furniture._h01(str(i),'fr-j6-section-v2')<.08)
        sp=roads.Spec(7.,'paved',False,'secondary',False);rr=[(fid,sp,np.array([[0.,0,0],[800.,0,0]]),7.8)]
        rows=regionalmarkers.place(rr,(-20,-20,820,20),lambda *p:'DE')
        self.assertGreater(len(rows),4)
        for a,b in zip(rows[::2],rows[1::2]):self.assertEqual(abs(a[4]-b[4]),180)


class TownTest(unittest.TestCase):
    def test_real_names_and_source_counts(self):
        data=townsigns.bundled_europe()
        for bounds,fragment in [((13.3,52.4,13.5,52.6),'Berlin'),((37.4,55.6,37.9,55.9),'Москва'),
                                ((22.8,40.4,23.1,40.7),'Θεσσαλονίκη')]:
            self.assertIn(fragment,[r['name'] for r in data.nearby(bounds,True)])
        self.assertGreater(sum(data.metadata['counts'].values()),300000)
    def test_shared_font_has_native_scripts_and_arret(self):
        glyphs=signmesh.font()['glyphs']
        for name in ('ARRÊT','DUR','МОСКВА','КИЇВ','ŁÓDŹ','ΘΕΣΣΑΛΟΝΊΚΗ','ČESKÝ KRUMLOV','თბილისი','Երևան'):
            for ch in name:self.assertIn(ch,glyphs)
    def test_country_board_materials_and_exit_variants(self):
        for code,expected in [('DE',signmesh.YELLOW),('PL',signmesh.GREEN),('SE',signmesh.BLUE),('AT',signmesh.BLUE),('RU',signmesh.WHITE)]:
            mesh=signmesh.Mesh();mesh.town((0,0,0),(0,1),'Teststadt',style=ei.town_style(code))
            self.assertIn(expected,mesh.tm);self.assertTrue(np.isfinite(mesh.p).all())
        self.assertEqual(ei.town_style('GB').exit,'none')
        self.assertEqual(ei.town_style('PL').meaning,'place-name')
        self.assertNotEqual(ei.town_style('CH','primary'),ei.town_style('CH','residential'))
        self.assertIsNone(ei.town_style('RO').header_speed)
        self.assertEqual(ei.town_style('RO','secondary',50).header_speed,50)
        self.assertEqual(ei.town_style('NL').exit,'slash')
        for code in ('LV','LT'):
            self.assertEqual(ei.town_style(code).background,'blue')
            self.assertEqual(ei.town_style(code).meaning,'place-name')
    def test_estimated_multiple_approaches_and_left_hand_shoulder(self):
        for lat,lon,code in ((52.5,13.4,'DE'),(52.0,-1.0,'GB')):
            frame=geodesy.EnuFrame(lat,lon);sp=roads.Spec(7.,'paved',False,'secondary',False)
            rr=[('east',sp,np.array([[-500.,0,0],[500.,0,0]]),7.8),
                ('north',sp,np.array([[0.,-500,0],[0.,500,0]]),7.8)]
            la,lo,_=frame.enu_to_geodetic(0,0,0)
            data=townsigns.SignData({'signs':[],'places':[{'id':'test','name':'Test Village','country':code,'lat':float(la),'lon':float(lo)}]})
            def urban(a,b):
                x,y,_=frame.geodetic_to_enu(a,b,np.zeros_like(a));return (abs(x)<230)&(abs(y)<230)
            mesh,posts,records=townsigns.place(frame,rr,(-600,-600,600,600),pp.geographic_profile('europe'),urban=urban,data=data)
            self.assertEqual(len(records),4);self.assertTrue(posts);self.assertIsNotNone(mesh)
            east=min(records,key=lambda r:abs(r['pos'][0]+230))
            self.assertLess(east['pos'][1],0) if code=='DE' else self.assertGreater(east['pos'][1],0)
    def test_foreign_nearby_place_not_used_across_border(self):
        frame=geodesy.EnuFrame(52.5,13.4);sp=roads.Spec(7.,'paved',False,'secondary',False)
        rr=[('east',sp,np.array([[-500.,0,0],[500.,0,0]]),7.8)]
        data=townsigns.SignData({'signs':[],'places':[{'id':'foreign','name':'Foreign','country':'PL','lat':52.5,'lon':13.4}]})
        def urban(a,b):
            x,y,_=frame.geodetic_to_enu(a,b,np.zeros_like(a));return x>0
        self.assertEqual(townsigns.place(frame,rr,(-600,-600,600,600),pp.geographic_profile('europe'),urban=urban,data=data)[2],[])
    def test_town_cdae_binds_coloured_and_white_glyph_materials(self):
        import cdae
        mesh=signmesh.Mesh();mesh.town((0,0,0),(0,1),'Москва',style=ei.TOWNS['blue'])
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'town.cdae';mesh.write(p);shape=cdae.read(p)
            names={m['name'] for m in shape['materials']}
            self.assertIn('bne_sign_blue',names);self.assertIn('bne_sign_glyph_white',names)

if __name__=='__main__':unittest.main()
