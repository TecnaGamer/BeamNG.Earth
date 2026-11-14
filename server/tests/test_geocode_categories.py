"""Offline category search contract: data, requests, ranking, caching and Enter-only services."""
import concurrent.futures
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geocode as g
import netfetch as nf


def osm(name='Nearby Circuit', lat=40.01):
    return {'elements': [{'type': 'way', 'id': 1, 'tags': {'name': name, 'addr:country': 'US'},
                          'center': {'lat': lat, 'lon': -75}}]}


def wd():
    return {'results': {'bindings': [{'itemLabel': {'value': 'Famous Circuit'},
            'coord': {'value': 'Point(10 50)'}, 'countryLabel': {'value': 'Germany'},
            'links': {'value': '80'}}]}}


class Net:
    def __init__(self):
        self.urls = []

    def get(self, url, kind='demand'):
        self.urls.append(url)
        host = urlparse(url).hostname
        data = (osm() if host == 'overpass-api.de' else wd() if host == 'query.wikidata.org' else
                [{'lat': '40', 'lon': '-75', 'boundingbox': ['39.9', '40.1', '-75.1', '-74.9'],
                  'address': {'country': 'United States'}}] if host == 'nominatim.openstreetmap.org' else {})
        return types.SimpleNamespace(body=json.dumps(data).encode())


