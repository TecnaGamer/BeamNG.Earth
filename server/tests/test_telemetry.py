"""telemetry.py: anonymous usage reports (counts since the last report, every process's, kept when a send fails)."""
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netfetch  # noqa: E402
import telemetry  # noqa: E402


class ReporterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent = []
        self.ok = True
        self.f = netfetch.Fetcher()
        self.proc = os.path.join(self.tmp.name, "proc")
        self.r = telemetry.Reporter(self.tmp.name, "1.9.0", lambda: (True, False), proc_dir=self.proc,
                                    send=lambda b: (self.sent.append(b), self.ok)[1])

    def count(self, host, n, nbytes=0, s429=0):
        h = self.f.host(host)
        self.f._bump(h, "requests", n)
        self.f._bump(h, "bytes", nbytes)
        self.f._bump(h, "s429", s429)

    def test_counts_since_the_last_report_and_a_stable_id(self):
        self.count("a.example.org", 10, 3_000_000, 1)
        self.r.report()
        src = self.sent[-1]["sources"]["a.example.org"]
        self.assertEqual((src["req"], src["mb"], src["e429"]), (10, 3.0, 1))
        self.count("a.example.org", 4)
        self.r.report()
        self.assertEqual(self.sent[-1]["sources"]["a.example.org"]["req"], 4)
        self.r.report()
        self.assertNotIn("a.example.org", self.sent[-1]["sources"])            # nothing new
        self.assertEqual(set(self.sent[-1]), {"id", "v", "os", "game", "mp", "interval", "sources", "rid", "seq"})
        self.assertEqual([b["seq"] for b in self.sent], [1, 2, 3])
        self.assertEqual(len({b["rid"] for b in self.sent}), 1)
        again = telemetry.Reporter(self.tmp.name, "1.9.0", lambda: (False, False), send=lambda b: True)
        self.assertEqual(again.id, self.r.id)                                    # the install's id is kept

    def test_a_failed_send_is_sent_again_as_it_was_then_the_new_counts(self):
        # (a report whose answer was lost may have arrived: sent again unchanged, its seq, so a collector can count it
        # once; what came meanwhile goes in the next one)
        self.count("b.example.org", 7)
        self.ok = False
        self.r.report()
        self.count("b.example.org", 1)
        self.r.report()                     # (failed again: still the same report)
        self.ok = True
        self.count("b.example.org", 2)
        self.r.report()
        self.assertEqual([b["sources"]["b.example.org"]["req"] for b in self.sent], [7, 7, 7])
        self.assertEqual(len({b["seq"] for b in self.sent}), 1)
        self.r.report()
        self.assertEqual(self.sent[-1]["sources"]["b.example.org"]["req"], 3)
        self.assertEqual(self.sent[-1]["seq"], self.sent[0]["seq"] + 1)
        self.r.report()
        self.assertNotIn("b.example.org", self.sent[-1]["sources"])

    def _collector(self, answer):
        """A collector that counts each (rid, seq) once, as native/serverlist does; answer(n) -> what the sender
        hears for the n-th report (True, False: no answer, or telemetry.REJECTED)."""
        seen, total, n = set(), {}, [0]

        def send(body):
            n[0] += 1
            got = answer(n[0])
            if got is not telemetry.REJECTED:          # (taken, whether or not its answer comes back)
                ident = (body["rid"], body["seq"])
                if ident not in seen:
                    seen.add(ident)
                    for host, d in body["sources"].items():
                        total[host] = total.get(host, 0) + d["req"]
                    for k, v in body.get("dem", {}).items():
                        total[k] = total.get(k, 0) + v
            self.sent.append(body)
            return got
        return send, total

    def test_lost_answers_never_count_a_report_twice(self):
        # (Codex review of 497c725b: after 4 lost answers the counts went into a new seq, counted again)
        send, total = self._collector(lambda n: n > 6)           # (every report arrives; six answers lost)
        self.r.send = send
        self.count("g.example.org", 5)
        netfetch.note_detail("mapterhorn:test_lost:16:net", 3)
        for _ in range(7):
            self.r.report()
            self.count("g.example.org", 1)
        self.r.report()
        self.r.report()
        self.assertEqual(total.get("g.example.org"), 5 + 7, "each request counted exactly once")
        self.assertEqual(total.get("mapterhorn:test_lost:16:net"), 3)
        self.assertEqual(len({b["seq"] for b in self.sent[:7]}), 1, "the same report until it is acknowledged")

    def test_a_rejected_report_is_dropped_not_sent_again(self):
        # (an explicit refusal, a 400 / 413: the collector took nothing, and the same body would be refused forever)
        send, total = self._collector(lambda n: telemetry.REJECTED if n == 1 else True)
        self.r.send = send
        self.count("h.example.org", 5)
        self.r.report()
        self.count("h.example.org", 2)
        self.r.report()
        self.assertEqual(total.get("h.example.org"), 2, "the refused counts dropped, never inflated")
        self.assertEqual(self.sent[1]["seq"], self.sent[0]["seq"] + 1)

    def test_post_tells_a_refusal_from_a_lost_answer(self):
        import io
        import urllib.error

        def raising(e):
            def f(*a, **k):
                raise e
            return f
        cases = [(urllib.error.HTTPError(telemetry.URL, 400, "bad", {}, io.BytesIO()), telemetry.REJECTED),
                 (urllib.error.HTTPError(telemetry.URL, 413, "big", {}, io.BytesIO()), telemetry.REJECTED),
                 (urllib.error.HTTPError(telemetry.URL, 429, "soon", {}, io.BytesIO()), False),
                 (urllib.error.HTTPError(telemetry.URL, 502, "gw", {}, io.BytesIO()), False),
                 (TimeoutError("lost"), False), (OSError("reset"), False)]
        for e, want in cases:
            with mock.patch("urllib.request.urlopen", raising(e)):
                self.assertIs(self.r._post({"id": "x"}), want, e)

    def test_other_processes_totals_are_summed_once(self):
        os.makedirs(self.proc)
        with open(os.path.join(self.proc, "999999.json"), "w") as fh:
            json.dump({"pid": 999999, "at": 1e12, "hosts": {"c.example.org": {"requests": 5, "bytes": 1_000_000}}}, fh)
        self.r.report()
        self.assertEqual(self.sent[-1]["sources"]["c.example.org"]["req"], 5)
        with open(os.path.join(self.proc, "999999.json"), "w") as fh:
            json.dump({"pid": 999999, "at": 1e12, "hosts": {"c.example.org": {"requests": 8, "bytes": 1_000_000}}}, fh)
        self.r.report()
        self.assertEqual(self.sent[-1]["sources"]["c.example.org"]["req"], 3)    # only what changed

    def _proc_file(self, d, pid, inst, req, at=1e12, detail=None):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{pid}-{inst}.json"), "w") as fh:
            json.dump({"pid": pid, "inst": inst, "at": at, "hosts": {"e.example.org": {"requests": req}},
                       "detail": detail or {}}, fh)

    def test_a_restart_never_counts_the_last_runs_files_again(self):
        # (2026-11-15 review: a new Reporter counted every surviving process file from zero, so each restart sent the
        # last run's worker totals again, and two servers on one folder counted each other's)
        root = os.path.join(self.tmp.name, "netstats", "proc")
        old_run = telemetry.run_proc_dir(root)
        self._proc_file(old_run, 999991, "aa", 50)
        with open(os.path.join(root, "999992.json"), "w") as fh:       # (a version before the run folders)
            json.dump({"pid": 999992, "at": 1e12, "hosts": {"e.example.org": {"requests": 70}}}, fh)
        first = telemetry.Reporter(self.tmp.name, "1", lambda: (True, False), proc_dir=old_run,
                                   send=lambda b: (self.sent.append(b), True)[1])
        first.report()
        self.assertEqual(self.sent[-1]["sources"]["e.example.org"]["req"], 50, "the run's own files, once")
        new_run = telemetry.run_proc_dir(root)                  # (the server started again)
        self.assertNotEqual(new_run, old_run)
        self.assertFalse(os.path.exists(os.path.join(root, "999992.json")), "an old version's file: dropped")
        self.assertTrue(os.path.isdir(old_run), "a recent run's folder is left (another server may still run)")
        again = telemetry.Reporter(self.tmp.name, "1", lambda: (True, False), proc_dir=new_run,
                                   send=lambda b: (self.sent.append(b), True)[1])
        again.report()
        self.assertNotIn("e.example.org", self.sent[-1]["sources"], "the old run's files are not counted again")
        old = os.path.getmtime(old_run) - telemetry.PROC_KEEP_S - 60
        for n in os.listdir(old_run):
            os.utime(os.path.join(old_run, n), (old, old))
        os.utime(old_run, (old, old))
        telemetry.run_proc_dir(root)
        self.assertFalse(os.path.exists(old_run), "a run's folder untouched for PROC_KEEP_S: removed")

    def test_a_pid_used_again_is_another_process(self):
        d = os.path.join(self.tmp.name, "run")
        r = telemetry.Reporter(self.tmp.name, "1", lambda: (True, False), proc_dir=d,
                               send=lambda b: (self.sent.append(b), True)[1])
        self._proc_file(d, 999993, "aa", 40)
        r.report()
        self._proc_file(d, 999993, "bb", 5)             # (the same pid, a new process instance: counted from zero)
        r.report()
        self.assertEqual([b["sources"]["e.example.org"]["req"] for b in self.sent], [40, 5])

    def test_a_gone_process_counted_once_then_its_file_removed(self):
        import time
        d = os.path.join(self.tmp.name, "run")
        r = telemetry.Reporter(self.tmp.name, "1", lambda: (True, False), proc_dir=d,
                               send=lambda b: (self.sent.append(b), True)[1])
        self._proc_file(d, 999994, "aa", 10)
        r.report()
        self._proc_file(d, 999994, "aa", 12, at=time.time() - telemetry.PROC_KEEP_S - 5)    # (its last write)
        r.report()
        self.assertEqual(self.sent[-1]["sources"]["e.example.org"]["req"], 2, "only what changed since last seen")
        self.assertEqual(os.listdir(d), [])
        r.report()
        self.assertNotIn("e.example.org", self.sent[-1]["sources"])

    def test_dem_detail_counts_since_the_last_report(self):
        d = os.path.join(self.tmp.name, "run")
        r = telemetry.Reporter(self.tmp.name, "1", lambda: (True, False), proc_dir=d,
                               send=lambda b: (self.sent.append(b), True)[1])
        r.report()                                      # (this process's own counts so far: taken)
        self._proc_file(d, 999995, "aa", 1, detail={"mapterhorn:grading:16:net": 4, "mapterhorn:farfield:13:disk": 9})
        r.report()
        self.assertEqual(self.sent[-1]["dem"], {"mapterhorn:grading:16:net": 4, "mapterhorn:farfield:13:disk": 9})
        self._proc_file(d, 999995, "aa", 1, detail={"mapterhorn:grading:16:net": 6, "mapterhorn:farfield:13:disk": 9})
        netfetch.note_detail("mapterhorn:heightmap:16:waited")
        r.report()
        self.assertEqual(self.sent[-1]["dem"], {"mapterhorn:grading:16:net": 2, "mapterhorn:heightmap:16:waited": 1})
        r.report()
        self.assertNotIn("dem", self.sent[-1], "nothing new: no section")
        many = {f"mapterhorn:m{i}:16:net": i + 1 for i in range(telemetry.DEM_KEYS + 40)}
        self._proc_file(d, 999996, "aa", 1, detail=many)
        r.report()
        dem = self.sent[-1]["dem"]
        self.assertEqual(len(dem), telemetry.DEM_KEYS, "bounded")
        self.assertEqual(sum(dem.values()), sum(many.values()), "the rest summed under other, nothing lost")
        self.assertIn("other", dem)

    def test_dem_detail_follows_the_network_switch(self):
        sent, a = [], [{"main": True, "net": False, "perf": False}]
        r = telemetry.Reporter(self.tmp.name, "1", lambda: (True, False), send=lambda b: (sent.append(b), True)[1],
                               allowed=lambda: a[0])
        netfetch.note_detail("mapterhorn:test:16:net")
        r.report()
        self.assertNotIn("dem", sent[-1])
        a[0] = {"main": True, "net": True, "perf": False}
        r.report()
        self.assertNotIn("dem", sent[-1], "the counts made while off were dropped")


