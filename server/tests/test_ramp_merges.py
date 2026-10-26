"""Connected ramp tapers, physical dash dimensions and source-stable clipping."""
import unittest
from unittest.mock import patch
from pathlib import Path
import sys
import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import roads
import roadmarkings
import rampmerges
from test_merge_signs import fixture


BOX = (-500., -100., 500., 100.)


def rows(entry=False, reverse=False, side=-1, state='MI', count=1):
    main = roads.Spec(roads.carriageway('motorway', 'paved', False, oneway=True, lanes=2),
                      'paved', False, 'motorway', True, dir=1, lanes=2, state=state)
    ramp = main._replace(width=roads.carriageway('motorway', 'paved', False, oneway=True,
                                               lanes=count, link=True), link=True, lanes=count,
                         dir=-1 if entry else 1)
    p = np.array([[0., 0., 10.], [-200. if entry else 200., side*40., 10.]])
    if reverse:
        p, ramp = p[::-1], ramp._replace(dir=-ramp.dir)
    return [('main', main, np.array([[-500., 0., 10.], [500., 0., 10.]]), -85.),
            ('ramp', ramp, p, -85.)]


class MergeTest(unittest.TestCase):
    def test_longer_merge_stays_on_asphalt_outside_through_lanes(self):
        for entry in (False, True):
            for side in (-1, 1):
                for count in (1, 2):
                    source = rows(entry, side=side, count=count)
                    with patch.object(rampmerges, 'EXTEND', 0.):
                        old = rampmerges.plans(source)[0]
                    p = rampmerges.plans(source)[0]
                    self.assertAlmostEqual((p['end']-p['start']) - (old['end']-old['start']), 30.)
                    np.testing.assert_allclose(p['guide'][-1], old['guide'][-1])
                    np.testing.assert_allclose(p['outer'][-1], old['outer'][-1])
                    outer = roads.densify(p['outer'], .25)
                    _, _, _, lateral, dm = rampmerges.project(p['path'], outer)
                    dr = rampmerges.project(p['source'], outer)[4]
                    paint = roadmarkings.profile(p['spec'].state).solid/2
                    self.assertTrue(np.all((dm <= source[0][1].width/2-paint+.01)
                                           | (dr <= source[1][1].width/2-paint+.01)))
                    self.assertTrue(np.all(lateral*p['side'] >= p['edge']-.01))

    def test_separator_paint_does_not_overlap_taper_edge(self):
        for entry in (False, True):
            for side in (-1, 1):
                for count in (1, 2, 3):
                    p = rampmerges.plans(rows(entry, side=side, count=count))[0]
                    marks = roadmarkings.paint(rampmerges.chunks([p], BOX))
                    solid = roadmarkings.profile(p['spec'].state).solid
                    for ch in marks:
                        if ch['p'] == 6:
                            line = roads.densify(np.asarray(ch['n']), .1)
                            distance = rampmerges.project(p['outer'], line)[4].min()
                            self.assertGreaterEqual(distance, (solid+ch['w'])/2+.08)

    def test_extension_crosses_connected_source_segment_boundary(self):
        source = rows()
        fid, sp, line, lon = source[0]
        source[0] = fid, sp, np.array([[-2., 0., 10.], [500., 0., 10.]]), lon
        source.append(('before', sp, np.array([[-500., 0., 10.], [-2., 0., 10.]]), lon))
        p = rampmerges.plans(source)[0]
        whole = rampmerges.plans(rows())[0]
        np.testing.assert_allclose(p['guide'], whole['guide'])
        np.testing.assert_allclose(p['outer'], whole['outer'])
        self.assertIn('before', p['mains'])

    def test_adjacent_merge_prevents_extension_overlap(self):
        source = rows()
        fid, sp, line, lon = source[1]
        source.append(('previous_ramp', sp, line + [-55., 0., 0.], lon))
        plans = rampmerges.plans(source)
        self.assertEqual(len(plans), 2)
        earlier, later = sorted(plans, key=lambda p: p['guide'][0, 0])
        self.assertGreaterEqual(later['guide'][0, 0] - earlier['guide'][-1, 0], .999)

    def test_tight_adjacent_merges_do_not_extend_into_each_other(self):
        from unittest.mock import patch
        source = rows()
        _, sp, line, lon = source[1]
        source.append(('previous_ramp', sp, line + [-25., 0., 0.], lon))
        with patch.object(rampmerges, 'EXTEND', 0.):
            baseline = sorted(rampmerges.plans(source), key=lambda p: p['guide'][0, 0])
        plans = sorted(rampmerges.plans(source), key=lambda p: p['guide'][0, 0])
        gap = plans[1]['guide'][0, 0] - plans[0]['guide'][-1, 0]
        original_gap = baseline[1]['guide'][0, 0] - baseline[0]['guide'][-1, 0]
        self.assertGreaterEqual(gap, original_gap - 1e-6)
        self.assertGreater(gap, roadmarkings.profile(sp.state).solid)

    def test_curved_merge_extension_and_road_edges_stay_on_pavement(self):
        source = rows()
        fid, sp, _, lon = source[0]
        source[0] = (fid, sp, np.array([[-500.,-30.,10.],[-150.,-5.,10.],[0.,0.,10.],
                                        [80.,0.,10.],[200.,15.,10.],[500.,80.,10.]]), lon)
        fid, sp, _, lon = source[1]
        source[1] = (fid, sp, np.array([[0.,0.,10.],[35.,-6.,10.],[75.,-15.,10.],
                                        [120.,-25.,10.],[200.,-45.,10.]]), lon)
        p = rampmerges.plans(source)[0]
        self.assertLess(p['guide'][0, 0], -15.)
        points = roads.densify(p['outer'], .25)
        dm = rampmerges.project(p['path'], points)[4]
        dr = rampmerges.project(p['source'], points)[4]
        self.assertTrue(np.all((dm <= source[0][1].width/2-.2) | (dr <= source[1][1].width/2-.2)))
        self.assertTrue(np.all(rampmerges.project(p['path'], points)[3]*p['side'] >= p['edge']-.01))

    def test_orange_ramp_edge_is_white_until_pavements_separate(self):
        for entry in (False, True):
            for reverse in (False, True):
                for side in (-1, 1):
                    source = rows(entry, reverse, side=side)
                    plans = rampmerges.plans(source)
                    plan = plans[0]
                    self.assertGreater(plan['color_cut'], plan['cut'])
                    fid, sp, line, lon = source[1]
                    original = roads.extras(sp, line, lon)
                    painted = rampmerges.trim(fid, original, plans)
                    left = [(lk, p) for lk, _, p in painted if lk.get('edge_side') == sp.dir]
                    self.assertEqual({lk['m'] for lk, _ in left}, {'line_white', 'line_yellow'})
                    for lk, p in left:
                        s = rampmerges.project(plan['source'], p)[0]
                        if lk['m'] == 'line_white':
                            self.assertLessEqual(s.max(), plan['color_cut'] + 1e-6)
                            self.assertAlmostEqual(s.max(), plan['color_cut'])
                        else:
                            self.assertGreaterEqual(s.min(), plan['color_cut'] - 1e-6)
                            self.assertAlmostEqual(s.min(), plan['color_cut'])
                    # Colour replacement must not mutate the shared source style.
                    self.assertEqual(next(lk['m'] for lk, _, _ in original if lk.get('edge_side') == sp.dir), 'line_yellow')
                    inner = roads.offset_polyline(plan['source'], -plan['side']*sp.width/2)
                    at = rampmerges.project(plan['source'], inner)[0]
                    point = np.array([[np.interp(plan['color_cut'], at, inner[:, d]) for d in range(3)]])
                    distance = rampmerges.project(plan['path'], point)[4][0]
                    self.assertAlmostEqual(distance, source[0][1].width/2, places=5)
                    self.assertTrue(all(c['m'] == 'line_white' for c in rampmerges.chunks(plans, BOX)))

    def test_colour_transition_is_stable_across_clipping_and_fixed_decks(self):
        from types import SimpleNamespace
        source = rows()
        plan = rampmerges.plans(source)[0]
        split_x = rampmerges.sample(plan['source'], plan['color_cut'])[0, 0]
        boxes = [(-500., -100., split_x, 100.), (split_x, -100., 500., 100.)]
        for box in boxes:
            tunnel = SimpleNamespace(parts=lambda cells, area: [(fid, sp, p[:, :2], p[:, 2], lon)
                                                                for fid, sp, p, lon in source])
            for chunks in (roads._deck_chunks(source, box), roads._tunnel_chunks(tunnel, [], box)):
                # Negative y distinguishes the ramp's inner edge from the main median edge.
                orange = [c for c in chunks if c['m'] == 'line_yellow' and c['n'][0][1] < 0.]
                for c in orange:
                    self.assertGreaterEqual(rampmerges.project(plan['source'], np.array(c['n']))[0].min(),
                                            plan['color_cut']-.002)
                self.assertTrue(all(c['fixed'] for c in chunks))

    def test_entries_exits_sides_and_reversed_osm_order(self):
        for entry in (False, True):
            for side in (-1, 1):
                a = rampmerges.plans(rows(entry, side=side))
                b = rampmerges.plans(rows(entry, True, side=side))
                self.assertEqual(len(a), 1)
                self.assertEqual(len(b), 1)
                p = a[0]
                self.assertEqual(p['entry'], entry)
                np.testing.assert_allclose(p['outer'], b[0]['outer'])
                self.assertEqual(p['color'], 'line_yellow' if side > 0 else 'line_white')
                self.assertAlmostEqual(p['span'][0], 0.)
                self.assertTrue(np.all(np.diff(p['span']) > 0))
                self.assertAlmostEqual(p['span'][-1], 4.8)

    def test_separators_use_notes_dimensions(self):
        for state in ('', 'MI', 'WI', 'CA', 'MN', 'KY'):
            plans = rampmerges.plans(rows(state=state))
            marks = roadmarkings.paint(rampmerges.chunks(plans, BOX, fixed=True))
            dashed = [c for c in marks if c['p'] == 6]
            profile = roadmarkings.profile(state)
            if profile.ramp is None:
                self.assertFalse(dashed)
                continue
            length, gap, width = profile.ramp
            self.assertGreaterEqual(len(dashed), 2)
            self.assertAlmostEqual(roadmarkings.arc(np.array(dashed[0]['n']))[-1], length, places=3)
            self.assertAlmostEqual(np.linalg.norm(np.array(dashed[1]['n'][0]) - dashed[0]['n'][-1]), gap, places=3)
            self.assertTrue(all(c['w'] == width and c['fixed'] for c in dashed))
            self.assertTrue(all(all(n[2] == 10. for n in c['n']) for c in marks))

    def test_taper_replaces_edges_but_preserves_through_lane_dividers(self):
        source = rows()
        plans = rampmerges.plans(source)
        for fid, sp, line, lon in source:
            ex = roads.extras(sp, line, lon)
            clipped = rampmerges.trim(fid, ex, plans)
            if fid == 'main':
                before = [p for lk, _, p in ex if 'dash' in lk]
                after = [p for lk, _, p in clipped if 'dash' in lk]
                np.testing.assert_allclose(before, after)
                self.assertEqual(len(clipped), len(ex)+1)
            else:
                for _, _, p in clipped:
                    self.assertGreaterEqual(rampmerges.project(plans[0]['source'], p)[0].min(), plans[0]['cut']-1e-6)

    def test_tile_clip_keeps_dash_phase(self):
        plans = rampmerges.plans(rows())
        whole = roadmarkings.paint(rampmerges.chunks(plans, BOX))
        split = roadmarkings.paint(rampmerges.chunks(plans, (-500., -100., 21., 100.)) +
                                   rampmerges.chunks(plans, (21., -100., 500., 100.)))
        total = lambda chunks: sum(roadmarkings.arc(np.array(c['n']))[-1] for c in chunks if c['p'] == 6)
        self.assertAlmostEqual(total(whole), total(split), places=3)
        starts = [c['n'][0][0] for c in whole if c['p'] == 6]
        for c in split:
            if c['p'] == 6 and abs(c['n'][0][0]-21.) > .001:
                self.assertTrue(any(abs(c['n'][0][0]-s) < .001 for s in starts))

    def test_unconnected_opposing_and_elevated_roads_do_not_merge(self):
        for change in ('offset', 'opposing', 'height'):
            source = rows()
            fid, sp, line, lon = source[0]
            if change == 'offset':
                line[:, 1] += 4.
            if change == 'height':
                line[:, 2] += 5.
            if change == 'opposing':
                sp = sp._replace(dir=-1)
            source[0] = fid, sp, line, lon
            self.assertFalse(rampmerges.plans(source))

    def test_ground_context_bridge_and_multiple_lanes(self):
        from types import SimpleNamespace
        for entry in (False, True):
            frame, feats = fixture(entry)
            a = rampmerges.ground(frame, [feats])
            self.assertEqual(len(a), 1)
            b = rampmerges.ground(frame, [feats, feats])
            np.testing.assert_allclose(a[0]['guide'], b[0]['guide'])
            source = rows(entry, count=2)
            tunnel = SimpleNamespace(parts=lambda cells, box: [(fid, sp, p[:, :2], p[:, 2], lon)
                                                               for fid, sp, p, lon in source])
            for chunks in (roads._deck_chunks(source, BOX), roads._tunnel_chunks(tunnel, [], BOX)):
                merge = [c for c in chunks if 'ramp_merge' in c]
                self.assertTrue(merge)
                self.assertTrue(all(c['fixed'] for c in merge))

    def test_taper_meets_surviving_ramp_edges_on_both_sides(self):
        for entry in (False, True):
            for side in (-1, 1):
                source = rows(entry, side=side)
                plans = rampmerges.plans(source)
                fid, sp, line, lon = source[1]
                pieces = rampmerges.trim(fid, roads.extras(sp, line, lon), plans)
                ends = np.array([p[0] for _, _, p in pieces])
                for key in ('guide', 'outer'):
                    self.assertLess(np.linalg.norm(ends-plans[0][key][-1], axis=1).min(), 1e-6)
                self.assertTrue(rampmerges.related('main', 'ramp', plans))
                self.assertTrue(rampmerges.related('ramp', 'main', plans))

    def test_ground_tile_emits_merge_paint(self):
        from test_roads import flat
        frame, feats = fixture(False)
        p = rampmerges.ground(frame, [feats])[0]
        mid = p['guide'][len(p['guide'])//2]
        tx, ty = int(mid[0]//roads.TILE), int(mid[1]//roads.TILE)
        chunks = roads.build_tile(frame, tx, ty, [feats], flat, terrain_paint=False)
        merge = [c for c in chunks if c.get('ramp_merge') == 'exit']
        self.assertTrue(any(c['p'] == 6 for c in merge))
        self.assertTrue(any(c['p'] == 2 for c in merge))


if __name__ == '__main__':
    unittest.main()
