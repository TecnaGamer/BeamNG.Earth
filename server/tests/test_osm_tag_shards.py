"""The weekly OSM road tag shards (tools/build_osm_tag_shards.py -> one PMTiles archive; server/osmtagshards.py +
earthfast osmtagshards.rs read a tile): the builder's query and cell test are osmtags.py's, a cell's tags, raceways,
busways, outlines and motorway geometry from a tile are exactly what the Layercake reader gives for the same parquet
file, the archive's leaves, a damaged or changed archive, and MapData's use of it (the Layercake file whenever the
shards cannot answer; BNE_OSMTAGS_SHARDS=off).

No network: a small Layercake-like parquet written here (DuckDB), built into an archive, served by a fake fetcher."""
import json
import math
import os
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2] / "tools")]
import build_osm_tag_shards as bt  # noqa: E402
import fast  # noqa: E402
import mapcells  # noqa: E402
import netfetch  # noqa: E402
import osmtags  # noqa: E402
import osmtagshards  # noqa: E402

try:
    import duckdb
except ImportError:
    duckdb = None

BASE = "https://shards.test/osmtags"
LON0, LAT0 = -111.66, 35.19         # (west of Greenwich: negative floats' ordered keys)


# ------------------------------------------------------------------------------------------------ the fixture
def _wkb(coords):
    c = np.asarray(coords, np.float64)
    return struct.pack("<BII", 1, 2, len(c)) + c.astype("<f8").tobytes()


def _ring(lon, lat, r_m, n=24):
    a = np.linspace(0, 2 * math.pi, n + 1)
    kx = 111320.0 * math.cos(math.radians(lat))
    pts = np.column_stack([lon + r_m * np.cos(a) / kx, lat + r_m * np.sin(a) / 110540.0])
    pts[-1] = pts[0]
    return np.round(pts * 1e7) / 1e7


def fixture_rows(seed=7):
    """Highway rows over ~11 x 13 km round (LON0, LAT0): every kind osmtags reads, a few it leaves out."""
    rng = np.random.default_rng(seed)
    rows = []
    cell = mapcells.cell_of(LAT0, LON0)
    edge = cell.bounds()[2]                     # a cell's east edge, exactly: a bbox ending on it
    names = ["Main St", "Straße der Einheit", "Route 66", None, "東京通り"]
    for i in range(400):
        lon = LON0 + rng.uniform(-0.06, 0.06)
        lat = LAT0 + rng.uniform(-0.06, 0.06)
        k = int(rng.integers(2, 6))
        line = np.round(np.column_stack([lon + np.cumsum(rng.normal(0, 4e-4, k)),
                                         lat + np.cumsum(rng.normal(0, 4e-4, k))]) * 1e7) / 1e7
        hw = ["residential", "primary", "motorway", "trunk_link", "service", "construction", "footway"][i % 7]
        tags = dict(highway=hw, name=names[i % 5],
                    lanes=[None, "3", "2;3", " 2 ", "4"][i % 5], width=[None, "7 m", "23'", "x"][i % 4],
                    maxspeed=[None, "30 mph", "50", "none"][i % 4], sidewalk=[None, "both", "no"][i % 3],
                    lit=[None, "yes"][i % 2], smoothness=[None, "good", None, "bad"][i % 4],
                    surface=[None, "asphalt", "gravel"][i % 3], oneway=[None, "yes", "-1"][i % 3],
                    layer=[None, "1", "-1", "x"][i % 4])
        if i % 11 == 0:                         # (no tag the roads use: left out)
            tags = dict(highway="footway")
        if i % 13 == 0:
            line[-1, 0] = edge                  # (a box ending on the cell's edge)
        if i % 17 == 0:                         # (a 6 km way: in many cells and tiles)
            line = np.array([[LON0 - 0.035, lat], [LON0 + 0.035, lat]])
        rows.append((1000 + i * 7, tags, line))
    # raceways: a facility's outline (a closed ring with no surface, width, lanes, oneway) round a kart track, an
    # oval with its infield course joined to it, and a busway
    rows.append((900001, dict(highway="raceway", name="Circuit Park"), _ring(LON0 + 0.01, LAT0 + 0.01, 400)))
    rows.append((900002, dict(highway="raceway", name="Kart", surface="asphalt"),
                 _ring(LON0 + 0.01, LAT0 + 0.01, 150)))
    oval = _ring(LON0 - 0.01, LAT0 - 0.012, 300)
    rows.append((900003, dict(highway="raceway", name="Oval"), oval))
    rows.append((900004, dict(highway="raceway", name="Infield", width="12"),
                 np.vstack([oval[0], [LON0 - 0.01, LAT0 - 0.012]])))
    rows.append((900005, dict(highway="busway", name="Bus", lanes="2", oneway="yes"),
                 np.array([[LON0 - 0.02, LAT0], [LON0 + 0.02, LAT0 + 0.001]])))
    rows.append((900006, dict(highway="primary", name="No box"), None))     # (a NULL bbox: in no cell)
    return rows


