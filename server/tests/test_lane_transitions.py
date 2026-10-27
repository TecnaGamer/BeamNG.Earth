"""Mainline additions and drops preserve right-to-left lane order and deck heights."""
import sys
from pathlib import Path
import unittest
import numpy as np
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import roads
import lanetransitions as lt
import roadmarkings
from test_roads import features, rules, FRAME, flat


def row(fid, count, a, b, direction=1, link=False):
    sp = roads.Spec(roads.carriageway('motorway', 'paved', False, oneway=True, lanes=count, link=link),
                    'paved', False, 'motorway', True, dir=direction, lanes=count, link=link, state='MI')
    line = np.array([[a, 0., 10.], [b, 0., 10.]])
    return fid, sp, line if direction == 1 else line[::-1], -85.


class LaneTransitionsTest(unittest.TestCase):
    def test_additions_and_drops_match_both_ends_and_keep_existing_lanes(self):
        for small, large in ((2, 3), (3, 4), (2, 4)):
            for reverse in (1, -1):
                for drop in (False, True):
                    rows = [row('n', small, -700, 0, reverse), row('w', large, 0, 700, reverse)]
                    if drop:
                        rows = [(fid, sp._replace(dir=-sp.dir), p, lon) for fid, sp, p, lon in rows]
                    layouts = lt.plans(rows)
                    self.assertEqual(set(layouts), {'w'})
                    fid, sp, p, lon = rows[1]
                    ex = lt.extras(fid, sp, p, lon, False, layouts)
                    narrow = roads.extras(rows[0][1], rows[0][2], lon)
                    for side in (sp.dir, -sp.dir):
                        line = next(q for look, _, q in ex if look.get('edge_side') == side)
                        other = next(q for look, _, q in narrow if look.get('edge_side') == side)
                        tip = line[np.argmin(abs(line[:, 0]))]
                        np.testing.assert_allclose(tip, other[np.argmin(abs(other[:, 0]))], atol=1e-6)
                    marks = [q for lk, _, q in ex if 'dash' in lk]
                    self.assertEqual(len(marks), large-1)
                    # New dividers start inside the widening lane, away from the edge.
                    at_tip = [q for q in marks if min(abs(q[:, 0])) < 1e-5]
                    self.assertEqual(len(at_tip), small-1)
                    edge = next(q for lk, _, q in ex if lk.get('edge_side') == sp.dir)
                    for q in marks:
                        order = np.argsort(edge[:, 0])
                        y = np.interp(q[:, 0], edge[order, 0], edge[order, 1])
                        self.assertGreaterEqual(np.min(abs(y-q[:, 1])), .499)
                    self.assertTrue(all(np.all(q[:, 2] == 10.) for _, _, q in ex))

    def test_chain_crosses_short_segments_and_avoids_two_overlapping_tapers(self):
        rows = [row('a', 2, -400, 0), row('b', 3, 0, 40), row('c', 3, 40, 400), row('d', 2, 400, 800)]
        layouts = lt.plans(rows)
        self.assertEqual(len(layouts['b']), 2)
        self.assertEqual({p['reach'] for p in layouts['b']}, {200.})
        endpoints = []
        for r in rows[1:3]:
            ex = lt.extras(*r, False, layouts)
            endpoints.append([(lk.get('edge_side'), q[np.argmin(abs(q[:, 0]-40))]) for lk, _, q in ex if 'edge_side' in lk])
        for a, b in zip(*endpoints):
            np.testing.assert_allclose(a[1], b[1])

    def test_ramps_opposing_traffic_crossings_and_unknown_counts_are_not_transitions(self):
        base = [row('a', 2, -400, 0), row('b', 3, 0, 400)]
        ramp = row('r', 1, 0, 150, link=True)
        ramp[2][-1, 1] = -40
        self.assertEqual(lt.plans(base+[ramp]), {})
        b = base[1]
        self.assertEqual(lt.plans([base[0], (b[0], b[1]._replace(dir=-1), b[2], b[3])]), {})
        self.assertEqual(lt.plans([base[0], (b[0], b[1]._replace(lanes=None), b[2], b[3])]), {})
        high = b[2].copy(); high[:, 2] += 6
        self.assertEqual(lt.plans([base[0], (b[0], b[1], high, b[3])]), {})

    def test_curved_lane_change_stays_on_deck_and_preserves_height(self):
        rows = [row('n', 2, -400, 0), row('w', 3, 0, 500)]
        fid, sp, _, lon = rows[1]
        p = np.array([[0.,0.,10.],[100.,0.,11.],[240.,15.,12.],[500.,60.,13.]])
        rows[1] = fid, sp, p, lon
        ex = lt.extras(fid, sp, p, lon, False, lt.plans(rows))
        for lk, w, q in ex:
            distance = __import__('rampmerges').project(p, q)[4]
            self.assertLess(np.max(distance)+w/2, sp.width/2)
            self.assertGreaterEqual(np.min(q[:,2]),10.)
        chunks = roads._deck_chunks(rows, (-500,-100,600,100))
        self.assertTrue(all(c.get('fixed') for c in chunks))
        self.assertTrue(any(c['m']=='line_yellow' for c in chunks))

    def test_source_layout_continues_from_ground_across_bridge(self):
        f = features([[(-400,0),(0,0)],[(0,0),(40,0)],[(40,0),(400,0)]], ['motorway']*3,
                     flags=[(2,'is_bridge')])
        f.rules['oneway'] = rules([(i,1.) for i in range(3)], 'float')
        f.attrs['lane_count'] = np.array([2.,3.,3.])
        plans = lt.source(FRAME, [f])
        self.assertEqual(set(plans), {'id001','id002'})
        self.assertAlmostEqual(plans['id002'][0]['reach'], lt.TAPER_M)
        for tx in (-1,0,1):
            chunks = roads.build_tile(FRAME, tx, 0, [f], flat)
            self.assertTrue(all(np.isfinite(c['n']).all() for c in chunks))

    def test_tile_clipping_does_not_restart_taper_or_dash_pattern(self):
        rows = [row('n',2,-400,0),row('w',3,0,700)]
        fid,sp,p,lon = rows[1]
        extras = lt.extras(fid,sp,p,lon,False,lt.plans(rows))
        def strokes(box):
            chunks = [roads._chunk(lk,w,q) for lk,w,line in extras if 'dash' in lk
                      for q in roads.clip_polyline(line,*box)]
            return roadmarkings.paint(chunks)
        whole = strokes((0,-20,400,20))
        tiled = strokes((0,-20,127,20))+strokes((127,-20,400,20))
        # Compare the actually painted points, including a cut through a dash.
        for x in np.arange(.125,399.9,.25):
            def at(chunks):
                ys=[]
                for c in chunks:
                    q=np.array(c['n']);order=np.argsort(q[:,0]);q=q[order]
                    if q[0,0]<=x<=q[-1,0]:ys.append(np.interp(x,q[:,0],q[:,1]))
                return sorted(ys)
            np.testing.assert_allclose(at(whole),at(tiled),atol=.002)

    def test_long_source_way_keeps_projection_bounded(self):
        rows=[row('n',2,-400,0),row('w',3,0,20000)]
        plan=lt.plans(rows)['w'][0]
        self.assertAlmostEqual(roadmarkings.arc(plan['path'])[-1],lt.TAPER_M)


if __name__ == '__main__': unittest.main()
