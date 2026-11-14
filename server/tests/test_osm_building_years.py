"""OSM building construction years (tools/build_osm_building_years.py -> PMTiles parts; server/osmyears.py + earthfast
buildyears.rs read a tile): the builder's year parse on the values OSM has most, a small Layercake-like file built
and read back through a fake host, a building on a tile edge, relations' keys, damaged tiles, and when a tile's
buildings fault (unreachable) or not (not published).

No network: the parquet file is written here (DuckDB), the archive served by a fake fetcher."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2] / "tools")]
import build_osm_building_years as by  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import netfetch  # noqa: E402
import osmyears  # noqa: E402
import revisit  # noqa: E402

try:
    import duckdb
except ImportError:
    duckdb = None

BASE = "https://years.test/osmyears"
LAT0, LON0 = 40.7484, -73.9857


class FakeFetcher:
    """Range reads of in-memory files by URL (whole files without a Range), one ETag; fail: raised for every get."""

    def __init__(self, files):
        self.files, self.fail, self.seen = files, None, []

    def get(self, url, *, kind="demand", headers=None, lane=None):
        self.seen.append((url.rsplit("/", 1)[-1], (headers or {}).get("Range")))
        if self.fail is not None:
            raise self.fail
        body = self.files.get(url)
        if body is None:
            raise netfetch.NotFound(f"{url}: 404", url, netfetch.host_of(url), status=404)
        if not headers or "Range" not in headers:
            return netfetch.Response(200, body, {})
        a, b = headers["Range"].split("=")[1].split("-")
        a, b = int(a), min(int(b), len(body) - 1)
        return netfetch.Response(206, body[a:b + 1], {"etag": '"y1"', "content-range": f"bytes {a}-{b}/{len(body)}"})


def write_parquet(path, rows, group_rows=8):
    """rows [(type, id, start_date, (x0, y0, x1, y1))] as Layercake's buildings file has them."""
    con = duckdb.connect()
    con.execute("CREATE TABLE b (type VARCHAR, id BIGINT, start_date VARCHAR, "
                "bbox STRUCT(xmin FLOAT, ymin FLOAT, xmax FLOAT, ymax FLOAT))")
    for t, i, s, bb in rows:
        con.execute("INSERT INTO b VALUES (?, ?, ?, ?)",
                    [t, i, s, dict(xmin=bb[0], ymin=bb[1], xmax=bb[2], ymax=bb[3])])
    con.execute(f"COPY b TO '{path}' (FORMAT PARQUET, ROW_GROUP_SIZE {group_rows})")
    con.close()


def z12_edge_lon():
    """A z12 tile's west edge near LON0 (a building across it is in both tiles)."""
    n = 1 << by.SHARD_ZOOM
    x = int((LON0 + 180.0) / 360.0 * n)
    return x / n * 360.0 - 180.0


def box(lon, lat, d=0.0003):
    return (lon - d, lat - d, lon + d, lat + d)


EDGE = z12_edge_lon()
ROWS = [
    ("way", 34633854, "1931", box(LON0, LAT0)),                   # the Empire State Building
    ("way", 42500770, "1930", box(LON0 + 0.01, LAT0 + 0.004)),    # Chrysler
    ("relation", 3565284, "c. 1890", box(LON0 - 0.002, LAT0)),    # a relation: its own key
    ("way", 3565284, "2001", box(LON0 - 0.002, LAT0 + 0.001)),    # the same number as a way
    ("way", 5, "1913..1915", (EDGE - 0.001, LAT0, EDGE + 0.001, LAT0 + 0.001)),   # across a tile edge
    ("way", 6, "unknown", box(LON0, LAT0 + 0.002)),               # no year: left out
    ("way", 7, None, box(LON0, LAT0 + 0.003)),                     # no start_date
    ("node", 8, "1950", box(LON0, LAT0 + 0.004)),                 # not a way or relation
    ("way", 9, "late C19", box(LON0 + 0.4, LAT0 + 0.3)),          # far off, another tile
] + [("way", 100 + k, str(1900 + k), box(LON0 + 0.001 * k, LAT0 - 0.003)) for k in range(30)]
WANT = {2 * 34633854: 1931, 2 * 42500770: 1930, 2 * 3565284 + 1: 1890, 2 * 3565284: 2001, 2 * 5: 1913,
        2 * 9: 1885, **{2 * (100 + k): 1900 + k for k in range(30)}}