def write_parquet(path, rows, group_rows=64):
    con = duckdb.connect()
    cols = ["highway", "name", "lanes", "width", "maxspeed", "sidewalk", "lit", "smoothness", "surface", "oneway",
            "layer", "bridge", "tunnel", "ref"]
    con.execute("CREATE TABLE h (id BIGINT, " + ", ".join(f'"{c}" VARCHAR' for c in cols)
                + ", bbox STRUCT(xmin FLOAT, ymin FLOAT, xmax FLOAT, ymax FLOAT), geometry BLOB)")
    for wid, tags, line in rows:
        box = None if line is None else dict(xmin=float(line[:, 0].min()), ymin=float(line[:, 1].min()),
                                             xmax=float(line[:, 0].max()), ymax=float(line[:, 1].max()))
        row = [wid] + [tags.get(c) for c in cols] + [box, None if line is None else _wkb(line)]
        con.execute("INSERT INTO h VALUES (" + ",".join("?" for _ in row) + ")", row)
    con.execute(f"COPY h TO '{path}' (FORMAT PARQUET, ROW_GROUP_SIZE {group_rows})")
    con.close()


def canon(got):
    """A cell's (tags, ways) comparable with ==: the ways' coords as lists."""
    tags, ways = got
    return tags, [{c: (v.tolist() if c == "coords" else v) for c, v in w.items()} for w in ways]


class FakeFetcher:
    """Range reads of local files by URL (the manifest whole), one ETag per file; requests counted."""

    def __init__(self, files, etag='"a1"'):
        self.files, self.etag, self.seen = files, etag, []
        self.fail = None

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
        return netfetch.Response(206, body[a:b + 1], {"etag": self.etag})


def layercake_store(tmp, path):
    """osmtags.OsmTags reading the local parquet file (its row groups as _groups parses them)."""
    con = duckdb.connect()

    class Local:
        def execute(self, sql):
            return con.execute(sql.replace("fixture://osm/highways.parquet", path))

        def close(self):
            pass
    store = osmtags.OsmTags(tmp, None)
    store._reader = SimpleNamespace(_connect=lambda *a: (Local(), "fixture", []), _fetch=mock.Mock())
    groups = {}
    for rg, first, n, (x0, y0, x1, y1) in bt.row_groups(path):
        groups[rg] = {"first": first, "n": n, "bbox": {"xmin": (x0, x0), "ymin": (y0, y0), "xmax": (x1, x1),
                                                     "ymax": (y1, y1)},
                      "cols": {c: [] for c in ("id", "bbox", "geometry", "highway", "name", "lanes")}}
    remote = SimpleNamespace(cache_dir=os.path.join(tmp, "lc"))
    store._groups = lambda *a: groups
    store.version = lambda *a: ("e", remote)
    return store, remote


