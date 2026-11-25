"""The CIP planet build's pin set (tools/build_cip.py --write-pins / --pins; pagesdata.seed_session): every weekly
dataset is decided once when a build is planned, and its shards, days later and in fresh caches, read exactly those
builds from their build-addressed folders whatever Pages serves meanwhile (2026-11-24: planet run 45290114494's 333
shards refused with exit 3 once rail.yml published the next week's builds).

  - the pin set is part of the build id (another week's manifest: another build; the code-made part, plan_key, not);
  - a pinned run (and its spawned worker processes) reads the pinned build and its files, never Pages' newer one, and
    a dataset the pin set does not name is no data, asked of no one;
  - a pinned build whose files are gone fails clearly (exit 4), not as "another build" (exit 3);
  - a rerun with the build's pin set, in a fresh cache after a newer weekly build landed, is the same build id;
  - the OSM road tags (cells hold none since roads 10; the "q" records are counted with the build's tags) are the
    pinned build's: MapData.osm_tags_build and the join read it while Pages serves the next week's.
No network: stand-ins for Pages and the tile host."""
import gzip
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2] / "tools"), str(HERE.parent)]
import cip  # noqa: E402
import fast  # noqa: E402
import netfetch  # noqa: E402
import pagesdata  # noqa: E402
import railtags  # noqa: E402

HAVE = getattr(fast._ef, "VERSION", 0) >= cip.NATIVE
if HAVE:
    import build_cip  # noqa: E402

LANES = f"{pagesdata.PAGES}/lanes"
RAIL = f"{pagesdata.PAGES}/rail"
DATA = "https://tiles.test/data/lanes"
SHARD = (36, 48)                # a z7 shard


class Site:
    """Pages and the tile host: files by URL; anything else a 404."""

    def __init__(self, files=None):
        self.files, self.calls = dict(files or {}), []

    def get(self, url, kind="demand", headers=None, **_kw):
        self.calls.append(url)
        got = self.files.get(url)
        if got is None:
            raise netfetch.NotFound(url, url, netfetch.host_of(url))
        return netfetch.Response(200, got, {})


def manifest(built, data):
    return json.dumps({"format": 1, "zoom": 7, "built": built, "shards": 1, "data": data}, sort_keys=True)


def week(built, lanes):
    """A weekly lanes build: its manifest's text and its build-addressed files ({url: bytes})."""
    d = f"{DATA}/{built}"
    text = manifest(built, d)
    shard = gzip.compress(json.dumps({"ways": {"100": {"lanes": lanes}}}).encode(), mtime=0)
    return text, {f"{d}/manifest.json": text.encode(), f"{d}/{SHARD[0]}/{SHARD[1]}.json.gz": shard}


OLD, OLD_FILES = week("2026-11-17T01:00:00Z", "2")
NEW, NEW_FILES = week("2026-11-24T03:02:54Z", "4")


def child_view(cache):
    """A spawned worker process of the pinned run (the environment inherited): its lanes and rail builds and lanes'
    shard, offline (the run's decisions are the pin set's)."""
    class Never:
        def get(self, url, **_kw):
            if url.startswith(DATA):
                return Site({**OLD_FILES, **NEW_FILES}).get(url)
            raise netfetch.Offline(f"offline: {url}", url, netfetch.host_of(url))
    lanes = railtags.Shards(cache, Never(), "lanes")
    rail = railtags.Shards(cache, Never(), "rail")
    sh = lanes.shard(*SHARD)
    return {"lanes": lanes.version(), "rail": rail.version(), "shard": sh["ways"].get("100") if sh else None}


class Case(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="cippins-")
        self.addCleanup(shutil.rmtree, self.cache, True)
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(pagesdata.SESSION_ENV, None)
        pagesdata.session_reset()
        self.addCleanup(pagesdata.session_reset)

    def fresh(self):
        """Another job's cache (each CI shard starts with an empty one)."""
        os.environ.pop(pagesdata.SESSION_ENV, None)
        pagesdata.session_reset()
        d = tempfile.mkdtemp(prefix="cippins-")
        self.addCleanup(shutil.rmtree, d, True)
        return d


