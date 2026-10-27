"""The 1.6 worker/fallback both stream signs without changing road geometry or physics."""
import asyncio
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
import json
import multiprocessing
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.request import Request

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker
import earthd
import highwaysigns
import milemarkers
import netfetch
import osmregions
import osmsigns
import roads
import revisit
import signstream
from test_blockworker import FakeDem, FakeMapData
from test_roads import FRAME, LAT, LON, FakeConn, features, rules


def fixture(raw_dir):
    f = features([[(-1900., 100.), (700., 100.)], [(0., 100.), (100., -20.), (200., -100.)]],
                 ['motorway', 'motorway'], ids=['osm:w10:0', 'osm:w20:0'])
    f.rules['oneway'] = rules([(0, 1.), (1, 1.)], 'float')
    f.rules['subclass'] = rules([(1, 'link')])
    f.attrs['osm_lanes'] = np.array([2., 1.])
    md = FakeMapData(f, roads.cells_of_tile(FRAME, 0, 0)[0])
    md.raw_dir, md.fetcher = raw_dir, SimpleNamespace(offline=True)
    md.highway_signs = highwaysigns.Source(md)
    main, ramp = f.parts(0)[0][0], f.parts(1)[0][0]
    exit_obj = dict(id=-20, exit=dict(label=dict(ref='36', names=['Auburn'], routes=[]),
                       approach=[main[0].tolist(), ramp[0].tolist()], ramp=ramp.tolist(),
                       main_ids=[10], ramp_id=20, side=1))
    la, lo, _ = FRAME.enu_to_geodetic(150., 100., 0.)
    mile = milemarkers._object('MI', 69, 116, 'E', [float(lo), float(la)], [1., 0.], 'dot:mi')
    md.highway_signs.objects.query_exits = lambda *args: [exit_obj]
    md.highway_signs.objects.query_mile_markers = lambda *args: [mile]
    return md


def server(md, out):
    anchor = SimpleNamespace(id=1, lat=LAT, lon=LON, frame=FRAME, zooms=[15], bridges=None, tunnels=None)
    return SimpleNamespace(mapdata=md, anchors={1: anchor}, road_pool=None, far_bld_pool=None, sign_pool=None,
        gas_stations=False, surface=None,
        grader_for=lambda a: None, ground=lambda f, la, lo, z: z, dem=FakeDem(),
        grader_lock=threading.Lock(), sea=None, buildings=False, road_widths=None,
        left_hand=lambda a: False, mph=lambda a: True, anchor_q=lambda a: None,
        airports=None, near_visual='detail', out_dir=out, vfs_dir='/temp/test', sidewalks=False,
        _tunnel_ground=lambda a, g: None)


def init_sign_worker(raw_dir):
    """Spawn-safe fixture: exercise the actual worker entry point in another process."""
    blockworker._s = SimpleNamespace(md=fixture(raw_dir), dem=FakeDem(), widths=None, surface=None,
        airports=None, sea=None, anchor=lambda *a: (None, None))


