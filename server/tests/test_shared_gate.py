"""netfetch.SharedGate: a shared host (the one-at-a-time export services: md6in, NAIP, the regional exports) admitted
across the server's processes, not each process alone (2026-11-13 review of the roofs' photo fetches: four road-tile
processes each had md6in's whole 1 request/s, and none saw another's Retry-After)."""
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netfetch as nf  # noqa: E402

HOST = "mdgeodata.md.gov"
URL = f"https://{HOST}/imagery/rest/services/SixInch/MapServer/export?bbox=1,2,3,4&size=1024,1024&f=image"


def policy(rate, burst=1.0, max_conc=1, **kw):
    return replace(nf.POLICIES[HOST], rate=rate, burst=burst, max_conc=max_conc, max_wait=30.0, max_attempts=1, **kw)


class _Transport:
    """Each request's (start, end) monotonic times; answers 200 after hold seconds (or status with retry_after)."""

    def __init__(self, hold, status=200, retry_after=None):
        self.hold, self.status, self.retry_after = hold, status, retry_after
        self.times = []

    def request(self, url, headers, timeout):
        t0 = time.monotonic()
        time.sleep(self.hold)
        self.times.append((t0, time.monotonic()))
        hd = {"Content-Type": "image/jpeg"}
        if self.retry_after is not None:
            hd["Retry-After"] = str(self.retry_after)
        return self.status, hd, b"\xff\xd8" + b"x" * 64


