"""One build of every weekly dataset per server run (pagesdata's session, 2026-11-24; ~/bnge-tests/manifestaudit
REPORT.md R1-R6): the main process decides each manifest before its workers start, every worker process (one started
later too: a respawn, an elastic extra) reads the same decision from the session folder its environment names, a
background refresh pins a newer build for the next start only, Pages' files are kept under the run's build only while
Pages serves that build, plans are kept only under the versions they were planned with, in a run folder of those
versions, and a layer in memory is joined on a copy, never on the object other threads read. No network: stand-ins
for Pages and the tile host; worker processes are real (multiprocessing spawn, as the server's pools)."""
import gzip
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import netfetch  # noqa: E402
import pagesdata  # noqa: E402
import railtags  # noqa: E402
import regionaldata  # noqa: E402
import revisit  # noqa: E402

LANES = f"{pagesdata.PAGES}/lanes"
REGIONAL = f"{pagesdata.PAGES}/regional"
STALE = pagesdata.MANIFEST_TTL_S + 600


class Site:
    """Pages and the tile host: files by URL (bytes, or (bytes, headers)); anything else a 404; down: URL prefixes
    that fail (HTTP 522)."""

    def __init__(self, files=None, down=()):
        self.files, self.down, self.calls, self.lock = dict(files or {}), tuple(down), [], threading.Lock()

    def get(self, url, kind="demand", **_kw):
        with self.lock:
            self.calls.append(url)
        if url.startswith(self.down):
            raise netfetch.FetchFailed(url + ": HTTP 522", url, netfetch.host_of(url))
        got = self.files.get(url)
        if got is None:
            raise netfetch.NotFound(url, url, netfetch.host_of(url))
        body, hdr = got if isinstance(got, tuple) else (got, {})
        return netfetch.Response(200, body, dict(hdr))


class Offline:
    """A fetcher that never answers (a worker must not need the network for the run's builds)."""

    def get(self, url, kind="demand", **_kw):
        raise netfetch.Offline(f"offline: {url}", url, netfetch.host_of(url))


def manifest(built, data=None):
    m = {"format": 1, "zoom": 7, "built": built, "files": {}}
    if data:
        m["data"] = data
    return json.dumps(m).encode()


def pin(cache, name, built, age=0.0, data=None):
    d = os.path.join(cache, "pagesdata", name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "manifest.json")
    with open(p, "wb") as fh:
        fh.write(manifest(built, data))
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def pinned(cache, name):
    with open(os.path.join(cache, "pagesdata", name, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)["built"]


def fresh_regional(cache, fetcher):
    regionaldata._state.update(cache=None, fetcher=None, pages=None, cells={})
    regionaldata.configure(cache, fetcher)


def child_builds(cache, session):
    """A worker process's view (spawned: imports fresh, the environment inherited; session False: started without
    the run's session folder, as a process of another run)."""
    if not session:
        os.environ.pop(pagesdata.SESSION_ENV, None)
    lanes = railtags.LaneTags(cache, Offline())
    fresh_regional(cache, Offline())
    return {"lanes": lanes.version(), "lanes_known": lanes.known(), "regional": regionaldata.signature(),
            "session": os.environ.get(pagesdata.SESSION_ENV)}


def child_put(cache, body, at):
    """Decide lanes' manifest at wall time at (two processes racing): the decision this process then uses."""
    time.sleep(max(0.0, at - time.time()))
    got, _mine = pagesdata.session_put(cache, f"lanes|{LANES}", body, "race")
    return got


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="manifestfreeze-")
        self.addCleanup(shutil.rmtree, self.cache, True)
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(pagesdata.SESSION_ENV, None)
        pagesdata.session_reset()
        self.addCleanup(pagesdata.session_reset)
        saved = dict(regionaldata._state)
        self.addCleanup(regionaldata._state.update, saved)
        self.ctx = multiprocessing.get_context("spawn")

    def spawn(self, fn, *args):
        """fn(*args) in a new spawned process (a worker started now: a respawn, an elastic extra)."""
        with self.ctx.Pool(1) as p:
            return p.apply(fn, args)

    def wait_pinned(self, name, built, s=5.0):
        t0 = time.time()
        while time.time() - t0 < s:
            try:
                if pinned(self.cache, name) == built:
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.02)
        self.fail(f"{name}: {built} not pinned")


