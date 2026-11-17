"""Offline unit tests for the v0.7 NVDB destination sign renderer."""
from pathlib import Path
import json
import os
import sys
import tempfile
from unittest import TestCase, main, mock

# Installed from repo root under server/tests; also runnable beside the installer.
HERE = Path(__file__).resolve().parent
if HERE.name == 'tests':
    sys.path.insert(0,str(HERE.parent))
else:
    sys.path.insert(0,str(HERE))
import norway_nvdb_destinations_v07 as d
import numpy as np


def sample(code='713.V - Vanlig vegviser, Venstre', text='283 Haugesund @723.11 E134',
           facing='Parallelt mot veg', color='Gul', pid=777):
    return {
        'id':pid,
        'metadata':{'type':{'id':96}},
        'geometri':{'wkt':'POINT Z (62.89390647 7.6787657 2.504)', 'srid':4326,'egengeometri':True},
        'lokasjon':{'geometri':{'wkt':'POINT Z (62.89387043 7.67871026 2.504)','srid':4326}},
        'egenskaper':[
            {'id':5530,'verdi':code},
            {'id':1910,'verdi':text},
            {'id':1294,'verdi':color},
            {'id':1894,'verdi':facing},
            {'id':1328,'verdi':2500},
            {'id':1588,'verdi':640},
            {'id':1912,'verdi':140},
        ]
    }


class Frame:
    def geodetic_to_enu(self, la, lo, z):
        la=np.asarray(la,float)
        lo=np.asarray(lo,float)
        return ((lo - 7.67)*52000, (la - 62.89)*111320, np.asarray(z,float))

    def enu_to_geodetic(self, east, north, z):
        east=np.asarray(east,float)
        north=np.asarray(north,float)
        return north/111320 + 62.89, east/52000 + 7.67, np.asarray(z,float)


