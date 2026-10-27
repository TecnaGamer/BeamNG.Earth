"""State selection, concave silhouette coverage and text containment on guide panels."""
from pathlib import Path
import sys,unittest
from unittest.mock import patch
import numpy as np
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import exitsigns,routeshields
from test_advance_sign_layout import sign


def area(points):
    return .5*np.sum(points[:,0]*np.roll(points[:,1],-1)-points[:,1]*np.roll(points[:,0],-1))


def inside(points,ring):
    # Ray crossing; the text vertices are deliberately inset from the silhouette.
    points=np.asarray(points);hit=np.zeros(len(points),bool)
    for a,b in zip(ring,np.roll(ring,-1,axis=0)):
        if abs(b[1]-a[1])<1e-12:continue
        x=a[0]+(points[:,1]-a[1])*(b[0]-a[0])/(b[1]-a[1])
        hit^=((a[1]>points[:,1])!=(b[1]>points[:,1])) & (points[:,0]<x)
    return hit


class StateMarkerTest(unittest.TestCase):
    def test_concave_profiles_have_complete_nonoverlapping_front_faces(self):
        for state in routeshields.SUPPORTED_STATES:
            p=routeshields.marker(state)
            self.assertFalse(p.contour.flags.writeable)
            self.assertIs(p,routeshields.marker(state))
            self.assertEqual(len(p.triangles),len(p.contour)-2)
            areas=np.array([area(p.contour[t]) for t in p.triangles])
            self.assertTrue(np.all(areas>1e-12),state)
            self.assertAlmostEqual(areas.sum(),area(p.contour),places=10,msg=state)
            self.assertTrue(inside(p.contour[p.triangles].mean(1),p.contour).all(),state)
        self.assertIsNone(routeshields.marker('XX'))

    def test_route_selection_uses_destination_state_or_local_state_for_generic_sr(self):
        for state in routeshields.SUPPORTED_STATES:
            explicit=exitsigns.route_symbols([state+' 99'],'MI')[0]
            local=exitsigns.route_symbols(['SR 99'],state)[0]
            self.assertEqual(explicit,local)
            self.assertEqual(local['state'],state)
            s=sign(routes=(state+' 99',))
            m=exitsigns.Mesh((0.,0.,0.))
            with patch.object(routeshields,'marker',wraps=routeshields.marker) as build:
                m.panel(s)
            build.assert_called_once_with(state)

    def test_digits_legends_and_suffixes_fit_inside_each_white_cutout(self):
        for state in routeshields.SUPPORTED_STATES:
            profile=routeshields.marker(state)
            for number in ('5','99','114','295','123A'):
                m=exitsigns.Mesh((0.,0.,0.));m.centre=np.zeros(3)
                m.face=np.array([0.,-1.,0.]);m.right=np.array([1.,0.,0.])
                route=dict(kind='state',state=state,number=number,y=0.,bottom=0.,width=1.,height=1.,qualifier='')
                m.route_shield(route)
                p,t,uv,materials,mi=m.result()
                text=t[mi==materials.index('bne_guide_letters_black')]
                self.assertTrue(inside(p[np.unique(text)][:,[0,2]],profile.contour).all(),(state,number))
                white=t[mi==1]
                self.assertAlmostEqual(sum(area(p[tri][:,[0,2]]) for tri in white),area(profile.contour),places=10)
                self.assertTrue(np.allclose(p[np.unique(white),1],-.067))

    def test_shape_legend_and_digit_bands_do_not_overlap(self):
        for state in routeshields.SUPPORTED_STATES:
            p=routeshields.marker(state)
            if not p.legend:continue
            _,z,h,_=p.number;_,_,lz,lh,_=p.legend
            self.assertTrue(z+h<lz or lz+lh<z,state)

    def test_michigan_digits_fit_the_diamond_even_with_wide_or_long_route_numbers(self):
        contour=np.array([[0.,0.],[.5,.5],[0.,1.],[-.5,.5]])
        for number in ('5','71','99','114','123A'):
            m=exitsigns.Mesh((0.,0.,0.));m.centre=np.zeros(3)
            m.face=np.array([0.,-1.,0.]);m.right=np.array([1.,0.,0.])
            m.route_shield(dict(kind='state',state='MI',number=number,y=0.,bottom=0.,width=1.,height=1.,qualifier=''))
            p,t,uv,materials,mi=m.result()
            text=t[mi==materials.index('bne_guide_letters_black')]
            self.assertTrue(inside(p[np.unique(text)][:,[0,2]],contour).all(),number)

    def test_qualifier_above_shield_and_other_networks_remain_separate(self):
        s=sign(routes=('OH 99 NORTH','I 75','US 23'))
        m=exitsigns.Mesh((0.,0.,0.))
        with patch.object(m,'guide_text',wraps=m.guide_text) as text:m.panel(s)
        north=[c for c in text.call_args_list if c.args[0]=='NORTH']
        self.assertEqual(len(north),1)
        route=exitsigns.advance_layout(s)['routes'][0]
        self.assertGreater(north[0].args[2],s['bottom']+route['bottom']+route['height'])
        self.assertEqual([r['kind'] for r in exitsigns.route_symbols(['I 75','US 23','OH 99'])],['interstate','us','state'])


if __name__=='__main__':unittest.main()