class SwitchTest(unittest.TestCase):
    def test_the_settings_page_switch_stops_reports_and_drops_their_counts(self):
        with tempfile.TemporaryDirectory() as d:
            sent, on = [], [False]
            f = netfetch.Fetcher()
            f._bump(f.host("d.example.org"), "requests", 5)
            r = telemetry.Reporter(d, "1.9.0", lambda: (True, False), send=lambda b: (sent.append(b), True)[1],
                                   allowed=lambda: on[0])
            r.report()
            self.assertEqual(sent, [])
            on[0] = True
            r.report()
            self.assertNotIn("d.example.org", sent[-1]["sources"])      # (the counts made while off were dropped)

    def test_player_settings_reads_the_switch(self):
        import cpupin
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, cpupin.MOD_SETTINGS[0]))
            path = os.path.join(d, cpupin.MOD_SETTINGS[0], cpupin.MOD_SETTINGS[1])
            with open(path, "w") as fh:
                json.dump({"telemetry": False}, fh)
            self.assertIs(cpupin.player_settings(d).get("telemetry"), False)
            with open(path, "w") as fh:
                json.dump({"telemetry": True}, fh)
            self.assertNotIn("telemetry", cpupin.player_settings(d))


class OptOutTest(unittest.TestCase):
    def test_off_by_default_for_direct_runs_and_the_launcher_switch(self):
        import earthd
        self.assertEqual(earthd.build_parser().parse_args([]).telemetry, "off") if hasattr(earthd, "build_parser") \
            else None
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import launcher
        from unittest import mock
        # (a test process never reports: the first night's 'players' were the CI's test servers)
        self.assertFalse(launcher.telemetry_on())
        with mock.patch.dict(os.environ, {"CI": "true"}):
            self.assertFalse(launcher.telemetry_on())
        # the switch itself, as a player's launcher (no unittest, no CI) sees it
        modules = {k: v for k, v in sys.modules.items() if k != "unittest"}
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "BNE_TELEMETRY")}
        with mock.patch.dict(sys.modules, modules, clear=True), mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(launcher.telemetry_on())
            os.environ["BNE_TELEMETRY"] = "off"
            self.assertFalse(launcher.telemetry_on())