@unittest.skipIf(duckdb is None, "DuckDB not installed")
class ShardsMatchLayercakeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fast.require(osmtagshards.NATIVE, "osmtagshards")
        cls.tmp = tempfile.mkdtemp(prefix="otg-test-")
        cls.parquet = os.path.join(cls.tmp, "highways.parquet")
        write_parquet(cls.parquet, fixture_rows())
        cls.out = os.path.join(cls.tmp, "out")
        # (a small root: the archive needs leaf directories, as the planet's does)
        cls.man = bt.build(cls.parquet, cls.out, workers=1, tmp=os.path.join(cls.tmp, "work"), log=lambda s: None,
                           root_max=40, z=13)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files(self):
        with open(os.path.join(self.out, "osmtags.pmtiles"), "rb") as f:
            arch = f.read()
        with open(os.path.join(self.out, "manifest.json"), "rb") as f:
            man = f.read()
        return {f"{BASE}/manifest.json": man, f"{BASE}/osmtags.pmtiles": arch}

    def shards(self, cache, fetcher):
        with mock.patch.object(osmtagshards, "BASE", BASE):
            return osmtagshards.TagShards(cache, fetcher)

    def cells(self):
        c = mapcells.cell_of(LAT0, LON0)
        return [mapcells.CellKey(14, c.x + dx, c.y + dy) for dy in range(-3, 4) for dx in range(-3, 4)]

    def tile_of(self, k):
        sh = 14 - self.man["zoom"]
        return k.x >> sh, k.y >> sh

    def test_manifest(self):
        m = self.man
        self.assertEqual((m["format"], m["zoom"], m["archive"]), (1, 13, "osmtags.pmtiles"))
        self.assertTrue(osmtagshards.schema_ok(m["schema"]))
        self.assertGreater(m["tiles"], 1)

    def test_every_cell_as_the_layercake_reader_gives_it(self):
        store, _ = layercake_store(os.path.join(self.tmp, "lc1"), self.parquet)
        f = FakeFetcher(self.files())
        sh = self.shards(os.path.join(self.tmp, "c1"), f)
        seen_tags = seen_ways = seen_geom = 0
        outlines = set()
        for k in self.cells():
            t = sh.tile(k)
            got = osmtags.from_tile(t, k.bounds())
            want = store._cell(SimpleNamespace(cache_dir=os.path.join(self.tmp, "lc1", "rg")), k.bounds(), "demand")
            self.assertEqual(list(got[0].items()), list(want[0].items()), k)     # (the same order and types too)
            for a, b in zip(got[0].values(), want[0].values()):
                self.assertEqual([type(v) for v in a.values()], [type(v) for v in b.values()])
            self.assertEqual(len(got[1]), len(want[1]), k)
            for a, b in zip(got[1], want[1]):
                self.assertEqual({c: v for c, v in a.items() if c != "coords"},
                                 {c: v for c, v in b.items() if c != "coords"})
                np.testing.assert_array_equal(a["coords"], b["coords"])
            ga = osmtags.geometry_from_tile(t, k.bounds(), None)
            gb = store.highway_geometry(k.bounds(), "demand", None)
            self.assertEqual(sorted(ga), sorted(gb), k)
            for w in ga:
                np.testing.assert_array_equal(ga[w], gb[w])
            need = [tuple(v) for v in (ga[w][[0, -1]].ravel()[[0, 1, 2, 3]] for w in list(ga)[:1])]
            self.assertEqual(sorted(osmtags.geometry_from_tile(t, k.bounds(), need)),
                             sorted(store.highway_geometry(k.bounds(), "demand", need)))
            seen_tags += len(want[0])
            seen_ways += len(want[1])
            seen_geom += len(gb)
            outlines |= {w["id"] for w in want[1]}
        self.assertGreater(seen_tags, 300)
        self.assertGreater(seen_geom, 10)
        self.assertIn(900005, outlines, "the busway")
        self.assertIn(900002, outlines, "the kart track")
        self.assertNotIn(900001, outlines, "the facility's outline is not drawn")
        self.assertIn(900003, outlines, "an oval with its infield joined is a track")
        self.assertNotIn(900006, {w for k in self.cells() for w in osmtags.from_tile(sh.tile(k), k.bounds())[0]})
        # (one head, the leaves, then one read per tile: never again for the same tile)
        tiles = {self.tile_of(k) for k in self.cells()}
        reads = [s for s in f.seen if s[0] == "osmtags.pmtiles"]
        self.assertGreater(self.man["tiles"], 4)
        self.assertLessEqual(len(reads), 1 + self.man["tiles"] + len(tiles))
        self.assertEqual(len(reads), len(set(reads)), "each range read once")
        self.assertTrue(os.listdir(os.path.join(sh._dir(sh.manifest()[0]), "leaf")), "the archive has leaves")

    def test_tile_ranges_are_osmtags_cell_test(self):
        rng = np.random.default_rng(3)
        z = 9
        n = 1 << z
        x0 = rng.uniform(-180, 179, 400).astype(np.float32)
        y0 = rng.uniform(-80, 79, 400).astype(np.float32)
        x1 = (x0 + rng.exponential(0.5, 400)).astype(np.float32)
        y1 = (y0 + rng.exponential(0.5, 400)).astype(np.float32)
        # boxes ending exactly on tile edges
        x1[:40] = np.float32(np.array([mapcells.CellKey(z, int(v), 0).bounds()[2] for v in (x0[:40] + 180) / 360 * n]))
        ax0, ax1, ay0, ay1 = bt.tile_ranges(x0, y0, x1, y1, z)
        # every tile's bounds as mapcells gives them, in float32: osmtags.cell_rows' test, tile by tile
        b = np.array([mapcells.CellKey(z, x, y).bounds() for x in range(n) for y in range(n)],
                     np.float64).astype(np.float32).reshape(n, n, 4)
        for i in range(400):
            hit = (x0[i] <= b[..., 2]) & (x1[i] >= b[..., 0]) & (y0[i] <= b[..., 3]) & (y1[i] >= b[..., 1])
            want = set(zip(*(v.tolist() for v in np.nonzero(hit))))
            got = {(x, y) for x in range(ax0[i], ax1[i] + 1) for y in range(ay0[i], ay1[i] + 1)}
            self.assertEqual(got, want, i)

    def test_a_tile_is_its_cells_union(self):
        # (a cell's float32 test against the tile's own edges: the cells' edges are the same doubles)
        for x, y in ((100, 200), (1919, 1567), (4095, 0)):
            t = mapcells.CellKey(bt.SHARD_ZOOM, x, y).bounds()
            sh = 14 - bt.SHARD_ZOOM
            cells = [mapcells.CellKey(14, (x << sh) + dx, (y << sh) + dy).bounds()
                     for dx in range(1 << sh) for dy in range(1 << sh)]
            self.assertEqual(t[0], min(c[0] for c in cells))
            self.assertEqual(t[2], max(c[2] for c in cells))
            self.assertEqual(t[1], min(c[1] for c in cells))
            self.assertEqual(t[3], max(c[3] for c in cells))
            self.assertEqual(bt.lat_of(y, 1 << bt.SHARD_ZOOM), mapcells._lat_of(y, 1 << bt.SHARD_ZOOM))

    def test_disk_copy_needs_no_network(self):
        f = FakeFetcher(self.files())
        cache = os.path.join(self.tmp, "c2")
        k = self.cells()[24]
        a = osmtags.from_tile(self.shards(cache, f).tile(k), k.bounds())
        f.fail = netfetch.HostUnavailable("down", BASE, "shards.test")
        sh = self.shards(cache, f)
        b = osmtags.from_tile(sh.tile(k), k.bounds())
        self.assertEqual(canon(a), canon(b))

    def test_damaged_tile_on_disk_is_read_again(self):
        f = FakeFetcher(self.files())
        cache = os.path.join(self.tmp, "c3")
        sh = self.shards(cache, f)
        k = self.cells()[24]
        want = osmtags.from_tile(sh.tile(k), k.bounds())
        built = sh.manifest()[0]
        z = self.man["zoom"]
        path = os.path.join(sh._dir(built), "tiles", f"{k.x >> (14 - z)}_{k.y >> (14 - z)}.otg")
        with open(path, "r+b") as fh:
            fh.seek(3)
            fh.write(b"\xff\xff\xff\xff")
        got = osmtags.from_tile(self.shards(cache, f).tile(k), k.bounds())
        self.assertEqual(canon(got), canon(want))

    def test_an_archive_that_changed_is_dropped(self):
        f = FakeFetcher(self.files())
        cache = os.path.join(self.tmp, "c4")
        sh = self.shards(cache, f)
        ks = self.cells()
        sh.tile(ks[24])
        f.etag = '"b2"'                       # (the file behind the URL replaced)
        far = next(k for k in ks if self.tile_of(k) != self.tile_of(ks[24])
                   and self.shards(os.path.join(self.tmp, "c4b"), FakeFetcher(self.files())).tile(k) is not None)
        with self.assertRaises(osmtagshards.Changed):
            sh.tile(far)
        self.assertFalse(os.path.exists(os.path.join(sh._dir(sh.manifest()[0]), "head.bin")))

    def test_a_manifest_this_does_not_read_is_not_used(self):
        files = self.files()
        m = json.loads(files[f"{BASE}/manifest.json"])
        m["schema"]["text"] = m["schema"]["text"][:-1]
        files[f"{BASE}/manifest.json"] = json.dumps(m).encode()
        sh = self.shards(os.path.join(self.tmp, "c5"), FakeFetcher(files))
        with self.assertRaises(osmtagshards.NotInUse):
            sh.tile(self.cells()[0])

    def test_no_manifest_published(self):
        sh = self.shards(os.path.join(self.tmp, "c6"), FakeFetcher({}))
        with self.assertRaises(osmtagshards.NotInUse):
            sh.tile(self.cells()[0])

    def test_one_read_of_a_tile_for_many_threads(self):
        f = FakeFetcher(self.files())
        sh = self.shards(os.path.join(self.tmp, "c7"), f)
        k = self.cells()[24]
        out = []
        ts = [threading.Thread(target=lambda: out.append(sh.tile(k))) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(out), 8)
        heads = [s for s in f.seen if s[1] == f"bytes=0-{osmtagshards.HEAD_BYTES - 1}"]
        self.assertEqual(len(heads), 1)


