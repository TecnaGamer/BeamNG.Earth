"""server/netfetch.py (docs/research/2026-10-10-rate-limits.md §5.4): the token bucket, the per-host
concurrency cap, backoff with full jitter, Retry-After, the circuit breaker and the failover of the
DEM chain, response classes and .404 marker TTLs, host policies, daily budgets (persisted per UTC
day), the User-Agent, --offline and the status note earthd sends. No network: a FakeTransport
answers from a script, a FakeClock / FakeSleep pair stands in for time, the rng is seeded."""
import email.utils
import getpass
import io
import json
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dem as demmod  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import gravity  # noqa: E402
import netfetch as nf  # noqa: E402
import test_server  # noqa: E402

USGS = "basemap.nationalmap.gov"
EOX = "tiles.maps.eox.at"
MT = "tiles.mapterhorn.com"
T0 = 1791700000.0           # the fake wall clock's start (2026-10-11 UTC)


def url(host=USGS, n=0):
    return f"https://{host}/tile/10/{n}/{n}"


class FakeClock:
    """Monotonic and wall clock in one (thread-safe); advance() moves both."""

    def __init__(self, now=T0):
        self.now = now
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, s):
        with self.lock:
            self.now += s


class FakeSleep:
    """time.sleep on the fake clock: advances it and records every sleep."""

    def __init__(self, clock):
        self.clock, self.sleeps = clock, []

    def __call__(self, s):
        self.sleeps.append(s)
        self.clock.advance(max(0.0, s))


class FakeTransport:
    """A script per URL pattern: a list of (status, headers, body, latency s) answered in order (the
    last one repeats); an exception instance in the list is raised. Logs (time, url, headers)."""

    def __init__(self, clock=None, script=None, real_latency=False):
        self.clock, self.real_latency = clock, real_latency
        self.script = dict(script or {})
        self.calls = []
        self.inflight = self.max_inflight = 0
        self.lock = threading.Lock()

    def request(self, url, headers, timeout):
        with self.lock:
            self.calls.append((self.clock() if self.clock else time.monotonic(), url, dict(headers)))
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            pat = next((p for p in self.script if re.search(p, url)), None)
            steps = self.script.get(pat, [(200, {}, b"data", 0.0)])
            step = steps.pop(0) if len(steps) > 1 else steps[0]
        try:
            if isinstance(step, BaseException):
                raise step
            status, hd, body, lat = step
            if lat:
                time.sleep(lat) if self.real_latency else self.clock.advance(lat)
            return status, hd, body
        finally:
            with self.lock:
                self.inflight -= 1


def fetcher(script=None, policies=None, **kw):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    tr = FakeTransport(clock, script)
    f = nf.Fetcher(clock=clock, sleep=sleep, rng=random.Random(7), transport=tr, wall=clock,
                   policies=policies, **kw)
    return f, clock, sleep, tr


class TokenBucketTest(unittest.TestCase):
    def test_burst_then_rate(self):
        """N demand requests at t=0: `burst` less URGENT_TOKENS go at once, the rest 1/rate apart (the bucket keeps
        URGENT_TOKENS for urgent requests)."""
        f, clock, sleep, tr = fetcher()
        p = nf.POLICIES[USGS]
        b = int(p.burst - nf.URGENT_TOKENS)
        n = b + 10
        for i in range(n):
            f.get(url(USGS, i))
        t = [c[0] - T0 for c in tr.calls]
        self.assertEqual(t[:b], [0.0] * b)
        for k, ti in enumerate(t[b:], 1):
            self.assertAlmostEqual(ti, k / p.rate, places=4)

    def test_urgent_goes_first(self):
        """With demand holding the bucket at its floor, an urgent request still gets a token at once (GitHub
        #116: the player's block behind the far field's requests)."""
        f, clock, sleep, tr = fetcher()
        p = nf.POLICIES[USGS]
        for i in range(int(p.burst - nf.URGENT_TOKENS)):
            f.get(url(USGS, i))
        t0 = clock()
        f.get(url(USGS, 999), kind="urgent")
        self.assertEqual(tr.calls[-1][0], t0, "the urgent request did not wait")
        self.assertTrue(f.allow(USGS, "urgent"))

    def test_prefetch_only_above_half(self):
        f, clock, sleep, tr = fetcher()
        p = nf.POLICIES[USGS]
        for i in range(int(p.burst / 2) - 1):          # 31 left of 60
            f.get(url(USGS, i))
        f.get(url(USGS, 100), kind="prefetch")          # 31 > 30: allowed (30 left)
        with self.assertRaises(nf.Deferred):
            f.get(url(USGS, 101), kind="prefetch")      # 30: not above half
        self.assertFalse(f.allow(USGS, "prefetch"))
        with self.assertRaises(nf.Deferred):            # background keeps even more for demand
            f.get(url(USGS, 102), kind="background")
        n = len(tr.calls)
        f.get(url(USGS, 103))                            # demand still goes
        self.assertEqual(len(tr.calls), n + 1)
        self.assertEqual(sleep.sleeps, [])
        clock.advance(10.0)                              # refilled
        self.assertTrue(f.allow(USGS, "prefetch"))
        f.get(url(USGS, 104), kind="background")

    def test_take_timeout(self):
        clock = FakeClock()
        b = nf.TokenBucket(2.0, 1.0, clock, FakeSleep(clock))
        self.assertTrue(b.take(0))
        self.assertFalse(b.take(0.4))                   # the next token is 0.5 s away
        self.assertTrue(b.take(0.6))
        self.assertAlmostEqual(clock() - T0, 0.5)

    def test_demand_waiting_too_long_is_rate_limited(self):
        pol = nf.with_policy(nf.POLICIES, USGS, rate=0.01, burst=1.0, max_wait=5.0)
        f, clock, sleep, tr = fetcher(policies=pol)
        f.get(url())
        with self.assertRaises(nf.RateLimited) as cm:
            f.get(url(USGS, 1))
        self.assertGreater(cm.exception.retry_after, 5.0)
        self.assertEqual(len(tr.calls), 1)


    def test_a_pause_while_waiting_gives_up(self):
        """A demand request waiting for a token when another worker's request meets a Retry-After does
        not sleep out the pause and then fire (the breaker would allow one probe, not every waiter):
        it gives up at once, RateLimited, and the caller fails over."""
        f, clock, sleep, tr = fetcher({f"{USGS}/tile/10/999/": [(429, {"Retry-After": "20"}, b"", 0)]})
        p = nf.POLICIES[USGS]
        for i in range(int(p.burst)):                   # the bucket is empty: the next one waits
            f.get(url(USGS, i))
        h = f.host(USGS)
        hit = []

        def other_worker_meets_429(s):
            if not hit:
                hit.append(s)
                with self.assertRaises(nf.RateLimited):
                    f.get(url(USGS, 999))
            sleep(s)
        h.bucket.sleep = other_worker_meets_429
        n = len(tr.calls)
        t0 = clock()
        with self.assertRaises(nf.RateLimited):
            f.get(url(USGS, 1000))
        self.assertEqual(len(tr.calls), n + 1)          # the 429 alone
        self.assertLess(clock() - t0, 1.0)