class PinnedSessionTest(Case):
    def test_a_pinned_run_reads_its_builds_whatever_pages_serves(self):
        site = Site({f"{LANES}/manifest.json": NEW.encode(), f"{RAIL}/manifest.json": b"{}", **OLD_FILES,
                     **NEW_FILES})
        pagesdata.seed_session(self.cache, {f"lanes|{LANES}": OLD})
        self.assertEqual(pagesdata.session_pinned(self.cache), frozenset({f"lanes|{LANES}"}))
        lanes = railtags.Shards(self.cache, site, "lanes")
        self.assertEqual(lanes.version(), "2026-11-17T01:00:00Z")
        self.assertEqual(lanes.data, f"{DATA}/2026-11-17T01:00:00Z")
        self.assertEqual(lanes.shard(*SHARD)["ways"].get("100"), {"lanes": "2"}, "the pinned build's shard")
        # (a dataset the pin set does not name: no data this run, asked of no one; and recorded for every process)
        rail = railtags.Shards(self.cache, site, "rail")
        self.assertIsNone(rail.version())
        self.assertNotIn(f"{LANES}/manifest.json", site.calls, "Pages' manifest never asked")
        self.assertNotIn(f"{RAIL}/manifest.json", site.calls)
        self.assertEqual(pagesdata.session_entries(self.cache), {f"lanes|{LANES}": OLD, f"rail|{RAIL}": None})
        # (no pin on disk fetched again for a next start: a pinned run decides nothing)
        self.assertIsNone(lanes.manifest.refresh_at)
        self.assertFalse(os.path.exists(os.path.join(self.cache, "pagesdata", "lanes", "manifest.json")))
        # a worker process of the run (spawned, as the build's pool): the same builds, offline
        with multiprocessing.get_context("spawn").Pool(1) as p:
            got = p.apply(child_view, (self.cache,))
        self.assertEqual(got, {"lanes": "2026-11-17T01:00:00Z", "rail": None, "shard": {"lanes": "2"}})

    def test_seed_refuses_what_is_not_a_manifest(self):
        with self.assertRaises(ValueError):
            pagesdata.seed_session(self.cache, {f"lanes|{LANES}": {"built": "x"}})


class FakeMD:
    """The map data a Builder reads its weekly builds from (its rail / lane shards; no OSM tag shards)."""

    def __init__(self, cache, fetcher):
        self.cache_dir, self.fetcher = cache, fetcher
        self.rail_tags = railtags.Shards(cache, fetcher, "rail")
        self.lane_tags = railtags.Shards(cache, fetcher, "lanes")

    def freeze_manifests(self, wait_s=None):
        for t in (self.rail_tags, self.lane_tags):
            t.version()
        return lambda: None

    def _osm_shards(self):
        return None

    def osm_tags_build(self, network=True):
        return "layercake"


