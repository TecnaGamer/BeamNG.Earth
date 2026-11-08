"""Dataset replacement is not an outage; saved sign candidates survive outages."""
from concurrent.futures import CancelledError
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
sys.path[:0] = [str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import netfetch
import osmtags
import rangeread
import routerepeaters
from test_netfetch import fetcher, url, USGS
from test_route_repeaters import corridor


class RecoveryTest(unittest.TestCase):
    def test_replaced_dataset_does_not_open_host_breaker(self):
        f, _, _, _ = fetcher({USGS: [(412, {}, b'', 0.)]})
        for _ in range(10):
            with self.assertRaises(netfetch.FetchFailed) as caught:
                f.get(url(), headers={'If-Match':'old'})
            self.assertEqual(caught.exception.status,412)
        self.assertEqual(f.snapshot()[USGS]['trips'],0)

    def test_version_probe_recovers_after_short_cooldown_and_clears_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetch = mock.Mock()
            fetch.get.side_effect = [OSError('offline'), SimpleNamespace(headers={'etag':'"new"','content-range':'bytes 0-15/100'})]
            source = osmtags.OsmTags(tmp,fetch)
            with mock.patch.object(osmtags.time,'time',return_value=1000.):
                with self.assertRaises(OSError): source.version()
            down = Path(tmp)/'osmtags/unreachable'
            os.utime(down,(1000.,1000.))
            with mock.patch.object(osmtags.time,'time',return_value=1030.):
                with self.assertRaises(rangeread.RangeError): source.version()
            self.assertEqual(fetch.get.call_count,1)
            with mock.patch.object(osmtags.time,'time',return_value=1061.):
                version, remote = source.version()
            self.assertEqual(version,'"new"')
            self.assertEqual(remote.if_match,'"new"')
            self.assertFalse(down.exists())
            self.assertEqual(list((Path(tmp)/'osmtags').glob('*.tmp')),[])

    def test_cancelled_probe_never_marks_source_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetch=mock.Mock();fetch.get.side_effect=CancelledError()
            with self.assertRaises(CancelledError): osmtags.OsmTags(tmp,fetch).version()
            self.assertFalse((Path(tmp)/'osmtags/unreachable').exists())

    def test_saved_candidates_survive_restart_and_resume_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidates=routerepeaters.thin(routerepeaters.candidates(corridor()),(-85.2,34.8,-85.,35.1))
            self.assertTrue(candidates)
            first=routerepeaters.Source(tmp,None)
            with mock.patch.object(first,'_online_cell',return_value=candidates):
                self.assertEqual(first._cell(-851,349),candidates)
            restarted=routerepeaters.Source(tmp,None)
            with mock.patch.object(restarted,'_online_cell',side_effect=OSError('offline')) as read:
                self.assertEqual(restarted._cell(-851,349),candidates)
                self.assertEqual(restarted._cell(-851,349),candidates)
                self.assertEqual(read.call_count,1)
            restarted.retry_at.clear()
            with mock.patch.object(restarted,'_online_cell',return_value=[]):
                self.assertEqual(restarted._cell(-851,349),[])
            restarted.retry_at.clear()
            with mock.patch.object(restarted,'_online_cell',side_effect=OSError('offline')):
                self.assertEqual(restarted._cell(-851,349),[], 'a successful empty refresh replaces stale signs')

    def test_uncached_neighbour_does_not_discard_available_signs(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=routerepeaters.Source(tmp,None)
            objects=routerepeaters.candidates(corridor())
            in_box=[o for o in objects if -85.2 <= o['lon'] <= -85. and 34.8 <= o['lat'] <= 35.1]
            with mock.patch.object(source,'_cell',side_effect=lambda x,y: in_box if x==-852 and y==348 else (_ for _ in ()).throw(OSError('offline'))):
                self.assertEqual(source.query(-85.2,34.8,-85.,35.1),in_box)

    def test_no_saved_data_preserves_failure_and_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=routerepeaters.Source(tmp,None)
            for error in (OSError('offline'),CancelledError()):
                with mock.patch.object(source,'_online_cell',side_effect=error):
                    with self.assertRaises(type(error)): source._cell(1,2)
            source.saved.mkdir(parents=True)
            (source.saved/'1_2.json').write_text('{broken')
            with mock.patch.object(source,'_online_cell',side_effect=OSError('offline')):
                with self.assertRaises(OSError): source._cell(1,2)


if __name__ == '__main__': unittest.main()
