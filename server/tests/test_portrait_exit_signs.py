"""Reference-style guide panels and the separate exit-entrance arrow station."""
import copy
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import exitsigns as signs
from test_exit_signs import FRAME, fixture


def panel(state='IN', kind='advance', distance='1/4 MILE', label=None):
    label = label or dict(ref='259', routes=['US 35 NORTH', 'IN 22'], names=['Upland', 'Gas City'])
    w, h = signs.dimensions(label, kind, state)
    return dict(label=label,state=state,kind=kind,distance=distance,width=w,height=h,
                bottom=2.4,centre=[0.,0.,0.],travel=[0.,1.],side=1)


class PortraitExitSignsTest(unittest.TestCase):
    def test_every_other_state_uses_portrait_michigan_keeps_wide_panel(self):
        for state in signs.STATE_CODES:
            s = panel(state)
            self.assertEqual(s['height']>s['width'], state!='MI', state)
            layout = signs.advance_layout(s)
            self.assertFalse(layout['arrow'])
            if state != 'MI':
                self.assertEqual([r['number'] for r in layout['routes']], ['35','22'])
                self.assertGreater(layout['routes'][0]['bottom'], layout['routes'][1]['bottom'])
                self.assertGreater(layout['routes'][1]['bottom'], layout['lines'][0]['baseline']+.58)
                self.assertEqual(layout['directions'][0]['value'], 'North')
        label = panel()['label']
        self.assertEqual(signs.dimensions(label,'advance','MI'), signs.dimensions(label,'advance'))

    def test_destinations_routes_and_direction_fit_with_no_overlaps(self):
        for routes in ([], ['US 35 NORTH'], ['I 196 WEST','US 127 NORTH','IN 22']):
            for names in ([], ['Upland','Gas City'], ['A very long destination name','Second','Third']):
                label = dict(ref='259', routes=routes, names=names)
                s = panel(label=label)
                l = signs.advance_layout(s)
                for r in l['routes']:
                    self.assertLessEqual(abs(r['y'])+r['width']/2,s['width']/2-.15)
                    self.assertLess(r['bottom']+r['height'],s['height']-.15)
                    self.assertGreater(r['bottom'],1.)
                for r in l['lines']+l['directions']:
                    self.assertGreater(r['baseline'],1.)
                    self.assertLess(r['baseline']+r['height'],s['height']-.15)
                    self.assertLessEqual(abs(r['y'])+r['max_width']/2,s['width']/2-.15)
                mesh = signs.Mesh((0,0,0));mesh.panel(s)
                p,t,uv,m,mi = mesh.result()
                self.assertTrue(np.isfinite(p).all())
                self.assertLess(t.max(),len(p))

    def test_only_decision_and_gore_have_arrows_on_both_exit_sides(self):
        for state in ('MI','IN','KY','CA'):
            for side in (-1,1):
                for kind,distance in (('advance','1/4 MILE'),('decision',''),('ramp','')):
                    s = panel(state,kind,distance);s['side']=side
                    mesh = signs.Mesh((0,0,0))
                    with patch.object(mesh,'arrow',wraps=mesh.arrow) as arrow:
                        mesh.panel(s)
                    self.assertEqual(arrow.call_count, int(kind!='advance'))
                    if kind!='advance':self.assertEqual(arrow.call_args.args[2],side<0)

    def test_decision_sign_is_before_exit_and_has_one_tile_owner(self):
        rows,obj = fixture()
        flat = lambda la,lo: np.zeros_like(la)
        with patch('signstates.states',return_value=np.array('IN')):
            whole = signs.place(FRAME,[obj],rows,(-2000,-300,300,300),flat,link_ids={'ramp'})
            tiles = [s for a,b in ((-2000,-100),(-100,0),(0,100),(100,300))
                     for s in signs.place(FRAME,[obj,obj],rows,(a,-300,b,300),flat,link_ids={'ramp'})]
        decision, = [s for s in whole if s['kind']=='decision']
        self.assertAlmostEqual(decision['centre'][0],-signs.DECISION_M,places=1)
        self.assertEqual(decision['distance'],'')
        self.assertEqual(decision['label'],obj['exit']['label'])
        self.assertEqual(len(decision['bases']),3)
        self.assertEqual(sorted((s['kind'],s['centre']) for s in whole),
                         sorted((s['kind'],s['centre']) for s in tiles))
        short=copy.deepcopy(obj)
        short['exit']['approach']=obj['exit']['approach'][-1:]*2
        self.assertFalse(any(s['kind']=='decision' for s in signs.place(
            FRAME,[short],rows,(-2000,-300,300,300),flat,link_ids={'ramp'})))


if __name__ == '__main__':unittest.main()