class ChoiceTest(unittest.TestCase):
    """Nothing before the welcome page's choice; the sub-switches only under the main one (TELEMETRY.md)."""

    def test_choices(self):
        c = telemetry.choices
        self.assertEqual(c({}), {"main": False, "net": False, "perf": False}, "not chosen yet: nothing")
        self.assertEqual(c({"telemetryChoice": telemetry.CONSENT_VERSION}), {"main": True, "net": True, "perf": True})
        v = telemetry.CONSENT_VERSION
        self.assertFalse(c({"telemetryChoice": v, "telemetry": False})["net"], "main off: all off")
        self.assertEqual(c({"telemetryChoice": v, "telemetryNet": False, "telemetryPerf": False}),
                         {"main": True, "net": False, "perf": False})
        self.assertEqual(c({"telemetryChoice": v - 1}), {"main": False, "net": False, "perf": False},
                         "chosen under an older consent version: asked again, nothing until then")

    def test_report_follows_the_switches(self):
        with tempfile.TemporaryDirectory() as d:
            sent, a = [], [{"main": False, "net": False, "perf": False}]
            r = telemetry.Reporter(d, "1.9.0", lambda: (True, False), send=lambda b: (sent.append(b), True)[1],
                                   allowed=lambda: a[0])
            r.deltas = lambda: {"api.example.org": {"req": 3, "mb": 1000}}
            r.report()
            self.assertEqual(sent, [], "not chosen: nothing sent")
            a[0] = {"main": True, "net": False, "perf": False}
            r.report()
            self.assertEqual(sent[-1]["sources"], {}, "network load off: no per-source counts")
            a[0] = {"main": True, "net": True, "perf": False}
            r.report()
            self.assertIn("api.example.org", sent[-1]["sources"])
            with open(os.path.join(d, telemetry.LAST_FILE), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["report"], sent[-1], "the last report kept for What is sent")

    def test_no_kept_id(self):
        with tempfile.TemporaryDirectory() as d:
            a = telemetry.Reporter(d, "1", lambda: (False, False), send=lambda b: True, keep_id=False)
            b = telemetry.Reporter(d, "1", lambda: (False, False), send=lambda b: True, keep_id=False)
            self.assertNotEqual(a.id, b.id)
            self.assertFalse(os.path.exists(os.path.join(d, telemetry.ID_FILE)), "never stored")