class ColdStartTest(unittest.TestCase):
    # The first selection's requests on a cold cache (session start or teleport), from
    # test_demand.NetDemandTest (rate-limit report §2.2): Baltimore (USGS first) and Munich (EOX
    # first); AWS is the no-lidar worst case. Before netfetch only the pools bounded them: ~32 req/s
    # to USGS (6 workers / 0.19 s), up to ~80 req/s to Mapterhorn (8 workers / 0.1 s).
    BURSTS = {USGS: 1431, EOX: 1185, MT: 237, "s3.amazonaws.com": 300}

    def test_bursts_are_bounded(self):
        """Every cold-start burst goes out at most `burst` at once, then at the policy rate: no 1 s
        window holds more than burst + rate requests, no minute more than burst + 60 x rate."""
        for host, n in self.BURSTS.items():
            with self.subTest(host=host):
                f, clock, sleep, tr = fetcher()
                p = nf.POLICIES[host]
                for i in range(n):
                    f.get(f"https://{host}/tile/16/{i}/0")
                t = [c[0] - T0 for c in tr.calls]
                peak1 = max(sum(1 for u in t[i:] if u < ti + 1.0) for i, ti in enumerate(t))
                first_min = sum(1 for u in t if u < 60.0)
                print(f"\n  {host}: {n} requests in {t[-1]:.1f} s, peak {peak1} in 1 s, {first_min} in the "
                      f"first minute, then {p.rate:g}/s", file=sys.stderr)
                self.assertLessEqual(peak1, p.burst + p.rate + 1)
                self.assertLessEqual(first_min, p.burst + 60 * p.rate + 1)
                self.assertAlmostEqual(t[-1], max(0.0, (n - p.burst + nf.URGENT_TOKENS) / p.rate), delta=1e-3)

    def test_a_pool_cannot_outrun_the_bucket(self):
        """8 threads (the DEM pool) with zero latency, real clock: the bucket still sets the pace."""
        pol = nf.with_policy(nf.POLICIES, MT, rate=200.0, burst=20.0, max_conc=8)
        tr = FakeTransport()
        f = nf.Fetcher(transport=tr, policies=pol)
        urls = [url(MT, i) for i in range(200)]
        t0 = time.monotonic()
        threads = [threading.Thread(target=lambda k=k: [f.get(u) for u in urls[k::8]]) for k in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        self.assertEqual(len(tr.calls), 200)
        self.assertGreaterEqual(time.monotonic() - t0, (200 - 20) / 200.0 - 0.05)
        self.assertLessEqual(tr.max_inflight, 8)


class ConcurrencyTest(unittest.TestCase):
    def test_cap(self):
        """16 threads against a host with max_conc=2: never more than 2 in flight."""
        pol = nf.with_policy(nf.POLICIES, EOX, max_conc=2, rate=1e6, burst=1e6)
        tr = FakeTransport(script={EOX: [(200, {}, b"x", 0.02)]}, real_latency=True)
        f = nf.Fetcher(transport=tr, policies=pol)
        threads = [threading.Thread(target=f.get, args=(url(EOX, i),)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(tr.calls), 16)
        self.assertEqual(tr.max_inflight, 2)

    def test_prefetch_never_waits_for_a_slot(self):
        pol = nf.with_policy(nf.POLICIES, EOX, max_conc=1)
        f, clock, sleep, tr = fetcher(policies=pol)
        h = f.host(EOX)
        h.sem.acquire()                     # a demand request in flight
        with self.assertRaises(nf.Deferred):
            f.get(url(EOX), kind="prefetch")
        h.sem.release()
        f.get(url(EOX), kind="prefetch")


class TransportTest(unittest.TestCase):
    """HttpsTransport over a fake connection class (no sockets): a failed request is sent again on a
    fresh connection only when it went out on a reused keep-alive one (the server may have closed it
    while idle); a fresh connection's failure is the attempt's result, so a timeout costs one timeout
    and the retry goes through the limiter and its backoff."""

    def transport(self, errors):
        made = []

        class Resp:
            status = 200

            def getheaders(self):
                return [("Content-Type", "image/jpeg")]

            def read(self):
                return b"ok"

        class Conn:
            def __init__(self, netloc, timeout=None):
                made.append(self)
                self.sent = 0

            def request(self, method, path, headers=None):
                self.sent += 1
                e = errors.pop(0) if errors else None
                if e is not None:
                    raise e

            def getresponse(self):
                return Resp()

            def close(self):
                pass
        patch = mock.patch.object(nf.http.client, "HTTPSConnection", Conn)
        patch.start()
        self.addCleanup(patch.stop)
        return nf.HttpsTransport(), made

    def test_fresh_connection_failure_is_not_repeated(self):
        for err in (TimeoutError("timed out"), ConnectionRefusedError("refused"), OSError("no route")):
            with self.subTest(err=type(err).__name__):
                tr, made = self.transport([err])
                with self.assertRaises(OSError):
                    tr.request(url(), {}, 20.0)
                self.assertEqual(sum(c.sent for c in made), 1)

    def test_closed_keep_alive_connection_is_reopened_once(self):
        tr, made = self.transport([None, nf.http.client.RemoteDisconnected("closed")])
        self.assertEqual(tr.request(url(), {}, 20.0)[0], 200)
        self.assertEqual(tr.request(url(), {}, 20.0)[0], 200)      # reused, closed by the server: reopened
        self.assertEqual((len(made), [c.sent for c in made]), (2, [2, 1]))


class RetryTest(unittest.TestCase):
    def test_backoff_full_jitter(self):
        """503, 503, 200: two sleeps, each within [0, base * 2^k] and exactly the seeded draws."""
        f, clock, sleep, tr = fetcher({USGS: [(503, {}, b"", 0), (503, {}, b"", 0), (200, {}, b"ok", 0)]})
        r = f.get(url())
        self.assertEqual((r.status, r.body), (200, b"ok"))
        self.assertEqual(len(tr.calls), 3)
        p = nf.POLICIES[USGS]
        self.assertEqual(len(sleep.sleeps), 2)
        rng = random.Random(7)
        for k, s in enumerate(sleep.sleeps):
            self.assertTrue(0 <= s <= p.base_delay * 2 ** k)
            self.assertEqual(s, rng.uniform(0.0, min(p.max_delay, p.base_delay * 2 ** k)))
        self.assertEqual(f.snapshot()[USGS]["retries"], 2)

    def test_attempts_bounded(self):
        f, clock, sleep, tr = fetcher({USGS: [(500, {}, b"", 0)]})
        with self.assertRaises(nf.FetchFailed) as cm:
            f.get(url())
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(len(tr.calls), nf.POLICIES[USGS].max_attempts)

    def test_network_errors_retried(self):
        f, clock, sleep, tr = fetcher({USGS: [ConnectionResetError("reset"), (200, {}, b"ok", 0)]})
        self.assertEqual(f.get(url()).body, b"ok")
        self.assertEqual(f.snapshot()[USGS]["errors"], 1)


class RetryAfterTest(unittest.TestCase):
    def test_pauses_the_host(self):
        """429 with Retry-After: 7: no retry, and no request to the host (from any thread) before t+7."""
        f, clock, sleep, tr = fetcher({USGS: [(429, {"Retry-After": "7"}, b"", 0), (200, {}, b"ok", 0)]})
        with self.assertRaises(nf.RateLimited) as cm:
            f.get(url())
        self.assertEqual(cm.exception.retry_after, 7.0)
        self.assertEqual((len(tr.calls), sleep.sleeps), (1, []))
        clock.advance(6.9)
        out = []

        def other():
            try:
                f.get(url(USGS, 1))
            except nf.FetchError as e:
                out.append(e)
        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        self.assertIsInstance(out[0], nf.HostUnavailable)
        self.assertEqual(len(tr.calls), 1)
        self.assertEqual(f.host(USGS).bucket.level(), 0.0)      # emptied, nothing refilled yet
        clock.advance(0.1)
        self.assertEqual(f.get(url(USGS, 2)).body, b"ok")      # the probe at t+7
        self.assertGreaterEqual(tr.calls[1][0] - tr.calls[0][0], 7.0)
        self.assertEqual(f.unavailable_hosts(), [])

    def test_503_retry_after(self):
        f, clock, sleep, tr = fetcher({MT: [(503, {"retry-after": "20"}, b"", 0)]})
        with self.assertRaises(nf.RateLimited):
            f.get(url(MT))
        self.assertEqual(f.unavailable_hosts(), [MT])

    def test_a_lane_has_its_own_breaker_under_the_hosts_policy(self):
        """GitHub #350: the status bar's town lookups timed out, opened photon.komoot.io's breaker and the map's
        search said 'unavailable'. A lane is the host's policy with its own bucket and breaker."""
        P = "photon.komoot.io"
        f, clock, sleep, tr = fetcher()
        f.host(P + "#reverse").breaker.trip(60)
        with self.assertRaises(nf.HostUnavailable):
            f.get(f"https://{P}/reverse?lat=1&lon=2", lane="reverse")
        self.assertEqual(f.get(f"https://{P}/api/?q=x").body, b"data")          # search: still open
        self.assertIs(f.host(P + "#reverse").policy, f.host(P).policy)

    def test_optional_hosts_left_out_of_the_status(self):
        """GitHub #23: SourceForge (gravity only) blocked in the player's country showed "Degraded" for good."""
        f, clock, sleep, tr = fetcher()
        for h in (MT, "downloads.sourceforge.net", "api.open-meteo.com"):
            f.host(h).breaker.trip(60)
        self.assertEqual(f.unavailable_hosts(), sorted([MT, "downloads.sourceforge.net", "api.open-meteo.com"]))
        self.assertEqual(f.unavailable_hosts(optional=False), [MT])

    def test_regional_imagery_left_out_of_the_status(self):
        """Lower Saxony's WMS down for maintenance (2026-10-21): the banner named it while Sentinel-2 drew every tile."""
        f, clock, sleep, tr = fetcher()
        for h in (MT, "opendata.lgln.niedersachsen.de"):
            f.host(h).breaker.trip(60)
        self.assertEqual(f.unavailable_hosts(optional=False), [MT])

    def test_parse(self):
        self.assertEqual(nf.parse_retry_after("7"), 7.0)
        self.assertEqual(nf.parse_retry_after("0"), nf.RETRY_AFTER_MIN)
        self.assertEqual(nf.parse_retry_after("86400"), nf.RETRY_AFTER_MAX)         # clamped to 15 min
        date = email.utils.formatdate(T0 + 20, usegmt=True)                         # an HTTP date
        self.assertAlmostEqual(nf.parse_retry_after(date, now_wall=T0), 20.0, places=3)
        self.assertEqual(nf.parse_retry_after(email.utils.formatdate(T0 + 4 * 3600, usegmt=True), now_wall=T0),
                         nf.RETRY_AFTER_MAX)
        self.assertIsNone(nf.parse_retry_after("soon"))
        self.assertIsNone(nf.parse_retry_after(None))

    def test_429_without_header_backs_off(self):
        f, clock, sleep, tr = fetcher({USGS: [(429, {}, b"", 0)]})
        with self.assertRaises(nf.RateLimited):
            f.get(url())
        self.assertEqual(len(tr.calls), nf.POLICIES[USGS].max_attempts)
        self.assertEqual(len(sleep.sleeps), nf.POLICIES[USGS].max_attempts - 1)


class BreakerTest(unittest.TestCase):
    def test_open_probe_double_close(self):
        pol = nf.with_policy(nf.POLICIES, USGS, max_attempts=1)
        f, clock, sleep, tr = fetcher({USGS: [(502, {}, b"", 0)]}, policies=pol)
        p = pol[USGS]
        for i in range(p.breaker_threshold):
            with self.assertRaises(nf.FetchFailed):
                f.get(url(USGS, i))
        n = len(tr.calls)
        with self.assertRaises(nf.HostUnavailable) as cm:
            f.get(url(USGS, 99))
        self.assertEqual(len(tr.calls), n)                      # no network while open
        self.assertAlmostEqual(cm.exception.retry_after, p.breaker_open)
        clock.advance(p.breaker_open)
        with self.assertRaises(nf.FetchFailed):                 # the probe fails ...
            f.get(url(USGS, 100))
        self.assertEqual(len(tr.calls), n + 1)
        with self.assertRaises(nf.HostUnavailable) as cm:      # ... open for twice as long
            f.get(url(USGS, 101))
        self.assertAlmostEqual(cm.exception.retry_after, 2 * p.breaker_open)
        clock.advance(2 * p.breaker_open)
        tr.script[USGS] = [(200, {}, b"ok", 0)]
        self.assertEqual(f.get(url(USGS, 102)).body, b"ok")   # the probe succeeds: closed
        self.assertEqual(f.host(USGS).breaker.state, "closed")
        self.assertEqual(f.host(USGS).breaker.open_s, p.breaker_open)

    def test_one_probe_at_a_time(self):
        clock = FakeClock()
        b = nf.CircuitBreaker(2, 30.0, 10.0, 900.0, clock)
        b.failure()
        self.assertTrue(b.failure())
        clock.advance(10.0)
        self.assertEqual(b.allow()[:2], (True, True))
        self.assertFalse(b.allow()[0])                          # a probe is out
        b.release_probe()                                       # it was dropped before the network
        self.assertEqual(b.allow()[:2], (True, True))

    def test_a_late_success_does_not_close_an_open_breaker(self):
        """A request sent before the host said Retry-After answers 200 afterwards: the breaker stays
        open (only its probe closes it), so demand keeps failing over at once instead of sleeping out
        the pause on the emptied bucket."""
        gate, sent = threading.Event(), threading.Event()
        f, clock, sleep, tr = fetcher({f"{USGS}/tile/10/1/": [(429, {"Retry-After": "20"}, b"", 0)]})
        request = tr.request

        def gated(u, headers, timeout):
            if u == url(USGS, 0):
                sent.set()
                gate.wait(5)
            return request(u, headers, timeout)
        tr.request = gated
        out = []
        th = threading.Thread(target=lambda: out.append(f.get(url(USGS, 0))))
        th.start()
        self.assertTrue(sent.wait(5))
        with self.assertRaises(nf.RateLimited):
            f.get(url(USGS, 1))
        gate.set()
        th.join(5)
        self.assertEqual(out[0].body, b"data")                 # the late answer is still delivered
        self.assertEqual(f.unavailable_hosts(), [USGS])
        n = len(tr.calls)
        with self.assertRaises(nf.HostUnavailable):
            f.get(url(USGS, 2))
        self.assertEqual((len(tr.calls), sleep.sleeps), (n, []))

    def test_failures_outside_the_window_do_not_add_up(self):
        clock = FakeClock()
        b = nf.CircuitBreaker(3, 30.0, 10.0, 900.0, clock)
        for _ in range(5):
            self.assertFalse(b.failure())
            self.assertFalse(b.failure())
            clock.advance(31.0)
        self.assertEqual(b.state, "closed")


def terrarium_png(elev, size):
    v = np.full((size, size), elev + 32768.0)
    r = np.floor(v / 256)
    g = np.floor(v - r * 256)
    b = np.round((v - r * 256 - g) * 256)
    a = np.stack([r, g, b], -1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(a, "RGB").save(buf, "PNG")
    return buf.getvalue()


class DemFailoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def dem(self, script):
        f, clock, sleep, tr = fetcher(script)
        return demmod.Dem(self.tmp.name, fetch_workers=2, fetcher=f), f, clock, tr

    def test_open_mapterhorn_uses_aws(self):
        d, f, clock, tr = self.dem({"amazonaws": [(200, {}, terrarium_png(123.0, 256), 0)]})
        f.host(MT).breaker.trip(60.0)
        a = d.tiles([(13, 1000, 2000)])[(13, 1000, 2000)]
        self.assertTrue(np.allclose(a, 123.0))
        self.assertTrue(tr.calls and all("amazonaws" in c[1] for c in tr.calls))
        self.assertEqual(len(tr.calls), 4)                      # AWS z14 for the z13 key
        self.assertEqual(d.stats_snapshot()["mapterhorn:failover"], 1)

    def test_failing_mapterhorn_fails_over(self):
        d, f, clock, tr = self.dem({MT: [(503, {}, b"", 0)], "amazonaws": [(200, {}, terrarium_png(-5.0, 256), 0)]})
        a = d.tiles([(10, 1, 2)])[(10, 1, 2)]
        self.assertTrue(np.allclose(a, -5.0))                   # AWS as it is (no sea clamp on failover)

    def test_both_down(self):
        d, f, clock, tr = self.dem({MT: [OSError("down")], "amazonaws": [OSError("down")]})
        with self.assertRaises(demmod.DemUnavailable):
            d.tiles([(10, 1, 2)])

    def test_404_marker_ttl_and_400(self):
        mt = terrarium_png(50.0, 512)
        d, f, clock, tr = self.dem({MT: [(404, {}, b"", 0)], "amazonaws": [(200, {}, terrarium_png(7.0, 256), 0)]})
        d._download(demmod.MAPTERHORN, 10, 1, 2)
        miss = os.path.join(self.tmp.name, "mapterhorn", "10", "1", "2.404")
        self.assertTrue(os.path.exists(miss))
        n = len(tr.calls)
        self.assertIsNone(d._download(demmod.MAPTERHORN, 10, 1, 2))     # remembered
        self.assertEqual(len(tr.calls), n)
        old = clock() - nf.POLICIES[MT].miss_ttl - 60      # the fetcher's wall clock (T0), not the real one
        os.utime(miss, (old, old))                              # 90 days old (or an old-style marker)
        tr.script[MT] = [(200, {}, mt, 0)]
        self.assertEqual(d._download(demmod.MAPTERHORN, 10, 1, 2), mt)  # asked again: Mapterhorn has lidar now
        self.assertFalse(os.path.exists(miss))
        # AWS markers never expire
        aws_miss = os.path.join(self.tmp.name, "aws-terrarium", "11", "0", "0.404")
        os.makedirs(os.path.dirname(aws_miss))
        open(aws_miss, "wb").close()
        os.utime(aws_miss, (0, 0))
        self.assertIsNone(d._download(demmod.AWS, 11, 0, 0))
        # 400: an error, no marker
        tr.script[MT] = [(400, {}, b"", 0)]
        with self.assertRaises(nf.FetchFailed):
            d._download(demmod.MAPTERHORN, 10, 5, 5)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "mapterhorn", "10", "5", "5.404")))

    def test_undecodable_tile_fails_over_and_is_not_kept(self):
        """A 200 whose body is no image (empty, or cut short where the server sent no length): the tile
        comes from AWS and the bad file leaves the cache (kept, it would fail every later session)."""
        bad = terrarium_png(50.0, 512)[:120]
        d, f, clock, tr = self.dem({MT: [(200, {}, bad, 0)], "amazonaws": [(200, {}, terrarium_png(7.0, 256), 0)]})
        a = d.tiles([(10, 1, 2)])[(10, 1, 2)]
        self.assertTrue(np.allclose(a, 7.0))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "mapterhorn", "10", "1", "2.webp")))
        tr.script["amazonaws"] = [(200, {}, b"", 0)]
        with self.assertRaises(demmod.DemUnavailable):          # AWS too: no exception from PIL
            d.tiles([(10, 3, 3)])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "aws-terrarium", "11", "6", "6.png")))
        self.assertEqual(d.stats_snapshot()["aws-terrarium:undecodable"], 1)

    def test_disk_hits_offline(self):
        f, clock, sleep, tr = fetcher(offline=True)
        p = os.path.join(self.tmp.name, "mapterhorn", "10", "1", "2.webp")
        os.makedirs(os.path.dirname(p))
        buf = io.BytesIO()
        Image.open(io.BytesIO(terrarium_png(77.0, 512))).save(buf, "WEBP", lossless=True)
        with open(p, "wb") as fh:
            fh.write(buf.getvalue())
        d = demmod.Dem(self.tmp.name, fetch_workers=2, fetcher=f)
        self.assertTrue(np.allclose(d.tiles([(10, 1, 2)])[(10, 1, 2)], 77.0))
        with self.assertRaises(demmod.DemUnavailable):
            d.tiles([(10, 3, 3)])
        self.assertEqual(tr.calls, [])