class MidSessionRefreshTest(SessionCase):
    """R5 / R6: a stale pin's refresh during a session changes no process's build, the main's nor a worker's, one
    started before or after the refresh; it is the next start's."""

    def test_no_process_of_the_run_takes_the_refreshed_build(self):
        pin(self.cache, "lanes", "A", STALE)
        pin(self.cache, "regional", "A", STALE)
        sdir = pagesdata.start_session(self.cache)
        self.assertEqual(os.environ[pagesdata.SESSION_ENV], sdir)
        before = self.spawn(child_builds, self.cache, True)     # (a worker of the pools: before the refresh came)
        site = Site({f"{LANES}/manifest.json": manifest("B"), f"{REGIONAL}/manifest.json": manifest("B")})
        lanes = railtags.LaneTags(self.cache, site)             # (the main process: refreshes its stale pins)
        fresh_regional(self.cache, site)
        self.assertEqual(lanes.version(), "A")
        self.assertEqual(regionaldata.signature(), "A")
        self.wait_pinned("lanes", "B")
        self.wait_pinned("regional", "B")
        time.sleep(0.05)
        self.assertEqual((lanes.version(), lanes.known()), ("A", "A"), "the main process keeps the run's build")
        self.assertEqual(regionaldata.signature(), "A", "regionaldata (recheck=False) stays frozen: R6")
        self.assertEqual(railtags.LaneTags(self.cache, site).version(), "A", "a reader made later in the process")
        after = self.spawn(child_builds, self.cache, True)      # (a respawned / elastic worker: after the refresh)
        for got in (before, after):
            self.assertEqual(got["session"], sdir, "the session folder inherited")
            self.assertEqual((got["lanes"], got["lanes_known"], got["regional"]), ("A", "A", "A"), got)
        other = self.spawn(child_builds, self.cache, False)     # (no session: the next start's view)
        self.assertEqual((other["lanes"], other["regional"]), ("B", "B"), "the newer pin is the next start's")

    def test_a_worker_decides_nothing_the_main_process_decided(self):
        """The workers read the main process's decisions (freeze_manifests before the pools), even when the pin on
        disk is replaced under them (another server run of the same cache pinning a newer build)."""
        pin(self.cache, "lanes", "A")
        pin(self.cache, "regional", "A")
        pagesdata.start_session(self.cache)
        self.assertEqual(railtags.LaneTags(self.cache, Offline()).version(), "A")
        fresh_regional(self.cache, Offline())
        self.assertEqual(regionaldata.signature(), "A")
        pin(self.cache, "lanes", "C")
        pin(self.cache, "regional", "C")
        got = self.spawn(child_builds, self.cache, True)
        self.assertEqual((got["lanes"], got["regional"]), ("A", "A"), got)

    def test_a_first_run_without_an_answer_is_none_for_the_whole_run(self):
        """No pin, no manifest in FETCH_WAIT_S: none for the run in every process, even once a pin is there."""
        pagesdata.start_session(self.cache)
        lanes = railtags.LaneTags(self.cache, Offline())
        self.assertIsNone(lanes.version())
        pin(self.cache, "lanes", "A")
        self.assertIsNone(lanes.version(), "none: decided for the run")
        self.assertIsNone(lanes.known())
        got = self.spawn(child_builds, self.cache, True)
        self.assertIsNone(got["lanes"], "a worker: none too")
        self.assertIsNone(got["lanes_known"])

    def test_a_first_run_takes_the_manifest_that_came(self):
        pagesdata.start_session(self.cache)
        site = Site({f"{LANES}/manifest.json": manifest("A")})
        self.assertEqual(railtags.LaneTags(self.cache, site).version(), "A")
        self.assertEqual(pinned(self.cache, "lanes"), "A", "pinned")
        self.assertEqual(self.spawn(child_builds, self.cache, True)["lanes"], "A")

    def test_racing_processes_take_one_decision(self):
        pagesdata.start_session(self.cache)
        at = time.time() + 1.5
        with self.ctx.Pool(2) as p:
            a = p.apply_async(child_put, (self.cache, manifest("A").decode(), at))
            b = p.apply_async(child_put, (self.cache, manifest("B").decode(), at))
            got = {a.get(30), b.get(30)}
        self.assertEqual(len(got), 1, got)
        self.assertEqual(pagesdata.session_get(self.cache, f"lanes|{LANES}"), (True, got.pop()))

    def test_new_yorks_shards_one_build_a_run(self):
        """nycshards (its own manifests): decided once a run in any process; the main process's peek (its guess of the
        workers' keys) is that decision, not a newer pin."""
        import nyctanks
        base = "https://example.invalid/nyctanks/v1"
        d = os.path.join(self.cache, "pagesdata", "nyctanks", "v1")
        os.makedirs(d)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"format": 1, "built": "2026-11-10", "cells": {}}, f)
        pagesdata.start_session(self.cache)
        a = nyctanks.Source(self.cache, fetcher=Offline(), base=base).identity()
        self.assertTrue(a.startswith("2026-11-10-"), a)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"format": 1, "built": "2026-11-20", "cells": {}}, f)
        pagesdata.session_reset()                   # (another process of the run: the session folder only)
        self.assertEqual(nyctanks.Source(self.cache, fetcher=Offline(), base=base).identity(peek=True), a)
        self.assertEqual(nyctanks.Source(self.cache, fetcher=Offline(), base=base).identity(), a)

    def test_session_folders_of_other_caches_are_not_read(self):
        other = tempfile.mkdtemp(prefix="manifestfreeze-other-")
        self.addCleanup(shutil.rmtree, other, True)
        pagesdata.start_session(other)
        self.assertIsNone(pagesdata.session_dir(self.cache))
        self.assertIsNotNone(pagesdata.session_dir(other))

    def test_a_helper_keeps_the_session_it_inherited(self):
        """build_decisions, bake_area, the BeamMP share helper: the server's session when they are its helpers, else
        one of their own."""
        d = pagesdata.ensure_session(self.cache)
        self.assertEqual(pagesdata.ensure_session(self.cache), d, "inherited: kept")
        other = tempfile.mkdtemp(prefix="manifestfreeze-other-")
        self.addCleanup(shutil.rmtree, other, True)
        self.assertNotEqual(pagesdata.ensure_session(other), d, "another cache: its own")

    def test_old_session_folders_pruned_this_runs_kept(self):
        old = pagesdata.start_session(self.cache)
        t = time.time() - pagesdata.SESSION_KEEP_S - 60
        os.utime(old, (t, t))
        new = pagesdata.start_session(self.cache)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(new))

    def test_the_cache_never_evicts_a_runs_decisions(self):
        import cache as cachemod
        c = cachemod.Cache.__new__(cachemod.Cache)
        c._lock = threading.Lock()
        c._entries = {"pagesdata/session/1_2/lanes-x.json": [300, 0.0], "pagesdata/lanes/A/1_2.json.gz": [300, 0.0]}
        c.root, c._used, c._dirty, c.on_change = self.cache, 600, False, None
        for k in c._entries:
            os.makedirs(os.path.dirname(c._path(k)), exist_ok=True)
            with open(c._path(k), "wb") as fh:
                fh.write(b"x" * 300)
        self.assertIsNone(c._remove("pagesdata/session/1_2/lanes-x.json", 1.0, True, True))
        self.assertEqual(c._remove("pagesdata/lanes/A/1_2.json.gz", 1.0, True, True), 300)


