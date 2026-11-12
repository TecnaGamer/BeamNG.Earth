"""Measured board/name bounds, national variants and backwards-compatible assets."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))  # (PR #505's data: the tests' slice)
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import numpy as np
from PIL import Image,ImageFont

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'tools')]
import europeinfra as ei
import poleprofiles
import signmesh as sm
import townlayout as tl
import revisit
from preview_town_signs import EXAMPLES,render,ART


class LayoutTest(unittest.TestCase):
    def test_every_existing_country_has_a_profile_and_example(self):
        self.assertEqual(set(tl.PROFILES),set(ei.COUNTRIES))
        self.assertEqual(set(EXAMPLES),set(ei.COUNTRIES))

    def test_national_shapes_are_not_one_shared_aspect_ratio(self):
        fr=sm.town_layout('CHEYLADE',ei.town_style('FR'))
        de=sm.town_layout('Clauen',ei.town_style('DE'))
        at=sm.town_layout('Pöllau',ei.town_style('AT'))
        pl=sm.town_layout('Obiecanowo',ei.town_style('PL'))
        self.assertAlmostEqual(fr.height,.43)
        self.assertAlmostEqual(de.height,.60)
        self.assertAlmostEqual(de.width,.90)
        self.assertGreaterEqual(at.width,.96);self.assertGreaterEqual(at.height,.63)
        self.assertAlmostEqual(pl.height,.53)
        self.assertGreater(fr.width/fr.height,de.width/de.height)

    def test_actual_glyph_vertices_fit_borders_all_countries_and_name_lengths(self):
        for country in sorted(ei.COUNTRIES):
            for name in (EXAMPLES[country],'Y','Bad Homburg vor der Höhe',
                         'Saint-Remy-en-Bouzemont-Saint-Genest-et-Isson',
                         'A'*150):
                with self.subTest(country=country,name=name):
                    style=ei.town_style(country);plan=sm.town_layout(name,style)
                    mesh=sm.Mesh();mesh.town((0,0,0),(0,1),name,kind='begin',style=style)
                    p=np.asarray(mesh.p);t=np.asarray(mesh.t);tm=np.asarray(mesh.tm)
                    ink=p[t[np.isin(tm,[sm.GLYPH,sm.GLYPH_WHITE])].ravel()]
                    inset=plan.profile.outer+plan.profile.border
                    self.assertTrue(np.isfinite(p).all());self.assertLess(len(p),4000)
                    self.assertLessEqual(abs(ink[:,0]).max(),plan.width/2-inset)
                    self.assertGreaterEqual(ink[:,2].min(),plan.bottom+inset)
                    self.assertLessEqual(ink[:,2].max(),plan.top-inset)
                    rows=[line.text for line in plan.lines if line.role=='name']
                    self.assertEqual(''.join(rows).replace(' ',''),sm.clean(name,style.uppercase).replace(' ',''))
                    if country!='DE':
                        self.assertGreaterEqual(min(line.cap for line in plan.lines),plan.profile.cap*.78-1e-9)

    def test_german_fixed_plate_and_separate_open_tube_frame(self):
        self.assertEqual([l.text for l in sm.town_layout('Hattingen',ei.town_style('DE'),('Ennepe-Ruhr-Kreis',)).lines],
                         ['Hattingen','Ennepe-Ruhr-Kreis'])
        for name in ('Ulm','Hattingen','Giebelstadt','Bad Homburg vor der Höhe','A'*150):
            for details in ((),('Gemeinde Hohenhameln','Landkreis Peine')):
                plan=sm.town_layout(name,ei.town_style('DE'),details)
                self.assertEqual((plan.width,plan.height),(.9,.6))
                m=sm.Mesh();m.german_frame(.9,.6,plan.centre)
                p=np.asarray(m.p);t=np.asarray(m.t)
                normals=np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]])
                self.assertTrue((np.linalg.norm(normals,axis=1)>1e-10).all())
                self.assertTrue((normals[:,1]<0).any() and (normals[:,1]>0).any())
                # Tubular silhouette extends outside the plate, leaving real air.
                self.assertGreater(p[:,0].max(),.5)
                self.assertFalse(((abs(p[:,0])<.30)&(abs(p[:,2]-plan.centre)<.2)).any())

    def test_admin_lines_are_smaller_and_supplied_only(self):
        style=ei.town_style('DE')
        plain=sm.town_layout('Clauen',style)
        plan=sm.town_layout('Clauen',style,('Gemeinde Hohenhameln','Landkreis Peine'))
        self.assertEqual(len(plain.lines),1)
        self.assertEqual([l.text for l in plan.lines[1:]],['Gemeinde Hohenhameln','Landkreis Peine'])
        self.assertTrue(all(l.cap<plan.lines[0].cap*.6 for l in plan.lines[1:]))
        mesh=sm.Mesh();mesh.town((0,0,0),(0,1),'Clauen',style=style,details=('Gemeinde Hohenhameln','Landkreis Peine'))
        glyph=np.asarray(mesh.p)[np.asarray(mesh.t)[np.asarray(mesh.tm)==sm.GLYPH].ravel()]
        self.assertGreater(glyph[:,2].min(),plan.bottom+.05)
        self.assertLess(glyph[:,2].max(),plan.top-.05)

    def test_locality_and_built_up_signs_are_kept_distinct(self):
        self.assertEqual(ei.town_style('PL').meaning,'place-name')
        self.assertFalse(ei.town_style('PL').skyline)
        self.assertEqual(ei.town_style('PL').exit,'slash')
        self.assertTrue(ei.town_style('BE').skyline)
        for code in ('ES','HU'):self.assertEqual(ei.town_style(code).border,'black')
        self.assertEqual(ei.town_style('TR',place_kind='village').background,'white')
        self.assertEqual(ei.town_style('TR',place_kind='town').background,'blue')
        self.assertEqual(ei.town_style('CH','primary').background,'blue')
        self.assertEqual(ei.town_style('CH','residential').background,'white')
        self.assertIsNone(ei.town_style('RO','secondary',50).header_speed)

    def test_native_accents_and_scripts_do_not_turn_into_question_marks(self):
        for code,name in EXAMPLES.items():
            plan=sm.town_layout(name,ei.town_style(code))
            for line in plan.lines:
                for ch in line.text:
                    self.assertTrue(ch in sm.face_font(line.font)['glyphs'] or ch in sm.face_font('fallback')['glyphs'],(code,ch))
        for country in ('DE','AT','NL','BE','RO'):
            self.assertEqual(sm.town_layout('Teststadt',ei.town_style(country)).lines[0].text,'Teststadt')

    def test_rounded_board_is_watertight_with_both_faces_and_valid_uvs(self):
        mesh=sm.Mesh();mesh.town((0,0,0),(0,1),'Gœrsdorf','D263')
        p=np.asarray(mesh.p);t=np.asarray(mesh.t);tm=np.asarray(mesh.tm);uv=np.asarray(mesh.uv)
        normals=np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]])
        glyph=normals[tm==sm.GLYPH]
        self.assertTrue((glyph[:,1]>0).any() and (glyph[:,1]<0).any())
        self.assertTrue(((uv>=0)&(uv<=1)).all());self.assertTrue((t>=0).all() and (t<len(p)).all())
        self.assertTrue((np.linalg.norm(normals,axis=1)>1e-10).all())
        with tempfile.TemporaryDirectory()as tmp:
            import cdae
            path=Path(tmp)/'signs.cdae';mesh.write(path);result=cdae.read(path)
            self.assertIsNotNone(result)

    def test_skyline_meshes_have_no_backwards_or_degenerate_triangles(self):
        for code in ('BE','DK','EE','FO','IS'):
            mesh=sm.Mesh();plan=sm.town_layout(EXAMPLES[code],ei.town_style(code))
            mesh._skyline(code,plan.width,plan.height,plan.centre,-.034)
            p=np.asarray(mesh.p);t=np.asarray(mesh.t)
            n=np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]])
            self.assertTrue((n[:,1]<-1e-10).all(),code)
            self.assertLess(p[:,2].max(),min(l.centre-l.cap/2 for l in plan.lines))


class AssetsTest(unittest.TestCase):
    def test_french_faces_use_unmodified_typoroute_l1_with_native_spacing(self):
        path=ROOT/'tools/fonts/town/Typoroute2002-L1_100.ttf'
        source=ImageFont.truetype(str(path),96)
        cap=source.getbbox('H',anchor='ls')[3]-source.getbbox('H',anchor='ls')[1]
        face=sm.face_font('l1')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),'225b8b8eed0f0a6b59ad1ba836f2e9394ca1297386aec79709f5c0fa47057291')
        self.assertEqual(face['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(sm.town_layout('MARCENAT').profile.font,'l1')
        self.assertEqual(face['horizontal_scale'],1.)
        for text in ('MARCENAT','CHEYLADE','AV','AA','SAINT-REMY', '-RÉMY', 'SAINT-',
                     'SAINT-ÉTIENNE','GŒRSDORF','D 679',"L’HÔPITAL"):
            with self.subTest(text=text):
                self.assertAlmostEqual(sm.measure(text,'l1')[3]*cap,source.getlength(text),delta=.08)
        self.assertNotEqual(sm.font_text('SAINT-REMY','l1'),'SAINT-REMY')
        self.assertEqual(sm.font_text('-REMY','l1'),'-REMY')
        self.assertEqual(sm.font_text("L'HÔPITAL",'l1'),"L’HÔPITAL")
        self.assertEqual(sm.glyph_for('😀','l1'),sm.face_font('fallback')['glyphs']['?'])

    def test_french_mesh_samples_l1_ink_and_keeps_accents_and_long_names(self):
        for name in ('MARCENAT','CHEYLADE','ÉTÉ À GŒRSDORF',"L’HÔPITAL",'SAINT-REMY-EN-BOUZEMONT-SAINT-GENEST-ET-ISSON'):
            plan=sm.town_layout(name);mesh=sm.Mesh();mesh.town((0,0,0),(0,1),name,kind='begin')
            actual=[tuple(mesh.uv[i])for tri,mat in zip(mesh.t,mesh.tm)if mat==sm.GLYPH for i in tri]
            allowed=set()
            for line in plan.lines:
                for ch in sm.font_text(line.text,'l1'):
                    self.assertIn(ch,sm.face_font('l1')['glyphs'])
                    u0,v0,u1,v1=sm.glyph_for(ch,'l1')['uv']
                    allowed.update(((u0,v0),(u1,v0),(u1,v1),(u0,v1)))
            self.assertTrue(set(actual)<=allowed)
            self.assertNotEqual(sm.measure(name,'l1')[0],sm.measure(name,'condensed')[0])

    def test_shared_atlas_alpha_uv_and_all_font_sources(self):
        data=sm.font();w,h=data['atlas_size']
        self.assertTrue(w<=4096 and h<=4096 and w&(w-1)==0 and h&(h-1)==0)
        with Image.open(ART/'sign_font.png')as black,Image.open(ART/'sign_font_white.png')as white,Image.open(ART/'sign_font_opacity.data.png')as opacity:
            self.assertEqual(black.size,(w,h));np.testing.assert_array_equal(black.getchannel('A'),opacity)
            np.testing.assert_array_equal(white.getchannel('A'),opacity)
        for key,face in data['fonts'].items():
            for ch,g in face['glyphs'].items():
                l,b,r,t=g['bounds'];u0,v0,u1,v1=g['uv']
                if r>l and t>b:self.assertTrue(0<=u0<u1<=1 and 0<=v0<v1<=1,(key,ch))

    def test_direction_labels_keep_legacy_geometry_metrics(self):
        for name in ('Wingen','Schönau','Gœrsdorf','A 35'):
            expected=sum(sm.font()['glyphs'][ch]['advance']+.08 for ch in name)*.78
            self.assertEqual(sm.advance(name),expected)
        mesh=sm.Mesh();mesh.direction((0,0,0),(0,1),['Schönau'])
        self.assertIn(sm.GLYPH,mesh.tm)

    def test_preview_rasterizes_mesh_glyphs_and_national_colour(self):
        mesh=sm.Mesh();mesh.town((0,0,0),(0,1),'Ulm',kind='begin',style=ei.town_style('DE'))
        pixels=np.asarray(render(mesh,(360,240)))
        self.assertTrue(((pixels[:,:,0]>200)&(pixels[:,:,1]>140)&(pixels[:,:,2]<110)).any())
        self.assertGreater(((pixels<40).all(axis=2)).sum(),300)

    def test_rendering_code_and_data_are_in_cache_and_app_inputs(self):
        self.assertIn('townlayout.py',revisit.source_closure(str(ROOT/'server')))
        self.assertIn(tl.VERSION,poleprofiles.geographic_profile('europe')[2])
        self.assertIn(hashlib.sha256(sm.FONT_PATH.read_bytes()).hexdigest(),poleprofiles.geographic_profile('europe')[2])
        import build_app
        with tempfile.TemporaryDirectory()as tmp,mock.patch.object(build_app.subprocess,'run')as run:
            build_app.pyinstaller(tmp,'test.ico',False)
        args=run.call_args.args[0]
        self.assertIn('townlayout',args)
        self.assertIn(str(ROOT/'server/data')+build_app.SEP+'data',args)


if __name__=='__main__':unittest.main()