class RegionTest(unittest.TestCase):
    def test_windows_country(self):
        # Windows names the US's and Canada's zones alike: the region setting's country, the stricter group wins
        import privacyregion
        self.assertEqual(privacyregion.decide("Eastern Standard Time", "CA"), ("perf_off", False))
        self.assertEqual(privacyregion.decide("Pacific Standard Time", "US"), ("all", False))
        self.assertEqual(privacyregion.decide("Eastern Standard Time", "DE"), ("neutral", False))
        self.assertEqual(privacyregion.decide("W. Europe Standard Time", "US"), ("neutral", False))
        self.assertEqual(privacyregion.decide("Eastern Standard Time", ""), ("all", False))
        self.assertEqual(privacyregion.decide("Eastern Standard Time", "??x"), ("all", False))
        self.assertEqual(privacyregion.decide("Russian Standard Time", "RU"), ("neutral", True))
        self.assertEqual(privacyregion.decide("America/New_York", "ru"), ("neutral", True))

    def test_groups(self):
        import privacyregion as p
        cases = {"Europe/Berlin": p.NEUTRAL, "Europe/Moscow": p.NEUTRAL, "Asia/Shanghai": p.NEUTRAL,
                 "Asia/Kolkata": p.NEUTRAL, "Europe/London": p.PERF_OFF, "America/Toronto": p.PERF_OFF,
                 "America/Sao_Paulo": p.PERF_OFF, "Asia/Seoul": p.PERF_OFF, "Europe/Istanbul": p.PERF_OFF,
                 "America/New_York": p.ALL, "Asia/Tokyo": p.ALL, "Australia/Sydney": p.ALL, "Europe/Zurich": p.ALL,
                 "W. Europe Standard Time": p.NEUTRAL, "Eastern Standard Time": p.ALL, "": p.NEUTRAL, "UTC": p.NEUTRAL}
        for z, want in cases.items():
            self.assertEqual(p.for_zone(z), want, z)
        self.assertTrue(p.is_russia("Asia/Novosibirsk") and not p.is_russia("Europe/Berlin"))

    def test_the_hello_asks_for_the_reporters_consent_version(self):
        import earthd
        self.assertEqual(earthd.TELEMETRY_CONSENT, telemetry.CONSENT_VERSION)