class PagesBuildTest(SessionCase):
    """R3: a shard is kept under the run's build only when it is of that build: from the build's data host, or from
    Pages while Pages' manifest names the run's build (and the file is of that manifest's deploy)."""

    SHARD = gzip.compress(b'{"ways": {}, "nodes": []}')
    DATA = "https://tiles.test/data/lanes/A-1"

    def kept(self, built="A"):
        d = os.path.join(self.cache, "pagesdata", "lanes", built)
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_a_newer_pages_build_is_not_filed_under_the_runs(self):
        pin(self.cache, "lanes", "A", STALE)            # (the run's: a stale pin; Pages serves B already)
        site = Site({f"{LANES}/manifest.json": manifest("B"), f"{LANES}/1/2.json.gz": self.SHARD})
        lanes = railtags.LaneTags(self.cache, site)
        self.assertEqual(lanes.version(), "A")
        with revisit.scope() as b:
            self.assertIsNone(lanes.shard(1, 2), "B's shard: not had in this run")
        self.assertFalse(b.complete, "a fault: what is built without it is not kept")
        self.assertEqual(self.kept(), [], "nothing kept under A")
        self.assertNotIn(f"{LANES}/1/2.json.gz", site.calls, "not even fetched")

    def test_pages_of_the_runs_build_is_read(self):
        pin(self.cache, "lanes", "A")
        site = Site({f"{LANES}/manifest.json": manifest("A"), f"{LANES}/1/2.json.gz": self.SHARD})
        lanes = railtags.LaneTags(self.cache, site)
        self.assertEqual(lanes.shard(1, 2)["nodes"], [])
        self.assertTrue(any(n.startswith("1_2.json.gz") for n in self.kept()))
        self.assertEqual(lanes.shard(5, 5), {}, "a 404 of Pages serving the run's build: none there")

    def test_a_file_of_another_pages_deploy_is_not_read(self):
        """Pages' CDN can answer an older manifest beside a newer deploy's file: their Last-Modified tell."""
        pin(self.cache, "lanes", "A")
        lm = {"last-modified": "Tue, 24 Nov 2026 06:02:40 GMT"}
        later = {"last-modified": "Wed, 25 Nov 2026 06:02:40 GMT"}
        site = Site({f"{LANES}/manifest.json": (manifest("A"), lm), f"{LANES}/1/2.json.gz": (self.SHARD, later),
                     f"{LANES}/1/3.json.gz": (self.SHARD, {"last-modified": "Tue, 24 Nov 2026 06:02:41 GMT"})})
        lanes = railtags.LaneTags(self.cache, site)
        self.assertIsNone(lanes.shard(1, 2), "another deploy's file")
        self.assertEqual(sum(u.endswith("/manifest.json") for u in site.calls), 2, "the manifest looked at again")
        self.assertIsNotNone(lanes.shard(1, 3), "the same deploy's")
        self.assertEqual([n for n in self.kept() if n.endswith(".json.gz")], ["1_3.json.gz"])

    def test_the_data_host_failing_reads_pages_only_of_the_runs_build(self):
        pin(self.cache, "lanes", "A", STALE, data=self.DATA)
        site = Site({f"{LANES}/manifest.json": manifest("B"), f"{LANES}/1/2.json.gz": self.SHARD,
                     f"{self.DATA}/1/2.json.gz": self.SHARD}, down=("https://tiles.test",))
        lanes = railtags.LaneTags(self.cache, site)
        self.assertEqual(lanes.version(), "A")
        self.assertIsNone(lanes.shard(1, 2))
        self.assertEqual(self.kept(), [])
        site.down = ()                                  # (the tile host back: the build's own copy)
        lanes.shard_down.clear()
        self.assertIsNotNone(lanes.shard(1, 2))
        self.assertTrue(self.kept())

    def test_the_data_host_is_read_without_asking_pages(self):
        pin(self.cache, "lanes", "A", data=self.DATA)
        site = Site({f"{self.DATA}/1/2.json.gz": self.SHARD})
        lanes = railtags.LaneTags(self.cache, site)
        self.assertIsNotNone(lanes.shard(1, 2))
        self.assertEqual(site.calls, [f"{self.DATA}/1/2.json.gz"])