class YearSqlTest(unittest.TestCase):
    @unittest.skipIf(duckdb is None, "DuckDB not installed")
    def test_values_osm_has_most(self):
        cases = {"1931": 1931, "1930-1931": 1930, "1976..1991": 1976, "~1900": 1900, "July 2023": 2023,
                 "1950s": 1950, "before 1976": 1975, "<2004": 2003, "after 1949": 1950, ">1971": 1972, "C19": 1850,
                 "early C20": 1915, "late C19": 1885, "mid C18": 1750, "2014-11-03": 2014, "c. 1890": 1890,
                 "12345": None, "0950": None, "abc": None, "2200": None, "C9": None, "": None}
        con = duckdb.connect()
        con.execute("CREATE TABLE t (start_date VARCHAR)")
        con.executemany("INSERT INTO t VALUES (?)", [[k] for k in cases])
        got = dict(con.execute(f"SELECT start_date, {by.YEAR_SQL} FROM t").fetchall())
        self.assertEqual(got, cases)

    def test_keys(self):
        self.assertEqual(osmyears.key_of("w34633854@78"), 2 * 34633854)
        self.assertEqual(osmyears.key_of("r3565284@3"), 2 * 3565284 + 1)
        self.assertIsNone(osmyears.key_of("n5@1"))
        self.assertIsNone(osmyears.key_of(""))
        self.assertIsNone(osmyears.key_of(None))
        self.assertEqual(by.key_of("relation", 7), osmyears.key_of("r7@1"))
        self.assertIsNone(by.key_of("node", 7))


class DecodeTest(unittest.TestCase):
    def setUp(self):
        fast.require(osmyears.NATIVE, "oby_decode")

    def test_round_trip_raw_and_deflated(self):
        keys = np.array([4, 10, 2 ** 40], np.uint64)
        years = np.array([1913, 1850, 2024], np.int64)
        blob = by.encode_tile(keys, years)
        for b in (blob, by.ots.deflate(blob)):
            k, y = fast._ef.oby_decode(b)
            self.assertEqual(k.tolist(), keys.tolist())
            self.assertEqual(y.tolist(), years.tolist())

    def test_damaged_tiles_raise(self):
        blob = by.encode_tile(np.array([4, 10], np.uint64), np.array([1913, 1999]))
        for bad in (b"", b"OBY1", blob[:-1], b"XXXX" + blob[4:], by.ots.deflate(blob)[:-3],
                    b"OBY1" + by.ots.uv1(1 << 40)):
            with self.assertRaises(ValueError):
                fast._ef.oby_decode(bad)