class ClassifyTest(unittest.TestCase):
    def test_classes(self):
        f, clock, sleep, tr = fetcher({
            "/404/": [(404, {}, b"", 0)], "/410/": [(410, {}, b"", 0)],
            "/400/": [(400, {}, b"", 0)],
            "/302/": [(302, {"location": "https://maps.eox.at/"}, b"", 0)],
            "/html/": [(200, {"content-type": "text/html"}, b"<!DOCTYPE html><html>busy</html>", 0)]})
        with self.assertRaises(nf.NotFound):
            f.get(f"https://{EOX}/404/1")
        with self.assertRaises(nf.NotFound):
            f.get(f"https://{EOX}/410/1")
        n = len(tr.calls)
        with self.assertRaises(nf.FetchFailed) as cm:
            f.get(f"https://{EOX}/400/1")
        self.assertEqual((cm.exception.status, len(tr.calls)), (400, n + 1))       # not retried
        with self.assertRaises(nf.RateLimited):                                     # EOX's throttle redirect
            f.get(f"https://{EOX}/302/1")
        n = len(tr.calls)
        with self.assertRaises(nf.BadResponse):
            f.get(f"https://{EOX}/html/1")
        self.assertEqual(len(tr.calls), n + 1)
        self.assertEqual(f.host(EOX).breaker.state, "open")         # 5 failures: 400, 3 x 302, the HTML page
        st = f.snapshot()[EOX]
        self.assertEqual((st["s404"], st["s3xx"], st["s4xx"]), (2, 3, 3))


