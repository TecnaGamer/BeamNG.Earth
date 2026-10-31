import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import mapcells  # noqa: E402
import mapdata  # noqa: E402


class FakeOsm:
    """osmfuel.OsmFuel's cell(), counting its calls."""

    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    def cell(self, bounds, kind="demand"):
        self.calls += 1
        return self.rows


class OsmFuelCacheTest(unittest.TestCase):
    """(the OSM test, 2026-10-28: "lets test OSM if it is too laggy we will revert") MapData keeps each cell's
    OpenStreetMap fuel stations on disk: the road tiles' workers only read it (osm_fuel_cached, None when not fetched
    yet: never waited for), the server fetches it in the background (osm_fuel_fetch)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.md = mapdata.MapData(self.dir, fetcher=object())
        self.key = next(iter(mapcells.cells_for_bbox(40.44, -80.0, 40.441, -79.999)))
        self.rows = [{"id": 7, "brand": "Shell", "name": "Shell", "building": "roof", "bbox": (-80.0, 40.44, -79.9999, 40.4401)}]

    def test_fetch_then_cached(self):
        self.md.osmfuel = FakeOsm(self.rows)
        self.assertIsNone(self.md.osm_fuel_cached(self.key), "not fetched yet")
        self.assertEqual(self.md.osm_fuel_fetch(self.key), self.rows)
        got = self.md.osm_fuel_cached(self.key)
        self.assertEqual(got, self.rows)
        self.assertIsInstance(got[0]["bbox"], tuple)
        # another process (a road tile's worker) reads the same file
        other = mapdata.MapData(self.dir, fetcher=object())
        self.assertEqual(other.osm_fuel_cached(self.key), self.rows)
        # a cell with none is kept too (not asked again)
        empty = next(iter(mapcells.cells_for_bbox(41.0, -80.0, 41.001, -79.999)))
        self.md.osmfuel = FakeOsm([])
        self.assertEqual(self.md.osm_fuel_fetch(empty), [])
        self.assertEqual(self.md.osm_fuel_cached(empty), [])

    def test_off(self):
        self.md.osmfuel = FakeOsm(self.rows)
        self.md.osm_fuel_fetch(self.key)
        old = mapdata.OSM_FUEL
        mapdata.OSM_FUEL = False
        try:
            self.assertEqual(self.md.osm_fuel_cached(self.key), [], "BNGE_OSM_FUEL=0: none (not \"not fetched\")")
            self.assertEqual(self.md.osm_fuel_fetch(self.key), [])
        finally:
            mapdata.OSM_FUEL = old



class OsmFuelBudgetTest(unittest.TestCase):
    """(the OSM test: OSM fuel shares data.openstreetmap.us's daily budget with the road tags, osmtags.py) it stops at
    OSM_FUEL_SHARE of the budget so the roads' lanes, widths and sidewalks always have the rest."""

    def test_share(self):
        import netfetch
        import osmfuel

        class F:
            policies = {osmfuel.HOST: netfetch.HostPolicy(daily_budget=5000)}

            def __init__(self, used):
                self.used = used

            def snapshot(self):
                return {osmfuel.HOST: {"requests": self.used}}
        self.assertTrue(osmfuel.budget_ok(F(100)))
        self.assertFalse(osmfuel.budget_ok(F(int(5000 * osmfuel.OSM_FUEL_SHARE))))
        md = mapdata.MapData(tempfile.mkdtemp(), fetcher=F(4999))
        md.osmfuel = FakeOsm([{"id": 1, "brand": "", "name": "", "building": "", "bbox": (0, 0, 0, 0)}])
        key = next(iter(mapcells.cells_for_bbox(40.44, -80.0, 40.441, -79.999)))
        with self.assertRaises(osmfuel.OverShare):
            md.osm_fuel_fetch(key)
        self.assertEqual(md.osmfuel.calls, 0, "no request")
        self.assertIsNone(md.osm_fuel_cached(key), "not kept: asked again tomorrow")


class OsmFuelBlocksTest(unittest.TestCase):
    """(the OSM test: 461 MB of the POI file's blocks for 54 places, 2026-10-28) the block cache kept under a cap, oldest
    first; the file's footer (its row-group index: 40 s to read again) never pruned."""

    def test_prune(self):
        import osmfuel
        d = tempfile.mkdtemp()
        B = 1000
        for i in range(30):                         # blocks 0..29 of a 30 000-byte file, block 29 the newest
            path = os.path.join(d, f"{i}.blk")
            with open(path, "wb") as f:
                f.write(b"x" * B)
            os.utime(path, (1000 + i, 1000 + i))
        osmfuel.prune_blocks(d, size=30 * B, cap=10 * B, keep_tail=5 * B, block=B)
        left = sorted(int(f[:-4]) for f in os.listdir(d))
        self.assertTrue(set(range(25, 30)) <= set(left), "the footer's blocks kept")
        body = [i for i in left if i < 25]
        self.assertLessEqual(len(body) * B, 10 * B)
        self.assertEqual(body, sorted(body)[-len(body):] if body else [], "(the newest kept)")
        self.assertEqual(body, list(range(25 - len(body), 25)))


class OsmFuelReadTest(unittest.TestCase):
    """(2026-10-29: the start's index read turned away ten times, "bucket low, prefetch dropped") amenity = 'fuel' made
    DuckDB read the amenity column's bloom filter of every row group in the file, block by block (a game cell: 133 of
    its 144 requests, 35 of its 46 MB): a cell reads its own row groups and none of the bloom filters."""

    @classmethod
    def setUpClass(cls):
        try:
            import duckdb
        except ImportError:
            raise unittest.SkipTest("no duckdb")
        cls.dir = tempfile.mkdtemp()
        p = os.path.join(cls.dir, "pois.parquet").replace("\\", "/")
        con = duckdb.connect()
        # 20 row groups of 2000 rows, row group g between latitudes g and g + 0.5, one fuel station each
        con.execute(f"""COPY (SELECT i::BIGINT AS id, 'Brand' AS brand, 'Name ' || i::VARCHAR AS name, '' AS building,
            {{'xmin': (-79.0 + (i % 2000) / 4000.0)::FLOAT, 'ymin': ((i // 2000) + (i % 2000) / 4000.0)::FLOAT,
              'xmax': (-79.0 + (i % 2000) / 4000.0 + 0.0001)::FLOAT,
              'ymax': ((i // 2000) + (i % 2000) / 4000.0 + 0.0001)::FLOAT}} AS bbox,
            CASE WHEN i % 2000 = 1000 THEN 'fuel' ELSE 'cafe' || (i % 50)::VARCHAR END AS amenity
            FROM range(40000) t(i) ORDER BY i) TO '{p}' (FORMAT parquet, ROW_GROUP_SIZE 2000)""")
        cls.blooms = con.execute(f"""SELECT bloom_filter_offset, bloom_filter_offset + bloom_filter_length
            FROM parquet_metadata('{p}') WHERE bloom_filter_offset IS NOT NULL""").fetchall()
        with open(p, "rb") as f:
            cls.data = f.read()

    def test_no_bloom_filters_read(self):
        import osmfuel
        import overture
        import rangeread
        self.assertTrue(self.blooms, "(the fixture has them)")
        o = osmfuel.OsmFuel(self.dir, None)
        o._reader = overture.Reader(self.dir, None)
        remote = rangeread.MemoryRange(self.data)
        self.assertEqual(o._fuel(remote, (10.0, 10.0, 10.1, 10.1), "demand"), [])   # (the footer read: no cell there)
        remote.reads.clear()
        rows = o._fuel(remote, (-79.0, 5.1, -78.0, 5.3), "demand")
        self.assertEqual([r["name"] for r in rows], ["Name 11000"])
        bloom = [(s, e) for s, e, _ in remote.reads for a, b in self.blooms if s < b and e > a]
        self.assertEqual(bloom, [], "no bloom filter read")

    def test_warm_reads_the_index_only(self):
        # (2026-10-29: the warm-up was a cell at (0, 0, 1e-6, 1e-6): 1342 requests, 414 MB, never done as prefetch)
        import osmfuel
        import overture
        import rangeread
        o = osmfuel.OsmFuel(self.dir, None)
        o._reader = overture.Reader(self.dir, None)
        remote = rangeread.MemoryRange(self.data)
        o.version = lambda kind="demand": ("etag", remote)
        o.warm("prefetch")
        self.assertEqual(len(o._meta[1]), 20, "the row groups' index")
        self.assertTrue(remote.reads and all(s >= len(self.data) - overture.FOOTER_GUESS for s, e, k in remote.reads),
                        "the footer's bytes only")
        self.assertEqual({k for s, e, k in remote.reads}, {"prefetch"})


if __name__ == "__main__":
    unittest.main()
