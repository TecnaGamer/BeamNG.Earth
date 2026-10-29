"""Regression cases from the 1.7.1 European furniture field trial."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
import numpy as np
from PIL import Image
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'tools')]
import furniture,geodesy,navgraph,poleprofiles as pp,regionalmarkers,roads,sidewalks,townsigns
import build_european_surfaces as surfaces
import cdae
from roadapproaches import Context


class TownApproachesTest(unittest.TestCase):
    def setUp(self):
        self.frame=geodesy.EnuFrame(49.026,7.816)
        la,lo,_=self.frame.enu_to_geodetic(500,100,0)
        self.data=townsigns.SignData({'signs':[],'places':[{'id':50,'lat':float(la),'lon':float(lo),'name':'Village-test'}]})
        self.sp=roads.Spec(7.,'paved',False,'secondary',False)
    def urban(self,lat,lon):
        x,y,_=self.frame.geodetic_to_enu(lat,lon,np.zeros_like(lat))
        return np.asarray(x)>150
    def rows(self,cls='secondary'):
        return [(str(i),self.sp._replace(cls=cls),np.array([[x,100.,0.],[x+50,100.,0.]]),7.8)
                for i,x in enumerate(range(-200,900,50))]
    def placed(self,box,rr=None):
        return townsigns.place(self.frame,rr or self.rows(),box,pp.FR_KIT,data=self.data,urban=self.urban)[2]
    def test_short_features_find_entry_and_half_open_tile_ownership(self):
        union=self.placed((0,0,512,256))
        split=self.placed((0,0,153,256))+self.placed((153,0,512,256))
        self.assertEqual(len(union),1)
        self.assertEqual(union,split)
        self.assertLess(abs(union[0]['pos'][0]-150),1.)
    def test_garden_gap_on_residential_street_does_not_invent_entry(self):
        self.assertEqual(self.placed((0,0,512,256),self.rows('residential')),[])
    def test_short_context_is_not_stretched_across_a_gap(self):
        rr=self.rows();rr=[r for r in rr if r[2][0,0]!=200]
        self.assertEqual(self.placed((0,0,512,256),rr),[])
    def test_reversed_continuation_and_ambiguous_fork(self):
        a=('a',self.sp,np.array([[0.,0,0],[50.,0,0]]),7.8)
        b=('b',self.sp,np.array([[100.,0,0],[50.,0,0]]),7.8)
        np.testing.assert_allclose(Context([a,b]).point(0,75)[0],[75,0,0])
        c=('c',self.sp,np.array([[50.,0,0],[100.,5,0]]),7.8)
        self.assertIsNone(Context([a,b,c]).point(0,75))
    def test_lembach_unnamed_mapped_node_is_retained(self):
        row=next(r for r in townsigns.bundled().signs if r['id']==433289740)
        self.assertEqual(row['name'],'Lembach')
        self.assertTrue(row['name_estimated'])
        self.assertAlmostEqual(row['lat'],49.0114374)
    def test_mapped_sign_moves_clear_of_farm_access_instead_of_disappearing(self):
        la,lo,_=self.frame.enu_to_geodetic(150,100,0)
        place=self.data.places[0]
        record={'id':1,'lat':float(la),'lon':float(lo),'name':place['name'],
                'centre':[place['lon'],place['lat']],'kind':'both'}
        data=townsigns.SignData({'signs':[record],'places':[]})
        rr=[('main',self.sp,np.array([[-200.,100,0],[900.,100,0]]),7.8),
            ('access',self.sp._replace(cls='track',width=3.5),np.array([[150.,80,0],[150.,98,0]]),7.8)]
        rec=townsigns.place(self.frame,rr,(0,0,512,256),pp.FR_KIT,data=data)[2]
        self.assertEqual(len(rec),1)
        self.assertGreater(abs(rec[0]['pos'][0]-150),5)
        self.assertLess(abs(rec[0]['pos'][0]-150),17)


class EuropeTest(unittest.TestCase):
    def test_real_countries_and_french_only_markings(self):
        token=pp.geographic_profile('europe')
        for lat,lon,expected in [(49.026,7.816,'FR'),(49.06593,7.79547,'DE'),(40.71,-74.0,'US')]:
            f=geodesy.EnuFrame(lat,lon)
            pole=pp.selector(token,f)('road',0,0,0)
            self.assertEqual(pole,pp.choose(pp.profile_for(expected,'europe'),'road'))
            self.assertEqual(pp.kit_selector(token,f)(0,0,0),expected=='FR')
            self.assertEqual(pp.europe_selector(token,f)(0,0,0),expected in ('FR','DE'))
    def test_sparse_stretches_not_every_small_road(self):
        sp=roads.Spec(7.,'paved',False,'secondary',False)
        line=np.array([[0.,0,0],[1000.,0,0]])
        def put(fid,spec=sp):
            return regionalmarkers.place([(fid,spec,line,7.8)],(-20,-20,1020,20),lambda *p:True)
        selected=[str(i) for i in range(1000) if furniture._h01(str(i),'fr-j6-section-v2')<.08]
        self.assertGreater(len(selected),40);self.assertLess(len(selected),120)
        self.assertTrue(any(p[0]=='fr_j6' for p in put(selected[0])))
        rejected=next(str(i) for i in range(1000) if str(i) not in selected)
        self.assertEqual(put(rejected),[])
        for cls in ('tertiary','unclassified','residential'):
            self.assertEqual(put(selected[0],sp._replace(cls=cls)),[])
    def test_sidewalk_palette_preserves_geometry_and_collision(self):
        curb=np.array([[0.,0,0],[60.,0,1.]])
        out=np.array([[0.,-1.],[0.,-1.]])
        def build(europe):
            b=sidewalks.Builder((0,0,0),collision=True)
            b.add_run(curb,out,2.,ramps=[(20,23)],europe=europe)
            return b
        old,new=build(False),build(True)
        a,b=old.arrays(),new.arrays()
        np.testing.assert_array_equal(a[0],b[0]);np.testing.assert_array_equal(a[1],b[1])
        self.assertEqual(set(b[3]),{4,5,6})
        for aa,bb in zip(old.col.arrays(),new.col.arrays()):np.testing.assert_array_equal(aa,bb)

    def test_exported_sidewalk_uses_packaged_srgb_materials(self):
        """Exercise the tile writer, material bindings and actual shipped texture pixels."""
        walk={'pts':np.array([[20.,20,0],[100.,20,0]]),'side':1,'w':2.,'caps':(False,False),'ramps':[(30.,34.)]}
        with tempfile.TemporaryDirectory() as temp:
            for europe in (False,True):
                roads.write_tile(temp,'/temp/test',1,0,0,[],walks=[dict(walk,europe=europe)])
                tile=json.loads(Path(temp,'roads_1_0_0.json').read_text())
                shape=cdae.read(Path(temp,Path(tile['walk']['shape']).name))
                names={m['name'] for m in shape['materials']}
                expected={'bne_eu_curb','bne_eu_asphalt_walk','bne_eu_tactile'} if europe else {'curbs','sidewalk1','tactile_indicator'}
                self.assertEqual(names,expected)
        materials=json.loads((surfaces.OUT/'main.materials.json').read_text())
        levels=ROOT/'mod/beamng_earth'
        means={}
        for name,mat in materials.items():
            self.assertEqual(mat['mapTo'],name)
            texture=mat['Stages'][0]['baseColorMap']
            self.assertTrue(texture.endswith('.color.png'))
            with Image.open(levels/texture.lstrip('/')) as im:
                pixels=np.asarray(im.convert('RGB'),float)
            means[name]=pixels.mean()
            self.assertGreater(pixels.std(),1.,'surface grain should survive')
        self.assertLess(means['bne_eu_asphalt_walk'],85.,'asphalt should be darker than the neutral road albedo')
        self.assertLess(means['bne_eu_curb'],means['bne_eu_asphalt_walk'])
        self.assertGreater(means['bne_eu_tactile'],means['bne_eu_asphalt_walk']+70.)

    def test_surface_generator_reproduces_decoded_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            surfaces.build(temp)
            for src in surfaces.OUT.glob('*.color.png'):
                with Image.open(src) as a,Image.open(Path(temp,src.name)) as b:
                    np.testing.assert_array_equal(np.asarray(a),np.asarray(b))
            self.assertEqual(json.loads((surfaces.OUT/'main.materials.json').read_text()),
                             json.loads(Path(temp,'main.materials.json').read_text()))


class StopsTest(unittest.TestCase):
    def arms(self):
        sp=roads.Spec(7.,'paved',True,'residential',False)
        return [(str(i),sp,np.array(u,float),np.array([100.,100.,0.]),True,80.)
                for i,u in enumerate([(1,0),(-1,0),(0,-1)])]
    def test_public_stops_are_identical_in_all_furniture_profiles(self):
        arms=self.arms();signals=np.zeros((0,2));box=(0,0,256,256)
        rr=[(fid,sp,np.array([p,p+np.r_[u*length,0.]]),0.) for fid,sp,u,p,at,length in arms]
        expected=furniture.junction_props(rr,box,signals=signals)
        self.assertEqual(len(expected),1);self.assertEqual(expected[0][0],furniture.G_STOP)
        for lat,lon in [(49.026,7.816),(49.06593,7.79547),(40.71,-74.)]:
            for profile in (None,pp.FR_KIT,pp.geographic_profile('europe'),pp.geographic_profile('fr-varied')):
                rows=furniture.place(rr,[],box,signals=signals,pole_profile=profile,pole_frame=geodesy.EnuFrame(lat,lon))
                self.assertEqual([r for r in rows if r[0] in (furniture.G_STOP,furniture.G_YIELD,furniture.G_SIGNAL)],expected)
        # Public 1.7.1 does not classify tertiary as a minor road as well as a major.
        tertiary=[(fid,sp._replace(cls='tertiary'),u,p,at,length) for fid,sp,u,p,at,length in arms]
        self.assertEqual(furniture.junction_controls(tertiary,None,signals=signals),[])
    def test_mapped_lights_and_roundabout_priority_remain(self):
        arms=self.arms()
        signals=np.array([[100.,100.]])
        self.assertEqual(len(furniture.junction_controls(arms,None,signals=signals)),3)
        self.assertEqual(furniture.junction_controls(arms,None,ring={'0','1'}),[(arms[2],furniture.G_YIELD)])
    def test_ai_stop_controller_matches_public_visual_stop(self):
        arms=self.arms()
        navarms={'j':[(fid,sp,u,p,0,length) for fid,sp,u,p,at,length in arms]}
        nodes={'j':[100.,100.,0.,3],**{fid:[100.+u[0]*80,100.+u[1]*80,0.,1] for fid,sp,u,p,at,length in arms}}
        approaches={('j',fid,0):(fid,True) for fid,*_ in arms}
        original=navgraph._signals(navarms,approaches,nodes,signals=np.zeros((0,2)))
        controls=furniture.junction_controls(arms,None,signals=np.zeros((0,2)))
        self.assertEqual(len(original),1)
        self.assertEqual(original[0]['kind'],'stop')
        self.assertEqual([a[0] for a in original[0]['app']],[a[0] for a,g in controls if g==furniture.G_STOP])


if __name__=='__main__':unittest.main()