@unittest.skipUnless(HAVE, "needs earthfast with cip")
class BuildIdTest(Case):
    def setUp(self):
        super().setUp()
        self.site = Site({f"{LANES}/manifest.json": OLD.encode(), **OLD_FILES, **NEW_FILES})
        site = self.site

        class Fake(build_cip.Builder):
            def __init__(self, cache, online, layers, release=None):
                self.cache, self.online, self.layers = cache, online, layers
                self.md = FakeMD(cache, site)
                self.release = "2026-11-18.0"
                self.store, self.stats, self.partial, self.lidar = True, {}, True, "skip"
                self.only, self.lidar_cells, self.cap, self.quality = None, None, 0, False
        self.enterContext(mock.patch.object(build_cip, "Builder", Fake))
        self.out = tempfile.mkdtemp(prefix="cippins-out-")
        self.addCleanup(shutil.rmtree, self.out, True)

    def run_main(self, cache, *extra):
        out = []
        self.err = []
        with mock.patch("builtins.print", lambda *a, **k: (self.err if "file" in k else out).append(a[0] if a else "")):
            rc = build_cip.main(["--cache", cache, "--out", self.out, "--world", "--scope", "content", "--online",
                                 "--lidar", "skip", "--quality", "off", "--inputs-only", *extra])
        return rc, (json.loads(out[-1]) if rc == 0 else None)

    def test_the_pin_set_is_in_the_build_id(self):
        inputs = {"code": {"cip": [1, 2]}, "data": {"lanes": "a", "lidar": "skip"}}
        a = dict(inputs, data=dict(inputs["data"], pins=build_cip.pin_summary({f"lanes|{LANES}": OLD})))
        b = dict(inputs, data=dict(inputs["data"], pins=build_cip.pin_summary({f"lanes|{LANES}": NEW})))
        c = dict(inputs, data=dict(inputs["data"], pins=build_cip.pin_summary({f"lanes|{LANES}": None})))
        self.assertEqual(len({cip.build_id(x) for x in (a, b, c)}), 3)
        self.assertEqual(len({build_cip.plan_key(x) for x in (a, b, c)}), 1, "the code and switches alone")
        self.assertEqual(a["data"]["pins"]["lanes"]["data"], f"{DATA}/2026-11-17T01:00:00Z")
        with self.assertRaises(ValueError):         # (two datasets of one name)
            build_cip.pin_summary({f"lanes|{LANES}": OLD, "lanes|https://elsewhere.test/lanes": OLD})

    def test_a_rerun_with_the_pin_set_is_the_same_build(self):
        pins = os.path.join(self.out, "pins.json")
        rc, first = self.run_main(self.cache, "--write-pins", pins)
        self.assertEqual(rc, 0)
        with open(pins, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["build"], first["build"])
        self.assertEqual(doc["datasets"], {f"lanes|{LANES}": OLD, f"rail|{RAIL}": None})   # (rail: not published)
        self.assertEqual(first["inputs"]["data"]["lanes"], "2026-11-17T01:00:00Z")
        # days later: a newer weekly build on Pages; a shard (a fresh cache) with the pin set is the same build
        self.site.files[f"{LANES}/manifest.json"] = NEW.encode()
        self.site.files[f"{RAIL}/manifest.json"] = manifest("2026-11-24T01:27:18Z", "https://tiles.test/data/rail/x")
        rc, again = self.run_main(self.fresh(), "--pins", pins)
        self.assertEqual(rc, 0)
        self.assertEqual(again["build"], first["build"])
        self.assertEqual(again["inputs"], first["inputs"])
        # without the pin set: the week's builds as served now, another build (what made the 333 shards refuse)
        rc, other = self.run_main(self.fresh())
        self.assertNotEqual(other["build"], first["build"])
        self.assertEqual(other["plan_key"], first["plan_key"], "the same code and switches: the pointer's key")
        self.assertEqual(other["inputs"]["data"]["lanes"], "2026-11-24T03:02:54Z")

    def test_a_missing_pinned_build_fails_clearly(self):
        pins = os.path.join(self.out, "pins.json")
        self.assertEqual(self.run_main(self.cache, "--write-pins", pins)[0], 0)
        for url in OLD_FILES:                        # (its folder gone from the tile host)
            del self.site.files[url]
        rc, _ = self.run_main(self.fresh(), "--pins", pins)
        self.assertEqual(rc, build_cip.EXIT_UNAVAILABLE)
        self.assertNotEqual(rc, 3)
        self.assertIn("pinned build unavailable", " ".join(self.err))
        # a plan whose dataset's build is not in a folder of its own fails at the plan, not in 200 shards
        self.site.files[f"{LANES}/manifest.json"] = manifest("2026-11-17T01:00:00Z", None).encode()
        rc, _ = self.run_main(self.fresh(), "--write-pins", pins + "2")
        self.assertEqual(rc, 0, "no data folder: Pages serves the build, still the same")
        self.site.files[f"{LANES}/manifest.json"] = NEW.encode()
        rc, _ = self.run_main(self.fresh(), "--pins", pins + "2")
        self.assertEqual(rc, build_cip.EXIT_UNAVAILABLE, "Pages serves another build and the pin names no folder")

    def test_another_pages_base_is_not_the_pin_set(self):
        """A shard run against another BNE_DATA_PAGES than its plan reads datasets the pin set does not name: refused
        (exit 4), never built as "no data"."""
        pins = os.path.join(self.out, "pins.json")
        self.assertEqual(self.run_main(self.cache, "--write-pins", pins)[0], 0)
        with open(pins, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["datasets"] = {k.replace(pagesdata.PAGES, "https://other.test"): v for k, v in doc["datasets"].items()}
        with open(pins, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        with mock.patch.object(sys, "stderr", SimpleNamespace(write=lambda s: None, flush=lambda: None)):
            rc, _ = self.run_main(self.fresh(), "--pins", pins)
        self.assertEqual(rc, build_cip.EXIT_UNAVAILABLE)

    def test_a_late_manifest_fails_the_plan(self):
        """A plan whose dataset could not be had (not a 404) is refused: never a whole planet build without it."""
        site = self.site

        class Down(Site):
            def get(self, url, kind="demand", headers=None, **_kw):
                if url == f"{RAIL}/manifest.json":
                    raise netfetch.FetchFailed(url + ": HTTP 522", url, netfetch.host_of(url))
                return site.get(url, kind, headers)
        down = Down()
        with mock.patch.object(FakeMD, "__init__", lambda s, c, f: (
                setattr(s, "cache_dir", c), setattr(s, "fetcher", down),
                setattr(s, "rail_tags", railtags.Shards(c, down, "rail")),
                setattr(s, "lane_tags", railtags.Shards(c, down, "lanes")), None)[-1]), \
                mock.patch.object(pagesdata, "FETCH_WAIT_S", 2.0), \
                mock.patch.object(sys, "stderr", SimpleNamespace(write=lambda s: None, flush=lambda: None)):
            rc, _ = self.run_main(self.cache, "--write-pins", os.path.join(self.out, "p.json"))
        self.assertEqual(rc, build_cip.EXIT_UNAVAILABLE)


try:
    import test_osm_tag_shards as ts
    import test_osmtagsjoin as tj
except Exception:               # (no DuckDB / pyarrow: skipped below)
    ts = tj = None


@unittest.skipIf(not HAVE or ts is None or ts.duckdb is None, "needs earthfast with cip and DuckDB")
class PinnedOsmTagsTest(tj.JoinAtDecodeTest if tj is not None else unittest.TestCase):
    """The roads' OSM tags (joined at the decode since roads 10; the builder's "q" records are counted with them and
    readers use a record only while their run's tag build is the build's inputs.data.osmtags) are the pinned build's,
    whatever Pages serves."""

    def run_pinned(self, week, pages):
        self.end_run()
        files = dict(self.files)
        files[f"{tj.BASE}/manifest.json"] = pages.manifest
        pagesdata.seed_session(self.cache, {f"osmtags|{tj.BASE}": abs_manifest(week)})
        t = tj.mf.Transport(files)
        md = tj.mapdata.MapData(self.cache, tj.mf.fetcher(t), tj.mf.RELEASE)
        self.addCleanup(md.close)
        md.rail_tags = md.lane_tags = md.station_tags = None
        md.layer_arrays = lambda key, layer, kind="demand": (dict(self.files_by_cell[tj.mapdata._key(key)]), True)
        md.osmtags = mock.Mock(cell=mock.Mock(side_effect=OSError("no Layercake here")),
                               highway_geometry=lambda *a, **k: {})
        return md, t

    def test_the_pinned_tags_whatever_pages_serves(self):
        md, t = self.run_pinned(self.w1, pages=self.w2)
        self.assertEqual(md.osm_tags_build(), "W1")
        joined, ok = md.roads_joined_arrays(tj.CELL_A, self.files_by_cell[tj.CELL_A])
        self.assertTrue(ok)
        i = list(joined["attr.src_record"]).index(f"w{tj.LONG}@1")
        self.assertEqual(float(joined["attr.osm_lanes"][i]), 3.0, "W1's tags, not Pages' W2")
        self.assertEqual(t.count(f"{tj.BASE}/manifest.json"), 0)
        b = SimpleNamespace(md=md)
        build_cip.check_pins(b, {f"osmtags|{tj.BASE}"})      # (its part answers: no raise)
        self.assertEqual(build_cip.Builder.data_builds(SimpleNamespace(md=md, online=True))["osmtags"], "W1")

    # (the inherited tests run unpinned runs: not again here)
    test_cells_of_different_weeks_join_the_runs_build = None
    test_a_join_of_the_same_build_is_the_same = None
    test_the_decoded_roads_key_holds_the_build = None
    test_shards_unreachable_make_a_stand_in = None
    test_the_layercake_file_stands_in_for_a_down_tile = None
    test_a_run_without_a_build_reads_the_layercake_file_keyed_so = None
    test_decisions_slices_hold_the_tags = None
    test_versions_vkey_and_slices = None


def abs_manifest(week):
    """week's manifest with its archive an absolute URL (the published builds' layout: <name>/<key>/ parts)."""
    m = json.loads(week.manifest)
    m["archive"] = f"{tj.BASE}/{m['archive']}"
    return json.dumps(m)


if __name__ == "__main__":
    unittest.main()
