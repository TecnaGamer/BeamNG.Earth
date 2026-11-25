"""pagesdata.Manifest's stale-if-error refresh (2026-11-24): a pinned manifest past MANIFEST_TTL_S stays in use while
its background refresh fails (offline, a host down, an HTTP error, a malformed answer); that failure faults no build
(revisit.fault: a background thread's fault was every running build's, so a frozen test cache and offline players
had every plan made short), is asked again after the backoff, and a refresh that works replaces the pin as before.
Without any pin a failure is a missing input, as before."""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
import netfetch  # noqa: E402
import pagesdata  # noqa: E402
import revisit  # noqa: E402


class Clock:
    """pagesdata's time module with a wall clock the test moves."""

    def __init__(self):
        self.t = time.time()
        self.mod = types.SimpleNamespace(time=lambda: self.t, monotonic=time.monotonic)


class Site:
    """A fetcher standing in for Pages: errors (a netfetch error class, an OSError, b"not json") or the manifest
    body; each error raised is made in get, as the real Fetcher makes it (its revisit.fault)."""

    def __init__(self):
        self.calls = []
        self.answer = netfetch.Offline
        self.lock = threading.Lock()
        self.release = threading.Event()    # (a test holding the answer back: cleared until it sets it)
        self.release.set()

    def get(self, url, kind="demand"):
        with self.lock:
            self.calls.append(url)
        self.release.wait(10)
        a = self.answer
        if isinstance(a, type) and issubclass(a, Exception):
            if issubclass(a, netfetch.FetchError):
                raise a(f"down: {url}", url, "tecnagamer.github.io")
            raise a(f"down: {url}")
        return mock.Mock(body=a)


def pin(cache, name, built, age):
    d = os.path.join(cache, "pagesdata", name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "manifest.json")
    with open(p, "w") as fh:
        json.dump({"built": built, "files": {}}, fh)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def settle(m, site, n):
    """Wait for the background refresh to be done (site asked n times, no fetch running)."""
    for _ in range(200):
        if len(site.calls) >= n and m.fetching is None:
            return
        time.sleep(0.01)
    raise AssertionError(f"refresh not done: {len(site.calls)} calls, fetching {m.fetching}")


