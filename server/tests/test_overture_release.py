"""The Overture release of an app session (overturepick.py; the user, 2026-11-18: Overture's releases without an app
update): one owner (the supervisor) decides once, within one deadline; every server of the session gets the same;
a server process freezes it (overture.effective); the release's STAC index is kept for a later start without an
answer (overture.keep_local_bundle, note_session) and taken then (overturepick.fallback).

The scenarios of Codex's review of the earlier design (overture-1118), each impossible here by construction:
- two server starts of one session (a restart) get the same release (no handoff file: test_two_starts_*);
- a slow or unreachable STAC never holds the start past the deadline (test_slow_stac_*, test_unreachable_*);
- a rejected release is never taken, from memory or otherwise (test_rejected_*: no verdict store exists);
- the mod's pin (a reload while disconnected, an old server without the field): mod/tests/test_releasepin.lua and
  test_core_robustness.lua 6c.
"""
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER.parent))
import netfetch  # noqa: E402
import overturepick as op  # noqa: E402

NEW = "2099-01-16.0"        # (a release id after the build's own)
OLD = "2020-01-16.0"
ROOT_CAT = {"links": [{"rel": "child", "href": f"{op.STAC_ROOT}/{NEW}/catalog.json"}]}
NEW_CAT = {"release:version": NEW, "schema:version": op.SUPPORTED_SCHEMAS[0]}


def stac_get(extra=None, root=ROOT_CAT, cat=NEW_CAT, switch=None):
    """get(url) of a fake network: the switch file (switch: its dict; None: unreachable), STAC's root and NEW's
    catalogue; anything else NotFound."""
    pages = {f"{op.STAC_ROOT}/catalog.json": root, f"{op.STAC_ROOT}/{NEW}/catalog.json": cat}
    pages.update(extra or {})

    def get(url):
        if url == op.SWITCH_URL:
            if switch is None:
                raise OSError("switch unreachable")
            return json.dumps(switch).encode()
        if url not in pages:
            raise netfetch.NotFound("404", url=url)
        if isinstance(pages[url], Exception):
            raise pages[url]
        return json.dumps(pages[url]).encode()
    return get


def pick(cache, get, deadline_s=5.0, environ=None):
    return op.Pick(cache, get=get, deadline_s=deadline_s, environ=environ or {})


def write_last(cache, release, bundle=True, schema=None):
    op.write_last(cache, release, schema or op.SUPPORTED_SCHEMAS[0])
    if bundle:
        path = op.local_bundle_path(cache, release)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump({"release": release, "collections": {"buildings/building": {"license": "x", "files": []}}}, f)


class PickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        # (after a test's own gates open, before the folder goes: the picks' threads may still write the switch copy)
        self.addCleanup(lambda: [t.join(5) for t in threading.enumerate() if t.name.startswith("overture-pick")])

    def test_switch_off_is_the_build_s_own(self):
        p = pick(self.cache, stac_get(switch={"imagery": {}}))
        self.assertEqual(p.release(), op.RELEASE)
        self.assertIn("names none", p.why)

    def test_switch_release_listed_and_supported(self):
        self.assertEqual(pick(self.cache, stac_get(switch={"overture": {"release": NEW}})).release(), NEW)

    def test_rejected_release_never_taken(self):
        """Not listed, a 404, another release's catalogue, another schema, unreadable, a malformed id: the build's
        own, even with an earlier session of it noted (nothing is remembered as accepted)."""
        write_last(self.cache, NEW)
        sw = {"overture": {"release": NEW}}
        cases = {
            "not listed": stac_get(root={"links": []}, switch=sw),
            "404": stac_get(extra={f"{op.STAC_ROOT}/{NEW}/catalog.json": netfetch.NotFound("gone")}, switch=sw),
            "another release": stac_get(cat=dict(NEW_CAT, **{"release:version": OLD}), switch=sw),
            "schema": stac_get(cat=dict(NEW_CAT, **{"schema:version": "9.0.0"}), switch=sw),
            "malformed": stac_get(switch={"overture": {"release": "../x"}}),
        }
        for why, get in cases.items():
            write_last(self.cache, NEW)         # (a rejection forgets it: noted again for each case)
            try:
                os.remove(os.path.join(self.cache, "cdn", "cdn.json"))     # (the switch as each case gives it)
            except OSError:
                pass
            self.assertEqual(pick(self.cache, get).release(), op.RELEASE, why)

    def test_unreadable_is_no_answer(self):
        """A page that is no catalogue (a captive portal): no answer, not a no (the fallback; nothing forgotten)."""
        write_last(self.cache, NEW)
        get = stac_get(root=["not", "a", "catalogue"], switch={"overture": {"release": NEW}})
        self.assertEqual(pick(self.cache, get).release(), NEW)
        self.assertFalse(os.path.exists(op._rejected_path(self.cache, NEW)))

    def test_session_note_after_a_rejection_never_restores_it(self):
        """Codex round 2: a server's session note (bundle + LAST_FILE) written after another start's rejection: the
        rejection's marker is only ever created, so the fallback still refuses the release."""
        get = stac_get(root={"links": []}, switch={"overture": {"release": NEW}})
        self.assertEqual(pick(self.cache, get).release(), op.RELEASE)
        write_last(self.cache, NEW)             # (the in-flight note lands after)
        self.assertEqual(op.fallback(self.cache)[0], op.RELEASE)
        self.assertEqual(op.Pick(self.cache, offline=True, environ={}).release(), op.RELEASE)

    def test_late_answers_never_taken(self):
        """Codex round 2: an answer that came after the deadline is not taken even when release() is first called
        later; a disk read that is not done by the deadline is not waited for (the build's own)."""
        gate = threading.Event()
        self.addCleanup(gate.set)

        def late(url):
            gate.wait(30)
            return stac_get(switch={"overture": {"release": NEW}})(url)
        p = pick(self.cache, late, deadline_s=0.1)
        time.sleep(0.2)
        gate.set()
        p._thread.join(5)
        self.assertEqual(p.release(), op.RELEASE, p.why)
        write_last(self.cache, NEW)
        disk = threading.Event()
        self.addCleanup(disk.set)
        real = op.fallback
        with mock.patch.object(op, "fallback", side_effect=lambda c: disk.wait(30) and real(c)):
            t0 = time.monotonic()
            p = op.Pick(self.cache, offline=True, deadline_s=0.2, environ={})
            self.assertEqual(p.release(), op.RELEASE, p.why)
            self.assertLess(time.monotonic() - t0, 2.0)
            disk.set()

    def test_cache_dir_by_the_server_s_rules(self):
        """Codex round 2: the pick's cache folder is the server's (the last --cache-dir, relative to its folder)."""
        self.assertEqual(op.cache_dir_of(["--cache-dir", "a", "--cache-dir=b"], "/d", "/root"), "/root/b")
        self.assertEqual(op.cache_dir_of([], "/d", "/root"), "/d")
        self.assertEqual(op.cache_dir_of(["--cache-dir", "~/c"], "/d", "/root"), os.path.expanduser("~/c"))

    def test_rejection_then_offline_never_takes_it(self):
        """Codex round 1: an answer that says no forgets the release as a fallback at once (not when a server later
        reaches a game's hello): an offline start right after takes the build's own."""
        write_last(self.cache, NEW)
        self.assertEqual(op.fallback(self.cache)[0], NEW)
        get = stac_get(root={"links": []}, switch={"overture": {"release": NEW}})
        self.assertEqual(pick(self.cache, get).release(), op.RELEASE)
        self.assertEqual(op.Pick(self.cache, offline=True, environ={}).release(), op.RELEASE)
        self.assertTrue(os.path.exists(op._rejected_path(self.cache, NEW)))

    def test_switch_fetched_is_kept_for_the_server(self):
        """The pick's switch file goes to the copy the server's cdn.switch reads while fresh: no second fetch."""
        import cdn
        self.assertEqual(pick(self.cache, stac_get(switch={"overture": {"release": NEW}, "x": 1})).release(), NEW)

        class NoNet:
            def get(self, url, **kw):
                raise AssertionError("no fetch expected")
        self.assertEqual(cdn.switch(NoNet(), self.cache).get("x"), 1)

    def test_slow_stac_never_holds_the_start(self):
        """The whole decision within its deadline: a STAC host that answers after 30 s (each request's own timeouts
        never count); the fallback then (no earlier session noted: the build's own)."""
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(url):
            if url == op.SWITCH_URL:
                return json.dumps({"overture": {"release": NEW}}).encode()
            gate.wait(30)
            return stac_get()(url)
        t0 = time.monotonic()
        p = pick(self.cache, slow, deadline_s=0.3)
        self.assertEqual(p.release(), op.RELEASE)
        self.assertLess(time.monotonic() - t0, 2.0, "the deadline held")
        self.assertIn("no answer in time", p.why)

    def test_slow_stac_takes_the_last_session_s_release(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        write_last(self.cache, NEW)
        p = pick(self.cache, lambda url: gate.wait(30) and b"{}", deadline_s=0.2)
        self.assertEqual(p.release(), NEW, p.why)

    def test_unreachable_falls_back(self):
        sw = {"overture": {"release": NEW}}
        get = stac_get(extra={f"{op.STAC_ROOT}/catalog.json": netfetch.HostUnavailable("down")}, switch=sw)
        self.assertEqual(pick(self.cache, get).release(), op.RELEASE, "nothing noted: the build's own")
        write_last(self.cache, NEW)
        self.assertEqual(pick(self.cache, get).release(), NEW, "the last session's, its index kept here")
        self.assertEqual(pick(self.cache, stac_get(switch=None)).release(), NEW, "no switch file either")

    def test_fallback_needs_newer_supported_and_its_index(self):
        write_last(self.cache, NEW, bundle=False)
        self.assertEqual(op.fallback(self.cache)[0], op.RELEASE, "no STAC index kept: the build's own")
        write_last(self.cache, OLD)
        self.assertEqual(op.fallback(self.cache)[0], op.RELEASE, "older than the build's own: never")
        write_last(self.cache, NEW, schema="9.0.0")
        self.assertEqual(op.fallback(self.cache)[0], op.RELEASE, "another schema")
        with open(op.local_bundle_path(self.cache, NEW), "wb") as f:
            f.write(b"\x1f\x8b truncated")
        op.write_last(self.cache, NEW, op.SUPPORTED_SCHEMAS[0])
        self.assertEqual(op.fallback(self.cache)[0], op.RELEASE, "a truncated index")
        self.assertEqual(op.fallback(None)[0], op.RELEASE)

    def test_two_starts_of_a_session_get_the_same(self):
        """The first release() decides; a later one (a restart) returns it even when the network's answer came in
        between with another release: no restart can change the release of a running game's session."""
        gate = threading.Event()
        self.addCleanup(gate.set)

        def late(url):
            gate.wait(30)
            return stac_get(switch={"overture": {"release": NEW}})(url)
        p = pick(self.cache, late, deadline_s=0.1)
        first = p.release()
        gate.set()
        p._thread.join(5)
        self.assertEqual(p._online[0][0][0], NEW, "(the late answer)")
        self.assertEqual([p.release() for _ in range(3)], [first] * 3)

    def test_two_starts_through_start_py(self):
        """start.py's run_restarting: the server and its restart get the same BNGE_OVERTURE_RELEASE."""
        import start
        import srvrestart
        gate = threading.Event()
        self.addCleanup(gate.set)

        def late(url):
            gate.wait(30)
            return stac_get(switch={"overture": {"release": NEW}})(url)
        p = pick(self.cache, late, deadline_s=0.1)
        envs = []

        def run(cmd, path, env):
            envs.append(dict(env))
            gate.set()
            p._thread.join(5)
            return srvrestart.EXIT_CODE if len(envs) == 1 else 0
        with mock.patch.object(start.install, "say"), mock.patch.object(start, "server_log_path", return_value=None):
            self.assertEqual(start.run_restarting(["server"], self.cache, {}, run=run, pick=p), 0)
        self.assertEqual([e[op.RELEASE_ENV] for e in envs], [op.RELEASE, op.RELEASE])

    def test_launcher_hands_the_pick_to_each_start(self):
        import launcher
        import srvrestart
        envs = []

        class Proc:
            pid = 1
            returncode = None

            def __init__(self, code):
                self.code = code

            def poll(self):
                self.returncode = self.code
                return self.code

            def wait(self, timeout=None):
                return self.poll()
        p = pick(self.cache, stac_get(switch={"overture": {"release": NEW}}))

        def popen(cmd, **kw):
            envs.append(kw["env"])
            return Proc(srvrestart.EXIT_CODE if len(envs) == 1 else None)
        with mock.patch.object(launcher.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(launcher.threading, "Thread"), mock.patch.object(launcher, "row"), \
                mock.patch.object(launcher, "say"), mock.patch.object(launcher, "server_port_busy", return_value=False), \
                mock.patch.object(launcher.os, "killpg", create=True):
            log = os.path.join(self.cache, "server.log")
            rs = launcher.RestartingServer(lambda: launcher.start_server(self.cache, [], log,
                                                                         overture_release=p.release()), 47613, None)
            rs.poll()
            rs.poll()
        self.assertEqual(len(envs), 2)
        self.assertEqual([e[op.RELEASE_ENV] for e in envs], [NEW, NEW])

    def test_env_given_by_hand_wins_without_a_request(self):
        def boom(url):
            raise AssertionError("no request")
        p = op.Pick(self.cache, get=boom, environ={op.RELEASE_ENV: NEW})
        self.assertEqual(p.release(), NEW)
        self.assertIsNone(p._thread)

    def test_offline_takes_the_fallback_at_once(self):
        write_last(self.cache, NEW)
        p = op.Pick(self.cache, offline=True, get=lambda url: self.fail("no request offline"), environ={})
        self.assertEqual(p.release(), NEW)

    def test_switch_copy_on_disk_read_when_fresh(self):
        os.makedirs(os.path.join(self.cache, "cdn"))
        with open(os.path.join(self.cache, "cdn", "cdn.json"), "w") as f:
            json.dump({"overture": {"release": NEW}}, f)
        self.assertEqual(pick(self.cache, stac_get(switch=None)).release(), NEW)

    def test_launcher_cache_dir_without_saving(self):
        import launcher
        with mock.patch.object(launcher, "app_settings", return_value={"cacheDir": "/saved"}):
            self.assertEqual(launcher.pick_cache_dir(None, ["--cache-dir", "/x"]), "/x")
            self.assertEqual(launcher.pick_cache_dir(None, ["--cache-dir", "/x", "--cache-dir=/y"]), "/y")
            self.assertEqual(launcher.pick_cache_dir(None, ["--cache-dir", "rel"]),
                             os.path.join(launcher.ROOT, "rel"))
            self.assertEqual(launcher.pick_cache_dir(None, []), "/saved")
            self.assertEqual(launcher.pick_cache_dir("default", []), launcher.apppaths.default_cache_dir())

    def test_constants_agree(self):
        import cdn
        import overture
        self.assertEqual(cdn.SWITCH_URL, op.SWITCH_URL)
        self.assertIs(overture.RELEASE, op.RELEASE)
        self.assertTrue(op.valid(op.RELEASE))
        self.assertLess(op.order("2026-11-18.9"), op.order("2026-11-18.10"))


def run_py(code, env_release=None):
    env = {k: v for k, v in os.environ.items() if k != op.RELEASE_ENV}
    if env_release is not None:
        env[op.RELEASE_ENV] = env_release
    out = subprocess.run([sys.executable, "-c", code], cwd=str(SERVER), env=env, capture_output=True, text=True,
                         timeout=120)
    if out.returncode:
        raise AssertionError(out.stderr)
    return out.stdout.strip().splitlines()


class EffectiveTest(unittest.TestCase):
    def test_env_frozen_per_process(self):
        code = ("import os, overture; a = overture.effective(); os.environ['BNGE_OVERTURE_RELEASE'] = '2098-01-01.0';"
                " print(a); print(overture.effective()); print(overture.Reader('/nonexistent', None).release)")
        self.assertEqual(run_py(code, NEW), [NEW, NEW, NEW])
        self.assertEqual(run_py(code)[0], op.RELEASE, "no supervisor: the build's own")
        self.assertEqual(run_py(code, "latest")[0], op.RELEASE, "not a release id: the build's own")

    def test_map_data_versions_key_the_release(self):
        """Every cache key that had the constant: MapData.versions()['overture'] (plans, facts, tiles, the share
        codes) is the process's release."""
        code = ("import tempfile, mapdata; d = tempfile.mkdtemp(); m = mapdata.MapData(d);"
                " print(m.release); print(m.versions()['overture'])")
        self.assertEqual(run_py(code, NEW), [NEW, NEW])
        self.assertEqual(run_py(code), [op.RELEASE, op.RELEASE])


class Resp:
    def __init__(self, body):
        self.body = body


class FakeStac:
    """Overture's STAC for NEW: one collection (buildings/building) of two items."""

    def __init__(self):
        base = f"{op.STAC_ROOT}/{NEW}/buildings/building"
        self.pages = {
            f"{op.STAC_ROOT}/{NEW}/catalog.json": NEW_CAT,
            f"{base}/collection.json": {"license": "ODbL", "links": [
                {"rel": "item", "href": f"{base}/a.json"}, {"rel": "item", "href": f"{base}/b.json"}],
                "extent": {"spatial": {"bbox": [[-180, -90, 180, 90], [0, 0, 10, 10], [-50, -10, -40, 0]]}}},
            f"{base}/a.json": {"assets": {"aws": {"href": "s3://o/a.parquet", "file:size": 11},
                                          "azure": {"href": "https://az/a.parquet"}}},
            f"{base}/b.json": {"assets": {"aws": {"href": "s3://o/b.parquet", "file:size": 22}}},
        }
        self.asked = []
        self.down = False

    def get(self, url, kind="demand", **kw):
        self.asked.append(url)
        if self.down:
            raise OSError("down")
        if url not in self.pages:
            raise netfetch.NotFound("404", url=url)
        return Resp(json.dumps(self.pages[url]).encode())


class LocalBundleTest(unittest.TestCase):
    def test_note_session_keeps_the_index_then_the_fallback_takes_it(self):
        import overture
        stac = FakeStac()
        with tempfile.TemporaryDirectory() as cache, \
                mock.patch.object(overture, "_stac_pairs", return_value=[("buildings", "building")]), \
                mock.patch.object(overture, "effective", return_value=NEW):
            raw = os.path.join(cache, "mapdata", "raw")
            want = overture.StacIndex(NEW, raw, stac).files("buildings", "building", (1, 1, 2, 2))
            self.assertEqual(op.fallback(cache)[0], op.RELEASE, "nothing noted yet")
            self.assertEqual(overture.note_session(cache, stac), op.SUPPORTED_SCHEMAS[0])
            self.assertEqual(op.fallback(cache)[0], NEW)
            stac.down = True
            import shutil
            shutil.rmtree(raw)          # (the cache's cap evicted the item files: the kept index is outside it)
            got = overture.StacIndex(NEW, raw, stac).files("buildings", "building", (1, 1, 2, 2))
            self.assertEqual(got, want, "the same files from the kept index, no request")
            self.assertEqual(got, [("a.parquet", ["s3://o/a.parquet", "https://az/a.parquet"], 11)])

    def test_note_session_refuses_another_schema(self):
        import overture
        stac = FakeStac()
        stac.pages[f"{op.STAC_ROOT}/{NEW}/catalog.json"] = dict(NEW_CAT, **{"schema:version": "9.0.0"})
        with tempfile.TemporaryDirectory() as cache, mock.patch.object(overture, "effective", return_value=NEW):
            self.assertIsNone(overture.note_session(cache, stac))
            self.assertFalse(os.path.exists(os.path.join(cache, "cdn", op.LAST_FILE)))


class FlakyStac(FakeStac):
    """FakeStac that defers two requests of every three (netfetch's bucket low) and fails some requests once each."""

    def __init__(self, fail_once=()):
        super().__init__()
        self.n = 0
        self.fail_once = set(fail_once)

    def get(self, url, kind="demand", **kw):
        self.n += 1
        if self.n % 3:
            raise netfetch.Deferred("bucket low, background dropped", url=url)
        if url in self.fail_once:
            self.fail_once.discard(url)
            raise netfetch.FetchFailed(f"{url}: timed out", url=url)
        return super().get(url, kind, **kw)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class SessionPrepTest(unittest.TestCase):
    def setUp(self):
        import overture
        self.overture = overture
        self.cache = tempfile.mkdtemp(prefix="ovprep_")
        self.addCleanup(__import__("shutil").rmtree, self.cache, True)
        for p in (mock.patch.object(overture, "_stac_pairs", return_value=[("buildings", "building")]),
                  mock.patch.object(overture, "effective", return_value=NEW)):
            p.start()
            self.addCleanup(p.stop)
        self.last = os.path.join(self.cache, "cdn", op.LAST_FILE)
        self.bundle = op.local_bundle_path(self.cache, NEW)

    def test_finishes_after_deferrals_and_transient_errors(self):
        base = f"{op.STAC_ROOT}/{NEW}/buildings/building"
        stac = FlakyStac(fail_once=[f"{base}/b.json", f"{base}/collection.json"])
        clock = FakeClock()
        seen = []

        def sleep(s):           # (at every wait: no note before the whole bundle is on disk)
            seen.append((os.path.exists(self.last), bool(op.read_bundle(self.bundle, NEW))))
            clock.sleep(s)
        got = self.overture.prepare_session(self.cache, stac, clock=clock, sleep=sleep)
        self.assertEqual(got, op.SUPPORTED_SCHEMAS[0])
        self.assertTrue(seen and not any(n for n, _ in seen), "no note while the bundle was being made")
        self.assertFalse(any(b for _, b in seen), "no bundle before the crawl finished")
        self.assertIn(self.overture.PREP_BACKOFF_S[0], clock.slept, "a failed pass backed off")
        self.assertIn(self.overture.PREP_DEFER_S, clock.slept, "deferrals waited")
        self.assertEqual(op.fallback(self.cache)[0], NEW)
        self.assertTrue(self.overture.bundle_complete(op.read_bundle(self.bundle, NEW)))
        # resumed: each item file came from the net once when it succeeded (the failed pass's saves kept)
        ok = [u for u in stac.asked if u.endswith("/a.json")]
        self.assertEqual(len(ok), 1, stac.asked)

    def test_gives_up_within_its_bound_without_a_note(self):
        stac = FakeStac()
        stac.down = True
        clock = FakeClock()
        with self.assertRaises(OSError):
            self.overture.prepare_session(self.cache, stac, clock=clock, sleep=clock.sleep)
        self.assertLessEqual(clock.t, self.overture.PREP_MAX_S)
        self.assertEqual(len([s for s in clock.slept]), self.overture.PREP_RETRIES)
        self.assertFalse(os.path.exists(self.last))
        self.assertFalse(os.path.exists(self.bundle))
        # deferred for ever: gives up at the bound, no note
        clock2 = FakeClock()

        class Never(FakeStac):
            def get(self, url, kind="demand", **kw):
                raise netfetch.Deferred("bucket low", url=url)
        with self.assertRaises(netfetch.Deferred):
            self.overture.prepare_session(self.cache, Never(), clock=clock2, sleep=clock2.sleep, max_s=60.0)
        self.assertLessEqual(clock2.t, 60.0)
        self.assertFalse(os.path.exists(self.last))

    def test_no_request_or_note_past_the_bound(self):
        """Slow successful requests (no deferral, no failure) still stop at the bound, and nothing is noted."""
        stac = FakeStac()
        clock = FakeClock()
        real = stac.get

        def slow(url, kind="demand", **kw):
            clock.t += 40.0
            return real(url, kind, **kw)
        stac.get = slow
        with self.assertRaises(self.overture.PrepOutOfTime):
            self.overture.prepare_session(self.cache, stac, clock=clock, sleep=clock.sleep, max_s=100.0)
        self.assertLessEqual(len(stac.asked), 3)
        self.assertFalse(os.path.exists(self.last))

    def test_note_write_failure_is_tried_again(self):
        stac = FakeStac()
        clock = FakeClock()
        real, calls = op.write_last, []

        def flaky(*a):
            calls.append(a)
            return False if len(calls) == 1 else real(*a)
        with mock.patch.object(op, "write_last", flaky):
            got = self.overture.prepare_session(self.cache, stac, clock=clock, sleep=clock.sleep)
        self.assertEqual(got, op.SUPPORTED_SCHEMAS[0])
        self.assertEqual(len(calls), 2)
        self.assertEqual(op.fallback(self.cache)[0], NEW)
        self.assertEqual(len([u for u in stac.asked if u.endswith("/a.json")]), 1, "the bundle kept, not crawled again")

    def test_release_gone_is_final(self):
        stac = FakeStac()
        del stac.pages[f"{op.STAC_ROOT}/{NEW}/buildings/building/collection.json"]
        clock = FakeClock()
        with self.assertRaises(self.overture.ReleaseGone):
            self.overture.prepare_session(self.cache, stac, clock=clock, sleep=clock.sleep)
        self.assertEqual(clock.slept, [])
        self.assertFalse(os.path.exists(self.last))

    def test_one_job_a_process(self):
        stac = FakeStac()
        gate = threading.Event()
        real = stac.get

        def slow(url, kind="demand", **kw):
            gate.wait(5)
            return real(url, kind, **kw)
        stac.get = slow
        ended = []
        with mock.patch.dict(self.overture._PREP, {"thread": None}):
            t = self.overture.start_session_prep(self.cache, stac, lambda s, e: ended.append((s, e)))
            self.assertIsNotNone(t)
            self.assertIsNone(self.overture.start_session_prep(self.cache, stac), "a second hello: nothing started")
            gate.set()
            t.join(5)
            self.assertEqual(ended, [(op.SUPPORTED_SCHEMAS[0], None)])
            self.assertIsNone(self.overture.start_session_prep(self.cache, stac), "after it ended: nothing started")


class SwitchCapTest(unittest.TestCase):
    BIG = {"overture": {"release": NEW}, "pad": "x" * (op.SWITCH_MAX_BYTES + 10)}

    def test_oversized_switch_from_the_net_is_no_answer(self):
        with tempfile.TemporaryDirectory() as cache:
            get = stac_get(switch=self.BIG)
            self.assertIsNone(op._switch_doc(get, cache))
            self.assertFalse(os.path.exists(os.path.join(cache, "cdn", "cdn.json")), "not kept")
            with self.assertRaises(op.Unreachable):
                op.decide_online(get, cache)
            write_last(cache, NEW)
            self.assertEqual(pick(cache, get).release(), NEW, "the fallback took over")

    def test_oversized_switch_on_disk_is_not_read(self):
        with tempfile.TemporaryDirectory() as cache:
            path = os.path.join(cache, "cdn", "cdn.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as fh:
                json.dump(self.BIG, fh)
            self.assertIsNone(op.read_switch_file(path))
            self.assertIsNone(op._switch_doc(stac_get(switch=None), cache))
            import cdn
            self.assertEqual(cdn.switch(netfetch.Fetcher(offline=True), cache), {})
            with open(path, "w") as fh:
                json.dump({"overture": {"release": NEW}}, fh)
            self.assertEqual(op.read_switch_file(path), {"overture": {"release": NEW}})
            # under the cap as it came, past it written again ("1E5" -> 100000.0): the copy before kept
            body = b'{"n":[' + b",".join([b"1E5"] * (op.SWITCH_MAX_BYTES // 4 - 4)) + b"]}"
            doc = op.parse_switch(body)
            self.assertIsNotNone(doc)
            op.keep_switch(path, doc)
            self.assertEqual(op.read_switch_file(path), {"overture": {"release": NEW}})

    def _fetcher(self, body, gz):
        class Tr:
            def __init__(self):
                self.calls = 0

            def request(self, url, headers, timeout):
                self.calls += 1
                rh = {"content-type": "application/json"}
                if gz:
                    rh["content-encoding"] = "gzip"
                return 200, rh, gzip.compress(body) if gz else body
        tr = Tr()
        pol = {"h.test": netfetch.HostPolicy(rate=1000.0, burst=1000.0, max_conc=4, daily_budget=None, gzip=True)}
        return netfetch.Fetcher(transport=tr, sleep=lambda s: None, policies=pol), tr

    def test_netfetch_max_bytes(self):
        big = b'{"pad": "' + b"x" * (op.SWITCH_MAX_BYTES + 10) + b'"}'
        small = b'{"overture": {}}'
        for gz in (False, True):
            f, tr = self._fetcher(big, gz)
            with self.assertRaises(netfetch.TooLarge):
                f.get("https://h.test/cdn.json", max_bytes=op.SWITCH_MAX_BYTES)
            self.assertEqual(tr.calls, 1, "not asked again")
            if gz:
                self.assertLess(len(gzip.compress(big)), op.SWITCH_MAX_BYTES, "(decoded past it, not on the wire)")
            f, _ = self._fetcher(small, gz)
            self.assertEqual(f.get("https://h.test/cdn.json", max_bytes=op.SWITCH_MAX_BYTES).body, small)
            f, _ = self._fetcher(big, gz)
            self.assertEqual(len(f.get("https://h.test/cdn.json").body), len(big), "no cap: as before")

    def test_transport_reads_no_further_than_the_cap(self):
        import http.server
        n = op.SWITCH_MAX_BYTES * 8

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(n))
                self.end_headers()
                try:
                    self.wfile.write(b"x" * n)
                except OSError:
                    pass

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        tr = netfetch.HttpsTransport()
        netfetch._body_cap.n = 1000
        try:
            status, _, body = tr.request(f"http://127.0.0.1:{srv.server_address[1]}/cdn.json", {}, 5.0)
        finally:
            netfetch._body_cap.n = None
        self.assertEqual((status, len(body)), (200, 1001))

    def test_transport_capped_read_refuses_a_short_body(self):
        """A fixed-length body cut short under the cap: IncompleteRead, as the unbounded read gave."""
        import http.client
        import socket
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        self.addCleanup(lsock.close)

        def serve():
            c, _ = lsock.accept()
            c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b'{"overture": {}}')
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        tr = netfetch.HttpsTransport()
        netfetch._body_cap.n = 1000
        try:
            with self.assertRaises(http.client.IncompleteRead):
                tr.request(f"http://127.0.0.1:{lsock.getsockname()[1]}/cdn.json", {}, 5.0)
        finally:
            netfetch._body_cap.n = None


if __name__ == "__main__":
    unittest.main()
