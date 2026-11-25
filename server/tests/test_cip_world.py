"""The CIP planet builder's batched reads (tools/build_cip.py unit_ahead): the OSM fuel / picnic rows of a z10 unit's
cells read in one scan of their union (osmfuel.OsmFuel.cells_many, picnicsource.Source.store_many) are, cell by cell,
the rows the per-cell reads give (the server's own path: every CIP section must be what the server makes)."""
import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402

import mapcells  # noqa: E402
import mapfixtures as mf  # noqa: E402
import osmfuel  # noqa: E402
import overture  # noqa: E402
import picnicsource  # noqa: E402
import rangeread  # noqa: E402


class ETagTransport(mf.Transport):
    def request(self, url, headers, timeout):
        status, h, body = super().request(url, headers, timeout)
        if status in (200, 206):
            h = dict(h, ETag='"v1"')
        return status, h, body


def pois_parquet(tmp, cells, n=700, seed=5):
    """A Layercake-like POIs file: rows in and around cells (some exactly on their edges, some spanning two), float32
    boxes, small row groups."""
    import duckdb
    rng = np.random.default_rng(seed)
    b = np.array([c.bounds() for c in cells])
    lon0, lat0, lon1, lat1 = b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()
    edges_x = np.unique(np.concatenate([b[:, 0], b[:, 2]]))
    edges_y = np.unique(np.concatenate([b[:, 1], b[:, 3]]))
    kinds = [("fuel", None, None), ("charging_station", None, None), ("picnic_table", None, None),
             (None, "picnic_table", None), (None, None, "picnic_site"), ("bbq", None, None), ("cafe", None, None),
             (None, "park", None)]
    rows = []
    for i in range(n):
        x = rng.uniform(lon0 - 0.01, lon1 + 0.01)
        y = rng.uniform(lat0 - 0.01, lat1 + 0.01)
        if i % 7 == 0:
            x = float(edges_x[rng.integers(len(edges_x))])
        if i % 11 == 0:
            y = float(edges_y[rng.integers(len(edges_y))])
        w = 0.0 if i % 3 else rng.uniform(0, 0.03)
        am, le, to = kinds[i % len(kinds)]
        wkb = struct.pack("<BIdd", 1, 1, x, y)
        rows.append((1000 + i, am, le, to, f"brand{i % 5}" if i % 2 else None, f"name {i}", "roof" if i % 4 else None,
                     x, y, x + w, y + w / 2, wkb))
    con = duckdb.connect()
    con.execute("""CREATE TABLE p (id BIGINT, amenity VARCHAR, leisure VARCHAR, tourism VARCHAR, brand VARCHAR,
        name VARCHAR, building VARCHAR, x0 DOUBLE, y0 DOUBLE, x1 DOUBLE, y1 DOUBLE, geometry BLOB)""")
    con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    path = os.path.join(tmp, "pois.parquet")
    con.execute(f"""COPY (SELECT id, amenity, leisure, tourism, brand, name, building,
        {{'xmin': x0::FLOAT, 'ymin': y0::FLOAT, 'xmax': x1::FLOAT, 'ymax': y1::FLOAT}} AS bbox, geometry
        FROM p ORDER BY y0, x0) TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 60)""")
    con.close()
    with open(path, "rb") as f:
        return f.read()


class BatchedSideRowsTest(unittest.TestCase):
    def setUp(self):
        self._saved = (rangeread.BLOCK, overture.FOOTER_GUESS, overture.MERGE_GAP)
        rangeread.BLOCK, overture.FOOTER_GUESS, overture.MERGE_GAP = 4096, 8192, 1024
        self.tmp = tempfile.mkdtemp(prefix="cipworld-test-")
        x0, y0 = 4705, 6244
        self.cells = [mapcells.CellKey(14, x, y) for x in range(x0, x0 + 4) for y in range(y0, y0 + 4)]
        data = pois_parquet(self.tmp, self.cells)
        self.t = ETagTransport({osmfuel.URL: data})

    def tearDown(self):
        rangeread.BLOCK, overture.FOOTER_GUESS, overture.MERGE_GAP = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fresh(self, name):
        return os.path.join(self.tmp, name), mf.fetcher(self.t)

    def test_fuel_many_is_cell_by_cell(self):
        raw, f = self.fresh("a")
        one = osmfuel.OsmFuel(raw, f)
        want = [one.cell(c.bounds()) for c in self.cells]
        self.assertTrue(sum(map(len, want)) > 20, "the fixture has stations in the cells")
        raw, f = self.fresh("b")
        many = osmfuel.OsmFuel(raw, f)
        got = many.cells_many([c.bounds() for c in self.cells])
        self.assertEqual(got, want)
        self.assertEqual(many.cells_many([]), [])

    def test_picnic_many_is_cell_by_cell(self):
        raw, f = self.fresh("a")
        src = picnicsource.Source(overture.Reader(raw, f), f)
        etag, remote = src._version("demand")
        want = [src._query(remote, c.bounds(), "demand") for c in self.cells]
        self.assertTrue(sum(map(len, want)) > 20, "the fixture has picnic rows in the cells")
        raw, f = self.fresh("b")
        src = picnicsource.Source(overture.Reader(raw, f), f)
        self.assertEqual(src.store_many(self.cells), len(self.cells))
        etag, remote = src._version("demand")
        got = [picnicsource._rows_load(remote, c) for c in self.cells]
        self.assertEqual(got, want)
        self.assertEqual(src.store_many(self.cells), 0, "kept: not read again")


if __name__ == "__main__":
    unittest.main()
