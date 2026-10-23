"""The 1.6 worker/fallback both stream signs without changing road geometry or physics."""
import asyncio
import json
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
    return SimpleNamespace(mapdata=md, anchors={1: anchor}, road_pool=None, surface=None,
        grader_for=lambda a: None, ground=lambda f, la, lo, z: z, dem=FakeDem(),
        grader_lock=threading.Lock(), sea=None, buildings=False, road_widths=None,
        left_hand=lambda a: False, mph=lambda a: True, anchor_q=lambda a: None,
        airports=None, near_visual='detail', out_dir=out, vfs_dir='/temp/test', sidewalks=False,
        _tunnel_ground=lambda a, g: None)


class PipelineTest(unittest.TestCase):
    def test_worker_and_server_emit_identical_signs_and_keep_upstream_road_meshes(self):
        with tempfile.TemporaryDirectory() as root:
            directories = [Path(root, name) for name in ('server', 'worker', 'unsigned')]
            for path in directories:
                path.mkdir()
            md = fixture(root)
            direct = server(md, str(directories[0]))
            expected = earthd.Server.build_roads_tile(direct, 1, 0, 0)
            blockworker._s = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None,
                airports=None, sea=None, lock=threading.Lock(), bridges={}, anchor=lambda *a: (None, None))
            result = blockworker.roads_tile(1, LAT, LON, 15, 0, 0, str(directories[1]),
                                            '/temp/test', False, earthd.DETAIL_ROAD_LIFT)
            self.assertEqual(result, expected)
            self.assertEqual(sorted(p.name for p in directories[0].iterdir()),
                             sorted(p.name for p in directories[1].iterdir()))
            for path in directories[0].iterdir():
                self.assertEqual(path.read_bytes(), (directories[1] / path.name).read_bytes(), path.name)
            data = json.loads(next(directories[0].glob('*.json')).read_text(encoding='utf-8'))
            for kind in ('exits', 'mileposts'):
                self.assertIn(kind, data)
                self.assertIn(kind, roads.shape_files(1, 0, 0))
                self.assertNotIn(kind, data.get('colliders', {}))
                shape = directories[0] / Path(data[kind]['shape']).name
                self.assertGreater(shape.stat().st_size, 1000)
            md.highway_signs.close()
            md.highway_signs = None
            earthd.Server.build_roads_tile(server(md, str(directories[2])), 1, 0, 0)
            unsigned = json.loads(next(directories[2].glob('*.json')).read_text(encoding='utf-8'))
            self.assertEqual({k: v for k, v in data.items() if k not in ('exits', 'mileposts')}, unsigned)
            for path in directories[2].glob('*.cdae'):
                self.assertEqual(path.read_bytes(), (directories[0] / path.name).read_bytes(), path.name)

    def test_optional_sign_failure_does_not_fail_the_road_tile(self):
        with tempfile.TemporaryDirectory() as root:
            md = fixture(root)
            with mock.patch.object(highwaysigns.osmsigns, 'tile_exit_objects', side_effect=OSError('offline')):
                with self.assertLogs('earthd.highwaysigns', level='WARNING'):
                    result = earthd.Server.build_roads_tile(server(md, root), 1, 0, 0)
            self.assertIsNotNone(result)
            self.assertTrue(list(Path(root).glob('road_*.cdae')))

    def test_background_index_refreshes_once_without_removing_visible_roads(self):
        with tempfile.TemporaryDirectory() as root:
            md = fixture(root)
            source, tracker = md.highway_signs, roads.Tracker()
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

    def test_refresh_during_build_retries_once_and_anchor_change_discards_it(self):
        tracker, conn = roads.Tracker(), FakeConn()
        started, release = threading.Event(), threading.Event()
        built = []

        def build(aid, tx, ty):
            built.append((aid, tx, ty))
            started.set()
            release.wait(5.)
            if len(built) == 1:
                return None                # index completed while a formerly empty tile was still building
            return '/tile.json', 0

        async def run():
            with mock.patch.object(roads, 'wanted_tiles', return_value={(0, 0)}):
                loop = asyncio.get_running_loop()
                tracker.on_pose(conn, 1, 0., 0., loop, build)
                while not started.is_set():
                    await asyncio.sleep(.001)
                tracker.refresh()
                release.set()
                for _ in range(30):
                    await asyncio.sleep(.002)
                    tracker.on_pose(conn, 1, 0., 0., loop, build)
                self.assertEqual(built, [(1, 0, 0), (1, 0, 0)])
                self.assertEqual(len(conn.sent), 1)
                tracker.refresh()
                tracker.on_pose(conn, 2, 0., 0., loop, build)
                self.assertFalse(tracker.stale)
                await asyncio.sleep(.01)
            tracker.close()
        asyncio.run(run())


class NetworkTest(unittest.TestCase):
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
