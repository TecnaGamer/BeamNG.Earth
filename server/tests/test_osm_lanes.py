"""Real PBF decoding, directional lane tags, joined tapers and render/physics agreement."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapgeom
import navgraph
import osmlanes
import roadlanes
import roads
import surface
import grading
import netfetch
from test_roads import FRAME, LAT, LON, M_LON, M_LAT, features, flat, rules


def fixture(folder, binary=False):
    nodes = [(1, -500, 0), (2, 0, 0), (3, 500, 0), (4, 1000, 0), (5, 0, -300)]
    ways = [(101, (1, 2), 2, 'motorway'), (102, (2, 3), 3, 'motorway'),
            (103, (3, 4), 4, 'motorway'), (104, (2, 5), 1, 'motorway_link')]
    xml = ['<osm version="0.6" generator="BeamNG.Earth tests">']
    for i, x, y in nodes:
        xml.append(f'<node id="{i}" version="1" lat="{LAT+y/M_LAT:.9f}" lon="{LON+x/M_LON:.9f}"/>')
    for i, refs, n, cls in ways:
        xml.append(f'<way id="{i}" version="1">')
        xml.extend(f'<nd ref="{r}"/>' for r in refs)
        xml.append(f'<tag k="highway" v="{cls}"/><tag k="oneway" v="yes"/><tag k="lanes" v="{n}"/></way>')
    xml.append('</osm>')
    path = os.path.join(folder, 'roads.osm')
    Path(path).write_text(''.join(xml), encoding='utf-8')
    if binary:
        import osmium
        out = os.path.join(folder, 'roads.osm.pbf')
        with osmium.SimpleWriter(out) as writer:
            for obj in osmium.FileProcessor(path):
                writer.add(obj)
        path = out
    db = os.path.join(folder, 'roads.sqlite')
    osmlanes.import_pbf(path, db)
    return db


def enriched(db, reverse=False):
    lines = [[(-500, 0), (0, 0)], [(0, 0), (500, 0)], [(500, 0), (1000, 0)]]
    if reverse:
        lines = [p[::-1] for p in lines]
    f = features(lines, ['motorway']*3)
    f.rules['oneway'] = rules([(i, -1. if reverse else 1.) for i in range(3)], 'float')
    f.attrs['osm_sources'] = mapgeom.text_column([json.dumps([
        dict(dataset='OpenStreetMap', property='', record_id=f'w{101+i}@1')]) for i in range(3)])
    source = osmlanes.LaneSource(os.path.dirname(db), Mock())
    source.cached = Mock(return_value=db)
    assert source.enrich(f, LAT, LON)
    return f


class Tags(unittest.TestCase):
    def test_oneway_and_directional_counts(self):
        for tags, expected in [({'lanes':'3', 'oneway':'yes'}, (3,0,0)),
                               ({'lanes':'4', 'oneway':'-1'}, (0,4,0)),
                               ({'lanes':'4'}, (2,2,0)),
                               ({'lanes':'3','lanes:forward':'2'}, (2,1,0)),
                               ({'lanes:forward':'2','lanes:backward':'1'}, (2,1,0)),
                               ({'lanes':'5','lanes:both_ways':'1'}, (2,2,1)),
                               ({'highway':'motorway','lanes':'2'}, (2,0,0))]:
            with self.subTest(tags=tags):
                self.assertEqual(osmlanes.lane_counts(tags), expected)

    def test_ambiguous_invalid_and_conditional_counts_keep_fallback(self):
        for tags in ({'lanes':'3'}, {'lanes':'2;3'}, {'lanes':'0'}, {'lanes':'100'},
                     {'lanes':'4','lanes:reversible':'1'}, {'lanes':'2','oneway':'reversible'},
                     {'lanes:conditional':'3 @ (Mo-Fr)'}, {'lanes':'4','lanes:forward':'3','lanes:backward':'3'},
                     {'lanes':'2','oneway':'yes','lanes:forward':'3'}):
            self.assertIsNone(osmlanes.lane_counts(tags), tags)

    def test_region_smallest_polygon_and_holes(self):
        def region(name, ring):
            return dict(properties=dict(id=name, urls=dict(pbf=f'https://download.geofabrik.de/{name}.osm.pbf')),
                        geometry=dict(type='Polygon', coordinates=[ring]))
        big = region('country', [[0,0],[10,0],[10,10],[0,10],[0,0]])
        small = region('state', [[1,1],[3,1],[3,3],[1,3],[1,1]])
        index = dict(features=[big,small])
        self.assertEqual(osmlanes.region_at(index, 2,2)[0], 'state')
        small['geometry']['coordinates'].append([[1.5,1.5],[2.5,1.5],[2.5,2.5],[1.5,2.5],[1.5,1.5]])
        self.assertEqual(osmlanes.region_at(index, 2,2)[0], 'country')


class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = fixture(self.temp.name, binary=True)
        self.f = enriched(self.db)
        self.specs = roads.road_specs(self.f)

    def points(self, i, n=251):
        r = self.f.parts(i)[0][0]
        ll = np.linspace(r[0], r[-1], n)
        return np.column_stack(FRAME.geodetic_to_enu(ll[:,1], ll[:,0], 0.))

    def test_pbf_counts_reach_specs_markings_and_ai(self):
        self.assertEqual([roads.lane_counts(s) for s in self.specs], [(2,0),(3,0),(4,0)])
        self.assertTrue(all(len(roads.extras(s, self.points(i), LON)) == n-1+2
                            for i,(s,n) in enumerate(zip(self.specs,(2,3,4)))))
        graph = navgraph.build(FRAME, [self.f], flat, (0.,0.))
        self.assertTrue({2,3,4} <= {e[3] for e in graph['edges']})

    def test_two_to_three_and_three_to_four_join_without_steps(self):
        for i in (0,1):
            a,b = self.specs[i:i+2]
            p,q = self.points(i), self.points(i+1)
            x,w = roadlanes.pavement(a,p,FRAME)
            y,v = roadlanes.pavement(b,q,FRAME)
            for side in (-1,1):
                np.testing.assert_allclose(x[-1,:2]+side*x[-1,3:5]*w/2,
                                           y[0,:2]+side*y[0,3:5]*v/2, atol=.003)
            # Paint has the same boundary at the join and a gentle slope through the taper.
            ea = [ln for look,_,ln in roads.extras(a,p,LON,frame=FRAME) if look.get('edge_side') == -1][0]
            eb = [ln for look,_,ln in roads.extras(b,q,LON,frame=FRAME) if look.get('edge_side') == -1][0]
            np.testing.assert_allclose(ea[-1,:2], eb[0,:2], atol=.003)
            self.assertLess(np.abs(np.diff(np.r_[ea[:,1], eb[1:,1]])).max(), .04)
            self.assertAlmostEqual(roadlanes.counts(a,p,FRAME)[-1,0], i+2.5, places=3)
        self.assertEqual(len(roadlanes.model(self.specs[0].lane_model)['ends']),1,
                         'perpendicular ramp must not turn the through road into an extra lane')

    def test_reverse_and_tile_or_bridge_cuts_retain_profile(self):
        f = enriched(self.db, reverse=True)
        rs = roads.road_specs(f)
        for i, sp in enumerate(self.specs):
            p = self.points(i)
            a,w = roadlanes.pavement(sp,p,FRAME)
            b,v = roadlanes.pavement(rs[i],p[::-1],FRAME)
            np.testing.assert_allclose(a[:,:3], b[::-1,:3], atol=1e-6)
            np.testing.assert_allclose(a[:,3:5]*w, -b[::-1,3:5]*v, atol=1e-6)
            c,z = roadlanes.pavement(sp,p[100:220],FRAME)
            np.testing.assert_allclose(a[100:220,3:5]*w,c[:,3:5]*z,atol=3e-6)
        bridge = enriched(self.db)
        bridge.rules['flags'] = rules([(0,'is_bridge',.6,1.)])
        bsp = roads.bridge_specs(bridge)[0]
        self.assertEqual(bsp.lanes, self.specs[0].lanes, 'bridge paint matches the OSM approach lane count')
        p = self.points(0)[-50:]
        chunks = roads._deck_chunks([('bridge',bsp,p,LON)],(-1000,-30,1000,30))
        self.assertTrue(any(c.get('deck') and c.get('fixed') for c in chunks))

    def test_surface_and_grading_cover_widened_taper(self):
        s = self.specs[0]
        self.assertGreater(roadlanes.grading_spec(s).width,s.width)
        plan = grading.plan_road(FRAME,self.f.parts(0)[0][0],s,flat)
        self.assertGreater(plan.half, s.width/2+abs(s.bias))
        xx,yy = np.meshgrid(np.arange(-25.,26.), np.arange(-15.,16.))
        la,lo,_ = FRAME.enu_to_geodetic(xx,yy,0.)
        idx = np.full(xx.shape,surface.GRASS,np.uint8)
        result = surface.paint_roads(idx,la,lo,[self.f],1.)
        self.assertTrue(np.all(result[(np.abs(yy)<6) & (np.abs(xx)<20)] == surface.ROAD_MATERIAL['paved']))

    def test_unknown_id_or_geometry_never_borrows_nearby_counts(self):
        f = features([[(-500,50),(0,50)]],['motorway'])
        f.attrs['osm_sources'] = mapgeom.text_column([json.dumps([dict(dataset='OpenStreetMap',record_id='w101@1')])])
        source = osmlanes.LaneSource(self.temp.name,Mock())
        source.cached = Mock(return_value=self.db)
        source.enrich(f,LAT,LON)
        self.assertEqual(len(f.rules['lanes_forward'].feature),0)
        self.assertIsNone(roads.road_specs(f)[0].lanes)

    def test_serialized_rules_and_asymmetric_two_way_counts(self):
        arrays = self.f.to_arrays('')
        f = mapgeom.Features.from_arrays('roads', arrays, '')
        self.assertEqual([s.lanes for s in roads.road_specs(f)],[(2,0,0),(3,0,0),(4,0,0)])
        two = features([[(0,0),(500,0)]],['primary'])
        for key,n in [('lanes_forward',2),('lanes_backward',1),('lanes_both_ways',1)]:
            two.rules[key] = rules([(0,n)],'float')
        sp = roads.road_specs(two)[0]
        self.assertEqual(roads.lane_counts(sp),(2,1))
        self.assertAlmostEqual(roads.driving_width(sp),13.2)
        edges = navgraph.build(FRAME,[two],flat,(0,0))['edges']
        self.assertTrue(all(e[3:5]==[2,1] for e in edges))

    def test_short_way_does_not_compress_half_a_lane_into_its_end(self):
        with sqlite3.connect(self.db) as con:
            ll = np.frombuffer(con.execute('SELECT geometry FROM ways WHERE id=103').fetchone()[0],'<f8').reshape(-1,2).copy()
            ll[-1,0] = ll[0,0]+10/M_LON
            con.execute('UPDATE ways SET geometry=? WHERE id=103',(ll.tobytes(),))
        con.close()
        f = enriched(self.db)
        # The longer 3-lane way carries nearly the entire change to the short 4-lane way.
        sp = roads.road_specs(f)[1]
        end = [e for e in roadlanes.model(sp.lane_model)['ends'] if e['other'][0] == 4][0]
        self.assertGreater(end['weight'],.95)

    def test_simplification_retains_the_nonlinear_taper(self):
        p = self.points(0)
        ribbon,width = roadlanes.pavement(self.specs[0],p,FRAME)
        short = roads.simplify(ribbon,roads.CURVE_SIMPLIFY_M)
        self.assertGreater(len(short),5)
        # Inspect actual pavement edge, including its varying width, after serialization.
        ch = roads._chunk(roads.LOOK['paved'],width,short)
        nodes = np.asarray(ch['n'])
        crosses = np.asarray(ch['cross'])
        expected = ribbon[:,1]-ribbon[:,4]*width/2
        edge = np.interp(p[:,0],nodes[:,0],nodes[:,1]-crosses[:,1]*width/2)
        self.assertLess(np.max(np.abs(edge-expected)),.03)


class Downloads(unittest.TestCase):
    def test_range_download_resumes_and_checks_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root,'extract.pbf')
            data = b'cached OSM sample'
            header = {'content-range':f'bytes 0-0/{len(data)}','etag':'"v1"'}
            f = Mock()
            f.get.side_effect = [netfetch.Response(206,data[:1],header), IOError('interrupted')]
            source = osmlanes.LaneSource(root,f)
            with self.assertRaises(IOError):
                source._download('https://download.geofabrik.de/a.pbf',path)
            f.get.side_effect = [netfetch.Response(206,data[:1],header),
                                 netfetch.Response(206,data,{'content-range':f'bytes 0-{len(data)-1}/{len(data)}'})]
            source._download('https://download.geofabrik.de/a.pbf',path)
            self.assertEqual(Path(path).read_bytes(),data)
            self.assertFalse(os.path.exists(path+'.part'))

    def test_only_explicit_same_provider_redirects_are_followed(self):
        from test_netfetch import fetcher
        host='download.geofabrik.de'
        f,_,_,tr = fetcher({r'latest':[(307,{'location':'/snapshot.pbf'},b'',0)],
                            r'snapshot':[(206,{'content-range':'bytes 0-0/10'},b'x',0)]})
        r=f.get(f'https://{host}/latest.pbf',headers={'Range':'bytes=0-0'},redirect_hosts=(host,))
        self.assertEqual(r.status,206)
        self.assertEqual(len(tr.calls),2)
        self.assertEqual(tr.calls[1][2]['Range'],'bytes=0-0')
        f,_,_,tr = fetcher({r'latest':[(307,{'location':'https://other.example/data'},b'',0)]})
        with self.assertRaises(netfetch.FetchFailed):
            f.get(f'https://{host}/latest.pbf',redirect_hosts=(host,))
        self.assertEqual(len(tr.calls),1)


class GradingCoverage(unittest.TestCase):
    def test_widening_and_outer_shoulder_are_flat_across_the_full_pavement(self):
        import grading
        sp = roads.Spec(11.4, 'paved', False, 'motorway', True, dir=1,
                        shoulder=2.1, bias=.9, lanes=(2,0,0), lane_w=3.6,
                        lane_model=json.dumps({'sections':[{'counts':[2,0,0]}, {'counts':[4,0,0]}]}))
        xy = np.c_[np.arange(0., 402., 2.), np.zeros(201)]
        plan = grading._plan(xy, np.zeros(201), sp)
        # The outer edge of four lanes plus the asymmetric shoulder is 10.2 m from the source centre.
        y = np.linspace(-10.2, 10.2, 41)
        heights = grading.grade_points(np.full(len(y), 200.), y, .1*y, [plan])
        self.assertLess(np.ptp(heights), .001, 'the widening cannot retain the hillside below the new lane')
        self.assertGreaterEqual(plan.road_half, 10.2-1e-9)


if __name__ == '__main__':
    unittest.main()
