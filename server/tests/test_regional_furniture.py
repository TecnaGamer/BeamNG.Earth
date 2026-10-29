"""Regional kit: geometry, country gating, sign layout, placement and real serialization."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'tools')]
import furniture
import geodesy
import poleprofiles as pp
import regionalmarkers
import roads
import signmesh
import townsigns
import directionsigns
import build_french_furniture as assets
from test_regional_poles import road,wire_rows


class FurnitureTest(unittest.TestCase):
    def test_stable_french_mix_and_country_gate(self):
        styles=[pp.choose(pp.FR_KIT,str(i)) for i in range(500)]
        self.assertEqual(set(styles),{pp.WOOD,pp.RECESSED})
        self.assertGreater(styles.count(pp.WOOD),styles.count(pp.RECESSED))
        self.assertEqual(styles,[pp.choose(pp.FR_KIT,str(i)) for i in range(500)])
        for country in ('DE','US','JP',None):self.assertEqual(pp.choose(pp.profile_for(country,'fr-varied'),'1'),pp.STOCK)
        for lat,lon,expected in ((49.026,7.816,True),(49.06593,7.79547,False)):
            frame=geodesy.EnuFrame(lat,lon)
            self.assertEqual(pp.kit_selector(pp.geographic_profile('fr-varied'),frame)(0,0,0),expected)
    def test_wire_bundle_joins_wood_and_concrete_but_not_stock(self):
        a=(0.,0.,0.,0.,pp.WOOD);b=(0.,40.,0.,0.,pp.RECESSED)
        self.assertTrue(furniture._own_spans([a,b],(-10,-10,50,50)))
        self.assertEqual(furniture._own_spans([a,(*b[:4],pp.STOCK)],(-10,-10,50,50)),[])
    def test_lantern_replaces_buried_village_lamps_and_emits_warm_heads(self):
        fid=next(str(i) for i in range(100) if furniture._h01(str(i),'lantern')<.7)
        args=([road(fid,True)],[],(0,0,256,256))
        rows=furniture.place(*args,town=True,buried=1,pole_profile=pp.FR_KIT)
        lamps=[r for r in rows if r[0]=='fr_lantern'];heads=[r for r in rows if r[0]=='fr_lantern_light']
        self.assertTrue(lamps);self.assertEqual(len(lamps),len(heads))
        for lamp,head in zip(lamps,heads):self.assertAlmostEqual(head[3]-lamp[3],4.64,places=2)
        stock=furniture.place(*args,town=True,buried=1)
        self.assertFalse(any(r[0].startswith('fr_') for r in stock))
    def test_rural_markers_own_tiles_and_do_not_cross_country(self):
        spec=roads.Spec(7.,'paved',False,'secondary',False)
        fid=next(str(i) for i in range(1000) if furniture._h01(str(i),'fr-j6-section-v2')<.08)
        rr=[(fid,spec,np.array([[-250.,50,0],[600.,50,0]]),7.8)]
        union=regionalmarkers.place(rr,(0,0,512,256),lambda *p:True)
        split=regionalmarkers.place(rr,(0,0,256,256),lambda *p:True)+regionalmarkers.place(rr,(256,0,512,256),lambda *p:True)
        self.assertTrue(union);self.assertEqual(sorted(union),sorted(split))
        self.assertEqual(regionalmarkers.place(rr,(0,0,512,256),lambda *p:False),[])
    def test_red_markers_at_junction_and_chevrons_only_on_sharp_curve(self):
        spec=roads.Spec(6.,'paved',False,'secondary',False)
        rr=[(str(i),spec,np.array(p,float),7.8) for i,p in enumerate([
            [[-150,0,0],[0,0,0]],[[0,0,0],[150,0,0]],[[0,0,0],[0,150,0]]])]
        r=regionalmarkers.place(rr,(-256,-256,256,256),lambda *p:True)
        self.assertEqual(sum(row[0]=='fr_j3' for row in r),3)
        angles=np.linspace(-1.3,1.3,35);curve=np.c_[25*np.cos(angles),25*np.sin(angles),np.zeros(len(angles))]
        r=regionalmarkers.place([('curve',spec,curve,7.8)],(-256,-256,256,256),lambda *p:True)
        self.assertTrue(any(row[0].startswith('fr_j4') for row in r));self.assertTrue(any(row[0]=='fr_j1' for row in r))


class ModelTest(unittest.TestCase):
    def assert_dae_matches(self, reference, generated):
        """Compare the export contract, not OS-dependent floating-point text.

        The shared COLLADA exporter writes five significant digits. A relative
        tolerance of 1e-4 permits one final digit at that precision; 1e-12 only
        handles near-zero trigonometry/normalisation residues. Everything else
        (indices, hierarchy, bindings, LOD/collision names and units) stays exact.
        """
        expected = list(ET.parse(reference).getroot().iter())
        actual = list(ET.parse(generated).getroot().iter())
        self.assertEqual(len(expected), len(actual), str(reference))
        for index, (a, b) in enumerate(zip(expected, actual)):
            context = f"{Path(reference).name}: element {index}, {a.get('id', a.tag)}"
            self.assertEqual(a.tag, b.tag, context)
            self.assertEqual(a.attrib, b.attrib, context)
            self.assertEqual(len(a), len(b), context)
            if a.tag == '{http://www.collada.org/2005/11/COLLADASchema}float_array':
                aa = np.asarray([float(v) for v in (a.text or '').split()])
                bb = np.asarray([float(v) for v in (b.text or '').split()])
                self.assertEqual(aa.shape, (int(a.attrib['count']),), context)
                self.assertEqual(bb.shape, aa.shape, context)
                self.assertTrue(np.isfinite(aa).all() and np.isfinite(bb).all(), context)
                np.testing.assert_allclose(aa, bb, rtol=1e-4, atol=1e-12,
                                           equal_nan=False, err_msg=context)
            else:
                self.assertEqual((a.text or '').split(), (b.text or '').split(), context)
            self.assertEqual((a.tail or '').split(), (b.tail or '').split(), context)
    def test_lods_normals_budgets_and_materials(self):
        mats=json.loads((assets.OUT/'main.materials.json').read_text())
        for kind in assets.HEIGHTS:
            counts=[]
            for m in [assets.model(kind,i) for i in range(3)]+[assets.collision(kind)]:
                p,n=np.array(m.p),np.array(m.n);t=np.array([tri for ts in m.tri.values() for tri in ts])
                counts.append(len(t));self.assertLess(len(p),3000);self.assertTrue(np.isfinite(p).all())
                np.testing.assert_allclose(np.linalg.norm(n,axis=1),1.,atol=1e-12)
                self.assertTrue((np.linalg.norm(np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]]),axis=1)>1e-10).all())
                self.assertTrue(set(m.tri)<=set(mats))
            self.assertGreaterEqual(counts[0],counts[1]);self.assertGreaterEqual(counts[1],counts[2]);self.assertLessEqual(counts[3],24)
    def test_chevron_faces_stay_on_board(self):
        for kind in ('j4_l','j4_r'):
            m=assets.model(kind);p=np.array(m.p);t=np.array(m.tri[assets.CHEVRON]);v=p[t]
            self.assertLessEqual(np.abs(v[:,:,0]).max(),1.)
    def test_exported_assets_reproduce_geometry(self):
        with tempfile.TemporaryDirectory() as d:
            assets.build(d)
            names = {'bne_fr_' + kind + '.dae' for kind in assets.HEIGHTS}
            self.assertEqual({p.name for p in assets.OUT.glob('*.dae')}, names)
            self.assertEqual({p.name for p in Path(d).glob('*.dae')}, names)
            for name in sorted(names):
                with self.subTest(model=name):
                    self.assert_dae_matches(assets.OUT/name, Path(d, name))
    def test_glyph_opacity_and_reflector_materials(self):
        from PIL import Image
        folder=assets.OUT.parent/'regional_signs'
        mats=json.loads((folder/'main.materials.json').read_text())
        glyph=mats['bne_sign_glyph'];self.assertTrue(glyph['alphaTest'])
        opacity=folder/Path(glyph['Stages'][0]['opacityMap']).name
        with Image.open(folder/'sign_font.png') as colour,Image.open(opacity) as mask:
            self.assertTrue(all(v<=4096 and v&(v-1)==0 for v in colour.size))
            np.testing.assert_array_equal(np.array(colour.getchannel('A')),np.array(mask))
        self.assertGreater(mats['bne_sign_white']['Stages'][0]['retroreflectivity'],0)


class SignsTest(unittest.TestCase):
    def setUp(self):
        self.frame=geodesy.EnuFrame(49.026,7.816)
        self.road=road('mapped',True)
        la,lo,_=self.frame.enu_to_geodetic(100,50,3)
        cla,clo,_=self.frame.enu_to_geodetic(400,50,3)
        self.record={'id':1,'lat':float(la),'lon':float(lo),'name':'Lembach','kind':'both','ref':'D927','centre':[float(clo),float(cla)]}
        self.data=townsigns.SignData({'signs':[self.record],'places':[]})
    def test_long_names_accents_bounds_and_unknown_glyph(self):
        for name in ('Wingen','Niederschaeffolsheim','Gœrsdorf','Saint-Remy-en-Bouzemont-Saint-Genest-et-Isson','Très\nlong\x00 nom 😀'):
            text,w,h,letter=signmesh.layout(name)
            self.assertLessEqual(signmesh.advance(text)*letter,w-.28+1e-9)
            m=signmesh.Mesh();m.town((0,0,0),(0,1),name,'D 263')
            self.assertTrue(np.isfinite(np.array(m.p)).all());self.assertLess(len(m.p),3000)
        self.assertIn('Œ',signmesh.font()['glyphs'])
        self.assertEqual(signmesh.valid_route('D927'),'D 927');self.assertEqual(signmesh.valid_route('N 4'),'')
    def test_relative_cardinal_orientation_and_unknown(self):
        tangent=np.array([1.,0.])
        np.testing.assert_allclose(townsigns.entry_direction({'direction':'forward','bearing':90},tangent),[1,0],atol=1e-10)
        np.testing.assert_allclose(townsigns.entry_direction({'direction':'W'},tangent),[1,0],atol=1e-10)
        np.testing.assert_allclose(townsigns.entry_direction({'direction':'W','kind':'end'},tangent),[-1,0],atol=1e-10)
        self.assertIsNone(townsigns.entry_direction({},tangent))
        self.assertIsNone(townsigns.entry_direction({'direction':'forward'},tangent))
    def test_placement_at_mapped_node_outside_carriageway(self):
        mesh,posts,records=townsigns.place(self.frame,[self.road],(0,0,256,256),pp.FR_KIT,data=self.data)
        self.assertIsNotNone(mesh);self.assertTrue(posts);self.assertEqual(len(records),1)
        self.assertEqual(records[0]['name'],'Lembach');self.assertEqual(records[0]['route'],'D 927')
        self.assertLess(records[0]['pos'][1],46.)
        self.assertIn('orientation-estimated',records[0]['source'])
    def test_tile_ownership_no_duplicate_and_no_sign_abroad(self):
        cases=[townsigns.place(self.frame,[self.road],b,pp.FR_KIT,data=self.data)[2] for b in ((0,0,512,256),(0,0,256,256),(256,0,512,256))]
        self.assertEqual(cases[0],cases[1]+cases[2])
        mesh,posts,records=townsigns.place(self.frame,[self.road],(0,0,256,256),None,data=self.data)
        self.assertEqual((mesh,posts,records),(None,[],[]))
    def test_real_cdae_tile_and_common_materials(self):
        mesh,posts,records=townsigns.place(self.frame,[self.road],(0,0,256,256),pp.FR_KIT,data=self.data)
        with tempfile.TemporaryDirectory() as d:
            roads.write_tile(d,'/temp/test',1,0,0,[],props=posts,signs=[(mesh,records)])
            j=json.loads(Path(d,'roads_1_0_0.json').read_text());self.assertEqual(j['regionalSigns'][0]['name'],'Lembach')
            import cdae
            result=cdae.read(Path(d,'signs_1_0_0.cdae'));self.assertIsNotNone(result)
            self.assertIn('fr_signpost',j['props']['groups'])
            self.assertEqual(roads.shape_files(1,0,0)['signs'],'signs_1_0_0.cdae')
    def test_estimated_entry_requires_sustained_built_up_transition(self):
        frame=geodesy.EnuFrame(49.026,7.816)
        la,lo,_=frame.enu_to_geodetic(500,100,0)
        data=townsigns.SignData({'signs':[],'places':[{'id':50,'lat':float(la),'lon':float(lo),'name':'Village-test'}]})
        spec=roads.Spec(8.,'paved',False,'secondary',False)
        rr=[('r',spec,np.array([[-200.,100.,0.],[900.,100.,0.]]),7.8)]
        def urban(lat,lon):
            x,y,z=frame.geodetic_to_enu(lat,lon,np.zeros_like(lat));return np.asarray(x)>150
        _,_,rec=townsigns.place(frame,rr,(0,0,256,256),pp.FR_KIT,data=data,urban=urban)
        self.assertEqual(len(rec),1);self.assertEqual(rec[0]['source'],'estimated')
        self.assertLess(abs(rec[0]['pos'][0]-150),45)
        _,_,rec=townsigns.place(frame,rr,(0,0,256,256),pp.FR_KIT,data=data,urban=lambda la,lo:np.zeros_like(la,dtype=bool))
        self.assertEqual(rec,[])
    def test_server_road_builder_forwards_real_signs(self):
        from test_roads import features,FRAME
        cells=[features([[(-200.,50.),(700.,50.)]],['residential'])]
        la,lo,_=FRAME.enu_to_geodetic(100,50,0);cla,clo,_=FRAME.enu_to_geodetic(400,50,0)
        record=dict(self.record,lat=float(la),lon=float(lo),centre=[float(clo),float(cla)])
        data=townsigns.SignData({'signs':[record],'places':[]})
        with mock.patch.object(townsigns,'bundled',return_value=data):
            props=[];signs=[]
            roads.build_tile(FRAME,0,0,cells,lambda la,lo:np.zeros_like(la),walks=[],props=props,
                             pole_profile=pp.FR_KIT,signs=signs)
        self.assertEqual(len(signs),1);self.assertTrue(any(p[0]=='fr_signpost' for p in props))


class DirectionTest(unittest.TestCase):
    def setUp(self):
        self.frame=geodesy.EnuFrame(49.026,7.816)
        def lonlat(x,y):
            la,lo,_=self.frame.enu_to_geodetic(x,y,0)
            return [float(lo),float(la)]
        pos=lonlat(100,42)
        self.record={'id':'relation/sign','lat':pos[1],'lon':pos[0],
                     'origin':lonlat(100,50),'towards':lonlat(150,50),'labels':['Schönau']}
        self.roads=[road('mapped',True)]
    def placed(self,rows,french=lambda *p:True):
        m=signmesh.Mesh()
        data=townsigns.SignData({'signs':rows,'places':[]})
        posts,records=directionsigns.add(self.frame,self.roads,(0,0,256,256),french,m,data=data)
        return m,posts,records
    def test_real_labels_both_faces_and_shared_support(self):
        rows=[self.record,dict(self.record,id='second',labels=['Wingen'])]
        m,posts,records=self.placed(rows)
        self.assertEqual(len(posts),1)
        self.assertEqual([r['name'] for r in records],['Schönau','Wingen'])
        p=np.array(m.p);t=np.array(m.t);tm=np.array(m.tm)
        n=np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]])
        # Both front and back text faces exist at two distinct stack heights.
        glyph=n[tm==signmesh.GLYPH];self.assertTrue((glyph[:,1]>0).any());self.assertTrue((glyph[:,1]<0).any())
        self.assertLess(p[t[tm==signmesh.GLYPH],2].min()-posts[0][3],2.1)
    def test_country_gate_and_nonroad_target_are_omitted(self):
        self.assertEqual(self.placed([self.record],lambda *p:False)[2],[])
        far=dict(self.record,towards=[7.816,49.04])
        self.assertEqual(self.placed([far])[2],[])
    def test_duplicate_relations_and_excess_stack_are_bounded(self):
        rows=[dict(self.record,id=str(i),labels=[str(i)]) for i in range(7)]
        _,posts,records=self.placed(rows+rows)
        self.assertEqual(len(posts),1);self.assertEqual(len(records),4)


class WorkerTest(unittest.TestCase):
    def test_worker_and_revisit_keep_sign_mesh_only_for_enabled_profile(self):
        import blockworker,threading,cdae
        from types import SimpleNamespace
        from test_roads import FRAME,LAT,LON,features
        from test_blockworker import FakeMapData,FakeDem
        f=features([[(-300.,100.),(600.,100.)]],['residential'])
        la,lo,_=FRAME.enu_to_geodetic(100,100,0);cla,clo,_=FRAME.enu_to_geodetic(400,100,0)
        data=townsigns.SignData({'signs':[{'id':1,'lat':float(la),'lon':float(lo),
            'name':'Village-test','centre':[float(clo),float(cla)]}],'places':[]})
        directions=townsigns.SignData({'signs':[],'places':[]})
        with tempfile.TemporaryDirectory() as d:
            state=SimpleNamespace(md=FakeMapData(f,roads.cells_of_tile(FRAME,0,0)[0]),dem=FakeDem(),
                widths=None,surface=None,airports=None,sea=None,lock=threading.Lock(),cache_dir=d,
                bridges={},anchor=lambda *args:(None,None))
            with mock.patch.object(blockworker,'_s',state),mock.patch.object(townsigns,'bundled',return_value=data),\
                    mock.patch.object(directionsigns,'bundled',return_value=directions):
                meshes=[]
                for i,profile in enumerate((pp.FR_KIT,None,pp.FR_KIT)):
                    out=Path(d,str(i));out.mkdir()
                    blockworker.roads_tile(1,LAT,LON,15,0,0,str(out),'/temp/kit',True,.05,
                                          buildings=True,pole_profile=profile)
                    result=json.loads((out/'roads_1_0_0.json').read_text())
                    if profile:
                        shape=out/Path(result['signs']['shape']).name
                        self.assertIsNotNone(cdae.read(shape));meshes.append(shape.read_bytes())
                        self.assertIn('fr_signpost',result['props']['groups'])
                        self.assertEqual(result['regionalSigns'][0]['name'],'Village-test')
                    else:
                        self.assertNotIn('signs',result)
                        self.assertNotIn('fr_signpost',result['props']['groups'])
                self.assertEqual(meshes[0],meshes[1])

if __name__=='__main__':unittest.main()
