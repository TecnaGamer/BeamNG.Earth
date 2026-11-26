"""Stale-if-error for every cached input refreshed after a time to live (2026-11-24: offline players, a host outage and
the frozen test cache had every plan made short by the refreshes' failures, revisit.fault). Per input class: a copy
past its time to live and a failing fetch (made in the fetcher, as the real one makes its errors: revisit.fault) ->
the stale copy is used, no build is faulted, the fetch is asked again after the backoff, an answer replaces the copy;
without any copy a failure stays a missing input. pagesdata manifests: test_pagesdata.py; Mapterhorn's file list:
test_dem.CoverageTest; the switch file: test_tilepack.SwitchTest; decision / lidarz manifests:
test_decisions.StaleManifestTest."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
import netfetch  # noqa: E402
import revisit  # noqa: E402


class Site:
    """A fetcher: answer is an error class (raised from get, made there) or the body (headers: hdr)."""

    def __init__(self, answer=netfetch.Offline, hdr=None):
        self.calls, self.answer, self.hdr = [], answer, hdr or {}

    def get(self, url, kind="demand", headers=None, lane=None, max_bytes=None):
        self.calls.append(url)
        if isinstance(self.answer, type):
            raise self.answer(f"down: {url}", url, netfetch.host_of(url))
        return netfetch.Response(206 if headers and "Range" in headers else 200, self.answer, dict(self.hdr))


def aged(path, s):
    t = time.time() - s
    os.utime(path, (t, t))


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def no_fault(self, fn, *a, **k):
        """fn(*a, **k) in a build's scope: (its result); asserts nothing was faulted (the scope's nor the process's)."""
        f0 = revisit.faults()
        with revisit.scope() as b:
            got = fn(*a, **k)
        self.assertTrue(b.complete, f"the build is complete ({b.why_not()})")
        self.assertEqual(revisit.faults(), f0, "no fault")
        return got

    def short(self, fn, *a, **k):
        with revisit.scope() as b:
            try:
                got = fn(*a, **k)
            except Exception as e:      # (a missing input may raise)
                got = e
        self.assertFalse(b.complete, "no copy: the build is short, as before")
        return got


class PinnedVersionTest(Case):
    """osmtags.OsmTags / picnicsource.Source (Layercake files pinned by ETag): rangeread.pinned_version."""

    def test_stale_pin_if_error(self):
        import rangeread
        folder = os.path.join(self.dir, "osmtags")
        os.makedirs(folder)
        pin = os.path.join(folder, "pinned")
        with open(pin, "w") as f:
            f.write(f'"old"\n1234\n{time.time() - 8 * 86400}\n')
        ttl, retry = 7 * 86400, 3600
        for answer in (netfetch.Offline, netfetch.FetchFailed, b"no etag"):
            site = Site(answer)
            try:
                os.remove(os.path.join(folder, "unreachable"))
            except OSError:
                pass
            etag, size, fresh_for = self.no_fault(rangeread.pinned_version, site, "https://x/f.parquet", folder,
                                                  "demand", ttl, retry, "test")
            self.assertEqual((etag, size), ('"old"', 1234), f"{answer}: the stale pin")
            self.assertAlmostEqual(fresh_for, retry, delta=1)
            self.assertEqual(len(site.calls), 1)
        # within the backoff: not asked; past it: asked, then twice as long
        self.assertEqual(rangeread.pinned_version(site, "u", folder, "demand", ttl, retry, "t")[0], '"old"')
        self.assertEqual(len(site.calls), 1, "not before retry_s")
        aged(os.path.join(folder, "unreachable"), retry + 1)
        _, _, fresh_for = self.no_fault(rangeread.pinned_version, site, "u", folder, "demand", ttl, retry, "t")
        self.assertEqual(len(site.calls), 2)
        self.assertAlmostEqual(fresh_for, 2 * retry, delta=1)
        # an answer replaces the pin
        aged(os.path.join(folder, "unreachable"), 3 * retry)
        site.answer, site.hdr = b"PAR1", {"etag": '"new"', "content-range": "bytes 0-15/999"}
        etag, size, fresh_for = rangeread.pinned_version(site, "u", folder, "demand", ttl, retry, "t")
        self.assertEqual((etag, size, fresh_for), ('"new"', 999, ttl))
        self.assertFalse(os.path.exists(os.path.join(folder, "unreachable")))
        with open(pin) as f:
            self.assertEqual(f.read().split("\n")[:2], ['"new"', "999"])
        # no pin: a failure raises and faults, as before; the next retry_s raise at once
        os.remove(pin)
        site.answer = netfetch.Offline
        self.assertIsInstance(self.short(rangeread.pinned_version, site, "u", folder, "demand", ttl, retry, "t"),
                              netfetch.Offline)
        with self.assertRaises(rangeread.RangeError):
            rangeread.pinned_version(site, "u", folder, "demand", ttl, retry, "t")

    def test_osmtags_uses_it(self):
        import osmtags
        t = osmtags.OsmTags(self.dir, Site())
        os.makedirs(os.path.join(self.dir, t.DIR))
        with open(os.path.join(self.dir, t.DIR, "pinned"), "w") as f:
            f.write(f'"v1"\n4096\n{time.time() - osmtags.VERSION_TTL_S - 60}\n')
        etag, remote = self.no_fault(t.version)
        self.assertEqual(etag, '"v1"', "the stale pin; its blocks' folder")
        self.assertTrue(remote.cache_dir.endswith("v1"), "read from the stale version's blocks")
        self.assertEqual(remote.if_match, '"v1"')
        n = len(t.fetcher.calls)
        t.version()
        self.assertEqual(len(t.fetcher.calls), n, "asked again only after the backoff")