class PolicyTest(unittest.TestCase):
    def test_denied_hosts(self):
        f, clock, sleep, tr = fetcher()
        for host in ("overpass-api.de", "vector.openstreetmap.org", "build.protomaps.com"):
            for kind in nf.KINDS:
                with self.assertRaises(nf.PolicyDenied):
                    f.get(f"https://{host}/x", kind=kind)
        self.assertEqual(tr.calls, [])

    def test_interactive_only_and_predownload(self):
        pol = dict(nf.POLICIES, **{"tile.example.org": nf.HostPolicy(interactive_only=True, allow_prefetch=True)})
        f, clock, sleep, tr = fetcher(policies=pol)
        with self.assertRaises(nf.PolicyDenied):
            f.get("https://tile.example.org/1/2/3.png", kind="prefetch")
        f.get("https://tile.example.org/1/2/3.png")
        with self.assertRaises(nf.PolicyDenied):                     # not for offline storage
            f.get("https://api.open-meteo.com/v1/forecast", kind="predownload")
        f.get(url(USGS), kind="predownload")                          # public domain: may be pre-downloaded
        with self.assertRaises(nf.PolicyDenied):                     # the weather is never prefetched
            f.get("https://api.open-meteo.com/v1/forecast", kind="prefetch")

    def test_unknown_host_default(self):
        f, clock, sleep, tr = fetcher()
        self.assertEqual(f.policy("example.org"), nf.DEFAULT_POLICY)
        self.assertFalse(nf.DEFAULT_POLICY.allow_prefetch)
        self.assertLessEqual(nf.DEFAULT_POLICY.rate, 2.0)

    def test_policies_match_the_plan(self):
        """docs/research/2026-10-10-rate-limits.md §5.2 for the hosts in use, raised 2026-10-16 (USGS again 2026-10-23)
        (docs/research/2026-10-16-rate-limits-raised.md)."""
        want = {USGS: (50, 150, 24, 150000), EOX: (10, 40, 6, 30000), MT: (50, 200, 32, 80000),
                "s3.amazonaws.com": (50, 100, 8, None), "api.open-meteo.com": (0.1, 2, 1, 500),
                "downloads.sourceforge.net": (1, 1, 1, None)}
        for h, (rate, burst, conc, soft) in want.items():
            p = nf.POLICIES[h]
            self.assertEqual((p.rate, p.burst, p.max_conc, p.daily_budget), (rate, burst, conc, soft), h)
        self.assertEqual(nf.POLICIES["api.open-meteo.com"].daily_hard, 500)          # 1,000 calls
        self.assertEqual(nf.POLICIES[MT].miss_ttl, 90 * nf.DAY)
        self.assertIsNone(nf.POLICIES["s3.amazonaws.com"].miss_ttl)