@unittest.skipIf(duckdb is None, "DuckDB not installed")
class BuildAndReadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fast.require(osmyears.NATIVE, "oby_decode")
        cls.tmp = tempfile.mkdtemp(prefix="oby-test-")
        cls.parquet = os.path.join(cls.tmp, "buildings.parquet")
        write_parquet(cls.parquet, ROWS)
        cls.out = os.path.join(cls.tmp, "out")
        with mock.patch.object(by.ots, "PART_SLACK", 400):      # (parts of a tile or two: several here)
            cls.man = by.build(cls.parquet, cls.out, workers=1, log=lambda s: None, part_bytes=480)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files(self):
        out = {}
        for p in self.man["parts"]:
            with open(os.path.join(self.out, p["archive"]), "rb") as f:
                out[f"{BASE}/{p['archive']}"] = f.read()
        with open(os.path.join(self.out, "manifest.json"), "rb") as f:
            out[f"{BASE}/manifest.json"] = f.read()
        return out

    def md(self, fetcher):
        return SimpleNamespace(fetcher=fetcher, cache_dir=tempfile.mkdtemp(dir=self.tmp))

    def pinned(self, files=None, fetcher=None):
        """A map data whose year manifest is pinned on disk (MapData.warm_manifests' fetch), not yet frozen."""
        md = self.md(fetcher or FakeFetcher(self.files() if files is None else files))
        osmyears.shards(md).version()
        return md

    def test_manifest(self):
        m = self.man
        self.assertEqual((m["format"], m["zoom"]), (1, 12))
        self.assertEqual(m["rows"], sum(len(by.cut(*self._rows())[t][0]) for t in by.cut(*self._rows())))
        self.assertGreaterEqual(len(m["parts"]), 2, "parts of at most 480 bytes")
        key = m["key"]
        self.assertRegex(key, r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
        self.assertEqual([p["archive"] for p in m["parts"]],
                         [f"{key}/osmyears-{i:03d}.pmtiles" for i in range(len(m["parts"]))])
        import hashlib
        for p in m["parts"]:
            b = open(os.path.join(self.out, p["archive"]), "rb").read()
            self.assertEqual((p["head_sha256"], p["sha256"], p["bytes"]),
                             (hashlib.sha256(b[:by.HEAD_BYTES]).hexdigest(), hashlib.sha256(b).hexdigest(), len(b)))
        self.assertEqual(by.HEAD_BYTES, __import__("osmtagshards").HEAD_BYTES)
        self.assertIsNotNone(osmyears.pinned_parts(m))
        for bad in (dict(m, key=None), dict(m, parts=[dict(m["parts"][0], head_sha256=None)]),
                    dict(m, parts=[dict(m["parts"][0], archive="osmyears-000.pmtiles")])):
            self.assertIsNone(osmyears.pinned_parts(bad), "a part not pinned to its build: not read")

    def _rows(self):
        got = [r for r in ROWS if r[0] in ("way", "relation") and r[2] and r[2] != "unknown"]
        yrs = {"c. 1890": 1890, "1913..1915": 1913, "late C19": 1885}
        return (np.array([by.key_of(t, i) for t, i, _s, _b in got], np.uint64),
                np.array([yrs.get(s, int(s[:4]) if s[:4].isdigit() else 0) for _t, _i, s, _b in got], np.int16),
                np.array([b for *_x, b in got], np.float32))

    def test_every_dated_building_read_back(self):
        with mock.patch.object(osmyears, "BASE", BASE):
            sh = osmyears.shards(self.pinned())
            got = osmyears.tiles_of(sh, LAT0 - 0.01, LON0 - 0.02, LAT0 + 0.31, LON0 + 0.41)
        self.assertEqual(dict(zip(got[0].tolist(), got[1].tolist())), WANT)
        self.assertEqual(got[0].tolist(), sorted(got[0].tolist()))

    def test_a_building_on_a_tile_edge_is_in_both_tiles(self):
        tiles = by.cut(*self._rows())
        holding = [t for t, (k, _y) in tiles.items() if 2 * 5 in k.tolist()]
        self.assertEqual(len(holding), 2)
        with mock.patch.object(osmyears, "BASE", BASE):
            sh = osmyears.shards(self.pinned())
            for d in (-0.0005, 0.0005):        # (from either side, one tile each)
                got = osmyears.tiles_of(sh, LAT0, EDGE + d, LAT0 + 0.0001, EDGE + d)
                self.assertEqual(dict(zip(got[0].tolist(), got[1].tolist())).get(10), 1913)

    def test_tile_fetch_years_and_lookup(self):
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned()
            n0 = revisit.faults()
            yrs = osmyears.Fetch(frame, -300, -300, 300, 300, md).years()
            self.assertEqual(revisit.faults(), n0)
            self.assertEqual(osmyears.identity(md), self.man["built"])
        self.assertEqual(osmyears.lookup(yrs, ["w34633854@78", "r3565284@1", "w3565284@2", "w6@1", None, "n1@1",
                                               "w999999@1"]).tolist(), [1931, 1890, 2001, 0, 0, 0, 0])

    def test_not_published_no_fault(self):
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned(files={})
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0, "nothing published: the buildings as before, kept")
            self.assertEqual(osmyears.identity(md), "none")

    def test_nothing_pinned_yet_no_years_no_fault(self):
        """The manifest unreachable at the process's first ask (or not asked yet): "none" for the whole process."""
        frame = geodesy.EnuFrame(LAT0, LON0)
        f = FakeFetcher(self.files())
        f.fail = netfetch.FetchFailed("offline")
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.md(f)
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0)
            self.assertEqual(osmyears.identity(md), "none")
            f.fail = None
            osmyears.shards(md).version()           # (pinned now: for the next start, not this process)
            self.assertEqual(osmyears.identity(md), "none")
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            fresh = SimpleNamespace(fetcher=FakeFetcher(self.files()), cache_dir=md.cache_dir)
            self.assertEqual(osmyears.identity(fresh), self.man["built"], "the next process: the pinned build")

    def test_the_build_is_frozen_for_the_process(self):
        """A newer manifest pinned while the process runs: its tiles stay keyed and built on the build it froze."""
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned()
            built = osmyears.identity(md)
            pin = os.path.join(md.cache_dir, "pagesdata", "osmyears", "manifest.json")
            m = json.loads(open(pin).read())
            m["built"] = "2099-01-01T00:00:00Z"
            with open(pin, "w") as f:
                json.dump(m, f)
            self.assertEqual(osmyears.identity(md), built)
            yrs = osmyears.Fetch(frame, -300, -300, 300, 300, md).years()
            self.assertEqual(osmyears.lookup(yrs, ["w34633854@1"]).tolist(), [1931])
            fresh = SimpleNamespace(fetcher=FakeFetcher({}), cache_dir=md.cache_dir)    # (another process)
            self.assertEqual(osmyears.identity(fresh), "2099-01-01T00:00:00Z")

    def test_unreachable_tiles_fault(self):
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned()
            md.fetcher.fail = netfetch.FetchFailed("offline")
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0 + 1, "built again later, with the years")
            n = len(md.fetcher.seen)                # (DOWN_S: the next tile faults at once, nothing asked)
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual((revisit.faults(), len(md.fetcher.seen)), (n0 + 2, n))

    def test_a_tile_that_fails_faults(self):
        files = self.files()
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned(files)
            for p in self.man["parts"]:             # (the archive gone)
                files.pop(f"{BASE}/{p['archive']}")
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0 + 1)

    def test_a_hostile_directory_length_is_not_fetched(self):
        """A directory entry claiming a 1 GB tile: refused before its range read, the tile faulted."""
        t = by.ots.tile_id(by.SHARD_ZOOM, *self._tile_of(LAT0, LON0))
        data = os.path.join(self.tmp, "hostile.bin")
        with open(data, "wb") as f:
            f.write(by.ots.deflate(by.encode_tile(np.array([4], np.uint64), np.array([1931]))))
        arch = os.path.join(self.tmp, "hostile.pmtiles")
        by.ots.write_pmtiles(arch, [(t, 0, 1 << 30, 1)], data, (-180.0, -85.0, 180.0, 85.0), 1, by.SHARD_ZOOM,
                             title="x", fmt="oby1")
        import hashlib
        body = open(arch, "rb").read()
        m = dict(self.man, key="hostile1", parts=[{"archive": "hostile1/hostile.pmtiles", "first": t, "last": t,
                                                   "bytes": len(body), "tiles": 1,
                                                   "head_sha256": hashlib.sha256(body[:16384]).hexdigest()}])
        files = {f"{BASE}/manifest.json": json.dumps(m).encode(), f"{BASE}/hostile1/hostile.pmtiles": body}
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned(files)
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0 + 1)
        ranges = [r for n, r in md.fetcher.seen if n == "hostile.pmtiles"]
        self.assertTrue(ranges)
        self.assertTrue(all(int(r.split("-")[1]) < (1 << 20) for r in ranges), ranges)

    def _republished(self):
        """(files of build A, files of build B): B built from other rows (Empire State 1999), in its own folder."""
        rows = [r if r[1] != 34633854 else ("way", 34633854, "1999", r[3]) for r in ROWS]
        pq = os.path.join(self.tmp, "b.parquet")
        write_parquet(pq, rows)
        out_b = os.path.join(self.tmp, "out_b")
        import time as _t
        _t.sleep(1.1)                               # (another build time: another key)
        with mock.patch.object(by.ots, "PART_SLACK", 400):
            man_b = by.build(pq, out_b, workers=1, log=lambda s: None, part_bytes=480)
        fb = {}
        for p in man_b["parts"]:
            fb[f"{BASE}/{p['archive']}"] = open(os.path.join(out_b, p["archive"]), "rb").read()
        fb[f"{BASE}/manifest.json"] = open(os.path.join(out_b, "manifest.json"), "rb").read()
        return man_b, fb

    def test_a_frozen_build_reads_its_own_archive_after_a_republish(self):
        """Frozen on A, B published (A's folder kept): an uncached part is read from A's folder, A's years."""
        man_b, fb = self._republished()
        self.assertNotEqual(man_b["key"], self.man["key"])
        frame = geodesy.EnuFrame(LAT0, LON0)
        files = self.files()
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned(files)
            self.assertEqual(osmyears.identity(md), self.man["built"])     # (frozen on A, nothing read yet)
            files.update(fb)                                                # (B published, A's folder kept)
            yrs = osmyears.Fetch(frame, -300, -300, 300, 300, md).years()
            self.assertEqual(osmyears.lookup(yrs, ["w34633854@1"]).tolist(), [1931], "A's year, not B's 1999")

    def test_a_frozen_build_never_reads_another_under_its_name(self):
        """Frozen on A, A's part URLs then serving B's bytes (a publisher that overwrote them) or gone: faulted, never
        B's years under A's identity."""
        man_b, fb = self._republished()
        frame = geodesy.EnuFrame(LAT0, LON0)
        files = self.files()
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned(files)
            self.assertEqual(osmyears.identity(md), self.man["built"])
            pa = [p["archive"] for p in self.man["parts"]]
            pb = [p["archive"] for p in man_b["parts"]]
            for a, b in zip(pa, pb):                # (A's names, B's bytes)
                files[f"{BASE}/{a}"] = fb[f"{BASE}/{b}"]
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0 + 1)
        md2 = self.pinned(self.files())
        with mock.patch.object(osmyears, "BASE", BASE):
            md2 = self.pinned(self.files())
            self.assertEqual(osmyears.identity(md2), self.man["built"])
            for a in [p["archive"] for p in self.man["parts"]]:     # (A's folder pruned)
                md2.fetcher.files.pop(f"{BASE}/{a}")
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md2).years())
            self.assertEqual(revisit.faults(), n0 + 1)

    def test_a_part_of_another_size_is_not_read(self):
        files = self.files()
        m = json.loads(files[f"{BASE}/manifest.json"])
        for p in m["parts"]:
            p["bytes"] += 1
        files[f"{BASE}/manifest.json"] = json.dumps(m).encode()
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned(files)
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0 + 1)

    def test_a_cached_head_of_another_archive_is_read_again(self):
        """A head on disk that is not the frozen build's (a copy of B's): dropped and read again, checked."""
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned()
            yrs = osmyears.Fetch(frame, -300, -300, 300, 300, md).years()
            sh = osmyears.shards(md)
            heads = [os.path.join(d, "head.bin") for d, _, fs in os.walk(sh._dir(self.man["built"]))
                     if "head.bin" in fs]
            self.assertTrue(heads)
            fresh = SimpleNamespace(fetcher=FakeFetcher(self.files()), cache_dir=md.cache_dir)  # (another process)
            shutil.rmtree(os.path.join(sh._dir(self.man["built"]), "tiles"))     # (the tiles read again too)
            for h in heads:
                with open(h, "r+b") as f:
                    f.seek(200)
                    f.write(b"\xff\xff")
            got = osmyears.Fetch(frame, -300, -300, 300, 300, fresh).years()
            self.assertEqual(osmyears.lookup(got, ["w34633854@1"]).tolist(), [1931])
            self.assertTrue([r for n, r in fresh.fetcher.seen if r == "bytes=0-16383"], "the head read again")
        self.assertEqual(osmyears.lookup(yrs, ["w34633854@1"]).tolist(), [1931])

    def _tile_of(self, lat, lon):
        import math
        n = 1 << by.SHARD_ZOOM
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        return x, y

    def test_an_oversized_tile_is_refused(self):
        with self.assertRaises(ValueError):
            fast._ef.oby_decode(b"OBY1" + bytes((16 << 20) + 1))
        with mock.patch.object(osmyears, "BASE", BASE):
            sh = osmyears.shards(self.md(FakeFetcher({})))
            with self.assertRaises(ValueError):
                sh._decode(b"OBY1" + bytes(sh.MAX_TILE_BYTES), None)

    def test_a_manifest_of_another_format_is_not_read(self):
        files = self.files()
        m = json.loads(files[f"{BASE}/manifest.json"])
        m["format"] = 99
        files[f"{BASE}/manifest.json"] = json.dumps(m).encode()
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned(files)
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0, "a newer format: no years, no faults")
            self.assertEqual(osmyears.identity(md), "none")

    def test_off(self):
        with mock.patch.object(osmyears, "ON", False):
            md = self.md(FakeFetcher(self.files()))
            self.assertIsNone(osmyears.shards(md))
            self.assertEqual(osmyears.identity(md), "off")
        import railtags
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE), mock.patch.object(railtags, "ON", False):
            n0 = revisit.faults()       # (the Pages datasets off: no years, no faults)
            md = self.md(FakeFetcher(self.files()))
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0)
            self.assertEqual(osmyears.identity(md), "none")
        self.assertIsNone(osmyears.shards(SimpleNamespace(fetcher=None, cache_dir=self.tmp)))
        self.assertEqual(osmyears.identity(SimpleNamespace(fetcher=None, cache_dir=self.tmp)), "off")

    def test_building_tile_keys_name_the_years(self):
        """The tiles that draw buildings are keyed on the years' identity (blockworker._md_versions(True)); the
        others, plans and cell facts not (MapData.versions())."""
        import blockworker
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.pinned()
            md.versions = lambda: {"pipeline": 1}
            with mock.patch.object(blockworker, "_s", SimpleNamespace(md=md)):
                self.assertEqual(blockworker._md_versions(), {"pipeline": 1})
                self.assertEqual(blockworker._md_versions(True), {"pipeline": 1, "osmyears": self.man["built"]})
            off = self.md(FakeFetcher({}))
            off.versions = lambda: {"pipeline": 1}
            with mock.patch.object(osmyears, "ON", False), mock.patch.object(blockworker, "_s", SimpleNamespace(md=off)):
                self.assertEqual(blockworker._md_versions(True), {"pipeline": 1, "osmyears": "off"})


if __name__ == "__main__":
    unittest.main()