class PlanVersionsTest(unittest.TestCase):
    """R1 / R2: a run's plans sit in a folder of the map data's versions, and a plan begun under some versions is
    never kept under others."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.environ["BNGE_PLAN_RUN"] = "test"
        self.addCleanup(os.environ.pop, "BNGE_PLAN_RUN", None)

    def grader(self, md):
        import grading
        import plancache
        from test_roads import FRAME
        g = grading.Grader(FRAME, md, lambda la, lo: 10.0 + 0.0 * np.asarray(la))
        g.shared = plancache.for_grader(self.dir, g, None, None)
        return g

    def md(self, versions):
        from test_grading import FakeMD
        from test_roads import features
        md = FakeMD(features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"]))
        md.v = dict(versions)
        md.versions = lambda: dict(md.v)
        return md

    def test_the_run_folder_names_the_versions(self):
        a = self.grader(self.md({"lanes": "A"})).shared
        b = self.grader(self.md({"lanes": "B"})).shared
        a2 = self.grader(self.md({"lanes": "A"})).shared
        self.assertNotEqual(a.dir, b.dir)
        self.assertEqual(a.dir, a2.dir)
        self.assertEqual(os.path.dirname(a.dir), os.path.join(self.dir, "plans", "test", "r10"))
        self.assertTrue(os.path.basename(a.dir).startswith("w0s0.v"))

    def test_a_version_change_while_planning_is_not_kept(self):
        import grading
        import revisit as rv
        from test_roads import FRAME
        md = self.md({"lanes": "A"})
        g = self.grader(md)
        stored = []
        orig = grading._Planner._make_cell

        def make(self_, key):
            got = orig(self_, key)
            md.v = {"lanes": "B"}                       # (the versions changed while the cell was planned)
            return got
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 20), np.linspace(-30, 60, 20), 0.0)
        with mock.patch.object(grading._Planner, "_make_cell", make), \
                mock.patch.object(rv, "store", lambda *a, **k: stored.append(a[2])):
            g.apply(la, lo, np.zeros(20))
        self.assertEqual(stored, [], "nothing kept in the revisit cache")
        files = [n for n in os.listdir(g.shared.dir) if n.endswith(".npz")]
        self.assertTrue(files)
        self.assertTrue(all(os.path.exists(os.path.join(g.shared.dir, n + ".short")) for n in files),
                        "short for the run's other processes")
        self.assertFalse(any(g.shared.known_complete(tuple(int(v) for v in n[:-4].split("_"))) for n in files))
        self.assertFalse(grading.plans_complete([tuple(int(v) for v in n[:-4].split("_")) for n in files]),
                         "and short in this process")

    def test_unchanged_versions_are_kept(self):
        import grading
        import revisit as rv
        from test_roads import FRAME
        g = self.grader(self.md({"lanes": "A"}))
        stored = []
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 20), np.linspace(-30, 60, 20), 0.0)
        with mock.patch.object(rv, "store", lambda *a, **k: stored.append(a[2])):
            g.apply(la, lo, np.zeros(20))
        self.assertTrue(stored, "kept")
        cells = [tuple(int(v) for v in n[:-4].split("_")) for n in os.listdir(g.shared.dir) if n.endswith(".npz")]
        want = self.grader(self.md({"lanes": "A"})).shared
        self.assertEqual(sorted(stored), sorted(want._revisit_key(k) for k in cells), "under the versions planned with")
        del grading


class CopyOnJoinTest(unittest.TestCase):
    """R4: a roads layer in memory whose lane join was not made at its decode (its shard unreachable) is joined on a
    copy that takes its place in memory; the object other threads hold is never changed."""

    def test_joined_on_a_copy(self):
        import mapdata
        import mapcells
        from test_roads import features
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        f.attrs["src_record"] = np.array(["w21@3"])
        key = mapcells.CellKey(14, 4000, 6000)

        class Lanes:
            name = "lanes"

            def version(self):
                return "A"

            def join(self, feats, k):
                feats.attrs["osm_lanes"] = np.array([2], object)
        md = SimpleNamespace(rail_tags=None, lane_tags=Lanes(), drive_tags=None, _mem_lock=threading.Lock())
        for n in ("_joins_due", "_rails", "_lanes", "_drive", "_joined"):
            setattr(md, n, getattr(mapdata.MapData, n).__get__(md))
        entry = (f, {}, 10)
        md._mem = {(str(key), "roads"): entry}
        before = dict(f.attrs)
        got = md._joined(key, "roads", f)
        self.assertIsNot(got, f)
        self.assertEqual(f.attrs.keys(), before.keys(), "the shared object untouched")
        self.assertNotIn("_lanes_built", f.__dict__)
        self.assertEqual(got.__dict__["_lanes_built"], "A")
        self.assertEqual(list(got.attrs["osm_lanes"]), [2])
        self.assertIs(md._mem[(str(key), "roads")][0], got, "the copy in memory in its place")
        self.assertIs(md._joined(key, "roads", got), got, "joined: the layer itself, no copy")


if __name__ == "__main__":
    unittest.main()