class BudgetTest(unittest.TestCase):
    def test_soft_and_hard(self):
        pol = nf.with_policy(nf.POLICIES, USGS, daily_budget=10, burst=100.0, rate=100.0)
        f, clock, sleep, tr = fetcher(policies=pol)
        with self.assertLogs("earthd.net", "WARNING") as logs:
            for i in range(8):
                f.get(url(USGS, i), kind="prefetch")
            with self.assertRaises(nf.BudgetExceeded):           # 80 %: no more prefetch
                f.get(url(USGS, 8), kind="prefetch")
            f.get(url(USGS, 9))                                   # demand goes on
            f.get(url(USGS, 10))
            self.assertEqual(f.host(USGS).bucket.rate, 100.0)
            f.get(url(USGS, 11))                                  # 100 %: half rate
            self.assertEqual(f.host(USGS).bucket.rate, 50.0)
            f.get(url(USGS, 12))
        self.assertEqual(len(logs.output), 2)                     # 80 %, then over budget (once an hour)
        pol = nf.with_policy(nf.POLICIES, "api.open-meteo.com", daily_hard=2, burst=10.0, rate=10.0)
        f, clock, sleep, tr = fetcher(policies=pol)
        f.get("https://api.open-meteo.com/a")
        f.get("https://api.open-meteo.com/b")
        with self.assertRaises(nf.BudgetExceeded):
            f.get("https://api.open-meteo.com/c")
        self.assertEqual(len(tr.calls), 2)

    def test_persisted_per_utc_day(self):
        with tempfile.TemporaryDirectory() as d:
            f, clock, sleep, tr = fetcher(stats_dir=d)
            for i in range(3):
                f.get(url(USGS, i))
            f.get(url(MT))
            f.close()
            day = time.strftime("%Y-%m-%d", time.gmtime(T0))
            with open(os.path.join(d, f"{day}.json")) as fh:
                saved = json.load(fh)
            self.assertEqual(saved["hosts"][USGS]["requests"], 3)
            self.assertEqual(saved["hosts"][USGS]["budget_left"], nf.POLICIES[USGS].daily_budget - 3)
            # a restart the same day goes on counting
            f2 = nf.Fetcher(clock=clock, sleep=sleep, transport=tr, wall=clock, stats_dir=d)
            self.assertEqual(f2.snapshot()[MT]["requests"], 1)    # loaded, not used yet: kept
            f2.get(url(USGS, 9))
            self.assertEqual(f2.snapshot()[USGS]["requests"], 4)
            f2.flush()
            with open(os.path.join(d, f"{day}.json")) as fh:
                self.assertEqual(json.load(fh)["hosts"][MT]["requests"], 1)
            # the next UTC day starts at zero; yesterday's file stays
            clock.advance(nf.DAY)
            f2.get(url(USGS, 10))
            self.assertEqual(f2.snapshot()[USGS]["requests"], 1)
            self.assertNotIn(MT, f2.snapshot())
            with open(os.path.join(d, f"{day}.json")) as fh:
                self.assertEqual(json.load(fh)["hosts"][USGS]["requests"], 4)

    def test_summary_once_a_minute(self):
        with tempfile.TemporaryDirectory() as d:
            f, clock, sleep, tr = fetcher({MT: [(404, {}, b"", 0)]}, stats_dir=d)
            self.assertEqual(f.tick(), "")
            f.get(url(USGS))
            with self.assertRaises(nf.NotFound):
                f.get(url(MT))
            line = f.tick()
            self.assertIn(f"{USGS} 1 req", line)
            self.assertIn(f"{MT} 1 req", line)
            self.assertIn("s404 1", line)
            self.assertEqual(f.tick(), "")                        # nothing new
            self.assertTrue(os.listdir(d))