class CategoriesTest(unittest.TestCase):
    def setUp(self):
        g._category_cache.clear()
        g._cache.clear()
        g._skip.clear()
        self.cat = g.category_query('race track')[0]

    def test_detection(self):
        cases = {'racetrack': 'race track', 'Race Tracks': 'race track', 'circuits': 'race track',
                 'raceways': 'race track', 'karting': 'karting', 'drag strips': 'drag strip',
                 'rallies': 'rally', 'rally stages': 'rally', 'gravel roads': 'rally',
                 'dirt': 'off-road', 'off-road': 'off-road', 'offroad': 'off-road', '4x4': 'off-road',
                 'trails': 'off-road', 'motocross': 'motocross', 'rocks': 'rock crawling',
                 'mountain peek': 'mountain peak', 'mountain peaks': 'mountain peak', 'summits': 'mountain peak',
                 'mountain passes': 'mountain pass', 'woods': 'forest', 'forests': 'forest',
                 'beaches': 'beach', 'lakes': 'lake', 'deserts': 'desert', 'airports': 'airport',
                 'bridges': 'bridge', 'tunnels': 'tunnel', 'dams': 'dam', 'canyons': 'canyon',
                 'scenic drives': 'scenic drive', 'racetrak': 'race track'}
        for text, expected in cases.items():
            with self.subTest(text=text):
                hit = g.category_query(text)
                self.assertIsNotNone(hit)
                self.assertEqual(hit[0]['name'], expected)
        self.assertEqual(g.category_query('race tracks NEAR New York')[1:], ('near', 'New York'))
        self.assertEqual(g.category_query('mountain peaks in Switzerland')[1:], ('in', 'Switzerland'))
        for text in ('Laguna Seca', 'Forest Hill', '123 Bridge Street', 'race track cafe', 'highest bridge in France'):
            self.assertIsNone(g.category_query(text), text)

    def test_builders(self):
        query = parse_qs(urlparse(g.category_overpass_url(self.cat, 40, -75)).query)['data'][0]
        self.assertIn('[timeout:5][maxsize:8388608]', query)
        self.assertIn('nwr["highway"="raceway"](around:50000,40.00,-75.00)', query)
        self.assertTrue(query.endswith('out center tags 200;'))
        self.assertNotIn('poly', query)
        query = parse_qs(urlparse(g.category_wikidata_url(self.cat, (1, 2, 3, 4))).query)['query'][0]
        self.assertIn('wd:Q1777138', query)
        self.assertIn('FILTER(?links >= 20)', query)
        self.assertIn('LIMIT 24', query)
        self.assertIn('geof:latitude(?coord) >= 1', query)
        self.assertNotIn('P279*', query)
        for cat in g.CATEGORIES:
            self.assertTrue(cat['osm'])
            self.assertTrue(all(c.startswith('Q') and c[1:].isdigit() for c in cat['classes']))

    def test_parsers(self):
        data = osm()
        data['elements'] += [{'lat': 91, 'lon': 0}, {'lat': 'NaN', 'lon': 0}, {},
                            {'lat': 40, 'lon': -75, 'tags': {}}]
        rows = g.parse_category_overpass(data, self.cat)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[-1]['unnamed'])
        self.assertEqual(rows[0]['detail'], 'US')
        self.assertEqual(g.parse_category_wikidata(wd(), self.cat)[0]['links'], 80)
        bad = wd()
        bad['results']['bindings'][0]['coord']['value'] = 'Point(200 100)'
        self.assertEqual(g.parse_category_wikidata(bad, self.cat), [])
        with self.assertRaises(ValueError):
            g.parse_category_overpass({'remark': 'timeout', **osm()}, self.cat)

    def test_enter_only_and_display(self):
        net = Net()
        g.search('race tracks', 40, -75, net=net)
        self.assertFalse(any('overpass' in u or 'query.wikidata' in u or 'nominatim' in u for u in net.urls))
        rows = g.search('race tracks', 40, -75, net=net, full=True)
        self.assertEqual([r['name'] for r in rows], ['Nearby Circuit', 'Famous Circuit'])
        self.assertIn('mi', rows[0]['detail'])
        self.assertIn('Germany', rows[1]['detail'])
        self.assertFalse(any(r.get('sure') for r in rows))
        with mock.patch.object(g, '_ask', return_value=([dict(name='Forest', detail='', kind='wikipedia', lat=1, lon=2)], [], 0)):
            self.assertNotIn('sure', g.search('forest', net=net)[0])

    def test_cache_rounding_and_worldwide_wire_reuse(self):
        net = Net()
        hit = g.category_query('racetrack')
        a = g._category_source(net, hit, 40.001, -75.001, 'overpass')
        g._category_source(net, g.category_query('race tracks'), 40.002, -75.002, 'overpass')
        self.assertEqual(len(net.urls), 1)
        g._category_source(net, hit, 40, -75, 'wikidata')
        g._category_source(net, hit, 41, -74, 'wikidata')
        self.assertEqual(len(net.urls), 2)
        self.assertEqual(a[0]['detail'], 'race track · 0.7 mi · US')
        with mock.patch.object(g.time, 'monotonic', return_value=g.time.monotonic() + g.CACHE_S + 1):
            g._category_source(net, hit, 40, -75, 'overpass')
        self.assertEqual(len(net.urls), 3)

    def test_anchor_shared_and_large_regions(self):
        net = Net()
        hit = g.category_query('race track near New York')
        g._category_source(net, hit, 40, -75, 'overpass')
        g._category_source(net, hit, 40, -75, 'wikidata')
        anchors = [u for u in net.urls if 'nominatim' in u]
        self.assertEqual(len(anchors), 1)
        self.assertEqual(parse_qs(urlparse(anchors[0]).query)['q'], ['New York'])
        with mock.patch.object(g, '_category_anchor', return_value=(40, -75, (20, 50, -120, -70), 'US')):
            self.assertEqual(g._category_source(net, g.category_query('race tracks in US'), 40, -75, 'overpass'), [])

    def test_empty_failure_and_concurrent_cache(self):
        fn = mock.Mock(return_value=[])
        self.assertEqual(g._category_cached('empty', fn), [])
        g._category_cached('empty', fn)
        fn.assert_called_once()
        fail = mock.Mock(side_effect=nf.FetchFailed('busy'))
        for _ in range(2):
            with self.assertRaises(nf.FetchFailed):
                g._category_cached('fail', fail)
        fail.assert_called_once()
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(1)
            return [1]
        fn = mock.Mock(side_effect=slow)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            a = pool.submit(g._category_cached, 'shared', fn)
            self.assertTrue(entered.wait(1))
            b = pool.submit(g._category_cached, 'shared', fn)
            release.set()
            self.assertEqual(a.result(), b.result())
        fn.assert_called_once()

    def test_ranking_dedup_and_name_search(self):
        def row(name, distance, source='overpass', **kw):
            return dict(name=name, detail='', kind='race track', lat=distance / 111, lon=0,
                        distance=distance, source=source, **kw)
        rows = [row('Far', 20), row('Near', 1), row('Unnamed race track', .1, unnamed=True),
                row('Famous', 200, 'wikidata', links=90), row('Near', 1, 'wikidata', links=30)]
        out = g.merge_categories('race track', [], rows)
        self.assertEqual([r['name'] for r in out], ['Near', 'Far', 'Unnamed race track', 'Famous'])
        net = Net()
        g.search('Laguna Seca', 40, -75, net=net, full=True)
        self.assertFalse(any('overpass' in u or 'query.wikidata' in u for u in net.urls))

    def test_policies(self):
        for host in ('overpass-api.de', 'query.wikidata.org'):
            p = nf.POLICIES[host]
            self.assertFalse(p.denied)
            self.assertLessEqual(p.rate, .1)
            self.assertEqual((p.burst, p.max_conc, p.max_attempts), (1, 1, 1))
            self.assertLessEqual(p.daily_hard, 100)
            self.assertFalse(p.allow_prefetch)
            self.assertTrue(p.interactive_only)
        self.assertEqual(nf.POLICIES['nominatim.openstreetmap.org'].rate, 1)
        self.assertIn('BeamNG', nf.USER_AGENT)

    def test_distance_across_date_line_and_global(self):
        self.assertAlmostEqual(g.category_km(0, 179.9, 0, -179.9), 22.239, places=2)
        self.assertAlmostEqual(g.category_km(0, 0, 0, 180), 20015.087, places=2)

    def test_no_full_search_category_failure_loses_normal_matches(self):
        normal = dict(name='Race Track Park', detail='US', kind='park', lat=40, lon=-75)
        with mock.patch.object(g, '_category_source', side_effect=nf.FetchFailed('busy')), \
                mock.patch.object(g, '_photon', return_value=[normal]), \
                mock.patch.object(g, '_nominatim', return_value=[]), \
                mock.patch.object(g, '_wikidata', return_value=[]), \
                mock.patch.object(g, '_wikipedia', return_value=[]):
            rows = g.search('race track', 40, -75, net=Net(), full=True)
        self.assertEqual(rows[0]['name'], normal['name'])

    def test_unknown_anchor_never_uses_player_as_silent_fallback(self):
        net = Net()
        with mock.patch.object(g, '_json', return_value=[]):
            with self.assertRaises(ValueError):
                g._category_source(net, g.category_query('race tracks near nowhere'), 40, -75, 'overpass')
        self.assertEqual(net.urls, [])

    def test_ui_result_metadata_survives_merging(self):
        net = Net()
        rows = g.search('race tracks', 40, -75, net=net, full=True)
        self.assertEqual(rows[0]['result_type'], 'category')
        self.assertAlmostEqual(rows[0]['distance_mi'], .691, places=2)
        self.assertEqual(rows[0]['country'], 'US')
        self.assertEqual(rows[1]['country'], 'Germany')
        address = g.merge('Main Street', [dict(name='Main Street', kind='street', detail='Town',
                           country='United States', lat=40, lon=-75)], 40, -75)[0]
        self.assertEqual(address['result_type'], 'address')
        self.assertEqual(address['distance_mi'], 0)
        self.assertEqual(address['country'], 'United States')
        normal = g.merge('Landmark', [dict(name='Landmark', kind='wikipedia', lat=40, lon=-75)])[0]
        self.assertEqual(normal['result_type'], 'name')
        self.assertNotIn('distance_mi', normal)
        self.assertEqual(normal['country'], '')

    def test_near_me_is_the_player_not_a_place(self):
        for text in ('race tracks near me', 'race tracks nearby', 'peaks around here', 'forest close to me'):
            with self.subTest(text=text):
                self.assertEqual(g.category_query(text)[1:], (None, None))

    def test_exact_place_name_matching_a_category_is_kept(self):
        def run(q, name):
            place = dict(name=name, detail='US', kind='village', lat=40.2, lon=-75)
            with mock.patch.object(g, '_photon', return_value=[place]), \
                    mock.patch.object(g, '_nominatim', return_value=[]), \
                    mock.patch.object(g, '_wikidata', return_value=[]), \
                    mock.patch.object(g, '_wikipedia', return_value=[]):
                return [r['name'] for r in g.search(q, 40, -75, net=Net(), full=True)]
        self.assertEqual(run('Lake in the Hills', 'Lake in the Hills')[0], 'Lake in the Hills')
        g._category_cache.clear()
        names = run('Summit', 'Summit')
        self.assertIn('Summit', names)
        self.assertEqual(names[0], 'Nearby Circuit', 'a bare category word still lists category places first')