class BuilderTest(unittest.TestCase):
    def test_query_is_osmtags(self):
        """The tool is fetched alone by the data repo's Action: its copy of osmtags' query must stay the same."""
        self.assertEqual(bt.COLUMNS, osmtags.COLUMNS)
        self.assertEqual(bt.TEXT_COLUMNS, osmtags.TEXT_COLUMNS)
        self.assertEqual(bt.EXACT32, osmtags.EXACT32)
        self.assertEqual(bt.USED, osmtags.USED)
        self.assertEqual(bt.UNBUILT, osmtags.UNBUILT)
        self.assertEqual(bt.RACEWAY_COLUMNS, osmtags.RACEWAY_COLUMNS)
        self.assertEqual(bt.linestring(_wkb([[1.5, 2.5], [3.0, 4.0]])).tolist(), [[1.5, 2.5], [3.0, 4.0]])
        self.assertIsNone(bt.linestring(struct.pack("<BI", 1, 3) + b"x" * 20))

    def test_varints(self):
        for vals in ([0, 1, 127, 128, 300, 2 ** 35, 2 ** 63 + 5], list(range(0, 10 ** 6, 9973))):
            a = np.array(vals, np.uint64)
            got, i, out = bt.uv(a), 0, []
            while i < len(got):
                v = s = 0
                while True:
                    c = got[i]
                    i += 1
                    v |= (c & 0x7F) << s
                    s += 7
                    if c < 0x80:
                        break
                out.append(v)
            self.assertEqual(out, vals)
        self.assertEqual(bt.zz(np.array([0, -1, 1, -2])).tolist(), [0, 1, 2, 3])
        self.assertEqual([bt.zz1(v) for v in (0, -1, 1, -2)], [0, 1, 2, 3])

    def test_coords_exact(self):
        c = np.array([[-111.6513, 35.1983], [-111.6514, 35.19831]])
        q = np.round(c * 1e7) / 1e7
        self.assertEqual(bt.coords_bytes(q)[0], 1)
        self.assertEqual(bt.coords_bytes(q + 1e-12)[0], 0, "not an integer / 1e7: raw")