class StaleIfErrorTest(unittest.TestCase):

    def setUp(self):
        pagesdata.session_reset()
        self.clock = Clock()
        p = mock.patch.object(pagesdata, "time", self.clock.mod)
        p.start()
        self.addCleanup(p.stop)

    def _stale(self, cache, answer):
        pin(cache, "rail", "old", pagesdata.MANIFEST_TTL_S + 60)
        site = Site()
        site.answer = answer
        return pagesdata.Manifest(cache, site, "rail"), site

    def check_kept(self, answer):
        with tempfile.TemporaryDirectory() as cache:
            m, site = self._stale(cache, answer)
            f0 = revisit.faults()
            with revisit.scope() as b:
                self.assertEqual(m.build(), "old", "the stale pin, at once")
                settle(m, site, 1)
                self.assertEqual(m.build(), "old", "kept when its refresh failed")
            self.assertEqual(len(site.calls), 1, "one background refresh")
            self.assertEqual(revisit.faults(), f0, "the failed refresh faults nothing")
            self.assertTrue(b.complete, f"the build is complete ({b.why_not()})")
            self.assertEqual(m.known(), "old", "the cache keys: the stale build's")
            return m, site

    def test_offline(self):
        self.check_kept(netfetch.Offline)

    def test_host_down(self):
        self.check_kept(netfetch.HostUnavailable)

    def test_http_error(self):
        self.check_kept(netfetch.FetchFailed)

    def test_os_error(self):
        self.check_kept(OSError)

    def test_malformed_answer(self):
        self.check_kept(b"<html>captive portal</html>")
        self.check_kept(b"[1, 2]")

    def test_retried_after_the_backoff(self):
        m, site = self.check_kept(netfetch.Offline)
        # (check_kept's folder is gone: the pin is read again from a new one below; the backoff is the Manifest's)
        with tempfile.TemporaryDirectory() as cache:
            m.dir = os.path.dirname(pin(cache, "rail", "old", pagesdata.MANIFEST_TTL_S + 60))
            self.clock.t += pagesdata.RETRY_S - 5
            self.assertEqual(m.build(), "old")
            time.sleep(0.05)
            self.assertEqual(len(site.calls), 1, "not asked again before RETRY_S")
            self.clock.t += 10
            self.assertEqual(m.build(), "old")
            settle(m, site, 2)
            self.assertEqual(len(site.calls), 2, "asked again after RETRY_S")
            self.assertEqual(m.backoff.failures, 2)
            self.assertAlmostEqual(m.backoff.until - self.clock.t, 2 * pagesdata.RETRY_S, delta=1)
            self.clock.t += 1.5 * pagesdata.RETRY_S
            m.build()
            time.sleep(0.05)
            self.assertEqual(len(site.calls), 2, "twice as long after a second failure")
            # back online: the fresh manifest is pinned for the next start; this run keeps its build
            site.answer = json.dumps({"built": "new", "files": {}}).encode()
            self.clock.t += pagesdata.RETRY_S
            m.build()
            settle(m, site, 3)
            self.assertEqual(m.build(), "old", "the run's build kept")
            with open(os.path.join(m.dir, "manifest.json")) as fh:
                self.assertEqual(json.load(fh)["built"], "new", "pinned")
            self.assertEqual(m.backoff.failures, 0, "the backoff starts over")

    def test_online_refresh_pins_for_the_next_start(self):
        """R6 of ~/bnge-tests/manifestaudit: a refresh that works pins the newer build for the next start and never
        replaces the run's (it did, whatever recheck=False asked: regionaldata switched mid-run)."""
        for recheck in (True, False):
            pagesdata.session_reset()
            with tempfile.TemporaryDirectory() as cache:
                pin(cache, "rail", "old", pagesdata.MANIFEST_TTL_S + 60)
                site = Site()
                site.answer = json.dumps({"built": "new", "files": {}}).encode()
                changed = []
                m = pagesdata.Manifest(cache, site, "rail", recheck=recheck, on_change=lambda: changed.append(m.built))
                self.assertEqual(m.build(), "old", "the pin in use at once")
                settle(m, site, 1)
                self.assertEqual(m.build(), "old", "the run's build kept after its refresh")
                self.assertEqual(m.known(), "old")
                self.assertEqual(changed, ["old"], "taken once")
                with open(os.path.join(cache, "pagesdata", "rail", "manifest.json")) as fh:
                    self.assertEqual(json.load(fh)["built"], "new", "pinned for the next start")
                self.assertLess(time.time() - os.path.getmtime(os.path.join(cache, "pagesdata", "rail",
                                                                            "manifest.json")), 60)
                again = pagesdata.Manifest(cache, site, "rail", recheck=recheck)
                self.assertEqual(again.build(), "old", "another reader of this process: the run's")
                pagesdata.session_reset()           # (the next start)
                nxt = pagesdata.Manifest(cache, site, "rail", recheck=recheck)
                self.assertEqual(nxt.build(), "new", "the next start takes the newer pin")

    def test_no_pin_and_a_failure_is_a_missing_input(self):
        with tempfile.TemporaryDirectory() as cache:
            site = Site()
            m = pagesdata.Manifest(cache, site, "rail")
            with revisit.scope() as b:
                self.assertIsNone(m.build(), "nothing to use")
            self.assertEqual(len(site.calls), 1)
            self.assertFalse(b.complete, "a build without the input is short, as before")
            self.assertIsNone(m.known())


class BackoffTest(unittest.TestCase):
    def test_doubles_to_the_cap_and_starts_over(self):
        b = netfetch.Backoff(10, 35)
        self.assertTrue(b.due(0))
        self.assertTrue(b.failed(0), "the first failure in a row: logged at info")
        self.assertFalse(b.due(9.9))
        self.assertTrue(b.due(10))
        self.assertFalse(b.failed(10))
        self.assertEqual(b.until, 30)
        b.failed(30)
        self.assertEqual(b.until, 30 + 35, "capped")
        b.ok()
        self.assertTrue(b.due(31))
        self.assertTrue(b.failed(31))
        self.assertEqual(b.until, 41)

    def test_stale_ok_quiets_only_inside(self):
        f0 = revisit.faults()
        with netfetch.stale_ok():
            netfetch.Offline("x")
        self.assertEqual(revisit.faults(), f0)
        with netfetch.stale_ok(False):
            netfetch.Offline("x")
        self.assertEqual(revisit.faults(), f0 + 1, "no copy: a fault as before")
        netfetch.Offline("x")
        self.assertEqual(revisit.faults(), f0 + 2, "outside: as before")


if __name__ == "__main__":
    unittest.main()