class DestinationTests(TestCase):
    def test_713_left_and_exact_text(self):
        r=d.normalize(sample())
        self.assertEqual(r['code'],'713.V')
        self.assertEqual(r['text'],'283 Haugesund @723.11 E134')
        self.assertEqual(r['color'],'gul')
        self.assertEqual(r['width_mm'],2500)

    def test_nvdb_at_delimiters_and_shields(self):
        p=d.parse_text('@723.15 60@Førde@@Måløy@@703.13 15@723.15 60@@Stranda')
        self.assertEqual(p[0],[['route','723.15','60'],['text','Førde']])
        self.assertEqual(p[1],[['text','Måløy']])
        self.assertNotIn('703.13',str(p))
        self.assertEqual(len(p),4)

    def test_no_fabricated_place_names(self):
        self.assertEqual(d.parse_text(''),[])
        self.assertIsNone(d.normalize(sample(text='')))
        self.assertIsNone(d.normalize(sample(text='@@@')))

    def test_no_speed_signs(self):
        self.assertIsNone(d.normalize(sample(code='362.60 - Fartsgrense 60 km/t')))

    def test_206_208_priority(self):
        for code in ('206','208'):
            row=d.normalize(sample(code=code+' - Priority',text='',color='Hvit'))
            self.assertEqual(row['kind'],code)

    def test_no_diagram_guess(self):
        self.assertIsNone(d.normalize(sample(code='703.1 - Diagramorienteringstavle')))
        self.assertIsNone(d.normalize(sample(code='705 - Avkjøringstavle')))

    def test_skip_no_color(self):
        self.assertIsNone(d.normalize(sample(code='711 - Tabellvegviser',color='Unknown')))

    def test_727_name(self):
        row=d.normalize(sample(code='727.1 - Vanlig stedsnavnskilt',text='Batnfjordsøra',color='Hvit'))
        self.assertEqual(d.parse_text(row['text']),[[['text','Batnfjordsøra']]])

    def test_723_shield(self):
        row=d.normalize(sample(code='723.11 - Europaveg',text='E39',color='Grønn'))
        self.assertEqual(row['color'],'grønn')
        nat=d.normalize(sample(code='723.13 - Riksveg',text='5',color='Grønn'))
        self.assertEqual(nat['color'],'grønn')
        self.assertEqual(d._rgb_color(None,'723.13'),'grønn')
        self.assertEqual(d._rgb_color(None,'723.15'),'hvit')

    def test_orientation(self):
        frame=Frame()
        a=d.normalize(sample(facing='Parallelt fra veg'))
        b=d.normalize(sample(facing='Parallelt mot veg'))
        fa=d.face_of(a,frame)[2]
        fb=d.face_of(b,frame)[2]
        self.assertAlmostEqual(float(np.dot(fa,fb)),-1.,places=6)
        c=d.normalize(sample(facing='Trafikk i metreringsretning'))
        fc=d.face_of(c,frame)[2]
        self.assertAlmostEqual(float(np.dot(fc,fa)),0,places=6)

    def test_wrong_geometry_skipped(self):
        obj=sample()
        obj['geometri']['egengeometri']=False
        self.assertIsNone(d.normalize(obj))

    def test_parent_fixture_no_speed_sign_included(self):
        prev=Path('/mnt/data/nvdb96_62.8900_7.6700_600m.v03.details.json')
        if not prev.exists():
            self.skipTest('Optional uploaded live fixture not mounted here')
        rows=json.loads(prev.read_text('utf8'))['plates']
        self.assertEqual(len(rows),8)
        for row in rows:
            self.assertEqual(row['sign_number'],'202')

    def test_download_cursor(self):
        obj=sample()
        urls=[]
        def fetch(u):
            urls.append(u)
            return {'objekter':[obj],'metadata':{'neste':{'start':'abc123'}}} if len(urls)==1 else {'objekter':[],'metadata':{}}
        result=d.download_cell((5240,320),fetch=fetch)
        self.assertEqual(len(result),1)
        self.assertIn('start=abc123',urls[1])
        self.assertTrue(all(u.startswith(d.base.BASE+'?') for u in urls))

    def test_download_overflow_refused(self):
        with mock.patch.object(d.base,'MAX_PAGES',1):
            with self.assertRaises(ValueError):
                d.download_cell((5240,320),fetch=lambda u:{'objekter':[sample()],'metadata':{'neste':{'start':'next'}}})

    def test_draw_text_sign(self):
        from PIL import Image
        a=d.normalize(sample())
        img=d._draw_face(a)
        self.assertIsInstance(img,Image.Image)
        self.assertTrue(img.size[0]<=1024)

    def test_arrow_low_poly(self):
        a=d.normalize(sample())
        p,t,uv,mi=d._arrow_mesh(a,[10,20],7,[1,0],1,[0,0],2.5,.6)
        self.assertEqual(len(p),len(uv))
        self.assertEqual(len(t),len(mi))
        self.assertLess(len(t),35)
        self.assertTrue((p[:,2]>0).any())

    def test_actual_arrow_tile_mesh(self):
        from types import SimpleNamespace
        row=d.normalize(sample())
        def merge(a,b):
            return b if a is None else a
        fake={'exitsigns':SimpleNamespace(_on_deck=lambda *a,**k:False),
              'fast':SimpleNamespace(),
              'busstationsign':SimpleNamespace(merge=merge)}
        with mock.patch.object(d,'tile_records',return_value=[row]), \
             mock.patch.object(d,'_face_filename',return_value=('bne_sample',Path('/x/bne_sample.dds'),(512,256))), \
             mock.patch.dict(sys.modules,fake):
            mesh, faces=d.build_tile(Frame(),(300.,300.,650.,650.),
                                    lambda la,lo:np.zeros_like(la)+12.,[], '/x','/v')
        self.assertIsNotNone(mesh)
        self.assertEqual(len(faces),1)
        self.assertGreater(len(mesh[1]),0)
        self.assertEqual(len(mesh[1]),len(mesh[4]))

    def test_real_priority_road_sign_mesh(self):
        from types import SimpleNamespace
        row=d.normalize(sample(code='206 - Forkjørsveg', text='',color='Hvit'))
        def backend(*args):
            return np.zeros((4,3)),np.array([[0,1,2]],dtype=np.int64),np.zeros((4,2)),np.array([0])
        roles = SimpleNamespace(tables=lambda :({'cols':8,'rows':16},
                                  {'sizes':{'default':{'diamond':[.6,.75,.9]}}},
                                  {'prio.main_road':(0,{'shape':'diamond'})}),
                                SHAPES={'diamond':3},METAL='bne_exit_metal')
        fake={'exitsigns':SimpleNamespace(_on_deck=lambda *a,**k:False),
              'fast':SimpleNamespace(_ef=SimpleNamespace(road_sign_mesh=backend)),
              'roadsigns':roles}
        with mock.patch.object(d,'tile_records',return_value=[row]), mock.patch.dict(sys.modules,fake):
            mesh=d.build_priority_tile(Frame(),(300.,300.,650.,650.),
                lambda la,lo:np.zeros_like(la)+12.,[], {'atlas':'vienna_w'})
        self.assertIsNotNone(mesh)
        self.assertEqual(mesh[3][0],'bne_signs_vienna_w')
        self.assertEqual(len(mesh[1]),1)

    def test_no_road_edits(self):
        self.assertNotIn('speed', ' '.join(d.DESTS))
        self.assertTrue(d.enabled())


if __name__=='__main__':
    main()