class NycShardsTest(Case):
    def test_stale_pin_refresh(self):
        import nycmassing
        s = nycmassing.Source(self.dir, Site(), base="https://pages.invalid/nycmassing")
        os.makedirs(s.root)
        path = os.path.join(s.root, "manifest.json")
        with open(path, "w") as f:
            json.dump({"built": "b1", "cells": {}}, f)
        aged(path, 2 * 86400)
        self.assertFalse(self.no_fault(s.pin, True), "kept, nothing pinned")
        self.assertTrue(os.path.exists(path), "the stale pin stays")
        os.remove(path)
        self.short(s.pin)


class AerialCoverageTest(Case):
    def test_stale_region_if_error(self):
        import aerialpacks
        cov = aerialpacks.Coverage(Site(), "https://cdn.invalid/aer", self.dir, request_url="off")
        r = (3, 4)
        path = cov._disk(r)
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(b"")                    # (the last answer: no file there, nothing built)
        aged(path, aerialpacks.COV_TTL_S + 60)
        import threading
        for answer in (netfetch.Offline, b"not a coverage file"):
            cov.fetcher = Site(answer)
            cov._failed.clear()
            cov._regions.clear()
            self.no_fault(cov._load, r, threading.Event())
            self.assertEqual(cov._regions[r][0], None, "the stale answer in use")
            self.assertEqual(Path(path).read_bytes(), b"", "a malformed answer does not replace the copy")
            b = cov._failed[r]
            self.assertAlmostEqual(b.until - time.time(), aerialpacks.COV_FAIL_S, delta=2)
        cov._regions[r] = (None, 0.0, 0.0)          # (due)
        self.assertEqual(cov._region(r, 0.0)[0], None, "within the backoff: the stale one, no thread")
        self.assertEqual(len(cov.fetcher.calls), 1)


class BindingsTest(Case):
    def test_stale_bindings_if_error(self):
        import mtarchive
        a = mtarchive.Archives(self.dir, Site(), {})
        os.makedirs(a.root, exist_ok=True)
        path = os.path.join(a.root, "bindings.json")
        with open(path, "w") as f:
            json.dump({"format": 1, "archives": {}}, f)
        aged(path, mtarchive.BIND_MAX_AGE_S + 60)
        with mock.patch.object(mtarchive, "BINDINGS_URL", "https://cdn.invalid/archives.json"):
            self.assertEqual(self.no_fault(a.bindings), {}, "the stale (empty) binding")
            self.assertEqual(len(a.fetcher.calls), 1)
            a._bind["at"] -= mtarchive.BIND_RETRY_S + 1         # (the time passed)
            a._bind["backoff"].until -= mtarchive.BIND_RETRY_S + 1
            self.no_fault(a.bindings)
            self.assertEqual(len(a.fetcher.calls), 2, "asked again after BIND_RETRY_S")
            self.assertAlmostEqual(a._bind["at"] + mtarchive.BIND_MAX_AGE_S - time.monotonic(),
                                   2 * mtarchive.BIND_RETRY_S, delta=2, msg="then twice as long")
            a._bind["at"] -= 2 * mtarchive.BIND_RETRY_S + 1
            a._bind["backoff"].until -= 2 * mtarchive.BIND_RETRY_S + 1
            a.fetcher.answer = json.dumps({"format": 1, "archives": {}, "new": 1}).encode()
            a.bindings()
            self.assertIn(b'"new"', Path(path).read_bytes(), "an answer replaces the copy")


class MirrorsTest(Case):
    def test_no_mirror_answering_keeps_the_order(self):
        import pmtiles
        m = pmtiles.Mirrors(self.dir, Site())
        order = list(range(len(pmtiles.MIRRORS)))[::-1]
        with open(os.path.join(self.dir, "mirrors.json"), "w") as f:
            json.dump({"t": time.time() - pmtiles.RANK_TTL_S - 60, "order": order, "mirrors": list(pmtiles.MIRRORS)}, f)
        self.assertEqual(self.no_fault(m.order, "2026-10-21.0", "demand")[:len(order)], order,
                         "the stale order; the probes faulted nothing")
        self.assertEqual(len(m.fetcher.calls), len(pmtiles.MIRRORS))


