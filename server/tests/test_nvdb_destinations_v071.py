"""Offline regression tests for Norwegian NVDB sign visual hotfix 0.7.1."""
import os,sys,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np

HERE=Path(__file__).resolve().parent
if HERE.name=='tests':sys.path.insert(0,str(HERE.parent))
else:sys.path.insert(0,str(HERE))
import norway_nvdb_destinations_v07 as d


def rec(code='713.H',text='Blakstad 31',xy=(7.6787657,62.89390647),pcode=10):
    return dict(id=100+pcode,code=code,kind=code.split('.')[0],text=text,color='gul',
                point=list(xy),road=[7.67871026,62.89387043],facing='parallelt fra veg',
                width_mm=None,height_mm=None,position_code=pcode)


class Frame:
    def geodetic_to_enu(self,la,lo,z):
        la=np.asarray(la,float);lo=np.asarray(lo,float)
        return (lo-7.67)*52000,(la-62.89)*111320,np.asarray(z,float)
    def enu_to_geodetic(self,e,n,z):
        e=np.asarray(e,float);n=np.asarray(n,float)
        return n/111320+62.89,e/52000+7.67,np.asarray(z,float)


class HotfixTests(unittest.TestCase):
    def test_route_shield_delimiter_not_printed(self):
        result=d.parse_text('@Gol@723.14¤7¤')
        self.assertEqual(result,[[['text','Gol'],['route','723.14','7']]])
        self.assertNotIn('¤',str(result))

    def test_arrow_code_32_is_not_a_number(self):
        self.assertEqual(d.parse_text('32@@723.11¤E39¤Molde'),
                         [[['arrow','32']],[['route','723.11','E39'],['text','Molde']]])

    def test_malformed_markup_not_visible(self):
        self.assertNotIn('¤',str(d.parse_text('Molde@¤bad¤')))

    def test_711_variant_number(self):
        self.assertEqual(d._number('711.H90 - Tabellvegviser'),'711.H90')
        self.assertEqual(d._number('713.V - Vanlig vegviser'),'713.V')

    def test_speed_remains_out(self):
        self.assertIsNone(d._kind('362.60'))

    def test_701_complex_not_rendered_until_diagram_supported(self):
        self.assertEqual(d._kind('701.1'),'701')

    def test_no_giant_723_e39_board(self):
        w,h=d._tile_dimensions(rec('723.11','E39'))
        self.assertLessEqual(w,.9)
        self.assertLessEqual(h,.65)
        r=rec('723.11','E39');r['width_mm']=2400;r['height_mm']=1200
        w,h=d._tile_dimensions(r)
        self.assertLessEqual(w,.9);self.assertLessEqual(h,.65)

    def test_713_shape_uv_and_one_pole(self):
        row=rec(); p,t,uv,mi=d._panel_mesh(row,[0.,0.],5.,[1.,0.],1,[0.,0.],1.8,.48,6.8)
        self.assertEqual(len(t),len(mi));self.assertEqual(len(p),len(uv))
        self.assertTrue((mi==0).any());self.assertTrue((mi==1).any())
        self.assertLess(len(t),35)
        bare=d._panel_mesh(row,[0.,0.],5.,[1.,0.],1,[0.,0.],1.8,.48,7.4,posts=False)
        self.assertLess(len(bare[0]),len(p))

    def test_right_arrow_butt_near_pole(self):
        row=rec('713.H')
        p,_,_,_=d._panel_mesh(row,[0,0],0,[1,0],1,[0,0],2,.5,1.8,posts=False)
        # front horizontal extents are -0.1 to +1.9 metres from support, not +/- 1m.
        self.assertAlmostEqual(float(p[:5,1].max()),1.9,places=4)
        self.assertAlmostEqual(float(p[:5,1].min()),-.1,places=4)

    def test_priority_and_route_shield_can_share_pole(self):
        priority=rec('206',''); priority['kind']='206'
        shield=rec('723.11','E39');shield['id']=999
        fake={'exitsigns': SimpleNamespace(_on_deck=lambda *a,**k:False),
              'busstationsign': SimpleNamespace(merge=lambda a,b:b if a is None else a)}
        with patch.object(d,'tile_records',return_value=[priority,shield]), \
             patch.object(d,'_face_filename',return_value=('mock',Path('/tmp/mock.dds'),(512,256))), \
             patch.dict(sys.modules,fake):
            mesh,faces=d.build_tile(Frame(),(0,0,1000,1000),lambda a,b:np.zeros_like(a),(),'/tmp','/v')
        self.assertIsNotNone(mesh)
        self.assertIn('mock',faces)
        # Shield is lifted above the diamond rather than removed.
        self.assertGreater(float(mesh[0][:,2].max()),2.55)

    def test_shared_pole_stacks_panels(self):
        r1=rec('713.H','Blakstad 31',pcode=10)
        r2=rec('713.V','Kvitnes 2',pcode=20);r2['id']=999
        fake={'exitsigns':SimpleNamespace(_on_deck=lambda *a,**k:False),
              'busstationsign':SimpleNamespace(merge=lambda a,b: b if a is None else (
                  np.vstack((a[0],b[0])),np.vstack((a[1],b[1]+len(a[0]))),
                  np.vstack((a[2],b[2])),a[3],np.r_[a[4],b[4]]))}
        with patch.object(d,'tile_records',return_value=[r1,r2]),\
             patch.object(d,'_face_filename',side_effect=lambda row,od: ('mock'+str(row['id']),Path('/x/mock'+str(row['id'])+'.dds'),(512,256))),\
             patch.dict(sys.modules,fake):
            mesh,faces=d.build_tile(Frame(),(0,0,1000,1000),lambda a,b:np.zeros_like(a),(),'/x','/v')
        self.assertIsNotNone(mesh)
        self.assertEqual(len(faces),2)
        # Only one shared post => two plates at different vertical heights.
        zvalues=mesh[0][:,2]
        self.assertGreater(float(zvalues.max()-zvalues.min()),.7)

    def test_arrow_border_matches_shape(self):
        row=rec('713.H','Blakstad 31')
        im=d._draw_face(row)
        # Tip coordinate on the right is yellow and not white/alpha.
        px=im.getpixel((im.width-60,im.height//2))
        self.assertGreater(px[0],190)
        self.assertLess(px[2],100)
        # The top-left border is black.
        px=im.getpixel((10,10))
        self.assertLess(px[0],100)

    def test_725_decoder_no_fake_distance(self):
        row=rec('725.1','Molde 32')
        lines=d._render_lines(row)
        self.assertEqual(lines,[[['text','Molde 32']]])

if __name__=='__main__': unittest.main()