class PipelineTest(unittest.TestCase):
    def test_spawned_sign_pool_matches_thread_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            md = fixture(root)
            self.addCleanup(md.highway_signs.close)
            direct, worker = Path(root, 'direct'), Path(root, 'worker')
            direct.mkdir()
            worker.mkdir()
            expected = earthd.Server.build_signs_tile(server(md, str(direct)), 1, 0, 0)
            with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context('spawn'),
                                     initializer=init_sign_worker, initargs=(root,)) as pool:
                srv = server(md, str(worker))
                srv.sign_pool = pool
                self.assertEqual(earthd.Server.build_signs_tile(srv, 1, 0, 0), expected)
            self.assertEqual({p.name: p.read_bytes() for p in direct.iterdir()},
                             {p.name: p.read_bytes() for p in worker.iterdir()})

    def test_revisited_roads_can_get_fresh_signs_without_rebuilding_roads(self):
        with tempfile.TemporaryDirectory() as root:
            md = fixture(root)
            self.addCleanup(md.highway_signs.close)
            first, revisit_dir = Path(root, 'first'), Path(root, 'revisited')
            first.mkdir()
            revisit_dir.mkdir()
            state = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None, sea=None,
                                    lock=threading.Lock(), cache_dir=root, bridges={},
                                    anchor=lambda *a: (None, None))
            with mock.patch.object(blockworker, '_s', state), mock.patch.object(revisit, 'ON', True), \
                    mock.patch.object(revisit, 'SKIP', set()):
                blockworker.roads_tile(1, LAT, LON, 15, 0, 0, str(first), '/temp/first', False, .05)
                self.assertTrue(list(Path(root, 'revisit').rglob('*.zip')))
                with mock.patch.object(blockworker, '_roads_tile', side_effect=AssertionError('cache missed')):
                    path, _, pads = blockworker.roads_tile(
                        2, LAT, LON, 15, 0, 0, str(revisit_dir), '/temp/revisited', False, .05)
                self.assertEqual(path, '/temp/revisited/roads_2_0_0.json')
                self.assertEqual(pads, [])
                signs = blockworker.signs_tile(2, LAT, LON, 15, 0, 0, str(revisit_dir), '/temp/revisited')
                data = json.loads((revisit_dir / Path(signs[0]).name).read_text())
                self.assertIn('exits', data)
                self.assertIn('mileposts', data)
                self.assertIn('/temp/revisited/', data['exits']['shape'])

    def test_sign_worker_and_fallback_match_without_touching_road_outputs(self):
        with tempfile.TemporaryDirectory() as root:
            md = fixture(root)
            dirs = [Path(root, name) for name in ('direct', 'worker', 'roads')]
            for path in dirs:
                path.mkdir()
            direct = server(md, str(dirs[0]))
            expected = earthd.Server.build_signs_tile(direct, 1, 0, 0)
            blockworker._s = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None,
                airports=None, sea=None, anchor=lambda *a: (None, None))
            result = blockworker.signs_tile(1, LAT, LON, 15, 0, 0, str(dirs[1]), '/temp/test')
            self.assertEqual(result, expected)
            for path in dirs[0].iterdir():
                self.assertEqual(path.read_bytes(), (dirs[1] / path.name).read_bytes(), path.name)
            data = json.loads((dirs[0] / 'signs_1_0_0.json').read_text())
            self.assertIn('exits', data)
            self.assertIn('mileposts', data)
            self.assertNotIn('colliders', data)
            self.assertEqual(data['chunks'], [])
            road_server = server(md, str(dirs[2]))
            with mock.patch.object(highwaysigns, 'build_tile', side_effect=AssertionError('road waited on signs')):
                earthd.Server.build_roads_tile(road_server, 1, 0, 0)
            before = {p.name: p.read_bytes() for p in dirs[2].iterdir()}
            earthd.Server.build_signs_tile(road_server, 1, 0, 0)
            for name, content in before.items():
                self.assertEqual(content, (dirs[2] / name).read_bytes(), name)
            md.highway_signs.close()

    def test_optional_source_failure_keeps_road_build_available(self):
        with tempfile.TemporaryDirectory() as root:
            md = fixture(root)
            with mock.patch.object(highwaysigns.osmsigns, 'tile_exit_objects', side_effect=OSError('offline')):
                with self.assertLogs('earthd.highwaysigns', level='WARNING'):
                    result = earthd.Server.build_signs_tile(server(md, root), 1, 0, 0)
            self.assertIsNotNone(result)
            self.assertIsNotNone(earthd.Server.build_roads_tile(server(md, root), 1, 0, 0))
            md.highway_signs.close()

    def test_background_index_refreshes_signs_once_and_keeps_visible_tiles(self):
        with tempfile.TemporaryDirectory() as root:
            md = fixture(root)
            source, tracker = md.highway_signs, signstream.Tracker()
            tracker.sent.add((0, 0))
            source.objects._region = lambda *args: 'us_michigan'
            index = Path(root, 'ready.sqlite')
            source.objects._readable = lambda *args: (str(index), 4) if index.exists() else None
            with mock.patch.object(highwaysigns.time, 'monotonic', return_value=0.):
                source.refresh(tracker, FRAME, 0., 0.)
            index.write_bytes(b'index completed by worker')
            with mock.patch.object(highwaysigns.time, 'monotonic', return_value=6.):
                source.refresh(tracker, FRAME, 0., 0.)
            self.assertEqual(tracker.sent, {(0, 0)})
            self.assertEqual(tracker.stale, {(0, 0)})
            tracker.stale.clear()
            with mock.patch.object(highwaysigns.time, 'monotonic', return_value=12.):
                source.refresh(tracker, FRAME, 0., 0.)
            self.assertFalse(tracker.stale)
            md.highway_signs.close()


class StreamingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tracker = signstream.Tracker()
        self.addCleanup(self.tracker.close)
        self.conn = SimpleNamespace(s=SimpleNamespace(out_dir=self.temp.name), sent=[])
        self.conn.send = self.conn.sent.append
        self.pending = []
        def queue(*args):
            future = asyncio.get_running_loop().create_future()
            self.pending.append(future)
            return future
        self.loop = SimpleNamespace(run_in_executor=queue)
        self.build = mock.Mock()

    def pose(self, ready, aid=1):
        self.tracker.on_pose(self.conn, aid, 0., 0., self.loop, self.build, ready)

    async def finish(self, index=0, aid=1):
        result = signstream.write_tile(self.temp.name, '/temp/test', aid, 0, 0, (None, None, None))
        self.pending[index].set_result(result)
        await asyncio.sleep(0)

    async def test_waits_for_roads_and_empty_replacements_do_not_pool_stale_shapes(self):
        self.pose(set())
        self.assertFalse(self.pending)
        self.pose({(0, 0)})
        pool = self.conn.s.pool_shapes = mock.Mock(return_value={})
        await self.finish()
        self.assertEqual(self.conn.sent[0]['key'], 's0_0')
        self.assertEqual(pool.call_args.args[1], {})
        self.pose({(0, 0)})
        self.assertEqual(len(self.pending), 1)
        self.pose(set())
        self.assertEqual(self.conn.sent[-1]['type'], 'roads_drop')
        self.assertEqual(self.conn.sent[-1]['key'], 's0_0')

    async def test_refresh_in_flight_requeues_and_discards_old_index_result(self):
        self.pose({(0, 0)})
        self.tracker.refresh()
        await self.finish()
        self.assertFalse(self.conn.sent)
        self.pose({(0, 0)})
        await self.finish(1)
        self.assertEqual(len(self.conn.sent), 1)
        self.assertFalse(self.tracker.stale)

    async def test_rebases_bound_work_and_do_not_announce_old_anchor(self):
        self.pose({(0, 0), (1, 0), (2, 0)})
        self.assertEqual(len(self.pending), signstream.WORKERS)
        self.pose({(0, 0)}, aid=2)
        self.assertEqual(len(self.pending), signstream.WORKERS)
        await self.finish()
        self.assertFalse(self.conn.sent)
        self.pose({(0, 0)}, aid=2)
        self.assertEqual(len(self.pending), signstream.WORKERS + 1)

    async def test_departed_tile_and_closed_connection_reject_completed_work(self):
        self.pose({(0, 0)})
        self.pose(set())
        await self.finish()
        self.assertFalse(self.conn.sent)
        self.pose({(0, 0)})
        self.tracker.close()
        await asyncio.sleep(0)
        self.assertFalse(self.conn.sent)

    async def test_worker_failure_retries_later_without_dropping_roads(self):
        self.pose({(0, 0)})
        with self.assertLogs('earthd.signs', level='WARNING'):
            self.pending[0].set_exception(OSError('unavailable'))
            await asyncio.sleep(0)
        self.assertFalse(self.conn.sent)
        self.pose({(0, 0)})
        self.assertEqual(len(self.pending), 1)
        self.tracker.retry[(0, 0)] = 0.
        self.pose({(0, 0)})
        self.assertEqual(len(self.pending), 2)

    async def test_road_setting_reset_cancels_old_sign_jobs_and_resends_signs(self):
        self.pose({(0, 0)})
        old = self.tracker
        self.conn.roads, self.conn.signs = roads.Tracker(), old
        earthd.Connection.dispatch(self.conn, {'type': 'roads_reset'})
        self.addCleanup(self.conn.roads.close)
        self.addCleanup(self.conn.signs.close)
        await asyncio.sleep(0)
        self.assertTrue(old.closed)
        self.assertTrue(self.pending[0].cancelled())
        self.tracker = self.conn.signs
        self.pose({(0, 0)})
        await self.finish(1)
        self.assertEqual([m['key'] for m in self.conn.sent], ['s0_0'])


