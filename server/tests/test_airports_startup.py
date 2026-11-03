"""Bound optional startup refresh; fake clocks/transports and owned spawn children, no internet."""
import multiprocessing
from dataclasses import replace
import os
from pathlib import Path
import random
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import airports
import blockworker
import netfetch
from test_airports import FakeTransport, af


def fixture_fetcher():
    a, r, _ = af.world()
    policies = {h: replace(p, rate=1e9, burst=1e9) for h, p in netfetch.POLICIES.items()}
    return netfetch.Fetcher(policies=policies,
                           transport=FakeTransport({"airports.csv": a, "runways.csv": r}))


def fixture_refresh(cache_dir, offline):
    fetcher = fixture_fetcher()
    fetcher.offline_flag = offline
    airports.Store(cache_dir, fetcher).load()


def fixture_hang(cache_dir, offline):
    Path(cache_dir, "helper.pid").write_text(str(os.getpid()))
    threading.Event().wait(45)  # safety lifetime even if the test runner exits unexpectedly


class Clock:
    def __init__(self):
        self.t = 2_000_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class StalledTransport:
    def __init__(self, clock):
        self.clock, self.timeouts = clock, []

    def request(self, url, headers, timeout):
        self.timeouts.append(timeout)
        self.clock.sleep(timeout)
        raise TimeoutError("synthetic socket stall")


class FakeProcess:
    def __init__(self, clock, start_cost=7, ignores_terminate=False, ignores_kill=False):
        self.clock, self.start_cost = clock, start_cost
        self.ignores_terminate, self.ignores_kill = ignores_terminate, ignores_kill
        self.pid = None
        self.exitcode = None
        self.alive, self.closed = False, False
        self.joins, self.terminated, self.killed = [], False, False

    def start(self):
        self.clock.sleep(self.start_cost)
        self.pid, self.alive = 123, True

    def join(self, timeout):
        self.joins.append(timeout)
        if self.alive:
            self.clock.sleep(timeout)

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        if not self.ignores_terminate:
            self.alive, self.exitcode = False, -15

    def kill(self):
        self.killed = True
        if not self.ignores_kill:
            self.alive, self.exitcode = False, -9

    def close(self):
        assert not self.alive
        self.closed = True


class StartupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self):
        return airports.Store(self.tmp.name, fixture_fetcher(), wall=self.clock).load()

    def load_fake(self, process, **kwargs):
        ctx = mock.Mock()
        ctx.Process.return_value = process
        return airports.load_startup(self.tmp.name, context=ctx, clock=self.clock, wall=self.clock, **kwargs)

    def test_original_stalled_transport_exceeds_readiness_with_and_without_cache(self):
        for cached in (False, True):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as root:
                clock = Clock()
                if cached:
                    airports.Store(root, fixture_fetcher(), wall=clock).load()
                    clock.sleep(8 * 86400)
                transport = StalledTransport(clock)
                net = netfetch.Fetcher(clock=clock, wall=clock, sleep=clock.sleep,
                                       rng=random.Random(0), transport=transport)
                started = clock()
                store = airports.Store(root, net, wall=clock)
                if cached:
                    with self.assertLogs("earthd.airports", "WARNING"):
                        self.assertTrue(store.load().runways)
                else:
                    with self.assertRaises(airports.AirportsUnavailable):
                        store.load()
                self.assertEqual(transport.timeouts, [120, 120])
                self.assertGreater(clock() - started, 240)

    def test_timeout_includes_spawn_time_and_preserves_cached_generation_and_backoff(self):
        expected = self.seed()
        self.clock.sleep(8 * 86400)
        process = FakeProcess(self.clock)
        started = self.clock()
        with self.assertLogs("earthd.airports", "WARNING"):
            actual = self.load_fake(process)
        self.assertEqual(self.clock() - started, airports.STARTUP_REFRESH_S)
        self.assertEqual(process.joins[0], airports.STARTUP_REFRESH_S - process.start_cost)
        self.assertTrue(process.terminated and process.closed)
        self.assertEqual(actual.snapshot, expected.snapshot)
        pointer = airports.Store(self.tmp.name).pointer()
        self.assertEqual(pointer["attempt"], self.clock())
        self.assertIn("exceeded", pointer["error"])
        ctx = mock.Mock()
        again = airports.load_startup(self.tmp.name, wall=self.clock, context=ctx)
        ctx.Process.assert_not_called()
        self.assertEqual(again.snapshot, expected.snapshot)

    def test_timeout_without_cache_reaps_before_unavailable_fallback(self):
        process = FakeProcess(self.clock, start_cost=25)
        with self.assertLogs("earthd.airports", "WARNING"), self.assertRaises(airports.AirportsUnavailable):
            self.load_fake(process)
        self.assertEqual(process.joins[0], 0, "startup overhead must not receive another full budget")
        self.assertTrue(process.terminated and process.closed)
        self.assertFalse(process.alive)

    def test_kill_escalation_and_unreapable_helper_is_fatal(self):
        self.seed()
        self.clock.sleep(8 * 86400)
        process = FakeProcess(self.clock, ignores_terminate=True)
        with self.assertLogs("earthd.airports", "WARNING"):
            self.load_fake(process)
        self.assertTrue(process.killed and process.closed)
        self.clock.sleep(airports.RETRY_S + 1)
        process = FakeProcess(self.clock, ignores_terminate=True, ignores_kill=True)
        with mock.patch.object(airports.Store, "load") as cached_read, \
                self.assertRaisesRegex(RuntimeError, "could not be stopped"):
            self.load_fake(process)
        cached_read.assert_not_called()
        self.assertTrue(process.killed)
        self.assertFalse(process.closed)

    def test_exit_racing_terminate_is_reaped_without_false_fatal_error(self):
        self.seed()
        self.clock.sleep(8 * 86400)
        process = FakeProcess(self.clock)
        def exited_while_terminating():
            process.alive, process.exitcode = False, 0
            raise ProcessLookupError("fixture exited")
        process.terminate = exited_while_terminating
        with self.assertLogs("earthd.airports", "WARNING"):
            self.assertTrue(self.load_fake(process).runways)
        self.assertTrue(process.closed)

    def test_fresh_and_offline_bypass_helper_and_network(self):
        expected = self.seed()
        ctx = mock.Mock()
        with mock.patch.object(netfetch.HttpsTransport, "request", side_effect=AssertionError("network")):
            for offline in (False, True):
                self.assertEqual(airports.load_startup(self.tmp.name, offline, wall=self.clock,
                                                     context=ctx).snapshot, expected.snapshot)
            self.clock.sleep(8 * 86400)
            self.assertEqual(airports.load_startup(self.tmp.name, True, wall=self.clock,
                                                 context=ctx).snapshot, expected.snapshot)
            with tempfile.TemporaryDirectory() as empty, self.assertRaises(airports.AirportsUnavailable):
                airports.load_startup(empty, True, context=ctx)
        ctx.Process.assert_not_called()

    def test_real_spawn_refresh_and_worker_use_same_committed_snapshot(self):
        before = {p.pid for p in multiprocessing.active_children()}
        db = airports.load_startup(self.tmp.name, budget=10, _refresh_target=fixture_refresh)
        with mock.patch.object(netfetch.HttpsTransport, "request", side_effect=AssertionError("network")):
            worker = blockworker.load_airports(self.tmp.name, None)
            parent = airports.load_startup(self.tmp.name, offline=True)
        self.assertEqual(db.snapshot, worker.db.snapshot)
        self.assertEqual(db.snapshot, parent.snapshot)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_real_spawn_hang_is_reaped_before_cached_fallback(self):
        expected = self.seed()
        self.clock.sleep(8 * 86400)
        before = {p.pid for p in multiprocessing.active_children()}
        started = time.monotonic()
        with self.assertLogs("earthd.airports", "WARNING"):
            db = airports.load_startup(self.tmp.name, wall=self.clock, budget=5, _refresh_target=fixture_hang)
        self.assertEqual(db.snapshot, expected.snapshot)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(Path(self.tmp.name, "helper.pid").is_file(), "fixture reached its stalled operation")
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_killed_startup_request_accounting_is_persisted_before_transport(self):
        stats = Path(self.tmp.name, "netstats")
        class InspectTransport:
            def request(inner, url, headers, timeout):
                reader = netfetch.Fetcher(stats_dir=str(stats), offline=True)
                self.assertEqual(reader.snapshot()[netfetch.host_of(url)]["requests"], 1)
                return 200, {}, b"fixture"
        fetcher = airports._StartupFetcher(stats_dir=str(stats), transport=InspectTransport())
        self.assertEqual(fetcher.get(airports.DATA_URL + "airports.csv").body, b"fixture")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()