if __name__ == "__main__":
    unittest.main()


class PerfTest(unittest.TestCase):
    """The performance section (perfstats.py, native perfclean.rs): only named fields of the allowed shapes, only under the
    "Performance data" switch, held beside the last report while PERF_UPLOAD is off."""

    def test_checked_on_the_way_in(self):
        import perfstats
        c = perfstats.Collector(tree=lambda: None)
        c.on_setup({"cpu": "AMD Ryzen 7 5800XT\n", "res": "4k", "path": "C:/Users/me", "gameSettings": {"vsync": True,
                    "home": "/home/me"}, "modSettings": {"quality": "high", "lastPlace": "Baltimore"}})
        c.on_summary({"v": 1, "fh": [1, 2, "x"], "lat": 39.28, "note": "text", "scene": {"urban": 120, "a b": 1},
                      "bands": {"s60": {"s": 120, "fh": [0, 5, 9]}}})
        out = c.take()
        self.assertEqual(out["setup"], {"cpu": "AMD Ryzen 7 5800XT", "res": "4k", "gameSettings": {"vsync": True},
                                        "modSettings": {"quality": "high"}})
        self.assertEqual(out["game"], [{"v": 1, "fh": [1, 2], "scene": {"urban": 120},
                                        "bands": {"s60": {"s": 120, "fh": [0, 5, 9]}}}])
        again = c.take()
        self.assertNotIn("setup", again)        # (sent once, again only when it changes)
        self.assertNotIn("game", again)

    def test_version_2_summary_and_server_tree(self):
        """perfsummary.lua version 2's fields pass (the fine histogram's 0-based keys, the context's fixed names, the
        machine's memory), anything else is still dropped; the server's tree gives memory and its worker count."""
        import perfstats
        c = perfstats.Collector(tree=lambda: (5.0, 6000.0, 9))
        c.on_setup({"modSettings": {"quality": "high", "framePacing": False, "teleportPriority": True, "lastPlace": "x"}})
        c.on_summary({"v": 2, "ft": {"5": 900, "13": 3}, "p50": 16.625, "p99": 40.125, "p999": 120.5, "over2x": 3,
                      "over4x": 1, "ctx": {"swap": [3, 2, 0, 0], "C:/Users": [1, 1, 1, 1]}, "cause1s": {"FramePresent": 1},
                      "tp": {"t10": [10, 1, 0, 0, 0]}, "idle": [60, 0, 0, 0, 0],
                      "mach": {"gameMB": 6144, "freeMB": 2048, "vramMB": 8192},
                      "bands": {"s60": {"s": 300, "fh": [1, 2], "hit": [1, 1, 0, 0]}}, "where": "Baltimore"})
        out = c.take()
        g = out["game"][0]
        self.assertEqual(out["setup"]["modSettings"], {"quality": "high", "framePacing": False, "teleportPriority": True})
        self.assertEqual(g["ft"], {"5": 900, "13": 3})
        self.assertEqual(g["ctx"], {"swap": [3, 2, 0, 0]})
        self.assertEqual((g["p99"], g["mach"]["freeMB"], g["tp"]["t10"][0], g["bands"]["s60"]["hit"]), (40.125, 2048, 10, [1, 1, 0, 0]))
        self.assertNotIn("where", g)
        self.assertEqual((out["server"]["memMB"], out["server"]["workers"]), (4096, 8))
        tree = perfstats.tree_stats()           # (this process: memory and no workers, wherever it runs)
        self.assertTrue(tree is None or (tree[1] > 0 and tree[2] == 0))

    def test_catchup_and_errors(self):
        import perfstats
        t = [0.0]
        c = perfstats.Collector(clock=lambda: t[0], tree=lambda: (10.0, 3000.0))
        c.catchup(True)
        t[0] = 42.0
        c.catchup(False)
        log = logging.getLogger("perftest")
        log.addHandler(c.errors)
        try:
            for _ in range(3):
                try:
                    raise KeyError("Baltimore")
                except KeyError:
                    log.exception("oops at 39.28,-76.61")
        finally:
            log.removeHandler(c.errors)
        srv = c.take()["server"]
        self.assertEqual((srv["catchups"], srv["catchupS"], srv["memMB"]), (1, 40, 2048))
        (fp, n), = srv["errors"].items()
        self.assertEqual((len(fp), n), (8, 3))
        self.assertEqual(srv["errorCodes"], {fp: {"KeyError": 3}})
        self.assertNotIn("Baltimore", json.dumps(srv))

    def test_os_error_codes(self):
        import perfstats
        h = perfstats.ErrorPrints()
        log = logging.getLogger("perfcodes")
        log.addHandler(h)
        try:
            for err in (OSError(24, "Too many open files"), OSError(28, "No space left on device")):
                try:
                    raise err
                except OSError:
                    log.exception("far tile failed")       # (one line: one fingerprint, two codes)
        finally:
            log.removeHandler(h)
        counts, codes = h.take()
        (fp, n), = counts.items()
        self.assertEqual((n, codes[fp]), (2, {"OSError:24": 1, "OSError:28": 1}))

    def test_codes_carry_where_it_was_raised(self):
        """The server module and line an exception was raised at ride with its code (never a path or a value): the far
        tiles' errors are all logged at one line whichever cache file was damaged (2026-11-09). A worker process's
        exception carries its traceback as text (_RemoteTraceback)."""
        import concurrent.futures.process as cfp
        import json as _json
        import perfstats
        h = perfstats.ErrorPrints()
        log = logging.getLogger("perfwhere")
        log.addHandler(h)
        try:
            try:
                _json.loads("{")            # (raised in the standard library only: no server frame)
            except ValueError:
                log.exception("plain")
            remote = ValueError("bad")
            remote.__cause__ = cfp._RemoteTraceback(
                'Traceback (most recent call last):\n  File "/x/BeamNG.Earth/server/blockworker.py", line 900, in far_tile\n'
                '  File "C:\\Users\\someone\\app\\server\\colormatch.py", line 1001, in _load\nzipfile.BadZipFile: ...')
            try:
                raise remote
            except ValueError:
                log.exception("far tile failed")
        finally:
            log.removeHandler(h)
        _counts, codes = h.take()
        got = {c for cs in codes.values() for c in cs}
        self.assertIn("JSONDecodeError", got, "no server frame: the plain code")
        self.assertIn("ValueError-colormatch-1001", got, "the innermost server frame of the worker's traceback")
        self.assertFalse(any("someone" in c or "Users" in c for c in got), "never a path")

    @mock.patch.object(telemetry, "PERF_UPLOAD", "dev")
    def test_only_under_the_switch(self):
        sent = []
        with tempfile.TemporaryDirectory() as d:
            sw = {"main": True, "net": True, "perf": False}
            r = telemetry.Reporter(d, "1", lambda: (True, False), send=lambda b: (sent.append(b), True)[1],
                                   allowed=lambda: sw, perf=lambda: {"server": {"catchups": 1}})
            r.report()
            with open(os.path.join(d, telemetry.LAST_FILE), encoding="utf-8") as fh:
                self.assertNotIn("perf_not_sent_yet", json.load(fh))
            sw["perf"] = True
            r.report()
            with open(os.path.join(d, telemetry.LAST_FILE), encoding="utf-8") as fh:
                last = json.load(fh)
        self.assertTrue(all("perf" not in b for b in sent))     # (a release build: not sent yet)
        self.assertEqual(last["perf_not_sent_yet"], {"server": {"catchups": 1}})

    def test_releases_send_it_too(self):
        # (the user, 2026-11-08: "releases should be the same as dev"; still only under the player's choice)
        self.assertTrue(telemetry.perf_upload("1.9.0-beta"))
        self.assertTrue(telemetry.perf_upload("1.9.0-beta.dev.300"))
        with mock.patch.object(telemetry, "PERF_UPLOAD", "dev"):
            self.assertFalse(telemetry.perf_upload("1.9.0-beta"))

    @mock.patch.object(telemetry, "PERF_UPLOAD", "dev")
    def test_dev_builds_send_it(self):
        self.assertTrue(telemetry.perf_upload("1.8.2-beta.dev.245"))
        self.assertFalse(telemetry.perf_upload("1.8.2-beta"))
        sent = []
        with tempfile.TemporaryDirectory() as d:
            r = telemetry.Reporter(d, "1.8.2-beta.dev.245", lambda: (True, False),
                                   send=lambda b: (sent.append(b), True)[1],
                                   allowed=lambda: {"main": True, "net": True, "perf": True},
                                   perf=lambda: {"server": {"catchups": 1}})
            r.report()
            r2 = telemetry.Reporter(d, "1.8.2-beta.dev.245", lambda: (True, False),
                                    send=lambda b: (sent.append(b), True)[1],
                                    allowed=lambda: {"main": True, "net": True, "perf": False},
                                    perf=lambda: {"server": {"catchups": 1}})
            r2.report()
        self.assertEqual(sent[0]["perf"], {"server": {"catchups": 1}})
        self.assertNotIn("perf", sent[1])                       # (switch off: never)
