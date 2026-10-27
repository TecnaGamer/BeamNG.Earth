"""Advance-panel arrows, destination layout, network shields and dedicated lettering."""
from pathlib import Path
import json,sys,unittest
from unittest.mock import patch
import numpy as np
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import exitsigns


def sign(distance='1 MILE',routes=('M 71',),names=('Durand','Corunna'),side=1):
    label=dict(ref='118',routes=list(routes),names=list(names))
    w,h=exitsigns.dimensions(label,'advance')
    return dict(label=label,kind='advance',distance=distance,advance_m=1609.344,
                width=w,height=h,bottom=2.4,centre=[0.,0.,0.],travel=[0.,1.],side=side,state='MI')


class AdvanceLayoutTest(unittest.TestCase):
    def test_advance_panels_have_distances_without_arrows(self):
        for distance in ('1 MILE','1/2 MILE','1/4 MILE','500 m'):
            for side in (-1,1):
                mesh=exitsigns.Mesh((0.,0.,0.))
                with patch.object(mesh,'arrow',wraps=mesh.arrow) as arrow:mesh.panel(sign(distance,side=side))
                self.assertEqual(arrow.call_count,0)
                if arrow.call_count:self.assertEqual(arrow.call_args.args[2],side<0)

    def test_rest_area_advance_arrows_obey_the_same_rule_and_gore_arrows_remain(self):
        for distance in ('1 MILE','1/2 MILE','1/4 MILE'):
            s=sign(distance,routes=(),names=('Rest Area',));s['label']['ref']=''
            s['width'],s['height']=exitsigns.dimensions(s['label'],'advance')
            mesh=exitsigns.Mesh((0.,0.,0.))
            with patch.object(mesh,'arrow',wraps=mesh.arrow) as arrow:mesh.panel(s)
            self.assertEqual(arrow.call_count,0)
        s=sign();s['kind']='ramp';s['width'],s['height']=exitsigns.dimensions(s['label'],'ramp')
        mesh=exitsigns.Mesh((0.,0.,0.))
        with patch.object(mesh,'arrow',wraps=mesh.arrow) as arrow:mesh.panel(s)
        self.assertEqual(arrow.call_count,1)

    def test_route_networks_use_explicit_prefixes_and_deduplicate(self):
        cases={'I-69':'interstate','Interstate 196':'interstate','US 23':'us','U.S. 127':'us',
               'US ROUTE 12':'us','M-71':'state','MI 71':'state','SR 37':'state','IN 49':'state',
               'KY 55 NORTH':'state','A 7':'text','E 45':'text'}
        for value,kind in cases.items():
            self.assertEqual(exitsigns.route_symbols([value],'MI')[0]['kind'],kind,value)
        symbols=exitsigns.route_symbols(['I-69 / US 23','I 69','M 71'],'MI')
        self.assertEqual([s['kind'] for s in symbols],['interstate','us','state'])
        self.assertEqual([s['number'] for s in symbols],['69','23','71'])
        self.assertEqual(exitsigns.route_symbols(['M 71'],'MI')[0]['state'],'MI')
        self.assertEqual(exitsigns.route_symbols(['M 25'])[0]['kind'],'text','foreign M routes are not Michigan highways')
        self.assertEqual(exitsigns.route_symbols(['71'],'MI')[0]['kind'],'state')
        self.assertEqual(exitsigns.route_symbols(['71'])[0]['kind'],'text')
        self.assertEqual(exitsigns.advance_layout(sign(routes=()))['routes'],[],
                         'an exit number is not a destination highway number')

    def test_destinations_and_left_shields_stay_within_content_region(self):
        for names in (('Durand',),('Durand','Corunna'),('A very long destination name','Second destination','Third')):
            for routes in ((),('M 71',),('I 69 NORTH','US 23','MI 71')):
                for side in (-1,1):
                    for distance in ('1 MILE','1/4 MILE'):
                        s=sign(distance,routes,names,side);layout=exitsigns.advance_layout(s)
                        lines=layout['lines'];symbols=layout['routes']
                        for line in lines:
                            self.assertGreater(line['max_width'],0.)
                            self.assertGreaterEqual(line['baseline'],.65)
                            self.assertLessEqual(line['baseline']+line['height'],s['height']-.14)
                        for route in symbols:
                            self.assertGreaterEqual(route['bottom'],.65)
                            self.assertLessEqual(route['bottom']+route['height']+.2,s['height']-.10)
                            self.assertLess(route['y']+route['width']/2,
                                lines[0]['y']-lines[0]['max_width']/2+.01)
                        if layout['arrow'] and side<0 and symbols:
                            self.assertLess(layout['arrow_y']+.32,symbols[0]['y']-.54)
        s=sign(routes=(),names=('Durand',));line=exitsigns.advance_layout(s)['lines'][0]
        self.assertAlmostEqual(line['y'],0.)
        self.assertGreater(line['baseline'],.9)
        self.assertLess(line['baseline']+line['height'],s['height']-.5)

    def test_route_only_panels_are_centered_instead_of_leaving_an_empty_text_column(self):
        s=sign(names=(),routes=('US 23',))
        layout=exitsigns.advance_layout(s)
        self.assertEqual(layout['lines'],[])
        self.assertAlmostEqual(layout['routes'][0]['y'],0.)

    def test_shield_faces_and_letter_materials_are_valid(self):
        for route in ('I 196','US 127','M 71','IN 49','CA 18'):
            s=sign(routes=(route,));mesh=exitsigns.Mesh((0.,0.,0.));mesh.panel(s)
            p,t,uv,materials,mi=mesh.result()
            self.assertTrue(np.isfinite(p).all());self.assertEqual(uv.shape,(len(p),2))
            self.assertLess(t.max(),len(p));self.assertLess(mi.max(),len(materials))
            face=np.array([0.,-1.,0.])
            for name in ('bne_guide_red','bne_guide_black','bne_guide_letters','bne_guide_letters_black'):
                ids=t[mi==materials.index(name)]
                if not len(ids):continue
                normals=np.cross(p[ids[:,1]]-p[ids[:,0]],p[ids[:,2]]-p[ids[:,0]])
                self.assertTrue(np.all(normals@face>0.),(route,name))
            self.assertTrue(np.any(mi==materials.index('bne_guide_letters')))
            if not route.startswith('I '):self.assertTrue(np.any(mi==materials.index('bne_guide_letters_black')))

    def test_three_digit_federal_shields_are_wider_without_encroaching_on_destinations(self):
        for prefix in ('I','US'):
            two=exitsigns.advance_layout(sign(routes=(prefix+' 69',)))['routes'][0]
            three=exitsigns.advance_layout(sign(routes=(prefix+' 196',)))['routes'][0]
            self.assertGreater(three['width'],two['width'])
            self.assertEqual(three['height'],two['height'])
            line=exitsigns.advance_layout(sign(routes=(prefix+' 196',)))['lines'][0]
            self.assertLess(three['y']+three['width']/2,line['y']-line['max_width']/2)

    def test_legacy_atlas_material_indices_and_non_ascii_names_remain_supported(self):
        self.assertEqual(len(exitsigns.MATERIALS),5,'mile-marker material indices retain the original base list')
        self.assertEqual(exitsigns.guide_lettering()['font'],'Roadgeek 2014 Series EEM')
        for text in ('Durand','Corunna','Montréal','Łódź','Αθήνα'):
            self.assertGreater(exitsigns.guide_width(text,.64),0.)
            mesh=exitsigns.Mesh((0.,0.,0.));mesh.panel(sign(names=(text,)))
            self.assertIsNotNone(mesh.result())
        self.assertEqual(exitsigns.guide_glyph('é')[0],exitsigns.lettering())

    def test_dedicated_atlas_and_license_are_packaged_by_the_build_command(self):
        from test_sign_packaging import frozen_data
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bundle=frozen_data(tmp)
            with patch.object(exitsigns,'__file__',str(bundle/'exitsigns.pyc')):
                exitsigns.guide_lettering.cache_clear()
                try:self.assertIn('D',exitsigns.guide_lettering()['glyphs'])
                finally:exitsigns.guide_lettering.cache_clear()
        root=Path(__file__).resolve().parents[2]
        folder=root/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside'
        materials=json.loads((folder/'exit.materials.json').read_text())
        for name in ('bne_guide_letters','bne_guide_letters_black'):
            self.assertTrue(materials[name]['alphaTest'])
            self.assertTrue((folder/Path(materials[name]['Stages'][0]['opacityMap']).name).is_file())
        self.assertIn('Sammi De Guzman',(folder/'Roadgeek-LICENSE.txt').read_text())


if __name__=='__main__':unittest.main()
