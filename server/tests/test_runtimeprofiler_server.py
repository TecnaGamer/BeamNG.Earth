"""Real protocol handshake and telemetry publication with an actual encoder job."""
import asyncio
from pathlib import Path
import sys
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import cdae
import runtimeprofiler
from test_server import ServerHarness


class LiveProfilerTest(ServerHarness):
    def test_handshake_job_steps_and_unsubscribe(self):
        self.addCleanup(runtimeprofiler.collector.close)
        self.c.send({"type": "hello", "protocol": 4, "format": 3, "modVersion": "profiler-test"})
        hello, _ = self.c.until(lambda m: m["type"] == "hello", timeout=10)
        self.assertEqual(hello["runtimeProfiler"], 2)
        publisher = asyncio.run_coroutine_threadsafe(runtimeprofiler.stream(self.server), self.loop)
        try:
            self.c.send({"type": "runtime_profiler", "enabled": True})
            state, _ = self.c.until(lambda m: m["type"] == "runtime_profiler", timeout=10)
            self.assertTrue(state["profile"]["enabled"])
            io=state['profile']['io']
            self.assertEqual(io['history'][-1]['at'],io['observedAt'])
            self.assertIn('intervalS',io['history'][-1])
            points, tris = [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]]
            self.c.send({"type": "runtime_profiler", "enabled": True})
            got = self.server.hm_pool.submit(cdae.encode, points, tris).result(timeout=10)
            self.assertEqual(got, cdae.encode(points, tris))
            state, _ = self.c.until(lambda m: m["type"] == "runtime_profiler" and
                                   any(j.get("name") == "encode" for j in m["profile"].get("recent", [])), timeout=10)
            job = next(j for j in state["profile"]["recent"] if j["name"] == "encode")
            self.assertEqual(job["pool"], "heightmap")
            self.assertEqual(job["outcome"], "ok")
            self.assertIn("Native shape encoding", [s["name"] for s in job["steps"]])
            self.assertGreaterEqual(job["queueMs"], 0)
            self.assertGreater(job["runMs"], 0)
            self.c.send({"type": "runtime_profiler", "enabled": False})
            until = time.monotonic() + 5
            while runtimeprofiler.collector.enabled and time.monotonic() < until:
                time.sleep(0.01)
            self.assertFalse(runtimeprofiler.collector.enabled)
        finally:
            publisher.cancel()

    def test_essential_service_feed_and_immediate_application_rtt_reply(self):
        self.addCleanup(runtimeprofiler.collector.close)
        self.c.send({'type':'hello','protocol':4,'format':3,'modVersion':'profiler-test'})
        self.c.until(lambda m:m['type']=='hello',timeout=10)
        publisher=asyncio.run_coroutine_threadsafe(runtimeprofiler.stream(self.server),self.loop)
        try:
            self.c.send({'type':'runtime_profiler','enabled':True,'mode':'essential','probe':123})
            pong,_=self.c.until(lambda m:m['type']=='runtime_profiler_pong',timeout=5)
            self.assertEqual(pong['probe'],123)
            state,_=self.c.until(lambda m:m['type']=='runtime_profiler',timeout=10)
            self.assertEqual(state['profile']['mode'],'essential')
            self.assertEqual(state['profile']['recent'],[])
            self.assertIn('services',state['profile']['io'])
            self.assertEqual(state['profile']['io']['processes'],[])
            io=state['profile']['io']
            self.assertEqual(io['history'][-1]['at'],io['observedAt'])
            self.assertIn('intervalS',io['history'][-1])
        finally: publisher.cancel()