def _worker(d, rate, n, hold, out):
    """One server process: its own Fetcher (as a road-tile process's roof reader has), n requests to the host."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import netfetch
    netfetch.SHARED_DIR = d
    t = _Transport(hold)
    f = netfetch.Fetcher(transport=t, policies={HOST: policy(rate)})
    for _ in range(n):
        f.get(URL, kind="demand")
    out.put(t.times)


class SharedGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = nf.SHARED_DIR
        nf._GATES.clear()

    def tearDown(self):
        nf.SHARED_DIR = self.old
        nf._GATES.clear()
        self.tmp.cleanup()

    def test_four_processes_share_one_host_rate_and_slot(self):
        rate, n, procs, hold = 10.0, 5, 4, 0.03
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        ps = [ctx.Process(target=_worker, args=(self.tmp.name, rate, n, hold, q)) for _ in range(procs)]
        for p in ps:
            p.start()
        times = sorted(t for _ in ps for t in q.get(timeout=120))
        for p in ps:
            p.join(30)
        self.assertEqual(len(times), n * procs)
        starts = [s for s, _e in times]
        # (the rate together: burst 1, so n * procs requests take at least (n * procs - 1) / rate)
        span = starts[-1] - starts[0]
        self.assertGreaterEqual(span, (n * procs - 1) / rate * 0.9, f"{n * procs} requests in {span:.2f} s")
        # (one at a time together: max_conc 1)
        for (s0, e0), (s1, _e1) in zip(times, times[1:]):
            self.assertGreaterEqual(s1, e0 - 1e-3, "two processes' requests in flight at once")
        # (and without the gate each process had the whole rate: four times as fast)
        nf.SHARED_DIR = None
        t = _Transport(0.0)
        f = nf.Fetcher(transport=t, policies={HOST: policy(rate, burst=1.0)})
        self.assertIsNone(nf.shared_gate(HOST, f.policy(HOST)))

    def test_another_process_s_retry_after_holds_this_one_off(self):
        nf.SHARED_DIR = self.tmp.name
        a = nf.Fetcher(transport=_Transport(0.0, status=429, retry_after=60), policies={HOST: policy(100.0, burst=10)})
        with self.assertRaises(nf.RateLimited):
            a.get(URL, kind="demand")
        nf._GATES.clear()           # (another process: a gate object of its own on the same folder)
        tb = _Transport(0.0)
        b = nf.Fetcher(transport=tb, policies={HOST: policy(100.0, burst=10)})
        with self.assertRaises(nf.HostUnavailable):
            b.get(URL, kind="demand")
        self.assertEqual(tb.times, [], "not asked while another process was told to wait")

    def test_a_breaker_opened_elsewhere_and_prefetch_when_busy(self):
        nf.SHARED_DIR = self.tmp.name
        g = nf.shared_gate(HOST, policy(100.0, burst=10))
        g.block(30)
        self.assertGreater(g.blocked(), 25)
        nf._GATES.clear()
        g2 = nf.shared_gate(HOST, policy(100.0, burst=10))
        with self.assertRaises(nf.HostUnavailable):
            g2.acquire("demand", 1.0, URL, HOST)
        g3 = nf.SharedGate(os.path.join(self.tmp.name, "other"), HOST, policy(100.0, burst=10))
        slot, _w = g3.acquire("demand", 1.0, URL, HOST)     # (the one slot held: a request in flight)
        try:
            with self.assertRaises(nf.Deferred):
                g3.acquire("prefetch", 1.0, URL, HOST)
            t0 = time.monotonic()
            with self.assertRaises(nf.RateLimited):
                g3.acquire("demand", 0.2, URL, HOST)
            self.assertGreaterEqual(time.monotonic() - t0, 0.15)
        finally:
            nf.SharedGate.release(slot)
        nf.SharedGate.release(g3.acquire("prefetch", 1.0, URL, HOST)[0])     # (free again)

    def test_a_gate_that_fails_never_keeps_this_process_s_slot(self):
        # (review of 172d8032: the local slot was given back only for a FetchError; an OSError from the gate's files,
        # a full disk, kept it, and with max_conc 1 every later request to the host waited forever)
        from unittest import mock
        nf.SHARED_DIR = self.tmp.name
        t = _Transport(0.0)
        f = nf.Fetcher(transport=t, policies={HOST: replace(policy(100.0, burst=10), max_wait=0.5)})
        with mock.patch.object(nf.SharedGate, "_state", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(f.get(URL, kind="demand").status, 200, "a full disk: this process's limits alone")
        with mock.patch.object(nf.SharedGate, "_slot", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                f.get(URL, kind="demand")
        self.assertEqual(f.get(URL, kind="demand").status, 200, "the slot given back")
        self.assertEqual(f.get(URL, kind="demand").status, 200)
        self.assertEqual(len(t.times), 3)

    def test_the_gate_s_wait_is_counted_and_logged(self):
        from unittest import mock
        nf.SHARED_DIR = self.tmp.name
        log = os.path.join(self.tmp.name, "gate.log")
        # (two fetchers, as two processes: each one's own bucket has room, the shared one makes them wait)
        fs = [nf.Fetcher(transport=_Transport(0.0), policies={HOST: policy(5.0, burst=1.0)}) for _ in range(2)]
        with mock.patch.object(nf, "GATE_LOG", log):
            for f in fs * 2:
                f.get(URL, kind="demand")
        lines = open(log).read().splitlines()
        self.assertGreaterEqual(len(lines), 3, "all but the first waited for the shared token")
        self.assertTrue(all(float(ln.split()[-1]) > 0.1 for ln in lines))
        self.assertGreaterEqual(sum(f.host(HOST).total["wait_ms"] for f in fs), 450, "counted as our own limits' wait")

    def test_only_shared_hosts_and_only_with_a_folder(self):
        nf.SHARED_DIR = self.tmp.name
        self.assertIsNone(nf.shared_gate("basemap.nationalmap.gov", nf.POLICIES["basemap.nationalmap.gov"]))
        self.assertIsNotNone(nf.shared_gate(HOST, nf.POLICIES[HOST]))
        self.assertTrue(nf.POLICIES["apps.geo.fpac.usda.gov"].shared)
        self.assertTrue(nf.POLICIES["imagery.nationalmap.gov"].shared)
        regional = [p for p in nf.POLICIES.values() if p.optional and p.max_conc == 1 and p.binary and p.rate == 1.0]
        self.assertTrue(regional and all(p.shared for p in regional), "the regional exports")


if __name__ == "__main__":
    unittest.main()