class UserAgentTest(unittest.TestCase):
    def test_project_user_agent_and_nothing_personal(self):
        self.assertEqual(nf.USER_AGENT, f"BeamNG.Earth/{earthd.SERVER_VERSION} (+https://github.com/TecnaGamer/BeamNG.Earth)")
        f, clock, sleep, tr = fetcher()
        for u in (url(USGS), url(EOX), url(MT), "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/1/0/0.png",
                  "https://api.open-meteo.com/v1/forecast?latitude=1"):
            f.get(u)
        personal = {os.path.expanduser("~"), socket.gethostname(), getpass.getuser()}
        try:
            personal.add(os.getlogin())
        except OSError:
            pass
        for _, u, headers in tr.calls:
            self.assertEqual(headers, {"User-Agent": nf.USER_AGENT})
            # the project URL is the one identifying part (its owner is the project, not the player)
            text = (u + " " + json.dumps(headers)).replace(nf.PROJECT_URL, "")
            for s in personal:
                if s:
                    self.assertNotIn(s, text)
            self.assertNotRegex(text, r"[\w.]+@[\w.]+")

    def test_gravity_download_through_the_fetcher(self):
        """gravity._download: the project UA through a session (one slot), and no download offline."""
        data = b"egm" * 1000
        import hashlib
        seen = []

        class Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

        def urlopen(req, timeout):
            seen.append(dict(req.header_items()))
            return Resp(data)
        f, clock, sleep, tr = fetcher()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gravity.urllib.request, "urlopen", urlopen):
            dest = os.path.join(d, "egm.tar.bz2")
            gravity._download(gravity.EGM2008_URL, dest, hashlib.sha256(data).hexdigest(), len(data),
                              log=lambda s: None, net=f)
            self.assertTrue(os.path.exists(dest))
            self.assertEqual(seen, [{"User-agent": nf.USER_AGENT}])
            self.assertEqual(f.snapshot()["downloads.sourceforge.net"]["requests"], 1)
            off, *_ = fetcher(offline=True)
            with self.assertRaises(nf.Offline):
                gravity._download(gravity.EGM2008_URL, dest + "2", "0", 10, log=lambda s: None, net=off)
            self.assertEqual(len(seen), 1)
        p = gravity.Provider(tempfile.gettempdir() + "/no-such-gravity-cache", net=off)
        p._start_download()
        self.assertIsNone(p.fetcher)                    # --offline: no download thread

    def test_session_honours_retry_after(self):
        f, clock, sleep, tr = fetcher()
        u = gravity.EGM2008_URL
        err = urllib.error.HTTPError(u, 503, "busy", {"Retry-After": "30"}, None)
        self.addCleanup(err.close)
        with self.assertRaises(urllib.error.HTTPError):
            with f.session(u):
                raise err
        with self.assertRaises(nf.HostUnavailable) as cm:
            with f.session(u):
                pass
        self.assertAlmostEqual(cm.exception.retry_after, 30.0)


