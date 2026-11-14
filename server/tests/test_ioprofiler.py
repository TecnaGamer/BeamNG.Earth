"""Passive service accounting, file counters, real HTTP and mode-cost gates."""
import http.server
from contextlib import contextmanager
import io as memoryio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ioprofiler as io
import netfetch
import cache
import runtimeprofiler as profiler


class IOTest(unittest.TestCase):
    def setUp(self):
        self.monitor=io.Monitor()
        self.monitor.configure('full','capture')
        self.patch=patch.object(io,'monitor',self.monitor)
        self.patch.start()

    def tearDown(self):
        self.monitor.configure('off')
        self.patch.stop()

    def view(self): return io.View().snapshot([self.monitor.snapshot()],self.monitor.mode)

    def test_endpoint_never_contains_credentials_or_request_path(self):
        self.assertEqual(io.endpoint('https://user:secret@example.com:8443/path?key=private'),'https://example.com:8443')
        self.assertEqual(io.endpoint('https://example.com:443/a'),'https://example.com')
        self.assertEqual(io.endpoint('tcp://[::1]:1234'),'tcp://[::1]:1234')
        self.assertIsNone(io.endpoint('file:///private/path'))
        self.assertIsNone(io.endpoint('https://host:invalid'))

    def test_requests_failures_latency_and_concurrency(self):
        with io.Request('https://example.com/private?token=secret',10) as r:
            self.assertEqual(self.monitor.snapshot()['services'][0]['inflight'],1)
            r.headers();r.complete(429,1024)
        try:
            with io.Request('https://example.com/another'): raise TimeoutError('secret text')
        except TimeoutError: pass
        row=self.view()['services'][0]
        self.assertEqual((row['requests'],row['responses'],row['failures'],row['inflight']),(2,1,1,0))
        self.assertEqual(row['receivedBytes'],1024)
        self.assertEqual(row['rateLimited'],1)
        self.assertEqual(row['lastError'],'TimeoutError')
        self.assertGreaterEqual(row['responseAvgMs'],0)
        self.assertIn('responseP95Ms',row)
        self.assertNotIn('secret',json.dumps(row))
        self.assertEqual(row['health'],'connection error')

    def test_404_is_a_response_not_a_transport_failure(self):
        with io.Request('https://example.com') as r: r.headers();r.complete(404,8)
        row=self.view()['services'][0]
        self.assertEqual(row['failures'],0)
        self.assertEqual(row['httpErrors'],1)
        self.assertEqual(row['health'],'reachable')

    def test_essential_has_no_histograms_or_os_queries(self):
        self.monitor.configure('essential','essential-capture')
        with patch.object(io,'process_io',side_effect=AssertionError('expensive check must be disabled')):
            with io.Request('https://example.com') as r: r.headers();r.complete(200,10)
            value=self.view()
        self.assertNotIn('samples',self.monitor.snapshot()['services'][0])
        self.assertNotIn('responseP95Ms',value['services'][0])
        self.assertEqual(value['processes'],[])
        self.assertIsNotNone(value['services'][0]['responseAvgMs'])

    def test_disabled_does_not_observe_requests_or_disk(self):
        self.monitor.configure('off')
        with io.Request('https://example.com') as r: r.complete(200,10)
        self.monitor.record_read(50,2)
        self.assertEqual(self.monitor.snapshot()['services'],[])
        self.assertEqual(self.monitor.snapshot()['disk']['readBytes'],0)

    def test_mode_change_discards_late_request(self):
        request=io.Request('https://example.com')
        self.monitor.configure('essential','new-capture')
        request.complete(200,10);request.__exit__(None,None,None)
        self.assertEqual(self.monitor.snapshot()['services'],[])

    def test_cache_read_bytes_misses_and_full_latency(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'cache.bin';p.write_bytes(b'abcdef')
            self.assertEqual(cache.NONE.read(str(p)),b'abcdef')
            self.assertIsNone(cache.NONE.read(str(p)+'-absent'))
            cache.NONE.wrote(str(p),6)
        d=self.monitor.snapshot()['disk']
        self.assertEqual((d['readBytes'],d['reads'],d['misses'],d['outputBytes']),(6,2,1,6))
        self.assertEqual(d['timedReads'],2)

    def test_essential_cache_reads_skip_timers(self):
        self.monitor.configure('essential','capture')
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a';p.write_bytes(b'123')
            self.assertEqual(cache.NONE.read(str(p)),b'123')
        d=self.monitor.snapshot()['disk']
        self.assertEqual(d['readBytes'],3)
        self.assertEqual(d['timedReads'],0)

    def test_io_snapshots_from_workers_are_not_double_counted(self):
        with io.Request('https://example.com') as r: r.headers();r.complete(200,30)
        a=self.monitor.snapshot()
        b=dict(a,pid=a['pid']+1)
        view=io.View();state=view.snapshot([a,b],'full')
        self.assertEqual(state['receivedBytes'],60)
        self.assertEqual(state['services'][0]['responses'],2)
        self.assertIsNone(state['rates']['rxBps'])
        state=view.snapshot([a,b],'full')
        self.assertEqual(state['receivedBytes'],60)
        self.assertEqual(state['rates']['rxBps'],0)

    def test_services_history_and_os_sampling_are_bounded(self):
        for i in range(io.MAX_SERVICES+5):
            with io.Request('https://host'+str(i)+'.example') as r: r.complete(200,1)
        self.assertEqual(len(self.monitor.services),io.MAX_SERVICES)
        self.assertEqual(self.monitor.omitted,5)
        with patch.object(io,'process_io',return_value=None) as sample:
            self.monitor.last_sample=0
            self.monitor.snapshot();self.monitor.snapshot()
            self.assertEqual(sample.call_count,1)
        view=io.View()
        for _ in range(140): view.snapshot([],'essential')
        self.assertEqual(len(view.history),120)

    def test_rate_intervals_preserve_monotonic_observation_time(self):
        view=io.View()
        with patch.object(io.time,'perf_counter',return_value=100.0):
            first=view.snapshot([], 'essential')
        with patch.object(io.time,'perf_counter',return_value=101.75):
            second=view.snapshot([], 'essential')
        self.assertEqual(first['observedAt'],100.0)
        self.assertEqual(first['history'][0]['at'],100.0)
        self.assertIsNone(first['history'][0]['intervalS'])
        self.assertEqual(second['history'][-1]['intervalS'],1.75)
        self.assertEqual(second['history'][-1]['at'],second['observedAt'])
        self.assertEqual(first['history'],second['history'][:1])

    def test_real_http_response_header_and_body_measurements(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length','4');self.end_headers()
                time.sleep(0.04);self.wfile.write(b'test')
            def log_message(self,*args): pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(netfetch.HttpsTransport,'_resolve_proxy',return_value=None):
                status,_,body=io.request(netfetch.HttpsTransport(),'http://127.0.0.1:'+str(server.server_port)+'/data',{},2)
            self.assertEqual((status,body),(200,b'test'))
            row=self.view()['services'][0]
            self.assertEqual(row['receivedBytes'],4)
            self.assertGreater(row['requestAvgMs'],row['responseAvgMs']+20)
        finally:
            server.shutdown();server.server_close();thread.join(2)

    def test_live_transfer_bytes_are_not_double_counted_at_completion(self):
        with io.Request('tcp://helper.example:1234') as r:
            io.peer_sent(12);io.peer_received(20);io.peer_headers()
            snapshot=self.view()['services'][0]
            self.assertEqual(snapshot['receivedBytes'],20)
            self.assertEqual(snapshot['inflight'],1)
        row=self.view()['services'][0]
        self.assertEqual((row['receivedBytes'],row['sentBytes'],row['responses']),(20,12,1))

    def test_streaming_urllib_wrapper_counts_progress_and_preserves_result(self):
        response=memoryio.BytesIO(b'abcdefgh');response.status=200
        with patch.object(io.urllib.request,'urlopen',return_value=response):
            with io.urlopen('https://updates.example/file') as r:
                self.assertEqual(r.read(3),b'abc')
                self.assertEqual(self.view()['receivedBytes'],3)
                self.assertEqual(r.read(),b'defgh')
        row=self.view()['services'][0]
        self.assertEqual(row['receivedBytes'],8)
        self.assertEqual(row['responses'],1)

    def test_download_session_records_headers_bytes_and_original_errors(self):
        @contextmanager
        def original(self,url): yield SimpleNamespace(status=200,nbytes=0)
        measured=io.session_request(original)
        with measured(None,'https://data.example/file') as session:
            io.headers_received();io.received_bytes(7);session.nbytes=7
        self.assertEqual(self.view()['receivedBytes'],7)
        with self.assertRaisesRegex(ValueError,'original'):
            with measured(None,'https://data.example/file'):
                raise ValueError('original')
        self.assertEqual(self.view()['services'][0]['failures'],0)

    def test_updater_stream_inside_download_session_is_counted_once(self):
        @contextmanager
        def original(self,url): yield SimpleNamespace(status=200,nbytes=0)
        response=memoryio.BytesIO(b'abcdefgh');response.status=200
        with patch.object(io.urllib.request,'urlopen',return_value=response):
            with io.session_request(original)(None,'https://updates.example/file') as session:
                with io.urlopen('https://updates.example/file') as r:
                    session.nbytes=len(r.read())
        row=self.view()['services'][0]
        self.assertEqual((row['requests'],row['responses'],row['receivedBytes'],row['inflight']),(1,1,8,0))

    def test_dead_worker_telemetry_is_not_a_permanently_active_connection(self):
        ticket=self.monitor.begin('https://data.example')
        source=self.monitor.snapshot();source['observed']-=10
        row=io.View().snapshot([source],'full')['services'][0]
        self.assertEqual(row['inflight'],0)
        self.assertEqual(row['unfinished'],1)
        self.assertEqual(row['health'],'telemetry stale')
        self.monitor.finish(ticket,status=200)


class EssentialExecutionTest(unittest.TestCase):
    def test_queued_capture_cannot_override_new_mode_or_session(self):
        with patch.object(io,'monitor',io.Monitor()):
            io.monitor.configure('essential','new-session')
            control=SimpleNamespace(value=('new-session','essential'))
            config=dict(mode='full',ownerPid=os.getpid(),control=control)
            with patch.object(profiler,'_register',side_effect=AssertionError('expired job must not trace')):
                self.assertEqual(profiler._run(None,'old-session','old-job',lambda: 7,(),{},io_config=config),7)
            self.assertEqual((io.monitor.session,io.monitor.mode),('new-session','essential'))
            config['mode']='essential'
            profiler._essential_run(None,'old-session',config,lambda: 8,(),{})
            self.assertEqual((io.monitor.session,io.monitor.mode),('new-session','essential'))

    def test_essential_wraps_only_io_context_not_worker_or_stage_timers(self):
        c=profiler.Collector()
        try:
            with patch.object(profiler,'collector',c):
                c.configure(True,'essential')
                with (patch.object(profiler,'_register',side_effect=AssertionError('no heartbeat')),
                      patch.object(profiler.time,'thread_time',side_effect=AssertionError('no CPU timer'))):
                    with profiler.ThreadPoolExecutor(1) as p:
                        fn=profiler.step('disabled stage')(lambda: 7)
                        self.assertEqual(p.submit(fn).result(),7)
                state=c.snapshot()
                self.assertEqual(state['mode'],'essential')
                self.assertEqual(state['recent'],[])
                self.assertEqual(state['active'],[])
        finally: c.close()


if __name__=='__main__': unittest.main()