@unittest.skipIf(duckdb is None, "DuckDB not installed")
class MapDataUseTest(unittest.TestCase):
    """MapData asks the shards first and the Layercake file whenever they cannot answer."""

    def md(self):
        import mapdata
        md = mapdata.MapData.__new__(mapdata.MapData)
        md._lock = threading.Lock()
        md.fetcher = object()
        md.cache_dir = md.raw_dir = tempfile.mkdtemp(prefix="otg-md-")
        return md

    def test_off(self):
        md = self.md()
        with mock.patch.object(osmtagshards, "ON", False), \
                mock.patch.object(osmtags.OsmTags, "cell", return_value=({1: {}}, [])) as lc:
            self.assertEqual(md._osm_cell(mapcells.cell_of(LAT0, LON0), "demand"), ({1: {}}, []))
        lc.assert_called_once()

    def test_layercake_when_the_shards_fail(self):
        md = self.md()
        sh = mock.Mock()
        sh.tile.side_effect = osmtagshards.ShardError("boom")
        md.osm_shards = sh
        with mock.patch.object(osmtags.OsmTags, "cell", return_value=({2: {}}, [])) as lc:
            self.assertEqual(md._osm_cell(mapcells.cell_of(LAT0, LON0), "demand"), ({2: {}}, []))
        lc.assert_called_once()
        sh.down.assert_called_once()

    def test_a_busy_prefetch_is_raised(self):
        md = self.md()
        sh = mock.Mock()
        sh.tile.side_effect = netfetch.Deferred("busy")
        md.osm_shards = sh
        with self.assertRaises(netfetch.Deferred):
            md._osm_cell(mapcells.cell_of(LAT0, LON0), "prefetch")
        with mock.patch.object(osmtags.OsmTags, "cell", return_value=({3: {}}, [])):
            self.assertEqual(md._osm_cell(mapcells.cell_of(LAT0, LON0), "demand"), ({3: {}}, []))

    def test_the_shards_answer(self):
        md = self.md()
        sh = mock.Mock()
        sh.tile.return_value = None            # (a tile without rows)
        md.osm_shards = sh
        with mock.patch.object(osmtags.OsmTags, "cell") as lc:
            self.assertEqual(md._osm_cell(mapcells.cell_of(LAT0, LON0), "demand"), ({}, []))
            self.assertEqual(md._osm_geometry(mapcells.cell_of(LAT0, LON0), "demand", None), {})
        lc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