class _Writer:
    def __init__(self):
        self.msgs = []

    def write(self, data):
        self.msgs.append(json.loads(data[4:].decode()))

    def is_closing(self):
        return False

    def get_extra_info(self, name):
        return ("127.0.0.1", 0)


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def server(self, net, imagery=("usgs", "eox2023", "eox2016")):
        s = earthd.Server(self.tmp.name, os.path.join(self.tmp.name, "cache"), imagery=imagery, net=net)
        s.dem = test_server.FakeDem()
        return s

    def connection(self, s):
        import asyncio

        async def make():
            return earthd.Connection(s, None, _Writer())
        return asyncio.run(make())

    def test_offline_flag(self):
        f, clock, sleep, tr = fetcher(offline=True)
        self.assertTrue(f.offline)
        with self.assertRaises(nf.Offline):
            f.get(url())
        self.assertEqual(tr.calls, [])
        s = self.server(f)
        c = self.connection(s)
        c.status("Far field ready (12 tiles)")
        self.assertEqual(c.writer.msgs[-1]["text"], "Far field ready (12 tiles) | Offline - using cached data")
        self.assertEqual(c.last_status, "Far field ready (12 tiles)")
        # an uncached tile: palette material, recorded as degraded
        a = s.new_anchor(test_server.LAT, test_server.LON)
        s.anchors[a.id] = a
        info = s.make_tile(a, ff.Tile(3, 0, 0))
        self.assertIsNone(info.material)
        self.assertEqual(s.degraded, {(a.id, ff.Tile(3, 0, 0).key)})
        s.tile_pool.shutdown(wait=True)
        s.hm_pool.shutdown(wait=True)

    def test_all_chain_hosts_open_is_offline(self):
        f, clock, sleep, tr = fetcher()
        s = self.server(f)
        c = self.connection(s)
        c.status("Ready")
        self.assertEqual(c.writer.msgs[-1]["text"], "Ready")          # all well: the text as before
        f.host(EOX).breaker.trip(60.0)
        self.assertFalse(f.offline)
        c.refresh_status()                                              # the note changed: re-sent
        self.assertEqual(c.writer.msgs[-1]["text"],
                         f"Ready | Degraded: {EOX} unreachable ({nf.ROLES[EOX]})")     # (what it affects)
        for h in (USGS, MT, "s3.amazonaws.com"):
            f.host(h).breaker.trip(60.0)
        self.assertTrue(f.offline)
        c.refresh_status()
        self.assertEqual(c.writer.msgs[-1]["text"], "Ready | Offline - using cached data")
        n = len(c.writer.msgs)
        c.refresh_status()                                              # unchanged: nothing sent
        self.assertEqual(len(c.writer.msgs), n)
        clock.advance(61.0)
        tr.script[USGS] = [(200, {}, b"ok", 0)]
        f.get(url(USGS))                                                # a probe closes USGS
        self.assertFalse(f.offline)
        c.refresh_status()
        self.assertIn("Degraded", c.writer.msgs[-1]["text"])
        self.assertNotIn(USGS, c.writer.msgs[-1]["text"])
        s.tile_pool.shutdown(wait=True)
        s.hm_pool.shutdown(wait=True)

    def test_expired_marker_counts_while_the_host_is_down(self):
        f, clock, sleep, tr = fetcher()
        with tempfile.TemporaryDirectory() as d:
            m = os.path.join(d, "x.404")
            open(m, "wb").close()
            os.utime(m, (0, 0))
            self.assertFalse(f.miss_alive(m, url(USGS)))
            f.host(USGS).breaker.trip(60.0)
            self.assertTrue(f.miss_alive(m, url(USGS)))
            self.assertFalse(f.miss_alive(m + "-none", url(USGS)))

    def test_cli_flag(self):
        got = []
        with mock.patch.object(sys, "argv", ["earthd.py", "--offline", "--nice", "0"]), \
                mock.patch.object(earthd.logging, "basicConfig"), \
                mock.patch.object(earthd, "main_async", lambda args: got.append(args)), \
                mock.patch.object(earthd.asyncio, "run", lambda coro: None):
            earthd.main()
            sys.argv[1:] = ["--nice", "0"]
            earthd.main()
        self.assertEqual([a.offline for a in got], [True, False])


if __name__ == "__main__":
    unittest.main()


class RolesTest(unittest.TestCase):
    def test_every_source_says_what_it_affects(self):
        # the status line names what a source that does not answer affects (the user, 2026-10-27); SOURCES.md too
        import imagery_regional
        missing = [h for h, p in nf.POLICIES.items() if not p.denied and nf.role_of(h) is None]
        self.assertEqual(missing, [])
        self.assertEqual(sorted(set(imagery_regional.REGIONAL) - set(imagery_regional.PLACE)), [])
        self.assertIn("Lower Saxony", nf.role_of("opendata.lgln.niedersachsen.de"))
        self.assertIsNone(nf.role_of("example.com"))

    def test_sources_md_names_every_source(self):
        doc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "SOURCES.md"),
                   encoding="utf-8").read()
        missing = [h for h, p in nf.POLICIES.items() if not p.denied and h not in doc]
        self.assertEqual(missing, [])