class NetworkTest(unittest.TestCase):
    def test_osmium_import_builds_a_queryable_destination_index(self):
        with tempfile.TemporaryDirectory() as root:
            osm = Path(root, 'small.osm')
            osm.write_text('''<osm version="0.6">
              <node id="1" lat="42.8" lon="-84.7"/>
              <node id="2" lat="42.8" lon="-84.69"><tag k="highway" v="motorway_junction"/><tag k="ref" v="36"/></node>
              <node id="3" lat="42.8" lon="-84.68"/>
              <node id="4" lat="42.799" lon="-84.688"/>
              <node id="5" lat="42.798" lon="-84.686"/>
              <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/>
                <tag k="highway" v="motorway"/><tag k="oneway" v="yes"/><tag k="ref" v="I 69"/>
              </way>
              <way id="20"><nd ref="2"/><nd ref="4"/><nd ref="5"/>
                <tag k="highway" v="motorway_link"/><tag k="oneway" v="yes"/>
                <tag k="destination" v="Auburn"/><tag k="destination:ref" v="US 27"/>
              </way>
            </osm>''', encoding='utf-8')
            target = Path(root, 'signs.sqlite')
            self.assertGreater(osmsigns.import_pbf(osm, str(target)), 0)
            with closing(sqlite3.connect(target)) as con:
                exits = con.execute("SELECT ways FROM objects WHERE kind='exit'").fetchall()
                self.assertTrue(exits)
                self.assertIn('Auburn', exits[0][0])
                self.assertGreater(con.execute('SELECT COUNT(*) FROM spatial').fetchone()[0], 0)

    def test_existing_index_is_readable_but_new_index_cannot_overwrite_legacy_objects(self):
        with tempfile.TemporaryDirectory() as root:
            regions = osmregions.RegionSource(root, SimpleNamespace(offline=True))
            Path(regions.root).mkdir()
            source = osmsigns.ObjectSource(regions)
            old = Path(regions.root, 'us_michigan.objects.v4.sqlite')
            old.write_bytes(b'legacy lights and signs')
            self.assertEqual(source._readable('us_michigan', True), (str(old), 4))
            self.assertTrue(source._current('us_michigan'))
            new = Path(source.path('us_michigan'))
            self.assertNotEqual(new, old)
            new.write_bytes(b'new sign-only index')
            self.assertEqual(source._readable('us_michigan', 'mile_marker'), (str(new), 4))
            self.assertEqual(old.read_bytes(), b'legacy lights and signs')

    def test_download_uses_current_fetcher_session_headers_and_byte_accounting(self):
        session = SimpleNamespace(headers={'User-Agent': 'BeamNG.Earth-test'}, status=0, nbytes=0)
        fetcher = mock.Mock()
        fetcher.session.return_value.__enter__ = mock.Mock(return_value=session)
        fetcher.session.return_value.__exit__ = mock.Mock(return_value=False)
        fetcher.policy.return_value.timeout = 7
        response = mock.MagicMock(status=206, headers={'Content-Range': 'bytes 0-0/100'},
                                  url='https://download.geofabrik.de/us/michigan-260101.osm.pbf')
        response.read.return_value = b'x'
        response.__enter__.return_value = response
        with mock.patch.object(osmregions, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            source = osmregions.RegionSource('unused', fetcher)
            result = source._get(response.url, {'Range': 'bytes=0-0'})
            request = opener.return_value.open.call_args.args[0]
        fetcher.session.assert_called_once_with(response.url, kind='background')
        self.assertEqual(request.get_header('Range'), 'bytes=0-0')
        self.assertEqual(request.get_header('User-agent'), 'BeamNG.Earth-test')
        self.assertEqual((session.status, session.nbytes), (206, 1))
        self.assertEqual(result.headers['content-range'], 'bytes 0-0/100')

    def test_offline_mode_makes_no_network_request(self):
        fetcher = netfetch.Fetcher(offline=True)
        source = osmregions.RegionSource('unused', fetcher)
        with mock.patch.object(osmregions, 'build_opener') as opener:
            with self.assertRaises(netfetch.FetchError):
                source._get(osmregions.INDEX_URL)
            opener.assert_not_called()

    def test_redirects_stay_on_the_https_extract_host(self):
        handler = osmregions.SameHostRedirect()
        request = Request('https://download.geofabrik.de/us/michigan-latest.osm.pbf')
        for url in ('http://download.geofabrik.de/a.osm.pbf', 'https://example.org/a.osm.pbf',
                    'https://download.geofabrik.de:8443/a.osm.pbf'):
            with self.assertRaises(OSError):
                handler.redirect_request(request, None, 302, 'Found', {}, url)
        target = 'https://download.geofabrik.de/us/michigan-260101.osm.pbf'
        self.assertEqual(handler.redirect_request(request, None, 302, 'Found', {}, target).full_url, target)


if __name__ == '__main__':
    unittest.main()