class MissMarkerTest(Case):
    """germanroads / lod2roofs: a shard the site did not have (an empty file) past MISS_TTL_S stays "none there"
    while the site cannot be asked."""

    def test_germanroads(self):
        import germanroads
        path = os.path.join(self.dir, "pagesdata", "deroads", "5_6.npz")
        os.makedirs(os.path.dirname(path))
        open(path, "wb").close()
        aged(path, germanroads.MISS_TTL_S + 60)
        state = {"cache": self.dir, "fetcher": Site(), "shards": {}, "down_at": 0.0}
        with mock.patch.object(germanroads, "_state", state):
            self.assertEqual(self.no_fault(germanroads._fetched, 5, 6), b"", "the stale miss")
            self.assertEqual(self.no_fault(germanroads._fetched, 5, 6), b"", "within RETRY_S: not asked")
            self.assertEqual(len(state["fetcher"].calls), 1)
            os.remove(path)
            self.assertIsNone(germanroads._fetched(5, 6), "no copy: unreachable, as before")

    def test_lod2roofs(self):
        import lod2roofs
        src = lod2roofs.Source.__new__(lod2roofs.Source)
        src.cache_dir, src.fetcher, src.down_at = self.dir, Site(), 0.0
        path = os.path.join(self.dir, "pagesdata", "lod2x", "5_6.npz")
        os.makedirs(os.path.dirname(path))
        open(path, "wb").close()
        aged(path, lod2roofs.MISS_TTL_S + 60)

        class Shards:
            def version(self):
                return "b"

            def get(self, rel):
                return src.fetcher.get("https://pages.invalid/lod2x/" + rel).body
        src._site = lambda folder: Shards()
        self.assertEqual(self.no_fault(src._fetched, 5, 6, "lod2x"), b"", "the stale miss")
        self.assertEqual(len(src.fetcher.calls), 1)


class ThreeDmrTest(Case):
    def test_stale_region_list(self):
        import threedmr
        import math
        lat, lon = 48.2, 16.37
        i, j = math.floor(lat / threedmr.REGION_DEG), math.floor(lon / threedmr.REGION_DEG)
        path = os.path.join(self.dir, "threedmr_cache", f"region_{i}_{j}.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            json.dump([7, 3], f)
        aged(path, threedmr.REGION_TTL_S + 60)
        calls = []

        def down(url, *a, **k):
            calls.append(url)
            raise OSError("offline")
        with mock.patch.object(threedmr, "_fetch_retry", down), mock.patch.dict(threedmr._region_backoff, clear=True), \
                mock.patch.dict(os.environ, {threedmr.ENABLE_ENV: "on"}):
            self.assertEqual(self.no_fault(threedmr.region_ids, lat, lon, self.dir), [7, 3])
            self.assertEqual(threedmr.region_ids(lat, lon, self.dir), [7, 3])
            self.assertEqual(len(calls), 1, "asked once: then only after the backoff")


class NoBuildInputTest(Case):
    """The weather and the add-ons page's data are no build's input: their failures (in the loop's default executor,
    no scope) fault nothing."""

    def test_weather(self):
        import weather
        with self.assertRaises(netfetch.Offline):
            self.no_fault(weather.fetch, 1.0, 2.0, Site())

    def test_addons(self):
        import addons
        c = addons.GitHubCache(os.path.join(self.dir, "gh"), Site(), clock=time.time)
        with self.assertRaises(addons.AddonError):
            self.no_fault(c.get, "https://api.github.com/x")


class BakeIndexTest(Case):
    def test_stale_index_if_error(self):
        import bakepack
        d = os.path.join(self.dir, bakepack.SUBDIR)
        os.makedirs(d)
        ip = os.path.join(d, "index.json")
        with open(ip, "w") as f:
            json.dump({"packs": []}, f)
        aged(ip, bakepack.INDEX_TTL_S + 60)
        calls = []

        def get(net, url, path, cap):
            calls.append(url)
            with open(path, "wb") as f:     # (a captive portal's page: not an index)
                f.write(b"<html>")
        state = {"at": -1e9, "busy": False, "backoff": None}
        with mock.patch.object(bakepack, "_get", get), mock.patch.object(bakepack, "_sync", state), \
                mock.patch.object(bakepack, "_expect_for", lambda md, sig: {"versions": {}}):
            self.assertEqual(self.no_fault(bakepack.sync, self.dir, object(), object(), 1.0, 2.0, "s", now=0.0), [])
            self.assertEqual(json.loads(Path(ip).read_text()), {"packs": []}, "the stale index kept")
            self.assertEqual(len(calls), 1)
            c = bakepack.CHECK_S
            bakepack.sync(self.dir, object(), object(), 1.0, 2.0, "s", now=c + 1)
            self.assertEqual(len(calls), 2, "asked again after CHECK_S")
            bakepack.sync(self.dir, object(), object(), 1.0, 2.0, "s", now=2 * c + 2)
            self.assertEqual(len(calls), 2, "then not before twice as long")
            bakepack.sync(self.dir, object(), object(), 1.0, 2.0, "s", now=3 * c + 2)
            self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
